"""
telink_daemon.py — persistent BLE connection daemon for Telink lamps.

Connects to all lamps on start, keeps connections alive, and serves commands
over a Unix socket so telink_cli.py commands complete in ~50ms instead of 1-2s.

Usage:
    python telink_cli.py daemon start                # recommended entrypoint
    python telink_cli.py daemon stop                 # graceful shutdown
    python telink_daemon.py                          # foreground (Ctrl+C to stop)
    python telink_daemon.py &> /tmp/telink-ble.log &  # background

Protocol (one JSON line per request/response):
    Request:  {"opcode": N, "params": [N, ...], "address": N,
               "selector": "all"|"isolated"|"shared" (legacy: "single"|"group"),
               "mac": "AA:BB:..."}
    Response: {"status": "ok", "count": N}
              {"status": "error", "msg": "..."}
"""

import asyncio
import json
import os
import signal
import sys

import lamp_registry as registry
from telink_ble import TelinkController
from config import CHAR_STATUS_UUID

SOCK_PATH = "/tmp/telink-ble.sock"
PID_PATH = "/tmp/telink-ble.pid"

# Pause flag shared with the web app (also honored by a remote sidecar daemon
# when both mount the same data dir).
PAUSE_FILE = os.path.join(os.environ.get("TELINK_DATA_DIR", "/data"), "daemon_paused")

# Remote-daemon mode (Variant B): when TELINK_DAEMON_HOST is set the daemon
# listens on TCP instead of the local Unix socket. This lets the BLE layer run
# in a privileged sidecar container while the Supervisor add-on only serves the
# web UI and bridges commands.
TCP_HOST = os.environ.get("TELINK_DAEMON_HOST")
TCP_PORT = int(os.environ.get("TELINK_DAEMON_PORT", "8097"))
_KEEPALIVE_INTERVAL = 28.0  # seconds idle before sending keepalive
_STATUS_PARAMS = bytes([0x10] + [0] * 9)
_MAX_START_ATTEMPTS = 3  # serial connect retries per lamp at startup
# Release a lamp's connection after this many seconds without a command. Telink
# mesh lamps stop advertising while connected, so holding sessions forever can
# strand them in a silent state (the bench's brief-connection model never had
# this problem). Sessions reconnect on demand.
IDLE_TIMEOUT = float(os.environ.get("TELINK_IDLE_TIMEOUT", "120"))


