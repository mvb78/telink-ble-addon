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
import contextlib
import json
import os
import resource
import signal
import sys
import time

import lamp_registry as registry
import group_registry
from telink_ble import TelinkController, SequenceManager, get_scanner
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
# Lamps with known-bad outputs (actuate nothing, never push): commands fail
# fast instead of burning global-lock rebuilds that wedge the healthy lamps.
_QUARANTINE_MACS = {m.strip().upper() for m in
                    os.environ.get("TELINK_QUARANTINE_MACS", "").split(",")
                    if m.strip()}
# Bound for the is_connected ground-truth probe (GATT read). Exceeding it
# means the link is really dead -> rebuild as before.
_STATUS_PARAMS = bytes([0x10] + [0] * 9)
_MAX_START_ATTEMPTS = 1  # startup tries each lamp once; the maintainer owns retries
# Release a lamp's connection after this many seconds without a command. Telink
# mesh lamps stop advertising while connected, so holding sessions forever can
# strand them in a silent state. TELINK_IDLE_TIMEOUT=0 (or negative) keeps the
# connection permanently — the daemon's health-check keepalive then handles
# dead links proactively instead (the old CLI's fast permanent-connection
# model). Sessions reconnect on demand.
IDLE_TIMEOUT = float(os.environ.get("TELINK_IDLE_TIMEOUT", "120"))

# Verify every send with a GATT read of the status characteristic: command
# writes use write-without-response (response=False), which never raises even
# when the lamp dropped the link — that caused silent morning failures (packets
# into the void, no error surfaced to callers). The read proves the ATT bearer
# is alive; on failure we reconnect and re-send within the same command call.
_LINK_VERIFY_TIMEOUT = float(os.environ.get("TELINK_LINK_VERIFY_TIMEOUT", "8"))
_SEND_ATTEMPTS = 2  # reconnect cycles allowed within one send()
# Fail-fast bounds for lock acquisition. A hung BLE op must never freeze
# the daemon's command path: waiters fail fast with an error instead of
# queueing forever behind a wedged holder (observed: one hung send froze
# every query/send, zero log output, healthy epoll — a living dead).
# Order is always session-then-adapter; bounds only break the wait, and a
# timeout here never corrupts lock state (cancelled acquirers are dropped).
_SESSION_LOCK_TIMEOUT = float(os.environ.get("TELINK_SESSION_LOCK_TIMEOUT", "60"))
_ADAPTER_LOCK_TIMEOUT = float(os.environ.get("TELINK_ADAPTER_LOCK_TIMEOUT", "120"))


@contextlib.asynccontextmanager
async def _locked(session_lock: asyncio.Lock, what: str):
    """Acquire session-then-adapter locks with fail-fast bounds."""
    try:
        await asyncio.wait_for(session_lock.acquire(), timeout=_SESSION_LOCK_TIMEOUT)
    except asyncio.TimeoutError:
        print(f"  [{what}] session lock busy >{_SESSION_LOCK_TIMEOUT:.0f}s, failing fast",
              flush=True)
        raise TimeoutError(f"{what}: session busy")
    try:
        try:
            await asyncio.wait_for(_ADAPTER_LOCK.acquire(), timeout=_ADAPTER_LOCK_TIMEOUT)
        except asyncio.TimeoutError:
            print(f"  [{what}] adapter lock busy >{_ADAPTER_LOCK_TIMEOUT:.0f}s, failing fast",
                  flush=True)
            raise TimeoutError(f"{what}: adapter busy")
        try:
            yield
        finally:
            _ADAPTER_LOCK.release()
    finally:
        session_lock.release()
# Actuation confirmation: after a verified unicast send to the session's own
# lamp, wait this long for a state push reflecting the command. No push =
# the packet was dropped (stale seq vs the lamp's dedupe window) or the
# lamp-side session is wedged — both fixed by reconnect + retry.
_PUSH_WAIT_S = float(os.environ.get("TELINK_PUSH_WAIT_S", "6.0"))
# Query-verification rounds after the push wait: the flood mesh can deliver
# seconds late (relays), so a single 4 s query proves nothing. Each round
# sends the 0xDA status query twice and compares the answer.
_PUSH_VERIFY_ROUNDS = int(os.environ.get("TELINK_PUSH_VERIFY_ROUNDS", "3"))
# If the last decoded push is older than this, the lamp may have advanced
# its seq counter without us seeing it (no pushes observed to learn from).
# Bump our counter forward pre-emptively so the send isn't dropped as a
# duplicate. Forward jumps are always accepted; only rewinds are rejected.
_PUSH_STALE_S = float(os.environ.get("TELINK_PUSH_STALE_S", "300"))
_SEQ_BUMP = int(os.environ.get("TELINK_SEQ_BUMP", "0x1000"), 0)

# Global adapter lock: every BLE operation (scan, connect, write, GATT read)
# across ALL sessions serializes here. The sidecar drives one radio (hci1);
# parallel BleakScanner/connect traffic from concurrent sessions disrupts
# live neighbor links, causing the reconnect-scan cascades that wedged the
# mesh for minutes (each scan steals adapter airtime from the other five
# sessions). Always acquired as a LEAF (after any session lock, never before)
# so lock ordering is deadlock-free.
_ADAPTER_LOCK = asyncio.Lock()

# Reconnect backoff for missing lamps: base seconds, doubled per consecutive
# failure, capped. A lamp that is genuinely powered off must not trigger a
# full multi-second adapter scan every cycle.
_RECONNECT_BASE_S = 15.0
_RECONNECT_MAX_S = 300.0
_reconnect_fails: dict[str, int] = {}
_reconnect_backoff: dict[str, float] = {}


def _decode_status_push(plain: bytes) -> tuple[dict | None, None]:
    """Decode a vendor 0xDB status frame into a state entry (no ts).

    Shared by note_plain() and the send() confirmation-query path so both
    interpret lamp state identically. Semantics per the 1.3.0 correct-off
    rule: a lamp always reports state ON; real off = brightness 0.
    Returns (entry, None) — the second slot keeps symmetry with the old
    inline code that also extracted the mesh sno from the raw frame.
    """
    if len(plain) < 20 or plain[7] != 0xDB:
        return None, None
    p = plain[10:]
    if len(p) < 7:
        return None, None
    bri = p[6]
    rgb = [p[7], p[8], p[9]] if len(p) >= 10 else None
    return ({
        "on": bool(p[5]) and bri > 0,
        "brightness": bri,
        "colortemp": max(0, min(100, 100 - p[3])),  # warm%
        "rgb": rgb,
    }, None)


