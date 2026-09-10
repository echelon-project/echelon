"""Tests for echelon_engine/workcycle.py — the ROOM verb.

tmp_path rooms only, no network, no real bank: session_state.open_ledger is
monkeypatched to a stub so checkpoint's ledger leg never touches ~/.echelon.

The ROOM is the state dir: init(root) returns <root>/.echelon and every verb
takes that dir (the same one room_path resolves and doctor row 1 checks).
"""
import json
import sqlite3

import pytest

from echelon_engine import session_state
from echelon_engine import workcycle as wc


class LedgerStub:
    """Stand-in for SessionLedger: records calls, never touches the bank."""

    def __init__(self):
        self.checkpoint_calls = []
        self.touched = []
        self.decisions = []

    def checkpoint(self, summary="", **kw):
        self.checkpoint_calls.append({"summary": summary, **kw})
        return {"summary": summary}

    def files_touched(self, n=20):
        return [{"path": p} for p in self.touched]

    def recent_decisions(self, n=5):
        return [{"conclusion": d} for d in self.decisions]


@pytest.fixture
def stub(monkeypatch):
    s = LedgerStub()
    monkeypatch.setattr(session_state, "open_ledger", lambda scope, **kw: s)
    return s


# ── init ─────────────────────────────────────────────────────────────────────

