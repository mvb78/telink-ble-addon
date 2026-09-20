# Changelog

## 1.6.4 - 2026-09-20 (query fallback closes lost-push gap)
- `_confirm_push` queries 0xDA on push timeout and compares against the
  commanded on/brightness/colortemp before declaring failure.
- Shared `_decode_status_push` / `_push_matches_command` helpers used by
  both the push cache and the confirmation path.

## 1.6.3 - 2026-09-20 (actuation confirmation)
- Verified sends now wait for the lamp's actuation push (unicast to own
  lamp); silence triggers reconnect+retry instead of phantom success.
- Stale-push sessions pre-bump their seq counter forward (always accepted).

## 1.6.2 - 2026-09-20 (state-cache ts = last seen)
- `note_plain` refreshes ts on every decoded push (even identical), so
  steady lamps stay fresh for the HA staleness watchdog.

## 1.6.1 - 2026-09-20 (kill the reconnect-scan cascade)
- Global adapter lock: all BLE ops across sessions serialize on one lock;
  concurrent scans no longer kill neighbor links.
- Per-lamp reconnect backoff (15s doubling, 300s cap).

## 1.6.0 - 2026-09-20 (dedicated Bluetooth dongle)
- Adapter pinning (`TELINK_HCI_ADAPTER` / add-on option `hci_adapter`):
  every scanner, client and the raw HCI monitor bind to one adapter only
  (here: hci1, ASUS USB-BT500); HA's Bluetooth stays on the internal hci0.

## 1.5.0 - 2026-09-19 (group reliability — lab findings)
- Discovery scan default 45s -> 60s (in-mesh lamps advertise 15-20x slower).
- Group add/remove unicast 0xD7 to the lamp's own mesh address (reliable
  firmware membership, per lab); `_execute` falls back across candidate
  relay sessions so one dead lamp can't wedge a group command.

## 1.4.2 - 2026-09-19 (fd exhaustion self-heal — the 04:00/08:00 outage)
- Daemon raises its own nofile soft limit to >=8192 at startup and runs an
  fd watchdog (restart at >=4096 fds), ending two silent overnight wedges.
- `_reconnect` no longer stacks duplicate keepalive tasks; log spam gated.

## 1.4.1 - 2026-09-16 (sequence-number desync self-heal)
- Liveness-verified sends now also self-heal sequence number desync:
  when a lamp's own push shows a mesh seq ahead of ours, the session's
  counter jumps past it (persisted), so later mesh writes cannot be
  dropped by the ±0x3F dedup window.
- Docs: README notes the Bluetooth adapter exclusivity / 2nd dongle.

## 1.4.0 - 2026-09-16 (lamp responses — evented state)
- **Per-lamp state cache**: the daemon decodes each lamp's `0xDB` status push
  (fired after every mesh write, including group broadcasts) into a
  timestamped cache; the lamps ARE the source of truth now.
- **`kind: state` TCP request**: memory-cache snapshot for consumers
  (HA integration polls it directly on 8097 — no BLE traffic, sub-ms reads).
- Mesh group state is composed from the member lamps' caches (OR over
  `brightness>0`, matching the 1.3.0 correct-off rule).

## 1.3.1 - 2026-09-15 (verified sends)
- **Liveness-verified sends**: every mesh command is confirmed by a status-char
  GATT read; failure triggers reconnect + resend (up to 2 cycles) and surfaces
  as an error to the caller instead of silent packet loss. This closes the
  "morning dead link" gap the health-check keepalive could only detect a full
  interval later: a command racing a dead session now recovers inside the
  same HTTP call.
- Reconnect housekeeping: keepalive-loop, query and read_status paths no
  longer spawn duplicate keepalive tasks.

## 1.3.0 - 2026-09-11 (speed & reliability, live-verified on HA)
- **Combined `POST /api/command/set`**: on/off + brightness + colour temperature
  in ONE call — a group turn_on with brightness+CT went from ~1.2 s (3 HTTP
  round-trips) to ~0.65 s (measured live).
- **Optimistic state**: lamp and group entities write assumed state immediately
  after a command (corrected on the next poll), so HA responds instantly.
- **Correct on/off**: lamps now report `off` when brightness is 0 (these lamps
  always report `state:"ON"`; "off" = brightness 0) — matches the web UI fix.