def _mesh_src_to_mac(src_addr: int) -> str | None:
    """Map a mesh-layer source address to a registry MAC.

    Mesh STATUS frames carry their origin lamp's unicast address in the
    (unencrypted) frame header; the registry holds each lamp's provisioned
    mesh_address, giving an exact, session-independent attribution — no
    guessing from signal or timing.
    """
    try:
        lamps = registry.load()
    except Exception:
        return None
    for lamp in lamps:
        try:
            if int(lamp.get("mesh_address")) == int(src_addr):
                return str(lamp.get("mac", "")).upper() or None
        except (TypeError, ValueError):
            continue
    return None


def _decode_mesh_status(plain: bytes) -> dict | None:
    """Decode a mesh-layer 0x1B STATUS frame into a state entry (no ts).

    Layout mirrors the validated `parse_mesh_status` (web_app
    mesh-get-status): [op|0xC0, cw LE 2B, ww LE 2B, bri LE 1-2B].
    Brightness scale: vendor 0-100 used as-is, larger values scaled from
    the 0-32767 PWM domain. Warm% from the cw/ww ratio; None when unknown.
    """
    if len(plain) < 6 or (plain[0] & 0xC0) != 0xC0 or (plain[0] & 0x3F) != 0x1B:
        return None
    cw = plain[1] | (plain[2] << 8)
    ww = plain[3] | (plain[4] << 8)
    br = (plain[5] | (plain[6] << 8)) if len(plain) > 6 else plain[5]
    bri = br if br <= 100 else round(max(0, min(32767, br)) * 100 / 32767)
    warm = round(100 * ww / (cw + ww)) if (cw + ww) > 0 else None
    return {
        "on": br > 0,
        "brightness": max(0, min(100, bri)),
        "colortemp": warm,
        "rgb": None,
    }


def _push_matches_command(entry: dict, opcode: int, params: bytes) -> bool:
    """True when a decoded status entry reflects the just-sent command."""
    try:
        if opcode == 0xD0:  # on/off
            return bool(entry.get("on")) == bool(params[0])
        if opcode == 0xD2 and params:  # brightness 0-100
            return abs(int(entry.get("brightness") or 0) - int(params[0])) <= 5
        if opcode == 0xE2 and len(params) >= 2 and params[0] == 0x05:
            # colortemp warm%: 100 - cool%
            return abs(int(entry.get("colortemp") or 0) - (100 - int(params[1]))) <= 8
    except (TypeError, ValueError, IndexError):
        return False
    return True


# Process-wide mesh sno source. Mesh packets carry no source address, so
# lamps dedupe on the bare sno: per-session counters interleave, and any
# lagging session's command packets are dropped as replays forever (reads
# are not deduped, which is why queries kept working while commands died).
# A single monotone counter keeps every packet above every lamp's window.
_SHARED_SEQ: SequenceManager | None = None


def _shared_seq() -> SequenceManager:
    assert _SHARED_SEQ is not None, "shared sno source not initialised"
    return _SHARED_SEQ


def _init_shared_seq(lamps: list[dict]) -> SequenceManager:
    """Start the shared counter above every persisted lamp window.

    Forward jumps are accepted by lamps (only rewinds are rejected), so
    starting at max(last_seq) + margin re-anchors all sessions in one
    move, including sessions whose counters lagged behind."""
    global _SHARED_SEQ
    if _SHARED_SEQ is not None:
        return _SHARED_SEQ
    top = 0
    for lamp in lamps or []:
        try:
            top = max(top, int(lamp.get("last_seq") or 0))
        except (TypeError, ValueError):
            pass
    _SHARED_SEQ = SequenceManager()
    if top:
        # Never wrap voluntarily at init: cap the margin at the ceiling so
        # a near-ceiling registry (normal after long uptime) restarts at
        # 0xFFFFFF and rolls over naturally (+1 modular step) instead of
        # jumping to a low value the lamps reject as replay.
        _SHARED_SEQ.advance_to(min(top + _SEQ_BUMP, 0xFFFFFF) - 1)
    # Absolute operator override for replay-desync recovery: the registry
    # file can be poisoned DOWNWARD by wrapped pre-bump persists while lamp
    # windows sit near the ceiling; start exactly here instead.
    try:
        forced = os.environ.get("TELINK_SEQ_START", "").strip()
        if forced:
            _SHARED_SEQ.seq = int(forced, 0) & 0xFFFFFF
            if _SHARED_SEQ.seq == 0:
                _SHARED_SEQ.seq = 1
    except (TypeError, ValueError):
        pass
    print(f"shared sno starts at {_SHARED_SEQ.seq} (persisted max {top})",
          flush=True)
    return _SHARED_SEQ


def _persist_shared_seq() -> None:
    """Max-merge the shared sno into every registry entry so a restart
    resumes above all lamp windows."""
    if _SHARED_SEQ is None:
        return
    try:
        lamps = registry.load()
        changed = False
        for lamp in lamps:
            try:
                cur = int(lamp.get("last_seq") or 0)
            except (TypeError, ValueError):
                cur = 0
            if _SHARED_SEQ.seq > cur:
                lamp["last_seq"] = _SHARED_SEQ.seq
                changed = True
        if changed:
            registry.save(lamps)
    except Exception:
        pass


