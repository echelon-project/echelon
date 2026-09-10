"""Tests for slice-1.5 bank hygiene (hygiene.py) — the verbatim-dup dedup verb.

Hermetic: a temp CardStore file bank (no live ~/.echelon/echelon.db, no LM Studio), earning done
HONESTLY through the real witnessed door (compile_atom_struct + remember_fetch — no hand-poked
scores), the planner reading it back over its own sqlite ro connection. Pins the spec's six bullets
PLUS the gate's 2026-07-08 refusal conditions:
  1. cluster of 3 BODY-verbatim copies across scopes -> canonical = heaviest by v2 earned weight;
     subsumes edges written; weights merged (use SUM, score MAX); honest before/after receipt.
  2. a doctrine-echo-shaped pair (different wording, no slug match) is untouched.
  3. idempotency: a re-plan after --apply plans 0 actions.
  4. a dry-run mutates nothing (per-table sha256 identical).
  5. within-scope duplicates are the same operation.
  6. apply without a VALID fresh backup refuses (absent / stale / junk-content all refuse).
Gate negatives: same-slug-DIFFERENT-body must NOT cluster (the exact refusal failure mode); a
disclaimed duplicate's earned score is never merged into the canonical's max (anti-laundering);
backup CONTENT is validated, not just glob+mtime. Plus the rule-5 orphan path (standalone
memory:<scope>:* empty-scope hash-id rows -> dispute tombstone; other empty-scope rows untouched).
"""
import json
import os
import shutil
import sqlite3
import time

import pytest

from echelon_engine.atoms.cards import CardStore
from echelon_engine.atoms.hygiene import HygienePlanner, table_sha256, _PROOF_TABLES, EDGE_REL


@pytest.fixture(autouse=True)
def _legacy_scope_omit(monkeypatch):
    """These fixtures predate the empty-scope guard (2026-07-09) and plant atoms with no
    scope= on real-scope-shaped heads. The guard's law stays strict (the gate refused
    allowlisting fixture heads — memory:* IS the orphan mechanism); tests opt in HERE,
    per-test and auto-reverted, via the deliberate escape hatch."""
    monkeypatch.setenv("ECHELON_LEGACY_SCOPE_OMIT", "1")


@pytest.fixture
def bank(tmp_path):
    """A temp file bank + a planner wired to temp plan/backup paths."""
    db = tmp_path / "echelon.db"
    store = CardStore(db)
    planner = HygienePlanner(db_path=db,
                             plan_path=tmp_path / "hygiene-plan.json",
                             backup_dir=tmp_path / "backups",
                             receipt_path=tmp_path / "hygiene-receipt.json")
    return store, planner, tmp_path


def _atom(store, scope, slug, claim, content=None, coordinate=None):
    """Mint an atom + its spine the way ingest would: real add_atom, real compile, then pin the
    spine's slug/claim so the candidate-generator keys are controlled by the test. NOTE: the
    QUALIFIER is the body (atoms.content) — verbatim twins must share `content`."""
    aid = store.add_atom(coordinate or f"{scope}:{slug}", content or claim,
                         scope=scope, kind="lesson")
    store.compile_atom_struct(aid)
    store.conn.execute("UPDATE atom_spine SET slug=?, claim=?, scope=? WHERE atom_id=?",
                       (slug, claim, scope, aid))
    store.conn.commit()
    return aid


def _earn(store, atom_id, times):
    """`times` WITNESSED uses through the real door (each fetch: +EARN_DELTA, use_count+1)."""
    for _ in range(times):
        store.remember_fetch(atom_id)


def _fresh_backup(planner, store):
    """Satisfy the backup gate HONESTLY: a real sqlite snapshot of the bank, newer than the plan.
    The gate validates CONTENT (size sanity + PRAGMA integrity_check), so a junk file no longer
    passes — see test_backup_content_is_validated."""
    planner.backup_dir.mkdir(parents=True, exist_ok=True)
    store.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")   # fold the WAL into the main file first
    b = planner.backup_dir / f"echelon_{time.strftime('%Y%m%d_%H%M%S')}.db"
    shutil.copy2(planner.db_path, b)
    # ensure strictly newer than the plan even on coarse mtime clocks
    os.utime(b, (time.time() + 5, time.time() + 5))
    return b


_CLAIM = "the silent floor swallowed the auth error and degraded lexical without a log line"


