"""Tests for the journal diet (2026-08-27): the TICK delta trigger split + `sync gc`
prune-on-ack. Measured ground: sync_journal was 68% of a 465MB live bank; 93,736 of 102,410
UPDATE rows were pure weight ticks on `atoms`, journaling the FULL row (content + growing
score_history) on every score/use_count move.

Hermetic: temp file banks only, never the live ~/.echelon/echelon.db. Mirrors
test_sync_journal.py's fixtures (CardStore over a tmp_path bank, ECHELON_SYNC=1 autouse).

Five classes (SPEC-journal-diet.md §C):
  1. weight-only UPDATE -> TICK, no 'content' key; content UPDATE -> full UPDATE payload.
  2. apply_ops applies a TICK to an existing atom; ignores a TICK for a missing atom;
     replay-idempotent.
  3. gc prunes only below min(pushed_through)-retain; refuses with no peers; auto-gc after push.
  4. Regression: existing sync round-trip stays green (the INSERT trigger untouched).
  5. Anti-no-op: temporarily re-fatten the tick trigger -> test 1 fails (prove the assertion
     bites), then restore.
"""
import json
import sqlite3

import pytest

from echelon_engine.atoms.cards import CardStore
from echelon_engine.atoms import sync_journal as sj
from echelon_engine.atoms import sync_cmd


@pytest.fixture(autouse=True)
def _sync_on(monkeypatch):
    monkeypatch.setenv("ECHELON_SYNC", "1")


@pytest.fixture
def bank(tmp_path):
    store = CardStore(tmp_path / "echelon.db")
    return store, tmp_path


def _plant(store, coord, content, scope="testscope"):
    aid = store.add_atom(coord, content, scope=scope)
    store.compile_atom_struct(aid)
    return aid


def _ops(store, since=0, **kw):
    return sj.ops_since(store.conn, since, **kw)


# ── 1. TICK vs full payload ────────────────────────────────────────────────────

def test_weight_only_update_journals_tick_with_no_content(bank):
    store, _ = bank
    aid = _plant(store, "lesson:tick", "# tick\nclaim")
    before = sj.head(store.conn)
    store.remember_fetch(aid, depth="spine")     # score/score_history/use_count only
    ops = [o for o in _ops(store, before) if o["tbl"] == "atom_earned"]
    assert ops, "a weight-only update did not journal at all"
    assert ops[-1]["op"] == "TICK"
    payload = json.loads(ops[-1]["payload"])
    assert "content" not in payload
    assert set(payload.keys()) <= {"score", "use_count", "score_history", "last_fetch_ts", "atom_id"}


def test_atoms_weight_only_update_journals_tick(bank):
    """Same law on atoms directly (not just atom_earned)."""
    store, _ = bank
    aid = _plant(store, "lesson:atick", "# atick\nclaim")
    before = sj.head(store.conn)
    store.conn.execute("UPDATE atoms SET score=?, use_count=use_count+1 WHERE id=?",
                       (123.4, aid))
    store.conn.commit()
    ops = [o for o in _ops(store, before) if o["tbl"] == "atoms"]
    assert ops and ops[-1]["op"] == "TICK"
    payload = json.loads(ops[-1]["payload"])
    assert "content" not in payload
    assert "kind" not in payload
    assert "scope" not in payload
    assert payload["score"] == 123.4


def test_kind_or_scope_update_journals_full_payload_not_tick(bank):
    """A real edit (kind changes alongside score) must still carry the FULL row — the tick
    trigger's WHEN guard must NOT fire when a non-tick column (kind/scope) also changed.
    (content is not a watched column at all — see sync_journal.py module docstring.)"""
    store, _ = bank
    aid = _plant(store, "lesson:full", "# full\nclaim")
    before = sj.head(store.conn)
    store.conn.execute("UPDATE atoms SET score=?, kind='disclaimed' WHERE id=?", (55.0, aid))
    store.conn.commit()
    ops = [o for o in _ops(store, before) if o["tbl"] == "atoms"]
    assert ops, "a real edit did not journal"
    assert ops[-1]["op"] == "UPDATE", "a real edit was mistaken for a pure tick"
    payload = json.loads(ops[-1]["payload"])
    assert payload["kind"] == "disclaimed"
    assert payload["score"] == 55.0