class DaemonSession:
    # All live session objects by MAC — lets mesh-relayed STATUS frames
    # (captured on ANY session) update the ORIGIN lamp's cache, not the
    # capturing session's. Replacements overwrite by MAC key; liveness is
    # always re-checked via is_connected before use.
    _sessions: dict[str, "DaemonSession"] = {}

    def __init__(self, lamp: dict):
        self.lamp = lamp
        DaemonSession._sessions[str(lamp.get("mac", "")).upper()] = self
        self.ctrl = TelinkController(lamp["mac"], lamp["name"], lamp["password"],
                                     initial_seq=lamp.get("last_seq"),
                                     on_plain=self.note_plain,
                                     seq_manager=_init_shared_seq([]))
        self._last_cmd_time: float = 0.0
        # Last known lamp state from the lamp's own 0xDB status pushes (the
        # lamp notifies after every mesh write, incl. group broadcasts fed in
        # via other proxies). Semantics per lamp_registryś 1.3.0 fix: a lamp
        # always reports state ON; real off = brightness 0.
        self.state_cache: dict | None = None
        # Monotonic push counter (bumped in note_plain on every decoded
        # status push). Lets send() detect "verified link, but the lamp
        # never actuated" by watching for a fresh push after the write.
        self._push_count: int = 0
        self._lock = asyncio.Lock()
        self._keepalive_task: asyncio.Task | None = None

    def note_plain(self, plain: bytes, raw: bytes | None = None):
        """Decode status pushes into state caches + seq-sync.

        Two frame formats arrive here (vendor checked first, same
        precedence as wait_for_opcode):
          * vendor 0x0211 (exactly 20 B, op at [7] = 0xDB) — the lamp's own
            direct GATT push on this session; its leading 3 bytes are the
            mesh sequence number the lamp just used/heeded (drives seq
            jumps),
          * mesh-layer STATUS (decrypted starts with 0xC0|0x1B, len != 20)
            — relayed by the mesh, possibly from ANY lamp: attribute via the
            unencrypted src address in the raw frame header (raw[3:5]) mapped
            through the registry's mesh_address, and update THAT lamp's
            cache. This is how keepalive replies and relayed pushes — the
            bulk of mesh traffic — keep every lamp's state fresh.
        """
        if len(plain) == 20 and len(plain) > 7 and plain[7] == 0xDB:
            pass  # vendor path below (own lamp)
        elif (len(plain) != 20 and raw is not None and len(raw) >= 5):
            src = int.from_bytes(raw[3:5], "little")
            entry = _decode_mesh_status(plain)
            if entry is not None:
                mac = _mesh_src_to_mac(src)
                target = DaemonSession._sessions.get(mac) if mac else None
                if (target is not None and target.ctrl.client is not None
                        and target.ctrl.client.is_connected):
                    entry["ts"] = round(time.time(), 3)
                    target.state_cache = entry
                    target._push_count += 1
                    if os.environ.get("TELINK_DEBUG_STATE"):
                        print(f"  [{target.lamp.get('name', mac)}] mesh-status "
                              f"src=0x{src:04x} -> {'on' if entry['on'] else 'off'} "
                              f"bri={entry['brightness']}", flush=True)
            return
        entry, _ = _decode_status_push(plain)
        if entry is None:
            return

        if raw is not None and len(raw) >= 3:
            seen_seq = int.from_bytes(raw[0:3], "little")
            ours = _shared_seq().seq
            if seen_seq > ours:
                # Lamps can advance their seq independently (phone app in the
                # mesh, mesh re-key after app control). Our next sends would
                # be silently dropped as "already seen" (dedup window ±0x3F)
                # — jump the SHARED counter ahead of the lamp's counter at
                # once so no session lags behind.
                _shared_seq().advance_to(seen_seq)
                _persist_shared_seq()
                if os.environ.get("TELINK_DEBUG_STATE"):
                    print(f"  [{self.lamp.get('name', self.lamp['mac'])}] seq "
                          f"{ours} -> {_shared_seq().seq} (lamp ahead)", flush=True)

        entry["ts"] = round(time.time(), 3)
        old = self.state_cache
        # Always refresh: ts means last-SEEN push (liveness), not last change.
        # A steady lamp pushes identical state on every keepalive; without a
        # ts refresh the HA-side staleness watchdog would false-positive on
        # healthy-but-quiet lamps.
        changed = (not old
                   or {k: v for k, v in old.items() if k != "ts"} != entry)
        self.state_cache = entry
        self._push_count += 1
        if not changed:
            return
        if os.environ.get("TELINK_DEBUG_STATE"):
            print(f"  [{self.lamp.get('name', self.lamp['mac'])}] state -> "
                  f"{'on' if entry['on'] else 'off'} bri={entry['brightness']}", flush=True)

    async def start(self):
        # Hard-bounded: BlueZ GATT ops inside login() carry no timeout of
        # their own; a wedged adapter/lamp can hang them forever, which used
        # to stall the whole initial connect behind a single lamp.
        async with _ADAPTER_LOCK:
            try:
                await asyncio.wait_for(self._start_inner(), timeout=60.0)
            except asyncio.TimeoutError:
                try:
                    await self.ctrl.disconnect()
                except Exception:
                    pass
                raise Exception(f"{self.lamp['mac']} start timed out (BLE stack hung)")

    async def _start_inner(self):
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
        # Bounded: an unclean restart orphans the lamp-side connection and
        # the lamp then stays silent (no advertising) until its supervision
        # timeout fires. Never hold SIGTERM past the container stop budget.
        try:
            await asyncio.wait_for(self.ctrl.disconnect(), timeout=10.0)
        except Exception:
            pass

    async def _read_status_char(self):
        """Live-link proof: plain GATT read of the status characteristic.

        Raises on any transport failure (dead/gone lamp, stale connection).
        Must be called while holding the session lock to avoid overlapping
        with command/query traffic.
        """
        return await asyncio.wait_for(
            self.ctrl.client.read_gatt_char(CHAR_STATUS_UUID),
            timeout=_LINK_VERIFY_TIMEOUT,
        )

    @property
    def _quarantined(self) -> bool:
        return str(self.lamp.get("mac", "")).upper() in _QUARANTINE_MACS

    async def _probe_alive(self) -> bool:
        """Ground truth for a suspect link: bleak's is_connected can report
        False on a healthy link (stale D-Bus state after adapter hiccups),
        and rebuilding such a link burns a global-lock scan that kills
        neighbor links. A cheap GATT read proves liveness; callers must
        hold the session lock. Never raises."""
        try:
            await self._read_status_char()
            return True
        except Exception:
            return False

    async def _safe_disconnect(self) -> None:
        """Disconnect with a hard timeout.

        A BlueZ-side hang inside disconnect() must never wedge the caller:
        send()/reconnect paths hold the global adapter lock while calling
        this, so an unbounded disconnect would freeze the whole daemon's
        command path (state reads keep working — exactly the observed
        "TCP alive, sends time out" signature).
        """
        try:
            await asyncio.wait_for(self.ctrl.disconnect(), timeout=10.0)
        except Exception:
            pass

    async def send(self, opcode: int, params: bytes, address: int):
        """Send a mesh command with link verification and bounded reconnect.

        Writes alone can't prove delivery (write-without-response swallows
        dead links), so after sending we do a status-char read. On failure
        (send or read) we reconnect and retry, up to _SEND_ATTEMPTS cycles.
        Raises on final failure so callers see ok=false instead of silence.

        Colortemp (0xE2) is force-routed to broadcast: bench-proven
        2026-09-22 that these lamps ignore addressed E2 (unicast AND group)
        but honor broadcast E2. Per-group CT is firmware-impossible, so all
        lamps share one CT (automations use a single value).

        The whole attempt sequence holds the global adapter lock: this
        session's scan/connect/write/read traffic must never overlap another
        session's, or the adapter scan steals airtime and kills neighbor
        links (reconnect-scan cascade).
        """
        if opcode == 0xE2 and int(address) != 0xFFFF:
            print(f"  [{self.lamp['name']}] E2 -> broadcast reroute "
                  f"(was {int(address):#06x})", flush=True)
            address = 0xFFFF
        async with _locked(self._lock, self.lamp.get('name', '?')):
            last_error: Exception | None = None
            for attempt in range(1, _SEND_ATTEMPTS + 1):
                try:
                    if self.ctrl.client is None:
                        await self._reconnect(spawn_keepalive=False)
                    elif not self.ctrl.client.is_connected:
                        # Suspect link: verify before rebuilding. A GATT read
                        # is ground truth; the attempt below (write+read+
                        # confirm) is the final arbiter, and the retry path
                        # rebuilds on genuine failure.
                        if await self._probe_alive():
                            print(f"  [{self.lamp['name']}] send-path pre-check "
                                  f"false alarm, link alive", flush=True)
                        else:
                            print(f"  [{self.lamp['name']}] send-path pre-check: "
                                  f"is_connected=False -> rebuilding", flush=True)
                            await self._reconnect(spawn_keepalive=False)
                    if self._keepalive_task is None or self._keepalive_task.done():
                        self._keepalive_task = asyncio.get_event_loop().create_task(
                            self._keepalive_loop()
                        )
                    self._maybe_bump_seq()
                    # Start from a clean queue: delayed relay duplicates of
                    # older pushes must not count as confirmation of THIS
                    # command (phantom ok when stale values match).
                    try:
                        await self.ctrl.drain_notifications(duration=0.3)
                    except Exception:
                        pass
                    push_before = self._push_count
                    await self.ctrl.send_command(opcode, params, address)
                    await asyncio.sleep(0.2)
                    await self.ctrl.send_command(opcode, params, address)
                    await self._read_status_char()  # liveness proof
                    await self._confirm_push(push_before, opcode, params, address)
                    if opcode == 0xE2:
                        # Broadcast sends skip push confirmation by design;
                        # verify against our own lamp's queried state so a
                        # lost E2 still raises (and retries) instead of
                        # reporting phantom ok.
                        verified = False
                        for _ in range(max(1, _PUSH_VERIFY_ROUNDS)):
                            try:
                                pkt = await self._query_locked(
                                    0xDA, _STATUS_PARAMS, 0xDB, timeout=4.0)
                            except Exception:
                                pkt = None
                            if pkt:
                                entry, _seq = _decode_status_push(pkt)
                                if entry is not None and _push_matches_command(
                                        entry, opcode, params):
                                    verified = True
                                    break
                            await asyncio.sleep(1.0)
                        if not verified:
                            raise TimeoutError(
                                f"E2 broadcast not reflected by "
                                f"{self.lamp['mac']}")
                    self._last_cmd_time = asyncio.get_event_loop().time()
                    _persist_shared_seq()
                    return
                except Exception as err:
                    last_error = err
                    if self._quarantined:
                        # Known-bad output stage: never actuates, never
                        # pushes. Fail fast instead of burning global-lock
                        # rebuilds that wedge the healthy lamps.
                        print(f"  [{self.lamp['name']}] quarantined, failing "
                              f"fast ({type(err).__name__})", flush=True)
                        raise last_error
                    if attempt < _SEND_ATTEMPTS:
                        print(f"  [{self.lamp['name']}] send attempt {attempt} "
                              f"failed ({type(err).__name__}: {err}) -> rebuilding",
                              flush=True)
                        try:
                            # drop the dead link first so _reconnect rebuilds
                            await self._safe_disconnect()
                            await self._reconnect(spawn_keepalive=False)
                        except Exception as reconnect_error:
                            last_error = reconnect_error
            assert last_error is not None
            # Persist even on failure: the snos were consumed from our
            # monotone counter and the fire-and-forget writes may have
            # advanced lamp windows anyway. Never let the file fall behind
            # the counter, or the next restart resumes inside old windows.
            _persist_shared_seq()
            raise last_error

    def _maybe_bump_seq(self) -> None:
        """Pre-emptive seq jump when our counter may lag the lamp's.

        If no status push was decoded for a long time we have no recent
        knowledge of the lamp's counter — and the lamp may have advanced it
        via other traffic. Our next write would then be dropped as a
        duplicate inside its ±0x3F dedupe window. A forward jump is always
        accepted (only rewinds are rejected), so bump ahead proactively.
        """
        st = self.state_cache
        if not st or not st.get("ts"):
            return
        if time.time() - float(st["ts"]) < _PUSH_STALE_S:
            return
        ours = _shared_seq().seq
        _shared_seq().advance_to((ours + _SEQ_BUMP) & 0xFFFFFF)
        _persist_shared_seq()
        print(f"  [{self.lamp.get('name', self.lamp['mac'])}] seq pre-bump "
              f"{ours} -> {_shared_seq().seq} (push stale)", flush=True)

    async def _confirm_push(self, push_before: int, opcode: int, params: bytes,
                            address: int) -> None:
        """Confirm actuation after a verified unicast send to the own lamp.

        Only meaningful for unicast writes to this session's own lamp
        (address == its mesh address): a processed write is always followed
        by a 0xDB push. Path:
          1. wait up to _PUSH_WAIT_S for any fresh push → success;
          2. else query 0xDA and compare the answer against the commanded
             state — if it matches, the lamp actuated but its push was lost
             in radio noise → success (no pointless reconnect);
          3. else raise so send() reconnects and retries instead of
             reporting phantom success.
        """
        own_addr = self.lamp.get("mesh_address")
        try:
            own_addr = int(own_addr) if own_addr is not None else None
        except (TypeError, ValueError):
            own_addr = None
        if own_addr is None or int(address) != own_addr:
            return
        deadline = asyncio.get_event_loop().time() + _PUSH_WAIT_S
        while asyncio.get_event_loop().time() < deadline:
            if self._push_count != push_before:
                # A push arrived — but only a push REFLECTING the command
                # proves actuation (stale relays/keepalive echoes carry old
                # values and must not count as success).
                st = self.state_cache
                if st is not None and _push_matches_command(st, opcode, params):
                    return
                push_before = self._push_count
            await asyncio.sleep(0.2)
        # No matching push seen — but the push itself may have been lost in
        # noise while the lamp did actuate, or the flood mesh may deliver
        # seconds late via relays. Ask directly (repeatedly) before
        # declaring failure: a query-verified state is honest success, and
        # avoids a pointless global-lock rebuild storm over late delivery.
        for rnd in range(max(1, _PUSH_VERIFY_ROUNDS)):
            try:
                pkt = await self._query_locked(0xDA, _STATUS_PARAMS, 0xDB,
                                               timeout=4.0)
            except Exception:
                pkt = None
            if pkt:
                entry, _seq = _decode_status_push(pkt)
                if entry is not None:
                    entry["ts"] = round(time.time(), 3)
                    self.state_cache = entry
                    self._push_count += 1
                    if _push_matches_command(entry, opcode, params):
                        return
            if rnd < _PUSH_VERIFY_ROUNDS - 1:
                await asyncio.sleep(1.0)
        raise TimeoutError(
            f"no actuation push from {self.lamp['mac']} within {_PUSH_WAIT_S}s "
            f"(op={opcode:#04x}); packet likely dropped")

    async def _query_locked(self, opcode: int, params: bytes,
                            response_opcode: int, timeout: float = 4.0) -> bytes | None:
        """query() body without lock handling — callers must hold both locks."""
        if not self.ctrl.client or not self.ctrl.client.is_connected:
            await self._reconnect(spawn_keepalive=False)
        await self.ctrl.drain_notifications(duration=0.2)
        try:
            await self.ctrl.send_command(opcode, params, 0xFFFF)
            await asyncio.sleep(0.15)
            await self.ctrl.send_command(opcode, params, 0xFFFF)
        except Exception:
            await self._reconnect(spawn_keepalive=False)
            await self.ctrl.send_command(opcode, params, 0xFFFF)
            await asyncio.sleep(0.15)
            await self.ctrl.send_command(opcode, params, 0xFFFF)
        pkt = await self.ctrl.wait_for_opcode(response_opcode, timeout=timeout)
        self._last_cmd_time = asyncio.get_event_loop().time()
        return pkt

    async def drain(self, duration: float = 0.5):
        """Clear stale notifications from this session's queue without acting on them."""
        try:
            await self.ctrl.drain_notifications(duration=duration)
        except Exception:
            pass

    async def query(self, opcode: int, params: bytes, response_opcode: int,
                    timeout: float = 4.0) -> bytes | None:
        """Send a query command over this session and return the matching decrypted response."""
        async with _locked(self._lock, self.lamp.get('name', '?')):
            try:
                return await self._query_locked(opcode, params, response_opcode, timeout)
            finally:
                _persist_shared_seq()

    async def read_status(self) -> bytes | None:
        """Read the lamp's status characteristic (0d1913) over the live session.

        The status char has 'read' + write-WoR properties and no notify, so we
        can read the lamp's current state directly without needing the HCI
        monitor (which is unavailable inside the add-on container).
        """
        async with _locked(self._lock, self.lamp.get('name', '?')):
            if not self.ctrl.client or not self.ctrl.client.is_connected:
                await self._reconnect(spawn_keepalive=False)
            data = await self.ctrl.client.read_gatt_char(CHAR_STATUS_UUID)
            self._last_cmd_time = asyncio.get_event_loop().time()
            return bytes(data)

    async def _connect_and_login(self) -> None:
        """Single connect+login sequence, bounded by the caller."""
        await self.ctrl.connect()
        await self.ctrl.login()

    async def _reconnect(self, spawn_keepalive: bool = True):
        # NOTE: the caller must already hold the global adapter lock OR the
        # session lock (all current callers do); _ADAPTER_LOCK is always a
        # leaf here, so lock ordering stays deadlock-free.
        # The shared sno source survives reconnects by design (it is not
        # per-controller), so no seq preservation dance is needed.
        await self._safe_disconnect()
        self.ctrl = TelinkController(
            self.lamp["mac"], self.lamp["name"], self.lamp["password"],
            initial_seq=self.lamp.get("last_seq"),
            on_plain=self.note_plain,
            seq_manager=_init_shared_seq([]),
        )
        # Hard-bounded: login()'s GATT ops carry no timeout of their own and
        # BlueZ can hang inside them forever. An unbounded hang here (while
        # holding the global adapter lock) froze the entire command path and
        # piled up one leaked CLOSE_WAIT socket per queued poll (~5/min).
        try:
            await asyncio.wait_for(self._connect_and_login(), timeout=40.0)
        except asyncio.TimeoutError:
            try:
                await self.ctrl.disconnect()
            except Exception:
                pass
            raise Exception(f"{self.lamp['mac']} reconnect timed out (BLE stack hung)")
        self._last_cmd_time = asyncio.get_event_loop().time()
        if spawn_keepalive and (self._keepalive_task is None
                                or self._keepalive_task.done()):
            # never stack keepalive loops — a dead-still-running loop from a
            # previous connection would duplicate health-check traffic and
            # fights the current one (leaked fds, wedged transmits).
            self._keepalive_task = asyncio.get_event_loop().create_task(
                self._keepalive_loop()
            )
        print(f"  [{self.lamp['name']}] reconnected", flush=True)

    async def _keepalive_loop(self):
        failed = 0
        loop = asyncio.get_event_loop()
        while True:
            await asyncio.sleep(5.0)
            idle = loop.time() - self._last_cmd_time
            if idle < _KEEPALIVE_INTERVAL:
                continue
            try:
                async with _locked(self._lock, self.lamp.get('name', '?')):
                    if self.ctrl.client is None:
                        await self._reconnect(spawn_keepalive=False)
                        continue
                    if not self.ctrl.client.is_connected:
                        if await self._probe_alive():
                            print(f"  [{self.lamp['name']}] keepalive pre-check "
                                  f"false alarm, link alive", flush=True)
                        else:
                            print(f"  [{self.lamp['name']}] keepalive pre-check: "
                                  f"is_connected=False -> rebuilding", flush=True)
                            await self._reconnect(spawn_keepalive=False)
                            continue
                    await self.ctrl.send_command(0xDA, _STATUS_PARAMS, 0xFFFF)
                    # Health check: the lamp pushes its own 0xDB on the keepalive.
                    # No reply two keepalives in a row -> drop the link so the
                    # next command reconnects fast instead of hanging on a
                    # half-dead session.
                    got = await self.ctrl.wait_for_opcode(0xDB, timeout=1.0)
                    if got is None:
                        failed += 1
                        if failed >= 2:
                            print(f"  [{self.lamp['name']}] keepalive unresponsive - dropping link", flush=True)
                            await self._safe_disconnect()
                            failed = 0
                            continue
                    else:
                        failed = 0
                    self._last_cmd_time = loop.time()
            except Exception:
                # Link error: rebuild only a link that is really down. An
                # earlier unconditional teardown here killed healthy links on
                # transient send errors, feeding the reconnect churn.
                try:
                    if self.ctrl.client is None or not self.ctrl.client.is_connected:
                        await self._reconnect(spawn_keepalive=False)
                    else:
                        print(f"  [{self.lamp['name']}] keepalive transient "
                              f"error, link alive", flush=True)
                except Exception:
                    try:
                        await self.ctrl.disconnect()
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


