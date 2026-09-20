import asyncio
import ctypes
import os
import socket
from bleak import BleakClient, BleakScanner

import sys

from config import (
    SERVICE_UUID,
    CHAR_COMMAND_UUID,
    CHAR_NOTIFY_UUID,
    CHAR_PAIR_UUID,
    KNOWN_PASSWORDS,
    VENDOR_ID,
    HCI_ADAPTER,
    hci_adapter_index,
    resolve_hci_adapter,
)


def active_hci_adapter() -> str | None:
    """Current kernel hciN name for the TELINK_HCI_ADAPTER pin (None = default).

    Resolved on every call so a replugged/re-enumerated dongle is picked up
    without restarts guessing stale hciN names. Cheap sysfs reads.
    """
    if not HCI_ADAPTER:
        return None
    return resolve_hci_adapter(HCI_ADAPTER)


def scanner_kwargs() -> dict:
    """bleak scanner kwargs pinned to the exclusive adapter (if configured)."""
    name = active_hci_adapter()
    return {"adapter": name} if name else {}


def client_kwargs() -> dict:
    """bleak client kwargs pinned to the exclusive adapter (if configured)."""
    name = active_hci_adapter()
    return {"adapter": name} if name else {}
# CHAR_NOTIFY_UUID (0d1911): lamp sends ATT_NOTIFY (opcode 0x1b) at handle 0x0012
# automatically after commands, WITHOUT requiring CCCD to be set.
# BlueZ discards these packets because CCCD was never written (and the lamp rejects
# CCCD writes with ATT 0x0e). We intercept them via a raw HCI_CHANNEL_MONITOR socket
# that shadows BlueZ traffic read-only.
from telink_crypto import (
    derive_base_key,
    build_challenge,
    verify_sample_s,
    encrypt_packet,
    decrypt_notification,
    decrypt_mesh_notification,
    decrypt_notification_auto,
    get_session_key,
)
from telink_mesh import SequenceManager, build_mesh_packet

BROADCAST = 0xFFFF

# Debug: print every decrypted notification. Very noisy (10k+ lines/hour on a
# live mesh) — it also slows the daemon's event loop via stdout I/O. Off by
# default; enable with TELINK_DEBUG_NOTIFY=1 when capturing frames.
DEBUG_NOTIFY = os.environ.get("TELINK_DEBUG_NOTIFY", "").lower() in ("1", "true", "yes")

# ATT_NOTIFY opcode + handle 0x0012 (little-endian) — 3-byte prefix we scan for
_ATT_NOTIFY_PREFIX = bytes([0x1B, 0x12, 0x00])

# sockaddr_hci: hci_family(u16) + hci_dev(u16) + hci_channel(u16)
# HCI_DEV_NONE=0xffff selects all adapters; HCI_CHANNEL_MONITOR=2 is read-only.
class _sockaddr_hci(ctypes.Structure):
    _fields_ = [
        ("hci_family",  ctypes.c_uint16),
        ("hci_dev",     ctypes.c_uint16),
        ("hci_channel", ctypes.c_uint16),
    ]


_libc = ctypes.CDLL("libc.so.6", use_errno=True)
_AF_BLUETOOTH    = 31
_BTPROTO_HCI     = 1
_HCI_DEV_NONE    = 0xFFFF
_HCI_CHANNEL_MONITOR = 2


