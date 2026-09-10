"""Tests for sync_journal — the change-capture organ for two-way bank sync (wave 1).

Hermetic: temp file banks only, never the live ~/.echelon/echelon.db. Earning is done through
the REAL witnessed door (compile_atom_struct + remember_fetch), never by hand-poking scores —
the whole point is proving the TRIGGERS catch what the scattered writers do.

Each test pins one failure mode the gated design must survive (DELTA-SYNC-DESIGN.md):
  - triggers capture inserts + earned-state updates from the real write paths
  - compiled tables (spine/body/sidecar/FTS) are NEVER journaled; recompile stays silent
  - `witness` IS journaled (the one authored field on a compiled table)
  - echo suppression: applying a remote batch does not re-journal (no ping-pong)
  - the merge law: earned weight from BOTH peers survives; byte-identical history entries
    are never content-deduped
  - deletes travel as explicit tombstones
  - a whole-file restore re-mints the generation (trigger-invisible path)
  - scope is stamped so the gateway can filter (no estate leakage)
  - crash mid-batch leaves nothing half-applied; the re-send is idempotent
  - the disabled default: no ECHELON_SYNC, no triggers, no journal rows
"""
import json
import os
import shutil
import sqlite3

import pytest

from echelon_engine.atoms.cards import CardStore
from echelon_engine.atoms import sync_journal as sj


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


# ── capture ───────────────────────────────────────────────────────────────────

def test_attach_installs_triggers_and_generation(bank):
    store, _ = bank
    trigs = sj.installed_triggers(store.conn)
    # 2 per source table (ins/upd), +1 more (ins/tick/upd) for each table in TICK_COLUMNS
    # (the journal-diet delta trigger), + the witness trigger. NO delete triggers: the
    # no-delete law (owner, 2026-08-18) makes sync append-only in both directions.
    expected = len(sj.SOURCE_TABLES) * 2 + len(sj.TICK_COLUMNS) + 1
    assert len(trigs) == expected
    assert "trg_sync_atoms_ins" in trigs
    assert "trg_sync_atoms_tick" in trigs
    assert "trg_sync_atom_earned_tick" in trigs
    assert "trg_sync_spine_witness" in trigs
    assert not [t for t in trigs if t.endswith("_del")]
    assert len(sj.generation(store.conn)) == 32


def test_insert_is_journaled_with_scope(bank):
    store, _ = bank
    aid = store.add_atom("lesson:one", "# one\nthe claim", scope="alpha")
    rows = [o for o in _ops(store) if o["tbl"] == "atoms"]
    assert len(rows) == 1
    assert rows[0]["op"] == "INSERT"
    assert json.loads(rows[0]["pk"])["id"] == aid
    # scope must be stamped — the gateway filters on it so a local bank's 100+ scopes
    # (other clients' estates) never leak into a member's cloud bank
    assert rows[0]["scope"] == "alpha"


def test_witnessed_earn_is_journaled(bank):
    """The real earn path (remember_fetch -> _witness_earn) mutates atom_earned through a
    scattered writer. The trigger is what catches it — this is the whole reason for F1a.

    remember_fetch touches ONLY score/score_history/use_count/last_fetch_ts — a pure weight
    tick — so it journals as TICK (the journal-diet delta trigger), not the fat UPDATE."""
    store, _ = bank
    aid = _plant(store, "lesson:earn", "# earn\nclaim")
    before = sj.head(store.conn)
    store.remember_fetch(aid, depth="body")
    earned_ops = [o for o in _ops(store, before) if o["tbl"] == "atom_earned"]
    assert earned_ops, "witnessed earn did not reach the journal"
    assert earned_ops[-1]["op"] == "TICK"
    payload = json.loads(earned_ops[-1]["payload"])
    assert "content" not in payload
    assert payload["use_count"] == 1
    assert json.loads(payload["score_history"])[-1][2] == "fetch"


