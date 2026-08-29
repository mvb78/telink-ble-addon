# Installing on Home Assistant OS (add-on + companion integration)

This guide installs the two pieces that bring Telink `Smart_qXsx` lamps into Home
Assistant:

1. **Add-on** (`telink-addon/`) — a Supervisor add-on that runs the BLE daemon +
   web UI in a container on your HAOS host. It owns the Bluetooth connections to
   the lamps.
2. **Companion integration** (`custom_components/telink_ble/`) — polls the add-on
   REST API and exposes each lamp and group as native `light.*` entities.

Both live in the same repository:

```
telink-ble-addon/
├── telink-addon/                 # Add-on (Supervisor)
└── custom_components/telink_ble/ # Home Assistant integration
```

---

## Prerequisites

- Home Assistant OS (or Supervised) with a Bluetooth adapter. Built-in BT or a
  USB dongle both work — the add-on uses the **host's** Bluetooth stack.
  (`host_dbus: true` + `usb: true` in the add-on manifest.)
- The phone/lamp app must be **disconnected** from the lamps.

---

## 1. Install the add-on

1. In Home Assistant go to **Settings → System → Add-ons → Add-on store**.
2. Click the ⋮ menu (top-right) → **Repositories**, add this repository URL:

   ```
   https://github.com/mvb78/telink-ble-addon
   ```

   (In a private repo you must clone it locally and push to GitHub first — see
   `README.md` → "Repository layout".)

3. The **Telink BLE CLI** add-on appears. Click it → **Install**.
4. Optional, before starting:
   - **known_passwords** — comma-separated list of mesh passwords to try during
     discovery (default `0000,1234,123`). Add yours if your lamps use a different
     password.
   - **scan_timeout** — seconds to scan during discovery (default `45`).
5. Start the add-on. On first run there are no saved lamps, so the daemon starts,
   sees an empty lamp database and waits (it retries every few seconds).

### Discover lamps from the add-on web UI

1. Open the add-on by clicking **OPEN WEB UI** (Ingress).
2. Click **Discover** (top-right). This scans (≤ `scan_timeout`s) with the
   configured passwords and saves the lamps to the add-on's persistent
   `/data/lamps.json`.
3. The daemon picks the lamps up on its next start attempt and connects to all of
   them — the header shows how many are connected.
4. The standalone web UI is now fully functional (on/off, brightness, color temp,
   scenes, ...). You can stop here if you don't want Home Assistant entities.

> If you add more lamps later, re-run **Discover** — existing lamps are kept and
> new ones are added.

---

## 2. Install the companion integration

1. Copy the `custom_components/telink_ble/` directory into your HA
   `config/custom_components/` directory (or clone the repo there):

   ```bash
   cd /config
   git clone https://github.com/mvb78/telink-ble-addon.git
   # then copy:
   cp -r telink-ble-addon/custom_components/telink_ble custom_components/
   ```

2. Restart Home Assistant.
3. Go to **Settings → Devices & Services → Add Integration**, search for
   **Telink BLE Lights**.
4. The flow pre-fills the add-on host. The add-on publishes its API on port
   **8099** in the host network namespace, so the integration normally uses the
   HA host's own LAN IP (or `host.docker.internal`, or `127.0.0.1` as a last
   resort). The flow probes candidates in order; adjust host/port if needed.
   Then **Submit** — it verifies connectivity before creating the entry.
5. Entities are created for every lamp and every group the add-on knows:

   - `light.smart_qxsx_<last-4-mac>` — a real, polled light (state, brightness,
     color temperature).
   - `light.<group-name>` — a group light, controlled with a single mesh packet
     to the group address. Telemetry for groups is **assumed state** only (the
     mesh has no group read-back), so reporting defaults to optimistic.

### Options

Open **Settings → Devices & Services → Telink BLE Lights → Options** to change
the add-on **host/port** (e.g. after moving the add-on) and the **poll interval**
(default 30 s, minimum 5 s). The integration only needs the add-on's REST API —
the Ingress web UI is separate.

---

## What you get

| Piece                      | Behavior                                                                 |
| -------------------------- | ------------------------------------------------------------------------ |
| Per-lamp `light` entity    | Tunable white only, 2700 K (warm) – 6500 K (cool); polled via bulk status |
| Per-group `light` entity   | Mesh group addressed via a single packet → very fast, but assumed state   |
| `light` color mode         | `ColorMode.COLOR_TEMP` — the lamps have **no RGB** capability             |
| Add-on REST / web UI       | `port 8099`; full UI via Ingress or `http://<host>:8099`                  |

Notes:

- The lamps are **tunable white only** (verified on hardware): brightness 0–100
  and color temperature 0(=warm)–100(=cool). The integration maps these to HA's
  Kelvin brightness/color-temperature API (mireds were removed in HA Core
  2026.3): 2700 K ⇔ warm end, 6500 K ⇔ cool end.
- The add-on and its daemon hold the single allowed BLE connection to each lamp.
  **Only one client** (phone app or add-on) may talk to a lamp at a time.