def _open_hci_monitor() -> socket.socket | None:
    """
    Open a read-only HCI_CHANNEL_MONITOR socket (same as btmon uses).
    Requires CAP_NET_ADMIN.  Returns None if permission denied (as it does
    inside HAOS add-on containers, where raw AF_BLUETOOTH sockets are denied).

    The monitor channel only binds to HCI_DEV_NONE (all adapters); exclusivity
    is enforced per-packet instead (see _monitor_index_ok()): every btmon
    packet carries its adapter index at [2..3], and readers drop anything
    that is not the pinned adapter. To use without sudo:
      sudo setcap cap_net_admin,cap_net_raw+eip $(readlink -f .venv/bin/python3)
    """
    try:
        sock = socket.socket(_AF_BLUETOOTH, socket.SOCK_RAW, _BTPROTO_HCI)
    except OSError as err:
        print(f"  [warn] HCI monitor: socket(AF_BLUETOOTH) failed: {err}", file=sys.stderr, flush=True)
        return None
    try:
        addr = _sockaddr_hci(_AF_BLUETOOTH, _HCI_DEV_NONE, _HCI_CHANNEL_MONITOR)
        ret = _libc.bind(sock.fileno(), ctypes.byref(addr), ctypes.sizeof(addr))
    except OSError as err:
        print(f"  [warn] HCI monitor: bind failed: {err}", file=sys.stderr, flush=True)
        sock.close()
        return None
    if ret != 0:
        err = ctypes.get_errno()
        print(f"  [warn] HCI monitor: bind errno={err} ({os.strerror(err)})", file=sys.stderr, flush=True)
        sock.close()
        return None
    sock.settimeout(0.2)  # blocking with short timeout — avoid epoll issues
    return sock


def _monitor_index_ok(pkt_index: int) -> bool:
    """True when this btmon packet's adapter index matches the pin.

    The monitor channel always binds to HCI_DEV_NONE (a per-adapter bind is
    rejected with EINVAL), so exclusivity is enforced here: with
    TELINK_HCI_ADAPTER set, only packets from that controller index are
    processed. Unset = accept everything (old behavior).
    """
    want = hci_adapter_index()
    return want is None or pkt_index == want


class AddrConfirmWatcher:
    """Watch the HCI monitor for the UNENCRYPTED 0xE1 address-confirm push.

    After a 0xE0 address-assign (dst 0x0000) the lamp pushes a raw 20-byte
    frame on the notify characteristic (handle 0x0012): [7]=0xE1,
    [8..10]=vendor 0x0211, [10..12]=assigned address LE (protocol doc §6.2,
    bench-validated in telink-ble-esp32). The frame is plaintext, so it fails
    every decryption attempt in the normal notify path and must be matched on
    the raw bytes here.

    Create the watcher BEFORE sending 0xE0 so the push isn't missed. All
    methods degrade to no-ops when the monitor is unavailable (HAOS add-on
    container — caller falls back to the old blind-settle behavior).
    """

    _NOTIFY_ATT_HANDLE = 0x0012
    _HCI_MON_ACL_RX_PKT = 0x0005

    def __init__(self):
        self._sock = _open_hci_monitor()
        self._buf = b""

    @property
    def available(self) -> bool:
        return self._sock is not None

    async def wait(self, timeout: float = 4.0) -> int | None:
        """Wait for the confirm push; returns the reported address or None."""
        if self._sock is None:
            return None
        loop = asyncio.get_event_loop()
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            try:
                chunk = await asyncio.to_thread(self._sock.recv, 4096)
            except asyncio.CancelledError:
                return None
            except (socket.timeout, OSError):
                continue
            self._buf += chunk
            packets, self._buf = self._parse_packets(self._buf)
            for pkt_opcode, payload in packets:
                reported = self._match_packet(pkt_opcode, payload)
                if reported is not None:
                    self._buf = b""
                    return reported
        return None

    @staticmethod
    def _parse_packets(buf: bytes) -> tuple[list, bytes]:
        """Split a btmon byte stream into (opcode, payload) tuples.

        Monitor packet layout: [0..1] opcode LE u16, [2..3] adapter index,
        [4..5] payload length LE u16, [6..] payload. Returns the complete
        packets plus the trailing incomplete bytes. Packets from a different
        adapter than the TELINK_HCI_ADAPTER pin are dropped here.
        """
        packets = []
        pos = 0
        while pos + 6 <= len(buf):
            pkt_opcode = buf[pos] | (buf[pos + 1] << 8)
            pkt_index = buf[pos + 2] | (buf[pos + 3] << 8)
            pkt_len = buf[pos + 4] | (buf[pos + 5] << 8)
            if pos + 6 + pkt_len > len(buf):
                break
            if _monitor_index_ok(pkt_index):
                packets.append((pkt_opcode, buf[pos + 6: pos + 6 + pkt_len]))
            pos += 6 + pkt_len
        return packets, buf[pos:]

    @staticmethod
    def _match_packet(pkt_opcode: int, payload: bytes) -> int | None:
        """Return the assigned address if this monitor packet is the 0xE1 push."""
        if pkt_opcode != AddrConfirmWatcher._HCI_MON_ACL_RX_PKT or len(payload) < 11:
            return None
        if payload[6] != 0x04 or payload[7] != 0x00:  # L2CAP CID = ATT
            return None
        if payload[8] != 0x1B:  # ATT_NOTIFY
            return None
        handle = payload[9] | (payload[10] << 8)
        if handle != AddrConfirmWatcher._NOTIFY_ATT_HANDLE:
            return None
        raw = payload[11:]
        if len(raw) < 12 or raw[7] != 0xE1:
            return None
        if raw[8] != (VENDOR_ID & 0xFF) or raw[9] != ((VENDOR_ID >> 8) & 0xFF):
            return None
        reported = raw[10] | (raw[11] << 8)
        if not 1 <= reported <= 250:  # unicast range sanity guard
            return None
        return reported

    def close(self):
        if self._sock:
            try:
                self._sock.close()
            except Exception:
                pass
            self._sock = None


