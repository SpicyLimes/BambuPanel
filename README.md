# BambuPanel

A lightweight Ubuntu GNOME top-panel indicator for monitoring a Bambu Lab
3D printer over LAN (local network only, no cloud required). Optionally
controls a smart plug via Home Assistant for external power toggling.

> **Heads-up: printer connection limits.** Some Bambu Lab printers only allow a
> few local connections at the same time. Your slicer, Home Assistant, dashboards
> and BambuPanel each use one. If one of them suddenly can't connect, another is
> probably using the last available connection — close something you aren't
> using and try again.

## Panel Display

| State       | Icon                    | Label                             |
|-------------|-------------------------|-----------------------------------|
| Printing    | printer-3d-nozzle       | `67% · 23 Min`                   |
| Ready       | printer-3d              | `35°C · 28°C` (bed · nozzle)     |
| Offline     | printer-3d-off          | *(icon only, no label)*           |
| Error/Pause | printer-3d-nozzle-alert | `Error!`                          |

## Dropdown Menu

The menu organizes all available data into submenus:

- **Status** — print error code, or "Printer OK"
- **Temperatures** — nozzle, bed, chamber (with actual + target)
- **Print Job** — state, file, progress, time remaining, layer, speed
- **AMS Filament** — 4 slots, each with type, brand, color, remaining %
- **Hardware** — Wi-Fi signal, SD card, chamber light, nozzle info, fan speeds
- **⏻ Power** — toggle smart plug on/off via Home Assistant *(optional)*

All items can be individually shown or hidden via `show:` flags in `config.yaml`.

## Prerequisites

### 1. Printer: Enable LAN Mode
On the printer touchscreen: **Settings → Network → LAN Mode → ON**

Note your **LAN Access Code** and **Serial Number** from the same screen.

> **Developer Mode** (same menu) is only needed if you want full write access.
> Read-only monitoring works with LAN Only mode.

### 2. Ubuntu: Install system dependencies
```bash
sudo apt update
sudo apt install python3-gi python3-gi-cairo \
                 gir1.2-gtk-3.0 \
                 gir1.2-ayatana-appindicator3-0.1
```

> **GNOME Shell note:** If you're on vanilla GNOME (Ubuntu 22.04+), AppIndicator
> icons live in the system tray area and require the
> **AppIndicator and KStatusNotifierItem Support** GNOME Shell extension.
> Install it from https://extensions.gnome.org/extension/615/appindicator-support/

### 3. Install Python dependencies
```bash
pip install -r requirements.txt --break-system-packages
```

### 4. Configure
```bash
cp config.yaml.example config.yaml
nano config.yaml   # fill in printer_ip, access_code, serial
```

### 5. Run
```bash
python3 bambupanel.py
```

## Autostart on Login

Copy the desktop entry to your autostart folder:

```bash
cp bambupanel.desktop ~/.config/autostart/bambupanel.desktop
```

Edit `~/.config/autostart/bambupanel.desktop` and update the `Exec=` and
`Icon=` lines to point to your actual install path.

## Home Assistant Power Toggle (Optional)

To add smart plug toggles to the dropdown menu, add these keys to
`config.yaml`:

```yaml
ha_url:     "http://<your-ha-ip>:8123"
ha_token:   "your-long-lived-access-token"
ha_switch1: "switch.your_plug_entity_id"
ha_switch2: "switch.second_plug_entity_id"   # optional
ha_poll_interval: 30                         # optional, seconds between switch reads
```

- `ha_token`: create one in HA under **your profile → Security → Long-Lived Access Tokens**
- `ha_switch1` / `ha_switch2`: entity IDs of your smart plug switches in Home Assistant
- No extra pip packages required — uses Python's built-in `urllib`

Switch states are read on their own timer (`ha_poll_interval`, default 30 s,
minimum 10) and right after you toggle one.

**Protection against Home Assistant's IP ban:** HA can ban an IP address
after a few failed logins, and every app reaching HA from that address shares
the counter. BambuPanel's HA client (`ha_client.py`) sends one
request at a time, checks the token with a single `GET /api/` first, and on a
`401` (token rejected) or `403` (IP banned) stops sending anything. The
switches then show `HA token rejected` / `HA IP banned` and a **Retry Home
Assistant** item appears; nothing more is sent until you click it or use
**Reload BambuPanel**. A bad token costs one failed request, not one per poll.

If these keys are absent or the token is left as the placeholder, the toggle
is silently hidden.

## Troubleshooting

**Icon doesn't appear in panel:**
Install the AppIndicator GNOME extension (see step 2 above). Also confirm
`gir1.2-ayatana-appindicator3-0.1` is installed via apt.

