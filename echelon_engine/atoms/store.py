"""The seed store — SQLite floor for the memory organ.

Reclaims UAME's proven spine (content-address sha256[:16], append-only, two-tier
core/working, supersedes correction-chains, WAL+retry, dedup-on-write) from
~/.claude/tools/echelon-arsenal/uame.py + EROS/ENGINE/uame, and adds the dimension
UAME lacked: SCOPE (it was single-DB, single-place — the fragility the owner named).

A SEED is deliberately loose: {id, scope, content, kind, supersedes, ts}. The
content can be messy (a log, an output, a path) — the intelligence is NOT in the
seed's shape, it's in the MATCHER (warmth.py). This store only holds and lists;
it never decides what's relevant. That decision is warmth, computed elsewhere.

The LLM never touches this. The brain-stem (this script) does storage; the model
asks remember()/recall() and gets back a warmth signal, not rows. "like human and
its brain." See memory: memory-is-a-weight-adjustor, boot-is-rediscovery-not-instruction.
"""
from __future__ import annotations
import json
import re
import time
from pathlib import Path

from .uame import UAME, Entry
from .cards import CardStore, DEFAULT_V2_DB

# ── leaf imports (pure atoms extracted to their own files) ────────────────────
from .scoring import compute_score                                           # used by reinforce/consolidate
from .scope_map import scope_to_domain, _SCOPE_DOMAIN                       # re-exported below
from .seed_types import Seed                                                 # re-exported below
from .coord_norm import norm_coord                                          # the guard's slug normalizer (S8b V8b)

# ── re-export for backward compat ─────────────────────────────────────────────
# callers: from echelon_engine.atoms.store import SeedStore, Seed, scope_to_domain,
#                                                  DEFAULT_DB, DEFAULT_V2_DB, PROMOTE_THRESHOLD
__all__ = [
    "SeedStore", "Seed", "scope_to_domain", "_SCOPE_DOMAIN",
    "DEFAULT_DB", "DEFAULT_V2_DB", "PROMOTE_THRESHOLD",
    "SCORE_BENCHMARK", "SCORE_K", "SCORE_LAMBDA", "PROMOTE_MIN_RECALLS",
]

# Home of the one soul + a default. core.db is the relationship-index/soul (owner's call);
# per-scope content shares this DB for now, partitioned by the `scope` column. (A future
# stone may split heavy scopes into their own files; the API stays the same.)
from .echelon_home import home_db as _home_db
DEFAULT_DB = _home_db("core.db")

# v1 (core.db) DETACHED (2026-06-26). v2 (echelon.db) is the sole WRITE target; reads still union v1 ∪ v2 until the drain completes. — STARVE IT, don't remove it (owner 2026-06-16, the v2-primary endgame).
# The goal is to make v1 (core.db) go STALE so it can be DETACHED later with zero risk: stop feeding
# it the two things that keep it alive — (1) NEW atoms/seeds/edges and (2) WARMTH/score updates. With
# both starved, v1 stops growing and its scores stop moving; everything live accrues in v2 (core_v2.db,
# the rich primary store post read-flip). READS still span v1 ∪ v2 (seeds() unions both) so nothing
# goes blind during the drain — v1 is the frozen cold-borrow well, read-only, never written. When the
# last live thing has a v2 home, v1 detaches. Flip to False only to deliberately resurrect v1 writes.
# See v2-is-primary-v1-is-cold-borrow, v2-primary-readflip-executed, atlas-lives-on-v2-core.
_V1_FROZEN = True

# A [[name]] reference inside a seed's content = an authored edge. Parsed at write-time so the
# soul-graph grows from real connections the author held while warm (seed-and-link-while-warm).
# A [[ref]] may be a single slug OR a full COS coordinate path (colon-delimited) — the colon MUST be
# allowed or [[domain:topic:atom]] refs silently fail to link (the keystone gap that bit once).
_REF = re.compile(r"\[\[([a-z0-9][a-z0-9 _:-]*?)\]\]", re.I)

# NOTE: the flat `seeds` table is RETIRED — storage is now UAME's two-prefix/multi-table schema
# (uame.py). The old `seeds` table stays ON DISK as a backup (append-only spirit, nothing deleted)
# but SeedStore no longer writes it; it's a scope-based ADAPTER over UAME. See
# uame-schema-two-prefix-multi-table.

# COS rating constants (COSys_DESIGN section 7).
SCORE_BENCHMARK = 100.0     # B: neutral, unproven — every seed starts here.
SCORE_K = 1.0               # delta scale: delta = (Q-50)*k, Q in [0,100]. Under the COS averaging
                            # model (score = decay-weighted AVG of deltas, not a sum), k must be
                            # large enough that genuine strong resonance clears B+25: q=95 -> +45 ->
                            # score ~145 (promotes); a lukewarm q~70 -> +20 -> ~120 (stays working).
PROMOTE_THRESHOLD = 125.0   # > B+25 (COS synthesis-eligibility) — proven enough to be soul.
PROMOTE_MIN_RECALLS = 3     # AND proven OVER TIME: not one warm fluke, a repeated resonance.
SCORE_LAMBDA = 0.02         # decay rate /day (COS effectiveness band ~0.005-0.02). A delta's
                            # weight halves about every ln(2)/λ ≈ 35 days; a resonance untouched
                            # for months fades toward neutral B without ever being deleted.


class BatchRememberError(RuntimeError):
    """Partial/uncertain bank mutation; inspect report before retrying any entry."""

    def __init__(self, report):
        self.report = report
        super().__init__("bank batch incomplete; inspect per-entry outcomes before retry")