class TelinkController:
    def __init__(self, mac: str, name: str, password: str, initial_seq: int | None = None,
                 on_plain=None):
        self.mac = mac.upper()
        self.name = name
        self.password = password
        self.mac_bytes = bytes.fromhex(mac.replace(":", ""))
        self.client = None
        self.seq_manager = SequenceManager(initial=initial_seq)
        # Optional callback for decrypted notification frames (daemon state
        # cache). First arg: decrypted plaintext frame; optional second arg:
        # the raw ATT notify value as received (sno/src header preserved).
        self.on_plain = on_plain
        self.session_key = None
        self._notify_queue: asyncio.Queue = asyncio.Queue()
        self._monitor_task: asyncio.Task | None = None
        self._monitor_sock: socket.socket | None = None

    async def connect(self, timeout: float = 8.0):
        print(f"  Scanning for {self.name} ({self.mac}) ...")
        target = None

        def callback(device, adv):
            nonlocal target
            if target:
                return
            if device.address.upper() == self.mac:
                target = device

        # Event-driven scan: check the callback result every 0.25 s instead of
        # sleeping 5 s before the first look — a lamp that is advertising is
        # found in ~0.25 s, not >=5 s (this was the reconnect penalty).
        if HCI_ADAPTER:
            print(f"  [ble] scanning on exclusive adapter {active_hci_adapter() or HCI_ADAPTER} ...", flush=True)
        loop = asyncio.get_event_loop()
        deadline = loop.time() + timeout
        async with BleakScanner(callback, **scanner_kwargs()) as scanner:
            while loop.time() < deadline:
                await asyncio.sleep(0.25)
                if target:
                    break

        if not target:
            raise Exception(
                f"{self.mac} not found — is the phone app disconnected? "
                f"(try `discover` to refresh the MAC)"
            )

        # Open the HCI monitor socket before connecting so we don't miss the
        # first notification burst that arrives right after login.
        self._monitor_sock = _open_hci_monitor()
        if self._monitor_sock:
            self._monitor_task = asyncio.get_event_loop().create_task(
                self._hci_monitor_loop()
            )
        else:
            print("  [warn] HCI monitor unavailable; notify readback disabled", flush=True)

        # Connect via the discovered device object, not the address string:
        # these lamps rotate RPA, so a string reconnect can miss/hang. Bleak
        # resolves the device's current address from the BLEDevice object.
        self.client = BleakClient(target, **client_kwargs())
        # Bounded: BlueZ can hang inside connect() forever on a stale
        # adapter (e.g. right after container start), which would wedge the
        # whole daemon behind one lamp. Fail fast so reconnect/backoff logic
        # applies instead.
        try:
            await asyncio.wait_for(self.client.connect(), timeout=15.0)
        except asyncio.TimeoutError:
            try:
                await self.client.disconnect()
            except Exception:
                pass
            raise Exception(f"{self.mac} connect timed out (adapter stale?)")
        await asyncio.sleep(0.5)

    async def disconnect(self):
        if self._monitor_task:
            self._monitor_task.cancel()
            try:
                await self._monitor_task
            except asyncio.CancelledError:
                pass
            self._monitor_task = None
        if self._monitor_sock:
            try:
                self._monitor_sock.close()
            except Exception:
                pass
            self._monitor_sock = None
        if self.client and self.client.is_connected:
            await self.client.disconnect()

    async def _hci_monitor_loop(self):
        """
        Read raw HCI monitor packets and extract ATT_NOTIFY for handle 0x0012.

        Monitor packet layout (btmon format):
          [0..3]  total length (LE u32) — length of everything after these 4 bytes
          [4..5]  opcode (LE u16)
          [6..7]  adapter index (LE u16)
          [8..11] timestamp seconds (LE u32)
          [12..15] timestamp microseconds (LE u32)
          [16..]  HCI payload

        HCI ACL RX opcode in monitor = 0x0003.
        ACL packet layout:
          [0..1]  connection handle (12 bits) + flags (4 bits), LE u16
          [2..3]  data length, LE u16
          [4..7]  L2CAP header: PDU length (LE u16) + CID (LE u16)
          [8..]   ATT PDU

        ATT_NOTIFY: opcode 0x1b + handle (2 bytes LE) + value (20 bytes)
        """
        buf = b""
        while True:
            try:
                chunk = await asyncio.to_thread(self._monitor_sock.recv, 4096)
                buf += chunk
            except asyncio.CancelledError:
                return
            except OSError:
                # timeout (no data) or transient error — yield and retry
                await asyncio.sleep(0)
                continue

            # Parse btmon packets: [opcode:2LE][index:2LE][len:2LE][HCI payload]
            pos = 0
            while pos + 6 <= len(buf):
                pkt_opcode = buf[pos] | (buf[pos + 1] << 8)
                pkt_index = buf[pos + 2] | (buf[pos + 3] << 8)
                pkt_len = buf[pos + 4] | (buf[pos + 5] << 8)
                if pos + 6 + pkt_len > len(buf):
                    break
                if not _monitor_index_ok(pkt_index):
                    pos += 6 + pkt_len
                    continue
                payload = buf[pos + 6: pos + 6 + pkt_len]
                pos += 6 + pkt_len

                # 0x0005 = HCI_MON_ACL_RX_PKT (lamp → host)
                if pkt_opcode != 0x0005 or len(payload) < 9:
                    continue
                # L2CAP CID at bytes [6..7]; 0x0004 = ATT
                if payload[6] != 0x04 or payload[7] != 0x00:
                    continue
                att_op = payload[8]
                if att_op == 0x1b and len(payload) >= 11:
                    handle = payload[9] | (payload[10] << 8)
                    if handle == 0x0012 and len(payload) >= 11:
                        # vendor path 20B at 11:31, mesh path variable: try max slice and let decrypt validate
                        raw_notify = payload[11:]
                        if self.session_key and len(raw_notify) >= 8:
                            # try 20B vendor slice first
                            plain = None
                            if len(raw_notify) >= 20:
                                plain = decrypt_notification(self.session_key, raw_notify[:20], self.mac_bytes)
                                # also try mesh format on same bytes if vendor MIC fails (fallback below)
                                if plain and plain[7] not in (0xDB, 0xDC, 0x1B, 0x14, 0x15, 0x16, 0x21):
                                    # maybe mesh format — try alternative
                                    mesh_plain = decrypt_mesh_notification(raw_notify, self.session_key, self.mac_bytes)
                                    if mesh_plain:
                                        plain = mesh_plain
                            if not plain and len(raw_notify) >= 8:
                                plain = decrypt_mesh_notification(raw_notify[: min(len(raw_notify), 32)], self.session_key, self.mac_bytes)
                            if not plain and len(raw_notify) >= 20:
                                # last try vendor auto
                                plain = decrypt_notification_auto(self.session_key, raw_notify[:20], self.mac_bytes)
                            if plain:
                                if self.on_plain:
                                    self.on_plain(plain, raw_notify)
                                self._notify_queue.put_nowait(plain)
            buf = buf[pos:]

    def _on_bleak_notify(self, characteristic, data: bytearray):
        """Bleak notification callback — fires if CCCD subscription succeeded."""
        if self.session_key and len(data) >= 8:
            raw = bytes(data)
            plain = None
            if len(raw) >= 20:
                plain = decrypt_notification(self.session_key, raw[:20], self.mac_bytes)
            if not plain:
                plain = decrypt_mesh_notification(raw, self.session_key, self.mac_bytes)
            if not plain and len(raw) >= 20:
                plain = decrypt_notification_auto(self.session_key, raw[:20], self.mac_bytes)
            if plain:
                if self.on_plain:
                    self.on_plain(plain, raw)
                self._notify_queue.put_nowait(plain)

    async def login(self):
        base_key = derive_base_key(self.name, self.password)
        r1 = os.urandom(8)
        challenge = build_challenge(base_key, r1)

        payload = bytearray(17)
        payload[0] = 0x0C
        payload[1:9] = r1
        payload[9:17] = challenge

        await self.client.write_gatt_char(CHAR_PAIR_UUID, bytes(payload), response=True)
        await asyncio.sleep(0.5)

        rsp = await self.client.read_gatt_char(CHAR_PAIR_UUID)
        if not rsp or rsp[0] != 0x0D or len(rsp) < 17:
            raise Exception(f"Login failed: {rsp.hex() if rsp else 'no response'}")

        r2 = bytes(rsp[1:9])
        sample_s = bytes(rsp[9:17])

        if not verify_sample_s(self.name, self.password, r2, sample_s):
            raise Exception("sample_s verification failed — wrong password")

        self.session_key = get_session_key(self.name, self.password, r1, r2)

        # Custom subscribe: writing 0x01 to the notify char VALUE (not CCCD) causes
        # the lamp to start sending ATT_NOTIFY.  CCCD writes disconnect the lamp.
        await self.client.write_gatt_char(CHAR_NOTIFY_UUID, b'\x01', response=True)

    async def send_packet(self, packet: bytes):
        """Encrypt and write a pre-built 20-byte mesh packet to the command characteristic."""
        if self.session_key is None:
            raise Exception("Not logged in")
        encrypted = encrypt_packet(self.session_key, packet, self.mac_bytes)
        await self.client.write_gatt_char(CHAR_COMMAND_UUID, encrypted, response=False)

    async def send_command(self, opcode: int, params: bytes, address: int = BROADCAST):
        if self.session_key is None:
            raise Exception("Not logged in")
        seq = self.seq_manager.next()
        packet = build_mesh_packet(seq, address, opcode, params)
        await self.send_packet(packet)

    async def wait_for_opcode(self, opcode: int, timeout: float = 3.0) -> bytes | None:
        """
        Wait for a decrypted notification with the given opcode.

        Drains packets from the queue, skipping opcodes that don't match.
        The lamp sends initial state broadcasts on subscribe before query responses.

        Two response layouts exist (bench-validated 2026-08-31, telink-lab):
          vendor 20B: opcode at pkt[7] (0xDB/0xDC/0xE9/0xC8/...)
          mesh layer: decrypted frame starts with op|0xC0 at pkt[0]
                      (0x14 GRP_RSP, 0x1B STATUS, 0x21 DEV_ADDR_RSP)
        """
        loop = asyncio.get_event_loop()
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            remaining = deadline - loop.time()
            try:
                pkt = await asyncio.wait_for(
                    self._notify_queue.get(), timeout=min(remaining, 0.5)
                )
                if (len(pkt) == 20 and pkt[7] == opcode) or (
                        len(pkt) != 20 and (pkt[0] & 0xC0) == 0xC0
                        and (pkt[0] & 0x3F) == opcode):
                    return pkt
            except asyncio.TimeoutError:
                continue  # keep polling until outer deadline expires
        return None

    async def drain_notifications(self, duration: float = 4.0) -> list[bytes]:
        """Collect all decrypted notifications for `duration` seconds — for diagnosis."""
        collected = []
        loop = asyncio.get_event_loop()
        deadline = loop.time() + duration
        while loop.time() < deadline:
            remaining = deadline - loop.time()
            try:
                pkt = await asyncio.wait_for(
                    self._notify_queue.get(), timeout=min(remaining, 0.5)
                )
                collected.append(pkt)
                if DEBUG_NOTIFY:
                    print(f"  [notify] opcode=0x{pkt[7]:02X}  raw={pkt.hex()}")
            except asyncio.TimeoutError:
                continue
        return collected

    async def dump_gatt(self) -> list[dict]:
        """Return all characteristics under the Telink service with their properties."""
        results = []
        for service in self.client.services:
            if service.uuid.lower() != SERVICE_UUID.lower():
                continue
            for char in service.characteristics:
                results.append({
                    "uuid": char.uuid,
                    "handle": char.handle,
                    "properties": char.properties,
                    "descriptors": [str(d) for d in char.descriptors],
                })
        return results