# ── 1. the canonical cluster: 3 body-verbatim copies across scopes ──────────────────────────────────
def test_cross_scope_cluster_canonical_merge(bank):
    store, planner, _ = bank
    # identical BODY (content=_CLAIM) in three scopes — the filing-copy shape
    a1 = _atom(store, "echelon", "silent-floor-trap", _CLAIM)
    a2 = _atom(store, "flux", "silent-floor-trap", _CLAIM)
    a3 = _atom(store, "mol", "silent-floor-trap", _CLAIM)
    _earn(store, a1, 3)     # some weight
    _earn(store, a2, 10)    # the heaviest -> must be canonical
    # a3 earns nothing (born neutral)

    plan = planner.plan()
    assert plan["cluster_count"] == 1
    c = plan["clusters"][0]
    assert c["canonical"]["id"] == a2                       # heaviest v2 earned weight wins
    assert {a["duplicate"] for a in c["actions"]} == {a1, a3}
    assert all(a["relation"] == EDGE_REL for a in c["actions"])
    # HONEST receipt: before = the canonical's CURRENT live earned state; after = the merged
    # projection (use SUM over members, score MAX) — a real delta, not the old circular max==max.
    assert c["weight_before"]["canonical_use_count"] == 10
    assert c["weight_before"]["members_use_count_sum"] == 13
    assert c["weight_after_projection"]["canonical_use_count"] == 13
    tot = plan["weight_totals"]
    assert tot["merged_in_delta"]["use_count"] == 3          # what the merge MOVES onto canonicals

    _fresh_backup(planner, store)
    rep = planner.apply()
    assert rep["ok"] and rep["applied"]["subsume"] == 2 and rep["applied"]["merge"] == 2

    # edges written: canonical --subsumes--> each duplicate, live
    edges = store.conn.execute(
        "SELECT from_id, to_id FROM atom_links WHERE relation=? AND superseded_on=0",
        (EDGE_REL,)).fetchall()
    assert {(e["from_id"], e["to_id"]) for e in edges} == {(a2, a1), (a2, a3)}
    # weights merged onto the canonical, nothing lost
    row = store.conn.execute("SELECT score, use_count FROM atom_earned WHERE atom_id=?", (a2,)).fetchone()
    assert row["use_count"] == 13                            # 10 + 3 + 0 (SUM)
    all_scores = {r["atom_id"]: r["score"] for r in store.conn.execute(
        "SELECT atom_id, score FROM atom_earned")}
    assert row["score"] == max(all_scores.values())          # MAX, not sum — no laundering
    # duplicates' rows still exist untouched (subsume never deletes)
    assert store.conn.execute("SELECT COUNT(*) FROM atoms").fetchone()[0] == 3
    assert all(rep_c["no_loss"] for rep_c in rep["cluster_receipts"])


# ── 2. a doctrine-echo-shaped pair stays untouched ──────────────────────────────────────────────────
def test_doctrine_echo_pair_untouched(bank):
    store, planner, _ = bank
    _atom(store, "echelon", "memory-is-a-weight-adjustor",
          "a memory stores the seed that re-creates an understanding when read")
    _atom(store, "flux", "recall-reshapes-the-model",
          "recall re-shapes the model rather than merely informing it with records")
    plan = planner.plan()
    assert plan["cluster_count"] == 0
    assert plan["action_count"] == 0


# ── gate negative: same slug, DIFFERENT body must NOT cluster (the refusal core) ────────────────────
def test_same_slug_different_body_does_not_cluster(bank):
    store, planner, _ = bank
    # a version chain: same slug + same claim in two scopes, but the BODIES have drifted apart —
    # exactly the 296/628 false-cluster shape the gate refused. Slug/claim may only GENERATE the
    # candidate pair; the body comparison must reject it.
    _atom(store, "echelon", "flux-the-ui-tier", "flux is the ui tier of the stack",
          content="flux is the ui tier of the stack\n\nv1 body: five lines of early notes")
    _atom(store, "flux", "flux-the-ui-tier", "flux is the ui tier of the stack",
          content="flux is the ui tier of the stack\n\nv7 body: twenty sections, the whole "
                  "deploy map, mirror eye, resize protocol — a 24KB descendant, not a copy")
    plan = planner.plan()
    assert plan["cluster_count"] == 0                        # a version chain is NOT verbatim
    assert plan["action_count"] == 0


