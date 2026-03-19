#!/usr/bin/env python3
"""
BambuPanel — Ubuntu top-panel indicator for Bambu Lab 3D printer monitoring.
Connects locally via MQTT (LAN Only / Developer Mode) and displays print
progress, time remaining, and temperatures in the GNOME top panel.

Requires: paho-mqtt, PyYAML, python3-gi, gir1.2-ayatana-appindicator3-0.1
"""

import gi
gi.require_version("Gtk", "3.0")
gi.require_version("AyatanaAppIndicator3", "0.1")

from gi.repository import Gtk, GLib, AyatanaAppIndicator3 as AppIndicator

import ssl
import json
import threading
import time
import os
import sys
import yaml
import urllib.request
import paho.mqtt.client as mqtt

# ── Paths ────────────────────────────────────────────────────────────────────

SCRIPT_DIR  = os.path.dirname(os.path.realpath(__file__))
ICONS_DIR   = os.path.join(SCRIPT_DIR, "icons")
CONFIG_PATH = os.path.join(SCRIPT_DIR, "config.yaml")

ICON = {
    "printing": os.path.join(ICONS_DIR, "printing.png"),
    "ready":    os.path.join(ICONS_DIR, "ready.png"),
    "offline":  os.path.join(ICONS_DIR, "offline.png"),
    "error":    os.path.join(ICONS_DIR, "error.png"),
}

# ── Config ───────────────────────────────────────────────────────────────────

SHOW_DEFAULTS = {
    "nozzle_temp":   True,  "bed_temp":      True,  "chamber_temp":  False,
    "job_state":     True,  "job_file":      True,  "job_progress":  True,
    "job_remaining": True,  "job_layers":    True,  "job_speed":     True,
    "ams_type":      True,  "ams_brand":     True,  "ams_color":     True,
    "ams_remain":    True,  "wifi_signal":   True,  "sdcard":        True,
    "chamber_light": True,  "nozzle_info":   True,  "fan_heatbreak": True,
    "fan_cooling":   True,  "fan_aux":       True,
}

def load_config():
    if not os.path.exists(CONFIG_PATH):
        print(f"[BambuPanel] Config not found at {CONFIG_PATH}")
        print("  Copy config.yaml.example to config.yaml and fill in your printer details.")
        sys.exit(1)
    with open(CONFIG_PATH, "r") as f:
        cfg = yaml.safe_load(f)
    required = ["printer_ip", "access_code", "serial"]
    for key in required:
        if not cfg.get(key):
            print(f"[BambuPanel] Missing required config key: '{key}'")
            sys.exit(1)
    # Merge show flags with defaults so missing keys always have a value
    show = {**SHOW_DEFAULTS, **(cfg.get("show") or {})}
    cfg["show"] = show
    return cfg

# ── Helpers ──────────────────────────────────────────────────────────────────

def format_time(minutes: int) -> str:
    """Format minutes into '23 Min' or '1 Hr 23 Min'."""
    if minutes < 60:
        return f"{minutes} Min"
    hours = minutes // 60
    mins  = minutes % 60
    if mins == 0:
        return f"{hours} Hr"
    return f"{hours} Hr {mins} Min"

def hex_to_rgb_name(hex_color: str) -> str:
    h = hex_color.lstrip("#")
    if len(h) >= 6:
        r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
        return f"#{h[:6].upper()}  ({r},{g},{b})"
    return hex_color

def fan_pct(raw: str) -> str:
    try:
        return f"{round(int(raw) / 15 * 100)}%"
    except (ValueError, TypeError):
        return raw or "—"

SPEED_LEVELS = {1: "Silent", 2: "Standard", 3: "Sport", 4: "Ludicrous"}

# ── Home Assistant Client ─────────────────────────────────────────────────────

class HAClient:
    """Minimal HA REST API client for reading/toggling a switch entity."""

    def __init__(self, base_url: str, token: str, entity_id: str, entity_id2: str = None):
        self.base_url   = base_url.rstrip("/")
        self.token      = token
        self.entity_id  = entity_id
        self.entity_id2 = entity_id2

    def _request(self, method: str, path: str, body: bytes = None):
        url = f"{self.base_url}{path}"
        req = urllib.request.Request(url, data=body, method=method)
        req.add_header("Authorization", f"Bearer {self.token}")
        req.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(req, timeout=5) as resp:
            return json.loads(resp.read().decode())

    def get_state(self, entity_id: str = None) -> str | None:
        """Return 'on', 'off', or None on error."""
        eid = entity_id or self.entity_id
        try:
            data = self._request("GET", f"/api/states/{eid}")
            return data.get("state")
        except Exception as e:
            print(f"[BambuPanel] HA get_state error: {e}")
            return None

    def toggle(self, entity_id: str = None):
        """Toggle the switch via the HA service call API."""
        eid = entity_id or self.entity_id
        try:
            body = json.dumps({"entity_id": eid}).encode()
            self._request("POST", "/api/services/switch/toggle", body)
        except Exception as e:
            print(f"[BambuPanel] HA toggle error: {e}")

