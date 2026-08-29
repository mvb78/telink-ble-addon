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
)
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
    get_session_key,
)
from telink_mesh import SequenceManager, build_mesh_packet

BROADCAST = 0xFFFF

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
    Requires CAP_NET_ADMIN.  Returns None if permission denied.

    To use without sudo:
      sudo setcap cap_net_admin,cap_net_raw+eip $(readlink -f .venv/bin/python3)
    """
    try:
        sock = socket.socket(_AF_BLUETOOTH, socket.SOCK_RAW, _BTPROTO_HCI)
        addr = _sockaddr_hci(_AF_BLUETOOTH, _HCI_DEV_NONE, _HCI_CHANNEL_MONITOR)
        ret = _libc.bind(sock.fileno(), ctypes.byref(addr), ctypes.sizeof(addr))
        if ret != 0:
            sock.close()
            return None
        sock.settimeout(0.2)  # blocking with short timeout — avoid epoll issues
        return sock
    except Exception:
        return None


class TelinkController:
    def __init__(self, mac: str, name: str, password: str):
        self.mac = mac.upper()
        self.name = name
        self.password = password
        self.mac_bytes = bytes.fromhex(mac.replace(":", ""))
        self.client = None
        self.seq_manager = SequenceManager()
        self.session_key = None
        self._notify_queue: asyncio.Queue = asyncio.Queue()
        self._monitor_task: asyncio.Task | None = None
        self._monitor_sock: socket.socket | None = None

    async def connect(self):
        print(f"  Scanning for {self.name} ({self.mac}) ...")
        target = None

        def callback(device, adv):
            nonlocal target
            if not target and device.address.upper() == self.mac:
                target = device

        async with BleakScanner(callback) as scanner:
            for _ in range(6):
                await asyncio.sleep(5.0)
                if target:
                    break

        if not target:
            raise Exception(f"{self.mac} not found — is the phone app disconnected?")

        # Open the HCI monitor socket before connecting so we don't miss the
        # first notification burst that arrives right after login.
        self._monitor_sock = _open_hci_monitor()
        if self._monitor_sock:
            self._monitor_task = asyncio.get_event_loop().create_task(
                self._hci_monitor_loop()
            )
        else:
            print("  [warn] HCI monitor unavailable (needs CAP_NET_ADMIN)")
            print("         run with sudo, or once: sudo setcap cap_net_admin,cap_net_raw+eip $(readlink -f .venv/bin/python3)")

        self.client = BleakClient(target.address)
        await self.client.connect()
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
                pkt_len = buf[pos + 4] | (buf[pos + 5] << 8)
                if pos + 6 + pkt_len > len(buf):
                    break
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
                    if handle == 0x0012 and len(payload) >= 31:
                        raw_notify = payload[11:31]
                        if self.session_key:
                            plain = decrypt_notification(self.session_key, raw_notify, self.mac_bytes)
                            if plain:
                                self._notify_queue.put_nowait(plain)
            buf = buf[pos:]

    def _on_bleak_notify(self, characteristic, data: bytearray):
        """Bleak notification callback — fires if CCCD subscription succeeded."""
        if self.session_key and len(data) == 20:
            plain = decrypt_notification(self.session_key, bytes(data), self.mac_bytes)
            if plain:
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
        """
        loop = asyncio.get_event_loop()
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            remaining = deadline - loop.time()
            try:
                pkt = await asyncio.wait_for(
                    self._notify_queue.get(), timeout=min(remaining, 0.5)
                )
                if pkt[7] == opcode:
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
        except Exception:
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

    _log(f"scan: starting BleakScanner (timeout={timeout}s)")
    async with BleakScanner(callback) as scanner:
        await asyncio.sleep(timeout)
    _log(f"scan: finished, raw candidates={len(found)}")

    return [{"mac": mac, "name": name} for mac, name in found.items()]