# ── 3. idempotency: a second run after --apply plans 0 actions ──────────────────────────────────────
def test_second_run_after_apply_plans_zero(bank):
    store, planner, _ = bank
    a1 = _atom(store, "echelon", "dup", _CLAIM)
    a2 = _atom(store, "flux", "dup", _CLAIM)
    _earn(store, a1, 5)
    plan = planner.plan()
    assert plan["action_count"] == 1
    _fresh_backup(planner, store)
    assert planner.apply()["ok"]
    plan2 = planner.plan()
    assert plan2["action_count"] == 0
    assert plan2["cluster_count"] == 0


# ── 4. the dry-run mutates nothing (per-table sha256) ───────────────────────────────────────────────
def test_dry_run_is_read_only(bank):
    store, planner, _ = bank
    a1 = _atom(store, "echelon", "dup", _CLAIM)
    _atom(store, "flux", "dup", _CLAIM)
    _earn(store, a1, 4)
    ro = sqlite3.connect(f"file:{planner.db_path.as_posix()}?mode=ro", uri=True)
    before = {t: table_sha256(ro, t) for t in _PROOF_TABLES}
    plan = planner.plan()
    after = {t: table_sha256(ro, t) for t in _PROOF_TABLES}
    ro.close()
    assert before == after                                   # the slice-1 read-only law, re-proven
    assert plan["read_only_proof"]["identical_after_dry_run"] is True
    assert plan["action_count"] == 1                         # it DID find work — just didn't do it


# ── 5. within-scope duplicates are the same operation ───────────────────────────────────────────────
def test_within_scope_duplicates(bank):
    store, planner, _ = bank
    # same scope, IDENTICAL body, distinct rows: id = hash(content + coordinate-domain + kind), so
    # identical bodies coexist when the coordinate DOMAIN differs (the real mis-filing shape —
    # ingest planting the same .md under both `epsilon-co:...` and `memory:epsilon-co:...`).
    body = "auth is google oauth only no local passwords"
    a1 = _atom(store, "epsilon-co", "auth-is-google-oauth", body, content=body,
               coordinate="epsilon-co:auth-is-google-oauth")
    a2 = _atom(store, "epsilon-co", "auth-is-google-oauth", body, content=body,
               coordinate="memory:epsilon-co:auth_is_google_oauth")
    assert a1 != a2
    _earn(store, a1, 6)
    plan = planner.plan(scope="epsilon-co")
    assert plan["cluster_count"] == 1
    c = plan["clusters"][0]
    assert c["cross_scope"] is False
    assert c["canonical"]["id"] == a1 and c["actions"][0]["duplicate"] == a2
    _fresh_backup(planner, store)
    rep = planner.apply()
    assert rep["ok"]
    row = store.conn.execute("SELECT use_count FROM atom_earned WHERE atom_id=?", (a1,)).fetchone()
    assert row["use_count"] == 6                              # 6 + 0, preserved


# ── anti-laundering: a disclaimed duplicate's earned score never rides the merge ────────────────────
def test_disclaimed_duplicate_score_not_merged(bank):
    store, planner, _ = bank
    a1 = _atom(store, "echelon", "dup", _CLAIM)
    a2 = _atom(store, "flux", "dup", _CLAIM)
    _earn(store, a1, 2)      # canonical-to-be: modest honest weight (score 104, use 2)
    _earn(store, a2, 20)     # the dup earns HIGH (score 140) ...
    store.dispute("atoms", a2, reason="stale — proven wrong after the earn")   # ... then is disclaimed
    plan = planner.plan()
    assert plan["cluster_count"] == 1
    c = plan["clusters"][0]
    assert c["canonical"]["id"] == a1                         # the disclaimed row ranks at its re-base
    act = c["actions"][0]
    assert act["duplicate"] == a2
    assert act["merge"]["score_candidate"] is None            # the stale earned score is EXCLUDED
    assert act["merge"]["use_count_add"] == 20                # counts are counts — they still sum
    _fresh_backup(planner, store)
    rep = planner.apply()
    assert rep["ok"]
    row = store.conn.execute("SELECT score, use_count FROM atom_earned WHERE atom_id=?", (a1,)).fetchone()
    assert row["use_count"] == 22
    assert row["score"] < 140.0                               # the lie's weight was NOT laundered in
    assert row["score"] == pytest.approx(104.0)               # canonical keeps its own honest earn