def test_fire_lower_is_journaled(bank):
    """fire_lower also touches only score/score_history — a pure tick."""
    store, _ = bank
    aid = _plant(store, "lesson:lower", "# lower\nclaim")
    before = sj.head(store.conn)
    store.fire_lower(aid, "misled me")
    ops = [o for o in _ops(store, before) if o["tbl"] == "atom_earned"]
    assert ops and ops[-1]["op"] == "TICK"
    hist = json.loads(json.loads(ops[-1]["payload"])["score_history"])
    assert hist[-1][2].startswith("fire_lower")


def test_links_journaled_and_resolve_scope_from_source_atom(bank):
    store, _ = bank
    a = _plant(store, "lesson:a", "# a\nclaim", scope="alpha")
    b = _plant(store, "lesson:b", "# b\nclaim", scope="alpha")
    before = sj.head(store.conn)
    store.link(a, b, "refines")
    ops = [o for o in _ops(store, before) if o["tbl"] == "atom_links"]
    assert len(ops) == 1
    assert ops[0]["scope"] == "alpha"   # resolved through the from_id atom


# ── what must NOT be journaled ────────────────────────────────────────────────

def test_compiled_tables_are_never_journaled(bank):
    """A bulk recompile of 7.5k atoms would fire ~15k junk ops if spine/body/sidecar were
    watched. Peers recompile locally from atoms.content instead."""
    store, _ = bank
    aid = _plant(store, "lesson:c", "# c\nclaim")
    before = sj.head(store.conn)
    for _ in range(5):
        store.compile_atom_struct(aid)      # REPLACEs spine + body every time
    tbls = {o["tbl"] for o in _ops(store, before)}
    assert not (tbls & {"atom_spine", "atom_body", "atom_sidecar", "atom_spine_fts"})


def test_witness_is_journaled_as_the_one_authored_exception(bank):
    """witness is parsed from .md frontmatter, NOT derivable from content — a peer that only
    recompiles would blank it. It gets one narrow UPDATE-OF trigger."""
    store, _ = bank
    aid = _plant(store, "lesson:w", "# w\nclaim")
    before = sj.head(store.conn)
    store.set_witness(aid, "owner")
    ops = [o for o in _ops(store, before) if o["tbl"] == "atom_spine"]
    assert len(ops) == 1
    assert ops[0]["op"] == "WITNESS"
    assert json.loads(ops[0]["payload"])["witness"] == "owner"


def test_recompile_after_witness_does_not_refire_witness(bank):
    """compile_atom_struct carries witness forward with the SAME value; the trigger's
    NEW<>OLD guard must keep that silent."""
    store, _ = bank
    aid = _plant(store, "lesson:w2", "# w2\nclaim")
    store.set_witness(aid, "execution")
    before = sj.head(store.conn)
    store.compile_atom_struct(aid)
    assert not [o for o in _ops(store, before) if o["op"] == "WITNESS"]


def test_attach_tolerates_a_bank_missing_a_table(tmp_path):
    """An OLDER bank predates some tables — box4's live bank has no wrap_reviews. CREATE
    TRIGGER on a missing table raises 'no such table' and would abort attach() half-way,
    leaving triggers partially installed. Caught 2026-08-02 against a copy of the real
    cloud bank, before deploying."""
    db = tmp_path / "old.db"
    conn = sqlite3.connect(str(db))
    # a minimal, older-shaped bank: atoms + atom_earned only
    conn.executescript("""
        CREATE TABLE atoms (id TEXT PRIMARY KEY, coordinate TEXT NOT NULL, content TEXT NOT NULL,
            score REAL DEFAULT 100.0, score_history TEXT DEFAULT '[]', use_count INTEGER DEFAULT 0,
            born_from TEXT DEFAULT '', ts INTEGER NOT NULL, scope TEXT DEFAULT '',
            kind TEXT DEFAULT '', valence REAL DEFAULT 0.0, arousal REAL DEFAULT 0.0);
        CREATE TABLE atom_earned (atom_id TEXT PRIMARY KEY, score REAL DEFAULT 100.0,
            use_count INTEGER DEFAULT 0, score_history TEXT DEFAULT '[]',
            last_fetch_ts INTEGER DEFAULT 0, scope TEXT DEFAULT '');
    """)
    conn.commit()
    assert sj.attach(conn, force=True) is True

    trigs = sj.installed_triggers(conn)
    assert any("atoms" in t for t in trigs), "present tables must still be watched"
    assert not any("wrap_reviews" in t for t in trigs), "watched a table that does not exist"

    conn.execute("INSERT INTO atoms (id,coordinate,content,ts,scope) VALUES "
                 "('x1','lesson:x','# x',1,'alpha')")
    conn.commit()
    assert len(sj.ops_since(conn)) == 1, "capture broken on the older bank"
    conn.close()