# Request handling self-defense: at most this many concurrent API requests,
# each living at most this long. A wedged BLE operation must never be able
# to pile up one leaked CLOSE_WAIT socket per queued poll (~5/min observed
# live: 963 stuck handlers with zero log output). Over-limit requests fail
# fast with an error instead of queueing behind a stuck lock forever —
# every caller already retries.
_MAX_CONCURRENT_REQUESTS = 8
_REQUEST_TIMEOUT_S = 30.0
_request_semaphore: asyncio.Semaphore | None = None


def _request_sem() -> asyncio.Semaphore:
    global _request_semaphore
    if _request_semaphore is None:
        _request_semaphore = asyncio.Semaphore(_MAX_CONCURRENT_REQUESTS)
    return _request_semaphore


async def _handle_client(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    sessions: dict[str, DaemonSession],
):
    try:
        try:
            await asyncio.wait_for(_request_sem().acquire(), timeout=10.0)
        except asyncio.TimeoutError:
            try:
                writer.write((json.dumps(
                    {"status": "error", "msg": "server busy, retry"}) + "\n").encode())
                await writer.drain()
            except Exception:
                pass
            return
        try:
            await asyncio.wait_for(
                _handle_client_inner(reader, writer, sessions),
                timeout=_REQUEST_TIMEOUT_S)
        finally:
            _request_sem().release()
    except (asyncio.TimeoutError, asyncio.CancelledError):
        try:
            writer.close()
        except Exception:
            pass
    except Exception as e:
        try:
            writer.write((json.dumps({"status": "error", "msg": str(e)}) + "\n").encode())
            await writer.drain()
        except Exception:
            pass
        finally:
            try:
                writer.close()
            except Exception:
                pass
    finally:
        try:
            writer.close()
        except Exception:
            pass
        try:
            await writer.wait_closed()
        except Exception:
            pass


