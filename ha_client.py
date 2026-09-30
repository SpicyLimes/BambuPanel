"""Home Assistant REST client that cannot trip HA's IP ban.

Home Assistant bans an IP address after a handful of failed logins
(``login_attempts_threshold``), and every app that reaches HA from the same
address shares that one counter. A client that keeps retrying a bad token, or
fires several requests in parallel, can get a whole machine locked out.

The rules this client follows:

* **One request at a time.** All calls are serialised by a lock, so a bad
  token can never produce a parallel burst of failures.
* **Probe first.** The first call (and the first after any failure) is a
  cheap ``GET /api/``. Entity requests are only sent once that succeeds.
* **Latch on 401/403.** A rejected token (401) or a banned address (403)
  will not fix itself, so the client stops sending *anything* until
  :meth:`retry` is called. That's one failed request per launch, at most.
* **Network errors are not latched.** A timeout or refused connection never
  reaches HA's auth check, so retrying on the next poll is harmless.
"""
from __future__ import annotations

import enum
import json
import threading
import urllib.error
import urllib.request


class HAStatus(enum.Enum):
    UNKNOWN = "unknown"                # not probed yet
    OK = "ok"
    TOKEN_REJECTED = "token_rejected"  # 401 — latched
    IP_BANNED = "ip_banned"            # 403 — latched
    UNREACHABLE = "unreachable"        # network error — retried next poll


_STATUS_TEXT = {
    HAStatus.UNKNOWN: "Checking…",
    HAStatus.OK: "OK",
    HAStatus.TOKEN_REJECTED: "HA token rejected",
    HAStatus.IP_BANNED: "HA IP banned",
    HAStatus.UNREACHABLE: "HA unreachable",
}


class _Latched(Exception):
    """Raised internally when a call is refused because the client is latched."""


class HAClient:
    def __init__(self, base_url: str, token: str, timeout: float = 5):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout
        self.status = HAStatus.UNKNOWN
        self._lock = threading.Lock()

    # ── public ──────────────────────────────────────────────────────────────

    @property
    def blocked(self) -> bool:
        return self.status in (HAStatus.TOKEN_REJECTED, HAStatus.IP_BANNED)

    def status_text(self) -> str:
        return _STATUS_TEXT[self.status]

    def retry(self) -> None:
        """User-initiated: allow exactly one fresh probe."""
        with self._lock:
            self.status = HAStatus.UNKNOWN

    def get_state(self, entity_id: str) -> str | None:
        """Return the entity's state string, or None on any failure."""
        try:
            data = self._call("GET", f"/api/states/{entity_id}")
        except (_Latched, OSError, ValueError):
            return None
        return data.get("state") if isinstance(data, dict) else None

    def toggle(self, entity_id: str) -> bool:
        """Toggle a switch entity. Returns True if HA accepted the call."""
        body = json.dumps({"entity_id": entity_id}).encode()
        try:
            self._call("POST", "/api/services/switch/toggle", body)
        except (_Latched, OSError, ValueError):
            return False
        return True

    # ── internals ───────────────────────────────────────────────────────────

    def _call(self, method: str, path: str, body: bytes | None = None):
        with self._lock:
            if self.blocked:
                raise _Latched()
            if self.status is not HAStatus.OK:
                self._send("GET", "/api/")
                self.status = HAStatus.OK
            return self._send(method, path, body)

    def _send(self, method: str, path: str, body: bytes | None = None):
        """One HTTP request. Caller must hold the lock."""
        req = urllib.request.Request(f"{self.base_url}{path}", data=body, method=method)
        req.add_header("Authorization", f"Bearer {self.token}")
        req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as e:
            if e.code == 401:
                self.status = HAStatus.TOKEN_REJECTED
                print("[BambuPanel] HA rejected the token (401) — HA requests "
                      "paused until 'Retry Home Assistant'.")
                raise _Latched() from None
            if e.code == 403:
                self.status = HAStatus.IP_BANNED
                print("[BambuPanel] HA refused this IP (403, likely banned) — HA "
                      "requests paused until 'Retry Home Assistant'.")
                raise _Latched() from None
            raise    # e.g. 404 for a mistyped entity — not an auth failure
        except (urllib.error.URLError, OSError):
            self.status = HAStatus.UNREACHABLE
            raise
        return json.loads(raw.decode()) if raw else None