# ── Printer State ─────────────────────────────────────────────────────────────

class AMSTray:
    def __init__(self):
        self.tray_type      = ""
        self.tray_sub_brand = ""
        self.color          = ""
        self.remain         = -1

class PrinterState:
    def __init__(self):
        self.reset()

    def reset(self):
        self.connected       = False
        self.gcode_state     = None
        self.mc_percent      = 0
        self.mc_remaining    = 0
        self.layer_num       = 0
        self.total_layer_num = 0
        self.bed_temp        = 0.0
        self.bed_target      = 0.0
        self.nozzle_temp     = 0.0
        self.nozzle_target   = 0.0
        self.chamber_temp    = 0.0
        self.nozzle_diameter = ""
        self.nozzle_type     = ""
        self.spd_lvl         = 0
        self.spd_mag         = 100
        self.subtask_name    = ""
        self.print_error     = 0
        self.wifi_signal     = ""
        self.sdcard          = None
        self.chamber_light   = ""
        self.fan_heatbreak   = ""
        self.fan_cooling     = ""
        self.fan_aux         = ""
        self.ams_trays       = {}
        self.last_update     = 0.0

    def is_printing(self):
        return self.gcode_state in ("running", "prepare", "slicing", "init")

    def is_ready(self):
        return self.gcode_state in ("idle", "finish")

    def is_error(self):
        return self.gcode_state == "failed"

    def is_paused(self):
        return self.gcode_state == "pause"

# ── MQTT Client ───────────────────────────────────────────────────────────────