def test_impressions_are_excluded(bank):
    """Telemetry, bulk-evicted — journaling it is tombstone noise."""
    store, _ = bank
    assert "impressions" not in sj.SOURCE_TABLES
    trigs = sj.installed_triggers(store.conn)
    assert not any("impressions" in t for t in trigs)


# ── echo suppression ──────────────────────────────────────────────────────────

def test_apply_does_not_rejournal(bank, tmp_path):
    """THE ping-pong guard. Without the temp-table sentinel, applying a peer's batch fires
    the local triggers and the op bounces back forever."""
    store, _ = bank
    peer = CardStore(tmp_path / "peer.db")
    aid = peer.add_atom("lesson:remote", "# remote\nfrom the other side", scope="alpha")
    ops = [o for o in sj.ops_since(peer.conn) if o["tbl"] == "atoms"]

    before = sj.head(store.conn)
    sj.apply_ops(store.conn, ops)
    after_ops = _ops(store, before)

    assert store.conn.execute("SELECT COUNT(*) FROM atoms WHERE id=?", (aid,)).fetchone()[0] == 1
    assert after_ops == [], f"applied ops re-journaled (echo loop): {after_ops}"


def test_sentinel_is_connection_local(bank, tmp_path):
    """Muting the applier must not mute a concurrent writer on another connection."""
    store, tmp = bank
    other = CardStore(tmp / "echelon.db")     # second connection, same file
    with sj._Muted(store.conn):
        other.add_atom("lesson:concurrent", "# c\nclaim", scope="alpha")
    rows = [o for o in _ops(store) if o["tbl"] == "atoms"]
    assert len(rows) == 1, "a concurrent connection's write was wrongly muted"


# ── the merge law ─────────────────────────────────────────────────────────────

def test_merge_preserves_both_peers_earned_weight(bank, tmp_path):
    """Same atom earned on BOTH sides between syncs. Neither side's witnessed use may be lost —
    that is the whole reason the cloud bank could not simply be overwritten."""
    store, tmp = bank
    peer = CardStore(tmp / "peer.db")
    content = "# shared\nthe same lesson, both banks"
    local_id = _plant(store, "lesson:shared", content, scope="alpha")
    peer_id = _plant(peer, "lesson:shared", content, scope="alpha")
    assert local_id == peer_id, "content-addressed ids must match across banks"

    for _ in range(3):
        store.remember_fetch(local_id, depth="spine")
    for _ in range(2):
        peer.remember_fetch(peer_id, depth="spine")

    ops = [o for o in sj.ops_since(peer.conn) if o["tbl"] == "atom_earned"]
    sj.apply_ops(store.conn, ops)

    row = store.conn.execute(
        "SELECT use_count, score_history FROM atom_earned WHERE atom_id=?", (local_id,)).fetchone()
    hist = json.loads(row[1])
    fetches = [h for h in hist if len(h) > 2 and h[2] == "fetch"]
    assert len(fetches) == 5, f"expected 3 local + 2 remote fetch events, got {len(fetches)}"


def test_history_merge_never_content_dedupes(bank):
    """The live bank holds byte-identical entries (two fetches in the same second), so value
    dedup would erase real earned weight. With both sides' entries carrying their witnessing
    bank, two peers that each earned in the same second keep BOTH sets — 2 local + 3 remote."""
    local = json.dumps([[100, 2.0, "fetch"], [100, 2.0, "fetch"]])
    remote = json.dumps([[100, 2.0, "fetch"], [100, 2.0, "fetch"], [100, 2.0, "fetch"]])
    merged = json.loads(sj._merge_history(local, remote,
                                          local_origin="bankA", remote_origin="bankB"))
    assert len(merged) == 5, "a peer's identical-looking earns were absorbed as duplicates"
    assert sum(1 for e in merged if sj._entry_origin(e) == "bankA") == 2
    assert sum(1 for e in merged if sj._entry_origin(e) == "bankB") == 3


