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
    head = bytes(7) + bytes([0xDB]) + bytes(2)          # pkt[7] = 0xDB, p at pkt[10]
    payload = bytes([0x10, 0, 0, ct_hw, 0, on, bri,
                     rgb[0], rgb[1], rgb[2]]) + bytes(10)
    return head + payload


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
