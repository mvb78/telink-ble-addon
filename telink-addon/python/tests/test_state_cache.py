"""
Unit tests for the 0x01 state-cache (liveness push parser) — Phase 1.

Run from telink-addon/python/:
    python3 -m pytest tests/test_state_cache.py -v

No BLE hardware needed — builds synthetic 0x1B->0x11 status frames following
the vendor layout documented in docs/telink-ble.md §5.1 (op at [7]=0xDB,
payload p = pkt[10:]: p[3]=ct_hw, p[5]=on flag, p[6]=bright, p[7:10]=RGB).
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from telink_daemon import DaemonSession


def make_session() -> DaemonSession:
    return DaemonSession({"mac": "AA:BB:CC:DD:EE:FF", "name": "L1", "password": "0000"})


def status_frame(on: int = 1, bri: int = 50, ct_hw: int = 0, rgb=(0, 0, 0)) -> bytes:
    # Real vendor frames are exactly 20 B: pkt[7] = 0xDB, payload p at pkt[10:]
    # (p[3]=ct_hw, p[5]=on flag, p[6]=bright, p[7:10]=RGB).
    head = bytes(7) + bytes([0xDB]) + bytes(2)          # pkt[7] = 0xDB, p at pkt[10]
    payload = bytes([0x10, 0, 0, ct_hw, 0, on, bri,
                     rgb[0], rgb[1], rgb[2]])
    frame = head + payload
    assert len(frame) == 20
    return frame


def test_vendor_status_on():
    s = make_session()
    s.note_plain(status_frame(on=1, bri=50, ct_hw=0))
    assert s.state_cache == {**s.state_cache, "on": True, "brightness": 50,
                             "colortemp": 100, "rgb": [0, 0, 0]}
    assert isinstance(s.state_cache["ts"], float)


def test_bri_zero_is_off():
    s = make_session()
    s.note_plain(status_frame(on=1, bri=0))
    assert s.state_cache["on"] is False
    assert s.state_cache["brightness"] == 0


def test_duplicate_push_is_noop():
    s = make_session()
    s.note_plain(status_frame())
    first = dict(s.state_cache)
    s.note_plain(status_frame())                     # identical semantic payload
    assert s.state_cache["on"] == first["on"]
    assert s.state_cache["brightness"] == first["brightness"]


def test_mesh_layer_frame_ignored():
    s = make_session()
    s.note_plain(bytes([0xDB, 0x11]) + bytes(6))     # mesh 0x1B payload (starts with op)
    assert s.state_cache is None                     # only vendor frames cached


def test_ts_refreshes_on_identical_push():
    # ts = last-SEEN push (liveness), not last change: a steady lamp that
    # pushes identical state on every keepalive must stay fresh, or the
    # HA-side staleness watchdog false-positives on healthy lamps.
    import time
    s = make_session()
    s.note_plain(status_frame())
    first_ts = s.state_cache["ts"]
    time.sleep(0.01)
    s.note_plain(status_frame())
    assert s.state_cache["ts"] >= first_ts
    assert s.state_cache["brightness"] == 50


def test_byte_clustered_no_raise_short_frame():
    s = make_session()
    s.note_plain(bytes(10))                          # no op, no payload
    assert s.state_cache is None


def test_seq_jumps_forward_when_lamp_is_ahead():
    s = make_session()
    ours_before = s.ctrl.seq_manager.seq
    assert ours_before == 0x1000                    # default SequenceManager start
    frame = status_frame()
    head = bytearray(0x5000.to_bytes(3, "little")) + bytes(7)  # lamp used 0x5000
    raw = bytes(head) + frame[10:]
    s.note_plain(bytes(frame), raw)
    assert s.ctrl.seq_manager.seq > ours_before      # jumped past the lamp


def test_seq_never_rewinds():
    s = make_session()
    s.note_plain(status_frame(), raw=(0x5000).to_bytes(3, "little") + status_frame()[:17])
    ahead = s.ctrl.seq_manager.seq
    s.note_plain(status_frame(), raw=(0x0005).to_bytes(3, "little") + status_frame()[:17])
    assert s.ctrl.seq_manager.seq == ahead


def test_maybe_bump_seq_when_push_stale():
    import time as _t
    from telink_daemon import _SEQ_BUMP
    s = make_session()
    before = s.ctrl.seq_manager.seq
    # fresh push -> no bump
    s.note_plain(status_frame())
    s._maybe_bump_seq()
    assert s.ctrl.seq_manager.seq == before
    # stale push -> forward jump
    s.state_cache["ts"] = _t.time() - 3600
    s._maybe_bump_seq()
    assert s.ctrl.seq_manager.seq > before
    assert s.ctrl.seq_manager.seq - before <= _SEQ_BUMP + 2


def test_reconcile_plan_splits_own_vs_others():
    from telink_daemon import _reconcile_plan
    groups = [
        {"name": "Oberlicht", "address": 32768,
         "lamps": ["68:EC:62:02:8A:54", "68:EC:62:02:87:BC", "68:EC:62:02:87:B3"]},
        {"name": "Arbeitsplatte", "address": 32769,
         "lamps": ["68:EC:62:02:8A:E9", "68:EC:62:02:87:A5", "68:EC:62:02:87:7C"]},
    ]
    remove, add = _reconcile_plan(groups, "68:EC:62:02:87:7C")
    assert remove == [32768] and add == [32769]
    remove, add = _reconcile_plan(groups, "68:ec:62:02:87:b3")  # case-insensitive
    assert remove == [32769] and add == [32768]
    remove, add = _reconcile_plan([], "68:EC:62:02:87:7C")
    assert (remove, add) == ([], [])


def test_decode_mesh_status():
    from telink_daemon import _decode_mesh_status
    # decrypted mesh STATUS: [0xC0|0x1B, cw LE, ww LE, bri LE]
    # cw=0x4000, ww=0x4000 -> warm 50; bri=64
    pkt = bytes([0xDB, 0x00, 0x40, 0x00, 0x40, 64])
    e = _decode_mesh_status(pkt)
    assert e == {"on": True, "brightness": 64, "colortemp": 50, "rgb": None}, e
    # bri 0 -> off; cw+ww 0 -> ct unknown
    e = _decode_mesh_status(bytes([0xDB, 0, 0, 0, 0, 0]))
    assert e["on"] is False and e["brightness"] == 0 and e["colortemp"] is None
    # non-STATUS / vendor frames rejected
    assert _decode_mesh_status(bytes([0xD4, 0, 0, 0, 0, 0])) is None
    assert _decode_mesh_status(bytes(20)) is None
    assert _decode_mesh_status(bytes([0xDB, 1, 2])) is None


def test_mesh_src_to_mac(monkeypatch):
    import telink_daemon
    monkeypatch.setattr(
        telink_daemon.registry, "load",
        lambda: [{"mac": "AA:BB:CC:DD:EE:FF", "mesh_address": 6},
                 {"mac": "11:22:33:44:55:66", "mesh_address": None}])
    assert telink_daemon._mesh_src_to_mac(6) == "AA:BB:CC:DD:EE:FF"
    assert telink_daemon._mesh_src_to_mac(99) is None


def test_mesh_relay_updates_origin_session():
    import types
    from telink_daemon import DaemonSession
    DaemonSession._sessions.clear()
    s = make_session()  # mac AA:BB:CC:DD:EE:FF
    s.ctrl.client = types.SimpleNamespace(is_connected=True)
    import telink_daemon
    orig = telink_daemon._mesh_src_to_mac
    telink_daemon._mesh_src_to_mac = lambda src: "AA:BB:CC:DD:EE:FF" if src == 6 else None
    try:
        # mesh STATUS, len != 20, raw header carries src=6
        plain = bytes([0xDB, 0x00, 0x40, 0x00, 0x40, 70])
        raw = bytes([0, 0, 0, 6, 0]) + bytes(10)
        s.note_plain(plain, raw)
        assert s.state_cache["brightness"] == 70
        assert s.state_cache["on"] is True
        # unknown src -> ignored, own cache untouched
        before = dict(s.state_cache)
        telink_daemon._mesh_src_to_mac = lambda src: None
        s.note_plain(bytes([0xDB, 0, 0, 0, 0, 10]), bytes([0, 0, 0, 9, 0]) + bytes(10))
        assert s.state_cache["brightness"] == before["brightness"]
    finally:
        telink_daemon._mesh_src_to_mac = orig
        DaemonSession._sessions.clear()


def test_resolve_hci_adapter_usb_vidpid(monkeypatch):
    import config
    entries = {"hci0": {"product": "b05/190e/200"},
               "hci1": {"product": "bda/b85b/0"}}
    monkeypatch.setattr(config, "_sysfs_hci_entries", lambda: entries)
    # sysfs drops leading zeros (b05 vs 0b05) — must still match
    assert config.resolve_hci_adapter("usb:0b05:190e") == "hci0"
    assert config.resolve_hci_adapter("usb:0B05:190E") == "hci0"
    assert config.resolve_hci_adapter("hci1") == "hci1"
    assert config.resolve_hci_adapter("") is None
    assert config.resolve_hci_adapter("usb:ffff:ffff") is None
    # MAC form needs address fields (absent here -> None, no crash)
    assert config.resolve_hci_adapter("AA:BB:CC:DD:EE:FF") is None


class _FakeScanner:
    def __init__(self, *a, **k):
        pass
    async def __aenter__(self):
        return self
    async def __aexit__(self, *a):
        return False


def test_scanner_lookup_fresh_stale_prune():
    import asyncio
    from telink_ble import ScannerService
    svc = ScannerService()
    loop = asyncio.new_event_loop()
    now = loop.time()
    dev = object()
    svc._devices["AA:BB:CC:DD:EE:FF"] = (dev, now, -60)
    svc._devices["11:22:33:44:55:66"] = (dev, now - 500, -70)  # beyond max_age
    assert svc.lookup("aa:bb:cc:dd:ee:ff") is dev
    assert svc.lookup("11:22:33:44:55:66") is None       # stale -> miss...
    assert "11:22:33:44:55:66" in svc._devices           # ...but kept (prune is 600s)
    svc._devices["11:22:33:44:55:66"] = (dev, now - 700, -70)
    assert svc.lookup("00:00:00:00:00:00") is None
    assert "11:22:33:44:55:66" not in svc._devices       # pruned past 600s
    assert svc.lookup("00:00:00:00:00:00") is None
    loop.close()


def test_scanner_wait_for_hits_table_and_times_out():
    import asyncio
    from telink_ble import ScannerService
    svc = ScannerService()
    dev = object()
    async def go():
        svc._devices["AA:BB:CC:DD:EE:FF"] = (dev, asyncio.get_event_loop().time(), -60)
        got = await svc.wait_for("aa:bb:cc:dd:ee:ff", timeout=5.0)
        assert got is dev
        miss = await svc.wait_for("00:00:00:00:00:00", timeout=0.3)
        assert miss is None
    asyncio.run(go())


def test_scanner_start_idempotent(monkeypatch):
    import asyncio
    import telink_ble
    monkeypatch.setattr(telink_ble, "BleakScanner", _FakeScanner)
    from telink_ble import ScannerService
    async def go():
        svc = ScannerService()
        await svc.start()
        first = svc._task
        assert svc.running
        await svc.start()  # second start must not spawn another task
        assert svc._task is first
        await svc.stop()
        assert not svc.running
    asyncio.run(go())