class BambuMQTT:
    MQTT_PORT    = 8883
    KEEPALIVE    = 60
    OFFLINE_SECS = 15

    def __init__(self, cfg, state: PrinterState, on_update):
        self.cfg           = cfg
        self.state         = state
        self.on_update     = on_update
        self._client       = None
        self._thread       = None
        self._stop         = threading.Event()
        self.topic_report  = f"device/{cfg['serial']}/report"
        self.topic_request = f"device/{cfg['serial']}/request"

    def _build_client(self):
        try:
            client = mqtt.Client(
                mqtt.CallbackAPIVersion.VERSION1,
                client_id=f"bambupanel_{int(time.time())}",
                protocol=mqtt.MQTTv311,
                clean_session=True,
            )
        except AttributeError:
            client = mqtt.Client(
                client_id=f"bambupanel_{int(time.time())}",
                protocol=mqtt.MQTTv311,
                clean_session=True,
            )
        client.username_pw_set("bblp", self.cfg["access_code"])
        tls_ctx = ssl.create_default_context()
        tls_ctx.check_hostname = False
        tls_ctx.verify_mode    = ssl.CERT_NONE
        client.tls_set_context(tls_ctx)
        client.on_connect    = self._on_connect
        client.on_disconnect = self._on_disconnect
        client.on_message    = self._on_message
        return client

    def _on_connect(self, client, userdata, flags, rc):
        if rc == 0:
            print("[BambuPanel] MQTT Connected.")
            client.subscribe(self.topic_report)
            payload = json.dumps({"pushing": {"sequence_id": "0", "command": "pushall"}})
            client.publish(self.topic_request, payload)
        else:
            print(f"[BambuPanel] MQTT Connect Failed, rc={rc}")
            self.state.connected = False
            GLib.idle_add(self.on_update)

    def _on_disconnect(self, client, userdata, rc):
        print(f"[BambuPanel] MQTT Disconnected (rc={rc}).")
        self.state.connected = False
        GLib.idle_add(self.on_update)

    def _on_message(self, client, userdata, msg):
        try:
            payload = json.loads(msg.payload.decode())
        except (json.JSONDecodeError, UnicodeDecodeError):
            return

        p = payload.get("print", {})
        if not p:
            return

        self.state.connected   = True
        self.state.last_update = time.monotonic()

        def getf(k):
            v = p.get(k)
            return round(float(v), 1) if v is not None else None

        def geti(k):
            v = p.get(k)
            return int(v) if v is not None else None

        if "gcode_state"          in p: self.state.gcode_state     = p["gcode_state"].lower()
        if "mc_percent"           in p: self.state.mc_percent       = geti("mc_percent")
        if "mc_remaining_time"    in p: self.state.mc_remaining     = geti("mc_remaining_time")
        if "layer_num"            in p: self.state.layer_num        = geti("layer_num")
        if "total_layer_num"      in p: self.state.total_layer_num  = geti("total_layer_num")
        if "bed_temper"           in p: self.state.bed_temp         = getf("bed_temper")
        if "bed_target_temper"    in p: self.state.bed_target       = getf("bed_target_temper")
        if "nozzle_temper"        in p: self.state.nozzle_temp      = getf("nozzle_temper")
        if "nozzle_target_temper" in p: self.state.nozzle_target    = getf("nozzle_target_temper")
        if "chamber_temper"       in p: self.state.chamber_temp     = getf("chamber_temper")
        if "nozzle_diameter"      in p: self.state.nozzle_diameter  = str(p["nozzle_diameter"])
        if "nozzle_type"          in p: self.state.nozzle_type      = str(p["nozzle_type"]).replace("_", " ").title()
        if "spd_lvl"              in p: self.state.spd_lvl          = geti("spd_lvl")
        if "spd_mag"              in p: self.state.spd_mag          = geti("spd_mag")
        if "subtask_name"         in p: self.state.subtask_name     = p.get("subtask_name") or ""
        if "print_error"          in p: self.state.print_error      = geti("print_error")
        if "wifi_signal"          in p: self.state.wifi_signal      = p.get("wifi_signal") or ""
        if "sdcard"               in p: self.state.sdcard           = bool(p["sdcard"])
        if "heatbreak_fan_speed"  in p: self.state.fan_heatbreak    = str(p["heatbreak_fan_speed"])
        if "cooling_fan_speed"    in p: self.state.fan_cooling      = str(p["cooling_fan_speed"])
        if "big_fan1_speed"       in p: self.state.fan_aux          = str(p["big_fan1_speed"])

        if "lights_report" in p:
            for light in p["lights_report"]:
                if light.get("node") == "chamber_light":
                    self.state.chamber_light = light.get("mode", "")

        if "ams" in p:
            for ams_unit in p["ams"].get("ams", []):
                ams_id = int(ams_unit.get("id", 0))
                for tray_data in ams_unit.get("tray", []):
                    tray_id = int(tray_data.get("id", 0))
                    tray = AMSTray()
                    tray.tray_type      = tray_data.get("tray_type", "")
                    tray.tray_sub_brand = tray_data.get("tray_sub_brands", "")
                    tray.color          = tray_data.get("tray_color", "")
                    remain              = tray_data.get("remain")
                    tray.remain         = int(remain) if remain is not None else -1
                    self.state.ams_trays[(ams_id, tray_id)] = tray

        GLib.idle_add(self.on_update)

    def _watchdog(self):
        while not self._stop.is_set():
            time.sleep(5)
            if self.state.connected and self.state.last_update > 0:
                elapsed = time.monotonic() - self.state.last_update
                if elapsed > self.OFFLINE_SECS:
                    print(f"[BambuPanel] No MQTT Message for {elapsed:.0f}s — Marking as Offline")
                    self.state.connected = False
                    GLib.idle_add(self.on_update)

    def start(self):
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        threading.Thread(target=self._watchdog, daemon=True).start()

    def _run(self):
        while not self._stop.is_set():
            try:
                self._client = self._build_client()
                self._client.connect(self.cfg["printer_ip"], self.MQTT_PORT, self.KEEPALIVE)
                self._client.loop_forever()
            except Exception as e:
                print(f"[BambuPanel] MQTT Error: {e}. Retrying in 10s...")
                self.state.connected = False
                GLib.idle_add(self.on_update)
                time.sleep(10)

    def stop(self):
        self._stop.set()
        if self._client:
            try:
                self._client.disconnect()
            except Exception:
                pass
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5)

# ── Indicator ─────────────────────────────────────────────────────────────────

