"""
telink_cli.py — control Telink BLE mesh lamps from the command line.

Opcodes verified from BT-Light APK (BltcMeshCommand) and btsnoop captures:
  0xD0  on/off
  0xD2  brightness (0-100)
  0xD7  add/remove group membership
  0xDA  status query
  0xE2  light property — sub-command byte selects mode:
          [0x04, R, G, B]             RGB color
          [0x05, value]               color temperature (0=warm, 100=cool)
          [0x07, W, R, G, B, CW, WW] 5-channel
  0xE3  kick out (remove lamp from mesh)
  0xE4  set time
  0xE8  get time
  0xEA  EA-expand family (reset, patterns, effects, info)
  0xEE  scene operation (add/remove)
  0xEF  scene recall
  0xC7  get firmware version
"""

import sys
import asyncio
import datetime
import json
import os
import signal
import socket as _socket
import time

import lamp_registry as registry
import group_registry as group_registry
from config import CHAR_NOTIFY_UUID, SCAN_TIMEOUT
from telink_ble import TelinkController, scan_for_telink_lamps, probe_lamp
from telink_mesh import build_redundant_packet

DAEMON_SOCK = "/tmp/telink-ble.sock"
BROADCAST = 0xFFFF


def _try_daemon(opcode: int, params: bytes, selector: str,
                mac: str | None, addr: int | None,
                expected_count: int | None = None) -> bool:
    """
    Route command to daemon if running.

    Returns True only when the daemon confirms success for all expected targets.
    Returns False to fall through to direct-connect mode.
    """
    if not os.path.exists(DAEMON_SOCK):
        return False
    req: dict = {"opcode": opcode, "params": list(params), "selector": selector,
                 "address": addr if addr is not None else BROADCAST}
    if mac:
        req["mac"] = mac
    try:
        sock = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
        sock.settimeout(5.0)
        sock.connect(DAEMON_SOCK)
        sock.sendall((json.dumps(req) + "\n").encode())
        data = b""
        while not data.endswith(b"\n"):
            chunk = sock.recv(256)
            if not chunk:
                break
            data += chunk
        sock.close()
        resp = json.loads(data)
        if resp.get("status") == "ok":
            count = resp.get("count")
            print(f"  [daemon] OK ({count if count is not None else '?'} lamp(s))")
            if expected_count is not None:
                if not isinstance(count, int):
                    print("  [daemon] missing/invalid lamp count — falling back to direct connect")
                    return False
                if count < expected_count:
                    print(f"  [daemon] partial coverage ({count}/{expected_count}) — falling back to direct connect")
                    return False
            return True

        print(f"  [daemon] ERROR: {resp.get('msg', '?')} — falling back to direct connect")
        return False
    except (ConnectionRefusedError, FileNotFoundError):
        return False
    except Exception as e:
        print(f"  [daemon] error: {e} — falling back to direct connect")
        return False


def _try_daemon_query(opcode: int, params: bytes, response_opcode: int,
                      selector: str = "all", mac: str | None = None):
    """
    Route a query command through the daemon and collect per-lamp responses.

    The daemon holds the BLE connections, so a direct query would collide with
    it (a Telink lamp allows only one connection). Returns the daemon's
    {"ok": bool, "results": [...], "msg": str, "daemon": bool} so the caller can
    decode notifications without opening its own connection. "daemon" is True
    when the daemon socket was present, so callers can tell an "all lamps
    offline" report from "no daemon running".
    """
    if not os.path.exists(DAEMON_SOCK):
        return {"ok": False, "results": [], "msg": "daemon not running", "daemon": False}
    req: dict = {"kind": "query", "opcode": opcode, "params": list(params),
                 "response_opcode": response_opcode, "selector": selector}
    if mac:
        req["mac"] = mac
    try:
        sock = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
        sock.settimeout(10.0)
        sock.connect(DAEMON_SOCK)
        sock.sendall((json.dumps(req) + "\n").encode())
        data = b""
        while not data.endswith(b"\n"):
            chunk = sock.recv(4096)
            if not chunk:
                break
            data += chunk
        sock.close()
        resp = json.loads(data)
        if resp.get("status") == "ok":
            return {"ok": True, "results": resp.get("results", []),
                    "msg": (resp.get("errors") or ["all lamps responded"])[0] if resp.get("errors") else "OK (daemon)",
                    "daemon": True}
        return {"ok": False, "results": [], "msg": resp.get("msg", "query failed"),
                "daemon": True}
    except (ConnectionRefusedError, FileNotFoundError):
        return {"ok": False, "results": [], "msg": "daemon not running", "daemon": False}
    except Exception as e:
        return {"ok": False, "results": [], "msg": str(e), "daemon": True}