class DaemonSession:
    def __init__(self, lamp: dict):
        self.lamp = lamp
        self.ctrl = TelinkController(lamp["mac"], lamp["name"], lamp["password"],
                                     initial_seq=lamp.get("last_seq"))
        self._last_cmd_time: float = 0.0
        self._lock = asyncio.Lock()
        self._keepalive_task: asyncio.Task | None = None

    async def start(self):
        await self.ctrl.connect()
        await self.ctrl.login()
        self._last_cmd_time = asyncio.get_event_loop().time()
        self._keepalive_task = asyncio.get_event_loop().create_task(
            self._keepalive_loop()
        )
        print(f"  [{self.lamp['name']}] connected", flush=True)

    async def stop(self):
        if self._keepalive_task:
            self._keepalive_task.cancel()
            try:
                await self._keepalive_task
            except asyncio.CancelledError:
                pass
        try:
            await self.ctrl.disconnect()
        except Exception:
            pass

    async def send(self, opcode: int, params: bytes, address: int):
        async with self._lock:
            if not self.ctrl.client or not self.ctrl.client.is_connected:
                await self._reconnect()
            try:
                await self.ctrl.send_command(opcode, params, address)
                await asyncio.sleep(0.2)
                await self.ctrl.send_command(opcode, params, address)
            except Exception:
                await self._reconnect()
                await self.ctrl.send_command(opcode, params, address)
                await asyncio.sleep(0.2)
                await self.ctrl.send_command(opcode, params, address)
            self._last_cmd_time = asyncio.get_event_loop().time()
            try:
                registry.update_seq(registry.load(), self.lamp["mac"], self.ctrl.seq_manager.seq)
                self.lamp["last_seq"] = self.ctrl.seq_manager.seq
            except Exception:
                pass

    async def drain(self, duration: float = 0.5):
        """Clear stale notifications from this session's queue without acting on them."""
        try:
            await self.ctrl.drain_notifications(duration=duration)
        except Exception:
            pass

    async def query(self, opcode: int, params: bytes, response_opcode: int,
                    timeout: float = 4.0) -> bytes | None:
        """Send a query command over this session and return the matching decrypted response."""
        async with self._lock:
            if not self.ctrl.client or not self.ctrl.client.is_connected:
                await self._reconnect()
            # Drop queued/stale notifications so we only read fresh responses.
            await self.ctrl.drain_notifications(duration=0.2)
            try:
                await self.ctrl.send_command(opcode, params, 0xFFFF)
                await asyncio.sleep(0.15)
                await self.ctrl.send_command(opcode, params, 0xFFFF)
            except Exception:
                await self._reconnect()
                await self.ctrl.send_command(opcode, params, 0xFFFF)
                await asyncio.sleep(0.15)
                await self.ctrl.send_command(opcode, params, 0xFFFF)
            pkt = await self.ctrl.wait_for_opcode(response_opcode, timeout=timeout)
            self._last_cmd_time = asyncio.get_event_loop().time()
            return pkt

    async def read_status(self) -> bytes | None:
        """Read the lamp's status characteristic (0d1913) over the live session.

        The status char has 'read' + write-WoR properties and no notify, so we
        can read the lamp's current state directly without needing the HCI
        monitor (which is unavailable inside the add-on container).
        """
        async with self._lock:
            if not self.ctrl.client or not self.ctrl.client.is_connected:
                await self._reconnect()
            data = await self.ctrl.client.read_gatt_char(CHAR_STATUS_UUID)
            self._last_cmd_time = asyncio.get_event_loop().time()
            return bytes(data)

    async def _reconnect(self):
        saved_seq = self.ctrl.seq_manager
        try:
            await self.ctrl.disconnect()
        except Exception:
            pass
        self.ctrl = TelinkController(
            self.lamp["mac"], self.lamp["name"], self.lamp["password"],
            initial_seq=self.lamp.get("last_seq")
        )
        # preserve monotonic seq across reconnects
        if saved_seq.seq != self.ctrl.seq_manager.seq:
            self.ctrl.seq_manager = saved_seq
        await self.ctrl.connect()
        await self.ctrl.login()
        self._last_cmd_time = asyncio.get_event_loop().time()
        self._keepalive_task = asyncio.get_event_loop().create_task(
            self._keepalive_loop()
        )
        print(f"  [{self.lamp['name']}] reconnected", flush=True)

    async def _keepalive_loop(self):
        while True:
            await asyncio.sleep(5.0)
            idle = asyncio.get_event_loop().time() - self._last_cmd_time
            if idle >= _KEEPALIVE_INTERVAL:
                try:
                    async with self._lock:
                        await self.ctrl.send_command(0xDA, _STATUS_PARAMS, 0xFFFF)
                        self._last_cmd_time = asyncio.get_event_loop().time()
                except Exception:
                    pass


def _resolve_targets(
    sessions: dict[str, DaemonSession], selector: str, mac: str | None
) -> list[DaemonSession]:
    if mac:
        mac = mac.upper()
        return [sessions[mac]] if mac in sessions else []

    normalized_selector = selector.strip().lower()
    if normalized_selector == "all":
        return list(sessions.values())

    if normalized_selector in ("single", "isolated"):
        target_mesh = "isolated"
    elif normalized_selector in ("group", "shared"):
        target_mesh = "shared"
    else:
        return []

    def session_mesh(s: DaemonSession) -> str:
        raw = s.lamp.get("mesh", s.lamp.get("group"))
        val = str(raw).strip().lower() if raw is not None else ""
        if val in ("single", "isolated"):
            return "isolated"
        if val in ("group", "shared"):
            return "shared"
        # Fallback from password if mesh marker is missing
        return "isolated" if s.lamp.get("password") == "0000" else "shared"

    return [s for s in sessions.values() if session_mesh(s) == target_mesh]


