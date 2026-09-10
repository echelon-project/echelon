"""OPEN-0109: `workcycle incident resolve <INC-id> --by <item>` — the missing close verb.

INC-0001 (gamma-support) was resolved by OPEN-0015 but the room kept counting it unresolved because
the only door was a board row. A resolve is an act: record stamped, ONE receipt (ref = id,
so a re-run dedupes), resume counts only unresolved, a second resolve is refused."""
import json

import pytest

from echelon_engine import workcycle as wc


@pytest.fixture
def room(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    r = wc.init(root, estate="x", scope="s")
    monkeypatch.setattr(wc, "_board_post", lambda text: posted.append(text))
    return r


posted: list[str] = []


def _rec(room, inc):
    return json.loads((room / "incidents" / f"{inc}.json").read_text(encoding="utf-8"))


def test_resolve_stamps_receipts_and_counts(room):
    posted.clear()
    a = wc.incident(room, "live bot lied")
    b = wc.incident(room, "second")
    assert "INCIDENTS 2 unresolved" in wc.resume_brief(room)
    assert wc.status(room)["incidents_unresolved"] == 2

    res = wc.incident_resolve(room, a, by="OPEN-0015", note="gate #4175", origin="seat-1")
    assert res["ok"] and res["unresolved"] == 1
    d = _rec(room, a)
    assert d["closed"] and d["resolved"]["by"] == "OPEN-0015"
    assert d["resolved"]["note"] == "gate #4175" and d["resolved"]["origin"] == "seat-1"
    # the record for b is untouched
    assert _rec(room, b)["closed"] is None

    # ONE receipt, kind incident_resolved, ref = the id
    rs = [r for r in wc.receipts(room) if r["kind"] == "incident_resolved"]
    assert len(rs) == 1 and rs[0]["ref"] == a and "OPEN-0015" in rs[0]["text"]

    # resume / status / governor-style readers see one unresolved, total still two
    assert "INCIDENTS 1 unresolved" in wc.resume_brief(room)
    st = wc.status(room)
    assert st["incidents"] == 2 and st["incidents_unresolved"] == 1
    assert [x["id"] for x in wc.unresolved_incidents(room)] == [b]

    # board row posted once, names the id and the resolver
    assert len(posted) == 1 and a in posted[0] and "OPEN-0015" in posted[0]


def test_resolve_refuses_missing_twice_and_no_by(room):
    a = wc.incident(room, "boom")
    with pytest.raises(wc.IncidentRefused):
        wc.incident_resolve(room, "INC-9999", by="OPEN-1")
    with pytest.raises(wc.IncidentRefused):
        wc.incident_resolve(room, a, by="   ")
    with pytest.raises(wc.IncidentRefused):
        wc.incident_resolve(room, "OPEN-0001", by="x")
    wc.incident_resolve(room, a, by="OPEN-1", board=False)
    with pytest.raises(wc.IncidentRefused, match="already resolved by OPEN-1"):
        wc.incident_resolve(room, a, by="OPEN-2")
    # exactly one receipt survives the refused second attempt
    assert sum(1 for r in wc.receipts(room) if r["kind"] == "incident_resolved") == 1


def test_cli_resolve(room, monkeypatch, capsys):
    monkeypatch.chdir(room.parent)
    assert wc.main(["incident", "cli incident"]) == 0
    assert wc.main(["incident", "resolve"]) == 2                      # no id
    assert wc.main(["incident", "resolve", "INC-0001"]) == 1           # no --by
    assert wc.main(["incident", "resolve", "INC-0001", "--by", "OPEN-0015",
                    "--note", "gate #4175", "--no-board"]) == 0
    out = capsys.readouterr().out
    assert "INC-0001 resolved by OPEN-0015" in out and "0 unresolved left" in out
    assert wc.main(["incident", "resolve", "INC-0001", "--by", "OPEN-0016", "--no-board"]) == 1
    assert "REFUSED" in capsys.readouterr().out
    assert _rec(room, "INC-0001")["resolved"]["by"] == "OPEN-0015"