def test_history_merge_drops_the_same_event_resynced(bank):
    """The other half of the law: re-applying the SAME peer's batch must not double-count.
    Same origin + same (ts,delta,src) = the same event, already recorded."""
    remote = json.dumps([[100, 2.0, "fetch"], [100, 2.0, "fetch"]])
    once = sj._merge_history("[]", remote, local_origin="bankA", remote_origin="bankB")
    twice = sj._merge_history(once, remote, local_origin="bankA", remote_origin="bankB")
    assert len(json.loads(twice)) == 2, "a re-applied batch double-counted earned weight"


def test_use_count_derives_from_merged_history(bank, tmp_path):
    """use_count is DERIVED from the merged fetch events — max() would discard one peer's
    uses, a blind sum would double-count a re-applied batch."""
    store, tmp = bank
    peer = CardStore(tmp / "peer_uc.db")
    content = "# derive\nclaim"
    aid = _plant(store, "lesson:derive", content, scope="alpha")
    _plant(peer, "lesson:derive", content, scope="alpha")
    for _ in range(3):
        store.remember_fetch(aid, depth="spine")
    for _ in range(2):
        peer.remember_fetch(aid, depth="spine")
    ops = [o for o in sj.ops_since(peer.conn) if o["tbl"] == "atom_earned"]
    sj.apply_ops(store.conn, ops)
    sj.apply_ops(store.conn, ops)          # re-apply must not inflate
    uc = store.conn.execute(
        "SELECT use_count FROM atom_earned WHERE atom_id=?", (aid,)).fetchone()[0]
    assert uc == 5, f"expected 3 local + 2 remote witnessed uses, got {uc}"


def test_history_merge_is_commutative_in_length(bank):
    a = json.dumps([[1, 2.0, "fetch"], [5, -10.0, "fire_lower:x"]])
    b = json.dumps([[3, 2.0, "fetch"]])
    assert len(json.loads(sj._merge_history(a, b))) == len(json.loads(sj._merge_history(b, a)))


def test_use_count_never_regresses_on_merge(bank, tmp_path):
    store, tmp = bank
    peer = CardStore(tmp / "peer2.db")
    content = "# uc\nclaim"
    aid = _plant(store, "lesson:uc", content, scope="alpha")
    _plant(peer, "lesson:uc", content, scope="alpha")
    for _ in range(4):
        store.remember_fetch(aid, depth="spine")
    peer.remember_fetch(aid, depth="spine")
    ops = [o for o in sj.ops_since(peer.conn) if o["tbl"] == "atom_earned"]
    sj.apply_ops(store.conn, ops)
    uc = store.conn.execute(
        "SELECT use_count FROM atom_earned WHERE atom_id=?", (aid,)).fetchone()[0]
    assert uc >= 4, "a lower remote use_count clobbered the local one"


def test_apply_is_idempotent(bank, tmp_path):
    """A crash after apply but before the cursor ack re-sends the same batch."""
    store, tmp = bank
    peer = CardStore(tmp / "peer3.db")
    aid = peer.add_atom("lesson:idem", "# idem\nclaim", scope="alpha")
    ops = [o for o in sj.ops_since(peer.conn) if o["tbl"] == "atoms"]
    sj.apply_ops(store.conn, ops)
    sj.apply_ops(store.conn, ops)
    sj.apply_ops(store.conn, ops)
    n = store.conn.execute("SELECT COUNT(*) FROM atoms WHERE id=?", (aid,)).fetchone()[0]
    assert n == 1