class BambuPanel:
    APP_ID = "bambupanel"

    # Slot keys in display order
    AMS_SLOTS = [(0, 0), (0, 1), (0, 2), (0, 3)]

    def __init__(self):
        self.cfg   = load_config()
        self.state = PrinterState()

        # Optional Home Assistant client
        ha_url    = self.cfg.get("ha_url")
        ha_token  = self.cfg.get("ha_token")
        ha_switch1 = self.cfg.get("ha_switch1")
        ha_switch2 = self.cfg.get("ha_switch2")
        if ha_url and ha_token and ha_switch1 and ha_token != "YOUR_LONG_LIVED_ACCESS_TOKEN":
            self.ha = HAClient(ha_url, ha_token, ha_switch1, ha_switch2 or None)
        else:
            self.ha = None

        self.indicator = AppIndicator.Indicator.new(
            self.APP_ID,
            ICON["offline"],
            AppIndicator.IndicatorCategory.APPLICATION_STATUS,
        )
        self.indicator.set_status(AppIndicator.IndicatorStatus.ACTIVE)
        self.indicator.set_label("  Off", "  Off")

        self._build_menu()

        self.mqtt = BambuMQTT(self.cfg, self.state, self._refresh)
        self.mqtt.start()

    # ── Menu build (runs once) ────────────────────────────────────────────────

    def _static_item(self, label: str) -> Gtk.MenuItem:
        item = Gtk.MenuItem(label=label)
        item.set_sensitive(False)
        return item

    def _build_menu(self):
        show = self.cfg["show"]
        menu = Gtk.Menu()

        # Status
        self.m_status = self._static_item("✓  Printer OK")
        menu.append(self.m_status)
        menu.append(Gtk.SeparatorMenuItem())

        # ── Temperatures submenu ──────────────────────────────────────────────
        temp_sub = Gtk.Menu()
        self.m_nozzle_temp  = self.m_bed_temp = self.m_chamber_temp = None
        if show["nozzle_temp"]:
            self.m_nozzle_temp  = self._static_item("")
            temp_sub.append(self.m_nozzle_temp)
        if show["bed_temp"]:
            self.m_bed_temp     = self._static_item("")
            temp_sub.append(self.m_bed_temp)
        if show["chamber_temp"]:
            self.m_chamber_temp = self._static_item("")
            temp_sub.append(self.m_chamber_temp)
        if temp_sub.get_children():
            temp_header = Gtk.MenuItem(label="Temperatures")
            temp_header.set_submenu(temp_sub)
            menu.append(temp_header)

        # ── Print Job submenu ─────────────────────────────────────────────────
        job_sub = Gtk.Menu()
        self.m_job_state = self.m_job_file = self.m_job_progress = None
        self.m_job_remaining = self.m_job_layers = self.m_job_speed = None
        for key, attr in (
            ("job_state",     "m_job_state"),
            ("job_file",      "m_job_file"),
            ("job_progress",  "m_job_progress"),
            ("job_remaining", "m_job_remaining"),
            ("job_layers",    "m_job_layers"),
            ("job_speed",     "m_job_speed"),
        ):
            if show[key]:
                item = self._static_item("")
                setattr(self, attr, item)
                job_sub.append(item)
        if job_sub.get_children():
            job_header = Gtk.MenuItem(label="Print Job")
            job_header.set_submenu(job_sub)
            menu.append(job_header)

        # ── AMS Filament submenu ──────────────────────────────────────────────
        ams_sub = Gtk.Menu()
        self.m_ams_slots = {}
        slot_names = {(0,0): "Slot 1", (0,1): "Slot 2", (0,2): "Slot 3", (0,3): "Slot 4"}
        for slot_key in self.AMS_SLOTS:
            slot_sub = Gtk.Menu()
            items = {}
            for key, field in (
                ("ams_type",   "type"),
                ("ams_brand",  "brand"),
                ("ams_color",  "color"),
                ("ams_remain", "remain"),
            ):
                if show[key]:
                    item = self._static_item("")
                    items[field] = item
                    slot_sub.append(item)
            slot_item = Gtk.MenuItem(label=slot_names[slot_key])
            slot_item.set_submenu(slot_sub)
            ams_sub.append(slot_item)
            self.m_ams_slots[slot_key] = items
        ams_header = Gtk.MenuItem(label="AMS Filament")
        ams_header.set_submenu(ams_sub)
        menu.append(ams_header)

        # ── Hardware submenu ──────────────────────────────────────────────────
        hw_sub = Gtk.Menu()
        self.m_hw_wifi = self.m_hw_sdcard = self.m_hw_light = None
        self.m_hw_nozzle = self.m_hw_fan_hb = self.m_hw_fan_cool = self.m_hw_fan_aux = None
        for key, attr in (
            ("wifi_signal",   "m_hw_wifi"),
            ("sdcard",        "m_hw_sdcard"),
            ("chamber_light", "m_hw_light"),
            ("nozzle_info",   "m_hw_nozzle"),
            ("fan_heatbreak", "m_hw_fan_hb"),
            ("fan_cooling",   "m_hw_fan_cool"),
            ("fan_aux",       "m_hw_fan_aux"),
        ):
            if show[key]:
                item = self._static_item("")
                setattr(self, attr, item)
                hw_sub.append(item)
        if hw_sub.get_children():
            hw_header = Gtk.MenuItem(label="Hardware")
            hw_header.set_submenu(hw_sub)
            menu.append(hw_header)

        menu.append(Gtk.SeparatorMenuItem())

        # ── External Power Toggle (Home Assistant) ────────────────────────────
        self.m_power_toggle = None
        self.m_power_toggle2 = None
        if self.ha:
            self.m_power_toggle = Gtk.MenuItem(label="⏻  Switch 1:  Checking…")
            self.m_power_toggle.connect("activate", self._on_power_toggle)
            menu.append(self.m_power_toggle)
            if self.ha.entity_id2:
                self.m_power_toggle2 = Gtk.MenuItem(label="⏻  Switch 2:  Checking…")
                self.m_power_toggle2.connect("activate", self._on_power_toggle2)
                menu.append(self.m_power_toggle2)
            menu.append(Gtk.SeparatorMenuItem())

        # ── Actions ───────────────────────────────────────────────────────────
        item_reload = Gtk.MenuItem(label="Reload BambuPanel")
        item_reload.connect("activate", self._on_reload)
        menu.append(item_reload)

        item_quit = Gtk.MenuItem(label="Quit")
        item_quit.connect("activate", self._on_quit)
        menu.append(item_quit)

        menu.show_all()
        self.indicator.set_menu(menu)

    # ── Refresh (runs on every MQTT update — updates labels only) ─────────────

    def _refresh(self):
        self._refresh_label()
        self._refresh_menu()

    def _refresh_label(self):
        s = self.state

        if not s.connected:
            self.indicator.set_icon_full(ICON["offline"], "Printer Offline")
            self.indicator.set_label("", "")
            return

        if s.is_error() or s.is_paused():
            self.indicator.set_icon_full(ICON["error"], "Printer Error")
            self.indicator.set_label("  Error!", "  Error!")
            return

        if s.is_printing():
            label = f"  {s.mc_percent}% · {format_time(s.mc_remaining)}"
            self.indicator.set_icon_full(ICON["printing"], "Printing")
            self.indicator.set_label(label, label)
            return

        if s.is_ready():
            label = f"  {s.bed_temp:.0f}°C · {s.nozzle_temp:.0f}°C"
            self.indicator.set_icon_full(ICON["ready"], "Ready")
            self.indicator.set_label(label, label)
            return

        self.indicator.set_icon_full(ICON["ready"], "Online")
        self.indicator.set_label("  Online", "  Online")

    def _refresh_menu(self):
        s = self.state

        # Status
        if s.print_error:
            self.m_status.set_label(f"⚠  Error code: {s.print_error}")
        else:
            self.m_status.set_label("✓  Printer OK")

        # Temperatures
        if self.m_nozzle_temp:
            self.m_nozzle_temp.set_label(
                f"Nozzle:    {s.nozzle_temp:.0f}°C  (target {s.nozzle_target:.0f}°C)")
        if self.m_bed_temp:
            self.m_bed_temp.set_label(
                f"Bed:       {s.bed_temp:.0f}°C  (target {s.bed_target:.0f}°C)")
        if self.m_chamber_temp:
            self.m_chamber_temp.set_label(f"Chamber:   {s.chamber_temp:.0f}°C")

        # Print Job
        state_str = (s.gcode_state or "unknown").title()
        if self.m_job_state:
            self.m_job_state.set_label(f"State:     {state_str}")
        if self.m_job_file:
            self.m_job_file.set_label(f"File:      {s.subtask_name or '—'}")
        if self.m_job_progress:
            self.m_job_progress.set_label(f"Progress:  {s.mc_percent}%")
        if self.m_job_remaining:
            self.m_job_remaining.set_label(f"Remaining: {format_time(s.mc_remaining)}")
        if self.m_job_layers:
            layers = f"{s.layer_num} / {s.total_layer_num}" if s.total_layer_num else "—"
            self.m_job_layers.set_label(f"Layer:     {layers}")
        if self.m_job_speed:
            spd_name = SPEED_LEVELS.get(s.spd_lvl, f"Level {s.spd_lvl}")
            self.m_job_speed.set_label(f"Speed:     {spd_name}  ({s.spd_mag}%)")

        # AMS slots
        for slot_key, items in self.m_ams_slots.items():
            tray = s.ams_trays.get(slot_key)
            filled = tray and tray.tray_type
            if "type" in items:
                items["type"].set_label(f"Type:      {tray.tray_type}" if filled else "Empty")
            if "brand" in items:
                items["brand"].set_label(f"Brand:     {tray.tray_sub_brand or '—'}" if filled else "")
            if "color" in items:
                color_str = hex_to_rgb_name(tray.color) if (filled and tray.color) else "—"
                items["color"].set_label(f"Color:     {color_str}" if filled else "")
            if "remain" in items:
                remain_str = f"{tray.remain}%" if (filled and tray.remain >= 0) else "—"
                items["remain"].set_label(f"Remaining: {remain_str}" if filled else "")

        # Hardware
        sdcard_str = ("Yes" if s.sdcard else "No") if s.sdcard is not None else "—"
        nozzle_str = f"{s.nozzle_diameter} mm  {s.nozzle_type}" if s.nozzle_diameter else "—"
        if self.m_hw_wifi:     self.m_hw_wifi.set_label(    f"Wi-Fi:       {s.wifi_signal or '—'}")
        if self.m_hw_sdcard:   self.m_hw_sdcard.set_label(  f"SD Card:     {sdcard_str}")
        if self.m_hw_light:    self.m_hw_light.set_label(   f"Light:       {s.chamber_light.title() or '—'}")
        if self.m_hw_nozzle:   self.m_hw_nozzle.set_label(  f"Nozzle:      {nozzle_str}")
        if self.m_hw_fan_hb:   self.m_hw_fan_hb.set_label(  f"Fan (HB):    {fan_pct(s.fan_heatbreak)}")
        if self.m_hw_fan_cool: self.m_hw_fan_cool.set_label(f"Fan (Cool):  {fan_pct(s.fan_cooling)}")
        if self.m_hw_fan_aux:  self.m_hw_fan_aux.set_label( f"Fan (Aux):   {fan_pct(s.fan_aux)}")

        # External power toggle — refresh labels in background to avoid blocking GTK
        if self.m_power_toggle:
            threading.Thread(target=self._refresh_power_label, daemon=True).start()
        if self.m_power_toggle2:
            threading.Thread(target=self._refresh_power_label2, daemon=True).start()

    def _refresh_power_label(self):
        state = self.ha.get_state(self.ha.entity_id)
        if state == "on":
            label = "⏻  Switch 1:  ON"
        elif state == "off":
            label = "⏻  Switch 1:  OFF"
        else:
            label = "⏻  Switch 1:  Offline"
        GLib.idle_add(self.m_power_toggle.set_label, label)

    def _refresh_power_label2(self):
        state = self.ha.get_state(self.ha.entity_id2)
        if state == "on":
            label = "⏻  Switch 2:  ON"
        elif state == "off":
            label = "⏻  Switch 2:  OFF"
        else:
            label = "⏻  Switch 2:  Offline"
        GLib.idle_add(self.m_power_toggle2.set_label, label)

    # ── Actions ───────────────────────────────────────────────────────────────

    def _on_power_toggle(self, _widget):
        if not self.ha:
            return
        self.m_power_toggle.set_label("⏻  Switch 1:  Toggling…")
        def _do_toggle():
            self.ha.toggle(self.ha.entity_id)
            self._refresh_power_label()
        threading.Thread(target=_do_toggle, daemon=True).start()

    def _on_power_toggle2(self, _widget):
        if not self.ha or not self.ha.entity_id2:
            return
        self.m_power_toggle2.set_label("⏻  Switch 2:  Toggling…")
        def _do_toggle():
            self.ha.toggle(self.ha.entity_id2)
            self._refresh_power_label2()
        threading.Thread(target=_do_toggle, daemon=True).start()

    def _on_reload(self, _widget):
        print("[BambuPanel] Reloading...")
        self.mqtt.stop()
        self.state.reset()
        self._refresh()
        self.mqtt = BambuMQTT(self.cfg, self.state, self._refresh)
        self.mqtt.start()

    def _on_quit(self, _widget):
        print("[BambuPanel] Quitting.")
        self.mqtt.stop()
        Gtk.main_quit()

    def run(self):
        Gtk.main()


# ── Entry Point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    panel = BambuPanel()
    panel.run()