class SeedStore:
    """Scope-based adapter over the two-prefix/multi-table UAME (uame.py). Keeps the proven
    remember()/seeds()/promote()/reinforce()/consolidate() API so every caller is untouched, but
    the STORAGE is now correct: scope->domain (the table), tier->prefix (core_/working_), the soul
    structurally walled. The flat `seeds` table is retired as the live store (kept on disk as
    backup post-migration). See uame-schema-two-prefix-multi-table."""

    def __init__(self, db_path: Path | str = DEFAULT_DB, v2_db: Path | str | None = None):
        # BUG FOUND IN MIGRATION (2026-06-18): the original hardwired the v2 CardStore to the
        # GLOBAL DEFAULT_V2_DB regardless of db_path. Since v2 is PRIMARY (new atoms go v2-only),
        # `remember()` ALWAYS wrote the real soul bank even when db_path pointed at a scratch db —
        # so an isolated SeedStore (for tests, or an alternate bank) was impossible. Fix is minimal
        # + backward-compatible: v2_db defaults to the global, so every existing call site is
        # unchanged; passing v2_db redirects the primary store too. The two dbs stay independently
        # addressable, matching how db_path already redirected the (now-frozen) v1 store.
        self.db_path = Path(db_path)
        self.u = UAME(db_path)   # the real store
        self.cards = CardStore(v2_db if v2_db is not None else DEFAULT_V2_DB)
        # Per-instance memo for the warmth edge-walk hot path (links_of / seed_by_id). The BFS in
        # warmth() revisits the same nodes ~1086 times across a single recall, and each call pays the
        # expensive v1<->v2 ID translation. The graph is READ-ONLY during a recall and a SeedStore is
        # created fresh per recall, so memoizing within the instance is correct (cannot go stale across
        # recalls) and collapses the 1086 lookups to one-per-unique-id. See warmth-travels-the-edge.
        self._links_cache: dict[str, list[dict]] = {}
        self._seed_cache: dict[str, "Seed | None"] = {}
        # V1 DETACHED (owner 2026-06-26): v2 is now richer in weight than v1, so there is nothing left
        # to cold-borrow. When detached, recall walks v2 atom_links in v2-id space DIRECTLY — no
        # _v1_id_for_v2 translation, no _all_tables() coordinate scan, no id-bridge. That deletes the
        # ~96%-of-recall-time v1 work (the BFS no longer lives in v1-id space). Env override keeps the
        # old v1∪v2 union available if a bank still needs the drain. See warmth-travels-the-edge.
        import os as _os
        self._v1_detached = _os.environ.get("ECHELON_V1_ATTACHED", "0") != "1"
        # FOREIGN-SCOPE EDGE GUARD (S8b V8b): per-call ledger of bare-slug [[refs]] that only
        # matched another estate — (name, scope, best_foreign_id). Read by the ingest summary.
        self.last_foreign_skips: list = []

    def remember(self, scope: str, content: str, kind: str = "note",
                 tier: str = "working", supersedes: str = "",
                 valence: float = 0.0, arousal: float = 0.0, coordinate: str = "",
                 self_seed: bool = False) -> str:
        return self.remember_many([{
            "scope": scope, "content": content, "kind": kind, "tier": tier,
            "supersedes": supersedes, "valence": valence, "arousal": arousal,
            "coordinate": coordinate, "self_seed": self_seed,
        }])[0]

    def remember_many(self, items: list[dict]) -> list[str]:
        """Bank atoms, then resolve authored links after all attempted writes.

        V2 commits each atom separately. Success returns legacy content IDs in
        input order; partial or uncertain writes/links raise BatchRememberError
        carrying per-entry outcomes. Inspect these before retrying: a raised
        batch can contain committed atoms. This is not an atomic batch.
        """
        self.last_batch_report = []
        if not items:
            return []
        self.last_foreign_skips = []
        entries = []
        for it in items:
            scope = it["scope"]; coord = it.get("coordinate", "")
            entries.append(Entry(
                content=it["content"], domain=scope_to_domain(scope, coord), coordinate=coord,
                kind=it.get("kind", "note"), permanent=(it.get("tier", "working") == "core"),
                scope=scope, supersedes=it.get("supersedes", ""),
                valence=it.get("valence", 0.0), arousal=it.get("arousal", 0.0),
                self_seed=it.get("self_seed", False)))
        # v1 STARVE (2026-06-16): when frozen, the batch is born in v2 ONLY — v1 gets nothing new (the
        # main leak was here: ingest/bulk-plant ALL go through this path). e.id is content-addressed so
        # it is a stable id regardless of v1. See _V1_FROZEN.
        ids = [e.id for e in entries] if _V1_FROZEN else self.u.append_many(entries)
        if not _V1_FROZEN:
            # legacy v1 uame_links seed-and-link — only while v1 is a live writer.
            for e in entries:
                for name in _REF.findall(e.content):
                    target = self._resolve_ref(name, scope=e.scope)
                    if target and target != e.id:
                        try:
                            self.u.link(e.id, target, "refs")
                        except Exception:
                            pass
        # v2 PRIMARY write — born complete in v2; the only home for a new atom when v1 is frozen.
        report = []
        for input_index, e in enumerate(entries):
            outcome = {"input_index": input_index, "coordinate": e.coordinate or e.scope, "scope": e.scope, "seed_id": e.id,
                       "atom_id": None, "write_status": "unknown", "link_status": "not_attempted"}
            report.append(outcome)
            try:
                bank_result = self.cards.bank_atom(e.coordinate or e.scope, e.content, born_from=e.id,
                                                   scope=e.scope, kind=e.kind, valence=e.valence, arousal=e.arousal)
                atom_id = bank_result["atom_id"]
                outcome.update(bank_result)
                actual = self.cards.get_atom(atom_id)
                if (actual is None or actual.content != e.content
                        or actual.scope != e.scope
                        or actual.coordinate != norm_coord(e.coordinate or e.scope)):
                    raise ValueError("committed atom read-back mismatch")
                outcome.update(atom_id=atom_id, write_status="committed")
            except Exception as v2_err:
                from .bank_review import BankTransactionError
                if isinstance(v2_err, BankTransactionError):
                    outcome.update(v2_err.outcome)
                    outcome["write_status"] = {
                        "conflict": "conflict", "failed": "failed",
                        "not_attempted": "not_attempted",
                    }.get(v2_err.outcome["insertion"], "unknown")
                outcome["error_type"] = type(v2_err).__name__
                import logging
                logging.getLogger(__name__).error(
                    "v2 PRIMARY batch write uncertain for seed %s scope=%r error_type=%s", e.id, e.scope, type(v2_err).__name__)
        # v2 atom_links seed-and-link pass — now every in-batch atom exists in v2, so intra-batch
        # [[refs]] resolve to v2 ids and become typed edges in the atlas edge layer (the soul graph in core).
        for e, outcome in zip(entries, report):
            src = outcome["atom_id"]
            if not src or outcome["write_status"] != "committed":
                continue
            outcome["link_status"] = "complete"
            outcome["references"] = []
            for name in _REF.findall(e.content):
                reference = {"name": name, "status": "unknown", "target_id": None}
                outcome["references"].append(reference)
                try:
                    skips_before = len(self.last_foreign_skips)
                    tgt = self._v2_ref_target(name, e.scope)
                    reference["target_id"] = tgt
                    if tgt == src:
                        reference["status"] = "self_skipped"
                    elif tgt:
                        self.cards.link(src, tgt, "refs")
                        # INSERT OR IGNORE may encounter a tombstoned edge: verify live state.
                        live = self.cards.conn.execute(
                            "SELECT 1 FROM atom_links WHERE from_id=? AND to_id=? AND relation='refs' AND superseded_on=0",
                            (src, tgt)).fetchone()
                        reference["status"] = "linked" if live else "retired"
                    else:
                        reference["status"] = "foreign_scope_skipped" if len(self.last_foreign_skips) > skips_before else "unresolved"
                except Exception as exc:
                    reference.update(status="failed", error_type=type(exc).__name__)
                    outcome["link_status"] = "partial"
                    outcome["link_error_type"] = type(exc).__name__
            if outcome["link_status"] == "complete" and any(
                    r["status"] in {"unresolved", "foreign_scope_skipped", "retired"} for r in outcome["references"]):
                outcome["link_status"] = "complete_with_unlinked"
            outcome["reference_counts"] = {status: sum(r["status"] == status for r in outcome["references"])
                for status in ("linked", "self_skipped", "unresolved", "foreign_scope_skipped", "retired", "failed")}
        # Offer persisted neighbors after the batch link pass, including partial links.
        # Recovery is capture-only: never replay an atom mutation to obtain this offer.
        from .bank_review import capture_review, BankTransactionError
        for outcome in report:
            outcome["review_status"] = "not_attempted"
            if outcome["write_status"] != "committed":
                continue
            if not outcome.get("review_id"):
                outcome["review_status"] = "historical_unqueued"
                continue
            try:
                event = capture_review(self.cards, outcome["review_id"], link_status=outcome["link_status"], references=outcome.get("references", []))
                outcome["review_status"] = event["event_kind"]
                outcome["review_event_id"] = event["event_id"]
                outcome["review"] = event["payload"]
            except Exception as exc:
                outcome["review_status"] = "unavailable"
                outcome["review_error_type"] = type(exc).__name__
                if isinstance(exc, BankTransactionError):
                    outcome["review_uncertain_operation"] = exc.outcome
        self.last_batch_report = report
        if any(r["write_status"] != "committed" or r["link_status"] not in {"complete", "complete_with_unlinked"}
               or r["review_status"] not in {"projection_available", "historical_unqueued"} for r in report):
            raise BatchRememberError(report)
        return ids

    def _resolve_ref(self, name: str, *, scope: str | None = None) -> str | None:
        """Resolve a [[name]] to a seed id by its coordinate slug (the [tag] a seed carries).
        Most recent match wins. Returns None if nothing carries that coordinate yet.

        FOREIGN-SCOPE EDGE GUARD (S8b V8b): when `scope` is given, resolve FIRST within that
        scope's OWN domain tables (scope_to_domain(scope) -> core_<dom>/working_<dom>,
        existence-checked like uame.count/scan, filtered by the scope column) — the authoring
        atom's estate wins even when a foreign scope holds a newer same-slug atom (the
        cross-estate link bug). No same-scope match: an EXPLICIT coordinate path (contains ':')
        resolves across all tables as today (the author wrote [[scope:…]] to mean it); a bare
        slug returns None and is recorded in self.last_foreign_skips as (name, scope,
        best_foreign_id) — NEVER link a bare slug into a foreign scope."""
        slug = name.strip().lower()
        if scope is not None and ":" not in slug:
            dom = scope_to_domain(scope)
            best_id, best_ts = None, -1
            with self.u._lock:
                for tbl in (f"core_{dom}", f"working_{dom}"):
                    if not self.u.conn.execute(
                            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                            (tbl,)).fetchone():
                        continue
                    for row in self.u.conn.execute(
                            f"SELECT id, ts FROM {tbl} WHERE lower(coordinate)=? AND scope=?",
                            (slug, scope)).fetchall():
                        if row["ts"] > best_ts:
                            best_id, best_ts = row["id"], row["ts"]
            if best_id is not None:
                return best_id
            # bare slug with no same-scope match — record the foreign skip, never link across
            foreign = self._resolve_ref(name)
            if foreign is not None:
                self.last_foreign_skips.append((name.strip(), scope, foreign))
            return None
        best_id, best_ts = None, -1
        with self.u._lock:
            for tbl in self.u._all_tables():
                for row in self.u.conn.execute(
                        f"SELECT id, ts FROM {tbl} WHERE lower(coordinate)=?", (slug,)).fetchall():
                    if row["ts"] > best_ts:
                        best_id, best_ts = row["id"], row["ts"]
        return best_id

    def _v2_ref_target(self, name: str, scope: str) -> str | None:
        """Scope-guarded v2 ref resolution — the PRODUCTION seed-and-link path (the v1 legacy
        _resolve_ref above only runs while v1 is unfrozen). Same guard, same rule: a bare slug
        resolves within the authoring atom's scope ONLY (exact coordinate, then last-segment —
        mirroring cards.atom_id_for_coordinate's semantics, confined to scope=?); no same-scope
        match -> record the foreign skip (name, scope, best_foreign_id) and return None; an
        explicit [[scope:…]] coordinate still resolves cross-scope (atom_id_for_coordinate as
        today)."""
        slug = name.strip().lower()
        coord = norm_coord(slug)
        if ":" in slug:
            # SAME-SCOPE FIRST even for a colon path (gate r1 V8b M-1): the estate's own
            # [[domain:topic:atom]] form must not bypass the guard — two estates holding the
            # same coordinate with a newer foreign row is the original bug wearing a colon.
            with self.cards._lock:
                r = self.cards.conn.execute(
                    "SELECT id FROM atoms WHERE scope=? AND coordinate=? ORDER BY ts DESC LIMIT 1",
                    (scope, coord)).fetchone()
            if r is not None:
                return r["id"]
            return self.cards.atom_id_for_coordinate(name)   # explicit cross-scope, by intent
        with self.cards._lock:
            r = self.cards.conn.execute(
                "SELECT id FROM atoms WHERE scope=? AND coordinate=? ORDER BY ts DESC LIMIT 1",
                (scope, coord)).fetchone()
            if r is None:
                r = self.cards.conn.execute(
                    "SELECT id FROM atoms WHERE scope=? AND coordinate LIKE ? ORDER BY ts DESC LIMIT 1",
                    (scope, "%:" + coord)).fetchone()
        if r is not None:
            return r["id"]
        foreign = self.cards.atom_id_for_coordinate(name)
        if foreign is not None:
            self.last_foreign_skips.append((name.strip(), scope, foreign))
        return None

    def promote(self, seed_id: str, scope: str) -> bool:
        """working_<domain> -> core_<domain>, atomic cross-table move (the soul gate)."""
        return self.u.promote(seed_id, scope_to_domain(scope))

    def self_seed(self, scope: str, content: str, kind: str = "lesson",
                  coordinate: str = "", valence: float = 0.0, arousal: float = 0.0) -> str:
        """EXIT B of the respect handshake (dream-and-the-respect-handshake). An LLM WISHES a seed
        kept though it did NOT meet the requirement. It is written DIRECTLY to the persona's OWN
        core (tier='core', this scope's table), with the immutable self_seed=True flag — sovereign,
        UNDELETABLE, and PERMANENTLY ineligible for the global soul (levels.eligible_for_global).

        This is NOT a back door into L1: the flag travels with the row, and grandvote/confirmation
        refuse any seed where eligible_for_global() is False. The model is sovereign over its OWN
        core; it can never reach the SHARED one. 'Respect is meant to be both ways.' No delete: even
        a wished conviction can only fall out of recall, never be erased (append-only is the law)."""
        return self.remember(scope, content, kind=kind, tier="core",
                             coordinate=coordinate, valence=valence, arousal=arousal,
                             self_seed=True)

    def _v2_atom_by_born_from(self, seed_id: str):
        """Resolve the v2 atom whose born_from == seed_id (the remember()-returned v1 content-id).
        The companion to cards.get_atom (which matches the atom's OWN id): a v2-born seed is held by
        callers under its born_from, not the atom id. Newest match wins. Returns an Atom or None."""
        try:
            with self.cards._lock:
                r = self.cards.conn.execute(
                    "SELECT id FROM atoms WHERE born_from=? ORDER BY ts DESC LIMIT 1",
                    (seed_id,)).fetchone()
            return self.cards.get_atom(r["id"]) if r else None
        except Exception:
            return None

    def seed_by_id(self, seed_id: str) -> Seed | None:
        """Memoized wrapper (per-recall cache) over _seed_by_id_uncached — see __init__."""
        if seed_id in self._seed_cache:
            return self._seed_cache[seed_id]
        s = self._seed_by_id_uncached(seed_id)
        self._seed_cache[seed_id] = s
        return s

    def _seed_by_id_uncached(self, seed_id: str) -> Seed | None:
        """Fetch a single seed by its id, across all tables (the id is content-addressed, unique).
        Used by warmth's edge-traversal to materialize a linked seed lexical never surfaced."""
        # V1 DETACHED (2026-06-26): the pool is v2-native, so go STRAIGHT to v2 — skip the v1 `_row`
        # table-walk (it was 366 calls / 0.27s of the post-detach recall, the last v1 leak in the path).
        if self._v1_detached:
            a = self.cards.get_atom(seed_id) or self._v2_atom_by_born_from(seed_id)
            if a is not None:
                return Seed(id=a.id, scope=a.scope, content=a.content, kind=a.kind or "note",
                            tier="working", supersedes="", ts=a.ts, valence=a.valence,
                            arousal=a.arousal, score=a.score, recall_count=0,
                            coordinate=a.coordinate, self_seed=False)
            return None
        # _row searches the guessed domain then ALL tables; domain hint is irrelevant for by-id.
        tbl, _perm, row = self._row(seed_id, "general")
        if row is None:
            # v2 fallback (the drain): a v2-born atom isn't in any v1 table — materialize from v2.
            # BUG FOUND IN MIGRATION (2026-06-18): get_atom matches the v2 atom's OWN id, but a
            # v2-born seed's id (the one remember() returns + callers hold) is the v1 content-id,
            # stored on the v2 atom as born_from — NOT as its id. So get_atom(seed_id) MISSED every
            # post-freeze seed, and seed_by_id returned None. That broke grandvote.ratify ("candidate
            # vanished") and warmth's edge-traversal for any v2-born seed. Fix: try the atom's own id
            # first (a union-read id), then fall back to a born_from match (the remember()-returned id).
            a = self.cards.get_atom(seed_id) or self._v2_atom_by_born_from(seed_id)
            if a is not None:
                return Seed(id=a.id, scope=a.scope, content=a.content, kind=a.kind or "note",
                            tier="working", supersedes="", ts=a.ts, valence=a.valence,
                            arousal=a.arousal, score=a.score, recall_count=0,
                            coordinate=a.coordinate, self_seed=False)
            return None
        e = Entry(content=row["content"], domain=tbl.split("_", 1)[1], coordinate=row["coordinate"],
                  kind=row["kind"], permanent=tbl.startswith("core_"), scope=row["scope"],
                  supersedes=row["supersedes"], valence=row["valence"], arousal=row["arousal"],
                  self_seed=bool(row["self_seed"]) if "self_seed" in row.keys() else False)
        e.id = row["id"]; e.ts = row["ts"]; e.score = row["score"]; e.recall_count = row["recall_count"]
        return self._to_seed(e)

    def think_about(self, problem: str, scope: str = "echelon", top_k: int = 7) -> dict:
        """The one-call THINK (owner 2026-06-16): given a PROBLEM string, recall the warm experience
        associatively (warmth travels the soul edges — the fix that makes recall pull in RELATED
        lessons, not just lexical hits), resolve those seeds to their v2 atom ids, and hand them to
        cards.think() which composes + replays a reasoning chain over the EARNED atoms — no model in
        the hot path. Returns think()'s {chain, confidence, blocked, path}, plus 'recalled'. A thought
        that pays off is crystallized via cards.crystallize() into a named card. See think (cards.py)."""
        from .warmth import warmth as _warmth
        reading = _warmth(problem, self, scope, top_k=top_k)
        v2_ids, recalled = [], []
        for sw in reading.warmest:
            coord = sw.seed.coordinate
            v2 = self.cards.atom_id_for_coordinate(coord) if coord else None
            if v2:
                v2_ids.append(v2)
                recalled.append({"coordinate": coord, "warmth": sw.score,
                                 "via_rel": getattr(sw, "via_rel", "")})
        out = self.cards.think(v2_ids, budget=top_k)
        out["recalled"] = recalled
        return out

    def links_of(self, seed_id: str) -> list[dict]:
        """Memoized wrapper (per-recall cache) over _links_of_uncached — see __init__."""
        cached = self._links_cache.get(seed_id)
        if cached is not None:
            return cached
        out = self._links_of_uncached(seed_id)
        self._links_cache[seed_id] = out
        return out

    def _links_of_uncached(self, seed_id: str) -> list[dict]:
        """Soul-graph edges touching seed_id, spanning v1 ∪ v2 (2026-06-16, the v1-starve drain). The
        warmth chain-walk reads THIS, not u.links_of directly, because new edges now land in v2's
        atom_links (the atlas edge layer) while legacy edges still live in v1's uame_links.

        THE ID-BRIDGE (2026-06-16 — the recall fix). The seed POOL carries v1 ids, but the rich v2
        atom_links edges are keyed by CONTENT-ADDRESSED v2 ids — so a naive v2 lookup by a v1 id finds
        NOTHING (the bug: 797 v2 edges were invisible to the walk, recall stayed lexical-only and never
        pulled in related experience). Fix: resolve the seed's COORDINATE to its v2 twin id, pull the v2
        edges, then TRANSLATE each v2 endpoint id back to the pool's v1 seed id (by coordinate) so the
        BFS — which lives in v1-id space — can actually traverse them. Returns the v1 shape
        {from_id,to_id,relation}, deduped. This is what makes recall ASSOCIATIVE (warmth travels the
        edge to the connected lesson) instead of myopically lexical. See warmth-travels-the-edge.

        V1 DETACHED FAST PATH (2026-06-26): when v1 is detached, the seed pool is v2-native (ids ARE v2
        atom ids), so we walk atom_links DIRECTLY in v2-id space — no coordinate resolution, no
        _v1_id_for_v2 (the ~96% cost), no _all_tables scan. Endpoints stay v2 ids, which the v2-native
        pool already uses, so the BFS traverses them as-is."""
        seen, out = set(), []
        if self._v1_detached:
            try:
                for e in self.cards.edges_of(seed_id):   # v2 atom_links, already v2-id keyed
                    k = (e["from_id"], e["to_id"], e["relation"])
                    if k not in seen:
                        seen.add(k); out.append({"from_id": e["from_id"], "to_id": e["to_id"],
                                                 "relation": e["relation"]})
            except Exception:
                pass
            return out
        try:
            for e in self.u.links_of(seed_id):   # v1 uame_links — already v1-id keyed
                k = (e["from_id"], e["to_id"], e["relation"])
                if k not in seen:
                    seen.add(k); out.append(dict(e))
        except Exception:
            pass
        # v2 atom_links — resolve THIS seed to its v2 id, then translate edge endpoints back to v1 ids.
        try:
            coord = self._coordinate_of(seed_id)
            v2_self = self.cards.atom_id_for_coordinate(coord) if coord else None
            if v2_self:
                for e in self.cards.edges_of(v2_self):   # live v2 edges on the twin
                    f_v1 = self._v1_id_for_v2(e["from_id"]) or e["from_id"]
                    t_v1 = self._v1_id_for_v2(e["to_id"]) or e["to_id"]
                    k = (f_v1, t_v1, e["relation"])
                    if k not in seen:
                        seen.add(k); out.append({"from_id": f_v1, "to_id": t_v1, "relation": e["relation"]})
        except Exception:
            pass
        return out

    def _coordinate_of(self, seed_id: str) -> str | None:
        """The coordinate (stable cross-store key) of a seed id — v1 first, then v2."""
        try:
            tbl, _p, row = self._row(seed_id, "general")
            if row is not None and row["coordinate"]:
                return row["coordinate"]
        except Exception:
            pass
        a = self.cards.get_atom(seed_id)
        return a.coordinate if a else None

    def _v1_id_for_v2(self, v2_id: str) -> str | None:
        """Translate a v2 atom id -> the pool's v1 seed id for the same atom, BY COORDINATE. Cached.
        Returns None if no v1 twin (a v2-native atom) — caller falls back to the v2 id itself."""
        cache = getattr(self, "_v2v1_cache", None)
        if cache is None:
            cache = {}
            self._v2v1_cache = cache
        if v2_id in cache:
            return cache[v2_id]
        a = self.cards.get_atom(v2_id)
        result = None
        if a and a.coordinate:
            try:
                with self.u._lock:
                    for tbl in self.u._all_tables():
                        r = self.u.conn.execute(
                            f"SELECT id FROM {tbl} WHERE coordinate=? ORDER BY ts DESC LIMIT 1",
                            (a.coordinate,)).fetchone()
                        if r:
                            result = r["id"]; break
            except Exception:
                result = None
        cache[v2_id] = result
        return result

    def count(self, scope: str | None = None, tier: str | None = None) -> int:
        """How many seeds in a SCOPE (the human axis) — without raw SQL or a guessed table name, and
        without building full Seed objects. A scope fragments across many domain tables (filtered by
        the `scope` COLUMN, not a table), so this counts by scope across every soul table. scope=None
        -> the whole soul (delegates to uame.count). The proper answer to 'how big is scope X?'."""
        if scope is None:
            return self.u.count(permanent=(None if tier is None else (tier == "core")))
        permanent = None if tier is None else (tier == "core")
        with self.u._lock:
            total = 0
            for t in self.u._all_tables(None if permanent is None else
                                        ("core_" if permanent else "working_")):
                total += self.u.conn.execute(
                    f"SELECT COUNT(*) c FROM {t} WHERE scope=?", (scope,)).fetchone()["c"]
            return total

    def stats(self) -> dict:
        """Whole-substrate census (delegates to uame.stats) — the 'what's in here?' call."""
        return self.u.stats()

    def seeds(self, scope: str | None = None, tier: str | None = None) -> list[Seed]:
        """List as Seed objects (caller-facing). tier maps to the prefix. None/None -> everything.

        SCOPE FILTERING walks ALL domain tables and filters by the stored `scope` column — NOT by
        a single scope->domain table. A scope FRAGMENTS across many coordinate-named tables (each
        `coordinate` routes into its own domain via scope_to_domain), so a one-domain scan silently
        MISSES a scope's coordinate-carrying seeds (the bug: seeds(scope='claude-self') returned 0
        of the hand-written seeds because they landed in working_<coordinate> tables, not
        working_claude). The scope column is the authoritative filter; the domain table is only a
        storage location. See uame-schema-two-prefix-multi-table (coordinate-as-domain fragments a
        scope across tables)."""
        permanent = None if tier is None else (tier == "core")
        out = []
        seen_content = set()
        # V1 DETACHED (2026-06-26): skip the v1 scan entirely — the v2 union below is the whole pool now
        # (v2 is richer than v1), and skipping keeps the seed ids v2-native so the v2-id-space edge walk
        # traverses them without translation. Re-attach with ECHELON_V1_ATTACHED=1.
        if not self._v1_detached:
            # domain=None -> scan every family; the per-row scope filter below is the real selector.
            entries = self.u.scan(domain=None, permanent=permanent)
            for e in entries:
                if scope is not None and e.scope != scope:
                    continue
                out.append(self._to_seed(e))
                seen_content.add((e.content or "")[:120])
        # READ SPANS v1 ∪ v2 (2026-06-16, the v1-starve endgame): a NEW atom is born in v2 ONLY (v1 is
        # frozen, draining to stale — _V1_FROZEN). So the v1 scan above MISSES every post-freeze atom;
        # union in the v2 atoms the v1 pool didn't already cover (dedup by the stable content-prefix key
        # the cold-borrow + dispute gate use). This keeps recall whole during the drain — v1 ages out,
        # nothing goes blind. When v1 is empty of live atoms, this union simply becomes the v2 read.
        try:
            for a in self.cards.atoms_in_scope(scope):
                key = (a.content or "")[:120]
                if key in seen_content:
                    continue
                seen_content.add(key)
                out.append(Seed(
                    id=a.id, scope=a.scope or (scope or ""), content=a.content, kind=a.kind or "note",
                    tier=("core" if (a.score or 0) >= 0 and False else "working"),
                    supersedes="", ts=a.ts, valence=a.valence, arousal=a.arousal,
                    score=a.score, recall_count=0, coordinate=a.coordinate, self_seed=False))
        except Exception:
            pass  # v2 unreadable -> degrade to the v1-only pool, never crash
        # SUPERSEDE = honest replacement: a superseded seed must NOT surface alongside its
        # corrected version (else the boot presents the stale claim AND its fix — worse than no
        # fix). Drop any seed whose id is the `supersedes` target of another seed IN THIS RESULT
        # SET (only when the superseder is present, so a scope/tier filter can't silently hide a
        # seed whose replacement was filtered out — the chain stays intact on disk regardless).
        superseded_ids = {s.supersedes for s in out if s.supersedes}
        if superseded_ids:
            out = [s for s in out if s.id not in superseded_ids]
        # SUBSUMED = hygiene's honest dedup (slice 1.5): a verbatim duplicate carries a live
        # `subsumes` atom_links edge FROM its canonical — the wk-group precedent (parent scope
        # --subsumes--> member, ECHELON commit 4dd1b67) at atom grain. Same discipline as the
        # supersede drop above: the copy is dropped ONLY when its canonical is present in this
        # result set (a scoped recall inside the copy's own scope still surfaces the copy — the
        # member keeps its memory), and NOTHING is deleted — the row + its earned history stay
        # legible; recall just prefers the canonical when both would surface. Best-effort: v2
        # unreadable -> no drop, never crash.
        try:
            sub_pairs = self.cards.live_subsume_pairs() if getattr(self, "cards", None) else []
        except Exception:
            sub_pairs = []
        if sub_pairs:
            present = {s.id for s in out}
            drop_ids = {dup for canon, dup in sub_pairs if canon in present and dup in present}
            if drop_ids:
                out = [s for s in out if s.id not in drop_ids]
        return out

    @staticmethod
    def _to_seed(e: "Entry") -> Seed:
        s = Seed(id=e.id, scope=e.scope, content=e.content, kind=e.kind,
                 tier=("core" if e.permanent else "working"), supersedes=e.supersedes,
                 ts=e.ts, valence=e.valence, arousal=e.arousal, score=e.score,
                 recall_count=e.recall_count, coordinate=e.coordinate,
                 self_seed=getattr(e, "self_seed", False))
        return s

    def _row(self, seed_id: str, domain: str):
        """Find a row by id. The id is the real key (content-addressed, PK per table), so we try
        the guessed (prefix,domain) tables first, then fall back to ALL tables — robust to any
        domain-derivation inconsistency between the write path and the read path. Returns
        (table, permanent, row) and the table name carries the true domain."""
        with self.u._lock:   # shared cross-thread connection — serialize (bg-harness worker threads)
            # fast path: the two tables for the guessed domain
            for perm in (False, True):
                tbl = f"{'core' if perm else 'working'}_{domain}"
                if self.u.conn.execute(
                        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (tbl,)).fetchone():
                    row = self.u.conn.execute(f"SELECT * FROM {tbl} WHERE id=?", (seed_id,)).fetchone()
                    if row:
                        return tbl, perm, row
            # fallback: search every family by id (the id IS unique across the store)
            for tbl in self.u._all_tables():
                row = self.u.conn.execute(f"SELECT * FROM {tbl} WHERE id=?", (seed_id,)).fetchone()
                if row:
                    return tbl, tbl.startswith("core_"), row
            return None, None, None

    def reinforce(self, seed_id: str, scope: str, q: float, allow_promote: bool = False) -> dict:
        """COS rating-on-resonance + earned promotion (working->core). Operates on the (prefix, domain)
        tables.

        allow_promote DISABLES (default) the working->core auto-promote — it ONLY raises the score.
        This is the CONSTITUTIONAL fix, now the DEFAULT (audit #1, 2026-06-11): promotion to the L1
        soul is GRAND-VOTE-ONLY (dream -> grandvote.nominate -> store.promote). A vote/warmth-driven
        score crossing PROMOTE_THRESHOLD must NOT silently elevate a seed to core, bypassing the
        collective — that side door (the old default=True) is the bypass the audit found, and closing
        it costs nothing: a denied seed keeps earning score in working tier, stays fully recallable,
        and the dream picks it up the instant it both meets_requirement AND is witnessed (levels.py),
        nominating it through the FRONT door. NEVER-DELETE makes the wait free — only L1 AUTHORITY is
        deferred, never the seed. So no separate 'promotion_proposed' flag is needed: meets_requirement
        already reads score+recalls off the seed, so the threshold-crosser IS the dream's Exit-A
        candidate by construction. The dream itself (dream.py) still calls store.promote directly after
        a grand vote — flipping THIS default does not touch that legitimate path. Callers that truly
        intend a direct promote (the ratify path) pass allow_promote=True explicitly. See
        dream-and-the-respect-handshake, test_collective_ratifies_into_l1."""
        # v1 STARVE (2026-06-16): warmth updates are one of the two things keeping v1 alive. When frozen,
        # v1 scores STOP MOVING — earning is a v2 concern (cards.reinforce_card via the trace-loop). v1
        # drains to stale. We no-op the v1 score write here; the dispute/earn engine lives in v2. (A
        # v2-born atom isn't in v1 anyway -> _row returns None below; this also freezes legacy v1 rows.)
        if _V1_FROZEN:
            return {"ok": True, "frozen": True, "note": "v1 warmth frozen; earn via v2 reinforce_card"}
        tbl, perm, row = self._row(seed_id, scope_to_domain(scope))
        if row is None:
            return {"ok": False, "reason": "no such seed"}
        # Act on the domain we ACTUALLY FOUND the row in (from the table name), NOT a re-derived
        # one — the write may have routed by coordinate, this call may not have it. (See
        # uame-id-and-domain-routing: prefer looked-up location over re-derived location.)
        found_dom = tbl.split("_", 1)[1]
        delta = (q - 50.0) * SCORE_K
        hist = json.loads(row["score_history"] or "[]")
        hist.append([int(time.time()), round(delta, 3)])
        new_score = compute_score(hist)
        new_recalls = row["recall_count"] + 1
        with self.u._lock:
            self.u._retry(f"UPDATE {tbl} SET score=?, score_history=?, recall_count=? WHERE id=?",
                          (new_score, json.dumps(hist), new_recalls, seed_id))
            self.u.conn.commit()
        promoted = False
        if allow_promote and (not perm) and new_score > PROMOTE_THRESHOLD and new_recalls >= PROMOTE_MIN_RECALLS:
            promoted = self.u.promote(seed_id, found_dom)
        return {"ok": True, "score": round(new_score, 2), "delta": round(delta, 3),
                "recalls": new_recalls, "promoted": promoted}

    def consolidate(self, scope: str | None = None) -> dict:
        """The dream: re-derive every entry's decay-weighted score across the requested families.
        Nothing deleted; core never auto-demoted here (heavier decision)."""
        domain = scope_to_domain(scope) if scope is not None else None
        tables = []
        if domain is not None:
            tables = [f"core_{domain}", f"working_{domain}"]
        else:
            tables = self.u._all_tables()
        changed = 0
        faded = []
        now = int(time.time())
        with self.u._lock:
            for tbl in tables:
                if not self.u.conn.execute(
                        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (tbl,)).fetchone():
                    continue
                perm = tbl.startswith("core_")
                for row in self.u.conn.execute(f"SELECT id,score,score_history FROM {tbl}").fetchall():
                    hist = json.loads(row["score_history"] or "[]")
                    new = compute_score(hist, now=now)
                    if abs(new - row["score"]) > 1e-6:
                        self.u._retry(f"UPDATE {tbl} SET score=? WHERE id=?", (new, row["id"]))
                        changed += 1
                    if (not perm) and new < SCORE_BENCHMARK and hist:
                        faded.append((row["id"], round(new, 1)))
                self.u.conn.commit()
            scanned = sum(1 for t in tables if self.u.conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (t,)).fetchone()
                for _ in self.u.conn.execute(f"SELECT 1 FROM {t}"))
        return {"scanned": scanned, "rescored": changed, "faded": faded}

    def mark_kind(self, seed_id: str, scope: str, new_kind: str) -> bool:
        """Flip a seed's kind in place (e.g. raw -> raw-consumed after distillation). Append-only
        spirit: the record survives, only its kind changes. Targets the right (prefix,domain) table.

        BUG FOUND IN MIGRATION (2026-06-18): the original ONLY updated the v1 UAME table via _row().
        Under the frozen-v1 regime a new seed is born v2-ONLY (cards), so it has NO v1 row — _row
        returned None and mark_kind was a SILENT NO-OP. That broke distill._mark_consumed: a consumed
        raw could never be retired, so distill_raw re-distilled the same raw every pass (lost
        idempotency = repeated model spend). Fix: also flip the kind on the v2 atom (the PRIMARY store),
        matched by born_from=seed_id (how store.remember links the v2 twin). Return True if EITHER store
        was updated, so a v2-born seed now reports success."""
        updated = False
        # v1 (frozen, but a legacy seed may still live there) — best-effort, never the only path now.
        tbl, _, row = self._row(seed_id, scope_to_domain(scope))
        if row is not None:
            with self.u._lock:
                self.u._retry(f"UPDATE {tbl} SET kind=? WHERE id=?", (new_kind, seed_id))
                self.u.conn.commit()
            updated = True
        # v2 PRIMARY — the real home of a post-freeze seed. Match on EITHER axis: a v1-origin
        # seed_id links the v2 twin via born_from; but a seed READ BACK from the v1∪v2 union carries
        # the v2 ATOM's OWN id (store.seeds reconstructs Seed.id = atom.id), so callers like
        # distill._mark_consumed pass the atom id, not the born_from. Cover both (id=? OR born_from=?)
        # so a kind-flip lands whichever id the caller holds — the subtlety that made the first fix
        # attempt miss (the union-read id != born_from).
        try:
            with self.cards._lock:
                cur = self.cards.conn.execute(
                    "UPDATE atoms SET kind=? WHERE id=? OR born_from=?", (new_kind, seed_id, seed_id))
                self.cards.conn.commit()
            if cur.rowcount > 0:
                updated = True
        except Exception:
            pass   # v2 unreadable -> fall back to the v1 result, never crash a kind-flip
        return updated

    # link passthrough (the relational layer is now first-class)
    def link(self, from_id: str, to_id: str, relation: str = "relates") -> None:
        self.u.link(from_id, to_id, relation)

    @property
    def conn(self):
        return self.u.conn

    def close(self):
        self.u.close()
