# Telink BLE Mesh Protocol — Consolidated Reference

> Compiled for the HA add-on + companion integration. Ported (2026-09-03) from
> the cross-validated deliverable in the sister project
> [`telink-ble-esp32`](https://github.com/mvb78/telink-ble-esp32)
> (`docs/TELINK_MESH_PROTOCOL.md`, commit `083df79`), which merged and
> reconciled the earlier addon reference `docs/telink-ble.md` with three
> additional reverse-engineering sources. Local additions are marked
> `[ADDON*]`; the ESP32 bench-validated notes are tagged `[ESP32]`.
>
> The Telink SIG Mesh Developer Handbook PDF was **excluded** by decision —
> our lamps do not speak SIG Mesh.

## 0. Sources & Provenance

| Tag | Source | Nature |
|---|---|---|
| `[FW]` | Rust re-implementation of the Telink TLSR8266 light firmware | **Legacy proprietary** protocol, node side. Richest source |
| `[APK]` | BT-Light 2.34 app, decompiled (androguard) | **Legacy proprietary**, app side |
| `[SDK]` | TelinkBleMeshLib / TelinkSigMeshLib / tl_ble_mesh | **Standard SIG Mesh** stacks — *different* protocol family, not spoken by our lamps |
| `[ADDON]` | this repo (`telink-addon/python/`) | Production Python implementation, bench-validated (2026-08/09) |
| `[CLI]` | `telink-ble-cli` | Same implementation family as `[ADDON]` (shared algorithms) |
| `[ESP32]` | `telink-ble-esp32` firmware (Bluedroid GATTC) | Control path verified on real lamps (Sep 2026); provisioning flows implemented, hardware tests T2–T6 pending |

Cross-validation: opcodes and framing agree across `[FW]`/`[APK]`/`[ADDON]`/`[ESP32]`.
Discrepancies are flagged inline and collected in §9.

---

## 1. Architecture & Protocol Summary

### 1.1 Two protocol families — DO NOT MIX

| | **Legacy Telink proprietary mesh** (our lamps) | **Telink SIG Mesh** (`TelinkSigMeshLib`, B91 SDK) |
|---|---|---|
| Transport | GATT vendor service `0x1910` + proprietary 48 B flood frames (L2CAP CID `0xFF03`) | SIG Provisioning `0x1827` / Proxy `0x1828`, mesh PDUs |
| Encryption | AES-128-ECB-based custom CTR+CBC-MAC, key = `name16 XOR pwd16` | SIG Mesh Security Material (ECDH P-256, K2/K3/K4, AES-CCM, NetKey/AppKey) |
| Opcodes | 1-byte vendor ops (`0xD0…0xF7` app layer, `0x02…0x35` mesh layer) | SIG model ops (`0x8201` Gen OnOff …) + vendor CID `0x0211` ops `0xC5…0xCD` |
| Keys | mesh name + password (ASCII) + optional LTK | NetKey / AppKey / DevKey (128-bit) |
| Our lamps? | **YES** | **No** |

**Conclusion:** implement only the legacy protocol.

### 1.2 Ecosystem data flow

```
HA integration ──HTTP:8098──► Flask add-on (Supervisor container) ──TCP 8097──► bleak daemon
                                (Variant B: daemon runs as a privileged sidecar
                                 container — seccomp blocks AF_BLUETOOTH inside
                                 the add-on container, so the HCI-monitor notify
                                 readback only works in the sidecar)
telink-ble-esp32 (ESP32-C3 Bluedroid GATTC controller, independent host)
telink-ble-http-api (FastAPI wrapper around [CLI])
```

---

## 2. GATT Topology (legacy lamps)

Service `00010203-0405-0607-0809-0a0b0c0d1910`, 4 characteristics:

| Char UUID suffix | Purpose | Properties | ATT handle | Notes |
|---|---|---|---|---|
| `…1911` | Notify (status) | notify | **`0x0012`** `[ESP32]` | lamp pushes ATT_NOTIFY **without CCCD**; enable by writing `0x01` to the **char value**. **Never write the CCCD** → ATT err `0x0e` + disconnect |
| `…1912` | Command | write-no-rsp | **`0x0015`** `[ESP32]` | 20-byte encrypted frames |
| `…1913` | OTA / status read | read, write | `0x0018` (derived) | GATT-read fallback for status in-container `[ADDON]` |
| `…1914` | Pair | read, write | **`0x001B`** `[ESP32]` | login / mesh-info state machine, 17-byte frames |

`[FW]` confirms the table order and the `0x0012` notify handle; the other handles
are derived from the attribute-table layout and confirmed on the bench.

Advertising: lamps use **RPA (rotating private addresses)** — connect to the
discovered object immediately, never a cached MAC `[ADDON/ESP32]`. Filter:
complete local name = mesh name and/or manufacturer data key **`0x0211`**.
Unprovisioned lamps advertise name `out_of_mesh`. Lamps **stop advertising
while connected**; only one GATT client per lamp at a time.

---

## 3. Packet Framing (20 bytes, command char `…1912`)

```
offset  size  field
[0..2]  3     seq   24-bit LE — monotonic per lamp, persist, start ≥ 0x1000
                    (lamp dedup window ±0x3F)
[3..4]  2     MIC   2-byte CBC-MAC over plaintext bytes [5..19], filled by encrypt
[5..6]  2     dst   target address LE (unicast 1..250 / group 0x8001..0xFFFE / all 0xFFFF)
[7]     1     opcode  (mesh-layer ops are sent as op|0xC0)
[8..9]  2     vendor ID 0x0211 LE → bytes 11 02
[10..19] 10   params, zero-padded
```

- Bytes `[0..5)` stay **plaintext** (seq + MIC); `[5..20)` is CTR-encrypted (§4).
- **Send every command twice**, ~0.2 s apart (flood-mesh redundancy).
- Notification (device → us, 20 B, handle `0x0012`): plaintext `[0..3)` seq,
  `[3..4)` src addr, `[5..6)` MIC, `[7..20)` encrypted op|vendor|params.
  Decrypt with IVS; opcode at `[7]`, verify vendor == `0x0211` at `[8..9]`.

### Addressing `[APK]`
- Unicast device addresses: **1…250** (app allocates lowest free — `[ADDON*]`
  `lamp_registry.allocate_unicast_addr`); group `0x8001…0x8010` in the app, protocol
  accepts `0x8000…0xFFFE`; `0xFFFF` = all devices
- Node-side match: group iff `dst & 0x8000`, `0x0000` dst = this device `[FW]`

---

## 4. Crypto Specification (exact)

All AES = **AES-128-ECB single block** with the Telink quirk
`java_aes(key, data) = reverse(AES-ECB(reverse(key), reverse(data)))`
(`[ADDON] telink_crypto.java_aes` — first arg is the **key**; `[ESP32]`
`crypto_esp32.java_aes(out, key, data)` agrees).

```
name16  = mesh name ASCII, zero-padded to 16
pwd16   = password  ASCII, zero-padded to 16
baseKey = name16[i] XOR pwd16[i]        (16 bytes — the only "key derivation")
```

### 4.1 Login handshake (pair char `…1914`) — ops `0x0C`/`0x0D`

| Step | Direction | Bytes |
|---|---|---|
| 1 | write 17 B | `0x0C ‖ r_app(8) ‖ challenge(8)` where `challenge = java_aes(r_app‖00…00, baseKey)[0..7]` |
| 2 | read 17 B | `0x0D ‖ r_dev(8) ‖ sample_s(8)` where `sample_s = java_aes(r_dev‖00…00, baseKey)[0..7]` — verify, else wrong password |
| 3 | derive | `sessionKey = java_aes(baseKey, r_app ‖ r_dev)` (16 B) |

**Bootstrap variant for factory-fresh lamps** `[ADDON/ESP32]`: same handshake
with fixed `r_app = A0 A1 A2 A3 A4 A5 A6 A7` — accepted only by lamps that are
not yet part of any mesh.

### 4.2 Command encryption (IVM)

```
ivm = reverseMAC[0..5] ‖ 0x01 ‖ seq(3 B LE)        (8 bytes)
MIC:  block0 = ivm ‖ 15 ‖ 00…00 → AES-ECB; CBC-MAC over plaintext[5..20)
      → MIC[0..2) → packet[3..5)
CTR:  counterBlock(n) = (16n) ‖ ivm ‖ 00…00; keystream = AES(counterBlock)
      packet[5+i] = plain[5+i] XOR ks[i mod 16]    (refresh keystream per 16 B)
```

### 4.3 Notification decryption (IVS)

```
ivs = reverseMAC[0..2] ‖ pkt[0..4]        (= revMAC[3] ‖ seq3 ‖ src2, 8 bytes)
decrypt packet[7..20) with the same CTR scheme; MIC over decrypted [7..20) vs [5..7)
```

### 4.4 Mesh-info writes (pair char, after login)

Each frame: `op ‖ AES_enc(sessionKey, payload16) ‖ flag` written to `…1914`:

| Op | Meaning | Payload |
|---|---|---|
| `0x04` | set mesh name | new name16 (zero-padded) |
| `0x05` | set password | new pwd16 |
| `0x06` | set LTK | new LTK16 **‖ 0x01** (trailing `0x01` = mesh-wide LTK flag — frame is **18 B**, not 17) |
| `0x07`/`0x0F` | pair-state verify (read) | success states `[ADDON/ESP32]` |
| `0x08` | get mesh LTK | response `0x09`, AES-wrapped `[ADDON]` |
| `0x0A` | reset mesh / delete pairing | `[0x0A] ‖ rand(8) ‖ proof(8)`, proof = `java_aes(baseKey, rand‖00…)[8..16]`; confirm = pair state `0x0B` `[ESP32]` — **⚠ doc-notation ambiguity: use key=baseKey per the ESP32 C code; T6 pending** |
| `0x0E` | legacy bare delete-pairing write | no payload — `[ADDON]` historical fallback, validation status unknown |

---

## 5. Opcode Tables

### 5.1 App layer (opcode as sent, byte `[7]`) — 10 param bytes max

| Op | Name | Params | Provenance |
|---|---|---|---|
| `0xD0` | on/off | `0x01` on / `0x00` off | `[ADDON/APK/ESP32]` |
| `0xD2` | brightness | `0..100` | ditto |
| `0xD4` | **short group rsp** | group ids as `0x80XX` single bytes, `0xFF`-terminated (represents 0x8001..0x80FF) | `[APK/ESP32]` |
| `0xD7` | group add/del | `0x01` add / `0x00` del, grpLo, grpHi — target = device unicast addr | `[ADDON/APK/ESP32]` |
| `0xDA` | status query | `[0x10, …]` | `[ADDON/ESP32]` |
| `0xDB` | status rsp | `p[3]`=ct (hw: 0=warm…100=cool), `p[5]`=on, `p[6]`=bri, `p[7..9]`=RGB | `[ADDON/ESP32]` |
| `0xDC` | online-status notify | 4 B records `[addr,status,bri,rsv]`, addr==0 ends | `[APK]` (unused by `[ADDON]` so far) |
| `0xDD` | **short group query** | `[0x10 notify-req, 0x01 selector]` → `0xD4` | `[APK/ESP32]` |
| `0xE0` | assign device address | addrLo, addrHi (target `0x0000` = connected dev) | `[APK/ADDON/ESP32]` |
| `0xE1` | address confirm (rsp) | addrLo, addrHi — **sent unencrypted**, ≤4 s after `0xE0` | `[APK/ESP32]` |
| `0xE2` | light config | `0x04`+RGB / `0x05`+ct (hw 0=warm…100=cool) / `0x07`+5ch | `[ADDON/ESP32]` |
| `0xE3` | kick out / factory reset | `0x01` — target = device addr | `[APK/ADDON/ESP32]` |
| `0xE4`/`0xE8` | set/get device time | `[yrLo,yrHi,M,D,h,m,s,0]` | `[APK/ADDON]` |
| `0xEA` | "user all" ext. cmd | `[0x0A, sub, …]` — sub `0x09`=set device id (`macRev6‖addrLE`), `0x0F`+`0x01`=soft reset, `0x12`=cycle | `[APK/ADDON]` |
| `0xEE`/`0xEF` | scene store/recall | add `[01,id,bri,R,G,B,ct,0,0]` / del `[00,id]` / clear `[00,FF]` / recall `[id]` | `[APK/ADDON/ESP32]` |
| `0xC6` | mesh OTA ctrl | start `[FF,FF,code]` / stop `[FE,FF]` | `[APK]` |
| `0xC7`→`0xC8` | fw version query | `[0x10,0x00,…]` | `[APK/ADDON]` |
| `0xF4` | misc: mesh-pair broadcast | `[3,flags,interval,times,keepHi,keepLo,page,range]`; stop `[4,…]` | `[APK]` |
| `0xF5` | set mesh info (bcast) | `[name4,pw4,devType,flag]` | `[APK]` |

### 5.2 Mesh layer (flood frames; opcode sent as `op|0xC0`, dispatch `op & 0x3F`)

| Op | Name | Params | Provenance |
|---|---|---|---|
| `0x09` | mesh pair broadcast | 6-frame key distribution (name1/2, pwd1/2, ltk1/2), to `0xFFFF`, every 500 ms | `[FW]` (disabled by default in fw build) |
| `0x17` | group add/del (mesh) | `0x01` add / `0x00` del, grpLo, grpHi | `[FW/ADDON]` |
| `0x1A`→`0xDB` | status query (mesh) | selector byte: 0=status | `[FW/ADDON]` |
| `0x1D`→`0x14/0x15/0x16` | group query (mesh) | selector: 1/2/3 | `[FW/ADDON]` |
| `0x20`→`0xE1` | set dev addr (mesh) | addrLE24, `p[2]=0x01`+MAC6 = MAC-targeted | `[FW]` |
| `0x23` | kick out (mesh-wide) | `p[0]`=reason (0=OutOfMesh) → factory reset + reboot | `[FW/ADDON]` |
| `0x30` | set light cw/ww | `p[8]` bitmask: bit0=bri `p[0..2]` LE, bit1=CCT `p[2..4]` (ww=v, cw=0x7FFF−v), bit2=ind. cw/ww; scale 0…0x7FFF | `[FW/ADDON]` |
| `0x31` | set MAC address | MAC6 → reboot | `[FW]` |

Responses carry `op|0xC0`; node stores **max 8 group addrs** (`[FW]`), 16 in
the app registry.

### 5.3 Known-good factory credentials

| Mesh | Password | Context |
|---|---|---|
| `out_of_mesh` | `123` | factory default, lamp joinable `[FW/APK/ADDON/ESP32]` |
| `telink_mesh1` | `123` | BLTC app factory name `[APK]` |
| `Smart_nSpq` | `0000` | lamp 1 (isolated mesh) |
| `Smart_qXsx` | `1234` | lamp 2+ (shared mesh, bench) — production deployment uses `8888` |
| LTK default | `C0 C1 C2 C3 C4 C5 C6 C7 D8 D9 DA DB DC DD DE DF` | written at provisioning `[ADDON/ESP32]` |

`[ADDON*]` discovery probes `8888, 1234, 0000, 123` (config `KNOWN_PASSWORDS`,
env-overridable). No NetKey/AppKey exist in the legacy protocol; the LTK is
the only extra key material.

---

## 6. Operational Recipes (exact sequences)

### 6.1 Control `[ADDON/ESP32]` — production-validated
```
connect(RPA from adv) → login(§4.1) → write 0x01 to …1911 value
→ [send twice, 0.2 s apart] e.g. on/off: dst=<addr|0xFFFF>, op=0xD0, params=[01]
```

### 6.2 Provision a factory lamp (Add)
1. Scan; select lamp advertising mesh name `out_of_mesh` / mfr `0x0211`.
2. Connect → **bootstrap login** `r_app = A0…A7`, baseKey from `out_of_mesh`/`123`.
3. Allocate free unicast address (lowest free of 1..250 vs registry).
4. Write cmd char: `0xE0, params=[addrLo, addrHi]`, dst `0x0000`; expect
   **unencrypted** `0xE1` notify with the address within ~4 s
   (mismatch → adopt the reported address `[ESP32]`; `[ADDON*]` watcher:
   `telink_ble.AddrConfirmWatcher`).
5. Pair char writes (~0.2–0.3 s apart, each AES(sessionKey)-wrapped, §4.4):
   `0x04` new mesh name → `0x05` new password → `0x06` new LTK ‖ `0x01`.
6. Read pair char → expect pair state `0x07`/`0x0F`.
7. Reconnect with new credentials → control test (0xD0) → registry upsert.

`[ADDON*]` implementation: `web_app._provision_direct` (+ `provision_lamp.py`).

### 6.3 Kick / factory reset (Remove)
- Per-lamp: dst = device addr, `0xE3, params=[0x01]` (app layer, verified `[ESP32/ADDON]`).
- Mesh-wide broadcast: `0x23, params=[reason]` (dst `0xFFFF`).
- Node side effect `[FW]`: full factory reset → name `out_of_mesh`, pwd `123`,
  default LTK, address-validation-pending flag set, reboot.
- Pair-char alternative: `0x0A` delete-pairing (proof per §4.4) — `[ESP32]`
  implements, `[ADDON*]` mirrors; **validate on hardware (T6)** — the `0x0A`
  op was a stub in the Rust fw re-implementation.

### 6.4 Re-add a kicked lamp
= §6.2 verbatim (kick returns the lamp to the factory-joinable state).

### 6.5 Grouping
```
add member:    dst=<device addr>, op=0xD7, params=[01, grpLo, grpHi]
remove member: params=[00, grpLo, grpHi]
group command: dst=<0x8001..>, op=0xD0/0xD2/0xE2/0x30 … (injected via ONE relay lamp)
read groups:   dst=<device addr>, op=0xDD, params=[0x10, 0x01] → rsp 0xD4 (short)
               or mesh-layer 0x1D selector 1/2/3 → rsp 0x14/15/16
```

Group membership lives **only on the lamp** (max 8 `[FW]`); keep an app-side
registry for the UI. `[ADDON*]`: `groups.json` is bookkeeping; sync it from a
lamp with `POST /api/groups/sync` (0xDD → 0xD4). The short format can only
report group addresses 0x8001..0x80FF — higher addresses stay
registry-authoritative (see the sync route's removal guard).

---

## 7. Implementation Status in this Add-on `[ADDON*]`

| Flow | Where | Bench status |
|---|---|---|
| Control (on/off, brightness, CCT, scenes, time, fw ver) | `web_app.py`, `telink_cli.py`, daemon | production-validated |
| Status query 0xDA→0xDB (+ mesh 0x1A→0x1B) | `telink_daemon.py`, `web_app._query_route` | production-validated |
| Group add/del 0xD7 / mesh 0x17, group query 0x1D→0x14 | `web_app.py` | bench-validated (2026-08-31) |
| Short group query **0xDD→0xD4** + `/api/groups/sync` | `web_app.py` | **new — unvalidated** (structure per `[APK/ESP32]`) |
| Provision (bootstrap login, 0xE0, name/pwd/LTK push, pair-state verify) | `web_app._provision_direct`, `provision_lamp.py` | bench-validated |
| Bootstrap creds auto-fallback (factory → target, ESP32 recipe) | `web_app._provision_direct` | **new — unvalidated** |
| **0xE1 unencrypted address confirm** (adopt reported addr) | `telink_ble.AddrConfirmWatcher` | **new — unvalidated** (needs HCI monitor = sidecar/bench host) |
| Kick 0xE3 / mesh 0x23 | `web_app.py` | 0xE3 validated; 0x23 alternative |
| Delete-pairing **0x0A+proof** (fallback: legacy bare 0x0E) | `provision_lamp.delete_pairing_proof` | **new — unvalidated (T6)** |
| Get LTK (pair op 0x08→0x09) | `provision_lamp.get_mesh_ltk` | validated |
| Seq persistence (dedup window ±0x3F, start ≥0x1000) | `telink_mesh.SequenceManager`, `lamp_registry` | production-validated |
| Notify readback (value-write subscribe + HCI monitor; GATT-read 1913 fallback) | `telink_ble.py`, `telink_daemon.py` | production-validated (sidecar only) |

---

## 8. Bench / HA Test Checklist (after this port)

Pre-flight: phone app BT off (one-client rule), lamp states known, HCI monitor
available (bench host or Variant B sidecar) for 0xE1 confirm.

| # | Test | Pass |
|---|---|---|
| D0 | Regression: HA light entities on/off/brightness/CCT | unchanged behavior |
| D1 | Delete-pairing via UI/API → log shows `0x0A proof … 0x0B` | lamp re-advertises `out_of_mesh`; if only legacy 0x0E works, record pair state |
| D2 | Provision the kicked lamp (`addr:"auto"`) | log shows `0xE1 confirm: addr 0x…`; registry stores lamp-reported address; control works |
| D3 | 0xE1 mismatch: provision with `addr` already used by another lamp | watcher adopts reported addr, registry updated, no duplicates |
| D4 | Group sync: add lamp to group (0xD7) → `/api/groups/sync` | reported list matches groups.json; stale membership removed |
| D5 | Discovery: factory lamp advertising `out_of_mesh` | found via `123` probe; in-container fallback (no monitor) still provisions via blind settle |
| D6 | In-container provision (no HCI monitor) | falls back to sleep-settle, pair-state verify still gates success |

---

## 9. Open Questions / Discrepancies

1. **CCT opcode:** `[ADDON]` uses `0xE2 0x05` (bench-validated); led-server/
   http-api use `0xF2` with naive crypto — broken/stale. Stick with `0xE2 0x05`.
2. **`0x0A` delete-pairing proof:** was a stub in the Rust fw; ESP32 C code
   uses key=baseKey. D1 decides on real hardware; bare `0x0E` kept as fallback.
3. **Mesh-layer status field order** `[FW]`: rsp `0x1B` = cw, ww, brightness at
   val[3..9] — differs from app-layer `0xDB` layout. Only relevant if we ever
   parse mesh-layer status (we do in `mesh-get-status`; field order there
   follows `[FW]`).
4. **Mesh-pair flood (`0x09`)** disabled by default in fw; app uses `0xF4`-driven
   broadcast instead. Advanced/optional — not implemented in the add-on.
5. **RGB opcodes absent** in the CCT-only TLSR8266 fw; our lamps are CCT-only —
   `0xE2 0x04` kept for RGB-capable models only.
6. **`0xDC` online-status notify** parsed by neither addon nor ESP32 yet —
   candidate for faster bulk-online detection later.
7. Handles `0x0015/0x0018/0x001B` are derived + bench-confirmed, not spec'd
   constants.