async def _handle_client_inner(
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

        if req.get("kind") == "state":
            # Snapshot of the last-known lamp states from 0xDB status pushes.
            # No BLE interaction — serves the HA integration's fast polling.
            state = {}
            for mac, sess in sessions.items():
                if sess.state_cache:
                    state[mac] = {
                        **sess.state_cache,
                        "name": sess.lamp.get("name", mac),
                    }
                else:
                    state[mac] = {"name": sess.lamp.get("name", mac), "unknown": True}
            resp = {"status": "ok", "state": state, "ts": time.time()}
        elif not targets:
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
            # Query commands respond per-lamp; query every session in parallel
            # (each session owns its own lamp's connection and decrypts only its
            # own frames), so status for N lamps takes ~1 query time, not N.
            response_opcode = req.get("response_opcode", 0xDB)
            results = []
            errors = []

            async def _one(sess):
                try:
                    pkt = await sess.query(opcode, params, response_opcode)
                    if pkt:
                        return {"ok": True, "sess": sess, "pkt": pkt}
                    return {"ok": False, "sess": sess, "err": "no response"}
                except Exception as e:
                    return {"ok": False, "sess": sess, "err": str(e)}

            for outcome in await asyncio.gather(*[_one(s) for s in targets]):
                if outcome["ok"]:
                    results.append({
                        "mac": outcome["sess"].lamp["mac"],
                        "name": outcome["sess"].lamp["name"],
                        "payload": list(outcome["pkt"]),
                    })
                else:
                    errors.append(f"{outcome['sess'].lamp['name']}: {outcome['err']}")
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
    _init_shared_seq(lamps)
    sessions: dict[str, DaemonSession] = {
        lamp["mac"].upper(): DaemonSession(lamp) for lamp in lamps
    }
    # Connect serially (not concurrently): BlueZ/dbus errors with
    # "Operation already in progress" when several GATT connects hit the
    # adapter at once. Serial connects + a small settle delay are reliable.
    # Each start is additionally hard-bounded: a hung BLE stack call must
    # never stall the whole initial connect (one wedged lamp blocked all
    # six for 4+ minutes before this guard existed).
    for mac, sess in sessions.items():
        for attempt in range(_MAX_START_ATTEMPTS):
            if attempt > 0:
                try:
                    await sess.stop()
                except Exception:
                    pass
                await asyncio.sleep(1.0)
            try:
                await asyncio.wait_for(sess.start(), timeout=90.0)
                break
            except asyncio.TimeoutError:
                print(f"  [{sess.lamp['name']}] attempt {attempt + 1} timed out "
                      f"(hung BLE stack, aborted)", flush=True)
            except Exception as e:
                print(f"  [{sess.lamp['name']}] attempt {attempt + 1} failed: {e}", flush=True)
        if sess.ctrl.session_key is None:
            try:
                await sess.stop()
            except Exception:
                pass
            print(f"  [{sess.lamp['name']}] giving up after {_MAX_START_ATTEMPTS} attempts", flush=True)
    return {mac: s for mac, s in sessions.items() if s.ctrl.session_key is not None}


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


# Nightly group-membership reconcile: re-assert every lamp's 0xD7 firmware
# membership from groups.json (remove-all-others + add-own, unicast through
# the lamp's own session). Firmware tables drift or never stick (0xD7 needs
# a quiet link); re-asserting daily during dead hours converges them instead
# of letting them rot. TELINK_RECONCILE_TIME="HH:MM" local (default 03:00);
# empty/0 disables.
_RECONCILE_TIME = (os.environ.get("TELINK_RECONCILE_TIME", "03:00") or "").strip()


def _reconcile_plan(groups: list[dict], mac: str) -> tuple[list[int], list[int]]:
    """(remove_addrs, add_addrs) for one lamp from registry membership."""
    mac_u = mac.upper()
    mine = sorted({int(g["address"]) for g in groups
                   if mac_u in {str(m).upper() for m in (g.get("lamps") or [])}})
    others = sorted({int(g["address"]) for g in groups} - set(mine))
    return others, mine


async def _reconcile_loop(sessions: dict[str, DaemonSession],
                          stop_event: asyncio.Event) -> None:
    """Daily firmware group-membership reconcile, one lamp at a time."""
    import datetime as _dt

    def _next_run() -> float:
        try:
            hh, mm = _RECONCILE_TIME.split(":")
            now = _dt.datetime.now()
            nxt = now.replace(hour=int(hh), minute=int(mm), second=0, microsecond=0)
            if nxt <= now:
                nxt += _dt.timedelta(days=1)
            return nxt.timestamp()
        except Exception:
            return time.time() + 24 * 3600

    if not _RECONCILE_TIME or _RECONCILE_TIME == "0":
        return
    while not stop_event.is_set():
        delay = max(0.0, _next_run() - time.time())
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=delay)
            return  # shutting down
        except asyncio.TimeoutError:
            pass
        try:
            groups = group_registry.load()
        except Exception as err:
            print(f"reconcile: cannot load groups: {err}", flush=True)
            continue
        if not groups:
            continue
        ok, fail = 0, 0
        for mac, sess in list(sessions.items()):
            if stop_event.is_set():
                return
            try:
                if not (sess.ctrl.client and sess.ctrl.client.is_connected):
                    continue  # only touch live sessions; the watcher owns the rest
                remove_addrs, add_addrs = _reconcile_plan(groups, mac)
                dst = sess.lamp.get("mesh_address")
                for op, addr in ([(0, a) for a in remove_addrs]
                                 + [(1, a) for a in add_addrs]):
                    await sess.send(0xD7, bytes([op, addr & 0xFF, (addr >> 8) & 0xFF]),
                                    int(dst) if dst else 0xFFFF)
                ok += 1
            except asyncio.CancelledError:
                raise
            except Exception as err:
                fail += 1
                print(f"reconcile: {mac} failed: {err}", flush=True)
        print(f"reconcile: {ok} ok, {fail} failed "
              f"({len(sessions)} sessions)", flush=True)