def test_scope_only_update_on_atom_earned_is_full_not_tick(bank):
    """atom_earned's scope column is a real edit, not a tick column — must go through _upd."""
    store, _ = bank
    aid = _plant(store, "lesson:scoped", "# s\nclaim", scope="alpha")
    before = sj.head(store.conn)
    store.conn.execute("UPDATE atom_earned SET scope='beta' WHERE atom_id=?", (aid,))
    store.conn.commit()
    ops = [o for o in _ops(store, before) if o["tbl"] == "atom_earned"]
    assert ops and ops[-1]["op"] == "UPDATE"


# ── 2. apply_ops TICK handling ─────────────────────────────────────────────────

def test_apply_ops_applies_tick_to_existing_atom(bank, tmp_path):
    store, tmp = bank
    peer = CardStore(tmp / "peer.db")
    content = "# shared\nsame atom both sides"
    local_id = _plant(store, "lesson:tickapply", content, scope="alpha")
    peer_id = _plant(peer, "lesson:tickapply", content, scope="alpha")
    assert local_id == peer_id

    before = sj.head(peer.conn)
    peer.remember_fetch(peer_id, depth="spine")
    ops = [o for o in sj.ops_since(peer.conn, before) if o["tbl"] == "atom_earned"
           and o["op"] == "TICK"]
    assert ops, "fixture did not produce a TICK op"

    res = sj.apply_ops(store.conn, ops)
    assert res["tick"] == 1
    row = store.conn.execute(
        "SELECT use_count, score_history FROM atom_earned WHERE atom_id=?", (local_id,)).fetchone()
    hist = json.loads(row[1])
    assert any(len(e) > 2 and e[2] == "fetch" for e in hist)


def test_apply_ops_ignores_tick_for_missing_atom(bank):
    """A tick for an atom this bank has never seen cannot be materialized and is meaningless
    (no content to insert with it) — skip loudly, never write a corrupt half-row."""
    store, _ = bank
    ops = [{"seq": 1, "tbl": "atom_earned", "op": "TICK", "pk": '{"atom_id":"never_seen"}',
            "payload": '{"score": 120.0, "use_count": 1, "score_history": "[]"}',
            "scope": "alpha", "ts": 0, "origin": ""}]
    res = sj.apply_ops(store.conn, ops)
    assert res["skipped"] == 1
    assert res.get("tick", 0) == 0
    assert store.conn.execute(
        "SELECT COUNT(*) FROM atom_earned WHERE atom_id='never_seen'").fetchone()[0] == 0


def test_apply_ops_tick_is_replay_idempotent(bank, tmp_path):
    store, tmp = bank
    peer = CardStore(tmp / "peer_idem.db")
    content = "# idem-tick\nclaim"
    aid = _plant(store, "lesson:idemtick", content, scope="alpha")
    _plant(peer, "lesson:idemtick", content, scope="alpha")
    peer.remember_fetch(aid, depth="spine")
    ops = [o for o in sj.ops_since(peer.conn) if o["tbl"] == "atom_earned" and o["op"] == "TICK"]
    sj.apply_ops(store.conn, ops)
    sj.apply_ops(store.conn, ops)
    sj.apply_ops(store.conn, ops)
    uc = store.conn.execute(
        "SELECT use_count FROM atom_earned WHERE atom_id=?", (aid,)).fetchone()[0]
    assert uc == 1, "re-applying the same TICK batch inflated use_count"


# ── 3. sync gc ──────────────────────────────────────────────────────────────────

def test_gc_refuses_with_no_peers(bank):
    store, _ = bank
    with pytest.raises(ValueError):
        sj.gc(store.conn)