- **Permanent connections** (the old CLI's fast model): `TELINK_IDLE_TIMEOUT=0`
  disables the idle-release sweeper (`deploy_sidecar.sh` now sets it); the
  health-check keepalive keeps sessions alive and detects dead links.
- **Health-check keepalive**: the 0xDA keepalive now expects the lamp's 0xDB
  reply; two missed replies drop the link so the next command reconnects fast
  instead of hanging on a half-dead session.
- **Fast reconnect**: `TelinkController.connect` scans event-driven (checks the
  scanner every 0.25 s) instead of sleeping 5 s before the first look —
  reconnect went from >=5 s (up to 30 s) to ~0.25 s when a lamp is advertising.
- **Parallel status query**: the daemon now queries all lamp sessions
  concurrently (`asyncio.gather`), so bulk status dropped from ~1.5 s to ~0.4 s
  for 4 lamps (measured live).
- **Notify-log spam gated**: `TELINK_DEBUG_NOTIFY` (default off) now controls
  the per-frame `[notify]` print that wrote 14k+ lines/hour (514k total);
  daemon log volume dropped ~27x.
- **Group relay robustness**: `_execute` tries each candidate relay session in
  turn before the direct-connect fallback, so a temporarily-missing session
  fails fast instead of triggering a 5-30 s BLE scan.
- **Integration availability**: light entities now use the coordinator's
  `last_update_success` instead of the daemon `connected` flag — a single
  failed poll can no longer flip every light unavailable (that caused
  automations to no-op for hours).
- **Integration poll parallelized**: lamps/groups/status/daemon fetched
  concurrently per poll.

## 1.2.0 - 2026-09-03 (UNVALIDATED — bench/HA test pending)
Protocol updates ported from the cross-validated telink-ble-esp32 research
(docs/TELINK_MESH_PROTOCOL.md):

- **Delete-pairing via 0x0A + proof frame** — `[0x0A]‖rand(8)‖proof(8)`,
  proof = java_aes(base_key, rand‖00…)[8:16]; lamp confirms with pair state
  0x0B. Falls back to the legacy bare 0x0E write when unconfirmed
  (`provision_lamp.delete_pairing_proof`). Needs hardware validation (T6).
- **0xE1 unencrypted address confirm during provisioning** — new
  `telink_ble.AddrConfirmWatcher` watches the HCI monitor for the raw 0xE1
  push after the 0xE0 write and ADOPTS the lamp-reported address (mismatch
  logged). Falls back to the old blind 4 s settle where the monitor is
  unavailable (in-container). Provisioning now also value-write subscribes
  the notify char so the push is sent at all.
- **Bootstrap login credential auto-fallback** (ESP32 recipe §6.2) — with
  `bootstrap:true` and no explicit current creds the flow now tries factory
  `out_of_mesh`/`123` first, then the target mesh creds, so a fresh or kicked
  lamp provisions without passing current_name/current_password.
- **State-driven login** (pairing.md §1-2) — `provision_lamp.login_with_random_exchange`
  reads the pair state and performs the `0x01 EXCHANGE_RANDOM` handshake first
  for Idle/Init (0x00/0x0E) lamps, the documented prerequisite for
  factory/unprovisioned lamps that reject a direct `0x0C` with pair state
  `0x0E`. Live test on HA (2026-09-10): the `0x01` exchange is accepted
  (0x00 → 0x02) on the two spare lamps, but `0x0C` still returns `0x0E` for
  every credential candidate — **factory login remains UNRESOLVED** (same
  blocker as telink-ble-esp32).
- **Short group query 0xDD → 0xD4** (app-layer, BT-Light APK flow) as
  `/api/command/app-get-groups`, plus **`POST /api/groups/sync`** which reads
  a lamp's group memberships and reconciles groups.json (creates unknown
  groups, drops stale memberships of the queried lamp — only for addresses
  the short format can report, 0x8001..0x80FF). UI: "Read groups from lamp"
  button in the membership panel.
- **Unicast addresses 1..250 + auto-allocation** — provision/assign-addr
  accept `addr:"auto"` (lowest free of 1..250 vs lamps.json, BT-Light app
  behavior); old hard 1..63 limit dropped; UI empty input = auto.
- **Discovery probe list** now `8888, 1234, 0000, 123` (factory creds for
  out_of_mesh/Smart_nSpq/Smart_qXsx meshes per the ESP32 research). Existing
  installs must update the `known_passwords` add-on option to benefit.
- New host-runnable unit tests: `telink-addon/python/tests/` (proof frame,
  0xD4 parse, 0xE1 watcher matching, address allocation) —
  `python3 -m pytest tests/`.
- Docs: ported `docs/TELINK_MESH_PROTOCOL.md` (tracked) from the ESP32
  deliverable; refreshed the stale gap list in the local telink-ble.md.

## 1.1.0 - 2026-09-01
- 1.1.x series: production state (Variant B sidecar, groups, HA integration).
  Changes in 1.0.25-1.0.48 are rolled into this release line.

## 1.0.48 - 2026-09-01
- Add watchdog (http://[HOST]:[PORT:8099]/api/daemon) so the Supervisor
  restarts the web app if it ever hangs again (queue saturation).
- Include the lamp MAC in status/query results so the HA integration can map
  state back to per-lamp entities (all lamps share the "Smart_mesh" name).
- Daemon starts its TCP/socket server immediately and connects lamps in the
  background — a slow/wedged connect no longer blocks startup (which made the
  add-on fall back to hanging direct-connects and saturated the web queue).
  Watcher skips reconnect while the initial connect is running.
- Connect via the discovered BLEDevice object instead of the address string
  (RPA-safe, per research notes) — fixes direct connects that could hang or
  miss a lamp after its address rotated.
- Stop stranding lamps: sessions now release after TELINK_IDLE_TIMEOUT
  (default 120s) of inactivity, mirroring the bench's brief-connection
  model. Telink lamps stop advertising while connected, so holding sessions
  forever put them in a silent state that required a power-cycle. Reconnect
  on demand; _reconnect restarts the keepalive.
- Fix single-lamp control: daemon sessions now connect by exact MAC only (the
  name-based RPA fallback attached sessions to whatever lamp was advertising,
  so unicast dst=<address> hit the wrong lamp and per-lamp control failed).
  Watcher reconnects missing lamps individually every 15s instead of reloading
  everything.
- Fix power switch: the lamps always report state "ON" (off = brightness 0),
  so the switch was forced back to ON every poll and seemed dead. Drive the
  switch from brightness instead.
- Fix top-right target dropdown (no change handler - selecting did nothing).
- Per-lamp alias: rename button, lamps show a distinct label everywhere
  (alias, or name + mesh address/MAC suffix) so the shared "Smart_mesh" name
  no longer makes dropdowns ambiguous. New POST /api/lamp/<mac>/alias.
- Web UI: authentic HA look — top app bar, HA-style toggle switch for power
  (with state reflection via status poll), HA sliders and filled/tonal buttons.
  Light/dark follows the browser/HA preference automatically.
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
