"""Tests for echelon_engine/contracts.py — tmp rooms only; never-raise paths
(load/applicable/receipt/doctor/main) PROVEN with broken drawers, not read."""
import json
from pathlib import Path

import pytest

from echelon_engine import contracts as c
from echelon_engine import workcycle as wc


def _make_room(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    return root, wc.init(root, estate="acme", scope="s")


def _write(room, rel, data):
    p = room / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def _err(capsys):
    return capsys.readouterr().err.strip().splitlines()


def _law(title, severity="error", witness="probe_standard.x", params=None, **extra):
    return {"title": title, "statement": "stmt", "severity": severity,
            "params": params or {}, "witness": witness, "source": "src", **extra}


# the four PROPOSAL-GATE ids must exist on any kind:ui contract (SPEC §2.1)
UI = {"v": 1, "id": "UI", "kind": "ui", "title": "os_client page laws",
      "scope_globs": ["os_client/**"],
      "laws": {"UI-TOKENS": _law("tokens", witness="probe_standard.tokens"),
               "UI-MOUNT-SCOPED-IDS": _law("scoped ids",
                                           witness="probe_standard.mount_scoped_ids"),
               "PAGE-RECEIPT-SHAPE": _law("receipt shape", "warning", "test:receipt"),
               "PAGE-RED-BUDGET": _law("red budget", params={"max_red": 3},
                                       witness="probe_standard.red_budget_and_hero_count")}}

CMP = {"v": 1, "id": "CMP-ov00", "kind": "component", "name": "ov00",
       "title": "overlay/modal", "registry": "ov00", "slots": {}, "variants": [],
       "events": ["open", "close"], "a11y": {"hidden_at_rest": True},
       "responsive": {"modes": ["preserve"]},
       "laws": {"OVERLAY-CONTENT-INSIDE-DIALOG":
                _law("overlay law", witness="probe_standard.overlay_content_inside",
                     source="buku_stok.js:642-716")}}

@pytest.mark.parametrize("doc,marker", [
    ({"v": 99, "id": "UI"}, "unsupported"),
    ({"v": 1, "kind": "ui", "laws": {}}, "missing id"),
    ({"id": "UI", "kind": "ui"}, "missing v"),
])
def test_load_skips_one_line_each(tmp_path, capsys, doc, marker):
    _, room = _make_room(tmp_path)
    _write(room, "contracts/ui.json", doc)
    assert c.load(room) == {}
    lines = _err(capsys)
    assert len(lines) == 1 and marker in lines[0]


def test_load_skips_bad_json(tmp_path, capsys, monkeypatch):
    root, room = _make_room(tmp_path)
    (room / "contracts" / "ui.json").write_text("{not json", encoding="utf-8")
    assert c.load(room) == {}
    lines = _err(capsys)
    assert len(lines) == 1 and "bad json" in lines[0]
    # the same broken drawer degrades in every door: applicable [], main never raises
    assert c.applicable(room, None, ["a.py"]) == []
    monkeypatch.chdir(root)
    assert c.main(["list"]) == 0 and c.main(["show", "X"]) == 1


def test_load_duplicate_id_first_wins(tmp_path, capsys):
    _, room = _make_room(tmp_path)
    _write(room, "contracts/ui.json", {**UI, "title": "A"})
    _write(room, "contracts/ui2.json", {**UI, "title": "B"})
    got = c.load(room)
    assert list(got) == ["UI"] and got["UI"]["title"] == "A"
    lines = _err(capsys)
    assert len(lines) == 1 and "duplicate id" in lines[0]



def test_applicable_globs_match_and_miss(tmp_path):
    _, room = _make_room(tmp_path)
    _write(room, "contracts/ui.json", UI)
    _write(room, "contracts/api.json", {"v": 1, "id": "API", "kind": "api"})
    assert c.applicable(room, None, ["os_client/pages/uang/uang.js"]) == ["API", "UI"]
    assert c.applicable(room, None, ["elsewhere/x.py"]) == ["API"]
    # Windows-style backslash touch is normalized to POSIX before fnmatch
    assert c.applicable(room, None, [r"os_client\pages\uang\uang.js"]) == ["API", "UI"]
    # kind narrows nothing yet (2d) — accepted and ignored
    assert c.applicable(room, "api", ["os_client/pages/uang/uang.js"]) == \
        c.applicable(room, None, ["os_client/pages/uang/uang.js"])
    # a contract with no scope_globs applies to every touch, even none
    assert c.applicable(room, None, []) == ["API"]
    assert c.applicable(room, None, ["any/where.py"]) == ["API"]


def test_laws_and_component_by_id_and_name(tmp_path):
    _, room = _make_room(tmp_path)
    _write(room, "contracts/ui.json", UI)
    _write(room, "contracts/components/ov00.json", CMP)
    assert set(c.laws(room, "UI")) == {"UI-TOKENS", "UI-MOUNT-SCOPED-IDS",
                                       "PAGE-RECEIPT-SHAPE", "PAGE-RED-BUDGET"}
    assert c.laws(room, "NOPE") == {}
    assert c.component(room, "ov00")["id"] == "CMP-ov00"
    assert c.component(room, "CMP-ov00")["name"] == "ov00"
    assert c.component(room, "nope") is None
    # the same valid drawer passes doctor on every row
    rows = c.doctor(room)
    assert rows and all(r["ok"] for r in rows) and all(r["row"] == 9 for r in rows)



def test_receipt_writes_latest_and_history_and_roundtrips(tmp_path):
    _, room = _make_room(tmp_path)
    findings = [{"id": "PAGE-RED-BUDGET", "severity": "error", "detail": "red=5"}]
    hist = c.receipt(room, gate="probe_standard", subject="belanja", findings=findings,
                     checks={"red": 5})
    assert hist.exists() and hist.parent == room / "verification" / "history"
    latest = json.loads((room / "verification" / "latest.json").read_text(encoding="utf-8"))
    assert latest["v"] == 1 and latest["ok"] is False and latest["findings"] == findings
    assert latest["checks"] == {"red": 5} and latest["gate"] == "probe_standard"
    assert wc._verify_violations(room) == 1  # the receipt reader counts findings[]
    # a clean receipt overwrites latest (last gate wins), history is append-only
    c.receipt(room, gate="g", subject="s", findings=[])
    latest = json.loads((room / "verification" / "latest.json").read_text(encoding="utf-8"))
    assert latest["ok"] is True and wc._verify_violations(room) == 0
    assert len(list((room / "verification" / "history").glob("*.json"))) == 2


def test_receipt_unwritable_dir_does_not_raise(tmp_path, capsys):
    _, room = _make_room(tmp_path)
    (room / "verification").rmdir()  # replace the dir with a FILE — mkdir must fail
    (room / "verification").write_text("a file, not a dir", encoding="utf-8")
    hist = c.receipt(room, gate="g", subject="s", findings=[])
    assert isinstance(hist, Path) and "receipt write failed" in _err(capsys)[0]



@pytest.mark.parametrize("remove_dir", [False, True])
def test_doctor_empty_or_missing_drawer_not_loaded(tmp_path, remove_dir):
    _, room = _make_room(tmp_path)
    if remove_dir:
        (room / "contracts").rmdir()
    assert c.applicable(room, None, ["a.py"]) == []  # never raises on a bare drawer
    assert c.doctor(room) == [{"row": 9, "check": "drawer present", "ok": False,
                               "evidence": "CONTRACTS-NOT-LOADED"}]


def test_doctor_ui_missing_proposal_gate_id(tmp_path):
    _, room = _make_room(tmp_path)
    _write(room, "contracts/ui.json", {**UI, "laws": {k: v for k, v in UI["laws"].items()
                                                     if k != "UI-TOKENS"}})
    bad = [r for r in c.doctor(room) if not r["ok"]]
    assert len(bad) == 1 and "UI-TOKENS" in bad[0]["evidence"]


def test_doctor_bad_severity_and_witness(tmp_path):
    _, room = _make_room(tmp_path)
    laws = {k: dict(v) for k, v in UI["laws"].items()}
    laws["PAGE-RED-BUDGET"]["severity"] = "fatal"
    laws["UI-TOKENS"]["witness"] = ""
    _write(room, "contracts/ui.json", {**UI, "laws": laws})
    bad = [r for r in c.doctor(room) if not r["ok"]]
    assert any("fatal" in r["evidence"] for r in bad)
    assert any("no witness" in r["evidence"] for r in bad)



def test_main_list_and_show(tmp_path, monkeypatch, capsys):
    root, room = _make_room(tmp_path)
    _write(room, "contracts/ui.json", UI)
    monkeypatch.chdir(root)
    assert c.main(["list"]) == 0
    assert "UI  ui  os_client page laws" in capsys.readouterr().out
    assert c.main(["show", "UI"]) == 0
    assert json.loads(capsys.readouterr().out)["id"] == "UI"
    assert c.main(["show", "NOPE"]) == 1
    assert "CONTRACT-CLAIMED-NOT-FOUND" in capsys.readouterr().out
    assert c.main(["applicable", "--touch", "os_client/pages/uang/uang.js"]) == 0
    assert json.loads(capsys.readouterr().out) == ["UI"]


def test_main_doctor_exit_codes_and_no_room(tmp_path, monkeypatch, capsys):
    root, room = _make_room(tmp_path)
    monkeypatch.chdir(root)
    assert c.main(["doctor"]) == 1  # empty drawer -> CONTRACTS-NOT-LOADED
    assert "CONTRACTS-NOT-LOADED" in capsys.readouterr().out
    _write(room, "contracts/ui.json", UI)
    assert c.main(["doctor"]) == 0  # valid drawer -> all rows ok
    nowhere = tmp_path / "nowhere"
    nowhere.mkdir()
    monkeypatch.chdir(nowhere)
    assert c.main(["list"]) == 1
    assert "ROOM unavailable" in capsys.readouterr().out


# RULINGS A1: dual-keyed drawer file — drawer envelope (v/id/kind/scope_globs/
# laws) authoritative, registry API-shape fields nested under "api".
API = {"v": 1, "id": "API", "kind": "api", "title": "engine wire",
       "scope_globs": ["app/**/*.py"], "source": "test",
       "laws": {"API-READ-ONLY": _law("read-only api", witness="code")},
       "api": {"version": 1, "exact": True, "read_only": True,
               "scope_globs": ["app/**/*.py"], "base_path": "/api",
               "collection_verbs": ["GET"], "member_verbs": ["GET"],
               "response_envelope": "none", "pagination_style": "none",
               "id_style": "opaque_string", "error_required_paths": ["/error"],
               "auth_default": "required"}}


def test_doctor_api_required_fields_present(tmp_path):
    _, room = _make_room(tmp_path)
    _write(room, "contracts/api.json", API)
    rows = [r for r in c.doctor(room) if "api.json fields" in r["check"]]
    assert len(rows) == 1 and rows[0]["ok"] and "all required" in rows[0]["evidence"]


def test_doctor_api_missing_field_reported(tmp_path):
    _, room = _make_room(tmp_path)
    bad = json.loads(json.dumps(API))
    del bad["api"]["read_only"]
    _write(room, "contracts/api.json", bad)
    rows = [r for r in c.doctor(room) if "api.json fields" in r["check"]]
    assert len(rows) == 1 and not rows[0]["ok"] and "read_only" in rows[0]["evidence"]


def test_doctor_api_on_disk_but_not_loaded_fails(tmp_path):
    """RULINGS A1 CT-A11: a file on disk that LOOKS like a kind:api contract
    but was dropped by load() (bad v/missing id) must FAIL a row, not merely
    be absent from the row list — the fail-open shape ac057ab caught."""
    _, room = _make_room(tmp_path)
    _write(room, "contracts/api.json", {"kind": "api", "api": {}})   # no v, no id
    rows = [r for r in c.doctor(room) if "api contract loads" in r["check"]]
    assert len(rows) == 1 and not rows[0]["ok"]


def test_applicable_string_scope_globs_is_skipped_not_fail_open(tmp_path, capsys):
    """GATE-CAUGHT 2026-08-27: a string `scope_globs` iterated per character,
    and its `*` chars fnmatch'd EVERY path — a malformed contract silently
    claimed the whole repo. Must skip with one stderr line instead."""
    from echelon_engine import contracts as C
    room = tmp_path / ".echelon"
    (room / "contracts").mkdir(parents=True)
    (room / "contracts" / "ui.json").write_text(
        '{"v": 1, "id": "UI", "kind": "ui", "scope_globs": "os_client/**", "laws": {}}',
        encoding="utf-8")
    assert C.applicable(room, None, ["other/x.js"]) == []
    assert C.applicable(room, None, ["os_client/x.js"]) == []
    assert capsys.readouterr().err.count("scope_globs must be a list") == 2



def test_receipt_ok_is_error_severity_not_any_finding(tmp_path):
    """Gate r1 V8b M-2 / INC-0003: an acked WARNING must not stamp a passing bind ok:false."""
    import json
    from echelon_engine import contracts
    room = tmp_path / ".echelon"; room.mkdir()
    p = contracts.receipt(room, gate="propose-bind", subject="x",
                          findings=[{"rule": "REF-DECLARED-NOT-USED", "severity": "warning"}])
    assert json.loads(p.read_text(encoding="utf-8"))["ok"] is True
    p = contracts.receipt(room, gate="propose-bind", subject="y",
                          findings=[{"rule": "DRIFT-FILE", "severity": "error"}])
    assert json.loads(p.read_text(encoding="utf-8"))["ok"] is False