# ── 6. apply without a VALID fresh backup refuses ───────────────────────────────────────────────────
def test_apply_without_backup_refuses(bank):
    store, planner, tmp = bank
    a1 = _atom(store, "echelon", "dup", _CLAIM)
    _atom(store, "flux", "dup", _CLAIM)
    _earn(store, a1, 2)
    planner.plan()
    rep = planner.apply()                                     # no backup dir at all
    assert rep.get("refused") and not rep.get("ok")
    # a STALE backup (taken before the plan) must also refuse — even if its content is valid.
    # Freshness is max(mtime, ctime): copy2 preserves the SOURCE db's mtime (so mtime alone
    # says when the bank was last written, not when the snapshot was taken — the live gate
    # refused a 30-min-fresh backup over this, 2026-07-09), and ctime is creation time, which
    # a test cannot backdate. So make the backup genuinely older by BOTH clocks: push the
    # PLAN's mtime into the future instead of fabricating an impossible file state.
    planner.backup_dir.mkdir(parents=True, exist_ok=True)
    store.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    stale = planner.backup_dir / "echelon_20200101_000000.db"
    shutil.copy2(planner.db_path, stale)
    os.utime(planner.plan_path, (time.time() + 3600, time.time() + 3600))
    rep2 = planner.apply()
    assert rep2.get("refused") and not rep2.get("ok")
    os.utime(planner.plan_path, None)                         # restore, keep later legs honest
    # nothing was written by the refusals
    assert store.conn.execute(
        "SELECT COUNT(*) FROM atom_links WHERE relation=?", (EDGE_REL,)).fetchone()[0] == 0


# ── gate condition 4: backup CONTENT is validated, not just glob+mtime ──────────────────────────────
def test_backup_content_is_validated(bank):
    store, planner, _ = bank
    a1 = _atom(store, "echelon", "dup", _CLAIM)
    _atom(store, "flux", "dup", _CLAIM)
    _earn(store, a1, 2)
    planner.plan()
    # a FRESH but junk backup (the 6-byte file that passed the old gate) must now refuse
    planner.backup_dir.mkdir(parents=True, exist_ok=True)
    junk = planner.backup_dir / f"echelon_{time.strftime('%Y%m%d_%H%M%S')}.db"
    junk.write_bytes(b"backup")
    os.utime(junk, (time.time() + 5, time.time() + 5))
    rep = planner.apply()
    assert rep.get("refused") and not rep.get("ok")
    # a fresh backup that is a valid sqlite db but a SLIVER of the bank must also refuse
    tiny = planner.backup_dir / f"echelon_{time.strftime('%Y%m%d_%H%M%S')}_tiny.db"
    # size gate fires first (a real empty db is ~4KB vs the bank's WAL-checkpointed size)
    c = sqlite3.connect(tiny)
    c.execute("CREATE TABLE t (x)"); c.commit(); c.close()
    os.utime(tiny, (time.time() + 5, time.time() + 5))
    if tiny.stat().st_size < planner.db_path.stat().st_size // 2:
        rep_tiny = planner.apply()
        assert rep_tiny.get("refused") and not rep_tiny.get("ok")
    # a REAL snapshot passes and apply proceeds
    _fresh_backup(planner, store)
    rep_ok = planner.apply()
    assert rep_ok["ok"]


# ── rule 8 regression: a TOMBSTONED subsume edge is a receipt, not an invitation to re-plan ─────────
def test_tombstoned_subsume_edge_vetoes_replan(bank):
    """The 2026-08-02 WEIGHT-LOSS rollback, reproduced end to end (fix: c11531a).

    Two organs share the edge with opposite semantics: hygiene reads a live `subsumes` edge as
    "already merged" (its idempotency ledger), while the immune `heal` tombstones an edge pointing
    at a disclaimed orphan. Tombstoning erases hygiene's RECEIPT but not the MERGE — and because
    link()'s INSERT OR IGNORE keys on (from,to,relation), the dead row still owns the PK, so apply
    can never re-draw the edge. Re-planning that pair therefore made apply skip the merge while the
    plan promised one, and the no-loss verifier rolled the WHOLE plan back (receipt ok:false, zero
    writes). The planner must treat a tombstoned pair as settled-by-intent."""
    store, planner, _ = bank
    a1 = _atom(store, "echelon", "tombstoned-receipt", _CLAIM)
    a2 = _atom(store, "flux", "tombstoned-receipt", _CLAIM)
    _earn(store, a1, 3)
    _earn(store, a2, 6)          # heaviest -> canonical

    # round 1: the real merge happens and writes the live receipt edge
    planner.plan()
    _fresh_backup(planner, store)
    rep1 = planner.apply()
    assert rep1["ok"] and rep1["applied"]["subsume"] == 1
    merged_use = store.conn.execute(
        "SELECT use_count FROM atom_earned WHERE atom_id=?", (a2,)).fetchone()["use_count"]
    assert merged_use == 9       # 6 + 3 merged onto the canonical

    # heal fires: the receipt edge is TOMBSTONED (the PK row survives — that is the whole trap)
    store.conn.execute(
        "UPDATE atom_links SET superseded_on=? WHERE relation=? AND from_id=? AND to_id=?",
        (int(time.time()), EDGE_REL, a2, a1))
    store.conn.commit()
    assert store.conn.execute(
        "SELECT COUNT(*) FROM atom_links WHERE relation=? AND superseded_on=0",
        (EDGE_REL,)).fetchone()[0] == 0                      # no LIVE receipt remains

    # round 2: the pair is settled-by-intent — planned as 0 actions, NOT re-merged
    plan2 = planner.plan()
    assert plan2["action_count"] == 0
    for c in plan2["clusters"]:
        for m in c["members"]:
            if m["id"] == a1:
                assert m["unsubsumed_by_intent"] is True     # reported, not silently dropped

    # and the apply that used to roll back now succeeds with nothing to do — no double-count
    _fresh_backup(planner, store)
    rep2 = planner.apply()
    assert rep2["ok"] and rep2["applied"]["subsume"] == 0 and rep2["applied"]["merge"] == 0
    still = store.conn.execute(
        "SELECT use_count FROM atom_earned WHERE atom_id=?", (a2,)).fetchone()["use_count"]
    assert still == merged_use   # weight neither lost to a rollback nor double-counted


