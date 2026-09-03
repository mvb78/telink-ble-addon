"""
Host-runnable unit tests for the protocol updates ported from the
telink-ble-esp32 research (2026-09-03, addon v1.2.0).

Run from telink-addon/python/:
    python3 -m pytest tests/ -v

No BLE hardware needed — pure logic only. Hardware validation checklist:
docs/TELINK_MESH_PROTOCOL.md §8.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lamp_registry import UNICAST_ADDR_MAX, allocate_unicast_addr, resolve_unicast_addr
from telink_crypto import build_delete_pairing_frame, derive_base_key, java_aes
from telink_mesh import parse_short_group_response
from telink_ble import AddrConfirmWatcher


# ── 0x0A delete-pairing proof frame ──────────────────────────────────────

def test_delete_pairing_frame_layout():
    rand = bytes(range(0x10, 0x18))
    frame = build_delete_pairing_frame("Smart_qXsx", "8888", rand)
    assert len(frame) == 17
    assert frame[0] == 0x0A
    assert frame[1:9] == rand
    # proof = java_aes(base_key, rand ‖ zeros)[8:16]  (base_key = AES key)
    base_key = derive_base_key("Smart_qXsx", "8888")
    expected_proof = java_aes(base_key, rand + b"\x00" * 8)[8:16]
    assert frame[9:17] == expected_proof


def test_delete_pairing_frame_depends_on_rand():
    f1 = build_delete_pairing_frame("out_of_mesh", "123", b"\x01" * 8)
    f2 = build_delete_pairing_frame("out_of_mesh", "123", b"\x02" * 8)
    assert f1[0] == f2[0] == 0x0A
    assert f1[9:17] != f2[9:17]


# ── 0xD4 short group response parse ─────────────────────────────────────

def _short_group_pkt(ids):
    pkt = bytearray(20)
    pkt[7] = 0xD4
    for i, b in enumerate(ids):
        if 10 + i >= 20:
            break
        pkt[10 + i] = b
    for i in range(len(ids), 10):
        pkt[10 + i] = 0xFF
    return bytes(pkt)


def test_short_group_response_parses_ids():
    pkt = _short_group_pkt([0x01, 0x03, 0x0A, 0xFF, 0x02])
    assert parse_short_group_response(pkt) == [0x8001, 0x8003, 0x800A]


def test_short_group_response_empty_and_full():
    assert parse_short_group_response(_short_group_pkt([0xFF])) == []
    assert parse_short_group_response(_short_group_pkt(list(range(0x01, 0x0B)))) == \
        [0x8000 | b for b in range(0x01, 0x0B)]


# ── 0xE1 unencrypted address-confirm watcher ────────────────────────────

def _e1_notify_value(addr: int) -> bytes:
    v = bytearray(20)
    v[7] = 0xE1
    v[8] = 0x11
    v[9] = 0x02  # vendor 0x0211 LE
    v[10] = addr & 0xFF
    v[11] = (addr >> 8) & 0xFF
    return bytes(v)


def _acl_rx(att_handle: int, value: bytes) -> bytes:
    """HCI ACL RX fragment: handle+flags, data len, L2CAP (pdu len + CID 0x0004),
    ATT_NOTIFY (0x1b) + handle + value."""
    att = bytes([0x1B]) + att_handle.to_bytes(2, "little") + value
    l2cap = len(att).to_bytes(2, "little") + b"\x04\x00"
    return (0x0040).to_bytes(2, "little") \
        + (len(l2cap) + len(att)).to_bytes(2, "little") + l2cap + att


def _monitor_packet(opcode: int, payload: bytes) -> bytes:
    return opcode.to_bytes(2, "little") + b"\x00\x00" \
        + len(payload).to_bytes(2, "little") + payload


def test_watcher_matches_e1_confirm():
    pkt = _monitor_packet(0x0005, _acl_rx(0x0012, _e1_notify_value(0x002A)))
    packets, rest = AddrConfirmWatcher._parse_packets(pkt)
    assert rest == b""
    assert len(packets) == 1
    assert AddrConfirmWatcher._match_packet(*packets[0]) == 0x002A


def test_watcher_parser_reassembles_split_stream():
    pkt = _monitor_packet(0x0005, _acl_rx(0x0012, _e1_notify_value(0x0005)))
    half = len(pkt) - 4
    packets, rest = AddrConfirmWatcher._parse_packets(pkt[:half])
    assert packets == []
    packets2, rest2 = AddrConfirmWatcher._parse_packets(rest + pkt[half:])
    assert rest2 == b""
    assert AddrConfirmWatcher._match_packet(*packets2[0]) == 0x0005


def test_watcher_rejects_wrong_notify_handle():
    pkt = _monitor_packet(0x0005, _acl_rx(0x0013, _e1_notify_value(0x002A)))
    packets, _ = AddrConfirmWatcher._parse_packets(pkt)
    op, payload = packets[0]
    assert AddrConfirmWatcher._match_packet(op, payload) is None


def test_watcher_rejects_wrong_att_opcode():
    # ATT opcode 0x4B instead of ATT_NOTIFY 0x1b
    pkt = _monitor_packet(0x0005, bytes([0x4B]) + (0x0012).to_bytes(2, "little")
                          + _e1_notify_value(0x002A))
    packets, _ = AddrConfirmWatcher._parse_packets(pkt)
    op, payload = packets[0]
    assert AddrConfirmWatcher._match_packet(op, payload) is None


def test_watcher_rejects_wrong_vendor():
    v = bytearray(_e1_notify_value(0x002A))
    v[8] = 0x99
    pkt = _monitor_packet(0x0005, _acl_rx(0x0012, bytes(v)))
    packets, _ = AddrConfirmWatcher._parse_packets(pkt)
    op, payload = packets[0]
    assert AddrConfirmWatcher._match_packet(op, payload) is None


def test_watcher_rejects_wrong_frame_opcode():
    v = bytearray(_e1_notify_value(0x002A))
    v[7] = 0xDB  # ordinary status push
    pkt = _monitor_packet(0x0005, _acl_rx(0x0012, bytes(v)))
    packets, _ = AddrConfirmWatcher._parse_packets(pkt)
    op, payload = packets[0]
    assert AddrConfirmWatcher._match_packet(op, payload) is None


def test_watcher_rejects_out_of_range_addr():
    for addr in (0x0000, 251, 0xFFFF):
        pkt = _monitor_packet(0x0005, _acl_rx(0x0012, _e1_notify_value(addr)))
        packets, _ = AddrConfirmWatcher._parse_packets(pkt)
        op, payload = packets[0]
        assert AddrConfirmWatcher._match_packet(op, payload) is None, hex(addr)


def test_watcher_rejects_non_acl_packets():
    pkt = _monitor_packet(0x0006, _acl_rx(0x0012, _e1_notify_value(0x002A)))  # event, not ACL RX
    packets, _ = AddrConfirmWatcher._parse_packets(pkt)
    op, payload = packets[0]
    assert AddrConfirmWatcher._match_packet(op, payload) is None


def test_watcher_unavailable_wait_returns_none():
    import asyncio
    w = AddrConfirmWatcher.__new__(AddrConfirmWatcher)
    w._sock = None
    w._buf = b""
    assert asyncio.new_event_loop().run_until_complete(w.wait(timeout=0.1)) is None


# ── unicast address allocation (1..250, auto) ────────────────────────────

def test_allocate_skips_used_addresses():
    lamps = [{"mesh_address": 1}, {"mesh_address": 2}, {"mesh_address": None}]
    assert allocate_unicast_addr(lamps) == 3


def test_allocate_full_range_raises():
    lamps = [{"mesh_address": a} for a in range(1, UNICAST_ADDR_MAX + 1)]
    try:
        allocate_unicast_addr(lamps)
        raise AssertionError("expected ValueError")
    except ValueError:
        pass


def test_resolve_auto_variants():
    lamps = [{"mesh_address": 1}, {"mesh_address": 3}]
    assert resolve_unicast_addr("auto", lamps) == (2, True)
    assert resolve_unicast_addr(None, lamps) == (2, True)
    assert resolve_unicast_addr("", lamps) == (2, True)


def test_resolve_explicit():
    lamps = [{"mesh_address": 1}]
    assert resolve_unicast_addr(42, lamps) == (42, False)
    assert resolve_unicast_addr("7", lamps) == (7, False)
    assert resolve_unicast_addr(UNICAST_ADDR_MAX, lamps) == (UNICAST_ADDR_MAX, False)


def test_resolve_rejects_out_of_range_and_garbage():
    lamps = []
    for bad in (0, -1, 251, 0xFFFF, "bogus", object()):
        try:
            resolve_unicast_addr(bad, lamps)
            raise AssertionError(f"expected ValueError for {bad!r}")
        except ValueError:
            pass
