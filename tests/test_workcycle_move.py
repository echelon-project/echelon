"""Tests for `workcycle move` — re-home an item/ruling/note between rooms (R-0154 slice 1).

tmp_path rooms only, no network, no real bank/registry. Item + note moves use
room PATHS (room_dir accepts a Path, so the ~/.echelon registry is never read).
Ruling move injects its two board calls, so no server is needed.
"""
import json

import pytest

from echelon_engine import session_state
from echelon_engine import workcycle as wc


class LedgerStub:
    def __init__(self):
        self.calls = []

    def checkpoint(self, summary="", **kw):
        self.calls.append(summary)
        return {"summary": summary}

    def files_touched(self, n=20):
        return []

    def recent_decisions(self, n=5):
        return []


@pytest.fixture
def stub(monkeypatch, tmp_path):
    s = LedgerStub()
    monkeypatch.setattr(session_state, "open_ledger", lambda scope, **kw: s)
    # isolate the room registry to a tmp home so a cross-room move can resolve
    # the target room by NAME without touching the live ~/.echelon/rooms.json.
    monkeypatch.setenv("ECHELON_HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()
    return s


def _room(tmp_path, name):
    """Create AND register a tmp room so its NAME resolves (moves store the
    target as a registry name)."""
    root = tmp_path / name
    root.mkdir()
    e = wc.init(root, estate=name, scope=name)
    wc.register_room(root, name=name, scope=name)
    return e


# ── item move ────────────────────────────────────────────────────────────────

def test_move_item_renumbers_in_target_and_tombstones_source(tmp_path, stub):
    a = _room(tmp_path, "roomA")
    b = _room(tmp_path, "roomB")
    # target already has one open item, so the moved one must land at OPEN-0002
    wc.open_item(b, "pre-existing in B")
    src_id = wc.open_item(a, "the item to move")
    assert src_id == "OPEN-0001"

    res = wc.move_item(a, src_id, b, by="tester")
    assert res["ok"] and res["new_id"] == "OPEN-0002"
    assert res["moved_to"].endswith("@roomB")

    # target has the record under the NEW id, carrying the original text + provenance
    tgt = json.loads((b / "open" / "OPEN-0002.json").read_text(encoding="utf-8"))
    assert tgt["id"] == "OPEN-0002" and tgt["text"] == "the item to move"
    assert tgt["moved_from"].startswith("OPEN-0001@")

    # source is a tombstone, NOT deleted
    tomb = json.loads((a / "open" / "OPEN-0001.json").read_text(encoding="utf-8"))
    assert wc.is_tombstone(tomb)
    assert tomb["moved_to"] == "OPEN-0002@roomB" and tomb["by"] == "tester"


def test_move_item_receipts_both_rooms(tmp_path, stub):
    a = _room(tmp_path, "roomA")
    b = _room(tmp_path, "roomB")
    src_id = wc.open_item(a, "x")
    wc.move_item(a, src_id, b, by="tester")
    a_kinds = [r["kind"] for r in wc.receipts(a)]
    b_kinds = [r["kind"] for r in wc.receipts(b)]
    assert "route" in a_kinds and "route" in b_kinds


def test_move_item_drops_claim_on_source(tmp_path, stub):
    a = _room(tmp_path, "roomA")
    b = _room(tmp_path, "roomB")
    src_id = wc.open_item(a, "x")
    # simulate a live claim on the source id
    (a / "claims.json").write_text(json.dumps({src_id: {"session": "s1"}}), encoding="utf-8")
    wc.move_item(a, src_id, b, by="tester")
    claims = json.loads((a / "claims.json").read_text(encoding="utf-8"))
    assert src_id not in claims


def test_move_item_idempotent_second_move_is_noop(tmp_path, stub):
    a = _room(tmp_path, "roomA")
    b = _room(tmp_path, "roomB")
    src_id = wc.open_item(a, "x")
    wc.move_item(a, src_id, b, by="tester")
    again = wc.move_item(a, src_id, b, by="tester")
    assert again["already"] is True
    assert again["moved_to"] == "OPEN-0002@roomB" or again["moved_to"].endswith("@roomB")


def test_move_item_same_room_refused(tmp_path, stub):
    a = _room(tmp_path, "roomA")
    src_id = wc.open_item(a, "x")
    with pytest.raises(wc.MoveRefused):
        wc.move_item(a, src_id, a, by="tester")


def test_move_item_unknown_id_refused(tmp_path, stub):
    a = _room(tmp_path, "roomA")
    b = _room(tmp_path, "roomB")
    with pytest.raises(wc.MoveRefused):
        wc.move_item(a, "OPEN-0099", b, by="tester")


# ── note move (inbox NOTE-nnnn) ──────────────────────────────────────────────

def test_move_note_rides_to_target_inbox(tmp_path, stub):
    a = _room(tmp_path, "roomA")
    b = _room(tmp_path, "roomB")
    (a / "inbox").mkdir(exist_ok=True)
    wc._write_json(a / "inbox" / "NOTE-0001.json",
                   {"v": 1, "id": "NOTE-0001", "from": "owner", "text": "carry me",
                    "ruling": "R-0007"})
    res = wc.move_item(a, "NOTE-0001", b, by="tester")
    assert res["ok"] and res["new_id"] == "NOTE-0001"  # target inbox empty -> keeps 0001
    tgt = json.loads((b / "inbox" / "NOTE-0001.json").read_text(encoding="utf-8"))
    assert tgt["text"] == "carry me" and tgt["ruling"] == "R-0007"
    tomb = json.loads((a / "inbox" / "NOTE-0001.json").read_text(encoding="utf-8"))
    assert wc.is_tombstone(tomb)


# ── alias resolution ─────────────────────────────────────────────────────────

def test_resolve_alias_follows_tombstone_across_rooms(tmp_path, stub):
    a = _room(tmp_path, "roomA")
    b = _room(tmp_path, "roomB")
    src_id = wc.open_item(a, "follow me")
    wc.move_item(a, src_id, b, by="tester")
    r = wc.resolve_alias(a, src_id)
    assert r["record"] is not None and not r.get("dangling")
    assert r["record"]["id"] == "OPEN-0001" and r["record"]["text"] == "follow me"
    assert r["hops"] == 1


def test_resolve_alias_live_record_is_itself(tmp_path, stub):
    a = _room(tmp_path, "roomA")
    src_id = wc.open_item(a, "live")
    r = wc.resolve_alias(a, src_id)
    assert r["hops"] == 0 and r["record"]["text"] == "live"


def test_resolve_alias_dangling_does_not_raise(tmp_path, stub):
    a = _room(tmp_path, "roomA")
    wc._write_json(a / "open" / "OPEN-0001.json",
                   {"v": 1, "id": "OPEN-0001", "moved_to": "OPEN-0007@nowhere",
                    "ts": "t", "by": "x"})
    r = wc.resolve_alias(a, "OPEN-0001")
    assert r.get("dangling") is True  # degrades, never crashes


def test_resolve_alias_two_hops(tmp_path, stub):
    a = _room(tmp_path, "roomA")
    b = _room(tmp_path, "roomB")
    c = _room(tmp_path, "roomC")
    src = wc.open_item(a, "hop twice")
    r1 = wc.move_item(a, src, b, by="t")     # A OPEN-0001 -> B OPEN-0001
    r2 = wc.move_item(b, r1["new_id"], c, by="t")  # B OPEN-0001 -> C OPEN-0001
    resolved = wc.resolve_alias(a, src)
    assert resolved["record"]["text"] == "hop twice" and resolved["hops"] == 2
    assert r2["new_id"] == "OPEN-0001"


# ── ruling move (injected board calls; no server) ────────────────────────────

def test_move_ruling_retags_via_injected_api(tmp_path):
    seen = {}

    def retag(rid, room):
        seen["retag"] = (rid, room)
        return {"id": rid, "room": room, "status": "open"}

    def say(rid, text, frm):
        seen["say"] = (rid, text, frm)
        return {"ok": True}

    res = wc.move_ruling("R-0042", "ledger-desk", by="tester",
                         retag_fn=retag, say_fn=say)
    assert res["ok"] and res["target"] == "ledger-desk"
    assert seen["retag"] == ("R-0042", "ledger-desk")
    assert seen["say"][0] == "R-0042" and "ledger-desk" in seen["say"][1]


def test_move_ruling_needs_retag_fn(tmp_path):
    with pytest.raises(wc.MoveRefused):
        wc.move_ruling("R-0042", "ledger-desk")


def test_move_ruling_rejects_non_r_id(tmp_path):
    with pytest.raises(wc.MoveRefused):
        wc.move_ruling("OPEN-0001", "x", retag_fn=lambda *a: {})