async def probe_lamp(mac: str, name: str) -> str | None:
    """
    Try each known password against a lamp. Returns the working password or None.
    Connects, attempts login, disconnects. Does not raise on wrong password.
    """
    for password in KNOWN_PASSWORDS:
        ctrl = TelinkController(mac, name, password)
        try:
            await ctrl.connect()
            await ctrl.login()
            await ctrl.disconnect()
            return password
        except Exception as err:
            print(f"[probe] {mac} pw={password}: {type(err).__name__}: {err}", file=sys.stderr, flush=True)
            try:
                await ctrl.disconnect()
            except Exception:
                pass
    return None


async def scan_for_telink_lamps(timeout: float = 15.0) -> list[dict]:
    """
    Scan for BLE devices advertising the Telink service UUID or manufacturer ID 0x0211.
    Some lamps (e.g. Smart_nSpq) omit the service UUID from advertisements but always
    include manufacturer data with key 0x0211.
    Returns list of {mac, name} dicts.
    """
    found = {}

    def _log(msg: str) -> None:
        print(f"[ble] {msg}", file=sys.stderr, flush=True)

    def callback(device, adv):
        uuids = [str(u).lower() for u in (adv.service_uuids or [])]
        has_service_uuid = SERVICE_UUID.lower() in uuids
        has_telink_mfr = VENDOR_ID in (adv.manufacturer_data or {})
        if (has_service_uuid or has_telink_mfr) and device.address.upper() not in found:
            found[device.address.upper()] = device.name or device.address
            _log(f"scan: candidate {device.address} ({device.name}) svc={has_service_uuid} mfr={has_telink_mfr}")

    _log(f"scan: starting BleakScanner (timeout={timeout}s)"
          + (f" on {HCI_ADAPTER}" if HCI_ADAPTER else ""))
    async with BleakScanner(callback, **scanner_kwargs()) as scanner:
        await asyncio.sleep(timeout)
    _log(f"scan: finished, raw candidates={len(found)}")

    return [{"mac": mac, "name": name} for mac, name in found.items()]
