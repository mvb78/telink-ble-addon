# Telink BLE Add-on & Companion Integration

Home Assistant OS add-on + companion Home Assistant integration for **Telink
TLSR private-mesh BLE lamps** (BT-Light / Smart_nSpq / Smart_qXsx).

- **Add-on** (`telink-addon/`) — runs the Flask web UI in a Supervisor
  container (port 8098 / Ingress). The BLE daemon runs as a **privileged
  sidecar container** (Variant B) that owns the Bluetooth connections, because
  the add-on's own container can't open the raw `AF_BLUETOOTH` socket (seccomp
  Errno 97) that the HCI-monitor notify readback needs.
- **Companion integration** (`custom_components/telink_ble/`) — polls the
  add-on's REST API and exposes each lamp and group as native `light.*`
  entities (tunable white only, Kelvin color-temperature API).

> The lamps are **tunable white only** (verified on hardware): brightness 0–100
> and color temperature 0(=warm)–100(=cool). No RGB. The integration maps these
> to HA's Kelvin API (mireds were removed in HA Core 2026.3): 2700 K ⇔ warm end,
> 6500 K ⇔ cool end.

---

## Disclaimer

**Use at your own risk.**

This project is **not affiliated with or endorsed by Telink** (Telink Semiconductor
or any related entity). It was built by analyzing BLE protocol captures against the
public BT-Light Android app; it contains **no proprietary SDK or source code**. The
mesh login handshake and packet encryption are implemented from scratch based on
those observations.

This software talks directly to consumer lighting hardware over BLE, including
commands that alter device state (brightness, color temperature, mesh membership,
soft reset). **The author accepts no liability** for any damage, loss, or
malfunction resulting from its use, including lost lamp configuration or failures
of your Home Assistant setup. Test on a spare lamp first.

_MIT licensed — see [LICENSE](LICENSE)._

---

## Install the add-on

1. Home Assistant → **Settings → System → Add-ons → Add-on store → ⋮ → Repositories**
2. Add: `https://github.com/mvb78/telink-ble-addon`
3. Install **Telink BLE CLI**, set options if needed:
   - `known_passwords` — comma-separated mesh passwords (default `8888`)
   - `scan_timeout` — discovery scan seconds (default `45`)
   - `daemon_host` / `daemon_port` — set `daemon_host` to `172.30.32.1` (and
     `daemon_port` to `8097`) only when running the BLE daemon as a privileged
     sidecar (Variant B, see [docs/INSTALL_HAOS.md](docs/INSTALL_HAOS.md)).
4. **Start** the add-on, open **Web UI**, and click **Discover** to find your
   lamps (the phone app must be disconnected).

### Variant B sidecar (required for state readback in the add-on container)

On this Supervisor the add-on's seccomp blocks `socket(AF_BLUETOOTH)`, so the
daemon must run in a `--privileged` Docker container on the HAOS host (get
Docker access via the community **Advanced SSH & Web Terminal** add-on with
protection mode off):

```bash
docker run -d --name telink-daemon \
  --privileged --network host \
  -v /run/dbus/system_bus_socket:/run/dbus/system_bus_socket \
  -v /mnt/data/supervisor/apps/data/c4c18bb5_telink_ble_cli:/data \
  -e TELINK_DATA_DIR=/data -e TELINK_KNOWN_PASSWORDS=8888 \
  -e TELINK_DAEMON_HOST=0.0.0.0 -e TELINK_DAEMON_PORT=8097 \
  --restart unless-stopped \
  ghcr.io/mvb78/telink-ble-cli:1.0.47 python3 telink_daemon.py
```

Then set `daemon_host: 172.30.32.1` in the add-on options and restart the
add-on. See [docs/INSTALL_HAOS.md](docs/INSTALL_HAOS.md) for the full
walkthrough (incl. finding the add-on's `/data` host path).

## Install the companion integration

**Option A — HACS** (recommended):

1. HACS → ⋮ → **Custom repositories** → type = *Integration*
2. URL: `https://github.com/mvb78/telink-ble-addon`
3. Download, then restart Home Assistant.
4. **Settings → Devices & Services → Add Integration → Telink BLE Lights**.
   The flow probes the add-on host automatically (port 8098).

**Option B — manual**:

```bash
cd /config
git clone https://github.com/mvb78/telink-ble-addon.git
cp -r telink-ble-addon/custom_components/telink_ble custom_components/
rm -rf telink-ble-addon
```

Then restart Home Assistant and add the integration as above.

---

## Details

- Full standalone web UI (daemon + waitress on Ingress port 8099; host port 8098).
- Add-on options reach the container as `CONFIG_*` Supervisor env vars and are
  mapped to the package's `TELINK_*` settings; data persists in `/data`.
- Group lights are addressed via a single mesh packet to the group's address
  (`dst`), so they are fast but use **assumed state** (the mesh has no group
  read-back). Per-lamp lights are polled in bulk through the daemon
  (`POST /api/command/status`).
- Integration options: add-on host/port + poll interval (default 30 s, min 5 s).

## Install guide

See **[docs/INSTALL_HAOS.md](docs/INSTALL_HAOS.md)** for the full walkthrough.

## Repository layout

```text
telink-addon/                  # Supervisor add-on (self-contained build dir)
├── config.yaml                # add-on manifest
├── Dockerfile                 # python:3.12-slim + waitress
├── run.sh                     # entrypoint (daemon + web, signal handling)
└── python/                    # synced copy of the CLI + daemon + web app
custom_components/telink_ble/  # companion Home Assistant integration
docs/INSTALL_HAOS.md           # install guide
```

## Development note

`telink-addon/python/` is the **source of truth** for the add-on's Python
package (the standalone CLI lives in the sister project `telink-ble-cli`).
Edit the add-on tree directly and commit — there is no sync step.