async def _watch_config(sessions: dict[str, DaemonSession], stop_event: asyncio.Event,
                        connect_busy: asyncio.Event | None = None) -> None:
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
                # by exact MAC so sessions always map to the right lamp. Skip
                # while the initial background connect is still running.
                now = asyncio.get_event_loop().time()
                if now >= next_reconnect and not (connect_busy and connect_busy.is_set()):
                    missing = _missing_lamp_entries(sessions)
                    if missing:
                        next_reconnect = now + 15.0
                        for lamp in missing:
                            mac = lamp["mac"].upper()
                            # Per-lamp backoff: a powered-off lamp must not
                            # trigger a multi-second adapter scan every cycle.
                            # Skip until its personal retry time arrives.
                            retry_at = _reconnect_backoff.get(mac, 0.0)
                            if now < retry_at:
                                continue
                            print(f"Reconnecting missing lamp {mac} ...", flush=True)
                            sess = DaemonSession(lamp)
                            try:
                                # NOTE: no outer _ADAPTER_LOCK here — start()
                                # acquires it itself; double-locking deadlocks
                                # (asyncio locks are not reentrant) and every
                                # maintainer retry burned the full 150 s.
                                await asyncio.wait_for(sess.start(), timeout=150.0)
                                sessions[mac] = sess
                                _reconnect_backoff.pop(mac, None)
                                print(f"  [{lamp['name']}] reconnected", flush=True)
                            except Exception as e:
                                try:
                                    await sess.stop()
                                except Exception:
                                    pass
                                fails = _reconnect_fails.get(mac, 0) + 1
                                _reconnect_fails[mac] = fails
                                wait = min(_RECONNECT_BASE_S * (2 ** (fails - 1)),
                                           _RECONNECT_MAX_S)
                                _reconnect_backoff[mac] = (
                                    asyncio.get_event_loop().time() + wait)
                                print(f"  [{lamp['name']}] reconnect failed: {e} "
                                      f"(retry in {wait:.0f}s)", flush=True)
                        last_sig = _lamps_signature()