def test_apply_is_all_or_nothing(bank, tmp_path):
    """A malformed op mid-batch must roll the WHOLE batch back — a half-applied batch with an
    acked cursor would lose the tail forever."""
    store, tmp = bank
    peer = CardStore(tmp / "peer4.db")
    peer.add_atom("lesson:good", "# good\nclaim", scope="alpha")
    ops = [o for o in sj.ops_since(peer.conn) if o["tbl"] == "atoms"]
    # Corrupt PK json — this raises inside the batch, after the good op has been written.
    ops.append({"seq": 999, "tbl": "atoms", "op": "UPDATE",
                "pk": "{not json at all", "payload": "{}",
                "scope": "alpha", "ts": 0, "origin": ""})
    before = store.conn.execute("SELECT COUNT(*) FROM atoms").fetchone()[0]
    with pytest.raises(Exception):
        sj.apply_ops(store.conn, ops)
    after = store.conn.execute("SELECT COUNT(*) FROM atoms").fetchone()[0]
    assert after == before, "a failed batch left rows behind"


def test_narrow_update_for_unknown_row_is_skipped_not_corrupt(bank, tmp_path):
    """An UPDATE op for a row this bank has never seen cannot be materialized — atoms has
    NOT NULL columns with no default. It must be skipped LOUDLY, never written as a half-row
    (an INSERT OR IGNORE missing them fails the constraint and silently writes nothing)."""
    store, tmp = bank
    ops = [{"seq": 1, "tbl": "atoms", "op": "UPDATE", "pk": '{"id":"never_seen"}',
            "payload": '{"score": 120.0}', "scope": "alpha", "ts": 0, "origin": ""}]
    res = sj.apply_ops(store.conn, ops)
    assert res["skipped"] == 1
    assert res.get("incomplete"), "an unmaterializable row was applied silently"
    assert store.conn.execute(
        "SELECT COUNT(*) FROM atoms WHERE id='never_seen'").fetchone()[0] == 0


# ── the no-delete law (owner, 2026-08-18) ─────────────────────────────────────
# Sync is APPEND-ONLY IN BOTH DIRECTIONS. Replaces test_delete_travels_as_a_tombstone, which
# asserted the original tombstone design. A tombstone is a destructive instruction that
# outlives the judgement that made it, and the far side cannot distinguish a deliberate
# retirement from an accident — the 2026-08-18 wipe destroyed 8,479 atoms in one command.

def test_local_delete_emits_no_tombstone(bank, tmp_path):
    """A local delete stays LOCAL: nothing is journaled, so nothing can propagate."""
    store, tmp = bank
    peer = CardStore(tmp / "peer5.db")
    content = "# gone\nclaim"
    aid = _plant(peer, "lesson:gone", content, scope="alpha")
    before = sj.head(peer.conn)
    peer.conn.execute("DELETE FROM atoms WHERE id=?", (aid,))
    peer.conn.commit()
    ops = [o for o in sj.ops_since(peer.conn, before) if o["tbl"] == "atoms"]
    assert ops == [], f"a delete was journaled and would propagate: {ops}"


def test_inbound_delete_is_refused_not_applied(bank, tmp_path):
    """A DELETE op from a peer — however it was minted — never destroys a local row.

    Covers tombstones left in an older journal from before the law: the applier refuses by OP,
    not by who emitted it, so dropping the triggers is belt AND braces.
    """
    store, tmp = bank
    aid = _plant(store, "lesson:keepme", "# keep\nclaim", scope="alpha")
    assert store.conn.execute(
        "SELECT COUNT(*) FROM atoms WHERE id=?", (aid,)).fetchone()[0] == 1

    hostile = [{"tbl": "atoms", "pk": json.dumps({"id": aid}), "op": "DELETE",
                "payload": "{}", "scope": "alpha", "ts": 0, "origin": "peer"}]
    res = sj.apply_ops(store.conn, hostile)

    assert store.conn.execute(
        "SELECT COUNT(*) FROM atoms WHERE id=?", (aid,)).fetchone()[0] == 1, \
        "an inbound DELETE destroyed a local atom — the no-delete law is broken"
    assert res.get("delete_refused") == 1
    assert res.get("delete", 0) == 0
    assert any(aid in p for p in res.get("refused_pks", []))


