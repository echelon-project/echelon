"""hygiene — slice 1.5 bank hygiene: one canonical atom per VERBATIM-duplicate cluster (dedup by
subsume, never delete), acting on the 2026-07-08 mining verdict (mining-classified.md: 325/329
cross-scope near-dup clusters are ingest FILING copies, not re-learned traps).

THE PROBLEM (owner, 2026-07-08): the bank is polluted by wholesale COPIES — the same atom (same slug,
same claim text, cosine ~1.0) planted into 2-4 scopes at ingest/filing time (mol <-> mol-data-engine;
the wk-sop memory folder visible in epsilon-co-sop/flux/ux-cartridge; often 4+ identical rows WITHIN
one scope), plus 9 hash-id atoms stranded in an EMPTY scope. Consolidation (slice 2) on this bank would
canonicalize noise, so hygiene blocks it. This module is the fix's verb:

    python -X utf8 -m echelon_engine hygiene dedup [--scope <s> | --all-scopes] [--apply]
    python -X utf8 -m echelon_engine hygiene report

THE LAWS (slice-1.5-hygiene-spec.md, every numbered rule, + the gate's 2026-07-08 tightening):
  1. CONTENT-VERBATIM TIER ONLY (tightened by the gate's refusal): a pair qualifies IFF its
     whitespace-normalized BODIES (atoms.content) are identical. Same slug / identical claim are
     strictly candidate GENERATORS — they nominate pairs for the body comparison, they never qualify
     one (the first build let slug/claim + spine-cosine qualify, and 296/628 clusters turned out to be
     version chains / multi-claim families whose bodies differ by up to 18KB — slice-2/3 material,
     not hygiene's). Clusters are recomputed LIVE, never replayed from the stale mining-report.json.
     The 3 doctrine-echo clusters (7/305/321 in judge-queue.json: different wording, no slug match)
     fall below this bar BY CONSTRUCTION and are never touched; semantic dedup waits for slice 3.
  2. CANONICAL = the heaviest by v2 earned weight (the top_earned effective-score rule: disclaimed ->
     atoms.score; witnessed-used -> max(atoms.score, atom_earned.score); else atoms.score), then
     witnessed use_count, then earliest created; final tie -> the atom whose coordinate names its own
     scope (home scope) wins.
  3. SUBSUME, NEVER DELETE — the wk-group precedent (commit 4dd1b67, group_scope.py) at atom grain:
     the wk-group parent scope got a `subsumes` atlas edge to each member (no rename, no id rewrite,
     zero earned weight lost). Here the canonical gets a `subsumes` atom_links edge to each duplicate
     (canonical --subsumes--> copy, the same parent->member direction and the same edge kind), and
     recall drops a subsumed copy ONLY when its canonical is present in the same result set — exactly
     the seeds() supersede-drop discipline, so a scoped recall inside the copy's own scope still
     surfaces it (the member-keeps-its-memory half of the precedent). Rows never move, never die.
  4. WEIGHT PRESERVATION: each duplicate's atom_earned weight is MERGED onto the canonical — use
     counts SUM; effective score = MAX, never sum (no laundering by aggregation; a disclaimed
     duplicate's stale earned score is additionally excluded from the max — the dispute re-base must
     not be laundered back up through a merge, mirroring top_earned's disclaim guard). The receipt
     is HONEST (gate fix): before = the canonical's CURRENT live earned state, after = the merged
     projection — a real delta, not the old circular max==max — and no-loss is asserted per cluster
     against live rows at apply, inside the transaction.
  5. WITHIN-SCOPE duplicates (the 4-identical-rows-in-one-scope case) are the same operation.
  6. EMPTY-SCOPE HASH-ID ORPHANS: ONLY the standalone memory:<live-scope>:* leg (the mis-filed ingest
     rows the mining verdict named) is tombstoned — via the atom-level tombstone, CardStore.dispute
     (judged-floor re-base, append-only, never deleted, excluded from surfacing by warmth's dispute
     gate). Every OTHER empty-scope row (persona/value/finding system rows — one persona:dev carries
     use_count=126) is untouchable by this slice: never tombstoned, never clustered. Earned weight a
     tombstoned orphan carries is REPORTED in weight_totals (nothing stranded silently). NOTE an
     honest deviation: the immune scan/heal precedent the spec names tombstones EDGES (unlink ->
     superseded_on); an ATOM has no superseded_on column, so its existing never-delete tombstone IS
     the disclaim organ — same law, different lever because the object differs.
  7. DRY-RUN IS THE DEFAULT and READ-ONLY, proven the slice-1 way: the planner opens the bank in
     sqlite ro mode and per-table sha256 before/after the dry-run must be identical (printed + stored
     in the plan). The plan is written to ~/.echelon/hygiene-plan.json.
  8. IDEMPOTENT: a duplicate already carrying a live `subsumes` edge is DONE (its weight already
     merged — the edge is the merge receipt, so a re-run never double-counts); an orphan already
     disclaimed is done. A second run after --apply plans 0 actions.
  9. BACKUP GATE + TRANSACTIONAL APPLY: --apply refuses unless a VALIDATED bank backup newer than the
     plan exists (`echelon backup` -> ~/.echelon/backups/echelon_<stamp>.db; validated = size sanity
     vs the live bank + PRAGMA integrity_check == ok — a fresh mtime alone is not a recovery path).
     The whole merge phase runs in ONE transaction: any failure (including a no-loss check) rolls the
     entire plan back and writes the receipt with ok:false. --apply executes exactly the LAST PLAN's
     rules against live rows — it never re-plans.

Pure-test-fixture clusters (every member scope matches the miner's throwaway-scope mark) are SKIPPED
and counted — the 1 test-pollution cluster in the verdict is not real estate; nothing there is touched.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import sys
import time
from pathlib import Path

from .cross_scope_miner import CrossScopeMiner   # only its _is_test_scope mark is reused
from .. import estate as _estate

_HOME = Path.home() / ".echelon"
DEFAULT_DB = _HOME / "echelon.db"
DEFAULT_PLAN = _HOME / "hygiene-plan.json"
DEFAULT_RECEIPT = _HOME / "hygiene-receipt.json"
DEFAULT_BACKUP_DIR = _HOME / "backups"
# The estate's mining report — the CROSS-CHECK only (rule: recompute, don't replay).
_ESTATE = _estate.estate_root_for("command_root")
MINING_REPORT = _ESTATE / "architecture" / "mining-report.json"

EDGE_REL = "subsumes"        # the wk-group precedent's edge kind, reused verbatim at atom grain
_JUDGED_MARK = "judged:disclaimed"
_REDEEMED_MARK = "redeemed:"
_HASH_SLUG = re.compile(r"^[0-9a-f]{12,16}$")
# The tables whose byte-identity proves the read-only law (everything hygiene could conceivably touch).
_PROOF_TABLES = ("atoms", "cards", "atom_links", "atom_spine", "atom_body", "atom_sidecar", "atom_earned")


# ── read-only proof ─────────────────────────────────────────────────────────────────────────────────
def table_sha256(conn: sqlite3.Connection, table: str) -> str:
    """Deterministic per-table sha256 (rows ordered by every column) — the slice-1 read-only proof."""
    if table not in _PROOF_TABLES:
        raise ValueError(f"not a proof table: {table!r}")
    cols = [r[1] for r in conn.execute(f'PRAGMA table_info("{table}")').fetchall()]
    if not cols:
        return "absent"
    order = ", ".join(f'"{c}"' for c in cols)
    h = hashlib.sha256()
    for row in conn.execute(f'SELECT * FROM "{table}" ORDER BY {order}'):
        h.update(repr(tuple(row)).encode("utf-8", "surrogateescape"))
    return h.hexdigest()


def _proof(conn: sqlite3.Connection) -> dict:
    return {t: table_sha256(conn, t) for t in _PROOF_TABLES}


# ── normalization (the verbatim keys) ───────────────────────────────────────────────────────────────
def _norm(text: str) -> str:
    """Whitespace-normalized, case-folded text — the spec's 'whitespace-normalized identical' key."""
    return " ".join((text or "").split()).casefold()