async def _idle_sweeper(sessions: dict[str, DaemonSession], stop_event: asyncio.Event) -> None:
    """Release connections idle too long (TELINK_IDLE_TIMEOUT>0) so the lamps go
    back to advertising and stay reachable. With TELINK_IDLE_TIMEOUT=0 the
    daemon keeps connections permanently (health-check keepalive handles dead
    links). Sessions are kept in the dict and reconnect on the next command."""
    while not stop_event.is_set():
        await asyncio.sleep(10.0)
        if IDLE_TIMEOUT <= 0:
            continue
        now = asyncio.get_event_loop().time()
        for mac, sess in list(sessions.items()):
            connected = sess.ctrl.client and sess.ctrl.client.is_connected
            if connected and (now - sess._last_cmd_time) >= IDLE_TIMEOUT:
                print(f"  [{sess.lamp['name']}] idle {IDLE_TIMEOUT:.0f}s - releasing connection", flush=True)
                await sess.stop()


def _raise_nofile_limit():
    """Lift the soft nofile limit to the hard one (docker default soft=1024).

    The daemon is a long-lived multi-socket process (one HCI monitor + one
    BLE connection per lamp, plus one API TCP connection per poll from HA).
    A leak creeping over 1024 wedged the whole daemon SILENTLY for hours
    (socket.accept() died with Errno 24 and the lamps simply "did nothing"
    until a manual restart) — this makes an accidental limit hit impossible
    to reach by mere drift and buys the watchdog time to act.
    """
    try:
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        target = max(8192, min(hard, 65536))
        resource.setrlimit(resource.RLIMIT_NOFILE, (min(target, hard), hard))
        print(f"nofile limit: {soft} -> {min(target, hard)} (hard {hard})", flush=True)
    except Exception as err:
        print(f"[warn] could not raise nofile limit: {err}", flush=True)


_FD_WARN_THRESHOLD = 4096  # watchdog fatal above this (limit is 8192+)
_FD_CHECK_INTERVAL = 60.0