def test_gc_refuses_when_pushed_through_never_set(bank):
    store, _ = bank
    sj.set_peer_state(store.conn, "cloud", pulled_through=5)   # pushed_through stays default 0
    with pytest.raises(ValueError):
        sj.gc(store.conn)


def test_gc_prunes_only_below_min_pushed_minus_retain(bank):
    store, _ = bank
    for i in range(20):
        store.add_atom(f"lesson:gc{i}", f"# gc{i}\nclaim", scope="alpha")
    head = sj.head(store.conn)
    assert head >= 20
    sj.set_peer_state(store.conn, "cloud", pushed_through=head)

    res = sj.gc(store.conn, retain=5)
    assert res["cutoff_seq"] == head - 5
    remaining = store.conn.execute("SELECT MIN(seq) FROM sync_journal").fetchone()[0]
    assert remaining is not None and remaining > head - 5
    n_after = store.conn.execute("SELECT COUNT(*) FROM sync_journal").fetchone()[0]
    assert n_after == head - (head - 5)   # everything above the cutoff survives


def test_gc_reports_rows_and_size_before_after(bank):
    store, _ = bank
    for i in range(10):
        store.add_atom(f"lesson:sz{i}", f"# sz{i}\nclaim " * 50, scope="alpha")
    head = sj.head(store.conn)
    sj.set_peer_state(store.conn, "cloud", pushed_through=head)
    res = sj.gc(store.conn, retain=0)
    assert res["pruned"] == head
    assert "size_before" in res and "size_after" in res


def test_gc_vacuum_shrinks_file(bank, tmp_path):
    store, tmp = bank
    for i in range(30):
        store.add_atom(f"lesson:vac{i}", f"# vac{i}\n" + ("x" * 500), scope="alpha")
    head = sj.head(store.conn)
    sj.set_peer_state(store.conn, "cloud", pushed_through=head)
    res = sj.gc(store.conn, retain=0, vacuum=True)
    assert res["vacuumed"] is True
    assert res["pruned"] == head


def test_auto_gc_after_push_prunes(bank, tmp_path, monkeypatch):
    """sync_once runs gc after a successful push that advances pushed_through, silent unless
    it prunes. Simulate via a fake gateway."""
    store, tmp = bank
    aid = _plant(store, "lesson:autogc", "# autogc\nclaim", scope="alpha")
    for _ in range(3):
        store.remember_fetch(aid, depth="spine")

    calls = {"push": 0}

    def fake_call(gateway, route, token, *, payload=None, query="", timeout=300):
        if route == "/sync/hello":
            return {"generation": "peergen", "head": 0, "your_cursor": 0, "engine": "unknown"}
        if route == "/sync/pull":
            return {"ops": []}
        if route == "/sync/push":
            calls["push"] += 1
            through = payload["ops"][-1]["seq"] if payload["ops"] else 0
            return {"through": through, "applied_ops": len(payload["ops"]), "applied": {}}
        raise AssertionError(route)

    monkeypatch.setattr(sync_cmd, "_call", fake_call)
    # Small retain so this tiny fixture journal actually clears the prunable floor after the
    # push advances pushed_through to the new head (the default 5000 would never trigger here).
    monkeypatch.setattr(sync_cmd, "_GC_RETAIN", 1)

    r = sync_cmd.sync_once(store.conn, gateway="http://fake", token="t", scope="alpha")
    assert calls["push"] >= 1
    assert r["pushed"] > 0
    assert "gc" in r
    assert r["gc"]["pruned"] >= 1


def test_gc_dry_run_never_prunes():
    """--dry-run must change nothing on disk (contract, not a gc-specific law) — sync_once's
    push branch returns before ops are sent, so gc must not even be attempted."""
    import inspect
    src = inspect.getsource(sync_cmd.sync_once)
    # gc call must be inside the `if not dry_run` guard — structural check that the guard exists.
    assert "if not dry_run and out[\"pushed\"]" in src


