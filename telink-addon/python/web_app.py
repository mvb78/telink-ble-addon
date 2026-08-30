"""
web_app.py — Flask web UI for Telink BLE CLI.

Run:   python web_app.py
Open:  http://localhost:5000

All commands route daemon-first (try /tmp/telink-ble.sock, fall back to direct BLE).
"""

import asyncio
import datetime
import os
import sys
import threading

from flask import Flask, jsonify, render_template, request

import group_registry as group_registry
import lamp_registry as registry
from config import CHAR_COMMAND_UUID, CHAR_NOTIFY_UUID, CHAR_PAIR_UUID, KNOWN_PASSWORDS, SCAN_TIMEOUT
from telink_ble import TelinkController, probe_lamp, scan_for_telink_lamps
from telink_cli import BROADCAST, _try_daemon, _try_daemon_query, _try_daemon_read, _stop_daemon, cmd_assign_addr, run_on_lamp

app = Flask(__name__)


@app.before_request
def _log_request():
    _log(f"HTTP {request.method} {request.path}")


def _log(msg: str) -> None:
    """Write a line to stderr so it appears in `ha apps logs` (flushed)."""
    print(f"[web] {msg}", file=sys.stderr, flush=True)

# ── discovery state ──────────────────────────────────────────────────────

_discovery = {"running": False, "result": None}


# ── helpers ──────────────────────────────────────────────────────────────