async def run_on_lamp(lamp: dict, opcode: int, params: bytes, mesh_address: int = BROADCAST):
    ctrl = TelinkController(lamp["mac"], lamp["name"], lamp["password"])
    try:
        await ctrl.connect()
        await ctrl.login()
        seq = ctrl.seq_manager.next()
        p1, p2 = build_redundant_packet(seq, mesh_address, opcode, params)
        await ctrl.send_packet(p1)
        await asyncio.sleep(0.2)
        await ctrl.send_packet(p2)
        await asyncio.sleep(0.5)
        print(f"  [{lamp['name']}] OK")
        return True
    except Exception as e:
        print(f"  [{lamp['name']}] FAILED: {e}")
        return False
    finally:
        await ctrl.disconnect()


async def cmd_assign_addr(mac: str, addr: int):
    """Assign a unicast mesh address to one lamp (opcode 0xE0) and persist it."""
    lamps = registry.load()
    lamp = next((l for l in lamps if l["mac"].upper() == mac.upper()), None)
    if not lamp:
        print(f"Lamp {mac} not in lamps.json — run 'discover' first.")
        return
    ctrl = TelinkController(lamp["mac"], lamp["name"], lamp["password"])
    try:
        await ctrl.connect()
        await ctrl.login()
        seq = ctrl.seq_manager.next()
        params = bytes([addr & 0xFF, (addr >> 8) & 0xFF])
        p1, p2 = build_redundant_packet(seq, 0, 0xE0, params)
        await ctrl.send_packet(p1)
        await asyncio.sleep(0.4)
        await ctrl.send_packet(p2)
        await asyncio.sleep(0.5)
        registry.upsert(lamps, lamp["mac"], lamp["name"], lamp["password"], mesh_address=addr)
        print(f"  [{lamp['name']}] assigned mesh address {addr} (0x{addr:04x})")
    except Exception as e:
        print(f"  [{lamp['name']}] FAILED: {e}")
    finally:
        await ctrl.disconnect()


async def run_on_all(targets: list[dict], opcode: int, params: bytes, unicast: bool = False):
    """Connect to each lamp individually; use unicast to its mesh address or broadcast."""
    for lamp in targets:
        dest = lamp.get("mesh_address", BROADCAST) if unicast else BROADCAST
        await run_on_lamp(lamp, opcode, params, dest)


async def query_lamp(lamp: dict, opcode: int, params: bytes, response_opcode: int,
                     parse_fn, unicast: bool = False):
    """Send a query command and wait for a response notification."""
    ctrl = TelinkController(lamp["mac"], lamp["name"], lamp["password"])
    dest = lamp.get("mesh_address", BROADCAST) if unicast else BROADCAST
    try:
        await ctrl.connect()
        await ctrl.login()
        await ctrl.send_command(opcode, params, dest)
        pkt = await ctrl.wait_for_opcode(response_opcode, timeout=3.0)
        if pkt:
            result = parse_fn(pkt)
            print(f"  [{lamp['name']}] {result}")
        else:
            print(f"  [{lamp['name']}] no response (opcode {response_opcode:#04x})")
    except Exception as e:
        print(f"  [{lamp['name']}] FAILED: {e}")
    finally:
        await ctrl.disconnect()


