# Changelog

## 1.5.0 - 2026-09-19 (group reliability — lab findings, 3+3 mesh)
- **Discovery scan 45 s → 60 s** (default). Lab proved in-mesh Telink lamps
  advertise ~15–20× slower than unprovisioned ones; 20–45 s scans miss them,
  a 60 s scan reliably catches all 6.
- **Group add/remove now unicast 0xD7 to the target lamp's own mesh address**
  (previously broadcast). Lab-verified: the lamp learns its group membership
  in firmware reliably only via the unicast path; broadcast delivery is
  inconsistent across sessions.
- **`_execute` relay fallback for ALL commands** (incl. pure broadcast group
  set): try each provisioned (8888) candidate session in turn, so a single
  dead/unreachable lamp session can no longer wedge the whole group command.
  Previously a pure broadcast required every session to answer and fell into
  slow direct-connect scans on any failure.
- Integration 1.2.0 (version sync only; behavior unchanged).

## 1.4.2 - 2026-09-19 (the 04:00/08:00 outage bug — fd exhaustion self-heal)
- Root cause of "lamps dead last night 04:00 and again 08:00": the daemon
  slowly leaked file descriptors (~14 h uptime) until the 1024-fd limit
  killed `socket.accept()` with 95k Errno-24 errors — every consumer wedged
  with no visible symptom.
- Daemon now raises its own `nofile` soft limit to ≥8192 at startup and
  runs an **fd watchdog**: at ≥4096 open fds it restarts the process
  (`os._exit(2)` + docker `restart: unless-stopped`), turning any future
  leak into a ~10 s blip instead of a dead morning.
- `_reconnect` no longer stacks duplicated keepalive tasks across
  reconnections.
- `Saved N lamp(s)` log spam (2 lines per mesh send) gated behind
  `TELINK_DEBUG_STATE`.
- deploy_sidecar.sh passes `--ulimit nofile=8192:8192` as belt & braces.

## 1.4.1 - 2026-09-16 (sequence-number desync self-heal)
- Daemon: when a lamp's own status push carries a mesh seq number ahead of
  ours (after phone-app usage or a mesh re-key), the session's sequence
  counter jumps past it immediately — sends can never again be silently
  dropped by the ±0x3F dedup window. The jump persists like a normal send.
- Docs: README gains the "Bluetooth adapter requirements" section —
  exclusive adapter or a second dongle (HA's own Bluetooth scanner disabled,
  phone apps must disconnect).

## 1.4.0 - 2026-09-16 (lamp responses — evented state)
- Daemon: state cache from the lamps' own `0xDB` status pushes. Every mesh
  write (incl. group broadcasts) triggers a push through each lamp's session;
  the daemon decodes it (`brightness>0` = on, matching the 1.3.0 correct-off
  semantics) and keeps a timestamped per-lamp cache. No BLE interaction on
  reads.
- Daemon TCP protocol: `{"kind": "state"}` request → snapshot of all
  lamp states (`{mac: {on, brightness, colortemp warm%, rgb, ts, name}}`).
- Integration 1.1.0: polls the daemon directly over TCP 8097 (bypassing the
  add-on web app's Flask layer) with graceful fallback to the status-query
  path; mesh **group entities now show real composed state** (OR over known
  members, matching the group-registry membership) instead of assumed state —
  correct after restarts and reflecting group broadcasts.
- lamp push semantics + TCP state protocol unit-tested
  (tests/test_state_cache.py, 5 cases); full suite 30/30 green.

## 1.3.1 - 2026-09-15 (verified sends, integration retry)
- Daemon: liveness-verified command sends — every mesh send is now proven by
  a GATT read of the status characteristic; on failure the session reconnects
  and re-sends within the same call (2 cycles) instead of silently dropping
  packets. Root cause of the "lamps don't switch on in the morning" class:
  write-without-response never raises on a dead link, so packets vanished
  without any error reaching HA.
- Daemon: keepalive-loop/query/read_status reconnects no longer spawn
  duplicate keepalive tasks (`_reconnect` gained a `spawn_keepalive` switch);
  `send()` re-arms the keepalive task itself.
- Integration 1.0.4: failed commands retry 3× (2 s apart) and raise
  `HomeAssistantError` on final failure instead of returning a bool that
  nobody checked — automations and UI taps now surface visible failures.
- Live-verified on HA: daemon 1.3.1 with 4/4 lamps connected, verified
  on/off round-trip through the HA service call.

## 1.3.0 - 2026-09-11 (speed & reliability, live-verified on HA)
- Permanent connections (`TELINK_IDLE_TIMEOUT=0`) + health-check keepalive that
  detects dead links; fast event-driven reconnect (~0.25 s instead of 5-30 s).
- Daemon status query parallelized: bulk status 1.5 s → 0.4 s (4 lamps).
- `[notify]` frame logging gated behind `TELINK_DEBUG_NOTIFY` (was 14k lines/h).
- Group relay: try each candidate session before the slow direct-connect fallback.
- Integration: availability via `last_update_success` (no more flapping that
  made automations no-op); coordinator poll requests run concurrently.
- Live fix on HA: reloaded the orphaned integration (all entities were
  "no longer being provided" since the Sep-10 restart), restored polling,
  confirmed `light.turn_off` service call at ~0.8 s end-to-end.

## 1.2.0 - 2026-09-03 (branch `feat/esp32-research-updates` — UNVALIDATED)
Protocol updates ported from the cross-validated telink-ble-esp32 research
(`docs/TELINK_MESH_PROTOCOL.md`); hardware/HA validation planned:
- Delete-pairing via 0x0A + proof frame (pair state 0x0B), legacy bare 0x0E
  kept as fallback.
- Provisioning: unencrypted 0xE1 address-confirm watcher (HCI monitor) that
  adopts the lamp-reported address; notify-char value-write subscribe so the
  push is sent; blind-settle fallback in-container; bootstrap login now
  auto-falls back factory creds → target creds (ESP32 recipe §6.2).
- Login: state-driven 0x01 EXCHANGE_RANDOM handshake for Idle/Init lamps
  (`login_with_random_exchange`). Live-tested on HA: 0x01 accepted, but 0x0C
  still 0x0E on the spare factory lamps — factory login unresolved (shared
  with telink-ble-esp32).
- Short group query 0xDD → 0xD4 (`/api/command/app-get-groups`) and
  `POST /api/groups/sync` reconciling groups.json from the lamp;
  "Read groups from lamp" UI button.
- Unicast addresses 1..250 with auto-allocation (`addr:"auto"`); 1..63
  UI limit dropped.
- Discovery probes factory creds `1234, 0000, 123` after `8888`.
- Host unit tests under `telink-addon/python/tests/` (18 cases).
- Docs: `docs/TELINK_MESH_PROTOCOL.md` ported (tracked); telink-ble.md gaps
  refreshed (local, gitignored).

## 1.1.0 - 2026-09-01
- 1.1.x production series (Variant B sidecar, groups, HA integration);
  rolls up 1.0.25-1.0.48.

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