def test_attach_drops_legacy_delete_triggers(bank):
    """Re-attaching an OLD bank is a real migration: CREATE-IF-NOT-EXISTS cannot remove a
    trigger, so attach() must drop pre-law delete triggers explicitly."""
    store, _ = bank
    store.conn.execute("""
        CREATE TRIGGER IF NOT EXISTS trg_sync_atoms_del AFTER DELETE ON atoms
        BEGIN
            INSERT INTO sync_journal (tbl, pk, op, payload, scope, ts, origin)
            VALUES ('atoms', json_object('id', OLD.id), 'DELETE', '{}', '', 0, '');
        END;""")
    store.conn.commit()
    assert "trg_sync_atoms_del" in sj.installed_triggers(store.conn)

    sj.attach(store.conn, force=True)
    assert "trg_sync_atoms_del" not in sj.installed_triggers(store.conn)


# ── the restore guard ─────────────────────────────────────────────────────────

def test_restore_remints_generation(bank, tmp_path):
    """A whole-file copy is trigger-invisible and rewinds seq under peers' cursors. The
    generation stamp is the only detector."""
    store, tmp = bank
    _plant(store, "lesson:r", "# r\nclaim")
    gen_before = sj.generation(store.conn)
    snap = tmp / "snap.db"
    shutil.copy2(tmp / "echelon.db", snap)
    store.conn.close()
    shutil.copy2(snap, tmp / "echelon.db")
    gen_after = sj.remint_generation(tmp / "echelon.db")
    assert gen_after != gen_before
    conn = sqlite3.connect(str(tmp / "echelon.db"))
    assert conn.execute("SELECT COUNT(*) FROM sync_peers").fetchone()[0] == 0
    conn.close()


def test_peer_cursor_roundtrip(bank):
    store, _ = bank
    sj.set_peer_state(store.conn, "hub", peer_generation="abc", pulled_through=42)
    st = sj.peer_state(store.conn, "hub")
    assert st["pulled_through"] == 42 and st["peer_generation"] == "abc"
    sj.set_peer_state(store.conn, "hub", pushed_through=7)
    st = sj.peer_state(store.conn, "hub")
    assert st["pulled_through"] == 42 and st["pushed_through"] == 7


def test_scope_filter_isolates_estates(bank):
    """A local bank holds 100+ scopes including other clients' estates. Pull/push MUST be
    scope-filtered or a member's cloud bank receives confidential material."""
    store, _ = bank
    store.add_atom("lesson:alpha", "# a\nclaim", scope="alpha")
    store.add_atom("lesson:beta", "# b\nclaim", scope="beta")
    alpha = _ops(store, scope="alpha")
    assert alpha and all(o["scope"] == "alpha" for o in alpha)
    assert not any(json.loads(o["pk"]).get("id") for o in alpha
                   if o["scope"] == "beta")


# ── the disabled default ──────────────────────────────────────────────────────

def test_disabled_by_default(tmp_path, monkeypatch):
    monkeypatch.delenv("ECHELON_SYNC", raising=False)
    store = CardStore(tmp_path / "off.db")
    assert sj.installed_triggers(store.conn) == []
    store.add_atom("lesson:off", "# off\nclaim", scope="alpha")
    with pytest.raises(sqlite3.OperationalError):
        store.conn.execute("SELECT COUNT(*) FROM sync_journal").fetchone()


def test_detach_keeps_journal_rows(bank):
    store, _ = bank
    store.add_atom("lesson:keep", "# k\nclaim", scope="alpha")
    n_before = store.conn.execute("SELECT COUNT(*) FROM sync_journal").fetchone()[0]
    dropped = sj.detach(store.conn)
    assert dropped > 0
    assert sj.installed_triggers(store.conn) == []
    assert store.conn.execute("SELECT COUNT(*) FROM sync_journal").fetchone()[0] == n_before


def test_compact_only_drops_ops_every_peer_pulled(bank):
    store, _ = bank
    store.add_atom("lesson:comp", "# c\nclaim", scope="alpha")
    sj.set_peer_state(store.conn, "hub", pulled_through=0)
    assert sj.compact_journal(store.conn, keep_days=0) == 0, "dropped ops no peer has pulled"