async def cmd_gatt_dump(lamp: dict):
    """
    Connect, enumerate characteristics, then poll the notify char for responses.
    The lamp pushes ATT_NOTIFY automatically after login but rejects CCCD writes,
    so we read the characteristic directly to capture whatever the lamp sends.
    """
    ctrl = TelinkController(lamp["mac"], lamp["name"], lamp["password"])
    try:
        await ctrl.connect()
        await ctrl.login()

        chars = await ctrl.dump_gatt()
        print(f"\n  [{lamp['name']}] Telink service characteristics:")
        for c in chars:
            props = ", ".join(c["properties"])
            descs = "  descriptors: " + ", ".join(c["descriptors"]) if c["descriptors"] else ""
            print(f"    handle=0x{c['handle']:04x}  uuid=...{c['uuid'][-4:]}  [{props}]{descs}")

        # Send a status query then poll for a response
        print(f"\n  [{lamp['name']}] Sending status query, polling ...1911 for 5 s ...")
        await ctrl.send_command(0xDA, bytes([0x10, 0, 0, 0, 0, 0, 0, 0, 0, 0]), BROADCAST)
        deadline = asyncio.get_event_loop().time() + 5.0
        seen: set[str] = set()
        while asyncio.get_event_loop().time() < deadline:
            try:
                raw = bytes(await ctrl.client.read_gatt_char(CHAR_NOTIFY_UUID))
                key = raw.hex()
                if key not in seen:
                    seen.add(key)
                    from telink_crypto import decrypt_notification
                    plain = decrypt_notification(ctrl.session_key, raw, ctrl.mac_bytes)
                    opcode = f"opcode=0x{plain[7]:02X}" if plain else "decrypt failed"
                    print(f"    raw: {key}  ({opcode})")
            except Exception as e:
                print(f"    read error: {e}")
                break
            await asyncio.sleep(0.15)

        if not seen:
            print(f"    Nothing read from ...1911 in 5 s.")

    except Exception as e:
        print(f"  [{lamp['name']}] FAILED: {e}")
    finally:
        await ctrl.disconnect()


async def cmd_discover():
    print(f"Scanning for Telink lamps ({SCAN_TIMEOUT} s) — disconnect the phone app first ...")
    devices = await scan_for_telink_lamps(timeout=SCAN_TIMEOUT)

    if not devices:
        print("No Telink lamps found.")
        return

    print(f"Found {len(devices)} device(s). Probing passwords ...")
    lamps = registry.load()

    for dev in devices:
        print(f"  {dev['name']} ({dev['mac']}) ...", end=" ", flush=True)
        password = await probe_lamp(dev["mac"], dev["name"])
        if password:
            print(f"password={password}")
            lamps = registry.upsert(lamps, dev["mac"], dev["name"], password)
        else:
            print("no matching password — skipped")

    registry.save(lamps)
    print()
    registry.print_table(lamps)


async def cmd_list():
    lamps = registry.load()
    if not lamps:
        print("No lamps saved. Run 'discover' first.")
        return
    registry.print_table(lamps)


async def cmd_list_groups():
    groups = group_registry.load()
    group_registry.print_table(groups)


async def cmd_probe_notify(targets: list[dict]):
    """Send status query, dump ALL decrypted notifications for 4s — find correct response opcode."""
    for lamp in targets:
        ctrl = TelinkController(lamp["mac"], lamp["name"], lamp["password"])
        try:
            await ctrl.connect()
            await ctrl.login()
            print(f"  [{lamp['name']}] collecting baseline notifications (2s before command)...")
            await ctrl.drain_notifications(duration=2.0)
            print(f"  [{lamp['name']}] sending 0xDA status query...")
            await ctrl.send_command(0xDA, bytes([0x10, 0, 0, 0, 0, 0, 0, 0, 0, 0]), BROADCAST)
            await asyncio.sleep(0.1)
            await ctrl.send_command(0xDA, bytes([0x10, 0, 0, 0, 0, 0, 0, 0, 0, 0]), BROADCAST)
            print(f"  [{lamp['name']}] waiting for response notifications (4s)...")
            pkts = await ctrl.drain_notifications(duration=4.0)
            if not pkts:
                print(f"  [{lamp['name']}] no notifications received after command")
        except Exception as e:
            print(f"  [{lamp['name']}] FAILED: {e}")
        finally:
            await ctrl.disconnect()


