"""liveness queue — the sweep moved OFF the wrap's critical path onto the daily backup.

WHY (owner, 2026-08-18): "liveness check won't affect a wrap right. then just wrote to a
global ./echelon liveness queue, it will run daily with db backup." The sweep was the slowest
step in the wrap ritual and it gated nothing — no claim in a wrap receipt is false if it never
ran. So /wrap enqueues (one file write) and `backup --push` drains.

THE LOAD-BEARING PROPERTY IS THAT THE DAEMON NEVER MUTATES AN ATOM. A dead path is usually a
MOVE, so auto-disputing on a failed grep would destroy earned weight on a rename. The daemon
does the mechanical half and hands the judgment half to the next session.

Pinned here too: the drain read `res["atoms"]` while `sweep()` returns `res["checked"]`, and
reported **0 checked on a bank of 1,400 atoms** — a clean-looking report that had examined
nothing. That is the third silent-zero of this session (the lens parser and the veto sweep
were the others), so the schema mismatch now raises instead of defaulting.
"""
import json

import pytest

from echelon_engine.atoms import liveness as L


@pytest.fixture
def home(tmp_path, monkeypatch):
    """Redirect the queue/report to a temp HOME — these are real files under ~/.echelon."""
    monkeypatch.setattr(L, "_QUEUE", tmp_path / "liveness-queue.json")
    monkeypatch.setattr(L, "_REPORT", tmp_path / "liveness-report.json")
    return tmp_path


# ── the queue ─────────────────────────────────────────────────────────────────

def test_enqueue_is_idempotent_per_scope(home):
    """The queue is a SET of scopes to sweep, not a log of requests — wrapping five times a
    day must queue one sweep, not five."""
    for _ in range(5):
        L.enqueue("echelon", ["D:/repos/estate"], 10)
    q = json.loads(L._QUEUE.read_text(encoding="utf-8"))
    assert list(q) == ["echelon"]


def test_enqueue_records_roots_and_sweep_size(home):
    L.enqueue("echelon", ["D:/b", "D:/a", "D:/a"], 7)
    spec = json.loads(L._QUEUE.read_text(encoding="utf-8"))["echelon"]
    assert spec["roots"] == ["D:/a", "D:/b"]     # deduped + stable order
    assert spec["sweep"] == 7 and spec["queued_utc"]


def test_enqueue_does_not_sweep(home, monkeypatch):
    """Enqueue must be a file write, nothing more — that is the whole point of the move."""
    monkeypatch.setattr(L, "sweep", lambda *a, **k: pytest.fail("enqueue must not sweep"))
    L.enqueue("echelon", [], 10)


# ── the drain ─────────────────────────────────────────────────────────────────

def _sweep_result(n_ok=2, n_gone=1):
    checked = [{"slug": f"live-{i}", "status": "anchors-live"} for i in range(n_ok)]
    checked += [{"slug": f"gone-{i}", "status": "ANCHOR-GONE",
                 "dead_paths": ["a/b.py"], "dead_verbs": ["oldverb"]} for i in range(n_gone)]
    return {"scope": "echelon", "checked": checked}


def test_drain_reports_and_empties_the_queue(home, monkeypatch):
    monkeypatch.setattr(L, "sweep", lambda *a, **k: _sweep_result())
    L.enqueue("echelon", [], 10)
    res = L.drain()
    assert res["scopes"]["echelon"]["checked"] == 3
    assert res["scopes"]["echelon"]["anchor_gone"] == 1
    assert len(res["needs_verdict"]) == 1
    assert res["needs_verdict"][0]["dead"] == ["a/b.py", "oldverb"]
    assert json.loads(L._QUEUE.read_text(encoding="utf-8")) == {}, "queue must drain"
    assert L.pending_report()["scopes"]["echelon"]["checked"] == 3


def test_drain_on_an_empty_queue_is_a_noop(home):
    assert L.drain()["scopes"] == 0 or L.drain().get("scopes") in (0, {})