def test_init_creates_drawers_and_gitignore(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    room = wc.init(root, estate="acme", scope="acme-ui")
    assert room == (root / ".echelon")
    for sub in ("journal", "open", "closed", "incidents", "proposals", "index",
                "contracts", "changes/active", "changes/completed", "verification"):
        assert (room / sub).is_dir(), sub
    rj = json.loads((room / "room.json").read_text(encoding="utf-8"))
    assert rj["v"] == 1
    assert rj["estate"] == "acme" and rj["type"] == "engine" and rj["scope"] == "acme-ui"
    cursor = json.loads((room / "cursor.json").read_text(encoding="utf-8"))
    assert cursor["v"] == 1 and cursor["stage"] == "init" and cursor["updated"]
    assert ".echelon/" in (root / ".gitignore").read_text(encoding="utf-8")


def test_init_scope_defaults_to_resolve_scope(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    monkeypatch.setattr(wc._resolve_scope_mod, "resolve_scope",
                        lambda cwd, **kw: "derived-scope")
    wc.init(root, estate="acme")
    rj = json.loads((root / ".echelon" / "room.json").read_text(encoding="utf-8"))
    assert rj["scope"] == "derived-scope"


def test_init_gitignore_appends_without_duplicate(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    (root / ".gitignore").write_text("venv/\n", encoding="utf-8")
    wc.init(root, estate="x", scope="s")
    gi = (root / ".gitignore").read_text(encoding="utf-8")
    assert "venv/" in gi
    assert gi.count(".echelon/") == 1


def test_init_raises_room_exists(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    wc.init(root, estate="x", scope="s")
    with pytest.raises(wc.RoomExists):
        wc.init(root, estate="x", scope="s")


# ── room_path ────────────────────────────────────────────────────────────────

def test_room_path_walks_up(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    wc.init(root, estate="x", scope="s")
    deep = root / "a" / "b" / "c"
    deep.mkdir(parents=True)
    assert wc.room_path(deep) == (root / ".echelon")
    nowhere = tmp_path / "nowhere"
    nowhere.mkdir()
    assert wc.room_path(nowhere) is None


def test_room_path_default_cwd(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    wc.init(root, estate="x", scope="s")
    monkeypatch.chdir(root)
    assert wc.room_path() == (root / ".echelon")


def test_room_path_ignores_bare_echelon_dir(tmp_path):
    # ~/.echelon (the bank) has no room.json — it must not be a room
    root = tmp_path / "banklike"
    root.mkdir()
    (root / ".echelon").mkdir()
    assert wc.room_path(root) is None


def test_room_path_honours_eos_layout_state(tmp_path):
    # .eos whose layout.state IS the room address (default .echelon/)
    root = tmp_path / "estate"
    root.mkdir()
    (root / "proj.eos").write_text(
        json.dumps({"layout": {"state": ".echelon/"}}), encoding="utf-8")
    wc.init(root, estate="x", scope="s")
    assert wc.room_path(root) == (root / ".echelon")
    # a non-default layout.state naming a built room dir
    root2 = tmp_path / "estate2"
    root2.mkdir()
    (root2 / "proj.eos").write_text(
        json.dumps({"layout": {"state": "state/"}}), encoding="utf-8")
    state = root2 / "state"
    state.mkdir()
    (state / "room.json").write_text(json.dumps({"v": 1}), encoding="utf-8")
    assert wc.room_path(root2) == state.resolve()
    # .eos present but the room not built there -> None (never creates)
    root3 = tmp_path / "estate3"
    root3.mkdir()
    (root3 / "proj.eos").write_text(
        json.dumps({"layout": {"state": ".echelon/"}}), encoding="utf-8")
    assert wc.room_path(root3) is None


# ── resume ───────────────────────────────────────────────────────────────────

def test_resume_brief_empty_room_exact_7_lines(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    room = wc.init(root, estate="acme", type_="ui", scope="acme-ui")
    assert wc.resume_brief(room) == "\n".join([
        "ROOM acme · type ui · scope acme-ui",
        "WHERE - · -/init · -",
        "NEXT -",
        "CHANGE -",
        "DO NOT CREATE -",
        "CONTRACTS -",
        "OPEN 0 items · INCIDENTS 0 unresolved · VERIFY 0 violations",
    ])


def test_resume_brief_populated_room(tmp_path, stub):
    root = tmp_path / "repo"
    root.mkdir()
    room = wc.init(root, estate="acme", scope="acme-ui")
    wc.checkpoint(room, summary="mid-fix", next_actions=["run tests", "commit"],
                  blockers=["waiting on owner"], files=["src/a.py", "src/b.py"])
    first = wc.open_item(room, "fix the door")
    wc.open_item(room, "second thing")
    wc.close_item(room, first, note="done")
    wc.incident(room, "test flake", evidence="run 42", fix="seed", receipt="INC-1")
    (room / "index" / "symbols.jsonl").write_text(
        json.dumps({"name": "fmtMoney", "canonical": True}) + "\n" +
        json.dumps({"name": "notCanon", "canonical": False}) + "\n",
        encoding="utf-8")
    (room / "contracts" / "ui.json").write_text(
        json.dumps({"v": 1, "id": "UI", "kind": "ui", "scope_globs": ["src/*.py"]}),
        encoding="utf-8")
    # R-0138: the WHERE line's checkpoint half is the receipt fold (open/close left
    # receipts), not the hand-written "mid-fix" — hands set NEXT, receipts move the room.
    brief = wc.resume_brief(room).splitlines()
    assert brief[1].startswith("WHERE - · -/init · ")
    assert "item_close OPEN-0001" in brief[1] and "item_open OPEN-0002" in brief[1]
    assert brief[:1] + brief[2:] == [
        "ROOM acme · type engine · scope acme-ui",
        "NEXT 1. run tests 2. commit",
        "CHANGE -",
        "DO NOT CREATE fmtMoney",
        "CONTRACTS UI",
        "OPEN 1 items · INCIDENTS 1 unresolved · VERIFY 0 violations",
    ]


def test_resume_brief_missing_room_never_raises(tmp_path):
    assert wc.resume_brief(tmp_path / "nope").startswith("ROOM unavailable")


def test_resume_full(tmp_path, stub):
    root = tmp_path / "repo"
    root.mkdir()
    room = wc.init(root, estate="acme", scope="s")
    wc.checkpoint(room, summary="cp1", files=["a.py"])
    lines = wc.resume_full(room).splitlines()
    assert lines[0].startswith("ROOM acme")
    assert "WORKING SET:" in lines
    assert "  a.py" in lines
    assert "JOURNAL:" in lines
    rec = json.loads(lines[-1])  # last journal entry verbatim, one line
    assert rec["kind"] == "checkpoint" and rec["summary"] == "cp1"


# ── status / journal ─────────────────────────────────────────────────────────

def test_status_counts(tmp_path, stub):
    root = tmp_path / "repo"
    root.mkdir()
    room = wc.init(root, estate="x", scope="s")
    st = wc.status(room)
    assert st["goal"] is None and st["stage"] == "init" and st["open"] == 0
    assert st["incidents"] == 0 and st["verify_violations"] == 0
    assert st["last_journal"] is None
    wc.checkpoint(room, summary="hello")
    st = wc.status(room)
    assert st["last_journal"]["kind"] == "checkpoint"
    wc.open_item(room, "thing")
    st = wc.status(room)
    assert st["open"] == 1
    # open_item is a cross-room act: it leaves a receipt and folds the cursor, so the
    # NEWEST journal line is the fold, not the earlier checkpoint (owner #932).
    assert st["last_journal"]["kind"] == "fold"


def test_journal_appends_and_returns_line(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    room = wc.init(root, estate="x", scope="s")
    rec = wc.journal(room, "note", {"hello": 1})
    assert rec["v"] == 1 and rec["kind"] == "note" and rec["hello"] == 1
    assert rec["ts"]
    jdir = room / "journal"
    assert len(list(jdir.glob("*.jsonl"))) == 1
    rec2 = wc.journal(room, "note", {"hello": 2})
    assert rec2["hello"] == 2
    lines = (jdir / sorted(jdir.glob("*.jsonl"))[0].name).read_text(
        encoding="utf-8").splitlines()
    assert len(lines) == 2


# ── checkpoint ───────────────────────────────────────────────────────────────

def test_checkpoint_upserts_cursor_journal_and_calls_ledger(tmp_path, stub):
    root = tmp_path / "repo"
    root.mkdir()
    room = wc.init(root, estate="acme", scope="acme-ui")
    stub.touched = ["x/1.py", "x/2.py"]
    stub.decisions = ["use the room"]
    res = wc.checkpoint(room, summary="first cp", completed=["a"], remaining=["b"],
                        next_actions=["n1"], blockers=["blk"], files=["f.py"])
    assert res["ok"] is True and res["kind"] == "checkpoint"
    assert res["summary"] == "first cp" and res["files"] == ["f.py"]
    cursor = json.loads((room / "cursor.json").read_text(encoding="utf-8"))
    assert cursor["v"] == 1
    assert cursor["checkpoint"] == "first cp"
    assert cursor["next_actions"] == ["n1"]
    assert cursor["blockers"] == ["blk"]
    assert cursor["working_set"] == ["f.py"]
    assert cursor["recent_decisions"] == ["use the room"]
    assert cursor["goal"] is None and cursor["stage"] == "init"
    assert cursor["updated"]
    jf = sorted((room / "journal").glob("*.jsonl"))[-1]
    line = json.loads(jf.read_text(encoding="utf-8").splitlines()[-1])
    assert line["kind"] == "checkpoint" and line["auto"] is False
    assert line["summary"] == "first cp" and line["files"] == ["f.py"]
    assert line["completed"] == ["a"] and line["remaining"] == ["b"]
    # the ledger stub was called with the summary + open_tasks=remaining
    assert stub.checkpoint_calls[-1]["summary"] == "first cp"
    assert stub.checkpoint_calls[-1]["open_tasks"] == ["b"]


def test_checkpoint_auto_files_default_to_ledger_touched(tmp_path, stub):
    root = tmp_path / "repo"
    root.mkdir()
    room = wc.init(root, estate="x", scope="s")
    stub.touched = ["x/1.py", "x/2.py"]
    res = wc.checkpoint(room, auto=True, summary="auto cp")
    assert res["ok"] is True
    cursor = json.loads((room / "cursor.json").read_text(encoding="utf-8"))
    assert cursor["working_set"] == ["x/1.py", "x/2.py"]


def test_checkpoint_auto_never_raises(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()

    class Boom:
        def checkpoint(self, *a, **k):
            raise RuntimeError("ledger down")

        def files_touched(self, n=20):
            raise RuntimeError("ledger down")

        def recent_decisions(self, n=5):
            raise RuntimeError("ledger down")

    monkeypatch.setattr(session_state, "open_ledger", lambda scope, **kw: Boom())
    wc.init(root, estate="acme", scope="s")
    room = root / ".echelon"
    res = wc.checkpoint(room, auto=True)  # no summary + broken ledger: never raises
    assert res["ok"] is True  # the room write still succeeded
    res2 = wc.checkpoint(tmp_path / "bad", auto=True)  # no room at all
    assert res2["ok"] is False and res2["err"]


def test_checkpoint_explicit_requires_summary(tmp_path, stub):
    root = tmp_path / "repo"
    root.mkdir()
    room = wc.init(root, estate="x", scope="s")
    with pytest.raises(ValueError):
        wc.checkpoint(room)  # not auto, no summary


# ── open / close / incident ──────────────────────────────────────────────────

def test_open_close_incident_ids_and_moves(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    room = wc.init(root, estate="x", scope="s")
    a = wc.open_item(room, "first")
    b = wc.open_item(room, "second")
    assert (a, b) == ("OPEN-0001", "OPEN-0002")
    rec = json.loads((room / "open" / "OPEN-0001.json").read_text(
        encoding="utf-8"))
    assert rec["v"] == 1 and rec["text"] == "first"
    assert rec["closed"] is None and rec["note"] is None
    closed = wc.close_item(room, a, note="done")
    assert closed["closed"] and closed["note"] == "done"
    assert not (room / "open" / "OPEN-0001.json").exists()
    assert (room / "closed" / "OPEN-0001.json").exists()
    # id space spans open + closed
    assert wc.open_item(room, "third") == "OPEN-0003"
    inc1 = wc.incident(room, "boom", evidence="e", fix="f", receipt="r")
    inc2 = wc.incident(room, "boom2")
    assert (inc1, inc2) == ("INC-0001", "INC-0002")
    irec = json.loads((room / "incidents" / "INC-0001.json").read_text(
        encoding="utf-8"))
    assert irec["evidence"] == "e" and irec["fix"] == "f" and irec["receipt"] == "r"
    with pytest.raises(FileNotFoundError):
        wc.close_item(room, "OPEN-9999")
    with pytest.raises(ValueError):
        wc.close_item(room, "OPEN-0001/../../x")


# ── doctor / version skew ────────────────────────────────────────────────────

def test_doctor_rows_1_2_5_12(tmp_path, stub):
    root = tmp_path / "repo"
    root.mkdir()
    room = wc.init(root, estate="acme", scope="s")
    rows = wc.doctor(room)
    assert [r["row"] for r in rows] == [1, 2, 5, 12, 9]
    for r in rows[:4]:
        assert r["ok"] is True, r
    # row 9 delegates to contracts.doctor: an empty drawer is not loaded
    assert rows[4]["row"] == 9 and rows[4]["ok"] is False
    assert rows[4]["evidence"] == "CONTRACTS-NOT-LOADED"
    # row 2 has TWO legs: one file writes cursor.json, and inside it exactly two
    # write sites (init creates, _checkpoint updates) — the guard that keeps fold()
    # from becoming a second updater (owner #932).
    assert rows[1]["evidence"].startswith("cursor.json writers: ['workcycle.py']")
    assert "in-module write sites: 2" in rows[1]["evidence"]
    # cursor restored after the v99 probe
    cursor = json.loads((room / "cursor.json").read_text(encoding="utf-8"))
    assert cursor["v"] == 1


def test_v99_cursor_skipped_with_one_line(tmp_path, capsys):
    root = tmp_path / "repo"
    root.mkdir()
    room = wc.init(root, estate="acme", scope="s")
    (room / "cursor.json").write_text(json.dumps({"v": 99, "goal": "x"}),
                                      encoding="utf-8")
    brief = wc.resume_brief(room)
    captured = capsys.readouterr()
    # stderr, never stdout: the reflex hook reads room files through this path and its
    # stdout is the harness protocol channel (gate M-1, 2026-08-29)
    assert captured.out == ""
    err = captured.err
    assert "v99" in err and "unsupported, skipped" in err
    assert len(err.splitlines()) == 1
    assert brief.splitlines()[0] == "ROOM acme · type engine · scope s"
    assert brief.splitlines()[1] == "WHERE - · -/- · -"  # cursor treated as missing


# ── CLI ──────────────────────────────────────────────────────────────────────

def test_cli_main_returns_0_for_each_verb(tmp_path, monkeypatch, stub, capsys):
    root = tmp_path / "repo"
    root.mkdir()
    assert wc.main(["init", "--root", str(root), "--estate", "acme",
                    "--scope", "s", "--type", "ui"]) == 0
    monkeypatch.chdir(root)
    assert wc.main(["status"]) == 0
    assert wc.main(["resume", "--brief"]) == 0
    assert wc.main(["resume"]) == 0
    assert wc.main(["checkpoint", "--summary", "cli cp",
                    "--next", "a", "--next", "b", "--file", "f.py"]) == 0
    assert wc.main(["journal", "note", '{"hello": 1}']) == 0
    assert wc.main(["open", "cli open"]) == 0
    assert wc.main(["close", "OPEN-0001", "--note", "bye"]) == 0
    assert wc.main(["incident", "cli incident"]) == 0
    # a healthy drawer keeps row 9 green (empty drawer -> doctor exits 1)
    gate_ids = ("UI-TOKENS", "UI-MOUNT-SCOPED-IDS", "PAGE-RECEIPT-SHAPE", "PAGE-RED-BUDGET")
    (root / ".echelon" / "contracts" / "ui.json").write_text(
        json.dumps({"v": 1, "id": "UI", "kind": "ui", "scope_globs": ["src/*.py"],
                    "laws": {lid: {"title": lid, "statement": "s", "severity": "error",
                                   "params": {}, "witness": "manual", "source": "spec"}
                             for lid in gate_ids}}),
        encoding="utf-8")
    assert wc.main(["doctor"]) == 0
    out = capsys.readouterr().out
    assert "OPEN-0001" in out
    # no room anywhere -> exit 1, never a crash
    monkeypatch.chdir(tmp_path)
    assert wc.main(["status"]) == 1


def test_auto_checkpoint_does_not_blank_prior_fields(tmp_path, monkeypatch):
    # orchestrator heal 2026-08-27: the Stop hook's auto checkpoint must UPSERT, never erase
    import sys, types
    from echelon_engine import workcycle as wc
    stub = types.ModuleType("echelon_engine.session_state")
    stub.open_ledger = lambda scope=None: None
    monkeypatch.setattr(wc, "session_state", stub, raising=False)
    room = wc.init(tmp_path, estate="t", type_="engine", scope="echelon")
    wc.checkpoint(room, summary="manual one", next_actions=["a", "b"], files=["x.py"])
    wc.checkpoint(room, auto=True)
    cur = json.loads((room / "cursor.json").read_text(encoding="utf-8"))
    assert cur["checkpoint"] == "manual one"
    assert cur["next_actions"] == ["a", "b"]
    assert cur["working_set"] == ["x.py"]


# ── auto-checkpoint dedupe + type / snooze (2026-08-29) ─────────────────────

def _journal_kinds(room):
    return [json.loads(l)["kind"] for p in sorted((room / "journal").glob("*.jsonl"))
            for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]


def test_auto_checkpoint_with_nothing_new_does_not_journal_again(tmp_path, stub):
    """129 identical auto lines buried the ECHELON journal (measured 2026-08-29): an auto
    checkpoint that repeats the last summary+files is not an event."""
    root = tmp_path / "repo"
    root.mkdir()
    room = wc.init(root, estate="acme", scope="acme")
    wc.checkpoint(room, summary="real work", files=["a.py"])
    r = wc.checkpoint(room, auto=True)
    assert r["ok"] and r.get("deduped") is True
    assert _journal_kinds(room).count("checkpoint") == 1
    # a changed working set IS an event
    stub.touched = ["b.py"]
    r = wc.checkpoint(room, auto=True, files=["b.py"])
    assert not r.get("deduped")
    assert _journal_kinds(room).count("checkpoint") == 2
    # so is a manual repeat (explicit calls are never deduped)
    wc.checkpoint(room, summary="real work", files=["b.py"])
    assert _journal_kinds(room).count("checkpoint") == 3
    # and so is a moved cursor (next_actions / blockers) with the same summary (gate S-4)
    r = wc.checkpoint(room, auto=True, next_actions=["NEW"])
    assert not r.get("deduped")
    assert _journal_kinds(room).count("checkpoint") == 4
    r = wc.checkpoint(room, auto=True)
    assert r.get("deduped") is True


def test_set_type_and_snooze_are_journaled_and_expire(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    room = wc.init(root, estate="acme", scope="acme")
    assert wc.room_type(room) == "engine"
    wc.set_type(room, "console-app")
    assert wc.room_type(room) == "console-app"
    for bad in ("", None, "zzz-never", "tomorrow"):
        with pytest.raises(ValueError):
            wc.snooze(room, "x", bad)
    # a malformed or missing `until` on disk EXPIRES the entry — it restores a guard,
    # never removes one (gate M-2)
    (room / "snooze.json").write_text(json.dumps({"v": 1, "entries": [
        {"name": "forever"}, {"name": "garbage", "until": "zzz-never"},
        {"name": "naive-live", "until": "2999-01-01T00:00:00"}, "not-a-dict"]}), encoding="utf-8")
    assert [s["name"] for s in wc.snoozes(room)] == ["naive-live"]
    assert wc.unsnooze(room, "naive-live") == ["naive-live"]
    wc.snooze(room, "x", "2999-01-01T00:00:00+00:00", why="noisy on greps")
    wc.snooze(room, "x", "2999-06-01T00:00:00+00:00")  # replaces, never duplicates
    wc.snooze(room, "old", "2000-01-01T00:00:00+00:00")
    live = wc.snoozes(room)
    assert [s["name"] for s in live] == ["x"] and live[0]["until"].startswith("2999-06")
    assert wc.unsnooze(room, "nope") == []
    assert wc.unsnooze(room, None) == ["x"]
    assert wc.snoozes(room) == []
    assert _journal_kinds(room) == ["type"] + ["snooze"] * 5
    assert wc.room_type(None) == "" and wc.snoozes(None) == []


# ── registry + jump (spec S8 V1, ruling R8) ──────────────────────────────────

@pytest.fixture(autouse=True)
def _isolated_registry(tmp_path, monkeypatch):
    """Every room-registry write goes to a tmp substrate home — init no longer
    auto-registers (e035cfd), but `register` must never touch the real ~/.echelon/rooms.json."""
    monkeypatch.setenv("ECHELON_HOME", str(tmp_path / "home"))
    return tmp_path / "home"


def _init_registered(tmp_path, name="repo", estate="acme", scope="acme"):
    root = tmp_path / name
    root.mkdir()
    room = wc.init(root, estate=estate, scope=scope)
    wc.register_room(root)  # the explicit verb is the registry door
    return root, room


def test_init_does_not_auto_register(tmp_path, _isolated_registry):
    """spec S8 V1 deviation: `workcycle register` is the explicit registry door —
    init alone never writes ~/.echelon/rooms.json (a test/tmp room must not
    pollute the live registry through any init caller)."""
    root = tmp_path / "repo"
    root.mkdir()
    (root / ".git").mkdir()  # even a real-looking workspace is not registered
    wc.init(root, estate="acme", scope="acme")
    registry = _isolated_registry / "rooms.json"
    assert not registry.exists()  # init wrote nothing
    wc.register_room(root)        # the explicit verb registers
    data = json.loads(registry.read_text(encoding="utf-8"))
    assert data["v"] == 1
    assert data["rooms"]["acme"]["scope"] == "acme"


def test_register_room_round_trip_and_idempotent(tmp_path, _isolated_registry):
    root, room = _init_registered(tmp_path, name="est", estate="my-estate", scope="my-scope")
    # re-register (idempotent — same key) with an explicit binding scope
    entry = wc.register_room(root, scope="estate-binding")
    assert entry["scope"] == "estate-binding"
    assert wc.registered_rooms() == ["my-estate"]
    data = json.loads((_isolated_registry / "rooms.json").read_text(encoding="utf-8"))
    assert data["rooms"]["my-estate"]["scope"] == "estate-binding"


def test_register_room_requires_an_existing_room(tmp_path, _isolated_registry):
    with pytest.raises(FileNotFoundError):
        wc.register_room(tmp_path / "nope")


# ── estates (R-0154 slice 2, LAW 2) ──────────────────────────────────────────

def test_estate_assign_group_and_read(tmp_path, _isolated_registry):
    _init_registered(tmp_path, name="a", estate="a", scope="a")
    _init_registered(tmp_path, name="b", estate="b", scope="b")
    wc.set_estate("myco", "a", tag="myco")
    wc.set_estate("myco", "b")
    est = wc.estates()
    assert set(est["myco"]["members"]) == {"a", "b"}
    assert est["myco"]["tag"] == "myco"
    assert wc.estate_of("a") == "myco"


def test_estate_map_survives_a_rooms_only_write(tmp_path, _isolated_registry):
    # _save_registry preserves the estates map on any rooms-only write (LAW 2: never dropped).
    root, _ = _init_registered(tmp_path, name="a", estate="a", scope="a")
    wc.set_estate("myco", "a")
    wc.register_room(root, scope="rebind")   # a rooms-only write
    assert wc.estates()["myco"]["members"] == ["a"]
    data = json.loads((_isolated_registry / "rooms.json").read_text(encoding="utf-8"))
    assert data["estates"]["myco"]["rooms"] == ["a"]


def test_estate_membership_is_exclusive(tmp_path, _isolated_registry):
    _init_registered(tmp_path, name="a", estate="a", scope="a")
    wc.set_estate("one", "a")
    wc.set_estate("two", "a")   # re-home: drops from `one`
    est = wc.estates()
    assert est["one"]["members"] == []
    assert est["two"]["members"] == ["a"]


def test_estate_refuses_a_child_room_directly(tmp_path, _isolated_registry):
    _init_registered(tmp_path, name="a", estate="a", scope="a")
    # forge a child entry via the registry writer to simulate a promoted child
    rooms = wc._registry_entries()
    rooms["a/kid"] = {"path": str(tmp_path / "a" / ".echelon"), "estate": "a/kid",
                      "parent": "a", "project": "kid", "scope": "a", "type": "engine"}
    wc._save_registry(rooms)
    with pytest.raises(ValueError):
        wc.set_estate("myco", "a/kid")
    # but a child rides its parent's estate once the parent is assigned
    wc.set_estate("myco", "a")
    assert "a/kid" in wc.estates()["myco"]["members"]
    assert wc.estate_of("a/kid") == "myco"


def test_room_path_explicit_name_wins_from_roomless_cwd(tmp_path, _isolated_registry):
    root, room = _init_registered(tmp_path)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    assert wc.room_path(elsewhere, name="acme") == room
    # an explicit name that is not registered NEVER falls through to cwd
    assert wc.room_path(elsewhere, name="ghost") is None


def test_room_path_roomless_unrelated_cwd_is_no_room(tmp_path, _isolated_registry, monkeypatch):
    # R-0154 slice 2 LAW 1: a roomless cwd that is NOBODY's project resolves to NO room, even when
    # its scope matches a registered room — the old scope-fallback landed it in the home room
    # ([[an-unbounded-upward-walk-lands-in-the-home-room]]).
    _init_registered(tmp_path, scope="acme-scope")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.setattr(wc._resolve_scope_mod, "resolve_scope",
                        lambda cwd, **kw: "acme-scope")
    assert wc.room_path(elsewhere) is None


def test_room_path_binds_a_registered_project_root(tmp_path, _isolated_registry):
    # R-0154 slice 2 LAW 1: a roomless sub-dir that IS a registered project of a room binds to it.
    root, room = _init_registered(tmp_path, name="repo")
    proj = root / "company" / "widget"
    proj.mkdir(parents=True)
    wc.set_room_projects("acme", {"widget": {"root": "company/widget", "repo": True}})
    assert wc.room_path(proj / "src") == room


def test_room_path_explicit_scope_still_binds_by_scope(tmp_path, _isolated_registry, monkeypatch):
    # a CALLER that explicitly passes scope= means "the room owning this scope" — legacy leg 4b.
    _init_registered(tmp_path, scope="acme-scope")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    assert wc.room_path(elsewhere, scope="acme-scope") == (tmp_path / "repo" / ".echelon")


def test_room_path_ancestor_walk_beats_scope_match(tmp_path, _isolated_registry, monkeypatch):
    root, room = _init_registered(tmp_path, scope="acme-scope")
    deep = root / "a" / "b"
    deep.mkdir(parents=True)
    monkeypatch.setattr(wc._resolve_scope_mod, "resolve_scope",
                        lambda cwd, **kw: "acme-scope")
    assert wc.room_path(deep) == room  # the walk wins, not the registry


def test_room_path_none_and_registered_rooms_list(tmp_path, _isolated_registry, monkeypatch):
    _init_registered(tmp_path, name="r1", estate="alpha", scope="alpha")
    _init_registered(tmp_path, name="r2", estate="beta", scope="beta")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.setattr(wc._resolve_scope_mod, "resolve_scope",
                        lambda cwd, **kw: "noscope")
    assert wc.room_path(elsewhere) is None
    assert wc.registered_rooms() == ["alpha", "beta"]


def test_jump_prints_repo_root(tmp_path, _isolated_registry, capsys):
    root, room = _init_registered(tmp_path)
    assert wc.main(["jump", "acme"]) == 0
    assert capsys.readouterr().out.strip() == str(root)
    assert wc.main(["jump", "ghost"]) == 1
    assert "registered: acme" in capsys.readouterr().out


def test_status_and_resume_room_flag(tmp_path, _isolated_registry, monkeypatch, capsys):
    root, room = _init_registered(tmp_path)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    assert wc.main(["status", "--room", "acme"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["stage"] == "init"
    assert wc.main(["resume", "--brief", "--room", "acme"]) == 0
    assert capsys.readouterr().out.startswith("ROOM acme")


def test_room_path_roomless_git_repo_is_no_room(tmp_path, _isolated_registry, monkeypatch):
    """R-0154 slice 2 LAW 1: a roomless git repo that is NOBODY's project resolves to NO room,
    even when its scope matches a registered room — the old leg-4 scope-fallback landed an
    unrelated repo in the home room. legs 2/3 no longer have a scope-fallback to shadow."""
    repo = tmp_path / "repo"; wc.init(repo, estate="acme"); wc.register_room(str(repo), scope="acme-scope")
    elsewhere = tmp_path / "other-repo" / "src"; elsewhere.mkdir(parents=True)
    (tmp_path / "other-repo" / ".git").mkdir()
    monkeypatch.setattr(wc, "_resolve_scope", lambda *_a, **_k: "acme-scope")
    assert wc.room_path(elsewhere) is None


def test_register_preserves_first_registered_timestamp(tmp_path):
    """Gate r1 S8a S-1: `registered` is provenance — a re-register keeps the original stamp."""
    repo = tmp_path / "repo"; wc.init(repo, estate="acme")
    first = wc.register_room(str(repo), scope="a")["registered"]
    second = wc.register_room(str(repo), scope="b")
    assert second["registered"] == first and second["scope"] == "b"


def test_verdict_verb_writes_verdict_kind(tmp_path, capsys):
    """Spec S8 V5: `workcycle verdict <campaign> <text>` is the first-class verdict kind."""
    repo = tmp_path / "repo"; room = wc.init(repo, estate="acme")
    rec = wc.record_verdict(room, "pilot-x", "PASS: the pilot proved the shape")
    assert rec["kind"] == "verdict" and rec["campaign"] == "pilot-x"
    lines = [json.loads(l) for l in (room / "journal").glob("*.jsonl").__next__().read_text(encoding="utf-8").splitlines()]
    assert lines[-1]["kind"] == "verdict" and lines[-1]["text"].startswith("PASS")
    with pytest.raises(ValueError):
        wc.record_verdict(room, "", "no campaign")
    assert wc.main(["verdict", "--room", "nope-not-registered", "c", "t"]) == 1


# ── claims (R-0008 pilot: bank row + claims.json backpointer, ONE tx) ────────

def _claim_room(tmp_path):
    """A fresh room (estate acme) + tmp bank for the claim tests."""
    root = tmp_path / "repo"; root.mkdir()
    room = wc.init(root, estate="acme", scope="acme")
    return room, tmp_path / "bank.db"


def test_claim_writes_row_and_pointer(tmp_path):
    """claim(): one active bank row AND the claims.json backpointer, one tx."""
    room, bank = _claim_room(tmp_path)
    wid = wc.open_item(room, "the door")
    res = wc.claim(room, wid, session="alice", bank=bank)
    assert res == {"id": wid, "session": "alice", "ts": res["ts"],
                   "room": "acme", "held": True}
    conn = sqlite3.connect(bank)
    row = conn.execute("SELECT room, open_id, session, released FROM room_claims"
                       " WHERE released IS NULL").fetchone()
    conn.close()
    assert row == ("acme", wid, "alice", None)
    ptr = json.loads((room / "claims.json").read_text(encoding="utf-8"))
    assert ptr[wid]["session"] == "alice" and ptr[wid]["ts"] == res["ts"]


def test_claim_second_session_held(tmp_path):
    """A second session gets ClaimHeld(holder, ts) and the pointer stays alice's."""
    room, bank = _claim_room(tmp_path)
    wid = wc.open_item(room, "the door")
    res = wc.claim(room, wid, session="alice", bank=bank)
    with pytest.raises(wc.ClaimHeld) as ei:
        wc.claim(room, wid, session="bob", bank=bank)
    assert ei.value.holder == "alice" and ei.value.ts == res["ts"]
    ptr = json.loads((room / "claims.json").read_text(encoding="utf-8"))
    assert ptr[wid]["session"] == "alice"


def test_claim_same_session_idempotent(tmp_path):
    """Re-claiming as the SAME session returns the existing claim — still ONE row."""
    room, bank = _claim_room(tmp_path)
    wid = wc.open_item(room, "the door")
    first = wc.claim(room, wid, session="alice", bank=bank)
    again = wc.claim(room, wid, session="alice", bank=bank)
    assert again["ts"] == first["ts"] and again["held"] is True
    conn = sqlite3.connect(bank)
    n = conn.execute("SELECT COUNT(*) FROM room_claims").fetchone()[0]
    conn.close()
    assert n == 1


def test_release_clears_row_and_pointer(tmp_path):
    """release(): released=now on the row AND the claims.json key, in one tx."""
    room, bank = _claim_room(tmp_path)
    wid = wc.open_item(room, "the door")
    wc.claim(room, wid, session="alice", bank=bank)
    res = wc.release(room, wid, session="alice", bank=bank)
    assert res["id"] == wid and res["session"] == "alice" and res["held"] is True
    conn = sqlite3.connect(bank)
    active = conn.execute("SELECT COUNT(*) FROM room_claims"
                          " WHERE released IS NULL").fetchone()[0]
    conn.close()
    assert active == 0
    assert wc.claims(room) == {}
    # the item is claimable again by anyone
    assert wc.claim(room, wid, session="bob", bank=bank)["session"] == "bob"


def test_release_other_session_refused_force_ok(tmp_path):
    """release() by a non-holder is ClaimHeld; --force releases anyway."""
    room, bank = _claim_room(tmp_path)
    wid = wc.open_item(room, "the door")
    wc.claim(room, wid, session="alice", bank=bank)
    with pytest.raises(wc.ClaimHeld):
        wc.release(room, wid, session="bob", bank=bank)
    assert wc.claims(room)[wid]["session"] == "alice"      # nothing dropped
    res = wc.release(room, wid, session="bob", bank=bank, force=True)
    assert res["session"] == "alice" and res["held"] is True
    assert wc.claims(room) == {} and wc.release(room, wid, session="x", bank=bank)["held"] is False


def test_claim_closed_or_unknown_id_errors(tmp_path):
    """claim() on a closed/unknown/malformed id raises and writes NO pointer."""
    room, bank = _claim_room(tmp_path)
    wid = wc.open_item(room, "the door")
    wc.close_item(room, wid)
    for bad in (wid, "OPEN-9999"):
        with pytest.raises(FileNotFoundError):
            wc.claim(room, bad, session="alice", bank=bank)
    with pytest.raises(ValueError):
        wc.claim(room, "OPEN-x", session="alice", bank=bank)
    assert not (room / "claims.json").exists()
    assert not bank.exists()          # validation raises BEFORE the bank is ever touched


def test_close_item_drops_pointer(tmp_path):
    """close_item() drops the claims.json key (file truth) and, given a bank,
    releases the row force=True best-effort. VERIFY-BY-OTHER (2026-09-01): a
    claimed item refuses an unverified self-close; a verifier (or another
    session, or --solo) opens the door."""
    room, bank = _claim_room(tmp_path)
    wid = wc.open_item(room, "the door")
    wc.claim(room, wid, session="alice", bank=bank)
    with pytest.raises(wc.UnverifiedClose):
        wc.close_item(room, wid, note="done", bank=bank, session="alice")
    with pytest.raises(wc.UnverifiedClose):          # anonymous close is a self-close too
        wc.close_item(room, wid, note="done", bank=bank)
    closed = wc.close_item(room, wid, note="done", bank=bank,
                           session="alice", verified_by="bob-gate")
    assert closed["note"] == "done"
    assert closed["verified_by"] == "bob-gate"
    assert closed["closed_by"] == "alice"
    assert wc.claims(room) == {}                      # pointer dropped
    conn = sqlite3.connect(bank)
    released = conn.execute("SELECT released FROM room_claims"
                            " WHERE open_id=?", (wid,)).fetchone()[0]
    conn.close()
    assert released is not None                       # row released, stale only if it lingers


def test_claim_pid_stamped_and_annotated_alive(tmp_path):
    """A claim with pid= stamps the pid into claims.json; claims() annotates
    liveness for THIS live process (owner 2026-09-01: a claim is a mark, not a freeze)."""
    import os
    room, bank = _claim_room(tmp_path)
    wid = wc.open_item(room, "the door")
    res = wc.claim(room, wid, session="alice", bank=bank, pid=os.getpid())
    assert res["pid"] == os.getpid()
    ptr = json.loads((room / "claims.json").read_text(encoding="utf-8"))
    assert ptr[wid]["pid"] == os.getpid()
    assert wc.claims(room)[wid]["alive"] is True


def test_claim_held_carries_liveness_and_live_claim_never_stolen(tmp_path):
    """ClaimHeld exposes the holder's pid + alive; steal_dead NEVER steals a LIVE claim."""
    import os
    room, bank = _claim_room(tmp_path)
    wid = wc.open_item(room, "the door")
    wc.claim(room, wid, session="alice", bank=bank, pid=os.getpid())
    with pytest.raises(wc.ClaimHeld) as ei:
        wc.claim(room, wid, session="bob", bank=bank, pid=1234, steal_dead=True)
    assert ei.value.pid == os.getpid() and ei.value.alive is True
    ptr = json.loads((room / "claims.json").read_text(encoding="utf-8"))
    assert ptr[wid]["session"] == "alice"


def test_claim_steal_dead_takes_over_and_receipts(tmp_path):
    """A claim whose pid is verifiably dead is stolen ONLY with steal_dead=True; the
    old row is released with a stolen-dead note and the pointer flips to the thief."""
    import subprocess, sys as _sys
    room, bank = _claim_room(tmp_path)
    wid = wc.open_item(room, "the door")
    p = subprocess.Popen([_sys.executable, "-c", "pass"]); p.wait()
    dead = p.pid
    wc.claim(room, wid, session="alice", bank=bank, pid=dead)
    assert wc.claims(room)[wid]["alive"] is False
    with pytest.raises(wc.ClaimHeld):                 # without the flag: still refused
        wc.claim(room, wid, session="bob", bank=bank, pid=None)
    res = wc.claim(room, wid, session="bob", bank=bank, steal_dead=True)
    assert res["held"] is True and res["stole_dead_claim"]["session"] == "alice"
    ptr = json.loads((room / "claims.json").read_text(encoding="utf-8"))
    assert ptr[wid]["session"] == "bob"
    conn = sqlite3.connect(bank)
    rows_ = conn.execute("SELECT session, released FROM room_claims ORDER BY ts").fetchall()
    conn.close()
    assert any(s == "alice" and r and "stolen-dead by bob" in r for s, r in rows_)
    assert any(s == "bob" and r is None for s, r in rows_)


def test_claim_concurrent_falsifier(tmp_path):
    """R-0008 falsifier: two sessions claim the same id concurrently — EXACTLY
    one succeeds per round and claims.json names the winner. 20 rounds; any
    double-success is a DESIGN FAIL (an in-process lock would mask it)."""
    import threading
    room, bank = _claim_room(tmp_path)
    wid = wc.open_item(room, "the door")
    results = []

    def claimer(session):
        try:
            wc.claim(room, wid, session=session, bank=bank)
            results.append(("ok", session))
        except wc.ClaimHeld:
            results.append(("held", session))
        except wc.ClaimBusy:
            results.append(("busy", session))
        except BaseException as exc:
            results.append(("error", session, type(exc).__name__))

    for rnd in range(20):
        results.clear()
        threads = [threading.Thread(target=claimer, args=(s,))
                   for s in ("alice", "bob")]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert len(results) == 2, f"round {rnd}: terminal results {results}"
        assert not [r for r in results if r[0] == "error"], f"round {rnd}: {results}"
        winners = [s for kind, s in results if kind == "ok"]
        assert len(winners) == 1, f"round {rnd}: winners {winners} ({results})"
        assert sum(kind in {"held", "busy"} for kind, *_ in results) == 1
        ptr = json.loads((room / "claims.json").read_text(encoding="utf-8"))
        assert ptr[wid]["session"] == winners[0]     # the pointer names the winner
        wc.release(room, wid, session=winners[0], bank=bank)   # release between rounds


def test_claim_and_release_busy_writer_lock_are_classified_without_mutation(tmp_path, monkeypatch):
    """A real SQLite writer lock is an explicit unknown outcome, never ClaimHeld."""
    room, bank = _claim_room(tmp_path)
    wid = wc.open_item(room, "the door")
    wc.claim(room, wid, session="alice", bank=bank)
    pointer_before = (room / "claims.json").read_bytes()
    bank_before = bank.read_bytes()
    real_connect = sqlite3.connect

    def short_connect(*args, **kwargs):
        kwargs["timeout"] = 0.05
        return real_connect(*args, **kwargs)

    monkeypatch.setattr(wc.sqlite3, "connect", short_connect)
    for operation, action in (
        ("claim", lambda: wc.claim(room, wid, session="bob", bank=bank)),
        ("release", lambda: wc.release(room, wid, session="alice", bank=bank)),
    ):
        writer = real_connect(bank, timeout=0.05)
        try:
            writer.execute("BEGIN IMMEDIATE")
            with pytest.raises(wc.ClaimBusy) as caught:
                action()
            assert caught.value.operation == operation
            assert isinstance(caught.value.__cause__, sqlite3.OperationalError)
            assert caught.value.code == caught.value.__cause__.sqlite_errorcode
            assert not hasattr(caught.value, "holder")
            assert (room / "claims.json").read_bytes() == pointer_before
            assert bank.read_bytes() == bank_before
        finally:
            writer.rollback()
            writer.close()
    conn = real_connect(bank)
    try:
        assert conn.execute("SELECT session FROM room_claims WHERE released IS NULL").fetchall() == [("alice",)]
    finally:
        conn.close()


@pytest.mark.parametrize("operation", ["claim", "release"])
def test_claim_busy_does_not_translate_unrelated_operational_error(tmp_path, monkeypatch, operation):
    room, bank = _claim_room(tmp_path)
    wid = wc.open_item(room, "the door")

    class BrokenConnection:
        isolation_level = None
        in_transaction = False
        def execute(self, _sql, *_args):
            raise sqlite3.OperationalError("disk I/O error")
        def close(self):
            pass

    monkeypatch.setattr(wc.sqlite3, "connect", lambda *_args, **_kwargs: BrokenConnection())
    action = (lambda: wc.claim(room, wid, session="alice", bank=bank)) if operation == "claim" else \
             (lambda: wc.release(room, wid, session="alice", bank=bank))
    with pytest.raises(sqlite3.OperationalError, match="disk I/O error"):
        action()


@pytest.mark.parametrize("code", [sqlite3.SQLITE_LOCKED, sqlite3.SQLITE_BUSY | 0x100])
def test_claim_busy_classifies_locked_and_extended_busy_codes(code):
    class BusyConnection:
        def execute(self, _sql):
            exc = sqlite3.OperationalError("busy fixture")
            exc.sqlite_errorcode = code
            raise exc

    with pytest.raises(wc.ClaimBusy) as caught:
        wc._begin_claim_transaction(BusyConnection(), "claim")
    assert caught.value.code == code
    assert caught.value.__cause__.sqlite_errorcode == code


@pytest.mark.parametrize("operation", ["claim", "release"])
def test_claim_busy_does_not_translate_busy_after_begin(tmp_path, monkeypatch, operation):
    room, bank = _claim_room(tmp_path)
    wid = wc.open_item(room, "the door")

    class PostBeginBusyConnection:
        isolation_level = None
        in_transaction = True
        def __init__(self):
            self.error = sqlite3.OperationalError("post-begin busy")
            self.error.sqlite_errorcode = sqlite3.SQLITE_BUSY
        def execute(self, sql, *_args):
            if sql == "BEGIN IMMEDIATE":
                return None
            if sql == "ROLLBACK":
                self.in_transaction = False
                return None
            raise self.error
        def close(self):
            pass

    connection = PostBeginBusyConnection()
    monkeypatch.setattr(wc.sqlite3, "connect", lambda *_args, **_kwargs: connection)
    action = (lambda: wc.claim(room, wid, session="alice", bank=bank)) if operation == "claim" else \
             (lambda: wc.release(room, wid, session="alice", bank=bank))
    with pytest.raises(sqlite3.OperationalError) as caught:
        action()
    assert caught.value is connection.error
    assert connection.in_transaction is False


@pytest.mark.parametrize("action", ["claim", "release"])
def test_claim_cli_busy_is_actionable_and_nonzero(tmp_path, monkeypatch, capsys, action):
    room, bank = _claim_room(tmp_path)
    wid = wc.open_item(room, "the door")
    monkeypatch.chdir(room.parent)
    busy = wc.ClaimBusy(action, sqlite3.SQLITE_BUSY)
    monkeypatch.setattr(wc, action, lambda *_args, **_kwargs: (_ for _ in ()).throw(busy))
    args = [action, wid, "--session", "alice", "--bank", str(bank)]
    assert wc.main(args) == 1
    output = capsys.readouterr().out
    assert f"{action} acquisition busy" in output
    assert f"[sqlite code {sqlite3.SQLITE_BUSY}]" in output
    assert "no " + ("claim" if action == "claim" else "release") + " acknowledgement" in output
    assert "explicit new attempt" in output


def test_status_resume_claims_never_open_db(tmp_path, monkeypatch, capsys):
    """status()/resume show claims from claims.json only — boot NEVER opens the db."""
    room, bank = _claim_room(tmp_path)
    wid = wc.open_item(room, "the door")
    wc.claim(room, wid, session="alice", bank=bank)

    def boom(*a, **k):
        raise RuntimeError("the db was opened")
    monkeypatch.setattr(wc.sqlite3, "connect", boom)
    monkeypatch.chdir(room.parent)

    assert wc.status(room)["claims"] == {wid: "alice"}
    assert wc.main(["status"]) == 0
    assert json.loads(capsys.readouterr().out)["claims"] == {wid: "alice"}
    assert wc._resume_brief(room).splitlines()[-1].endswith("· CLAIMED 1")


def test_claim_cli_held_one_liner(tmp_path, capsys, monkeypatch):
    """CLI ClaimHeld -> exit 1 + the exact one-liner naming holder and ts."""
    room, bank = _claim_room(tmp_path)
    wid = wc.open_item(room, "the door")
    monkeypatch.chdir(room.parent)
    assert wc.main(["claim", wid, "--session", "alice", "--bank", str(bank)]) == 0
    assert json.loads(capsys.readouterr().out)["session"] == "alice"
    rc = wc.main(["claim", wid, "--session", "bob", "--bank", str(bank)])
    assert rc == 1
    assert capsys.readouterr().out.strip().startswith(f"{wid} held by alice since")
    rc = wc.main(["claims"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out[wid]["session"] == "alice"


def test_resume_brief_string_next_actions_is_one_item_not_per_character(tmp_path):
    """2026-08-30: jump.py wrote next_actions as a STRING; the banner iterated it per character
    ('NEXT 1. R 2. e 3. a ...' for hundreds of chars) - that letter-spelled blob tripped the
    Fable 5 safeguard on EVERY alpha-app session start. A string is one action, never 200."""
    root = tmp_path / "repo"; root.mkdir()
    room = wc.init(root, estate="acme", scope="acme-ui")
    cur = json.loads((room / "cursor.json").read_text(encoding="utf-8"))
    cur["next_actions"] = "Read the playbook first, then work the tail"
    (room / "cursor.json").write_text(json.dumps(cur), encoding="utf-8")
    brief = wc.resume_brief(room)
    next_line = [l for l in brief.splitlines() if l.startswith("NEXT ")][0]
    assert next_line == "NEXT 1. Read the playbook first, then work the tail"
    assert "2. e" not in next_line


def test_room_path_git_worktree_resolves_to_main_room(tmp_path):
    """owner #932 (2026-09-02): a worktree has a `.git` FILE and no .echelon/ — its room is the
    main checkout's room, not None."""
    main = tmp_path / "delta-shop"
    (main / ".git" / "worktrees" / "wt").mkdir(parents=True)
    wc.init(main, estate="delta-shop", scope="delta-shop")
    wt = tmp_path / "delta-shop-wt"
    (wt / "sub").mkdir(parents=True)
    (wt / ".git").write_text("gitdir: " + str(main / ".git" / "worktrees" / "wt").replace(chr(92), "/") + "\n", "utf-8")
    assert wc.room_path(wt / "sub") == (main / ".echelon").resolve()


# ── receipts: the cursor is a fold of receipts (owner #932, 2026-09-02) ──────

def test_receipt_folds_the_target_rooms_cursor(tmp_path, stub, monkeypatch):
    """The complaint: directing another room's work from a different room was not
    recorded. A receipt addressed to a room moves THAT room's cursor — no cwd, no hook."""
    monkeypatch.setattr(wc, "_now", lambda: "2026-09-01T00:00:00+00:00")
    root = tmp_path / "lad"
    root.mkdir()
    room = wc.init(root, estate="alpha-app", scope="alpha-app")
    before = json.loads((room / "cursor.json").read_text(encoding="utf-8"))
    monkeypatch.setattr(wc, "_now", lambda: "2026-09-01T00:00:01+00:00")
    res = wc.receipt(room, "board_receipt", "[alpha-app] R-0136 CLOSED",
                     origin="echelon", ref=916)
    assert res["ok"] is True
    cur = json.loads((room / "cursor.json").read_text(encoding="utf-8"))
    assert cur["last_act"]["kind"] == "board_receipt"
    assert cur["last_act"]["origin"] == "echelon" and cur["last_act"]["ref"] == 916
    assert "R-0136 CLOSED" in cur["last_act"]["digest"]
    assert cur["updated"] != before["updated"]
    # shape preserved — a fold is not a new file format
    for k in ("v", "goal", "task", "stage", "checkpoint", "next_actions",
              "blockers", "working_set", "recent_decisions", "updated"):
        assert k in cur, k


def test_receipt_by_registry_name_not_only_path(tmp_path, stub, monkeypatch):
    """board.py/runs.py know rooms by NAME; room_dir resolves the registry."""
    root = tmp_path / "ws"
    root.mkdir()
    room = wc.init(root, estate="ws", scope="ws")
    monkeypatch.setattr(wc, "_registry_entries",
                        lambda: {"ws": {"path": str(room), "scope": "ws"}})
    assert wc.room_dir("ws") == room.resolve()
    assert wc.receipt("ws", "run_state", "run-7 SUCCEEDED", origin="runs", ref="run-7")["ok"]
    cur = json.loads((room / "cursor.json").read_text(encoding="utf-8"))
    assert cur["last_act"]["kind"] == "run_state" and cur["last_act"]["ref"] == "run-7"


def test_receipt_is_idempotent_per_kind_and_ref(tmp_path, stub):
    """The board re-reads its rows; re-reading a row is not a new act."""
    root = tmp_path / "r"
    root.mkdir()
    room = wc.init(root, estate="r", scope="r")
    wc.receipt(room, "board_receipt", "DONE", origin="echelon", ref=42)
    second = wc.receipt(room, "board_receipt", "DONE", origin="echelon", ref=42)
    assert second["deduped"] is True
    assert len(wc.receipts(room)) == 1
    # a DIFFERENT kind on the same ref is a different act
    wc.receipt(room, "route", "routed", origin="board", ref=42)
    assert len(wc.receipts(room)) == 2


def test_receipts_own_the_checkpoint_text_and_hands_do_not(tmp_path, stub):
    """R-0138 (owner, board #991): 'RECEIPTS MOVE THE ROOM, HANDS SET NEXT' — the
    checkpoint text is DERIVED ONLY from receipts (each line source-stamped), and a
    hand (Stop hook, jump.py, manual checkpoint) sets next_actions/blockers only.
    That split is what stopped the text going days stale while `updated` moved."""
    root = tmp_path / "r"
    root.mkdir()
    room = wc.init(root, estate="r", scope="r")
    wc.checkpoint(room, summary="mid-fix on the upload door")
    wc.receipt(room, "commit", "abc1234 pinned the dep", origin="echelon", ref="abc1234")
    cur = json.loads((room / "cursor.json").read_text(encoding="utf-8"))
    assert "abc1234 pinned the dep" in cur["checkpoint"]
    assert "<echelon>" in cur["checkpoint"]              # source-stamped
    assert "abc1234" in cur["last_act"]["digest"]
    # a later HAND may not rewrite the text — only next_actions/blockers
    wc.checkpoint(room, summary="I decided to call it something else",
                  next_actions=["run the gate"], blockers=["waiting on owner"])
    cur = json.loads((room / "cursor.json").read_text(encoding="utf-8"))
    assert "abc1234 pinned the dep" in cur["checkpoint"]
    assert "something else" not in cur["checkpoint"]
    assert cur["next_actions"] == ["run the gate"] and cur["blockers"] == ["waiting on owner"]


def test_hand_summary_override_is_said_not_silent(tmp_path, stub):
    """Board #2481/#2486 (2026-09-04): a alpha-app worker whose --summary vanished under
    R-0138 filed it as 'the checkpoint tool pulls a stale cached summary'. The law stays;
    the result must SAY the hand summary was not applied, and only when it differs."""
    root = tmp_path / "r"
    root.mkdir()
    room = wc.init(root, estate="r", scope="r")
    # no receipts yet: a hand summary lands, nothing to report
    res = wc.checkpoint(room, summary="mid-fix on the upload door")
    assert "summary_ignored" not in res
    wc.receipt(room, "commit", "abc1234 pinned the dep", origin="echelon", ref="abc1234")
    res = wc.checkpoint(room, summary="I decided to call it something else",
                        next_actions=["run the gate"])
    assert "R-0138" in res["summary_ignored"] and "not applied" in res["summary_ignored"]
    assert "abc1234 pinned the dep" in res["summary"]
    # a hand that passes NO summary (the Stop hook's auto checkpoint) is not told off
    res = wc.checkpoint(room, auto=True, summary="", next_actions=["run the gate"])
    assert "summary_ignored" not in res


def test_fold_fills_an_empty_checkpoint(tmp_path, stub):
    root = tmp_path / "r"
    root.mkdir()
    room = wc.init(root, estate="r", scope="r")
    wc.receipt(room, "route", "[ws] owner: ship the invoice", origin="board", ref=864)
    cur = json.loads((room / "cursor.json").read_text(encoding="utf-8"))
    assert "ship the invoice" in cur["checkpoint"]


def test_fold_quiet_kinds_move_freshness_but_never_narrate(tmp_path, stub):
    """A check/beat receipt is bookkeeping: it becomes last_act (freshness) but the checkpoint
    text stays the last LOUD receipts — six checks after one route must not bury the route."""
    root = tmp_path / "r"
    root.mkdir()
    room = wc.init(root, estate="r", scope="r")
    wc.receipt(room, "route", "[ws] owner: ship the invoice", origin="board", ref=864)
    for i in range(6):
        wc.receipt(room, "check", f"verified checkpoint at 12:5{i} WIB, no change",
                   origin="room-pulse", ref=f"chk-{i}")
    wc.receipt(room, "staff_beat", "staff x beat_ts=... cost_usd=0", origin="staff", ref="b1")
    cur = json.loads((room / "cursor.json").read_text(encoding="utf-8"))
    assert "ship the invoice" in cur["checkpoint"]
    assert "no change" not in cur["checkpoint"]
    assert cur["last_act"]["kind"] == "staff_beat"


def test_receipt_never_raises_on_an_unknown_room(tmp_path, stub):
    """The board must post even if a room dir is missing — same law as route_to_room."""
    res = wc.receipt(tmp_path / "nope", "board_receipt", "x", origin="echelon")
    assert res["ok"] is False and res["err"]


def test_receipt_rejects_an_unknown_kind(tmp_path, stub):
    root = tmp_path / "r"
    root.mkdir()
    room = wc.init(root, estate="r", scope="r")
    assert wc.receipt(room, "gossip", "x", origin="e")["ok"] is False
    assert not (room / "receipts.jsonl").exists()


def test_open_and_close_item_leave_receipts(tmp_path, stub):
    """workcycle open/close take an EXPLICIT room — cross-room by construction."""
    root = tmp_path / "r"
    root.mkdir()
    room = wc.init(root, estate="r", scope="r")
    iid = wc.open_item(room, "fix the door")
    wc.close_item(room, iid, note="done", session="other-seat")
    kinds = [r["kind"] for r in wc.receipts(room)]
    assert kinds == ["item_open", "item_close"]
    cur = json.loads((room / "cursor.json").read_text(encoding="utf-8"))
    assert cur["last_act"]["kind"] == "item_close" and cur["last_act"]["ref"] == iid


def test_fold_of_a_receiptless_room_changes_nothing(tmp_path, stub):
    root = tmp_path / "r"
    root.mkdir()
    room = wc.init(root, estate="r", scope="r")
    before = (room / "cursor.json").read_text(encoding="utf-8")
    assert wc.fold(room) == {"ok": True, "folded": 0}
    assert (room / "cursor.json").read_text(encoding="utf-8") == before


def test_checkpoint_is_still_the_only_cursor_writer(tmp_path, stub):
    """MUTATION GUARD (doctor row 2, second leg): fold() must not grow its own
    cursor write. The row counts the in-module write sites."""
    root = tmp_path / "r"
    root.mkdir()
    room = wc.init(root, estate="r", scope="r")
    row = [r for r in wc.doctor(room) if r["row"] == 2][0]
    assert row["ok"] is True
    assert "in-module write sites: 2" in row["evidence"]


def test_a_replayed_receipt_keeps_the_acts_own_timestamp(tmp_path, stub):
    """The #932 backfill replays 48h of facts that already happened. Without `at`, the
    replay would stamp every one with the backfill's wall clock — making the cursor lie
    about WHEN in the very act of making it honest about WHAT."""
    root = tmp_path / "r"
    root.mkdir()
    room = wc.init(root, estate="r", scope="r")
    wc.receipt(room, "board_receipt", "[r] OPEN-0004 SHIPPED", origin="claude", ref=864,
               at="2026-09-02T08:29:55+07:00")
    cur = json.loads((room / "cursor.json").read_text(encoding="utf-8"))
    assert cur["updated"] == "2026-09-02T08:29:55+07:00"
    assert cur["last_act"]["ts"] == "2026-09-02T08:29:55+07:00"


def test_receipt_line_stamps_wib_one_clock():
    """Owner ONE CLOCK law (board #1224, 2026-09-02): the digest that the war page
    shows must read in WIB, not the raw UTC ISO slice (a 15:08 receipt read 08:08)."""
    from echelon_engine.workcycle import _receipt_line
    line = _receipt_line({"ts": "2026-09-02T08:14:59.744468+00:00", "kind": "board_receipt",
                          "ref": 1225, "origin": "claude", "text": "x"})
    assert line.startswith("09-02T15:14 board_receipt 1225 <claude>: x")
    # already-WIB input is not shifted twice
    assert _receipt_line({"ts": "2026-09-02T08:29:55+07:00", "kind": "k", "text": "y"}).startswith("09-02T08:29 k")


def test_project_schema_and_string_blocker_back_compat(tmp_path, stub):
    root = tmp_path / "repo"
    root.mkdir()
    room = wc.init(root, estate="acme", scope="acme")
    iid = wc.open_item(room, "ship console", project="console")
    assert json.loads((room / "open" / f"{iid}.json").read_text("utf-8"))["project"] == "console"
    wc.checkpoint(room, summary="legacy", blockers=["legacy"])
    assert json.loads((room / "cursor.json").read_text("utf-8"))["blockers"] == ["legacy"]
    wc.checkpoint(room, summary="scoped", blockers=["needs owner"], project="console")
    assert json.loads((room / "cursor.json").read_text("utf-8"))["blockers"] == [
        {"text": "needs owner", "project": "console"}]


def test_register_preserves_and_writer_sets_projects(tmp_path, _isolated_registry):
    root = tmp_path / "repo"
    root.mkdir()
    wc.init(root, estate="acme", scope="acme")
    wc.register_room(root)
    wc.set_room_projects("acme", {"console": {"name": "Console", "root": "console",
                                                      "repo": True, "deploy": False}})
    wc.register_room(root)
    assert wc._registry_entries()["acme"]["projects"]["console"]["repo"] is True

# appended by OPEN-0062 step 2 builder — engine gate tests for child rooms

# ── SPEC-S9 step 2: earned child rooms (promote/demote) ──────────────────────

def _estate_with_mrp(tmp_path, estate="beta-svc", open_n=3, *, repo=True, deploy=True):
    """A registered parent estate whose `mrp` project is promotion-ready."""
    root = tmp_path / estate
    root.mkdir()
    wc.init(root, estate=estate, scope="mol")
    wc.register_room(root, scope="mol")
    proot = root / "company" / "MRP"
    proot.mkdir(parents=True)
    wc.set_room_projects(estate, {"mrp": {"name": "MRP", "root": "company/MRP",
                                          "repo": repo, "deploy": deploy}})
    room = root / ".echelon"
    for i in range(1, open_n + 1):
        wc.open_item(room, f"mrp item {i}", project="mrp")
    return root, room, proot


def test_gate_1_child_has_no_inbox_orchestrator_or_own_bank_scope(tmp_path, _isolated_registry, stub):
    root, room, proot = _estate_with_mrp(tmp_path)
    out = wc.promote("beta-svc", "mrp")
    assert out["ok"] and out["child"] == "beta-svc/mrp"
    child = proot / ".echelon"
    rj = json.loads((child / "room.json").read_text("utf-8"))
    assert rj["estate"] == "beta-svc/mrp" and rj["parent"] == "beta-svc"
    assert rj["project"] == "mrp" and rj["scope"] == "mol"  # inherits; no independent scope
    tops = {p.name for p in child.iterdir()}
    assert tops <= {"room.json", "cursor.json", "open", "closed", "incidents", "receipts.jsonl", ".receipts.lock"}
    assert not (child / "inbox").exists()          # no child inbox (gate 1)
    assert not (child / "journal").exists()        # no orchestrator/journal seat
    assert not (child / "claims.json").exists()    # no claims seat
    assert wc.registry_entry("beta-svc/mrp")["parent"] == "beta-svc"  # qualified name + parent


def test_gate_2_parent_and_child_never_both_write_one_cursor(tmp_path, _isolated_registry, stub):
    root, room, proot = _estate_with_mrp(tmp_path)
    wc.promote("beta-svc", "mrp")
    child = proot / ".echelon"
    assert (room / "cursor.json").resolve() != (child / "cursor.json").resolve()
    parent_before = (room / "cursor.json").read_bytes()
    wc.open_item(child, "child-side work", project="mrp")  # a child act folds the CHILD cursor
    assert (room / "cursor.json").read_bytes() == parent_before  # parent cursor untouched
    child_cursor = json.loads((child / "cursor.json").read_text("utf-8"))
    assert child_cursor["last_act"]["kind"] == "item_open"


def test_gate_3_nearest_cwd_resolves_child_estate_cwd_resolves_parent(tmp_path, _isolated_registry, monkeypatch, stub):
    root, room, proot = _estate_with_mrp(tmp_path)
    wc.promote("beta-svc", "mrp")
    deep = proot / "src" / "deep"
    deep.mkdir(parents=True)
    monkeypatch.chdir(deep)
    assert wc.room_path() == (proot / ".echelon").resolve()   # child wins nearest-ancestor
    monkeypatch.chdir(root)
    assert wc.room_path() == room.resolve()                    # estate cwd -> parent
    assert wc.room_path(name="beta-svc/mrp") == (proot / ".echelon").resolve()


def test_gate_7_promotion_refuses_repo_false_deploy_false_or_fewer_than_3(tmp_path, _isolated_registry, stub):
    # three independent estates (distinct registry names): one broken criterion each
    cases = [({"repo": False}, "criterion 1"), ({"deploy": False}, "criterion 2"),
             ({"open_n": 2}, "criterion 3")]
    for i, (kw, crit) in enumerate(cases):
        estate = f"beta-svc{i}"
        root, room, proot = _estate_with_mrp(tmp_path, estate=estate, **kw)
        with pytest.raises(ValueError, match=crit):
            wc.promote(estate, "mrp")
        assert not (proot / ".echelon").exists()  # a refusal writes zero bytes


# ── R-0154 estate mode (owner ruling B, #4145): gate on repo-exists only ──────

def test_estate_mode_waives_deploy_and_3items_when_repo_exists(tmp_path, _isolated_registry, stub):
    # deploy=false AND 0 tagged items — the standard gate refuses on BOTH; estate
    # mode promotes because the project root is a real git repo.
    root, room, proot = _estate_with_mrp(tmp_path, estate="edesk", open_n=0,
                                         repo=True, deploy=False)
    (proot / ".git").mkdir()  # a real repo at the project root
    out = wc.promote("edesk", "mrp", estate=True)
    assert out["ok"] and out["child"] == "edesk/mrp"
    assert (proot / ".echelon" / "room.json").exists()


def test_estate_mode_still_refuses_without_a_repo(tmp_path, _isolated_registry, stub):
    root, room, proot = _estate_with_mrp(tmp_path, estate="norepo", open_n=0,
                                         repo=True, deploy=False)
    # no .git at proot
    with pytest.raises(ValueError, match="estate criterion"):
        wc.promote("norepo", "mrp", estate=True)
    assert not (proot / ".echelon" / "room.json").exists()


def test_promote_scope_override_binds_own_scope(tmp_path, _isolated_registry, stub):
    root, room, proot = _estate_with_mrp(tmp_path, estate="ownscope", open_n=0,
                                         repo=True, deploy=False)
    (proot / ".git").mkdir()
    out = wc.promote("ownscope", "mrp", estate=True, scope_override="mrp")
    assert out["ok"]
    rj = json.loads((proot / ".echelon" / "room.json").read_text("utf-8"))
    assert rj["scope"] == "mrp"  # own name, not the parent's "mol"


def test_promote_scope_defaults_to_parent(tmp_path, _isolated_registry, stub):
    root, room, proot = _estate_with_mrp(tmp_path, estate="inh", open_n=0,
                                         repo=True, deploy=False)
    (proot / ".git").mkdir()
    wc.promote("inh", "mrp", estate=True)
    rj = json.loads((proot / ".echelon" / "room.json").read_text("utf-8"))
    assert rj["scope"] == "mol"  # inherited


def test_estate_mode_coexists_with_existing_obj_state(tmp_path, _isolated_registry, stub):
    # the ledger-desk case: .echelon already holds framework obj state — promote
    # must ADD its room files, never clobber the obj files.
    root, room, proot = _estate_with_mrp(tmp_path, estate="objdesk", open_n=0,
                                         repo=True, deploy=False)
    (proot / ".git").mkdir()
    obj = proot / ".echelon"
    obj.mkdir()
    (obj / "cache.json").write_text('{"obj":1}', encoding="utf-8")
    (obj / "ledger.jsonl").write_text('{"x":1}\n', encoding="utf-8")
    (obj / "txn").mkdir()
    out = wc.promote("objdesk", "mrp", estate=True)
    assert out["ok"]
    # obj state untouched
    assert (obj / "cache.json").read_text(encoding="utf-8") == '{"obj":1}'
    assert (obj / "ledger.jsonl").read_text(encoding="utf-8") == '{"x":1}\n'
    assert (obj / "txn").is_dir()
    # room files added alongside
    assert (obj / "room.json").exists() and (obj / "cursor.json").exists()


def test_gate_8_dry_run_writes_zero_bytes_and_apply_is_idempotent(tmp_path, _isolated_registry, stub):
    root, room, proot = _estate_with_mrp(tmp_path)
    home = tmp_path / "home"
    reg = home / "rooms.json"
    registry_before = reg.read_bytes()
    receipts_before = (room / "receipts.jsonl").read_bytes()
    plan = wc.promote("beta-svc", "mrp", dry_run=True)
    assert plan["dry_run"] and plan["criteria"] == {"repo": True, "deploy": True,
                                                    "open_items": 3, "project_root": str(proot)}
    assert plan["counts"] == {"open": 3, "closed": 0, "incidents": 0}
    assert not (proot / ".echelon").exists()          # zero bytes: no child dir...
    assert not (room / "manifests").exists()          # ...no manifest...
    assert reg.read_bytes() == registry_before        # ...no registry write...
    assert (room / "receipts.jsonl").read_bytes() == receipts_before  # ...no receipts
    out = wc.promote("beta-svc", "mrp")                # apply
    assert out["ok"] and out["counts"]["open"] == 3
    receipts_after_first = len(wc.receipts(room))
    again = wc.promote("beta-svc", "mrp")              # idempotent: registered child -> no-op
    assert again["already"] is True
    assert len(wc.receipts(room)) == receipts_after_first  # no second promotion receipt
    assert len(list((proot / ".echelon" / "open").glob("*.json"))) == 3  # no double copies
    child_receipts = wc.receipts(proot / ".echelon")
    assert child_receipts and child_receipts[-1]["kind"] == "promoted_from_parent"
    journal = json.loads((home / "registry-journal.jsonl").read_text("utf-8").splitlines()[-1])
    assert journal["kind"] == "child_promoted" and journal["child"] == "beta-svc/mrp"


def test_gate_9_rollback_keeps_source_and_every_state_record(tmp_path, _isolated_registry, stub):
    root, room, proot = _estate_with_mrp(tmp_path)
    wc.promote("beta-svc", "mrp")
    child = proot / ".echelon"
    marker = proot / "src" / "main.py"
    marker.parent.mkdir(parents=True)
    src_before = b"# project source\n"
    marker.write_bytes(src_before)
    wc.open_item(child, "child-only idea", project="mrp")          # child-only work (OPEN-0004)
    wc.close_item(child, "OPEN-0001", note="done in the child")    # an evolved copy
    assert not list((room / "closed").glob("*.json"))              # parent had no closed items yet
    out = wc.demote("beta-svc/mrp")
    assert out["ok"]
    assert marker.read_bytes() == src_before               # project source untouched (gate 9)
    assert not child.exists()                              # only the promotion-created dir deleted
    assert not (room / "manifests" / "mrp.json").exists()
    assert wc.registry_entry("beta-svc/mrp") == {}          # unregistered
    parent_open = {p.stem: json.loads(p.read_text("utf-8")) for p in (room / "open").glob("*.json")}
    parent_closed = {p.stem: json.loads(p.read_text("utf-8")) for p in (room / "closed").glob("*.json")}
    # the three copied baselines stayed with the parent (promotion copied, never moved)
    assert {"OPEN-0001", "OPEN-0002", "OPEN-0003"} <= set(parent_open)
    # the child-only idea came back under a collision-safe id, still tagged mrp
    back = next((it for it in parent_open.values() if "child-only" in it["text"]), None)
    assert back is not None and back["project"] == "mrp"
    assert back["id"] not in {"OPEN-0001", "OPEN-0002", "OPEN-0003"}
    # the child's close of a copy came back as a new closed record — nothing lost
    assert parent_closed and next(iter(parent_closed.values()))["project"] == "mrp"
    last = wc.receipts(room)[-1]
    assert last["kind"] == "child_demoted" and last["ref"] == "beta-svc/mrp"
    assert "->" in last["text"]  # the mapping receipt
    # demote of a demoted child is a hard error, not a silent no-op
    with pytest.raises(KeyError, match="no registered child"):
        wc.demote("beta-svc/mrp")


def test_receipt_concurrent_processes_dedup_one_durable_record(tmp_path):
    import subprocess
    import sys
    room = tmp_path / 'room'
    room.mkdir()
    (room / 'room.json').write_text('{}', encoding='utf-8')
    script = """
import json,sys
from pathlib import Path
from echelon_engine.workcycle import receipt
print(json.dumps(receipt(Path(sys.argv[1]), 'route', 'same event', origin='fixture', ref='same-id', fold_now=False)))
"""
    workers = [subprocess.Popen([sys.executable, '-X', 'utf8', '-c', script, str(room)],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE) for _ in range(8)]
    results = []
    for worker in workers:
        stdout, stderr = worker.communicate(timeout=20)
        assert worker.returncode == 0, stderr
        results.append(json.loads(stdout))
    assert all(r['ok'] for r in results)
    assert sum(not r.get('deduped', False) for r in results) == 1
    rows = (room / 'receipts.jsonl').read_text(encoding='utf-8').splitlines()
    assert len(rows) == 1 and json.loads(rows[0])['ref'] == 'same-id'


def test_receipt_corruption_is_not_silently_skipped_or_appended(tmp_path):
    room = tmp_path / 'room'
    room.mkdir()
    (room / 'room.json').write_text('{}', encoding='utf-8')
    path = room / 'receipts.jsonl'
    original = b'{broken record'
    path.write_bytes(original)
    result = wc.receipt(room, 'route', 'new event', origin='fixture', ref='new', fold_now=False)
    assert result['ok'] is False and result['error_type'] == 'ReceiptIncompleteRecord'
    assert result['line'] == 1
    assert path.read_bytes() == original


def test_receipt_retry_cannot_bypass_failed_fsync(tmp_path, monkeypatch):
    room = tmp_path / 'room'
    room.mkdir()
    (room / 'room.json').write_text('{}', encoding='utf-8')
    real_sync = wc.os.fsync
    calls = []
    def fail(fd):
        calls.append(fd)
        raise OSError('fixture flush failure')
    monkeypatch.setattr(wc.os, 'fsync', fail)
    first = wc.receipt(room, 'route', 'event', origin='fixture', ref='same', fold_now=False)
    second = wc.receipt(room, 'route', 'event', origin='fixture', ref='same', fold_now=False)
    assert not first['ok'] and not second['ok']
    assert len(calls) == 2
    monkeypatch.setattr(wc.os, 'fsync', real_sync)
    recovered = wc.receipt(room, 'route', 'event', origin='fixture', ref='same', fold_now=False)
    assert recovered['ok'] and recovered['deduped']
    assert len((room / 'receipts.jsonl').read_text(encoding='utf-8').splitlines()) == 1


def test_receipt_retry_repairs_failed_fold_without_appending(tmp_path, monkeypatch):
    room = tmp_path / 'room'
    room.mkdir()
    (room / 'room.json').write_text('{}', encoding='utf-8')
    def fail(*args):
        raise OSError('fixture fold failure')
    monkeypatch.setattr(wc, 'fold', fail)
    first = wc.receipt(room, 'route', 'event', origin='fixture', ref='same')
    assert not first['ok']
    monkeypatch.setattr(wc, 'fold', lambda room: {'ok': True, 'folded': 1})
    recovered = wc.receipt(room, 'route', 'event', origin='fixture', ref='same')
    assert recovered['ok'] and recovered['deduped']
    assert recovered['fold']['ok']
    assert len((room / 'receipts.jsonl').read_text(encoding='utf-8').splitlines()) == 1


@pytest.mark.parametrize('tail,expected', [
    (b'{"kind":"route","ref":"old"}', 'ReceiptIncompleteRecord'),
    (b'{bad json}\n', 'ReceiptCorruption'),
])
def test_receipt_never_appends_to_incomplete_or_malformed_record(tmp_path, tail, expected):
    room = tmp_path / 'room'
    room.mkdir()
    (room / 'room.json').write_text('{}', encoding='utf-8')
    path = room / 'receipts.jsonl'
    path.write_bytes(tail)
    result = wc.receipt(room, 'route', 'next', origin='fixture', ref='new', fold_now=False)
    assert result['ok'] is False and result['error_type'] == expected
    assert path.read_bytes() == tail


def test_strict_receipt_identity_refuses_changed_payload(tmp_path):
    room = tmp_path / 'room'
    room.mkdir()
    (room / 'room.json').write_text('{}', encoding='utf-8')
    first = wc.receipt(room, 'route', 'original', origin='fixture', ref='event',
                       fold_now=False, strict_identity=True)
    assert first['ok']
    original = (room / 'receipts.jsonl').read_bytes()
    conflict = wc.receipt(room, 'route', 'changed', origin='fixture', ref='event',
                          fold_now=False, strict_identity=True)
    assert conflict['ok'] is False
    assert conflict['error_type'] == 'ReceiptIdentityConflict'
    assert (room / 'receipts.jsonl').read_bytes() == original
    retry = wc.receipt(room, 'route', 'original', origin='fixture', ref='event',
                       fold_now=False, strict_identity=True)
    assert retry['ok'] and retry['deduped']


def test_strict_receipt_hash_covers_text_beyond_display_truncation(tmp_path):
    room = tmp_path / 'room'
    room.mkdir()
    (room / 'room.json').write_text('{}', encoding='utf-8')
    first = wc.receipt(room, 'route', 'x'*600 + 'original', origin='fixture', ref='event',
                       fold_now=False, strict_identity=True)
    assert first['ok'] and len(first['receipt']['text']) == 600
    conflict = wc.receipt(room, 'route', 'x'*600 + 'changed', origin='fixture', ref='event',
                          fold_now=False, strict_identity=True)
    assert conflict['error_type'] == 'ReceiptIdentityConflict'