async def cmd_status(targets: list[dict]):
    """Query each lamp for its current state; decode 0xDB status response."""
    def parse_status(pkt: bytes) -> str:
        # Confirmed from observed packets (raw=00000014000105000000, colortemp=80, brightness=5):
        #   p[3] = colortemp hardware value (0=warm, 100=cool; inverse of CLI scale)
        #   p[5] = on_off (1=on, 0=off)
        #   p[6] = brightness (0-100)
        #   p[7..9] = R, G, B (unconfirmed — zero in white mode)
        p = pkt[10:]
        on_off = p[5]
        brightness = p[6]
        colortemp = 100 - p[3]   # convert hardware scale to CLI scale (0=cool, 100=warm)
        r, g, b = p[7], p[8], p[9]
        state = "ON" if on_off else "OFF"
        if r or g or b:
            return f"{state}  brightness={brightness}  rgb=({r},{g},{b})"
        return f"{state}  brightness={brightness}  colortemp={colortemp}"

    da_params = bytes([0x10, 0, 0, 0, 0, 0, 0, 0, 0, 0])
    for lamp in targets:
        ctrl = TelinkController(lamp["mac"], lamp["name"], lamp["password"])
        try:
            await ctrl.connect()
            await ctrl.login()
            await ctrl.send_command(0xDA, da_params, BROADCAST)
            await asyncio.sleep(0.1)
            await ctrl.send_command(0xDA, da_params, BROADCAST)
            pkt = await ctrl.wait_for_opcode(0xDB, timeout=3.0)
            if pkt:
                result = parse_status(pkt)
                print(f"  [{lamp['name']}] {result}")
            else:
                print(f"  [{lamp['name']}] no response")
        except Exception as e:
            print(f"  [{lamp['name']}] FAILED: {e}")
        finally:
            await ctrl.disconnect()


async def cmd_gettime(targets: list[dict]):
    def parse_time(pkt: bytes) -> str:
        p = pkt[10:]
        year = p[0] | (p[1] << 8)
        month, day = p[2], p[3]
        hour, minute, second = p[4], p[5], p[6]
        return f"{year:04d}-{month:02d}-{day:02d} {hour:02d}:{minute:02d}:{second:02d}"

    for lamp in targets:
        await query_lamp(lamp, 0xE8, bytes([0x08, 0, 0, 0, 0, 0, 0, 0, 0, 0]), 0xE9, parse_time)


async def cmd_fwver(targets: list[dict]):
    def parse_fwver(pkt: bytes) -> str:
        p = pkt[10:]
        fw = "".join(chr(b) if 32 <= b < 127 else f"\\x{b:02x}" for b in p[1:5])
        return f"fw={fw.strip()}"

    for lamp in targets:
        await query_lamp(lamp, 0xC7, bytes([0x10, 0x00, 0, 0, 0, 0, 0, 0, 0, 0]), 0xC8, parse_fwver)


def _stop_daemon():
    import telink_daemon

    pid = None
    try:
        with open(telink_daemon.PID_PATH) as f:
            pid = int(f.read().strip())
    except (FileNotFoundError, ValueError):
        pass

    if pid is None or not _pid_alive(pid):
        # Stale/missing PID file — clean up leftovers and report.
        for path in (telink_daemon.SOCK_PATH, telink_daemon.PID_PATH):
            if os.path.exists(path):
                os.unlink(path)
        print("Daemon is not running.")
        return

    os.kill(pid, signal.SIGTERM)
    print(f"Sent SIGTERM to daemon (pid {pid}), waiting for shutdown ...")
    deadline = time.time() + 10.0
    while time.time() < deadline:
        if not _pid_alive(pid):
            print("Daemon stopped.")
            return
        time.sleep(0.2)
    print("Daemon did not exit within 10 s — sending SIGKILL.")
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    for path in (telink_daemon.SOCK_PATH, telink_daemon.PID_PATH):
        if os.path.exists(path):
            os.unlink(path)


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