def _run_async(coro):
    """Run an async coroutine from a sync Flask route."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _get_targets(data):
    """Resolve lamp targets from request JSON."""
    lamps = registry.load()
    if not lamps:
        return []
    mac = data.get("mac")
    addr = data.get("addr")
    selector = data.get("selector", "all")
    if mac:
        return [l for l in lamps if l["mac"].upper() == mac.upper()]
    if addr is not None:
        return [l for l in lamps if l.get("mesh_address") == int(addr)]
    return registry.get_targets(lamps, selector, None, None)


async def _execute(opcode, params, targets, dst=None, mac=None):
    """Execute a command with daemon-first routing.

    For a plain broadcast command the mesh fabric relays one packet to every
    member, so we ask the daemon (mac=None -> all sessions) and only fall back
    to direct connect if the daemon can't cover them. When a specific `dst`
    (group/unicast) is given, one relay lamp is enough -> expected=1.
    """
    if not targets:
        return False, "No targets"
    if dst is not None:
        # Explicit mesh destination: one relay lamp injects the packet.
        selector, send_mac, expected = "all", targets[0]["mac"], 1
    elif mac is not None:
        selector, send_mac, expected = "all", mac, 1
    else:
        # Broadcast to all: the mesh fabric relays ONE packet to every member,
        # so any connected session is enough; we accept whatever the daemon did.
        selector, send_mac, expected = "all", None, None
    # Packet destination: explicit group dst wins; then unicast to a single
    # provisioned lamp's own mesh address (so one lamp ≠ all lamps); else broadcast.
    if dst is not None:
        packet_address = dst
    elif mac is not None and len(targets) == 1 and targets[0].get("mesh_address"):
        packet_address = int(targets[0]["mesh_address"])
    else:
        packet_address = BROADCAST
    if _try_daemon(opcode, params, selector, send_mac, packet_address, expected_count=expected):
        return True, "OK (daemon)"
    ok = await run_on_lamp(targets[0], opcode, params, packet_address)
    return (True, "OK (direct)") if ok else (False, "direct connect failed")


async def _query(opcode, params, response_opcode, parse_fn, targets):
    """Send a query command and collect responses from all targets."""
    results = []
    for lamp in targets:
        ctrl = TelinkController(lamp["mac"], lamp["name"], lamp["password"])
        try:
            await ctrl.connect()
            await ctrl.login()
            await ctrl.send_command(opcode, params, BROADCAST)
            await asyncio.sleep(0.1)
            await ctrl.send_command(opcode, params, BROADCAST)
            pkt = await ctrl.wait_for_opcode(response_opcode, timeout=3.0)
            if pkt:
                results.append({"lamp": lamp["name"], "result": parse_fn(pkt)})
            else:
                results.append({"lamp": lamp["name"], "result": "no response"})
        except Exception as e:
            results.append({"lamp": lamp["name"], "error": str(e)})
        finally:
            await ctrl.disconnect()
    return results


def _cmd(opcode, params, data, dst=None):
    """Common command helper — runs async execute in sync Flask context."""
    targets = _get_targets(data)
    mac = data.get("mac")
    ok, msg = _run_async(_execute(opcode, params, targets, dst=dst, mac=mac))
    return jsonify({"ok": ok, "msg": msg, "targets": len(targets)})


def _query_route(opcode, params, response_opcode, parse_fn, targets, data):
    """Run a query, preferring the daemon (which holds the BLE connections).

    Direct queries collide with the daemon because a Telink lamp allows only one
    active connection. The daemon proxies queries over its socket; this decodes
    its returned payloads. When the daemon is running, we NEVER fall back to a
    direct connect scan: that would both collide with the daemon's live
    connections and take ~30-60s per absent lamp (scan timeout). Instead we
    surface per-lamp offline markers so callers see the lamps as down fast.
    """
    mac = data.get("mac")
    dq = _try_daemon_query(opcode, params, response_opcode,
                           selector=data.get("selector", "all"), mac=mac)
    if dq["ok"]:
        out = []
        for r in dq["results"]:
            try:
                parsed = parse_fn(bytes(r["payload"]))
            except Exception as e:
                parsed = f"parse error: {e}"
            out.append({"lamp": r["name"], "result": parsed})
        return out
    # Daemon socket present but query failed (lamps asleep/offline).
    if dq.get("daemon") is True:
        out = []
        for t in targets:
            out.append({
                "lamp": t["name"],
                "result": {"error": f"{t['mac']} offline (daemon)"},
            })
        return out
    # No daemon running at all — fall back to a direct connect per lamp.
    return _run_async(_query(opcode, params, response_opcode, parse_fn, targets))


# ── pages ────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


# ── daemon API ───────────────────────────────────────────────────────────

@app.route("/api/daemon")
def api_daemon():
    paused = os.path.exists(os.path.join(_data_dir(), "daemon_paused"))
    return jsonify({"running": os.path.exists("/tmp/telink-ble.sock"),
                    "paused": paused})


def _data_dir():
    import config as _c
    return _c._DATA_DIR


@app.route("/api/daemon/pause", methods=["POST"])
def api_daemon_pause():
    d = _data_dir()
    open(os.path.join(d, "daemon_paused"), "w").close()
    return _run_sync(lambda: _stop_daemon()) or {"ok": True}


def _run_sync(fn):
    import threading
    result = {}
    def _w():
        result["ret"] = fn()
    t = threading.Thread(target=_w, daemon=True)
    t.start()
    t.join()
    return result.get("ret")


@app.route("/api/daemon/resume", methods=["POST"])
def api_daemon_resume():
    p = os.path.join(_data_dir(), "daemon_paused")
    if os.path.exists(p):
        os.unlink(p)
    # run.sh picks the flag up on its next loop and restarts the daemon.
    return {"ok": True}


@app.route("/api/debug/scan", methods=["POST"])
def api_debug_scan():
    """Read-only: raw BLE scan returning every advertisement (name + MAC + RSSI).

    No registry changes, no connects. Used to discover what name a lamp advertises
    when its credentials drifted (e.g. lamps 1 & 4 unreachable after re-provision).
    Caller should pause the daemon first so the single-connection lamps aren't held.
    """
    body = request.get_json(silent=True) or {}
    timeout = float(body.get("timeout", 10))

    async def _scan():
        from bleak import BleakScanner
        devs = {}
        def cb(device, adv):
            addr = device.address.upper()
            rssi = getattr(adv, "rssi", None) or getattr(device, "rssi", None)
            name = device.name or adv.local_name or ""
            devs.setdefault(addr, {"mac": addr, "name": name, "rssi": rssi})
        async with BleakScanner(cb) as s:
            await asyncio.sleep(timeout)
        return list(devs.values())

    results = _run_async(_scan())
    return jsonify({"ok": True, "results": results})


@app.route("/api/lamp/<mac>/creds", methods=["POST"])
def api_lamp_creds(mac):
    """Update a lamp's stored mesh login name & password in the registry.

    Point the daemon at the credentials the lamp physically currently uses
    (e.g. factory `Smart_mesh`/`8888`) without re-provisioning firmware.
    """
    data = request.get_json(silent=True) or {}
    name = data.get("name")
    password = data.get("password")
    if not name or not password:
        return jsonify({"ok": False, "msg": "name and password required"}), 400
    lamps = registry.load()
    lamp = next((l for l in lamps if l["mac"].upper() == mac.upper()), None)
    if not lamp:
        return jsonify({"ok": False, "msg": f"lamp {mac} not in registry"}), 404
    registry.upsert(lamps, lamp["mac"], name, password)
    registry.save(lamps)
    _log(f"creds {mac}: now {name}/{password}")
    return jsonify({"ok": True, "msg": f"{mac} creds -> {name}/{password}"})


# ── lamp API ─────────────────────────────────────────────────────────────

@app.route("/api/lamps")
def api_lamps():
    return jsonify(registry.load() or [])


@app.route("/api/lamp/<mac>/assign-addr", methods=["POST"])
def api_lamp_assign_addr(mac):
    data = request.get_json(silent=True) or {}
    addr = data.get("addr")
    if addr is None:
        return jsonify({"ok": False, "msg": "addr required"}), 400
    ok, msg = _run_async(cmd_assign_addr(mac, int(addr)))
    _log(f"assign-addr {mac} -> {addr}: {msg}")
    return jsonify({"ok": ok, "msg": msg})


@app.route("/api/lamp/<mac>/provision", methods=["POST"])
def api_lamp_provision(mac):
    """Fully provision one lamp (APK flow): pause daemon, direct-connect,
    login on pair char, set mesh address, set name/password/LTK, verify,
    then unpause. Only one lamp at a time because a Telink lamp accepts a
    single BLE connection."""
    data = request.get_json(silent=True) or {}
    addr = data.get("addr")
    name = data.get("name", "Smart_qXsx")
    password = data.get("password", "1234")
    current_name = data.get("current_name")
    current_password = data.get("current_password")
    if addr is None:
        return jsonify({"ok": False, "msg": "addr required"}), 400
    try:
        addr = int(addr)
    except (TypeError, ValueError):
        return jsonify({"ok": False, "msg": "addr must be an int"}), 400

    paused = os.path.exists(os.path.join(_data_dir(), "daemon_paused"))
    if not paused:
        _run_sync(lambda: _stop_daemon())
        open(os.path.join(_data_dir(), "daemon_paused"), "w").close()

    ok, msg = _run_async(_provision_direct(mac, addr, name, password, current_name, current_password, bootstrap=bool(data.get("bootstrap", False))))
    if ok:
        lamps = registry.load()
        registry.upsert(lamps, mac.upper(), name, password, mesh_address=addr)
        registry.save(lamps)
        _log(f"provision {mac} -> {addr}: registry updated")
    _log(f"provision {mac} -> {addr}: {msg}")
    return jsonify({"ok": ok, "msg": msg, "daemon_paused": True})


async def _provision_direct(mac, addr, name, password, current_name=None, current_password=None, bootstrap=False):
    """Replicate provision_lamp.py's APK flow, adapted to run in-container.

    `current_name`/`current_password` are the credentials the lamp currently keys
    with (used to log in when its credentials drifted, e.g. back to the MAC-string
    default). If omitted, the new `name`/`password` are used for login too.
    """
    try:
        import provision_lamp as pl
    except Exception as e:
        return False, f"provision_lamp import failed: {e}"
    try:
        from bleak import BleakScanner
        mac = mac.upper()
        mac_bytes = bytes.fromhex(mac.replace(":", ""))
        device = await BleakScanner.find_device_by_address(mac, timeout=15)
        if not device:
            return False, f"{mac} not found (daemon held/still down?)"
        login_name = current_name if current_name else name
        login_password = current_password if current_password else password
        from bleak import BleakClient
        async with BleakClient(device.address) as client:
            if bootstrap:
                # Brand-new / unprovisioned Telink node bootstrap: the APK uses a
                # FIXED nonce R_APP = A0..A7 for the initial 0x0C pairing write
                # (reverse-engineered from factory provisioning), rather than a
                # random r1. The lamp only accepts the pre-baked nonce when it is
                # not yet part of any mesh (e.g. right after a kick/reset).
                from telink_crypto import derive_base_key, build_challenge, get_session_key, verify_sample_s
                from config import CHAR_PAIR_UUID as _PAIR
                R_APP = bytes([0xA0, 0xA1, 0xA2, 0xA3, 0xA4, 0xA5, 0xA6, 0xA7])
                base_key = derive_base_key(login_name, login_password)
                challenge = build_challenge(base_key, R_APP)
                payload = bytearray(17)
                payload[0] = 0x0C
                payload[1:9] = R_APP
                payload[9:17] = challenge
                await client.write_gatt_char(_PAIR, bytes(payload), response=True)
                await asyncio.sleep(0.6)
                rsp = await client.read_gatt_char(_PAIR)
                if not rsp or rsp[0] != 0x0D or len(rsp) < 17:
                    return False, f"bootstrap login rejected: rsp=0x{rsp.hex() if rsp else 'none'}"
                r2 = bytes(rsp[1:9])
                sample_s = bytes(rsp[9:17])
                if not verify_sample_s(login_name, login_password, r2, sample_s):
                    return False, "bootstrap sample_s mismatch"
                session_key = get_session_key(login_name, login_password, R_APP, r2)
                _log(f"bootstrap login OK for {mac}")
            else:
                session_key = await pl.apk_login(client, login_name, login_password)
            params = bytes([addr & 0xFF, (addr >> 8) & 0xFF])
            from telink_mesh import SequenceManager, build_mesh_packet
            from telink_crypto import encrypt_packet
            seq = SequenceManager().next()
            packet = build_mesh_packet(seq, 0, 0xE0, params)
            await client.write_gatt_char(CHAR_COMMAND_UUID, encrypt_packet(session_key, packet, mac_bytes), response=False)
            await asyncio.sleep(4.0)
            await pl.set_mesh_param(client, session_key, 0x04, pl.normalize_16(name))
            await pl.set_mesh_param(client, session_key, 0x05, pl.normalize_16(password))
            default_ltk = bytearray([
                0xc0, 0xc1, 0xc2, 0xc3, 0xc4, 0xc5, 0xc6, 0xc7,
                0xd8, 0xd9, 0xda, 0xdb, 0xdc, 0xdd, 0xde, 0xdf])
            ltk_payload = pl.encrypt_pair_data(session_key, default_ltk)
            ltk_cmd = bytearray([0x06]) + ltk_payload + bytearray([0x01])
            await client.write_gatt_char(CHAR_PAIR_UUID, bytes(ltk_cmd), response=True)
            await asyncio.sleep(0.3)
            result = await client.read_gatt_char(CHAR_PAIR_UUID)
            state = result[0] if result else None
            if state in (0x07, 0x0F):
                ok = True
                msg = f"{mac} provisioned addr={addr} state=0x{state:02x}"
            else:
                ok = False
                msg = f"{mac} pair state 0x{state:02x} (unexpected), addr attempt {addr}"
            return ok, msg
    except Exception as e:
        import traceback
        tb = traceback.format_exc()
        _log(f"provision exception for {mac}: {tb}")
        return False, f"provision failed: {type(e).__name__}: {e}"


@app.route("/api/discover", methods=["POST"])
def api_discover():
    if _discovery["running"]:
        return jsonify({"status": "already running"}), 409

    def _worker():
        _discovery["running"] = True
        _discovery["result"] = None
        _log("discover: starting scan and probe")
        try:
            loop = asyncio.new_event_loop()
            devices = loop.run_until_complete(scan_for_telink_lamps(timeout=SCAN_TIMEOUT))
            _log(f"discover: scan returned {len(devices)} Telink device(s)")
            lamps = registry.load()
            found = 0
            for dev in devices:
                pw = loop.run_until_complete(probe_lamp(dev["mac"], dev["name"]))
                _log(f"discover: probe {dev['mac']} ({dev['name']}) -> password={pw}")
                if pw:
                    lamps = registry.upsert(lamps, dev["mac"], dev["name"], pw)
                    found += 1
            registry.save(lamps)
            _discovery["result"] = {"found": found, "total_devices": len(devices)}
            _log(f"discover: done, found={found}, total={len(devices)}")
            loop.close()
        except Exception as e:
            _discovery["result"] = {"error": str(e)}
            _log(f"discover: ERROR {e!r}")
        finally:
            _discovery["running"] = False

    threading.Thread(target=_worker, daemon=True).start()
    return jsonify({"status": "started"})


@app.route("/api/discover/status")
def api_discover_status():
    return jsonify(_discovery)


# ── group API ────────────────────────────────────────────────────────────

@app.route("/api/groups")
def api_groups():
    return jsonify(group_registry.load() or [])


@app.route("/api/groups", methods=["POST"])
def api_groups_create():
    data = request.get_json() or {}
    name = data.get("name", "").strip()
    if not name:
        return jsonify({"error": "name required"}), 400
    try:
        groups = group_registry.load()
        groups, created = group_registry.create(groups, name)
        group_registry.save(groups)
        return jsonify(created)
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@app.route("/api/groups/<name>", methods=["DELETE"])
def api_groups_delete(name):
    try:
        groups = group_registry.load()
        groups, removed = group_registry.delete(groups, name)
        group_registry.save(groups)
        return jsonify(removed)
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@app.route("/api/groups/<name>/add", methods=["POST"])
def api_group_add_lamp(name):
    data = request.get_json() or {}
    mac = data.get("mac")
    if not mac:
        return jsonify({"error": "mac required"}), 400
    groups = group_registry.load()
    group = group_registry.find_by_name(groups, name)
    if not group:
        return jsonify({"error": f"group '{name}' not found"}), 404
    addr = group["address"]
    lamps = registry.load()
    targets = [l for l in lamps if l["mac"].upper() == mac.upper()]
    if not targets:
        return jsonify({"error": f"lamp {mac} not found"}), 404
    p = bytes([0x01, addr & 0xFF, (addr >> 8) & 0xFF])
    ok, msg = _run_async(_execute(0xD7, p, targets))
    if ok:
        group_registry.add_member(group, mac)
        group_registry.save(groups)
    return jsonify({"ok": ok, "msg": msg})


@app.route("/api/groups/<name>/remove", methods=["POST"])
def api_group_remove_lamp(name):
    data = request.get_json() or {}
    mac = data.get("mac")
    if not mac:
        return jsonify({"error": "mac required"}), 400
    groups = group_registry.load()
    group = group_registry.find_by_name(groups, name)
    if not group:
        return jsonify({"error": f"group '{name}' not found"}), 404
    addr = group["address"]
    lamps = registry.load()
    targets = [l for l in lamps if l["mac"].upper() == mac.upper()]
    if not targets:
        return jsonify({"error": f"lamp {mac} not found"}), 404
    p = bytes([0x00, addr & 0xFF, (addr >> 8) & 0xFF])
    ok, msg = _run_async(_execute(0xD7, p, targets))
    if ok:
        group_registry.remove_member(group, mac)
        group_registry.save(groups)
    return jsonify({"ok": ok, "msg": msg})


# ── command API ──────────────────────────────────────────────────────────

@app.route("/api/command/on", methods=["POST"])
def api_on():
    data = request.get_json() or {}
    return _cmd(0xD0, bytes([1]), data, dst=data.get("dst"))


@app.route("/api/command/off", methods=["POST"])
def api_off():
    data = request.get_json() or {}
    return _cmd(0xD0, bytes([0]), data, dst=data.get("dst"))


@app.route("/api/command/brightness", methods=["POST"])
def api_brightness():
    data = request.get_json() or {}
    v = max(0, min(100, int(data.get("value", 50))))
    return _cmd(0xD2, bytes([v]), data, dst=data.get("dst"))


@app.route("/api/command/colortemp", methods=["POST"])
def api_colortemp():
    data = request.get_json() or {}
    v = max(0, min(100, int(data.get("value", 50))))
    return _cmd(0xE2, bytes([0x05, 100 - v]), data, dst=data.get("dst"))


@app.route("/api/command/rgb", methods=["POST"])
def api_rgb():
    data = request.get_json() or {}
    r = max(0, min(255, int(data.get("r", 255))))
    g = max(0, min(255, int(data.get("g", 0))))
    b = max(0, min(255, int(data.get("b", 0))))
    return _cmd(0xE2, bytes([0x04, r, g, b]), data, dst=data.get("dst"))


@app.route("/api/command/5ch", methods=["POST"])
def api_5ch():
    data = request.get_json() or {}
    w = max(0, min(255, int(data.get("w", 0))))
    r = max(0, min(255, int(data.get("r", 0))))
    g = max(0, min(255, int(data.get("g", 0))))
    b = max(0, min(255, int(data.get("b", 0))))
    cw = max(0, min(255, int(data.get("cw", 0))))
    ww = max(0, min(255, int(data.get("ww", 0))))
    return _cmd(0xE2, bytes([0x07, w, r, g, b, cw, ww]), data, dst=data.get("dst"))


@app.route("/api/command/scene", methods=["POST"])
def api_scene():
    data = request.get_json() or {}
    sid = max(1, min(16, int(data.get("id", 1))))
    return _cmd(0xEF, bytes([sid]), data)


@app.route("/api/command/scene-add", methods=["POST"])
def api_scene_add():
    data = request.get_json() or {}
    sid = max(1, min(16, int(data.get("id", 1))))
    brightness = max(0, min(100, int(data.get("brightness", 50))))
    r = max(0, min(255, int(data.get("r", 255))))
    g = max(0, min(255, int(data.get("g", 255))))
    b = max(0, min(255, int(data.get("b", 255))))
    ct = max(0, min(100, int(data.get("ct", 50))))
    p = bytes([0x01, sid, brightness, r, g, b, ct, 0, 0])
    return _cmd(0xEE, p, data)


@app.route("/api/command/scene-del", methods=["POST"])
def api_scene_del():
    data = request.get_json() or {}
    sid = max(1, min(16, int(data.get("id", 1))))
    return _cmd(0xEE, bytes([0x00, sid]), data)


@app.route("/api/command/scene-clear", methods=["POST"])
def api_scene_clear():
    data = request.get_json() or {}
    return _cmd(0xEE, bytes([0x00, 0xFF]), data)


@app.route("/api/command/cycle", methods=["POST"])
def api_cycle():
    data = request.get_json() or {}
    speed = max(0, min(255, int(data.get("speed", 100))))
    return _cmd(0xEA, bytes([0x0a, 0x12, 0x00, speed]), data)


@app.route("/api/command/reset", methods=["POST"])
def api_reset():
    data = request.get_json() or {}
    return _cmd(0xEA, bytes([0x0a, 0x0f, 0x01, 0x00]), data)


@app.route("/api/command/kick", methods=["POST"])
def api_kick():
    data = request.get_json() or {}
    return _cmd(0xE3, b"\x01", data)


@app.route("/api/command/time", methods=["POST"])
def api_time():
    now = datetime.datetime.now()
    p = bytes([
        now.year & 0xFF, (now.year >> 8) & 0xFF,
        now.month, now.day,
        now.hour, now.minute, now.second,
        0x00,
    ])
    data = request.get_json() or {}
    return _cmd(0xE4, p, data)


@app.route("/api/command/gettime", methods=["POST"])
def api_gettime():
    data = request.get_json() or {}
    targets = _get_targets(data)
    if not targets:
        return jsonify({"ok": False, "msg": "No targets"})

    def parse_time(pkt):
        p = pkt[10:]
        year = p[0] | (p[1] << 8)
        return f"{year:04d}-{p[2]:02d}-{p[3]:02d} {p[4]:02d}:{p[5]:02d}:{p[6]:02d}"

    results = _query_route(0xE8, bytes([0x08, 0, 0, 0, 0, 0, 0, 0, 0, 0]), 0xE9,
                           parse_time, targets, data)
    return jsonify({"ok": True, "results": results})


@app.route("/api/command/fwver", methods=["POST"])
def api_fwver():
    data = request.get_json() or {}
    targets = _get_targets(data)
    if not targets:
        return jsonify({"ok": False, "msg": "No targets"})

    def parse_fwver(pkt):
        p = pkt[10:]
        fw = "".join(chr(b) if 32 <= b < 127 else f"\\x{b:02x}" for b in p[1:5])
        return f"fw={fw.strip()}"

    results = _query_route(0xC7, bytes([0x10, 0x00, 0, 0, 0, 0, 0, 0, 0, 0]), 0xC8,
                           parse_fwver, targets, data)
    return jsonify({"ok": True, "results": results})


@app.route("/api/command/status", methods=["POST"])
def api_status():
    data = request.get_json() or {}
    targets = _get_targets(data)
    if not targets:
        return jsonify({"ok": False, "msg": "No targets"})

    def parse_status(pkt):
        p = pkt[10:]
        on_off = p[5]
        brightness = p[6]
        colortemp = 100 - p[3]
        r, g, b = p[7], p[8], p[9]
        state = "ON" if on_off else "OFF"
        if r or g or b:
            return {"state": state, "brightness": brightness, "rgb": [r, g, b]}
        return {"state": state, "brightness": brightness, "colortemp": colortemp}

    results = _query_route(0xDA, bytes([0x10, 0, 0, 0, 0, 0, 0, 0, 0, 0]), 0xDB,
                           parse_status, targets, data)
    return jsonify({"ok": True, "results": results})


@app.route("/api/command/inspect-address", methods=["POST"])
def api_inspect_address():
    """Read-only: report each lamp's ACTUAL mesh source address as seen in its own
    0xDB status response (bytes [5:7], little-endian), plus what lamps.json thinks.

    A status query is harmless (read-only); the daemon proxies it so we never
    collide with its live connections. This is how we verify what address the
    lamp firmware actually has after provisioning (0x0000 = unassigned).
    """
    data = request.get_json() or {}
    targets = _get_targets(data)
    if not targets:
        return jsonify({"ok": False, "msg": "No targets"})

    def parse_inspect(pkt):
        src_addr = pkt[5] | (pkt[6] << 8)
        return {
            "raw": pkt.hex(),
            "src_address": f"0x{src_addr:04x}",
        }

    results = _query_route(0xDA, bytes([0x10, 0, 0, 0, 0, 0, 0, 0, 0, 0]), 0xDB,
                           parse_inspect, targets, data)
    # Attach expected address from registry for easy comparison.
    expected = {l["mac"]: l.get("mesh_address") for l in registry.load()}
    for r in results:
        mac = next((t["mac"] for t in targets if t["name"] == r.get("lamp")), None)
        if mac:
            r["expected_address"] = expected.get(mac)
    return jsonify({"ok": True, "results": results})


@app.route("/api/lamp/read-status", methods=["POST"])
def api_lamp_read_status():
    """GATT-read the status characteristic (0d1913) on live daemon connections.

    This is a plain GATT read that needs no HCI-monitor notification capture,
    so it works inside the add-on. The lamp's returned bytes can reveal the
    current on/off state and (in some Telink builds) per-lamp identity.
    """
    data = request.get_json() or {}
    mac = data.get("mac")
    selector = data.get("selector", "all")
    dr = _try_daemon_read(mac=mac, selector=selector)
    if not dr["ok"]:
        return jsonify({"ok": False, "msg": dr["msg"], "results": [], "daemon": dr["daemon"]})
    out = []
    for r in dr["results"]:
        payload = bytes(r["payload"])
        out.append({
            "mac": r["mac"],
            "name": r["name"],
            "hex": payload.hex(),
            "len": len(payload),
        })
    _log(f"read-status: {len(out)} result(s)")
    return jsonify({"ok": True, "msg": dr["msg"], "results": out, "daemon": True})


@app.route("/api/debug/login", methods=["POST"])
def api_debug_login():
    """Raw login probe: connect directly to a lamp and dump the full pair-char
    exchange (request + raw response bytes) for a given name/password pair.

    No mesh commands, no provisioning — only the 0x0C login handshake. This tells
    us whether the lamp authenticates as in-mesh vs unprovisioned, and the exact
    failure byte. Caller must pause the daemon first.
    """
    body = request.get_json(silent=True) or {}
    mac = (body.get("mac") or "").upper()
    name = body.get("name", "Smart_qXsx")
    password = body.get("password", "1234")
    if not mac:
        return jsonify({"ok": False, "msg": "mac required"}), 400

    async def _probe():
        import os as _os
        from bleak import BleakScanner, BleakClient
        import provision_lamp as pl
        from config import CHAR_PAIR_UUID
        device = await BleakScanner.find_device_by_address(mac, timeout=15)
        if not device:
            return {"ok": False, "msg": f"{mac} not found"}
        out = {"mac": mac, "advertised": device.name}
        base_key = pl.derive_base_key(name, password)
        r1 = _os.urandom(8)
        challenge = pl.build_challenge(base_key, r1)
        payload = bytearray(17)
        payload[0] = 0x0C
        payload[1:9] = r1
        payload[9:17] = challenge
        async with BleakClient(device.address) as client:
            await client.write_gatt_char(CHAR_PAIR_UUID, bytes(payload), response=True)
            await asyncio.sleep(0.6)
            rsp = await client.read_gatt_char(CHAR_PAIR_UUID)
            out["tried_name"] = f"{name!r}"
            out["tried_password"] = f"{password!r}"
            if not rsp:
                out["response"] = "NO RESPONSE"
                return out
            out["response_byte"] = f"0x{rsp[0]:02x}"
            out["response_hex"] = rsp.hex()
            return out

    return jsonify({"ok": True, "result": _run_async(_probe())})


@app.route("/api/debug/bootstrap-login", methods=["POST"])
def api_debug_bootstrap_login():
    """Bootstrap (fixed R_APP) login probe for out-of-mesh/unprovisioned nodes.

    Mirrors the bootstrap used by the APK: fixed nonce R_APP=A0..A7 with
    base_key derived from the given name/password. The response first byte is
    0x0d on auth-OK and sample_s verification tells us the password is right —
    without doing any destructive provisioning writes. Caller must pause the
    daemon first.
    """
    body = request.get_json(silent=True) or {}
    mac = (body.get("mac") or "").upper()
    name = body.get("name", "Smart_qXsx")
    password = body.get("password", "1234")
    if not mac:
        return jsonify({"ok": False, "msg": "mac required"}), 400

    async def _probe():
        from bleak import BleakScanner, BleakClient
        from telink_crypto import derive_base_key, build_challenge, get_session_key, verify_sample_s
        from config import CHAR_PAIR_UUID
        device = await BleakScanner.find_device_by_address(mac, timeout=15)
        if not device:
            return {"ok": False, "msg": f"{mac} not found"}
        out = {"mac": mac, "advertised": device.name,
               "tried_name": f"{name!r}", "tried_password": f"{password!r}"}
        R_APP = bytes([0xA0, 0xA1, 0xA2, 0xA3, 0xA4, 0xA5, 0xA6, 0xA7])
        base_key = derive_base_key(name, password)
        challenge = build_challenge(base_key, R_APP)
        payload = bytearray(17)
        payload[0] = 0x0C
        payload[1:9] = R_APP
        payload[9:17] = challenge
        async with BleakClient(device.address) as client:
            await client.write_gatt_char(CHAR_PAIR_UUID, bytes(payload), response=True)
            await asyncio.sleep(0.6)
            rsp = await client.read_gatt_char(CHAR_PAIR_UUID)
            if not rsp:
                out["response"] = "NO RESPONSE"
                return out
            out["response_hex"] = rsp.hex()
            out["response_byte"] = f"0x{rsp[0]:02x}"
            if rsp[0] == 0x0D and len(rsp) >= 17:
                r2 = bytes(rsp[1:9])
                sample_s = bytes(rsp[9:17])
                out["sample_s_ok"] = verify_sample_s(name, password, r2, sample_s)
            return out

    return jsonify({"ok": True, "result": _run_async(_probe())})


if __name__ == "__main__":
    import os as _os
    _host = _os.environ.get("TELINK_WEB_HOST", "0.0.0.0")
    _port = int(_os.environ.get("TELINK_WEB_PORT", "5000"))
    _debug = _os.environ.get("TELINK_WEB_DEBUG", "0") in ("1", "true", "yes")
    _threaded = _os.environ.get("TELINK_WEB_THREADED", "1") in ("1", "true", "yes")
    # Prefer waitress (production WSGI) inside the add-on; it is installed there.
    # Keep the single-threaded Flask dev server only as an explicit fallback,
    # since it blocks on concurrent requests and its debug reloader forks.
    if _os.environ.get("TELINK_WEB_WSGI") in ("1", "true", "yes"):
        from waitress import serve
        serve(app, host=_host, port=_port, threads=8)
    else:
        app.run(host=_host, port=_port, debug=_debug, threaded=_threaded)