def _is_hash_slug(slug: str, atom_id: str) -> bool:
    s = (slug or "").strip()
    return (not s) or s == (atom_id or "")[:12] or bool(_HASH_SLUG.match(s))


# ── the planner ─────────────────────────────────────────────────────────────────────────────────────
class HygienePlanner:
    """Recomputes verbatim clusters LIVE over a read-only connection and emits the plan. Never writes
    the bank — the only artifact is the plan JSON. apply() is a separate, backup-gated act."""

    def __init__(self, db_path: Path | str = DEFAULT_DB,
                 plan_path: Path | str = DEFAULT_PLAN,
                 backup_dir: Path | str = DEFAULT_BACKUP_DIR,
                 receipt_path: Path | str = DEFAULT_RECEIPT):
        self.db_path = Path(db_path)
        self.plan_path = Path(plan_path)
        self.backup_dir = Path(backup_dir)
        self.receipt_path = Path(receipt_path)

    def _ro(self) -> sqlite3.Connection:
        """A genuinely read-only connection (sqlite ro mode) — CardStore's __init__ runs migrations,
        which would violate the dry-run law, so the planner NEVER opens the store."""
        conn = sqlite3.connect(f"file:{self.db_path.as_posix()}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        return conn

    # ── load ────────────────────────────────────────────────────────────────────────────────────────
    def _load(self, conn: sqlite3.Connection, scope: str | None) -> list[dict]:
        q = ("SELECT a.id, a.scope, a.kind, a.coordinate, a.content, a.score a_score, "
             "a.use_count a_use, a.born_from, a.ts, "
             "COALESCE(s.slug,'') slug, COALESCE(s.claim,'') claim, COALESCE(s.directive,'') directive, "
             "e.score e_score, COALESCE(e.use_count,0) e_use "
             "FROM atoms a LEFT JOIN atom_spine s ON s.atom_id=a.id "
             "LEFT JOIN atom_earned e ON e.atom_id=a.id")
        params: tuple = ()
        if scope is not None:
            q += " WHERE a.scope=?"
            params = (scope,)
        return [dict(r) for r in conn.execute(q, params).fetchall()]

    @staticmethod
    def _live_subsumed(conn: sqlite3.Connection) -> dict[str, str]:
        """{duplicate_id: canonical_id} for every LIVE subsumes edge — the idempotency ledger."""
        return {r["to_id"]: r["from_id"] for r in conn.execute(
            "SELECT from_id, to_id FROM atom_links WHERE relation=? AND superseded_on=0", (EDGE_REL,))}

    @staticmethod
    def _tombstoned_subsumed(conn: sqlite3.Connection) -> dict[str, str]:
        """{duplicate_id: canonical_id} for every TOMBSTONED subsumes edge — un-subsumed BY INTENT.
        The (from,to,relation) PK is still occupied, so apply's INSERT OR IGNORE can never re-draw
        the edge; planning a merge for such a pair makes apply skip it and the no-loss verifier
        roll the whole plan back (the 2026-08-02 WEIGHT-LOSS rollback). The planner must treat these
        pairs as settled — a deliberate un-subsume stays un-subsumed until a dedicated verb retires
        the tombstone row."""
        return {r["to_id"]: r["from_id"] for r in conn.execute(
            "SELECT from_id, to_id FROM atom_links WHERE relation=? AND superseded_on!=0", (EDGE_REL,))}

    @staticmethod
    def _is_disclaimed(born_from: str) -> bool:
        bf = born_from or ""
        return _JUDGED_MARK in bf and not bf.startswith(_REDEEMED_MARK)

    def _effective(self, a: dict) -> float:
        """top_earned's effective-score rule, row-local: disclaimed -> atoms.score; witnessed-used ->
        max(atoms.score, atom_earned.score); else atoms.score."""
        if self._is_disclaimed(a["born_from"]):
            return float(a["a_score"])
        if a["e_use"] > 0 and a["e_score"] is not None:
            return max(float(a["a_score"]), float(a["e_score"]))
        return float(a["a_score"])

    @staticmethod
    def _is_home(a: dict) -> bool:
        """Home scope = the atom's coordinate names its own scope as a segment (rule 1 tie-break)."""
        sc = a["scope"] or ""
        return bool(sc) and sc in (a["coordinate"] or "").split(":")

    # ── verbatim clustering (recomputed live with the miner's mechanical signals) ────────────────────
    def _clusters(self, atoms: list[dict]) -> list[list[int]]:
        """Union-find over CONTENT-VERBATIM pairs, within-scope pairs included (rule 4).

        THE GATE'S TIGHTENING (refusal, 2026-07-08): the first build let same-slug / same-claim +
        spine-cosine QUALIFY a pair — but the spine never sees the BODY, so version chains (same slug,
        5.5KB -> 24KB bodies), multi-claim families, and canonical-shorter-than-member cases (296/628
        clusters on the live bank) rode in as 'verbatim'. The fixed law: a member JOINS a cluster ONLY
        on identical whitespace-normalized BODY (atoms.content, the id-anchor truth blob). Same slug /
        same claim remain strictly candidate GENERATORS (bucket keys that nominate pairs for the body
        comparison), never qualifiers — the spine-cosine leg is gone entirely. A same-slug family with
        drifting bodies is a VERSION CHAIN, which is slice-2/3 material, not hygiene's.

        Empty-scope atoms are excluded from cluster membership outright: this slice neither
        canonicalizes on them nor subsumes them (the persona:* rows the gate protected — one carries
        use_count=126); the only empty-scope action is the standalone rule-5 orphan leg in plan()."""
        n = len(atoms)
        parent = list(range(n))

        def find(x):
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        def union(i, j):
            ri, rj = find(i), find(j)
            if ri != rj:
                parent[ri] = rj

        by_slug: dict[str, list[int]] = {}      # candidate GENERATOR only
        by_claim: dict[str, list[int]] = {}     # candidate GENERATOR only
        by_content: dict[str, list[int]] = {}   # the qualifier key: identical normalized body
        norm_content = [""] * n
        for i, a in enumerate(atoms):
            if a["scope"] == "":
                continue   # empty-scope rows are untouchable by the subsume path (see docstring)
            body = _norm(a["content"])
            norm_content[i] = body
            if not body:
                continue   # a bodiless row can never be body-verbatim with anything
            by_content.setdefault(hashlib.sha1(body.encode("utf-8")).hexdigest(), []).append(i)
            slug = (a["slug"] or "").strip()
            if slug and not _is_hash_slug(slug, a["id"]):
                by_slug.setdefault(slug, []).append(i)
            nc = _norm(a["claim"])
            if nc:
                by_claim.setdefault(nc, []).append(i)

        def qualifies(i, j) -> bool:
            # THE ONE VERBATIM LAW: identical whitespace-normalized BODY. Nothing else qualifies.
            return bool(norm_content[i]) and norm_content[i] == norm_content[j]

        for bucket in list(by_slug.values()) + list(by_claim.values()) + list(by_content.values()):
            if len(bucket) < 2:
                continue
            for other in bucket[1:]:
                # pair `other` against any already-settled member whose body it matches (a slug
                # bucket like 'reflex' holds 25 near-strangers — only true body-twins may union)
                for prev in bucket:
                    if prev is other:
                        break
                    if find(prev) != find(other) and qualifies(prev, other):
                        union(prev, other)
                        break
        comps: dict[int, list[int]] = {}
        for i in range(n):
            comps.setdefault(find(i), []).append(i)
        return [m for m in comps.values() if len(m) >= 2]

    # ── plan ────────────────────────────────────────────────────────────────────────────────────────
    def plan(self, scope: str | None = None) -> dict:
        """The dry-run: recompute clusters, choose canonicals, emit actions + receipts + the read-only
        proof. Writes ONLY the plan file."""
        conn = self._ro()
        try:
            sha_before = _proof(conn)
            atoms = self._load(conn, scope)
            already = self._live_subsumed(conn)
            vetoed = self._tombstoned_subsumed(conn)   # un-subsumed by intent — settled, never re-planned
            comps = self._clusters(atoms)
            miner = CrossScopeMiner.__new__(CrossScopeMiner)   # only for _is_test_scope (no DB open)

            clusters_out, orphans_out = [], []
            skipped_test = 0
            tot_use_before = tot_use_after = 0
            tot_score_before = tot_score_after = 0.0
            n_subsume = n_orphan = 0

            for members in sorted(comps, key=lambda m: -len(m)):
                rows = [atoms[i] for i in members]
                scopes = sorted({r["scope"] for r in rows})
                if all(miner._is_test_scope(s) for s in scopes):
                    skipped_test += 1
                    continue   # test-fixture pollution — the gate discounts it; hygiene never touches it

                # already-subsumed members are DONE (their weight already merged; the edge is the
                # receipt). Tombstone-vetoed members are ALSO done — un-subsumed by intent; the dead
                # edge still occupies the PK, so apply could never re-merge them (see
                # _tombstoned_subsumed) and planning them guarantees a whole-plan rollback.
                pending = [r for r in rows if r["id"] not in already and r["id"] not in vetoed]
                if not pending:
                    continue

                # canonical selection (rule 1): eff desc, witnessed use desc, earliest ts, home wins.
                # (earliest-created is a safe tie-break HERE because every member is body-verbatim —
                # a version chain can no longer smuggle its oldest body in as canonical.)
                canonical = sorted(
                    pending, key=lambda r: (-self._effective(r), -int(r["e_use"]),
                                            int(r["ts"] or 0), 0 if self._is_home(r) else 1, r["id"]))[0]
                dups = [r for r in pending if r["id"] != canonical["id"]]
                if not dups:
                    continue   # every copy already subsumed on a prior run — plans 0 for this cluster

                # HONEST RECEIPT (gate fix, 2026-07-08): before = the canonical's CURRENT live earned
                # state; after = the merged projection. The old receipt defined before as max(pool) —
                # identically the merge result, so 'loss 0' was circular. Now the receipt states a real
                # delta, and the no-loss claim is the separate assertion that the merged value covers
                # every member's earned weight (use SUM; score >= every non-disclaimed member's score).
                can_use_before = int(canonical["e_use"])
                can_score_before = float(canonical["e_score"]) if canonical["e_score"] is not None else 100.0
                use_after = can_use_before + sum(int(d["e_use"]) for d in dups)
                score_pool = [can_score_before] + [
                    float(d["e_score"]) for d in dups
                    if d["e_score"] is not None and not self._is_disclaimed(d["born_from"])]
                score_after = max(score_pool)

                actions = []
                for d in dups:
                    actions.append({
                        "type": "subsume", "canonical": canonical["id"], "duplicate": d["id"],
                        "relation": EDGE_REL,
                        "merge": {"use_count_add": int(d["e_use"]),
                                  "score_candidate": (None if self._is_disclaimed(d["born_from"])
                                                      else d["e_score"])}})
                    n_subsume += 1

                tot_use_before += can_use_before
                tot_use_after += use_after
                tot_score_before += can_score_before
                tot_score_after += score_after

                clusters_out.append({
                    "scopes": scopes, "cross_scope": len(scopes) > 1,
                    "canonical": {"id": canonical["id"], "scope": canonical["scope"],
                                  "slug": canonical["slug"], "coordinate": canonical["coordinate"],
                                  "eff": round(self._effective(canonical), 2),
                                  "e_use": int(canonical["e_use"]), "home": self._is_home(canonical)},
                    "members": [{"id": r["id"], "scope": r["scope"], "slug": r["slug"],
                                 "eff": round(self._effective(r), 2), "e_use": int(r["e_use"]),
                                 "already_subsumed": r["id"] in already,
                                 "unsubsumed_by_intent": r["id"] in vetoed,
                                 "disclaimed": self._is_disclaimed(r["born_from"])} for r in rows],
                    "weight_before": {"canonical_use_count": can_use_before,
                                      "canonical_earned_score": round(can_score_before, 3),
                                      "members_use_count_sum": use_after},
                    "weight_after_projection": {"canonical_use_count": use_after,
                                                "canonical_earned_score": round(score_after, 3)},
                    "actions": actions})

            # RULE 5, the ONLY orphan leg (gate condition 2): the mining verdict's orphans are
            # memory:<scope>:* rows stranded with scope='' + a hash-id slug — mis-filed INGEST
            # artifacts of the 2026-07-04 memory-folder-split ancestor. The mechanical, provable
            # criterion: empty scope + hash-id slug + coordinate `memory:<s>:*` where a real non-empty
            # scope <s> exists in the bank (the row names the home it lost). Every OTHER empty-scope
            # row (persona/value/finding system rows — one persona:dev row carries use_count=126) is
            # untouchable by this slice: never tombstoned, never clustered (see _clusters).
            # Any earned weight a tombstoned orphan DOES carry is reported in weight_totals — a
            # tombstone re-bases the row's rank but strands its earned trail, and the receipt must
            # say so out loud (the gate called silent 'loss: 0' false-by-omission here).
            orphan_use_stranded = 0
            orphan_score_stranded = 0.0
            if scope is None:
                # A coordinate is NORMALIZED on write (hyphens become underscores), so a
                # scope like `my-app` is stored as `memory:my_app:*`. Comparing the raw
                # coordinate segment against the raw scope therefore matched only scopes
                # with no hyphen — and silently missed the orphans of every hyphenated
                # scope, which is most of them. Compare on the normalized form instead.
                def _norm(s: str) -> str:
                    return s.replace("-", "_").lower()

                live_scopes = {_norm(r[0]) for r in conn.execute(
                    "SELECT DISTINCT scope FROM atoms WHERE scope != ''")}
                for a in atoms:
                    coord = a["coordinate"] or ""
                    parts = coord.split(":")
                    if (a["scope"] == "" and _is_hash_slug(a["slug"], a["id"])
                            and len(parts) >= 3 and parts[0] == "memory"
                            and _norm(parts[1]) in live_scopes
                            and not self._is_disclaimed(a["born_from"])):
                        e_use = int(a["e_use"])
                        e_score = float(a["e_score"]) if a["e_score"] is not None else 100.0
                        orphan_use_stranded += e_use
                        orphan_score_stranded += max(0.0, e_score - 100.0)   # weight above born-neutral
                        orphans_out.append({
                            "type": "tombstone-orphan", "atom_id": a["id"], "coordinate": coord,
                            "slug": a["slug"],
                            "earned": {"use_count": e_use, "score": round(e_score, 3)},
                            "mechanism": "CardStore.dispute -> judged-floor re-base (atom-level "
                                         "tombstone; never deleted, excluded from surfacing by "
                                         "warmth's dispute gate)"})
                        n_orphan += 1

            sha_after = _proof(conn)
        finally:
            conn.close()

        identical = sha_before == sha_after
        cross = sum(1 for c in clusters_out if c["cross_scope"])
        plan = {
            "generated_by": "hygiene (slice 1.5) — verbatim-tier dedup plan; subsume-never-delete",
            "generated_ts": int(time.time()),
            "db": str(self.db_path),
            "scope_filter": scope or "(all scopes)",
            "verbatim_rules": {"qualifier": "identical whitespace-normalized BODY (atoms.content) ONLY",
                               "generators": "same slug | identical normalized claim (candidate nomination only)",
                               "edge_relation": EDGE_REL},
            "atom_count": len(atoms),
            "cluster_count": len(clusters_out),
            "cross_scope_cluster_count": cross,
            "within_scope_cluster_count": len(clusters_out) - cross,
            "skipped_pure_test_clusters": skipped_test,
            "actions_by_type": {"subsume": n_subsume, "tombstone-orphan": n_orphan},
            "action_count": n_subsume + n_orphan,
            "weight_totals": {
                # before = the canonicals' CURRENT live earned state; after = the merged projection.
                # The delta is the weight MOVED onto canonicals from their duplicates (a real number,
                # not the old circular max==max receipt). No-loss = after covers every member (asserted
                # per cluster at apply); the orphan block reports what the rule-5 tombstones strand.
                "canonicals_before": {"use_count_sum": tot_use_before,
                                      "earned_score_sum": round(tot_score_before, 3)},
                "canonicals_after_projection": {"use_count_sum": tot_use_after,
                                                "earned_score_sum": round(tot_score_after, 3)},
                "merged_in_delta": {"use_count": tot_use_after - tot_use_before,
                                    "earned_score": round(tot_score_after - tot_score_before, 3)},
                "orphan_tombstoned_weight": {"use_count_sum": orphan_use_stranded,
                                             "earned_score_above_neutral_sum": round(orphan_score_stranded, 3),
                                             "note": "earned weight carried by rule-5 tombstoned orphans — "
                                                     "re-based out of surfacing, NOT merged anywhere; "
                                                     "reported so nothing is stranded silently"}},
            "orphans": orphans_out,
            "read_only_proof": {"tables": sha_before, "identical_after_dry_run": identical},
            "drift_vs_mining_report": self._drift(cross),
            "clusters": clusters_out,
        }
        self.plan_path.parent.mkdir(parents=True, exist_ok=True)
        self.plan_path.write_text(json.dumps(plan, indent=2, ensure_ascii=False), encoding="utf-8")
        if not identical:
            # this must be impossible on an ro connection — surface it LOUDLY if it ever fires
            raise RuntimeError("READ-ONLY LAW VIOLATED: per-table sha256 changed during a dry-run")
        return plan

    def _drift(self, recomputed_cross: int) -> dict:
        """Cross-check the recomputed verbatim set against the stale mining-report (never replayed)."""
        out = {"expected_ingest_duplicate_clusters": 325,
               "recomputed_cross_scope_verbatim_clusters": recomputed_cross,
               "drift": recomputed_cross - 325}
        try:
            rep = json.loads(MINING_REPORT.read_text(encoding="utf-8"))
            out["mining_report_cross_scope_clusters"] = rep.get("cross_scope_cluster_count")
            out["mining_report_path"] = str(MINING_REPORT)
        except Exception:
            out["mining_report_path"] = f"(not readable: {MINING_REPORT})"
        return out

    # ── apply (backup-gated; executes exactly the LAST plan) ────────────────────────────────────────
    def _fresh_backup(self) -> Path | None:
        """The newest `echelon backup` snapshot (backup_cmd.py names them echelon_<stamp>.db in
        ~/.echelon/backups) that is NEWER than the plan file — the gate's proof of a recovery path."""
        if not self.plan_path.exists() or not self.backup_dir.exists():
            return None
        plan_mtime = self.plan_path.stat().st_mtime

        # backup_cmd copies with copy2, which PRESERVES the live db's mtime — so a backup's
        # mtime says when the BANK was last written, never when the snapshot was taken, and a
        # plan written after the bank's last write could never find a "newer" backup (found
        # live 2026-07-09: the gate refused a 30-minutes-fresh backup). Freshness = the newest
        # of mtime/ctime; on Windows st_ctime is file CREATION time, which a copy resets to now.
        def _taken(b: Path) -> float:
            st = b.stat()
            return max(st.st_mtime, st.st_ctime)

        candidates = [b for b in self.backup_dir.glob("echelon_*.db") if _taken(b) >= plan_mtime]
        for b in sorted(candidates, key=_taken, reverse=True):
            if self._backup_valid(b):
                return b
        return None

    def _backup_valid(self, backup: Path) -> bool:
        """Gate condition 4: a fresh mtime is NOT a recovery path — a 6-byte junk file passed the old
        gate. Validate the CONTENT: (a) size sanity — the backup must be at least half the live bank
        (a snapshot can only shrink modestly via WAL checkpointing/vacuum, never to a sliver); (b)
        sqlite PRAGMA integrity_check must answer 'ok' on a read-only open. Best-effort False on any
        error — an unverifiable backup is not a backup."""
        try:
            live_size = self.db_path.stat().st_size
            if backup.stat().st_size < max(1, live_size // 2):
                return False
            conn = sqlite3.connect(f"file:{backup.as_posix()}?mode=ro", uri=True)
            try:
                row = conn.execute("PRAGMA integrity_check").fetchone()
                return bool(row) and str(row[0]).lower() == "ok"
            finally:
                conn.close()
        except Exception:
            return False

    def apply(self) -> dict:
        """Execute the last plan's rules against live rows — as ONE TRANSACTION (gate condition 3).

        All subsume edges + weight merges + per-cluster no-loss verification run inside a single
        BEGIN IMMEDIATE on a dedicated connection; ANY failure (including a no-loss check) rolls the
        WHOLE plan back and writes the receipt with ok:false — the bank is never left mid-merged with
        no receipt. Orphan tombstones run AFTER the commit via CardStore.dispute (each is a single
        atomic, idempotent row op through the honest disclaim organ — replaying it is a no-op).

        Edge semantics + idempotency: the edge insert is `INSERT OR IGNORE` on the (from,to,relation)
        PK; an already-present edge means the weight was already merged on a prior run, so the merge
        is SKIPPED (never double-counted). WARNING for a future un-subsume verb: a TOMBSTONED subsume
        edge (superseded_on != 0) still occupies the PK, so the same pair can never be re-subsumed by
        this path — a deliberate un-subsume stays un-subsumed unless the tombstone row is first
        retired by a dedicated verb. Refuses without a validated fresh backup (rule 8)."""
        if not self.plan_path.exists():
            return {"ok": False, "reason": f"no plan at {self.plan_path} — run `hygiene dedup` first"}
        backup = self._fresh_backup()
        if backup is None:
            return {"ok": False, "refused": True,
                    "reason": (f"BACKUP GATE: no VALID backup newer than the plan in {self.backup_dir} "
                               "(freshness + size sanity + PRAGMA integrity_check). "
                               "Run `python -X utf8 -m echelon_engine backup` then re-apply.")}
        plan = json.loads(self.plan_path.read_text(encoding="utf-8"))
        now = int(time.time())
        applied = {"subsume": 0, "merge": 0, "skipped_existing_edge": 0, "tombstone-orphan": 0}
        cluster_receipts = []

        # A dedicated writable connection with EXPLICIT transaction control (isolation_level=None ->
        # manual BEGIN/COMMIT). CardStore is not used for the merge phase because its ops commit
        # internally, which would break whole-plan atomicity.
        conn = sqlite3.connect(str(self.db_path), timeout=30)
        conn.row_factory = sqlite3.Row
        conn.isolation_level = None
        try:
            conn.execute("BEGIN IMMEDIATE")
            for c in plan.get("clusters", []):
                cid = c["canonical"]["id"]
                for act in c.get("actions", []):
                    if act["type"] != "subsume":
                        continue
                    did = act["duplicate"]
                    cur = conn.execute(
                        "INSERT OR IGNORE INTO atom_links (from_id,to_id,relation,ts) VALUES (?,?,?,?)",
                        (cid, did, EDGE_REL, now))
                    if cur.rowcount == 0:
                        applied["skipped_existing_edge"] += 1
                        continue   # edge already present (live OR tombstoned) -> merge already settled
                    applied["subsume"] += 1
                    m = act.get("merge") or {}
                    add_use = int(m.get("use_count_add") or 0)
                    cand = m.get("score_candidate")
                    row = conn.execute(
                        "SELECT score, use_count, score_history FROM atom_earned WHERE atom_id=?",
                        (cid,)).fetchone()
                    if row is None:
                        scope_row = conn.execute("SELECT scope FROM atoms WHERE id=?", (cid,)).fetchone()
                        conn.execute(
                            "INSERT OR IGNORE INTO atom_earned (atom_id, scope, score, use_count, score_history) "
                            "VALUES (?,?,?,?,?)",
                            (cid, (scope_row["scope"] if scope_row else ""), 100.0, 0, "[]"))
                        row = conn.execute(
                            "SELECT score, use_count, score_history FROM atom_earned WHERE atom_id=?",
                            (cid,)).fetchone()
                    old_score = float(row["score"])
                    new_score = max(old_score, float(cand)) if cand is not None else old_score
                    hist = json.loads(row["score_history"] or "[]")
                    hist.append([now, round(new_score - old_score, 3),
                                 f"hygiene-merge:{did[:12]}(+{add_use}use)"])
                    conn.execute(
                        "UPDATE atom_earned SET score=?, use_count=?, score_history=? WHERE atom_id=?",
                        (new_score, int(row["use_count"]) + add_use, json.dumps(hist), cid))
                    applied["merge"] += 1
                # in-transaction verification: canonical must now hold >= the planned totals (>=
                # because a prior partial contribution may exist — never <, which would be loss)
                live = conn.execute(
                    "SELECT COALESCE(use_count,0) u, COALESCE(score,100.0) s FROM atom_earned WHERE atom_id=?",
                    (cid,)).fetchone()
                want = c["weight_after_projection"]
                got_u = int(live["u"]) if live else 0
                got_s = float(live["s"]) if live else 100.0
                ok = (got_u >= int(want["canonical_use_count"])
                      and got_s >= float(want["canonical_earned_score"]) - 1e-9)
                cluster_receipts.append({"canonical": cid,
                                         "before": c["weight_before"], "planned_after": want,
                                         "live_after": {"use_count": got_u, "earned_score": round(got_s, 3)},
                                         "no_loss": ok})
                if not ok:
                    raise RuntimeError(f"WEIGHT LOSS on cluster canonical {cid}: "
                                       f"live {got_u}/{got_s} < planned {want}")
            conn.execute("COMMIT")
        except Exception as e:
            try:
                conn.execute("ROLLBACK")
            except Exception:
                pass
            receipt = {"applied_ts": now, "plan": str(self.plan_path), "backup_used": str(backup),
                       "ok": False, "rolled_back": True, "error": str(e),
                       "applied": {k: 0 for k in applied},   # the rollback undid everything
                       "cluster_receipts": cluster_receipts}
            self.receipt_path.write_text(json.dumps(receipt, indent=2, ensure_ascii=False), encoding="utf-8")
            return receipt
        finally:
            conn.close()

        # Orphan tombstones AFTER the committed merge phase: each dispute is its own atomic engine op
        # through the honest disclaim organ (idempotent — an already-disclaimed row is a no-op).
        from .cards import CardStore
        cs = CardStore(self.db_path)
        for o in plan.get("orphans", []):
            res = cs.dispute("atoms", o["atom_id"],
                             reason="hygiene slice-1.5: empty-scope hash-id orphan (mining verdict 2026-07-08)")
            if res.get("ok") and not res.get("noop"):
                applied["tombstone-orphan"] += 1

        receipt = {"applied_ts": now, "plan": str(self.plan_path), "backup_used": str(backup),
                   "applied": applied, "cluster_receipts": cluster_receipts,
                   "totals": plan.get("weight_totals"), "ok": True}
        self.receipt_path.write_text(json.dumps(receipt, indent=2, ensure_ascii=False), encoding="utf-8")
        return receipt


# ── rendering ───────────────────────────────────────────────────────────────────────────────────────
def _render_plan(p: dict, verbose: bool = False) -> str:
    L = [f"HYGIENE DEDUP PLAN (dry-run — nothing written to the bank; plan at ~/.echelon/hygiene-plan.json)",
         f"  db: {p['db']}   scope: {p['scope_filter']}   atoms: {p['atom_count']}",
         f"  verbatim clusters: {p['cluster_count']} "
         f"(cross-scope {p['cross_scope_cluster_count']}, within-scope {p['within_scope_cluster_count']}; "
         f"pure-test skipped {p['skipped_pure_test_clusters']})",
         f"  actions: {p['actions_by_type']['subsume']} subsume+merge, "
         f"{p['actions_by_type']['tombstone-orphan']} orphan tombstone  (total {p['action_count']})",
         f"  canonical weight: use_count {p['weight_totals']['canonicals_before']['use_count_sum']} -> "
         f"{p['weight_totals']['canonicals_after_projection']['use_count_sum']}  "
         f"earned-score-sum {p['weight_totals']['canonicals_before']['earned_score_sum']} -> "
         f"{p['weight_totals']['canonicals_after_projection']['earned_score_sum']}  "
         f"(merged-in delta: +{p['weight_totals']['merged_in_delta']['use_count']} use, "
         f"+{p['weight_totals']['merged_in_delta']['earned_score']} score)",
         f"  orphan-tombstoned weight (stranded, reported not merged): "
         f"use_count {p['weight_totals']['orphan_tombstoned_weight']['use_count_sum']}, "
         f"score-above-neutral {p['weight_totals']['orphan_tombstoned_weight']['earned_score_above_neutral_sum']}",
         f"  drift vs mining-report: recomputed {p['drift_vs_mining_report']['recomputed_cross_scope_verbatim_clusters']} "
         f"cross-scope verbatim vs {p['drift_vs_mining_report']['expected_ingest_duplicate_clusters']} expected "
         f"(drift {p['drift_vs_mining_report']['drift']})",
         f"  read-only proof: per-table sha256 identical after dry-run = "
         f"{p['read_only_proof']['identical_after_dry_run']}"]
    if p["action_count"] == 0:
        L.append("  IDEMPOTENT: 0 actions — the bank is already clean at the verbatim tier.")
    show = p["clusters"] if verbose else p["clusters"][:10]
    for i, c in enumerate(show, 1):
        can = c["canonical"]
        L.append(f"  [{i}] scopes={','.join(s or '(empty)' for s in c['scopes'])} "
                 f"members={len(c['members'])} -> canonical [{can['scope']}] {can['slug'] or can['id'][:12]} "
                 f"(eff {can['eff']}, use {can['e_use']}) — {len(c['actions'])} subsume")
    if not verbose and len(p["clusters"]) > 10:
        L.append(f"  ... and {len(p['clusters']) - 10} more clusters (full detail in the plan file)")
    if p["orphans"]:
        L.append(f"  orphans (empty-scope hash-id, dispute-tombstone path): {len(p['orphans'])}")
        for o in p["orphans"]:
            L.append(f"    - {o['atom_id'][:16]}  {o['coordinate'][:60]}")
    L.append("  apply: python -X utf8 -m echelon_engine backup && "
             "python -X utf8 -m echelon_engine hygiene dedup --all-scopes --apply")
    return "\n".join(L)


# ── CLI ─────────────────────────────────────────────────────────────────────────────────────────────
def _main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    ap = argparse.ArgumentParser(
        prog="echelon hygiene",
        description="Slice 1.5 bank hygiene: one canonical atom per verbatim-duplicate cluster; "
                    "copies subsumed (never deleted), earned weight merged, receipts prove no loss.")
    sub = ap.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("dedup", help="plan (dry-run default) or --apply the verbatim dedup")
    g = d.add_mutually_exclusive_group(required=True)
    g.add_argument("--scope", help="restrict to ONE scope (within-scope dedup only)")
    g.add_argument("--all-scopes", action="store_true", help="the whole bank (cross- + within-scope)")
    d.add_argument("--apply", action="store_true",
                   help="execute the LAST plan (refuses without a bank backup newer than the plan)")
    d.add_argument("--verbose", action="store_true", help="print every cluster, not just the head")
    d.add_argument("--db", default=str(DEFAULT_DB))
    d.add_argument("--plan", default=str(DEFAULT_PLAN))
    d.add_argument("--backup-dir", default=str(DEFAULT_BACKUP_DIR))

    r = sub.add_parser("report", help="print the last plan's summary (read-only)")
    r.add_argument("--plan", default=str(DEFAULT_PLAN))
    r.add_argument("--verbose", action="store_true")

    a = ap.parse_args(argv)
    if a.cmd == "report":
        plan_path = Path(a.plan)
        if not plan_path.exists():
            print(f"(no plan at {plan_path} — run `hygiene dedup` first)")
            return 1
        print(_render_plan(json.loads(plan_path.read_text(encoding="utf-8")), verbose=a.verbose))
        return 0

    planner = HygienePlanner(db_path=a.db, plan_path=a.plan, backup_dir=a.backup_dir)
    if a.apply:
        rep = planner.apply()
        if not rep.get("ok"):
            # two not-ok shapes: pre-flight refusal ({reason}) and mid-apply rollback
            # ({error, rolled_back}) — the printer must never crash on either (bit live
            # 2026-07-09 when the first rollback fired and this line KeyError'd).
            why = rep.get("reason") or rep.get("error") or json.dumps(rep)[:300]
            verb = "ROLLED BACK" if rep.get("rolled_back") else "REFUSED"
            print(f"hygiene apply {verb}: {why}")
            return 1
        print(f"HYGIENE APPLIED: {rep['applied']}  (backup: {Path(rep['backup_used']).name})")
        print(f"  receipts: {len(rep['cluster_receipts'])} clusters verified no-loss; "
              f"full receipt at {planner.receipt_path}")
        print("  verify idempotency: re-run `hygiene dedup` — it must plan 0 actions.")
        return 0
    plan = planner.plan(scope=(None if a.all_scopes else a.scope))
    print(_render_plan(plan, verbose=a.verbose))
    return 0


if __name__ == "__main__":
    sys.exit(_main())