USAGE = """\
Usage (daemon — keeps connections alive for instant commands):
  python telink_cli.py daemon start                   start persistent daemon (foreground)
  python telink_cli.py daemon start &> /tmp/telink-ble.log &   start in the background
  python telink_cli.py daemon stop                    graceful shutdown of a running daemon

Usage (investigation):
  python telink_cli.py gatt-dump         [target]   list all GATT chars + test notify

Usage:
  python telink_cli.py discover                       scan and save all lamps
  python telink_cli.py list                           show saved lamps

  python telink_cli.py on            [target]         turn on
  python telink_cli.py off           [target]         turn off
  python telink_cli.py brightness <0-100>  [target]   dim/brighten
  python telink_cli.py colortemp  <0-100>  [target]   0=cool white, 100=warm white
  python telink_cli.py cw          <0-100>  [target]   colortemp alias: 100=full cool (same as colortemp 0)
  python telink_cli.py ww          <0-100>  [target]   colortemp alias: 100=full warm (same as colortemp 100)
  python telink_cli.py rgb <R> <G> <B>      [target]   RGB color (0-255 each)
  python telink_cli.py 5ch <W> <R> <G> <B> <CW> <WW>  5-channel direct (0-255)
  python telink_cli.py scene <1-16>         [target]   recall stored scene
  python telink_cli.py scene-add <id> <brightness> <R> <G> <B> <colortemp>  [target]
  python telink_cli.py scene-del <id>       [target]   delete a scene
  python telink_cli.py scene-clear          [target]   delete all scenes
  python telink_cli.py cycle <speed 0-255>  [target]   RGB cycle effect
  python telink_cli.py reset                [target]   soft reset lamp
  python telink_cli.py kick                 [target]   remove lamp from mesh
  python telink_cli.py list-groups                     list named groups
  python telink_cli.py create-group <name>             create named group (allocates 0x8000..0xFFFE)
  python telink_cli.py delete-group <name>             delete named group
  python telink_cli.py add-to-group <name>  [target]   add lamp(s) to named group
  python telink_cli.py remove-from-group <name> [target] remove lamp(s) from named group
  python telink_cli.py addgroup <addr>      [target]   add lamp to group address
  python telink_cli.py delgroup <addr>      [target]   remove lamp from group
  python telink_cli.py time                 [target]   sync current system time
  python telink_cli.py gettime              [target]   read lamp clock
  python telink_cli.py fwver               [target]   read firmware version
  python telink_cli.py status              [target]   query lamp state

Target flags (default: --all):
  --all              all saved lamps
  --isolated         lamps with password 0000
  --shared           lamps with password 1234
  --single           legacy alias for --isolated
  --group            legacy alias for --shared
  --mac <AA:BB:...>  one specific lamp by MAC
  --addr <N>         one specific lamp by mesh address (unicast)
  --dst <N>          destination mesh address for the command (e.g. group 0x8001)
"""


