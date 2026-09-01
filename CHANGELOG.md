# Changelog

## 1.0.48 - 2026-09-01
- Add watchdog so the Supervisor restarts the web app if it hangs (queue
  saturation).
- Include the lamp MAC in status/query results so the HA integration can map
  state back to per-lamp entities (all lamps share the "Smart_mesh" name).

## 1.0.46 - 2026-09-01
- Daemon starts its TCP/socket server immediately and connects lamps in the
  background — a slow/wedged connect no longer blocks startup (which made the
  add-on fall back to hanging direct-connects and saturated the web queue).
  Watcher skips reconnect while the initial connect is running.

## 1.0.45 - 2026-09-01
- Connect via the discovered BLEDevice object instead of the address string
  (RPA-safe, per research notes) — fixes direct connects that could hang or
  miss a lamp after its address rotated.

## 1.0.44 - 2026-09-01
- Idle-release connections: sessions release after `TELINK_IDLE_TIMEOUT`
  (default 120 s) of inactivity, mirroring the bench's brief-connection model.
  Telink lamps stop advertising while connected, so holding sessions forever
  put them in a silent state that required a power-cycle. Reconnect on demand;
  `_reconnect` restarts the keepalive.

## 1.0.43 - 2026-09-01
- Fix single-lamp control: daemon sessions connect by exact MAC only (the
  name-based RPA fallback attached sessions to whatever lamp was advertising,
  so unicast `dst=<address>` hit the wrong lamp). Watcher reconnects missing
  lamps individually every 15 s.

## 1.0.42 - 2026-09-01
- Web UI power switch driven by brightness, not the `state` field (these lamps
  always report `state:"ON"`; off = brightness 0).

## 1.0.41 - 2026-09-01
- Fix top-right target dropdown (no change handler). Per-lamp display aliases
  (rename button, `POST /api/lamp/<mac>/alias`) so the shared "Smart_mesh"
  name is never ambiguous in lists/dropdowns.

## 1.0.40 - 2026-09-01
- Web UI: authentic Home Assistant look — top app bar, HA-style toggle switch
  for power (with state reflection), HA sliders and filled/tonal buttons.
  Light/dark follows `prefers-color-scheme`.

## 1.0.39 - 2026-09-01
- Serve HTML/JS/CSS with no-cache headers so stale browser caches can never
  break the UI after an add-on update.

## 1.0.38 - 2026-09-01
- White temperature slider applies on release (no extra Apply button) like
  brightness. Cache-busted static asset URLs (`?v=<version>`).

## 1.0.37 - 2026-09-01
- Web UI: Home Assistant Material 3 theme (Roboto, HA card/background/primary
  colors); removed the Color section (lamps are tunable-white only).

## 1.0.36 - 2026-09-01
- Daemon no longer exits/crash-loops when all lamps are momentarily offline:
  it stays up and the config watcher reconnects periodically. Important for
  the privileged sidecar (`--restart` would otherwise restart it forever).

## 1.0.35 - 2026-09-01
- Daemon reload only on lamp-set change (MAC/password/name), not when its own
  seq writes touch lamps.json (an mtime-based watcher caused an endless
  reload loop that dropped sessions).

## 1.0.34 - 2026-09-01
- run.sh reads `daemon_host`/`daemon_port` from `/data/options.json` so option
  changes apply without recreating the container (CONFIG_* env is only
  injected at creation).

## 1.0.33 - 2026-09-01
- **Variant B**: split BLE daemon from web UI. The daemon can run as a
  privileged sidecar container listening on TCP (`TELINK_DAEMON_HOST`/`PORT`),
  while the Supervisor add-on runs web-only and bridges to it. Fixes state
  readback where the add-on container's seccomp blocks the raw HCI monitor
  (`socket(AF_BLUETOOTH)` → Errno 97). Adds `daemon_host`/`daemon_port`
  options, `run_daemon.sh` entrypoint, and a pause/reload watcher in the
  daemon (honors the shared `daemon_paused` flag + re-reads lamps.json).

## 1.0.32 - 2026-09-01
- Remove the bleak `start_notify` fallback — these lamps reject CCCD writes
  (ATT 0x0e) and drop the connection, so subscribing via bleak killed the
  session before login completed. Control commands (no response needed) work
  again; notify readback stays unavailable where the container blocks
  AF_BLUETOOTH (fixed properly by Variant B).

## 1.0.31 - 2026-09-01
- `full_access: true` (replaces privileged NET_ADMIN/NET_RAW + usb/devices) —
  tried to lift the seccomp that blocks `socket(AF_BLUETOOTH)`; didn't help on
  this Supervisor, superseded by Variant B. Default `known_passwords` is now
  just `8888` (faster discovery).

## 1.0.30 - 2026-09-01
- Re-pull fixed image (bumped tag so the store re-pulls after a bad 1.0.29).

## 1.0.29 - 2026-09-01
- Log per-password probe errors during discovery instead of silently returning
  None (debug aid; superseded).

## 1.0.28 - 2026-09-01
- Debug: log per-password probe errors in discovery.

## 1.0.27 - 2026-09-01
- Default `known_passwords` includes `8888` (the Smart_qXsx mesh password),
  both in the config option and the code fallback.

## 1.0.26 - 2026-09-01
- Move add-on host port mapping from 8099 to 8098 (host 8099 was taken by a
  `ttyd` process, so the mapping silently failed and the integration could not
  reach the add-on).

## 1.0.25 - 2026-08-31
- Switch to **prebuilt image distribution**: `image: ghcr.io/mvb78/telink-ble-cli`
  in config.yaml; the store install now pulls the image instead of running a
  local docker buildx build (which silently hangs on Supervisor 7.x/HAOS 6.1).
- Dockerfile: add `io.hass.*` + OCI labels per the 2026 builder-migration docs.

## 1.0.24 - 2026-08-31
- Fix `wait_for_opcode` (`telink_ble.py`) to also match **mesh-layer** responses:
  frames decrypted by `decrypt_mesh_notification` start with `op|0xC0` at byte 0
  (`0x14` GRP_RSP, `0x1B` STATUS, `0x21` DEV_ADDR_RSP); previously only the vendor
  20B layout (`pkt[7]`) matched, so group reads silently timed out.
  Bench-validated against hardware (telink-lab matrix, 4 lamps).
- `SequenceManager` default start is now `0x1000` for entries without persisted
  `last_seq`: lamps reject seqs at/below their stored sno (±0x3F dedup window),
  so a fresh registry must never start at 1 against lamps with bench history.
- New `POST /api/lamp/<mac>/seq` to seed `last_seq` (dedup-window rescue after
  registry loss).
- Direct-query fallback in `web_app._query` now drains queued/stale 0xDB pushes
  before matching (lamps push status after every write; stale reads otherwise).
- HA integration: color temperature support (`color_temp_kelvin` 2700–6500 K),
  `telink_ble.recall_scene/store_scene/delete_scene/sync_time` services.

## 1.0.23 - 2026-08-31
- Fix `SequenceManager` monotonic `last_seq` persistence (`telink_mesh.py:4`, `lamp_registry.py:85`) — `TelinkController` now resumes from `last_seq` (`telink_ble.py:79`) and `DaemonSession`/`run_on_lamp` persist after each command. Prevents `sno` duplicate window `0x3F` (`ble_hardware_reference.md:2110`) rejecting `0xD7`/`0x17` group writes, which left flash `0x79000` stale after reboot.

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