# ── 4. regression: existing round-trip stays green ──────────────────────────────

def test_insert_trigger_untouched_full_row_on_insert(bank):
    store, _ = bank
    aid = store.add_atom("lesson:ins", "# ins\nthe claim", scope="alpha")
    ops = [o for o in _ops(store) if o["tbl"] == "atoms" and o["op"] == "INSERT"]
    assert len(ops) == 1
    payload = json.loads(ops[0]["payload"])
    assert payload["content"] == "# ins\nthe claim"
    assert payload["id"] == aid


def test_round_trip_merge_law_still_holds_with_tick_split(bank, tmp_path):
    """The full merge-law round trip (test_sync_journal.py's headline scenario) must survive
    the trigger split unchanged: both peers' witnessed uses land."""
    store, tmp = bank
    peer = CardStore(tmp / "peer_rt.db")
    content = "# rt\nboth banks earn this"
    local_id = _plant(store, "lesson:rt", content, scope="alpha")
    peer_id = _plant(peer, "lesson:rt", content, scope="alpha")
    assert local_id == peer_id
    for _ in range(3):
        store.remember_fetch(local_id, depth="spine")
    for _ in range(2):
        peer.remember_fetch(peer_id, depth="spine")
    ops = [o for o in sj.ops_since(peer.conn) if o["tbl"] == "atom_earned"]
    sj.apply_ops(store.conn, ops)
    row = store.conn.execute(
        "SELECT use_count FROM atom_earned WHERE atom_id=?", (local_id,)).fetchone()
    assert row[0] == 5


# ── 5. anti-no-op: prove the assertion bites ─────────────────────────────────────

def test_anti_no_op_tick_assertion_bites_on_a_refattened_trigger(bank):
    """If the tick trigger regresses to carrying content (re-fattened), test 1's assertion
    ('content' not in payload) must FAIL. Prove it by installing a deliberately fat tick
    trigger in place of the real one, observing the failure, then restoring the real one."""
    store, _ = bank
    aid = _plant(store, "lesson:antinoop", "# antinoop\nclaim")

    # Install a hostile, fat TICK trigger that also carries content — simulating a regression.
    store.conn.execute("DROP TRIGGER IF EXISTS trg_sync_atom_earned_tick")
    store.conn.execute(f"""
        CREATE TRIGGER trg_sync_atom_earned_tick AFTER UPDATE OF score, score_history,
            use_count, last_fetch_ts ON atom_earned
        WHEN {sj._MUTE}
        BEGIN
            INSERT INTO sync_journal (tbl, pk, op, payload, scope, ts, origin)
            VALUES ('atom_earned', json_object('atom_id', NEW.atom_id), 'TICK',
                json_object('score', NEW.score, 'use_count', NEW.use_count,
                    'score_history', NEW.score_history, 'content', 'FATTENED-REGRESSION'),
                '', CAST(strftime('%s','now') AS INTEGER), '');
        END;""")
    store.conn.commit()

    before = sj.head(store.conn)
    store.remember_fetch(aid, depth="spine")
    ops = [o for o in _ops(store, before) if o["tbl"] == "atom_earned"]
    payload = json.loads(ops[-1]["payload"])

    # The real assertion from test 1, applied here: it must now FAIL, proving it bites.
    with pytest.raises(AssertionError):
        assert "content" not in payload

    # RESTORE: drop the hostile trigger and re-attach the real one.
    store.conn.execute("DROP TRIGGER IF EXISTS trg_sync_atom_earned_tick")
    store.conn.commit()
    sj.attach(store.conn, force=True)
    assert "trg_sync_atom_earned_tick" in sj.installed_triggers(store.conn)

    before2 = sj.head(store.conn)
    store.remember_fetch(aid, depth="spine")
    ops2 = [o for o in _ops(store, before2) if o["tbl"] == "atom_earned"]
    payload2 = json.loads(ops2[-1]["payload"])
    assert "content" not in payload2, "restore failed — the real trigger should be back"