async def main():
    command, values, selector, mac, addr, dst = parse_args()

    if not command:
        print(USAGE)
        return

    if command == "daemon":
        subcmd = sys.argv[2] if len(sys.argv) > 2 else "start"
        if subcmd == "start":
            import telink_daemon
            await telink_daemon.start_daemon()
        elif subcmd == "stop":
            _stop_daemon()
        else:
            print("Usage: python telink_cli.py daemon start|stop")
        return

    if command == "discover":
        await cmd_discover()
        return

    if command == "list":
        await cmd_list()
        return

    if command in ("list-groups", "groups"):
        await cmd_list_groups()
        return

    if command == "create-group":
        group_name = _group_name_from_argv()
        if not group_name:
            print("Usage: create-group <name>")
            return
        try:
            groups = group_registry.load()
            groups, created = group_registry.create(groups, group_name)
            group_registry.save(groups)
            print(
                f"Created group '{created['name']}' at address {created['address']:#06x} ({created['address']})."
            )
        except Exception as e:
            print(f"Error: {e}")
        return

    if command == "delete-group":
        group_name = _group_name_from_argv()
        if not group_name:
            print("Usage: delete-group <name>")
            return
        try:
            groups = group_registry.load()
            groups, removed = group_registry.delete(groups, group_name)
            group_registry.save(groups)
            print(
                f"Deleted group '{removed['name']}' (address {removed['address']:#06x})."
            )
        except Exception as e:
            print(f"Error: {e}")
        return

    if command == "assign-addr":
        if mac is None or not values:
            print("Usage: assign-addr --mac <AA:BB:CC:DD:EE:FF> <1-63>")
            return
        await cmd_assign_addr(mac, values[0])
        return

    lamps = registry.load()
    if not lamps:
        print("No lamps saved. Run 'discover' first.")
        return

    try:
        targets = registry.get_targets(lamps, selector, mac, addr)
    except Exception as e:
        print(f"Error: {e}")
        return

    if not targets:
        print(f"No lamps match target '{selector}'.")
        return

    unicast = addr is not None
    v0 = values[0] if len(values) >= 1 else None

    # When --addr is used, resolve to a specific MAC so the daemon routes correctly
    daemon_mac = mac
    if addr is not None and targets:
        daemon_mac = targets[0]["mac"]

    # Packet destination address:
    #   --dst: explicit mesh destination (e.g. group address 0x8000+)
    #   --addr: unicast destination to that lamp mesh address
    #   --mac (single, provisioned): unicast to that lamp's own mesh address
    #   default: mesh broadcast
    if dst is not None:
        packet_address = dst
    elif addr is not None:
        packet_address = addr
    elif mac is not None and len(targets) == 1 and targets[0].get("mesh_address"):
        packet_address = int(targets[0]["mesh_address"])
    else:
        packet_address = BROADCAST

    # For explicit --dst, one relay lamp is enough to inject the packet into mesh.
    relay_targets = targets
    daemon_expected = len(targets)
    daemon_target_mac = daemon_mac
    if dst is not None:
        relay_targets = [targets[0]]
        daemon_expected = 1
        if daemon_target_mac is None:
            daemon_target_mac = relay_targets[0]["mac"]

    async def send_write(opcode: int, params: bytes):
        if not _try_daemon(
            opcode,
            params,
            selector,
            daemon_target_mac,
            packet_address,
            expected_count=daemon_expected,
        ):
            # One BLE connection is sufficient — the mesh fabric handles broadcast/relay
            await run_on_lamp(targets[0], opcode, params, packet_address)

    if command == "gatt-dump":
        for lamp in targets:
            await cmd_gatt_dump(lamp)
        return

    if command == "probe-notify":
        await cmd_probe_notify(targets)
        return

    if command == "on":
        await send_write(0xD0, bytes([1]))

    elif command == "off":
        await send_write(0xD0, bytes([0]))

    elif command == "brightness":
        if v0 is None or not (0 <= v0 <= 100):
            print("Usage: brightness <0-100>")
            return
        await send_write(0xD2, bytes([v0]))

    elif command == "colortemp":
        if v0 is None or not (0 <= v0 <= 100):
            print("Usage: colortemp <0-100>  (0=cool white, 100=warm white)")
            return
        # sub-cmd 0x05: hardware value 0=warm, 100=cool — invert so 0=cool feels right
        await send_write(0xE2, bytes([0x05, 100 - v0]))

    elif command == "cw":
        if v0 is None or not (0 <= v0 <= 100):
            print("Usage: cw <0-100>  (100=max cool white)")
            return
        await send_write(0xE2, bytes([0x05, v0]))

    elif command == "ww":
        if v0 is None or not (0 <= v0 <= 100):
            print("Usage: ww <0-100>  (100=max warm white)")
            return
        await send_write(0xE2, bytes([0x05, 100 - v0]))

    elif command == "rgb":
        if len(values) < 3 or not all(0 <= v <= 255 for v in values[:3]):
            print("Usage: rgb <R> <G> <B>  (0-255 each)")
            return
        r, g, b = values[0], values[1], values[2]
        await send_write(0xE2, bytes([0x04, r, g, b]))

    elif command == "5ch":
        if len(values) < 6 or not all(0 <= v <= 255 for v in values[:6]):
            print("Usage: 5ch <W> <R> <G> <B> <CW> <WW>  (0-255 each)")
            return
        w, r, g, b, cw, ww = values[:6]
        await send_write(0xE2, bytes([0x07, w, r, g, b, cw, ww]))

    elif command == "scene":
        if v0 is None or not (1 <= v0 <= 16):
            print("Usage: scene <1-16>")
            return
        await send_write(0xEF, bytes([v0]))

    elif command == "scene-add":
        # scene-add <id> <brightness> <R> <G> <B> <colortemp>
        if len(values) < 6 or not (1 <= values[0] <= 16):
            print("Usage: scene-add <id 1-16> <brightness 0-100> <R> <G> <B> <colortemp 0-100>")
            return
        scene_id, brightness, r, g, b, ct = values[:6]
        p = bytes([0x01, scene_id, brightness, r, g, b, ct, 0, 0])
        await send_write(0xEE, p)

    elif command == "scene-del":
        if v0 is None or not (1 <= v0 <= 16):
            print("Usage: scene-del <1-16>")
            return
        await send_write(0xEE, bytes([0x00, v0]))

    elif command == "scene-clear":
        await send_write(0xEE, bytes([0x00, 0xFF]))

    elif command == "cycle":
        speed = v0 if v0 is not None else 100
        if not (0 <= speed <= 255):
            print("Usage: cycle <speed 0-255>  (default 100)")
            return
        await send_write(0xEA, bytes([0x0a, 0x12, 0x00, speed]))

    elif command == "reset":
        await send_write(0xEA, bytes([0x0a, 0x0f, 0x01, 0x00]))

    elif command == "kick":
        await send_write(0xE3, b"\x01")

    elif command == "add-to-group":
        group_name = _group_name_from_argv()
        if not group_name:
            print("Usage: add-to-group <group_name>  [target]")
            return
        try:
            groups = group_registry.load()
            group = group_registry.find_by_name(groups, group_name)
            if not group:
                print(f"Unknown group '{group_name}'. Use 'list-groups' or create it first.")
                return
            addr = group["address"]
            print(f"  Adding lamp(s) to group '{group['name']}' ({addr:#06x})")
            p = bytes([0x01, addr & 0xFF, (addr >> 8) & 0xFF])
            await send_write(0xD7, p)
        except Exception as e:
            print(f"Error: {e}")
            return

    elif command == "remove-from-group":
        group_name = _group_name_from_argv()
        if not group_name:
            print("Usage: remove-from-group <group_name>  [target]")
            return
        try:
            groups = group_registry.load()
            group = group_registry.find_by_name(groups, group_name)
            if not group:
                print(f"Unknown group '{group_name}'. Use 'list-groups'.")
                return
            addr = group["address"]
            print(f"  Removing lamp(s) from group '{group['name']}' ({addr:#06x})")
            p = bytes([0x00, addr & 0xFF, (addr >> 8) & 0xFF])
            await send_write(0xD7, p)
        except Exception as e:
            print(f"Error: {e}")
            return

    elif command == "addgroup":
        if v0 is None:
            print("Usage: addgroup <group_addr>  (e.g. 32768 = 0x8000)")
            return
        p = bytes([0x01, v0 & 0xFF, (v0 >> 8) & 0xFF])
        await send_write(0xD7, p)

    elif command == "delgroup":
        if v0 is None:
            print("Usage: delgroup <group_addr>")
            return
        p = bytes([0x00, v0 & 0xFF, (v0 >> 8) & 0xFF])
        await send_write(0xD7, p)

    elif command == "time":
        now = datetime.datetime.now()
        p = bytes([
            now.year & 0xFF, (now.year >> 8) & 0xFF,
            now.month, now.day,
            now.hour, now.minute, now.second,
            0x00,  # local time
        ])
        print(f"  Syncing time: {now.strftime('%Y-%m-%d %H:%M:%S')}")
        await send_write(0xE4, p)

    elif command == "gettime":
        await cmd_gettime(targets)

    elif command == "fwver":
        await cmd_fwver(targets)

    elif command == "status":
        await cmd_status(targets)

    else:
        print(f"Unknown command: {command}\n")
        print(USAGE)


def parse_args():
    args = sys.argv[1:]
    if not args:
        return None, [], "all", None, None, None

    command = args[0].lower()
    selector = "all"
    mac = None
    addr = None
    dst = None

    remaining = args[1:]

    values = []
    while remaining and not remaining[0].startswith("--"):
        try:
            values.append(int(remaining[0]))
            remaining = remaining[1:]
        except ValueError:
            break

    for i, token in enumerate(remaining):
        if token in ("--single", "--isolated"):
            selector = "isolated"
        elif token in ("--group", "--shared"):
            selector = "shared"
        elif token == "--all":
            selector = "all"
        elif token == "--mac" and i + 1 < len(remaining):
            mac = remaining[i + 1]
        elif token == "--addr" and i + 1 < len(remaining):
            addr = int(remaining[i + 1], 0)
        elif token == "--dst" and i + 1 < len(remaining):
            dst = int(remaining[i + 1], 0)

    return command, values, selector, mac, addr, dst


def _group_name_from_argv() -> str | None:
    """Return group name from argv[2], if present and not a flag."""
    if len(sys.argv) < 3:
        return None
    name = sys.argv[2].strip()
    if not name or name.startswith("--"):
        return None
    return name


if __name__ == "__main__":
    asyncio.run(main())