# ── rule 5: standalone empty-scope hash-id orphans -> tombstone path, not subsume ───────────────────
def test_empty_scope_orphan_tombstoned_not_subsumed(bank):
    store, planner, _ = bank
    real = _atom(store, "epsilon-co", "cookie-secure-trap",
                 "secure cookies break behind the local http proxy set samesite lax")
    # orphan #1: body-identical twin of the real atom — but empty-scope rows never cluster;
    # only the standalone memory:<live-scope>:* leg may catch it
    orphan = store.add_atom("memory:epsilon-co:cookie_secure_trap",
                            "secure cookies break behind the local http proxy set samesite lax",
                            scope="", kind="lesson")
    store.compile_atom_struct(orphan)
    store.conn.execute("UPDATE atom_spine SET slug=?, claim=?, scope='' WHERE atom_id=?",
                       (orphan[:12],   # the hash-id slug shape the mining run found
                        "secure cookies break behind the local http proxy set samesite lax", orphan))
    # orphan #2: NO verbatim twin at all (the real mining shape: the scoped version was re-worded)
    lone = store.add_atom("memory:epsilon-co:prod_db_asyncmy_tls_windows_trap",
                          "asyncmy TLS handshake throws WinError 87 on Windows even to localhost",
                          scope="", kind="lesson")
    store.compile_atom_struct(lone)
    store.conn.execute("UPDATE atom_spine SET slug=?, scope='' WHERE atom_id=?", (lone[:12], lone))
    # a protected empty-scope SYSTEM row (the persona:dev shape the gate defended) — must be ignored
    persona = store.add_atom("persona:dev", "the dev persona's self seed", scope="", kind="note")
    store.compile_atom_struct(persona)
    store.conn.execute("UPDATE atom_spine SET slug=?, scope='' WHERE atom_id=?", (persona[:12], persona))
    store.conn.commit()

    plan = planner.plan()
    assert {o["atom_id"] for o in plan["orphans"]} == {orphan, lone}   # persona row NOT an orphan
    assert plan["actions_by_type"]["tombstone-orphan"] == 2
    assert plan["actions_by_type"]["subsume"] == 0            # empty-scope rows never ride subsume
    # any earned weight an orphan carries is reported, never silently stranded
    assert "orphan_tombstoned_weight" in plan["weight_totals"]
    _fresh_backup(planner, store)
    rep = planner.apply()
    assert rep["ok"] and rep["applied"]["tombstone-orphan"] == 2
    # tombstoned = disclaimed (judged-floor re-base), NEVER deleted
    for oid in (orphan, lone):
        row = store.conn.execute("SELECT born_from, score FROM atoms WHERE id=?", (oid,)).fetchone()
        assert row is not None and "judged:disclaimed" in row["born_from"]
    # the real atom AND the persona row are untouched
    for pid in (real, persona):
        r = store.conn.execute("SELECT born_from FROM atoms WHERE id=?", (pid,)).fetchone()
        assert "judged:disclaimed" not in (r["born_from"] or "")
    # idempotent: a re-plan tombstones nothing twice
    assert planner.plan()["action_count"] == 0
