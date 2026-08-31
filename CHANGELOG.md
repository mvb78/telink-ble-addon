# Changelog

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