**MQTT connection fails:**
- Confirm LAN Mode is enabled on the printer
- Confirm printer IP is reachable (`ping <printer_ip>`)
- Confirm access code and serial are correct in `config.yaml`
- Check that your printer isn't in Cloud Mode (must be LAN Only)

**Won't connect while the slicer is open (or the slicer can't connect while
BambuPanel runs):**
The printer may have run out of local connections — see the connection-limit
note at the top of this page. Close one of the other apps and try again.

**Switch shows "HA token rejected" or "HA IP banned":**
Home Assistant refused the request (401 / 403) and BambuPanel has stopped
contacting it.
- *Token rejected:* create a new Long-Lived Access Token, put it in
  `config.yaml`, then click **Retry Home Assistant**.
- *IP banned:* this machine's address is on HA's ban list, usually from an app
  that kept retrying a bad token. From a different device, remove the address
  from `ip_bans.yaml` in HA's config directory (the *File editor* add-on works),
  restart Home Assistant, fix the offending token, then click **Retry Home
  Assistant**.

**Switch shows "HA unreachable" or "Unavailable":**
- Confirm `ha_url` is reachable from this machine (`curl http://<ha_ip>:8123/api/`)
- "Unavailable" means HA answered but the entity ID is wrong or unavailable

**Label shows "Off" immediately:**
The printer may be sleeping or powered down. The indicator will reconnect
automatically when the printer becomes reachable.

## Project Structure

```
BambuPanel/
├── bambupanel.py          # Main indicator script
├── ha_client.py           # Home Assistant client with IP-ban protection
├── config.yaml            # Your printer config (git-ignored)
├── config.yaml.example    # Config template
├── requirements.txt       # Python dependencies
├── bambupanel.desktop     # Autostart entry (git-ignored, contains local paths)
└── icons/
    ├── printing.png       # MDI: printer-3d-nozzle (22px white)
    ├── ready.png          # MDI: printer-3d (22px white)
    ├── offline.png        # MDI: printer-3d-off (22px white)
    └── error.png          # MDI: printer-3d-nozzle-alert (22px white)
```

## MQTT Details

BambuPanel connects to `mqtts://<printer_ip>:8883` using:
- Username: `bblp`
- Password: your LAN Access Code
- TLS: enabled, cert verification disabled (Bambu uses self-signed certs)
- Subscribe topic: `device/<serial>/report`
- Publish topic: `device/<serial>/request` (used once on connect for `pushall`)

`gcode_state` values and how they are displayed:

| Value | Display |
|---|---|
| `running`, `prepare`, `slicing`, `init` | Printing — `67% · 23 Min` |
| `idle`, `finish` | Ready — `35°C · 28°C` |
| `failed`, `pause` | Error — `Error!` |
| no connection / watchdog timeout | Offline — icon only |

> **Bambu firmware note (January 2025):** Bambu Lab introduced an Authorization
> Control System in firmware updates for some models. Read-only MQTT monitoring
> (subscribing to the `report` topic) is explicitly unaffected. LAN Mode keeps
> the full local API accessible.

## Security & Disclaimer

> **Use at your own risk.**
>
> Enabling LAN Mode on your Bambu Lab printer exposes an MQTT broker on your local network. Historically, IoT device MQTT interfaces have been a vector for unauthorized access, credential theft, and network pivoting when misconfigured or exposed beyond a trusted LAN. BambuPanel disables TLS certificate verification by design (Bambu uses self-signed certificates); connections should never be routed over untrusted networks.
>
> This project was developed with AI assistance ("vibe coding") and has **not** undergone a formal security audit. The code is provided as-is, without warranty of any kind. You are solely responsible for reviewing the source code, assessing its suitability for your environment, and ensuring it does not introduce risk to your network or devices before use.
>
> **Recommended precautions:**
> - Ensure to firewall all sensitive information/software/hardware behind your router
> - Never forward MQTT port 8883 through your router to the internet
> - Treat your LAN Access Code as a credential — rotate it if compromised
> - Review `bambupanel.py` in full before running it on any system

## License

MIT — see [LICENSE](LICENSE) for details.

Panel icons are derived from [Material Design Icons](https://materialdesignicons.com) (Apache 2.0).

> BambuPanel is an independent, unofficial project and is not affiliated with or endorsed by Bambu Lab.

## Credits

See [Credits](CREDITS.md) for more information.

- **Bambu Lab** — MQTT API and developer documentation
- **[OpenBambuAPI](https://github.com/Doridian/OpenBambuAPI)** — community MQTT payload field reference (MIT)
- **[Material Design Icons](https://materialdesignicons.com)** — panel icon SVG sources (Apache 2.0)
- **[Claude / Anthropic](https://anthropic.com)** — AI assistance in design and development