async def _handle_client(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    sessions: dict[str, DaemonSession],
):
    try:
        line = await asyncio.wait_for(reader.readline(), timeout=5.0)
        req = json.loads(line.decode())

        opcode = req.get("opcode")
        params = bytes(req["params"]) if "params" in req else b""
        address = req.get("address", 0xFFFF)
        selector = req.get("selector", "all")
        mac = req.get("mac")
        targets = _resolve_targets(sessions, selector, mac)

        if not targets:
            resp = {"status": "error", "msg": "no matching lamps"}
        elif req.get("kind") == "read":
            # GATT read of the status characteristic over the live session.
            results = []
            errors = []
            for sess in targets:
                try:
                    val = await sess.read_status()
                    if val:
                        results.append({
                            "mac": sess.lamp["mac"],
                            "name": sess.lamp["name"],
                            "payload": list(val),
                        })
                    else:
                        errors.append(f"{sess.lamp['name']}: no status value")
                except Exception as e:
                    errors.append(f"{sess.lamp['name']}: {e}")
            if results:
                resp = {"status": "ok", "results": results,
                        "errors": errors if errors else None}
            else:
                resp = {"status": "error",
                        "msg": "; ".join(errors) if errors else "no responses"}
        elif req.get("kind") == "query":
            # Query commands respond per-lamp; collect each session's answer.
            response_opcode = req.get("response_opcode", 0xDB)
            results = []
            errors = []
            for sess in targets:
                try:
                    pkt = await sess.query(opcode, params, response_opcode)
                    if pkt:
                        results.append({
                            "mac": sess.lamp["mac"],
                            "name": sess.lamp["name"],
                            "payload": list(pkt),
                        })
                    else:
                        errors.append(f"{sess.lamp['name']}: no response")
                except Exception as e:
                    errors.append(f"{sess.lamp['name']}: {e}")
            if results:
                resp = {"status": "ok", "results": results,
                        "errors": errors if errors else None}
            else:
                resp = {"status": "error",
                        "msg": "; ".join(errors) if errors else "no responses"}
        else:
            errors = []
            for sess in targets:
                try:
                    await sess.send(opcode, params, address)
                except Exception as e:
                    errors.append(f"{sess.lamp['name']}: {e}")
            if errors:
                resp = {"status": "error", "msg": "; ".join(errors)}
            else:
                resp = {"status": "ok", "count": len(targets)}

        writer.write((json.dumps(resp) + "\n").encode())
        await writer.drain()
    except Exception as e:
        try:
            writer.write((json.dumps({"status": "error", "msg": str(e)}) + "\n").encode())
            await writer.drain()
        except Exception:
            pass
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass


async def _build_sessions() -> dict[str, DaemonSession]:
    """Connect to every lamp in the registry, returning live sessions."""
    lamps = registry.load()
    sessions: dict[str, DaemonSession] = {
        lamp["mac"].upper(): DaemonSession(lamp) for lamp in lamps
    }
    # Connect serially (not concurrently): BlueZ/dbus errors with
    # "Operation already in progress" when several GATT connects hit the
    # adapter at once. Serial connects + a small settle delay are reliable.
    for mac, sess in sessions.items():
        for attempt in range(_MAX_START_ATTEMPTS):
            if attempt > 0:
                try:
                    await sess.stop()
                except Exception:
                    pass
                await asyncio.sleep(1.0)
            try:
                await sess.start()
                break
            except Exception as e:
                print(f"  [{sess.lamp['name']}] attempt {attempt + 1} failed: {e}", flush=True)
        if sess.ctrl.session_key is None:
            try:
                await sess.stop()
            except Exception:
                pass
            print(f"  [{sess.lamp['name']}] giving up after {_MAX_START_ATTEMPTS} attempts", flush=True)
    return {mac: s for mac, s in sessions.items() if s.ctrl.session_key is not None}


async def _build_sessions_if_any() -> dict[str, DaemonSession]:
    """Like _build_sessions but returns {} instead of letting the daemon exit."""
    try:
        return await _build_sessions()
    except Exception as e:
        print(f"  [daemon] session build error: {e}", flush=True)
        return {}


async def _reload_sessions(sessions: dict[str, DaemonSession]) -> None:
    """Stop all sessions and reconnect from the current registry."""
    for s in list(sessions.values()):
        try:
            await s.stop()
        except Exception:
            pass
    sessions.clear()
    new = await _build_sessions()
    sessions.update(new)
    print(f"Daemon reloaded ({len(sessions)} lamp(s)).", flush=True)


def _lamps_signature():
    """(mac, password, name) tuple set — ignores seq-only writes by the daemon."""
    try:
        lamps = registry.load()
        return sorted(
            (lamp["mac"].upper(), lamp.get("password", ""), lamp.get("name", ""))
            for lamp in lamps
        )
    except Exception:
        return None


def _missing_lamp_entries(sessions: dict[str, DaemonSession]) -> list[dict]:
    """Registry lamps that don't have a live session yet."""
    try:
        lamps = registry.load()
    except Exception:
        return []
    present = {mac.upper() for mac in sessions}
    return [l for l in lamps if l["mac"].upper() not in present]


