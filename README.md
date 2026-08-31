# Telink BLE Add-on & Companion Integration

Home Assistant OS add-on + companion Home Assistant integration for **Telink
TLSR private-mesh BLE lamps** (BT-Light / Smart_nSpq / Smart_qXsx).

- **Add-on** (`telink-addon/`) — runs the Telink BLE daemon + web UI in a
  Supervisor container, using the host's Bluetooth stack (built-in or USB).
  Provides a full standalone web UI (on/off, brightness, color temp, scenes,
  discovery) reachable via Ingress.
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
   - `known_passwords` — comma-separated mesh passwords (default `0000,1234,123`)
   - `scan_timeout` — discovery scan seconds (default `45`)
4. **Start** the add-on, open **Web UI**, and click **Discover** to find your
   lamps (the phone app must be disconnected).

## Install the companion integration

**Option A — HACS** (recommended):

1. HACS → ⋮ → **Custom repositories** → type = *Integration*
2. URL: `https://github.com/mvb78/telink-ble-addon`
3. Download, then restart Home Assistant.
4. **Settings → Devices & Services → Add Integration → Telink BLE Lights**.
   The flow probes the add-on host automatically (port 8099).

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

- Full standalone web UI (daemon + waitress on Ingress port 8099).
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