def test_drain_never_mutates_an_atom(home, monkeypatch):
    """The safety property. A dead path is usually a MOVE; auto-disputing destroys earned
    weight on a rename. If the daemon ever reaches for a write verb, this fails."""
    import echelon_engine.atoms.cards as cards
    for verb in ("dispute", "disclaim"):
        if hasattr(cards.CardStore, verb):
            monkeypatch.setattr(cards.CardStore, verb,
                                lambda *a, **k: pytest.fail(f"drain must never call {verb}()"))
    monkeypatch.setattr(L, "sweep", lambda *a, **k: _sweep_result(n_gone=3))
    L.enqueue("echelon", [], 10)
    assert len(L.drain()["needs_verdict"]) == 3    # reported, not acted on


def test_one_bad_scope_cannot_kill_the_backup_job(home, monkeypatch):
    """The drain rides `backup --push`. Hygiene must never break the recovery path."""
    def boom(scope, *a, **k):
        if scope == "bad":
            raise RuntimeError("scope exploded")
        return _sweep_result()
    monkeypatch.setattr(L, "sweep", boom)
    L.enqueue("bad", [], 10)
    L.enqueue("echelon", [], 10)
    res = L.drain()
    assert "error" in res["scopes"]["bad"]
    assert res["scopes"]["echelon"]["checked"] == 3, "a sibling failure must not lose good work"


def test_schema_drift_is_reported_not_silently_zero(home, monkeypatch):
    """THE BUG THIS PINS: drain read res['atoms'] while sweep() returns res['checked'], so it
    reported 0-checked over 1,400 atoms — a report that examined nothing and looked clean.
    A missing key must surface as an error, never as a comfortable default."""
    monkeypatch.setattr(L, "sweep", lambda *a, **k: {"scope": "echelon", "atoms": []})
    L.enqueue("echelon", [], 10)
    res = L.drain()
    assert "error" in res["scopes"]["echelon"]
    assert "checked" in res["scopes"]["echelon"]["error"]


def test_report_before_any_sweep_is_empty_not_a_crash(home):
    assert L.pending_report() == {}


# ── scope drift: filed where you were STANDING, not where it BELONGS ──────────

def test_scope_drift_flags_a_canonical_that_is_the_odd_one_out(home, monkeypatch):
    """Owner 2026-08-18, on prod_db_connection: "maybe it was a day when i do wk job at flux?
    and the cross repo still act that it was flux atom?" — yes. Hygiene canonicalises by
    earned WEIGHT, not by domain, so a lesson about estate A filed while standing in estate B
    lives in B forever if B's copy was heaviest. One question about one atom surfaced 19."""
    from echelon_engine.atoms import liveness as LV
    rows = [{"canon_scope": "flux", "canon_coord": "flux:x", "dup_scope": "wk"},
            {"canon_scope": "flux", "canon_coord": "flux:x", "dup_scope": "wk"},
            {"canon_scope": "flux", "canon_coord": "flux:x", "dup_scope": "ux"}]

    class _Cur:
        def execute(self, *a): return self
        def fetchall(self): return rows

    class _CS:
        _lock = __import__("threading").Lock()
        conn = _Cur()

    monkeypatch.setattr("echelon_engine.atoms.cards.CardStore", lambda *a, **k: _CS())
    d = LV.scope_drift()
    assert len(d) == 1 and d[0]["canonical_scope"] == "flux"
    assert d[0]["top_by_count"] == "wk"


def test_scope_drift_is_silent_when_the_canonical_agrees_with_its_copies(home, monkeypatch):
    """A canonical that already shares a scope with its subsumed copies is correctly filed,
    however many scopes it merged. Flagging those would bury the real drift in noise."""
    from echelon_engine.atoms import liveness as LV
    rows = [{"canon_scope": "wk", "canon_coord": "wk:x", "dup_scope": "wk"},
            {"canon_scope": "wk", "canon_coord": "wk:x", "dup_scope": "flux"}]

    class _Cur:
        def execute(self, *a): return self
        def fetchall(self): return rows

    class _CS:
        _lock = __import__("threading").Lock()
        conn = _Cur()

    monkeypatch.setattr("echelon_engine.atoms.cards.CardStore", lambda *a, **k: _CS())
    assert LV.scope_drift() == []