_BLUEZ_CHECK_INTERVAL = float(os.environ.get("TELINK_BLUEZ_CHECK_INTERVAL", "60"))
_BLUEZ_MAX_FAILS = int(os.environ.get("TELINK_BLUEZ_MAX_FAILS", "4"))


async def _bluez_adapters_ok(timeout: float = 10.0) -> bool:
    """True when host BlueZ exposes at least one adapter via D-Bus.

    Host bluetoothd dies every ~30-60 min under BLE load and nothing
    restarts it; every BLE op then hangs and the daemon freezes silently.
    An external watchdog resurrects bluetoothd — this check detects the
    outage so we can re-exec onto a fresh D-Bus connection afterwards
    (bleak never recovers a dead bus on its own)."""
    try:
        from dbus_fast import Message
        from dbus_fast.aio import MessageBus
        bus = await MessageBus(
            bus_address="unix:path=/run/dbus/system_bus_socket").connect()
        try:
            r = await asyncio.wait_for(bus.call(Message(
                destination="org.bluez", path="/",
                interface="org.freedesktop.DBus.ObjectManager",
                member="GetManagedObjects", signature="", body=[])),
                timeout=timeout)
            return any("org.bluez.Adapter1" in ifaces
                       for ifaces in r.body[0].values())
        finally:
            try:
                bus.disconnect()
            except Exception:
                pass
    except Exception:
        return False


async def _bluez_watchdog(sessions: dict[str, "DaemonSession"],
                          stop_event: asyncio.Event) -> None:
    """Re-exec the daemon when host BlueZ is gone.

    Stops sessions cleanly first (bounded) so lamps drop to advertising
    instead of holding orphaned links, then os.execv's into a fresh
    process: fresh D-Bus connection, fresh initial connect."""
    fails = 0
    while not stop_event.is_set():
        await asyncio.sleep(_BLUEZ_CHECK_INTERVAL)
        if stop_event.is_set():
            return
        if await _bluez_adapters_ok():
            fails = 0
            continue
        fails += 1
        print(f"BlueZ check failed #{fails}; host bluetoothd likely dead",
              flush=True)
        if fails < _BLUEZ_MAX_FAILS:
            continue
        print("BlueZ gone; stopping sessions and re-execing daemon ...",
              flush=True)
        try:
            await asyncio.wait_for(asyncio.gather(
                *[s.stop() for s in list(sessions.values())],
                return_exceptions=True), timeout=30.0)
        except Exception:
            pass
        sys.stdout.flush()
        os.execv(sys.executable,
                 [sys.executable, os.path.abspath(__file__)] + sys.argv[1:])


async def _fd_watchdog():
    """Kill the process when file descriptors saturate.

    A silent FD leak took the daemon down twice overnight (lamps 'not
    working' at 04:00/08:00 until a manual HA restart). Failing to accept
    new API connections wedges every consumer without a single visible
    symptom. With this watchdog the failure mode becomes a ~10 s blip: the
    container restarts itself via `restart: unless-stopped` and reconnects.
    """
    while True:
        try:
            count = len(os.listdir("/proc/self/fd"))
            if count >= _FD_WARN_THRESHOLD:
                print(f"FATAL: {count} open fds >= {_FD_WARN_THRESHOLD}; restarting process "
                      f"(docker will revive it). Apologies — a leak wedged earlier runs.",
                      flush=True)
                sys.stdout.flush()
                os._exit(2)
        except Exception:
            pass  # /proc unavailable in some containers — degrade silently
        await asyncio.sleep(_FD_CHECK_INTERVAL)


async def start_daemon():
    _raise_nofile_limit()
    if os.path.exists(PAUSE_FILE):
        print("Daemon paused (flag present) — holding.", flush=True)

    with open(PID_PATH, "w") as f:
        f.write(str(os.getpid()))

    if os.path.exists(SOCK_PATH):
        os.unlink(SOCK_PATH)

    sessions: dict[str, DaemonSession] = {}
    lamps = registry.load()
    has_lamps = bool(lamps)

    async def _client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        await _handle_client(reader, writer, sessions)

    # Start the server FIRST so the web app can reach us immediately. The
    # initial lamp connect runs in the background and never blocks startup
    # (a wedged/slow connect must not make every add-on request hang).
    if TCP_HOST:
        server = await asyncio.start_server(_client, TCP_HOST, TCP_PORT)
        print(f"Daemon ready ({len(sessions)} lamp(s)). TCP: {TCP_HOST}:{TCP_PORT}", flush=True)
    else:
        server = await asyncio.start_unix_server(_client, path=SOCK_PATH)
        os.chmod(SOCK_PATH, 0o600)
        print(f"Daemon ready ({len(sessions)} lamp(s)). Socket: {SOCK_PATH}", flush=True)

    connect_busy = asyncio.Event()
    if not os.path.exists(PAUSE_FILE) and has_lamps:
        print(f"Connecting to {len(lamps)} lamp(s) in background ...", flush=True)

        async def _initial():
            connect_busy.set()
            try:
                # Warm the persistent scanner first so connects resolve from
                # the live table instead of each starting their own scan.
                try:
                    await get_scanner().start()
                    await asyncio.sleep(3.0)
                except Exception as err:
                    print(f"  [warn] persistent scanner failed to start: {err} "
                          f"(connects fall back per-attempt)", flush=True)
                new = await _build_sessions()
                sessions.update(new)
                print(f"Initial connect done ({len(new)} lamp(s)).", flush=True)
            finally:
                connect_busy.clear()

        asyncio.get_event_loop().create_task(_initial())

    loop = asyncio.get_event_loop()
    stop_event = asyncio.Event()

    def _on_signal():
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _on_signal)

    watcher = asyncio.get_event_loop().create_task(
        _watch_config(sessions, stop_event, connect_busy)
    )
    idle_sweeper = asyncio.get_event_loop().create_task(
        _idle_sweeper(sessions, stop_event)
    )
    fd_watchdog = asyncio.get_event_loop().create_task(_fd_watchdog())
    bluez_watchdog = asyncio.get_event_loop().create_task(
        _bluez_watchdog(sessions, stop_event))
    reconciler = asyncio.get_event_loop().create_task(
        _reconcile_loop(sessions, stop_event)
    )

    async with server:
        await stop_event.wait()
        server.close()

    watcher.cancel()
    idle_sweeper.cancel()
    reconciler.cancel()
    bluez_watchdog.cancel()
    for task in (watcher, idle_sweeper, reconciler, bluez_watchdog):
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
