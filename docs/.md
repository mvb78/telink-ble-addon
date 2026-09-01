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
  (`host_dbus: true` + `full_access: true` in the add-on manifest.)
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
     discovery (default `8888`). Add yours if your lamps use a different
     password.
   - **scan_timeout** — seconds to scan during discovery (default `45`).
   - **daemon_host / daemon_port** — leave empty for the normal single-container
     mode. Set `daemon_host` only if you run the BLE daemon as a privileged
     sidecar (see [Variant B](#variant-b-privileged-sidecar-daemon) below).
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
   **8098** in the host network namespace, so the integration normally uses the
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
| Add-on REST / web UI       | `port 8098`; full UI via Ingress or `http://<host>:8098`                  |

Notes:

- The lamps are **tunable white only** (verified on hardware): brightness 0–100
  and color temperature 0(=warm)–100(=cool). The integration maps these to HA's
  Kelvin brightness/color-temperature API (mireds were removed in HA Core
  2026.3): 2700 K ⇔ warm end, 6500 K ⇔ cool end.
- The add-on and its daemon hold the single allowed BLE connection to each lamp.
  **Only one client** (phone app or add-on) may talk to a lamp at a time.

---

## Variant B: privileged sidecar daemon

The add-on's container runs under the Supervisor's **seccomp profile**, which on
some Supervisors still blocks the raw `AF_BLUETOOTH` socket even with
`full_access: true` + protection mode off. Symptom (add-on log):

```
[warn] HCI monitor: socket(AF_BLUETOOTH) failed: [Errno 97] Address family not supported by protocol
```

Without the HCI monitor the daemon cannot capture the lamps' ATT_NOTIFY
responses, so **state readback** (status polling) fails — commands still work.
If you see this, run the BLE daemon as a privileged sidecar container on the
same HAOS host and let the add-on bridge to it over TCP:

1. **Get Docker access on the host** — install the community **"Advanced SSH &
   Web Terminal"** add-on from the store and turn its **protection mode OFF**
   (Configuration tab). It exposes the host Docker socket.
2. **Find the add-on's data dir on the host** (its `/data` lives here; both
   processes must share `lamps.json` and the pause flag):
   ```bash
   docker inspect app_c4c18bb5_telink_ble_cli --format '{{range .Mounts}}{{if eq .Destination "/data"}}{{.Source}}{{end}}{{end}}'
   ```
3. **Run the sidecar daemon** (privileged → seccomp off → HCI monitor works):
   ```bash
   docker run -d --name telink-daemon \
     --privileged --network host \
     -v /run/dbus/system_bus_socket:/run/dbus/system_bus_socket \
     -v "<ADDON_DATA_PATH>":/data \
     -e TELINK_DATA_DIR=/data \
     -e TELINK_KNOWN_PASSWORDS=8888 \
     -e TELINK_DAEMON_HOST=0.0.0.0 \
     -e TELINK_DAEMON_PORT=8097 \
     --restart unless-stopped \
     ghcr.io/mvb78/telink-ble-cli:<VERSION> \
     python3 telink_daemon.py
   ```
   Replace `<ADDON_DATA_PATH>` with the path from step 2 and `<VERSION>` with
   the current add-on version (1.0.33+). Confirm it connected to the lamps:
   `docker logs telink-daemon`.
4. **Point the add-on at the sidecar** — in the Telink BLE CLI add-on
   Configuration set `daemon_host` to `172.30.32.1` (the host gateway from the
   add-on's network) and `daemon_port` to `8097`, then restart the add-on. It now
   runs web-only and bridges every command to the sidecar. Status readback works.

The daemon watches the shared `lamps.json` and `daemon_paused` flag, so
**Discover** in the add-on UI still works (it pauses the sidecar, scans, saves,
and the sidecar reloads).

> Note: the web UI and the HA integration still connect to the **add-on** on
> port 8098/Ingress as usual — only the BLE layer moved to the sidecar.