async def _watch_config(sessions: dict[str, DaemonSession], stop_event: asyncio.Event) -> None:
    """
    Watch the pause flag and the lamp set so the daemon can be paused/resumed
    and pick up discovery results without a restart (works for both the local
    daemon and a remote sidecar sharing the data dir). Reloads only when the
    lamp *set* changes — seq updates written by this daemon are ignored.
    """
    last_sig = _lamps_signature()
    paused = os.path.exists(PAUSE_FILE)
    next_reconnect = 0.0
    while not stop_event.is_set():
        await asyncio.sleep(2.0)
        is_paused = os.path.exists(PAUSE_FILE)
        if is_paused and not paused:
            for s in list(sessions.values()):
                try:
                    await s.stop()
                except Exception:
                    pass
            sessions.clear()
            print("Daemon paused.", flush=True)
            paused = True
            continue
        if not is_paused and paused:
            print("Daemon resuming ...", flush=True)
            paused = False
        if not is_paused:
            sig = _lamps_signature()
            if sig is not None and sig != last_sig:
                print("lamps.json changed; reloading ...", flush=True)
                await _reload_sessions(sessions)
                last_sig = _lamps_signature()
            else:
                # Reconnect registry lamps that don't have a session (they were
                # not advertising when we tried, or a session dropped). Connect
                # by exact MAC so sessions always map to the right lamp.
                now = asyncio.get_event_loop().time()
                if now >= next_reconnect:
                    missing = _missing_lamp_entries(sessions)
                    if missing:
                        next_reconnect = now + 15.0
                        for lamp in missing:
                            mac = lamp["mac"].upper()
                            print(f"Reconnecting missing lamp {mac} ...", flush=True)
                            sess = DaemonSession(lamp)
                            try:
                                await sess.start()
                                sessions[mac] = sess
                                print(f"  [{lamp['name']}] reconnected", flush=True)
                            except Exception as e:
                                try:
                                    await sess.stop()
                                except Exception:
                                    pass
                                print(f"  [{lamp['name']}] reconnect failed: {e}", flush=True)
                        last_sig = _lamps_signature()


async def _idle_sweeper(sessions: dict[str, DaemonSession], stop_event: asyncio.Event) -> None:
    """Release connections that have been idle too long so the lamps go back
    to advertising and stay reachable (mirrors the bench's brief-connection
    model). Sessions are kept in the dict and reconnect on the next command."""
    while not stop_event.is_set():
        await asyncio.sleep(10.0)
        now = asyncio.get_event_loop().time()
        for mac, sess in list(sessions.items()):
            connected = sess.ctrl.client and sess.ctrl.client.is_connected
            if connected and (now - sess._last_cmd_time) >= IDLE_TIMEOUT:
                print(f"  [{sess.lamp['name']}] idle {IDLE_TIMEOUT:.0f}s - releasing connection", flush=True)
                await sess.stop()


async def start_daemon():
    if os.path.exists(PAUSE_FILE):
        print("Daemon paused (flag present) — holding.", flush=True)

    with open(PID_PATH, "w") as f:
        f.write(str(os.getpid()))

    if os.path.exists(SOCK_PATH):
        os.unlink(SOCK_PATH)

    if os.path.exists(PAUSE_FILE):
        # Stay up but idle (no sessions) so pause/resume still works over the socket.
        sessions: dict[str, DaemonSession] = {}
    else:
        lamps = registry.load()
        if not lamps:
            print("No lamps saved. Run 'discover' first.", flush=True)
            # Keep the server up (idle) so the web app can still reach us; the
            # config watcher will load sessions once lamps are discovered.
            sessions = await _build_sessions_if_any()
        else:
            print(f"Connecting to {len(lamps)} lamp(s) ...", flush=True)
            sessions = await _build_sessions()
        # Never exit on a transient "all lamps offline": stay up and let the
        # watcher re-connect. A crash-loop here restarts the container forever.
        print(f"Daemon ready ({len(sessions)} lamp(s)). {('TCP: ' + TCP_HOST + ':' + str(TCP_PORT)) if TCP_HOST else ('Socket: ' + SOCK_PATH)}", flush=True)

    async def _client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        await _handle_client(reader, writer, sessions)

    if TCP_HOST:
        server = await asyncio.start_server(_client, TCP_HOST, TCP_PORT)
        print(f"Daemon ready ({len(sessions)} lamp(s)). TCP: {TCP_HOST}:{TCP_PORT}", flush=True)
    else:
        server = await asyncio.start_unix_server(_client, path=SOCK_PATH)
        os.chmod(SOCK_PATH, 0o600)
        print(f"Daemon ready ({len(sessions)} lamp(s)). Socket: {SOCK_PATH}", flush=True)

    loop = asyncio.get_event_loop()
    stop_event = asyncio.Event()

    def _on_signal():
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _on_signal)

    watcher = asyncio.get_event_loop().create_task(
        _watch_config(sessions, stop_event)
    )
    idle_sweeper = asyncio.get_event_loop().create_task(
        _idle_sweeper(sessions, stop_event)
    )

    async with server:
        await stop_event.wait()
        server.close()

    watcher.cancel()
    idle_sweeper.cancel()
    for task in (watcher, idle_sweeper):
        try:
            await task
        except asyncio.CancelledError:
            pass

    print("Shutting down ...", flush=True)
    await asyncio.gather(*[s.stop() for s in sessions.values()], return_exceptions=True)
    for path in (SOCK_PATH, PID_PATH):
        if os.path.exists(path):
            os.unlink(path)
    print("Done.", flush=True)


if __name__ == "__main__":
    asyncio.run(start_daemon())
