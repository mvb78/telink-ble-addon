# Changelog

## 1.0.39 - 2026-09-01
- Serve HTML/JS/CSS with no-cache headers so stale browser caches can never
  break the UI after an add-on update (a cached old app.js referencing the
  removed swatches element was crashing on load, hiding the lamp list).
- White temperature slider applies on release (no extra Apply button), like
  brightness. Cache-bust versioned static asset URLs (?v=1.0.38) so browsers
  never serve stale JS/CSS after an add-on update.
- Web UI: restyle to the Home Assistant Material 3 look (Roboto, HA dark
  card theme) and remove the Color section (lamps are tunable-white only).
- Daemon no longer exits/crash-loops when all lamps are momentarily offline:
  it stays up and the config watcher reconnects periodically. This matters for
  the privileged sidecar (--restart would otherwise restart it forever).
- Fix endless reload loop: the daemon only reloads when the lamp *set*
  (MAC/password/name) changes, not when its own seq writes touch lamps.json.
- run.sh reads daemon_host/daemon_port from /data/options.json so option
  changes apply without recreating the container (CONFIG_* env is only
  injected at creation).
- **Variant B**: split BLE daemon from web UI. The daemon can run as a
  privileged sidecar container listening on TCP (`TELINK_DAEMON_HOST`/`PORT`),
  while the Supervisor add-on runs web-only and bridges to it. Fixes state
  readback where the add-on container's seccomp blocks the raw HCI monitor
  (`socket(AF_BLUETOOTH)` → Errno 97). Adds `daemon_host`/`daemon_port`
  options, `run_daemon.sh` entrypoint, and a pause/reload watcher in the
  daemon (honors the shared `daemon_paused` flag + re-reads lamps.json).

## 1.0.32 - 2026-09-01
- `full_access: true` (replaces privileged NET_ADMIN/NET_RAW + usb/devices):
  the container's seccomp blocks `socket(AF_BLUETOOTH)`, so the raw HCI
  monitor used to capture lamp responses is unavailable and status queries
  fail. Running with full access (protection mode off) lets the HCI monitor
  open. Requires protection mode to be disabled for this add-on.
- Default `known_passwords` now only `8888` (faster discovery).

## 1.0.30 - 2026-09-01
- Fix command responses inside HAOS add-on container: the raw HCI monitor
  (ATT_NOTIFY capture) is unavailable there, and the bleak notify callback
  was never subscribed, so status/command responses never arrived. Fall back
  to a bleak subscription when the HCI monitor is unavailable; log the monitor
  failure reason for diagnostics.

## 1.0.28 - 2026-09-01
- Log per-password probe errors during discovery instead of silently returning
  None, so container BLE failures are visible in the add-on log.

## 1.0.27 - 2026-09-01
- Add `8888` to the default `known_passwords` list (config default + code
  fallback). The Smart_qXsx lamps use the mesh password `8888`, so discovery
  would silently fail to log in and save lamps with only the old defaults.

## 1.0.26 - 2026-08-31
- Move add-on host port mapping from 8099 to 8098: host port 8099 is taken
  by a `ttyd` process on this installation, so the mapping silently failed
  and the HA integration could not reach the add-on. Requires image rebuild
  (version tag) so the container is recreated with the new mapping.

## 1.0.25 - 2026-08-31
- Switch to **prebuilt image distribution**: `image: ghcr.io/mvb78/telink-ble-cli`
  in config.yaml; the store install now pulls the image instead of running a
  local docker buildx build (which silently hangs on Supervisor 7.x/HAOS 6.1).
- Dockerfile: add `io.hass.*` + OCI labels per the 2026 builder-migration docs.

## 1.0.22 - 2026-08-30
- Fix add-on `url:` so the store "Visit ... page" link points to the **public**
  repo (`https://github.com/mvb78/telink-ble-addon`) instead of the private one.

## 1.0.21 - 2026-08-30
- Fix group commands (`dst=<group>`) by selecting a **working relay lamp** that is
  currently provisioned in the mesh (current `Smart_mesh`/`8888` creds) instead of
  blindly using the first lamp in the registry (which could be a broken/out-of-mesh
  lamp). This makes `light.telink_oben` / `light.telink_arbeitsplatte` group control
  work end to end.

## 1.0.20 - 2026-08-30
- Pick a working relay lamp for group-destination (`dst`) commands.

## 1.0.19 - 2026-08-30
- Faster brute-force login harness (0.12s delay between attempts).
