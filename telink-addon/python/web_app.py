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
from config import CHAR_NOTIFY_UUID, KNOWN_PASSWORDS, SCAN_TIMEOUT
from telink_ble import TelinkController, probe_lamp, scan_for_telink_lamps
from telink_cli import BROADCAST, _try_daemon, _try_daemon_query, cmd_assign_addr, run_on_lamp

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
        # Broadcast to all: the daemon sends on every connected session.
        selector, send_mac, expected = "all", None, len(targets)
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
    return jsonify({"running": os.path.exists("/tmp/telink-ble.sock")})


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