def test_scope_drift_reports_candidates_not_a_single_verdict(home, monkeypatch):
    """The count is a HINT. A lesson ported into the substrate scope can outnumber the estate
    it is actually about (prod_db_connection: echelon x2 vs WK x1, and WK is the real owner).
    Naming one winner would launder a tally into an answer."""
    from echelon_engine.atoms import liveness as LV
    rows = [{"canon_scope": "flux", "canon_coord": "flux:p", "dup_scope": "echelon"},
            {"canon_scope": "flux", "canon_coord": "flux:p", "dup_scope": "echelon"},
            {"canon_scope": "flux", "canon_coord": "flux:p", "dup_scope": "wk"}]

    class _Cur:
        def execute(self, *a): return self
        def fetchall(self): return rows

    class _CS:
        _lock = __import__("threading").Lock()
        conn = _Cur()

    monkeypatch.setattr("echelon_engine.atoms.cards.CardStore", lambda *a, **k: _CS())
    d = LV.scope_drift()[0]
    assert [s for s, _ in d["candidates"]] == ["echelon", "wk"], "all candidates must travel"
    assert "suggested_scope" not in d, "a single 'suggested' field reads as a verdict"


def test_scope_drift_never_migrates(home, monkeypatch):
    """It REPORTS. `atoms` has no rename-scope verb, and a canonical carries merge receipts
    plus inbound refs that would dangle on an in-place rewrite — the dangling-successor
    defect. Re-canonicalising is a decision, not a cron job."""
    from echelon_engine.atoms import liveness as LV
    import echelon_engine.atoms.cards as cards
    monkeypatch.setattr(cards.CardStore, "add_atom",
                        lambda *a, **k: pytest.fail("scope_drift must never write"))
    LV.scope_drift()


# ── the VERB carries the ritual, not the skill ────────────────────────────────

def test_wrap_verb_queues_liveness_itself(home, monkeypatch):
    """Owner 2026-08-18: "harness with no skills or echelon agent itself will run those."

    ~/.claude/skills/wrap/SKILL.md is a CLAUDE-HARNESS surface. A bare `echelon wrap`, the
    agent's own close-out, or any non-Claude harness never reads it — so a ritual step that
    lives only in the skill silently does not happen for every other caller. The atlas shows
    zero module-level consumers of atoms-wrap, which makes the VERB the single door."""
    from echelon_engine.atoms.wrap import WrapSession
    w = WrapSession.__new__(WrapSession)
    w.scope = "echelon"
    res = w._queue_liveness()
    assert res["ok"] is True and res["queued"] == "echelon"
    assert json.loads(L._QUEUE.read_text(encoding="utf-8"))["echelon"]["scope"] == "echelon"


def test_wrap_queues_both_roots(home):
    """An engine atom cites paths in ECHELON-AGENT, an estate atom cites paths in ECHELON.
    Checking an anchor against only ONE root reports a live file as GONE — a false death
    that invites a dispute which would destroy earned weight."""
    from echelon_engine.atoms.wrap import WrapSession
    w = WrapSession.__new__(WrapSession)
    w.scope = "echelon"
    w._queue_liveness()
    roots = json.loads(L._QUEUE.read_text(encoding="utf-8"))["echelon"]["roots"]
    assert len(roots) >= 2, f"both engine and estate roots must be queued, got {roots}"


def test_liveness_failure_never_fails_the_wrap(home, monkeypatch):
    """Hygiene must not break the close-out. A wrap that dies because a queue file was
    unwritable would lose the whole session's bank phases."""
    from echelon_engine.atoms import wrap as W
    import echelon_engine.atoms.liveness as LV
    monkeypatch.setattr(LV, "enqueue", lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")))
    w = W.WrapSession.__new__(W.WrapSession)
    w.scope = "echelon"
    res = w._queue_liveness()
    assert res["ok"] is False and "disk full" in res["error"]
