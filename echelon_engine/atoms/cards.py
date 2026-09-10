"""COS CARD LAYER — the honest seeder (v2). Significance EARNED, never asserted.

The seeder lineage (honest-seeder-v2-token-weight-forecast):
  v0 EROS      — log dump as seed (the firehose, BAD).
  v1 ECHELON-OS— summary-of-event as seed + a model ASSERTS the insight & promotes it to core
                 (OK, but NOT honest: the model grades its own homework — significance by fiat).
  v2 (this)    — the COS CARD method (COSys_DESIGN §6/§7/§9/§20), which ECHELON never built. A seed
                 is a TOKEN with a WEIGHT; significance is EARNED by use, never declared.

THE UNITS (COS §6):
  ATOM  — a seed-token: one move/fact at a COORDINATE (e.g. `action:read_file:path_memory`). Born at
          B=100, UNPROVEN. The injectable leaf.
  CARD  — "the command" (§6): REFS (coordinates of the load-bearing atoms it composes) + a live RATING
          + use_count. Owner's `action -> read_file -> path_file` IS a card's refs chain. Invoking a
          card = FORECASTING the next action; the "transformer from load-bearing seeds -> output token"
          is the SCORE-ORDERED RECURSIVE REF-EXPANSION (§7): highest-rated refs compose first, lowest
          drop under budget. "The highest weight for his environment raises the seed" = card resolution
          by earned rating.

THE HONESTY (COS §7 + §7-P2 — the whole reason v2 exists):
  - §7: "Score defines fallback order, NOT quality judgment... a candidate at 50 occupies the fallback
    position UNTIL IT EARNS ITS RANK." No atom/card asserts its own importance.
  - §7-P2 (the keystone): when a card is rated Q on use, EACH atom it loaded gets a PARTIAL delta
    `atom.delta += ((Q-50)*k) * (1/num_atoms)`. A seed does NOT rate itself — it earns weight ONLY when
    a CARD THAT USED IT SUCCEEDED. Credit flows BACKWARD from outcome to the contributing atoms. The
    v1 dishonesty (assert-then-promote) is structurally impossible here.
  - Q comes from the EXECUTION TRACE (§20: did the run work / get cheaper), not a model's say-so.

THE TIERS ARE THE CLOUD LEVELS (why insight/core is "all in COS"):
  B=100 unproven (L3 persona) -> >B+25 synthesis-eligible (L2 insight, §9) -> pinned BEACON (L1 core,
  §20.3 "significantly better than historical average -> globally visible"). Promotion is SCORE-EARNED,
  the grand vote the consent gate on top. See cloud-substrate-three-levels-grand-vote.

NEW DB by default (core_v2.db) — the existing ~/.echelon/core.db is NEVER touched (the substrate's
'nothing is ever deleted' law; the lived soul stays the immutable source). Reuses the COS score spine
(uame.compute_score, B/λ, decay) — no re-derivation.
"""
from __future__ import annotations
import hashlib
import json
import math
import os
import re
import sqlite3
import time
from pathlib import Path

from .uame import compute_score, SCORE_BENCHMARK, SCORE_LAMBDA, content_id
from .scoring import lam_for, DORMANT_THRESHOLD, DORMANT_EXEMPT

# ── leaf-atom imports — pure math/types/rules extracted from this file ────
# coord_norm: _norm_coord + _COORD_RE (pure regex, no DB)
from .coord_norm import _norm_coord, _COORD_RE              # noqa: F401 (re-exported for compat)
# card_types: Atom + Card dataclasses (pure, no DB)
from .card_types import Atom, Card                          # noqa: F401 (re-exported; importers keep working)
# card_credit: scoring constants + delta arithmetic (pure, no DB)
from .card_credit import (                                  # noqa: F401 (re-exported)
    SCORE_K, SYNTH_ELIGIBLE, BEACON_THRESHOLD,
    CHAIN_DECAY, CHAIN_MAX_HOPS, CHAIN_MIN_DELTA,
    SHOCK_BONUS, _REFLEX_STOP,
)
# card_scan_rules: immune-scan predicate functions (pure, no DB)
from .card_scan_rules import (
    is_live_lie as _scan_is_live_lie,
    check_dangling_from as _scan_dangling_from,
    check_dangling_to as _scan_dangling_to,
    check_edge_to_disclaimed as _scan_edge_to_disclaimed,
    check_unknown_relation as _scan_unknown_relation,
    check_orphan_disclaim as _scan_orphan_disclaim,
    check_live_contradiction as _scan_live_contradiction,
)
# struct_split: pure md->structured-fields parse (the compiler leaf)
from .struct_split import split_content                     # noqa: F401 (re-exported)
# earn_law: pure witnessed-door earn/decay math (the weight-move leaf)
from . import earn_law


from .echelon_home import home_db as _home_db


def _fts_sanitize(query: str) -> str:
    """Sanitize a user query for FTS5. Strips special chars, adds
    prefix matching on the last word. Returns empty string if noise."""
    q = re.sub(r"[^\w\s]", "", query).strip().lower()
    if not q or len(q) < 2:
        return ""
    words = q.split()
    if words:
        words[-1] = words[-1] + "*" if len(words[-1]) > 2 else words[-1]
    return " ".join(words)


# ── Table-name whitelist (SQL injection defense-in-depth) ──────────────────
# The `table` parameter in _mutate_score, redeem_by_trace, disclaim_judged,
# and dispute is interpolated into SQL via f-string. The CLI front door
# (correct.py) constrains --table via argparse choices, but a direct Python or
# MCP caller passing a crafted table name would get arbitrary SQL. Validate at
# the DATA LAYER so no SQL runs with an un-vetted table name.
_ALLOWED_TABLES = frozenset({"atoms", "cards"})


def _check_table(table: str) -> str:
    """Reject any table name not in the closed whitelist. Returns `table` so
    callers can write `table = _check_table(table)` as a one-liner guard."""
    if table not in _ALLOWED_TABLES:
        raise ValueError(f"illegal table name: {table!r}")
    return table


DEFAULT_V2_DB = _home_db("echelon.db")   # the STRUCTURED primary (spine/body/sidecar/
# earned additive tables; migrated from core_v2.db 2026-06-19, that backed up as core_v2_pre_struct_backup.db).
# NOT core.db — the old v1 soul is untouched. See structured-atom-additive-sidecar-schema.

_SCHEMA = """
CREATE TABLE IF NOT EXISTS atoms (
    id TEXT PRIMARY KEY, coordinate TEXT NOT NULL, content TEXT NOT NULL,
    score REAL NOT NULL DEFAULT 100.0, score_history TEXT NOT NULL DEFAULT '[]',
    use_count INTEGER NOT NULL DEFAULT 0, born_from TEXT NOT NULL DEFAULT '', ts INTEGER NOT NULL,
    scope TEXT NOT NULL DEFAULT '', kind TEXT NOT NULL DEFAULT '',
    valence REAL NOT NULL DEFAULT 0.0, arousal REAL NOT NULL DEFAULT 0.0);
CREATE INDEX IF NOT EXISTS idx_atoms_coord ON atoms(coordinate);
CREATE TABLE IF NOT EXISTS cards (
    id TEXT PRIMARY KEY, label TEXT NOT NULL, refs TEXT NOT NULL DEFAULT '[]',
    score REAL NOT NULL DEFAULT 100.0, score_history TEXT NOT NULL DEFAULT '[]',
    use_count INTEGER NOT NULL DEFAULT 0, born_from TEXT NOT NULL DEFAULT '',
    prev TEXT NOT NULL DEFAULT '', ts INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS atom_links (
    from_id TEXT NOT NULL, to_id TEXT NOT NULL, relation TEXT NOT NULL DEFAULT 'refs',
    ts INTEGER NOT NULL, superseded_on INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (from_id, to_id, relation));
CREATE INDEX IF NOT EXISTS idx_atomlinks_from ON atom_links(from_id);
CREATE INDEX IF NOT EXISTS idx_atomlinks_to ON atom_links(to_id);
"""

# STRUCTURED-ATOM ADDITIVE TABLES (structured-atom-additive-sidecar-schema). Keyed by atom id;
# `atoms.content` (the id-anchor + truth) and `atom_links` (edges) are NEVER touched. Separate tables
# by access tier: spine HOT (served every recall), body COLD (drill only), sidecar = rebuildable
# index (embedding over SPINE text only), earned = the weight moved OFF the immutable truth row.
_STRUCT_SCHEMA = """
CREATE TABLE IF NOT EXISTS atom_spine (
    atom_id TEXT PRIMARY KEY, slug TEXT NOT NULL, claim TEXT NOT NULL,
    directive TEXT NOT NULL DEFAULT '', compiler_version INTEGER NOT NULL DEFAULT 1,
    compiled_ts INTEGER NOT NULL DEFAULT 0);
CREATE INDEX IF NOT EXISTS idx_spine_slug ON atom_spine(slug);
CREATE TABLE IF NOT EXISTS atom_body (
    atom_id TEXT PRIMARY KEY, why TEXT NOT NULL DEFAULT '', evidence TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS atom_sidecar (
    atom_id TEXT PRIMARY KEY, embedding TEXT NOT NULL DEFAULT '', trigger_signals TEXT NOT NULL DEFAULT '[]');
CREATE TABLE IF NOT EXISTS atom_earned (
    atom_id TEXT PRIMARY KEY, score REAL NOT NULL DEFAULT 100.0, use_count INTEGER NOT NULL DEFAULT 0,
    score_history TEXT NOT NULL DEFAULT '[]', last_fetch_ts INTEGER NOT NULL DEFAULT 0);
CREATE INDEX IF NOT EXISTS idx_struct_earned_score ON atom_earned(score DESC);
"""

# Operational telemetry — not memory, not the soul. Impressions track which atoms
# surface on recall queries so the view-decay pass can measure "surfaced but not
# taken up." TTL-deletable (90-day horizon); see view-through decay.
_IMPRESSIONS_SCHEMA = """
CREATE TABLE IF NOT EXISTS impressions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts INTEGER NOT NULL,
    scope TEXT NOT NULL,
    query_hash TEXT NOT NULL,
    atom_coord TEXT NOT NULL,
    opened INTEGER NOT NULL DEFAULT 0,
    consumed INTEGER NOT NULL DEFAULT 0);
CREATE INDEX IF NOT EXISTS idx_impressions_scope_ts ON impressions(scope, ts);
CREATE INDEX IF NOT EXISTS idx_impressions_atom_coord ON impressions(atom_coord, ts);
"""

# wrap-review — explicit feedback layer (2026-07-31): atoms a session leaned on
# get approved or disputed at wrap. The PK (atom_id, session_window_start) enforces
# idempotence: re-running with the same slugs in the same window is a no-op.
_WRAP_REVIEW_SCHEMA = """
CREATE TABLE IF NOT EXISTS wrap_reviews (
    atom_id TEXT NOT NULL,
    session_window_start INTEGER NOT NULL,
    verdict TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    ts INTEGER NOT NULL,
    PRIMARY KEY (atom_id, session_window_start)
);
"""

# atom_links = the ATLAS architecture-edge layer, native to v2 CORE (owner 2026-06-16: the atlas model
# LIVES ON the echelon substrate, in core — not a separate JS atlas repo). The atom IS the atlas
# `define` card (one fact, content-addressed id); atom_links is the `architecture/<view>` typed edge
# layer (holds NO facts, only typed references between atom ids). Mirrors v1 uame_links (from,to,
# relation) AND the MCP knowledge-graph relationType (directed, active voice) — all three converge.
# Append-only: a retired edge gets superseded_on (the atlas tombstone, never DELETE). See atlas-pattern.
#
# THE CLOSED RELATION VOCABULARY (the atlas keys.schema discipline: every edge type must be declared,
# no free-text relations — that is what keeps the graph meaningful instead of a tag soup). Active-voice,
# directed from_id --rel--> to_id:
RELATION_TYPES = {
    "refines",      # from sharpens/extends the lesson in to
    "supersedes",   # from replaces to (the tombstone edge; pairs with dispute superseded_by)
    "instances",    # from is a concrete case of the abstract lesson to (warmth travels this edge)
    "part_of",      # from is a component of the larger procedure/topic to
    "depends_on",   # from requires to to hold first
    "contradicts",  # from is in tension with to (surface both, don't hide — honesty)
    "refs",         # generic [[link]] with no stronger type asserted yet (the default)
    "subsumes",     # from (the CANONICAL) subsumes to (a verbatim duplicate copy) — the wk-group
                    # precedent (commit 4dd1b67: parent scope --subsumes--> member via the atlas) at
                    # atom grain, added by slice-1.5 hygiene. The copy is never deleted; recall drops
                    # it only when its canonical is present in the same result set (see store.seeds).
}
# NOTE: the idx_cards_prev index is created in __init__ AFTER the prev-column migration — never in
# _SCHEMA, because on a pre-existing cards table (created before `prev` existed) the CREATE TABLE
# IF NOT EXISTS no-ops and an index referencing `prev` here would fail before the ALTER runs.


class CardStore:
    """COS card layer over its own db. The honest seeder: atoms/cards earn rank by use, never assert it."""

    def __init__(self, db_path: Path | str = DEFAULT_V2_DB):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        import threading
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(str(self.db_path), timeout=10, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        with self._lock:
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.executescript(_SCHEMA)
            # APPEND-ONLY migration: add the card→card edge column to a pre-existing cards table.
            # ADD COLUMN is non-destructive (existing rows get the DEFAULT ''); never drop/rewrite.
            cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(cards)").fetchall()}
            if "prev" not in cols:
                self.conn.execute("ALTER TABLE cards ADD COLUMN prev TEXT NOT NULL DEFAULT ''")
            # CARD_KIND governed classifier (2026-07-22, root-cause fix for the relive born_from-
            # filter rot). A non-hashed INT column (Card.id excludes it, exactly like prev), added
            # by the SAME idempotent ALTER pattern. 0=unknown is the honest legacy default; the
            # backfill + wrap stamp real codes. Filtering on this replaces the fragile
            # `born_from LIKE 'wrap-session%'` that silently missed 35 sessions. See registry.py.
            if "kind" not in cols:
                self.conn.execute("ALTER TABLE cards ADD COLUMN kind INTEGER NOT NULL DEFAULT 0")
            # Index created here (post-migration) so the prev/kind columns are guaranteed to exist —
            # covers both fresh DBs and ones migrated just above.
            self.conn.execute("CREATE INDEX IF NOT EXISTS idx_cards_prev ON cards(prev)")
            self.conn.execute("CREATE INDEX IF NOT EXISTS idx_cards_kind ON cards(kind)")
            # APPEND-ONLY migration of the RICH atom fields (2026-06-16, v2-primary read-flip step 1).
            # A v2 db created before these columns existed has a thin 8-column atoms table; ADD COLUMN
            # is non-destructive (existing rows get the DEFAULT) — never drop/rewrite. Same pattern as
            # `prev` above. The idx_atoms_scope is created HERE (post-ALTER), never in _SCHEMA — on a
            # pre-existing thin table CREATE TABLE IF NOT EXISTS no-ops, so a _SCHEMA index on `scope`
            # would fail before this ALTER runs (the same trap noted for idx_cards_prev). See
            # v2-primary-code-gap-lossy-shadow.
            acols = {r["name"] for r in self.conn.execute("PRAGMA table_info(atoms)").fetchall()}
            for col, ddl in (("scope", "TEXT NOT NULL DEFAULT ''"), ("kind", "TEXT NOT NULL DEFAULT ''"),
                             ("valence", "REAL NOT NULL DEFAULT 0.0"), ("arousal", "REAL NOT NULL DEFAULT 0.0")):
                if col not in acols:
                    self.conn.execute(f"ALTER TABLE atoms ADD COLUMN {col} {ddl}")
            self.conn.execute("CREATE INDEX IF NOT EXISTS idx_atoms_scope ON atoms(scope)")
            # STRUCTURED-ATOM ADDITIVE TABLES (structured-atom-additive-sidecar-schema, 2026-06-19).
            # The atom's content blob stays the immutable content-addressed id-anchor + truth; the
            # structured fields live in additive tables joined on atom id — nothing re-keyed, append-
            # only honored. spine=hot (every recall), body=cold (drill), sidecar=rebuildable index,
            # earned=the weight moved OFF the truth row. CREATE IF NOT EXISTS = idempotent.
            self.conn.executescript(_STRUCT_SCHEMA)
            # Operational telemetry — impressions table for view-through decay
            self.conn.executescript(_IMPRESSIONS_SCHEMA)
            self.conn.executescript(_WRAP_REVIEW_SCHEMA)
            # ── PERFORMANCE INDEX MIGRATION (2026-06-26) ──────────────────
            # recall + atoms_in_scope were doing full scans over 5K+ atoms at
            # 20MB — O(N) Python-side tokenization on every call. Fix:
            #  1. FTS5 index on atom_spine(slug, claim) — SQLite pre-filters
            #     candidates before Python n-gram overlap
            #  2. scope column on atom_spine — denormalized so scoped spine
            #     queries don't JOIN with atoms
            #  3. Compound indexes for common sort/filter patterns
            #  4. born_from prefix index for disclaim/wiring scans
            self._migrate_perf_indexes()
            # SYNC JOURNAL (2026-08-02, wave 1) — trigger-fed change capture for two-way
            # sync. OFF unless ECHELON_SYNC=1. Triggers live in the FILE (so they fire for
            # every process on this bank once installed); the applier's echo-mute is a
            # per-connection pragma needing no setup here. See sync_journal.py.
            try:
                from . import sync_journal as _sync
                _sync.attach(self.conn)
            except Exception:
                pass  # journal is additive; never break a bank open over it
            self.conn.commit()

    def _migrate_perf_indexes(self):
        """Append-only performance migration (2026-06-26). Adds FTS5 index,
        scope column on spine, compound indexes. Idempotent — IF NOT EXISTS
        on every statement. Never drops or rewrites data."""
        # 1. FTS5 virtual table on atom_spine (slug + claim) for fast text search.
        #    Uses the IMPLICIT integer rowid for content sync. External content —
        #    data stays in atom_spine, FTS is index only.
        existing_fts = self.conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='atom_spine_fts'"
        ).fetchone()
        # Detect old schema (content_rowid='atom_id' — broken, text PK not int rowid)
        has_old_schema = existing_fts is not None and "content_rowid" in (existing_fts[0] or "")
        needs_rebuild = existing_fts is None or has_old_schema
        if has_old_schema:
            self.conn.execute("DROP TABLE IF EXISTS atom_spine_fts")
        self.conn.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS atom_spine_fts "
            "USING fts5(slug, claim, content='atom_spine')")
        if needs_rebuild:
            self.conn.execute(
                "INSERT INTO atom_spine_fts(atom_spine_fts) VALUES('rebuild')")
        # 2. scope column on atom_spine — denormalized so scoped queries
        #    don't need a JOIN with atoms. Populated from atoms.scope.
        scols = {r["name"] for r in self.conn.execute(
            "PRAGMA table_info(atom_spine)").fetchall()}
        if "scope" not in scols:
            self.conn.execute(
                "ALTER TABLE atom_spine ADD COLUMN scope TEXT NOT NULL DEFAULT ''")
            # Backfill from atoms table — one-time cost. Only updates rows
            # where scope is empty AND the atom exists.
            self.conn.execute(
                "UPDATE atom_spine SET scope = COALESCE("
                "(SELECT a.scope FROM atoms a WHERE a.id = atom_spine.atom_id), '')"
                " WHERE scope = ''")
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_spine_scope ON atom_spine(scope)")
        # WITNESS PROVENANCE (B1, ruflo ADR-171/174 study 2026-07-06): how a lesson was
        # witnessed — 'execution' | 'owner' | 'inference'; '' = legacy/unset (never a demotion).
        # Additive column on atom_spine (the hot recall row), same non-destructive ALTER pattern as
        # `scope`. DISPLAY-ONLY this generation: recall SHOWS the tag but NOTHING ranks or earns on it
        # yet (ranking influence is a later, separately-gated change). See ruflo-lesson-provenance-
        # tiers-and-honest-edges.
        if "witness" not in scols:
            self.conn.execute(
                "ALTER TABLE atom_spine ADD COLUMN witness TEXT NOT NULL DEFAULT ''")
        # CLAIM CONFIDENCE (2026-07-31 gate catch: column existed in live bank but was
        # never added to the canonical init path — fresh banks lacked it, causing import
        # strict-column checks to fire. Same non-destructive ALTER pattern.)
        if "claim_confidence" not in scols:
            self.conn.execute(
                "ALTER TABLE atom_spine ADD COLUMN claim_confidence REAL NOT NULL DEFAULT 1.0")
        # 3. Compound index: scope + score for ranked recall
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_atoms_scope_score ON atoms(scope, score DESC)")
        # 4. born_from prefix index — speeds LIKE 'judged:%' and wiring scans
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_atoms_born_from ON atoms(born_from)")
        # 5. atom_earned join optimization — atom_id PK is already indexed,
        #    but add scope denorm for scoped warmth queries
        ecols = {r["name"] for r in self.conn.execute(
            "PRAGMA table_info(atom_earned)").fetchall()}
        if "scope" not in ecols:
            self.conn.execute(
                "ALTER TABLE atom_earned ADD COLUMN scope TEXT NOT NULL DEFAULT ''")
            self.conn.execute(
                "UPDATE atom_earned SET scope = COALESCE("
                "(SELECT a.scope FROM atoms a WHERE a.id = atom_earned.atom_id), '')"
                " WHERE scope = ''")
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_earned_scope_score ON atom_earned(scope, score DESC)")

    # --- write (born neutral — NO score asserted) ---
    # ── EMPTY-SCOPE GUARD (2026-07-09, slice-1.5 addendum §root-cause fix #2) ──
    # An atom planted with scope="" becomes an orphan — 175 such rows existed at the time
    # of diagnosis (the 9 hash-id orphans the dedup tombstones are a subset). Reject empty
    # scope UNLESS the coordinate head is a known system domain (these heads are written by
    # legitimate system paths: identity/self_seed/distill for persona:/value:/finding:/skill:,
    # add_steering for audit:, and clone_cartridges may propagate empty-scope system atoms).
    # The escape hatch ECHELON_LEGACY_SCOPE_OMIT allows deliberate empty-scope writes
    # (migration scripts, the test_hygiene orphan fixtures).
    _SYSTEM_DOMAIN_HEADS = frozenset({
        # PRODUCTION system domains ONLY (gate-tightened 2026-07-09): the builder had also
        # allowlisted heads that merely appear in test fixtures (tooling/echelon/memory) —
        # but `memory:*` IS the phenomenon-3 orphan mechanism this guard exists to close,
        # and `echelon:` is a real scope head; permitting them re-opens the hole. Fixtures
        # opt in per-test via ECHELON_LEGACY_SCOPE_OMIT (monkeypatch), never by weakening
        # the law to fit the fixture.
        "persona",   # identity.py / self_seed.py — persona-local identity atoms; clone_cartridges legacy
        "value",     # identity.py / self_seed.py — value-provenance atoms; clone_cartridges legacy
        "finding",   # distill.py — audit-finding atoms; clone_cartridges legacy
        "skill",     # self_seed.py — skill-definition atoms; clone_cartridges legacy
        "audit",     # echelon_engine/agent/add_steering.py:1395 — audit dispatch atoms (no scope kwarg)
    })

    def add_atom(self, coordinate: str, content: str, born_from: str = "",
                 scope: str = "", kind: str = "", valence: float = 0.0, arousal: float = 0.0) -> str:
        """Born neutral — NO score asserted. As of 2026-06-16 (v2-primary read-flip) the atom is born
        COMPLETE: scope/kind/valence/arousal carry from the v1 Entry so v2 can be the store recall ranks.
        All four default empty/0.0 — an old call site (coordinate, content, born_from only) is unchanged."""
        # EMPTY-SCOPE GUARD: reject scope="" for non-system coordinate heads.
        if not scope and not os.environ.get("ECHELON_LEGACY_SCOPE_OMIT"):
            head = (coordinate or "").split(":", 1)[0].strip().lower()
            if head not in self._SYSTEM_DOMAIN_HEADS:
                raise ValueError(
                    f"add_atom: empty scope for coordinate {coordinate!r} "
                    f"(head={head!r} not in system-domain allowlist). "
                    f"Pass scope= explicitly, or set ECHELON_LEGACY_SCOPE_OMIT=1 "
                    f"to allow empty-scope writes."
                )
        a = Atom(coordinate=coordinate, content=content, born_from=born_from,
                 scope=scope, kind=kind, valence=valence, arousal=arousal)
        with self._lock:
            self.conn.execute(
                "INSERT OR IGNORE INTO atoms (id,coordinate,content,score,score_history,use_count,born_from,ts,"
                "scope,kind,valence,arousal) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (a.id, a.coordinate, a.content, a.score, a.score_history, a.use_count, a.born_from, a.ts,
                 a.scope, a.kind, a.valence, a.arousal))
            self.conn.commit()
        return a.id

    def bank_atom(self, coordinate: str, content: str, born_from: str = "",
                  scope: str = "", kind: str = "", valence: float = 0.0, arousal: float = 0.0) -> dict:
        """Bank exact content and a pending related-memory offer atomically."""
        for name, value in (("coordinate", coordinate), ("content", content), ("scope", scope),
                            ("kind", kind), ("born_from", born_from)):
            if not isinstance(value, str):
                raise TypeError(f"{name} must be a string")
        # EMPTY-SCOPE GUARD: reject scope="" for non-system coordinate heads.
        if not scope and not os.environ.get("ECHELON_LEGACY_SCOPE_OMIT"):
            head = (coordinate or "").split(":", 1)[0].strip().lower()
            if head not in self._SYSTEM_DOMAIN_HEADS:
                raise ValueError(
                    f"add_atom: empty scope for coordinate {coordinate!r} "
                    f"(head={head!r} not in system-domain allowlist). "
                    f"Pass scope= explicitly, or set ECHELON_LEGACY_SCOPE_OMIT=1 "
                    f"to allow empty-scope writes."
                )
        a = Atom(coordinate=coordinate, content=content, born_from=born_from,
                 scope=scope, kind=kind, valence=valence, arousal=arousal)
        from .bank_review import bank_atom_transaction
        return bank_atom_transaction(self, a)

    # --- ATLAS EDGE LAYER (native v2 core): typed, directed atom→atom relations ------------------
    def link(self, from_id: str, to_id: str, relation: str = "refs") -> bool:
        """Wire a typed, directed edge from_id --relation--> to_id (the atlas architecture-edge, living
        in v2 core). relation MUST be in the closed RELATION_TYPES vocabulary (the keys.schema
        discipline — no free-text edges). Append-only + idempotent (INSERT OR IGNORE on the
        (from,to,relation) key); a self-edge or an unknown relation is rejected. Returns True if a NEW
        edge was written. The atoms themselves need not pre-exist (an edge to a not-yet-planted atom is
        allowed — append-only spirit, like seed-and-link-while-warm); resolution happens at read."""
        if not from_id or not to_id or from_id == to_id:
            return False
        if relation not in RELATION_TYPES:
            raise ValueError(f"relation {relation!r} not in closed vocabulary {sorted(RELATION_TYPES)}")
        with self._lock:
            cur = self.conn.execute(
                "INSERT OR IGNORE INTO atom_links (from_id,to_id,relation,ts) VALUES (?,?,?,?)",
                (from_id, to_id, relation, int(time.time())))
            self.conn.commit()
        return cur.rowcount > 0

    def unlink(self, from_id: str, to_id: str, relation: str) -> bool:
        """Retire an edge by TOMBSTONE (atlas: append over rewrite, tombstone don't erase). Sets
        superseded_on; never DELETEs. A tombstoned edge stops being walked but stays legible."""
        with self._lock:
            cur = self.conn.execute(
                "UPDATE atom_links SET superseded_on=? WHERE from_id=? AND to_id=? AND relation=? AND superseded_on=0",
                (int(time.time()), from_id, to_id, relation))
            self.conn.commit()
        return cur.rowcount > 0

    def relink(self, from_id: str, old_to: str, new_to: str, relation: str) -> bool:
        """RE-POINT an edge from old_to to new_to (same relation): tombstone the old edge, mint
        the new one. The heal action for an edge into a SUPERSEDED atom — the connection is kept
        but now points at the successor that holds the truth, instead of being severed. Atomic
        under the lock; a no-op returns False (nothing live to re-point)."""
        with self._lock:
            cur = self.conn.execute(
                "UPDATE atom_links SET superseded_on=? WHERE from_id=? AND to_id=? AND relation=? AND superseded_on=0",
                (int(time.time()), from_id, old_to, relation))
            self.conn.commit()
        if cur.rowcount == 0:
            return False
        self.link(from_id, new_to, relation)
        return True

    def live_subsume_pairs(self) -> list[tuple[str, str]]:
        """Every LIVE `subsumes` edge as (canonical_id, duplicate_id) — the slice-1.5 hygiene ledger.
        store.seeds() consults this to drop a subsumed verbatim copy when its canonical is present in
        the same result set (the wk-group precedent's read side at atom grain). Best-effort callers
        treat [] as 'no subsumption in force'."""
        with self._lock:
            rows = self.conn.execute(
                "SELECT from_id, to_id FROM atom_links WHERE relation='subsumes' AND superseded_on=0"
            ).fetchall()
        return [(r["from_id"], r["to_id"]) for r in rows]

    def edges_of(self, atom_id: str, live_only: bool = True) -> list:
        """All live (non-tombstoned) edges touching atom_id, either direction. Each: dict(from_id,to_id,
        relation,dir) where dir='out' (atom_id is from) or 'in' (atom_id is to)."""
        out = []
        q = ("SELECT from_id,to_id,relation FROM atom_links WHERE (from_id=? OR to_id=?)"
             + (" AND superseded_on=0" if live_only else ""))
        with self._lock:
            for r in self.conn.execute(q, (atom_id, atom_id)).fetchall():
                out.append({"from_id": r["from_id"], "to_id": r["to_id"], "relation": r["relation"],
                            "dir": "out" if r["from_id"] == atom_id else "in"})
        return out

    def atom_id_for_coordinate(self, coordinate: str) -> str | None:
        """Resolve a coordinate/slug to its atom id (most recent wins). A [[link]] names a coordinate;
        the edge stores ids — this bridges them. Matches exact coordinate, else the last path segment."""
        coord = _norm_coord(coordinate)
        with self._lock:
            r = self.conn.execute(
                "SELECT id FROM atoms WHERE coordinate=? ORDER BY ts DESC LIMIT 1", (coord,)).fetchone()
            if r:
                return r["id"]
            # fall back: a slug that matches the LAST segment of some coordinate
            r = self.conn.execute(
                "SELECT id FROM atoms WHERE coordinate LIKE ? ORDER BY ts DESC LIMIT 1",
                ("%:" + coord,)).fetchone()
        return r["id"] if r else None

    # --- THE IMMUNE SYSTEM: scan the graph for BROKEN MEMORIES (owner 2026-06-16) -----------------
    # Port of the atlas/chainboard SCANNER into core, framed as the brain's ANTIBODIES: a memory graph,
    # like a mind, accrues broken memories — dangling references, edges into known-false claims, half-
    # finished corrections. Left alone they corrupt the mentality. scan() is the immune response: it
    # finds them by MECHANISM (not a model's say-so) and reports FAIL (provably broken — quarantine) vs
    # WARN (drift it can't prove). It composes with the existing CONTENT antibody (dispute/disclaim =
    # "this memory is false"); scan() is the STRUCTURE antibody ("this reference is broken"). Together
    # they keep the graph sane. The atlas law: each bug CLASS becomes a new check → the graph gets
    # HARDER to break over time. scan() READS ONLY — it reports; healing is a separate deliberate act
    # (dispute/unlink), never an auto-edit of the soul. See atlas-pattern, chainboard-atom-framework,
    # dispute-organ-and-warmth-laundering.
    def scan(self, scope: str | None = None) -> dict:
        """Immune scan over the atom+edge graph. Returns {fail:[...], warn:[...], stats:{...}}. Each
        finding: {rule, severity, detail, from_id?/to_id?/atom_id?}. READ-ONLY (never mutates)."""
        fail, warn = [], []
        with self._lock:
            # atom id set (scope-filtered if asked) + the judged/disclaimed + superseded markers
            if scope:
                atom_rows = self.conn.execute(
                    "SELECT id, born_from FROM atoms WHERE scope=?", (scope,)).fetchall()
            else:
                atom_rows = self.conn.execute("SELECT id, born_from FROM atoms").fetchall()
            ids = {r["id"] for r in atom_rows}
            # CURRENTLY disclaimed = bears the judged mark AND has NOT since been redeemed. A redeem
            # preserves the `<- judged:disclaimed` LINEAGE in born_from (so the dream never re-disclaims
            # a redeemed row), so the mark substring SURVIVES a legitimate redemption — detecting on the
            # bare substring would keep flagging a row the trace already cleared (the redeem-invisible-to-
            # scan bug, caught 2026-06-18). A `redeemed:` prefix means the lie was discharged by trace.
            def _is_live_lie(bf: str) -> bool:
                return self._JUDGED_MARK in (bf or "") and not (bf or "").startswith(self._REDEEMED_MARK)
            disclaimed = {r["id"] for r in atom_rows if _is_live_lie(r["born_from"])}
            # ALL atom ids (unscoped) — an edge may legitimately cross scopes, so dangling is judged
            # against the WHOLE atom set, not just the scoped slice.
            all_ids = {r[0] for r in self.conn.execute("SELECT id FROM atoms")}
            all_disclaimed = {r[0] for r in self.conn.execute(
                "SELECT id,born_from FROM atoms WHERE born_from LIKE ?", (f"%{self._JUDGED_MARK}%",))
                if _is_live_lie(r[1])}
            edges = self.conn.execute(
                "SELECT from_id,to_id,relation FROM atom_links WHERE superseded_on=0").fetchall()
            # A disclaimed atom that names a SUCCESSOR (a live `supersedes` OUT-edge — the
            # ANTIBODY-4 contract a `dispute --superseded-by` writes) has not vanished: its
            # truth MOVED to the successor. An inbound edge into such an atom is not routing
            # to a lie — it is routing to a CORRECTED memory, and the honest repair is to
            # RE-POINT the edge at the successor (heal), not to tombstone it or leave an
            # uncleaarable FAIL. Map each superseded atom to its live successor so ANTIBODY 2
            # can split the two cases (OPEN-0025/0033; the delta-shop-margin-ceiling disagreement).
            successor_of = {e["from_id"]: e["to_id"] for e in self.conn.execute(
                "SELECT from_id,to_id FROM atom_links WHERE relation='supersedes' AND superseded_on=0")
                if e["from_id"] in all_disclaimed and e["to_id"] in all_ids
                and e["to_id"] not in all_disclaimed}

            for e in edges:
                f, t, rel = e["from_id"], e["to_id"], e["relation"]
                # ANTIBODY 1 — DANGLING EDGE (FAIL): an edge into a memory that doesn't exist.
                if f not in all_ids:
                    fail.append({"rule": "dangling_edge_from", "severity": "FAIL",
                                 "detail": f"edge {rel} has from_id with no atom",
                                 "from_id": f, "to_id": t, "relation": rel})
                if t not in all_ids:
                    fail.append({"rule": "dangling_edge_to", "severity": "FAIL",
                                 "detail": f"edge {rel} points at a non-existent atom",
                                 "from_id": f, "to_id": t, "relation": rel})
                # ANTIBODY 2 — EDGE INTO A DISCLAIMED MEMORY (FAIL, two flavours):
                #   (a) the disclaimed atom names a SUCCESSOR → the truth moved; the edge should
                #       be RE-POINTED to the successor, not tombstoned. `edge_to_superseded`
                #       carries successor_id so heal can re-point it. (Clears the uncleaarable
                #       FAIL that grew every time a new atom linked to a superseded slug.)
                #   (b) no successor → a genuine route to a known lie with nowhere to go (the
                #       disputed-hub-with-no-successor case). `edge_to_disclaimed`, healed by
                #       tombstone. The `supersedes` edge itself is the correction — always exempt.
                elif t in all_disclaimed and rel != "supersedes":
                    if t in successor_of:
                        fail.append({"rule": "edge_to_superseded", "severity": "FAIL",
                                     "detail": f"{rel} edge points at a SUPERSEDED atom — re-point to the "
                                               f"successor (its truth moved there)",
                                     "from_id": f, "to_id": t, "relation": rel,
                                     "successor_id": successor_of[t]})
                    else:
                        fail.append({"rule": "edge_to_disclaimed", "severity": "FAIL",
                                     "detail": f"{rel} edge points at a DISCLAIMED atom (route to a known lie)",
                                     "from_id": f, "to_id": t, "relation": rel})
                # ANTIBODY 3 — RELATION OUT OF VOCABULARY (FAIL): a structurally-foreign edge type.
                if rel not in RELATION_TYPES:
                    fail.append({"rule": "unknown_relation", "severity": "FAIL",
                                 "detail": f"relation {rel!r} not in closed vocabulary", "from_id": f, "to_id": t})

            # ANTIBODY 4 — ORPHAN CORRECTION (WARN): a disclaimed atom with no supersedes edge OUT to a
            # replacement — a correction that flagged a lie but never pointed at the truth that replaces it.
            superseders = {e["from_id"] for e in self.conn.execute(
                "SELECT from_id FROM atom_links WHERE relation='supersedes' AND superseded_on=0")}
            # An explicitly ACKNOWLEDGED orphan (disclaim --no-successor stamped the marker into
            # born_from) is settled, not suspect — a fact can retire without a replacement. A
            # permanent warn on an acknowledged case is alert noise, not immunity.
            acked = {row["id"] for row in self.conn.execute(
                "SELECT id FROM atoms WHERE born_from LIKE '%[no-successor]%'")}
            for aid in (disclaimed if scope else all_disclaimed):
                if aid not in superseders and aid not in acked:
                    warn.append({"rule": "orphan_disclaim", "severity": "WARN",
                                 "detail": "disclaimed atom has no supersedes edge to its replacement",
                                 "atom_id": aid})

            # ANTIBODY 5 — LIVE CONTRADICTION (WARN, not fail): two atoms joined by `contradicts`, both
            # still live (neither disclaimed). The honest move is to SURFACE the tension, not hide it.
            for e in self.conn.execute(
                    "SELECT from_id,to_id FROM atom_links WHERE relation='contradicts' AND superseded_on=0"):
                if e["from_id"] not in all_disclaimed and e["to_id"] not in all_disclaimed:
                    warn.append({"rule": "live_contradiction", "severity": "WARN",
                                 "detail": "two live atoms contradict — surface + resolve (dispute one)",
                                 "from_id": e["from_id"], "to_id": e["to_id"]})

            # ANTIBODY 6 — UNSTAMPED CLASSIFIER (WARN): a card whose born_from narrative CLASSIFIES
            # to a real CARD_KIND code but whose stored `kind` is 0 (unknown). This is the exact
            # bug that broke relive for 10 days — wrap wrote a session card but the kind column
            # never got the code, so the session-band filter couldn't see it. The antibody makes
            # that class of drift VISIBLE instead of silent (mirrors ANTIBODY 3's discipline: each
            # bug class becomes a check → the graph gets harder to break). Read-only; the heal is a
            # deliberate re-run of the backfill. See registry.classify, backfill_card_kind.
            from ..registry import classify as _classify_kind
            for cr in self.conn.execute("SELECT id, born_from, kind FROM cards"):
                stored = cr["kind"] if "kind" in cr.keys() else 0
                if (stored or 0) == 0:
                    want = _classify_kind(cr["born_from"])
                    if want != 0:
                        warn.append({"rule": "unstamped_card_kind", "severity": "WARN",
                                     "detail": f"card born_from classifies to {want} but kind=0 "
                                     f"(run backfill_card_kind) — the relive-rot class",
                                     "card_id": cr["id"]})

        return {"fail": fail, "warn": warn,
                "stats": {"atoms": len(ids), "fail": len(fail), "warn": len(warn),
                          "scope": scope or "ALL"}}

    def add_card(self, label: str, refs: list, born_from: str = "", prev: str = "") -> str:
        """Create (or no-op if it exists) an action-card. `prev` = the upstream card id this one
        chained from (the card→card edge). Because a card's id is content-addressed on label+refs
        (NOT prev), the same recurring action is the same card; if it already exists, its edge is
        SET (not overwritten with empty) so a recurrence inside a chain still records the wiring —
        append-only spirit: we add the edge, we never erase one."""
        c = Card(label=label, refs=refs, born_from=born_from, prev=prev)
        # Stamp the governed CARD_KIND code from the born_from narrative at WRITE time (the machine
        # classifier lives in its own column; born_from stays free narrative). registry.classify is
        # the single source of the code. A born_from with no marker -> 0 (unknown), honestly.
        from ..registry import classify as _classify_kind
        kind = _classify_kind(born_from)
        with self._lock:
            self.conn.execute(
                "INSERT OR IGNORE INTO cards (id,label,refs,score,score_history,use_count,born_from,prev,ts,kind)"
                " VALUES (?,?,?,?,?,?,?,?,?,?)",
                (c.id, c.label, json.dumps(c.refs), c.score, c.score_history, c.use_count,
                 c.born_from, c.prev, c.ts, kind))
            # Record the edge if this card has no edge yet and one is offered (never clobber an
            # existing edge; never write an empty one over a real one).
            if prev:
                self.conn.execute(
                    "UPDATE cards SET prev=? WHERE id=? AND (prev='' OR prev IS NULL)", (prev, c.id))
            self.conn.commit()
        return c.id

    # --- read ---
    def _atom_row(self, r) -> Atom:
        a = Atom(coordinate=r["coordinate"], content=r["content"], born_from=r["born_from"]); a.id = r["id"]
        a.score = r["score"]; a.score_history = r["score_history"]; a.use_count = r["use_count"]; a.ts = r["ts"]
        # Rich fields (2026-06-16). keys() guards a row read via a SELECT that predates the migration;
        # post-migration every row carries them. Defaults match the dataclass.
        keys = r.keys()
        a.scope = r["scope"] if "scope" in keys else ""
        a.kind = r["kind"] if "kind" in keys else ""
        a.valence = r["valence"] if "valence" in keys else 0.0
        a.arousal = r["arousal"] if "arousal" in keys else 0.0
        return a

    def atoms_at(self, prefix: str) -> list[Atom]:
        """Atoms at/under a coordinate prefix, SCORE-ORDERED (highest earned first = §7 fallback order)."""
        pfx = _norm_coord(prefix)
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM atoms WHERE coordinate=? OR coordinate LIKE ? ORDER BY score DESC",
                (pfx, pfx + ":%")).fetchall()
        return [self._atom_row(r) for r in rows
                if r["coordinate"] == pfx or r["coordinate"].startswith(pfx + ":")]

    def v2_reflex_match(self, reasoning: str, scope: str, *, floor: float = 0.30) -> dict | None:
        """PURE-v2 reasoning-keyed reflex lookup (2026-06-16) — for a standalone cartridge that is v2
        ONLY (no v1 cold-borrow, no SeedStore, NO downloaded/foreign embed model).

        THE CARTRIDGE IS ITS OWN EMBEDDER (owner: "use the reflex cartridge as the embed, replacing the
        model"). We fit the estate's soul-shaped OwnEmbedder (bank_embed_own — pure stdlib, $0, no
        download) on the cartridge's OWN earned reflex atoms: meaning comes from co-occurrence in the
        cartridge's own material, not borrowed weights. So 'piece above the king, transfer' matches the
        reflex earned on 'rook over the king, transfer' once the corpus has placed those terms — the
        synonym generalization lexical word-overlap can't do (Jaccard was 0.125). The embedder GROWS
        with the cartridge (sparse co-occurrence is weak at <few atoms — it degrades to the self-term
        lexical anchor, which is correct: cold cartridge = think, not a confident wrong reflex).

        TWO TIERS (owner: "the cartridge should fire on THINK too"). A reflex atom matches by MEANING
        regardless of earning; its EARNING decides the TIER:
          - tier='warm'  (effective_score>neutral, EARNED)   -> fires as REFLEX (System 1: proven, do it)
          - tier='cold'  (effective_score<=neutral, SEEDED)  -> surfaces to THINK (System 2: an UNPROVEN
            candidate to CONSIDER and try — a win then EARNS it into a reflex). A cold seed is NOT inert:
            think reasons over it. (If it were invisible, it could never earn — the bootstrap dead-end.)
        So a freshly-seeded cartridge (e.g. the opus level) helps THINK on day one, and becomes REFLEX
        as trace warms its seeds. Returns {atom_id, coordinate, content, match, eff, tier, via} or None
        (no atom matches the reasoning's meaning at all = true new ground)."""
        from echelon_sdk.minilm_embed import make_embedder
        if not (reasoning or "").strip():
            return None
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM atoms WHERE scope=? AND content LIKE '%[reflex]%'", (scope,)).fetchall()
        atoms = [(self._atom_row(r),) for r in rows]
        atoms = [(a, self.effective_score(a)[0]) for (a,) in atoms]
        if not atoms:
            return None
        # THE HARVEST (2026-06-20): prefer the REAL all-MiniLM-L6-v2, degrade to OwnEmbedder. For MiniLM
        # fit() is a no-op (it KNOWS synonymy already); for OwnEmbedder it still fits the reflex space.
        # Same .embed/.similarity contract -> the match logic below is unchanged. See minilm-embedder-is-the-one-jcode-harvest.
        emb = make_embedder("auto").fit([a for a, _ in atoms])
        qv = emb.embed(reasoning)
        if not qv:
            return None
        best, best_s = None, 0.0
        for a, eff in atoms:
            s = emb.similarity(qv, emb.embed(a.content))
            if s > best_s:
                best_s, best = s, (a, eff)
        if best is None or best_s < floor:
            return None                        # no atom matches the meaning -> true new ground
        a, eff = best
        return {"atom_id": a.id, "coordinate": a.coordinate, "content": a.content,
                "match": round(best_s, 3), "eff": round(eff, 1),
                "tier": "warm" if eff > SCORE_BENCHMARK else "cold", "via": "own-embed"}

    def wire_edges_from_links(self, scope: str | None = None, *, dry_run: bool = False) -> dict:
        """Backfill the v2 soul graph from atoms' [[links]] (2026-06-16). An estate atomized BEFORE the
        edge layer existed (e.g. Epsilon-Co) has the atoms but ZERO v2 atom_links edges — the [[link]]
        references sit inert in atom CONTENT. This resolves every [[name]] in a scope's atoms to its
        target atom id and wires a typed `refs` edge (idempotent — link() is INSERT OR IGNORE). After
        this, think()/recall compose tightly over the scope (proven on WK: 0 -> 237 internal edges raised
        think confidence 0.465 -> 0.625). Append-only; never removes an edge. Returns counts."""
        _REF = re.compile(r"\[\[([a-z0-9\-:]+)\]\]")
        wired = unresolved = 0
        with self._lock:
            if scope:
                rows = self.conn.execute("SELECT id, content FROM atoms WHERE scope=?", (scope,)).fetchall()
            else:
                rows = self.conn.execute("SELECT id, content FROM atoms").fetchall()
        for r in rows:
            for name in _REF.findall(r["content"] or ""):
                tgt = self.atom_id_for_coordinate(name)
                if tgt and tgt != r["id"]:
                    if dry_run:
                        wired += 1
                    elif self.link(r["id"], tgt, "refs"):
                        wired += 1
                else:
                    unresolved += 1
        return {"wired": wired, "unresolved": unresolved, "scope": scope or "ALL", "dry_run": dry_run}

    def atoms_in_scope(self, scope: str | None) -> list[Atom]:
        """All v2 atoms in a scope (or every atom if scope is None). The v2-side read pool for the
        v1∪v2 union in SeedStore.seeds — lets a v2-born atom (post v1-freeze) be recalled. Excludes the
        thin coordinate-only ported shadows? No — returns all; the union dedups by content upstream."""
        with self._lock:
            if scope:
                rows = self.conn.execute("SELECT * FROM atoms WHERE scope=?", (scope,)).fetchall()
            else:
                rows = self.conn.execute("SELECT * FROM atoms").fetchall()
        return [self._atom_row(r) for r in rows]

    def count_atoms_in_scope(self, scope: str | None = None) -> int:
        """Fast COUNT — no rows loaded. <1ms for any scope size."""
        with self._lock:
            if scope:
                r = self.conn.execute(
                    "SELECT COUNT(*) FROM atoms WHERE scope=?", (scope,)).fetchone()
            else:
                r = self.conn.execute("SELECT COUNT(*) FROM atoms").fetchone()
        return r[0] if r else 0

    def atom_ids_in_scope(self, scope: str) -> list[dict]:
        """Lightweight — returns {id, coordinate, scope, kind, born_from} dicts.
        No content blob loaded. For callers that only need identity/metadata."""
        with self._lock:
            rows = self.conn.execute(
                "SELECT id, coordinate, scope, kind, born_from, score, ts "
                "FROM atoms WHERE scope=? ORDER BY ts DESC",
                (scope,)).fetchall()
        return [dict(r) for r in rows]

    def get_atom(self, atom_id: str) -> Atom | None:
        with self._lock:
            r = self.conn.execute("SELECT * FROM atoms WHERE id=?", (atom_id,)).fetchone()
        return self._atom_row(r) if r else None

    # ── THE WITNESSED DOOR (remember-is-the-witnessed-door-earn-law) ─────────────────────────────
    # recall is a dead link no more (recall-is-a-dead-link-the-bank-never-witnesses): the engine is
    # the ONLY access path to a memory's structured fields, so every access is witnessed by
    # construction and CAN earn. recall_peek() is FREE (spine only, no mutation); remember_fetch() is
    # the witnessed door (earns by default, mechanical); fire_lower() is the only downward hand-path.
    # The earn/decay NUMBER comes from the earn_law leaf — one source, no drift.

    def compile_atom_struct(self, atom_id: str, content: str | None = None) -> bool:
        """Compile (or recompile) one atom's structured projection FROM its content blob. Idempotent:
        spine/body/sidecar are REPLACE (rebuildable index), earned is born-once at 100 (a recompile
        never resets earned weight). Delegates the parse to the struct_split leaf. Returns True if the
        atom existed. The atoms truth row + edges are untouched."""
        with self._lock:
            if content is None:
                r = self.conn.execute(
                    "SELECT content, scope FROM atoms WHERE id=?", (atom_id,)).fetchone()
                if not r:
                    return False
                content = r["content"]
                scope = r["scope"]
            else:
                scope = ""
            p = split_content(content)
            now = int(time.time())
            slug = p["slug"] or atom_id[:12]
            claim = p["claim"] or (content or "").strip().split("\n", 1)[0][:200]
            # PRESERVE WITNESS across recompile (B1): the spine is REPLACE (rebuildable index), but
            # `witness` is authored provenance parsed from the .md frontmatter at ingest — NOT
            # derivable from the content blob — so a recompile must not blank it. Carry the existing
            # value forward. (ingest sets it via set_witness after this compile.)
            prev_w = self.conn.execute(
                "SELECT witness FROM atom_spine WHERE atom_id=?", (atom_id,)).fetchone()
            witness = (prev_w["witness"] if prev_w and "witness" in prev_w.keys() else "") or ""
            self.conn.execute(
                "INSERT OR REPLACE INTO atom_spine (atom_id, slug, claim, directive, scope, witness, compiled_ts) "
                "VALUES (?,?,?,?,?,?,?)", (atom_id, slug, claim, p["directive"], scope, witness, now))
            self.conn.execute("INSERT OR REPLACE INTO atom_body (atom_id, why, evidence) VALUES (?,?,?)",
                              (atom_id, p["why"], p["evidence"]))
            self.conn.execute("INSERT OR IGNORE INTO atom_sidecar (atom_id, embedding, trigger_signals) "
                              "VALUES (?,?,?)", (atom_id, "", "[]"))
            self.conn.execute(
                "INSERT OR IGNORE INTO atom_earned (atom_id, scope, score, use_count, score_history) "
                "VALUES (?,?,?,?,?)", (atom_id, scope, 100.0, 0, "[]"))
            self.conn.commit()
        return True

    def search_spine_candidates(self, query: str, scope: str = "",
                                 limit: int = 100) -> list[dict]:
        """FTS pre-filter for warmth(). Returns {atom_id, slug, claim, scope, score}
        for the top FTS matches in a scope. The caller then does n-gram scoring
        over this reduced set instead of all seeds. Uses idx_earned_scope_score
        to rank by earned weight within the FTS matches."""
        # Sanitize query for FTS5 — escape special chars, use prefix* for partial match
        safe = _fts_sanitize(query)
        if not safe:
            return []
        with self._lock:
            rows = self.conn.execute(
                "SELECT s.atom_id, s.slug, s.claim, s.scope, "
                "COALESCE(e.score, 100.0) as earned_score "
                "FROM atom_spine_fts f "
                "JOIN atom_spine s ON s.rowid = f.rowid "
                "LEFT JOIN atom_earned e ON e.atom_id = s.atom_id "
                "WHERE atom_spine_fts MATCH ? AND s.scope = ? "
                "ORDER BY e.score DESC "
                "LIMIT ?",
                (safe, scope, limit)).fetchall()
        return [dict(r) for r in rows]

    def search_spine(self, query: str, scope: str = "", limit: int = 50) -> list[dict]:
        """Fast FTS5 search on atom_spine. Returns matching {atom_id, slug, claim, scope}.
        Uses the FTS index (not a full scan). Filters by scope if given.
        Joins on the implicit integer rowid (not atom_id — that's TEXT PK)."""
        with self._lock:
            if scope:
                rows = self.conn.execute(
                    "SELECT s.atom_id, s.slug, s.claim, s.scope "
                    "FROM atom_spine_fts f "
                    "JOIN atom_spine s ON s.rowid = f.rowid "
                    "WHERE atom_spine_fts MATCH ? AND s.scope = ? "
                    "LIMIT ?",
                    (query, scope, limit)).fetchall()
            else:
                rows = self.conn.execute(
                    "SELECT s.atom_id, s.slug, s.claim, s.scope "
                    "FROM atom_spine_fts f "
                    "JOIN atom_spine s ON s.rowid = f.rowid "
                    "WHERE atom_spine_fts MATCH ? "
                    "LIMIT ?",
                    (query, limit)).fetchall()
        return [dict(r) for r in rows]

    def set_witness(self, atom_id: str, witness: str) -> bool:
        """B1 — record HOW an atom's lesson was witnessed on its spine row (execution|owner|inference).
        DISPLAY-ONLY provenance: NOTHING ranks or earns on this yet (a later, separately-gated change).
        Empty/unset is never a demotion (legacy atoms carry ''). An unknown/absent value returns early
        (never demotes a set value to ''), so a same-id re-ingest whose .md dropped the field leaves the
        prior owner-set value intact. NOTE: this guarantee is per-atom_id only — a re-ingest that CHANGES
        the body mints a NEW atom row (most-recent-wins), which legitimately starts blank until its own
        .md declares a witness. Returns True if a compiled spine existed to stamp.
        See ruflo-lesson-provenance-tiers-and-honest-edges."""
        w = (witness or "").strip().lower()
        if w not in ("execution", "owner", "inference"):
            return False  # unknown/absent -> leave whatever is there (never demote to '')
        with self._lock:
            cur = self.conn.execute(
                "UPDATE atom_spine SET witness=? WHERE atom_id=?", (w, atom_id))
            self.conn.commit()
        return cur.rowcount > 0

    def spine_witness(self, atom_id: str) -> str:
        """Read an atom's witness provenance tag (''=legacy/unset). Cheap; recall display uses it."""
        with self._lock:
            r = self.conn.execute(
                "SELECT witness FROM atom_spine WHERE atom_id=?", (atom_id,)).fetchone()
        return (r["witness"] if r and "witness" in r.keys() else "") or ""

    def recall_peek(self, atom_id: str) -> dict | None:
        """FREE peek — the spine only (slug/claim/directive). NO body, NO mutation. 'recall is free;
        fetch is what earns.' Returns None if the atom has no compiled spine yet."""
        with self._lock:
            r = self.conn.execute(
                "SELECT slug, claim, directive FROM atom_spine WHERE atom_id=?", (atom_id,)).fetchone()
        return dict(r) if r else None

    def remember_fetch(self, atom_id: str, depth: str = "spine") -> dict | None:
        """THE WITNESSED DOOR. Serve the structured fields at `depth` and EARN BY DEFAULT (mechanical,
        no outcome gate, no self-report — the fetch IS the witness). depth='spine' -> slug/claim/
        directive; depth='body' -> + why/evidence. Body is NEVER served without depth='body'. Returns
        the served dict (and earns), or None if the atom has no spine."""
        if depth not in ("spine", "body"):
            raise ValueError("depth must be 'spine' or 'body'")
        with self._lock:
            sp = self.conn.execute(
                "SELECT slug, claim, directive FROM atom_spine WHERE atom_id=?", (atom_id,)).fetchone()
            if not sp:
                return None
            served = {"id": atom_id, "slug": sp["slug"], "claim": sp["claim"], "directive": sp["directive"]}
            if depth == "body":
                bd = self.conn.execute(
                    "SELECT why, evidence FROM atom_body WHERE atom_id=?", (atom_id,)).fetchone()
                if bd:
                    served["why"], served["evidence"] = bd["why"], bd["evidence"]
            self._witness_earn(atom_id)   # the fetch through the door IS the earn
            # Mark impressions as opened — this atom was taken up, not passed over
            try:
                ar = self.conn.execute(
                    "SELECT coordinate, scope FROM atoms WHERE id=?", (atom_id,)).fetchone()
                if ar and ar["coordinate"] and ar["scope"]:
                    self.mark_impressions_opened(ar["scope"], ar["coordinate"])
            except Exception:
                pass  # best-effort; impression marking must never break a fetch
        return served

    def fire_lower(self, atom_id: str, reason: str) -> dict:
        """The ONLY downward hand-path — the reader's DUTY when a fetched memory MISLED them. Decays
        (earn_law), NEVER deletes. There is deliberately no fire_higher (weight rises only by witnessed
        use). Returns {ok, before, after}."""
        with self._lock:
            r = self.conn.execute("SELECT score, score_history FROM atom_earned WHERE atom_id=?",
                                  (atom_id,)).fetchone()
            if not r:
                return {"ok": False, "reason": "no earned row (compile the atom first)"}
            before = r["score"]
            after = earn_law.decay(before)
            hist = json.loads(r["score_history"] or "[]")
            hist.append([int(time.time()), round(after - before, 3), f"fire_lower:{reason}"])
            self.conn.execute("UPDATE atom_earned SET score=?, score_history=? WHERE atom_id=?",
                              (after, json.dumps(hist), atom_id))
            self.conn.commit()
        return {"ok": True, "before": round(before, 2), "after": round(after, 2)}

    def _witness_earn(self, atom_id: str) -> None:
        """Mechanical earn on a witnessed fetch: score += earn_law.EARN_DELTA, use_count += 1, log it.
        The ONE upward writer (with fire_lower the ONE downward) — the antibody to a stray external
        re-scorer writing weight from outside the door (warmth-update-was-a-no-op)."""
        r = self.conn.execute("SELECT score, use_count, score_history FROM atom_earned WHERE atom_id=?",
                              (atom_id,)).fetchone()
        if not r:
            return
        hist = json.loads(r["score_history"] or "[]")
        hist.append([int(time.time()), earn_law.EARN_DELTA, "fetch"])
        self.conn.execute(
            "UPDATE atom_earned SET score=?, use_count=?, score_history=?, last_fetch_ts=? WHERE atom_id=?",
            (earn_law.earn(r["score"]), r["use_count"] + 1, json.dumps(hist), int(time.time()), atom_id))
        self.conn.commit()

    def struct_earned(self, atom_id: str) -> dict | None:
        """Read the structured-earned row (score, use_count) — for ranking + verification."""
        with self._lock:
            r = self.conn.execute(
                "SELECT score, use_count FROM atom_earned WHERE atom_id=?", (atom_id,)).fetchone()
        return dict(r) if r else None

    def earn_day_spread(self, atom_id: str) -> int:
        """How many DISTINCT calendar days the witnessed-door earns for this atom span (from the
        per-event ts in atom_earned.score_history — each 'fetch' event carries its own int-ts). This is
        the honest 'proven across TIME' signal the re-based promotion gate uses in place of independent
        witness-convergence: on a single-persona estate true independence is impossible (the same reason
        grandvote cannot ratify), so cross-session recurrence is the strongest available evidence that a
        use is not one enthusiastic session laundering itself. Returns 0 if no earned row / no ts / error.
        (fire_lower events also carry ts but are not door-uses; only 'fetch' events count as a witnessed
        USE — kindle inversion: reads never earn, so only the door's earn is a use.)

        Returns: the DISTINCT-day count (>=0) when fetch events carry a usable per-event ts; -1 when there
        ARE fetch events but NONE carry a ts (the 'no per-event ts' case the ruling flags — the caller then
        accepts plain use_count>=N but says so); 0 when there are no fetch events at all / no row / error."""
        try:
            with self._lock:
                r = self.conn.execute(
                    "SELECT score_history FROM atom_earned WHERE atom_id=?", (atom_id,)).fetchone()
            if not r:
                return 0
            hist = json.loads(r["score_history"] or "[]")
            days = set()
            saw_fetch = False
            saw_ts = False
            for h in hist:
                if len(h) > 2 and str(h[2]).startswith("fetch"):
                    saw_fetch = True
                    if isinstance(h[0], (int, float)) and h[0]:
                        saw_ts = True
                        days.add(time.strftime("%Y-%m-%d", time.localtime(int(h[0]))))
            if saw_fetch and not saw_ts:
                return -1        # fetch events exist but carry no ts — spread unverifiable
            return len(days)
        except Exception:
            return 0

    # --- v2-native scoring (v1 cold-borrow REMOVED 2026-06-26) ---
    # v2 atoms earn their own weight by trace. The cold-borrow from v1 was the
    # v2 cold-start bridge (2026-06-09); v2 is now weight-richer on its own.

    # --- read-time decay (2026-07-31): effective_score recomputes from score_history
    # at serve time so decay is continuously priced in, not only on mutation. The
    # stored atom.score is the fallback when history is missing/unparseable. Born-
    # neutral (empty history) still returns 100.0 unchanged. Per-type lambda controls
    # how fast each atom type decays toward neutral. ---

    def _recompute_score(self, score_history: str, fallback: float,
                         kind: str | None = None, now: int | None = None) -> float:
        """Recompute score from JSON score_history at `now`. Returns `fallback` on error."""
        if now is None:
            now = int(time.time())
        try:
            hist = json.loads(score_history or "[]")
            if not isinstance(hist, list):
                return fallback
            if not hist:
                return SCORE_BENCHMARK
            lam = lam_for(kind)
            return compute_score(hist, now=now, lam=lam)
        except Exception:
            return fallback

    def _effective_from_row(self, *, score: float, score_history: str,
                            kind: str | None, born_from: str,
                            use_count: int, earned_use_count: int,
                            earned_history_str: str | None, now: int,
                            door_require_earned_use: bool = True) -> float:
        """ONE shared effective-score law — the row-path core (2026-07-31 fold-in).

        Judges the effective score of an atom from its row fields.  Callers handle
        exempt-type gating and threshold comparisons OUTSIDE this helper.

        ``door_require_earned_use=True`` (effective_score path): door-borrow ONLY
        when *earned_use_count > 0* (the earned row's own use_count).

        ``door_require_earned_use=False`` (dormant path): door-borrow when the
        *sum* of atom.use_count + earned_use_count > 0 AND earned_history_str is
        non-empty.  This is a wider gate — an atom whose OWN use_count is non-zero
        but whose earned row has use_count=0 can still borrow if the earned row
        carries score_history entries (see dormant-foldin-report.md §Disagreement).

        O(N) per atom at current scale — no caching, no indexes.  Acceptable.
        """
        # Judged-mark atoms: return stored score, bypass recompute
        if self._JUDGED_MARK in (born_from or ""):
            return score

        # Recompute own score from score_history
        own = self._recompute_score(score_history, score, kind, now=now)

        # Door-borrow from atom_earned
        if door_require_earned_use:
            # effective_score path: only borrow when earned_use_count > 0
            if (earned_use_count or 0) > 0 and earned_history_str:
                try:
                    eh = json.loads(earned_history_str)
                    if isinstance(eh, list) and eh:
                        door_score = compute_score(eh, now=now, lam=lam_for(kind))
                        own = max(own, door_score)
                except Exception:
                    pass
        else:
            # Dormant path: borrow when any use_count > 0 AND earned_history exists
            if ((use_count or 0) + (earned_use_count or 0)) > 0 and earned_history_str:
                try:
                    eh = json.loads(earned_history_str)
                    if isinstance(eh, list) and eh:
                        door_score = compute_score(eh, now=now, lam=lam_for(kind))
                        own = max(own, door_score)
                except Exception:
                    pass

        return own

    def effective_score(self, atom: Atom) -> tuple[float, bool]:
        """Score to RANK by: recomputed at read time from score_history via
        compute_score (per-type lambda). Falls back to stored atom.score only if
        history is missing/unparseable. Returns (score, borrowed=False always).
        Routes through _effective_from_row — the ONE shared scoring law."""
        now = int(time.time())
        atom_kind = getattr(atom, "kind", None)
        # Gather inputs the shared helper needs
        earned = self.struct_earned(getattr(atom, "id", "")) if getattr(atom, "id", "") else None
        earned_use = earned.get("use_count", 0) if earned else 0
        # Fetch earned_history_str — only needed when earned_use > 0 (door_require_earned_use=True)
        eh_str: str | None = None
        if earned_use > 0:
            try:
                eh = self._get_earned_history(atom.id)
                if eh:
                    eh_str = json.dumps(eh)  # round-trip: helper expects raw string
            except Exception:
                pass
        eff = self._effective_from_row(
            score=atom.score,
            score_history=atom.score_history,
            kind=atom_kind,
            born_from=atom.born_from or "",
            use_count=atom.use_count or 0,
            earned_use_count=earned_use,
            earned_history_str=eh_str,
            now=now,
            door_require_earned_use=True,
        )
        return eff, False

    def _get_earned_history(self, atom_id: str) -> list | None:
        """Return parsed atom_earned score_history, or None if missing."""
        try:
            with self._lock:
                r = self.conn.execute(
                    "SELECT score_history FROM atom_earned WHERE atom_id=?", (atom_id,)).fetchone()
            if r:
                hist = json.loads(r["score_history"] or "[]")
                return hist if isinstance(hist, list) and hist else None
        except Exception:
            pass
        return None

    def effective_scores_by_content(self, scope: str | None = None) -> dict:
        """content-prefix -> effective_score for v2 atoms. warmth() uses this map to tilt
        lexical ranking by earned v2 weight. Uses lightweight queries — only loads
        columns needed, single batch join for earned scores. Best-effort: {} on any error."""
        out: dict = {}
        try:
            with self._lock:
                if scope:
                    rows = self.conn.execute(
                        "SELECT a.id, a.content, a.score, a.use_count, a.born_from, "
                        "COALESCE(e.score, NULL) as earned_score "
                        "FROM atoms a LEFT JOIN atom_earned e ON e.atom_id = a.id "
                        "WHERE a.scope=?", (scope,)).fetchall()
                else:
                    rows = self.conn.execute(
                        "SELECT a.id, a.content, a.score, a.use_count, a.born_from, "
                        "COALESCE(e.score, NULL) as earned_score "
                        "FROM atoms a LEFT JOIN atom_earned e ON e.atom_id = a.id").fetchall()
            for r in rows:
                key = (r["content"] or "")[:120]
                if not key:
                    continue
                # Anti-laundering: disclaimed → 50.0
                if r["born_from"] and self._JUDGED_MARK in (r["born_from"] or ""):
                    eff = 50.0
                elif r["use_count"] and r["use_count"] > 0 and r["earned_score"] is not None:
                    eff = r["earned_score"]
                else:
                    eff = float(r["score"] or 100.0)
                if key not in out or eff > out[key]:
                    out[key] = eff
        except Exception:
            return {}
        return out

    def disclaimed_contents(self) -> set:
        """The set of content-prefix keys (content[:120]) for every v2 atom carrying the judged/
        disclaim mark. This is the bridge that lets the READ path (recall.warmth, which ranks v1
        seeds by lexical overlap and never consults bank-score) HONOR a dispute: a v1 seed whose
        content matches a disclaimed v2 atom must be suppressed/read-cold, exactly as the
        confirmation gate already drops unconfirmed insight-claims. Without this, dispute() lowers a
        v2 score that recall never reads, so a stale atom surfaces as warm as the truth — the organ
        is invisible. Content-prefix is the same stable cross-store key effective_score borrows on.
        Returns a set (empty if v2 unreadable — recall then degrades to un-gated, never crashes)."""
        out: set = set()
        try:
            like_m = f"%{self._JUDGED_MARK}%"
            with self._lock:
                for (content,) in self.conn.execute(
                        "SELECT content FROM atoms WHERE born_from LIKE ?", (like_m,)):
                    if content:
                        out.add(content[:120])
        except Exception:
            return set()
        return out

    # port_facts_from_core / backfill_rich_fields / migrate_v1_to_v2_complete REMOVED (2026-06-26).
    # v1 migration tools — the backfill ran once; v2 is weight-richer on its own.

    # (old v1 migration method bodies removed 2026-06-26)
    def card_scope(self, refs: list) -> str:
        """DERIVE a card's scope from the scopes of the atoms it refs — cards carry no scope column
        of their own, but an arc-card's atoms are all from ONE session = ONE scope (verified 40/40
        session cards single-scope, 2026-07-22). Returns the plurality scope over the resolvable refs,
        or '' if none resolve. This is what lets `relive --list --scope X` show only X's sessions
        instead of the shared-bank flat list where echelon/alpha-app/gamma-support/mol clash. Cheap: one indexed
        lookup per ref, cards have ~2-9 refs."""
        from collections import Counter
        scopes: Counter = Counter()
        with self._lock:
            for coord in (refs or []):
                r = self.conn.execute(
                    "SELECT scope FROM atoms WHERE coordinate=? AND scope!='' ORDER BY ts DESC LIMIT 1",
                    (_norm_coord(coord),)).fetchone()
                if r and r["scope"]:
                    scopes[r["scope"]] += 1
        return scopes.most_common(1)[0][0] if scopes else ""

    def card(self, card_id: str) -> Card | None:
        with self._lock:
            r = self.conn.execute("SELECT * FROM cards WHERE id=?", (card_id,)).fetchone()
        if not r:
            return None
        c = Card(label=r["label"], refs=json.loads(r["refs"]), born_from=r["born_from"],
                 prev=(r["prev"] if "prev" in r.keys() else "")); c.id = r["id"]
        c.score = r["score"]; c.score_history = r["score_history"]; c.use_count = r["use_count"]
        return c

    def read_history(self, card_id: str) -> list[dict]:
        """Read a card's score_history with the DELTA DECODED back to its q + a green/red label —
        so no one (human or agent) misreads the stored DELTA as if it were q. The earn path stores
        `(q-50)*SCORE_K` as the delta (op='earn', cards.py); a raw +35.0 in history is a GREEN q=85,
        NOT 'q=35'. This mistake was made live 2026-06-18 (two-witnesses-caught-the-checker-being-wrong:
        an auditor read the delta column as q and falsely called a green run a red stall). Returns one
        dict per entry: {ts, delta, q, verdict in green|red|neutral, source}."""
        c = self.card(card_id)
        if c is None:
            return []
        out = []
        for entry in json.loads(c.score_history or "[]"):
            ts, delta = entry[0], entry[1]
            src = entry[2] if len(entry) > 2 else None
            q = delta / SCORE_K + 50.0 if SCORE_K else 50.0
            verdict = "green" if delta > 0 else ("red" if delta < 0 else "neutral")
            out.append({"ts": ts, "delta": round(delta, 3), "q": round(q, 1),
                        "verdict": verdict, "source": src})
        return out

    def coord_warmth(self, coordinate: str) -> tuple[float, int]:
        """The earned warmth at a coordinate, summed over the cards that reference it: (best_score,
        total_use_count). For a `script:<name>` coord this is 'how proven is this script by reuse' —
        the honest rank signal for auto-select (warmth = EARNED proven-ness, never asserted relevance).
        Returns (SCORE_BENCHMARK, 0) for a coordinate nothing has earned at yet (neutral, unproven).

        The coordinate is NORMALIZED first (_norm_coord — lowercase, non-alnum -> '_') because that is
        how a card's refs are stored (Card.__post_init__): `script:summarize-text` is filed as
        `script:summarize_text`. Querying the raw hyphenated form would miss the very warmth it earned —
        a real bug caught live 2026-06-11 (dont-assume-read-the-substrate)."""
        coordinate = _norm_coord(coordinate)
        with self._lock:
            rows = self.conn.execute(
                "SELECT score, use_count FROM cards WHERE refs LIKE ?", (f'%"{coordinate}"%',)).fetchall()
        if not rows:
            return (SCORE_BENCHMARK, 0)
        best = max(r["score"] for r in rows)
        uses = sum(r["use_count"] for r in rows)
        return (best, uses)

    # --- FORECAST: the transformer from load-bearing seeds -> the next action (§6 invoke + §7 expand) ---
    def forecast(self, coordinate: str, budget: int = 5) -> list[Atom]:
        """Given the environment's target coordinate, compose the LOAD-BEARING atoms that predict the
        next action — score-ordered, with PATH-DEPTH FALLBACK (ascend if nothing at depth). This IS the
        'transformer from seeds into an output token': highest-earned atoms first, budget-bounded. The
        agent reads these as 'what worked here before' and acts. Earning is downstream (reinforce)."""
        segs = _norm_coord(coordinate).split(":")
        while segs:
            hits = self.atoms_at(":".join(segs))
            if hits:
                # RE-RANK by effective_score, not the raw stored score atoms_at sorted by: a cold
                # v2 atom (use_count=0) ranks by old core's BORROWED lived weight, not flat born-100.
                # This is the precondition the owner's safe-sequence demands (Mol L1577) — the borrow
                # must answer the LIVE read path before v1 writes stop. Earned atoms (use_count>0)
                # already rank by their own score (effective_score returns it). Stable sort keeps
                # atoms_at's order for ties.
                return sorted(hits, key=lambda a: self.effective_score(a)[0], reverse=True)[:budget]
            segs = segs[:-1]           # path-depth fallback — ascend one level (§7)
        return []

    # --- THINK: compose a reasoning chain from earned atoms + replay it (NO model in the hot path) ---
    # The cartridge dream, operational (owner 2026-06-16): "create a miraculous card from a stack of
    # reasoned atoms — simulated lightning fast because it was an atom." forecast() answers "what worked
    # at THIS coordinate"; think() answers "given this PROBLEM, what chain of my earned experience solves
    # it" — problem-driven, cross-coordinate, and it RELIVES the chain instead of re-reasoning. Each atom
    # is already a baked, earned conclusion, so "running" the chain is a forward PASS over baked weights
    # (warmth chain-ruled along the soul edges), not inference — that is why it is ~free and fast. A model
    # may READ the assembled chain to act, but think itself calls no model. Guarded by the immune system:
    # a chain that would route through a DISCLAIMED atom is refused (you cannot think through a known lie).
    def think(self, problem_atom_ids: list[str], *, budget: int = 7) -> dict:
        """Compose + replay a reasoning chain over EARNED atoms (no model). Input: the seed atom ids the
        problem recalled warm (the caller runs associative recall, hands the warm atom ids here). Output:
        {chain:[atom ids in reasoned order], confidence, blocked:[disclaimed ids skipped], path:[steps]}.

        COMPOSE: order the atoms by their soul edges — a `depends_on`/`part_of` target comes BEFORE its
        dependent (prerequisites first); `instances`→`refines` climbs concrete→abstract. Atoms off the
        graph keep recall order. REPLAY: walk the ordered chain; each atom contributes its effective_score
        (earned/borrowed weight) decayed by CHAIN_DECAY per hop from the head — the chain-rule, same decay
        the card backprop uses. confidence = the mean decayed earned weight, normalized to [0,1] on B.
        IMMUNE GUARD: a disclaimed atom is DROPPED from the chain (blocked) — think never routes through a
        known lie. Pure read; crystallize() turns a chain that PAID OFF into a named card (separate, earned)."""
        # 1) hydrate + immune-filter (drop disclaimed atoms — the structure antibody at think-time) +
        # DEDUP by coordinate (recall can surface the same lesson twice; one atom = one chain link).
        atoms, blocked, seen_coord = [], [], set()
        for aid in problem_atom_ids:
            a = self.get_atom(aid)
            if a is None:
                continue
            if self._JUDGED_MARK in (a.born_from or ""):
                blocked.append(aid); continue        # cannot think through a known lie
            if a.coordinate in seen_coord:
                continue                              # same lesson already in the chain
            seen_coord.add(a.coordinate)
            atoms.append(a)
        if not atoms:
            return {"chain": [], "confidence": 0.0, "blocked": blocked, "path": [],
                    "note": "no live atoms to compose"}
        atoms = atoms[:budget]

        # 2) COMPOSE: topological-ish order by the soul edges among THIS set (prereqs first). Build a
        # 'must-come-before' map from depends_on/part_of (target before source) + instances (concrete
        # before abstract). A light stable sort by in-degree gives prereqs-first without a full toposort.
        idset = {a.id for a in atoms}
        before = {a.id: 0 for a in atoms}            # how many of THIS set must precede a (higher = later)
        for a in atoms:
            for e in self.edges_of(a.id):
                if e["dir"] == "out" and e["relation"] in ("depends_on", "part_of", "instances"):
                    if e["to_id"] in idset:
                        before[a.id] += 1            # a depends on something in-set -> a comes later
        ordered = sorted(atoms, key=lambda a: (before[a.id], -self.effective_score(a)[0]))

        # 3) REPLAY (the forward pass): chain-rule the earned weight along the path. No inference.
        path, total = [], 0.0
        for hop, a in enumerate(ordered):
            eff, borrowed = self.effective_score(a)
            decayed = eff * (CHAIN_DECAY ** hop)      # warmth fades along the chain (same law as backprop)
            total += decayed
            path.append({"atom_id": a.id, "coordinate": a.coordinate, "hop": hop,
                         "earned": round(eff, 1), "borrowed": borrowed,
                         "contribution": round(decayed, 1), "gist": (a.content or "")[:80]})
        # confidence = mean decayed earned weight over the chain, normalized on the neutral benchmark.
        confidence = round(min(1.0, (total / len(ordered)) / SCORE_BENCHMARK), 3) if ordered else 0.0
        return {"chain": [a.id for a in ordered], "confidence": confidence,
                "blocked": blocked, "path": path}

    def crystallize(self, label: str, chain: list[str], *, born_from: str = "think") -> str:
        """Turn a thought that PAID OFF into a named card — 'the miraculous card from a stack of reasoned
        atoms' (owner 2026-06-16). The chain (atom ids from think()) becomes a card whose refs are the
        atoms' COORDINATES in reasoned order. Born neutral; it EARNS rank only when it is later re-run and
        succeeds (reinforce_card → §7-P2 credit flows back to every atom it chained). So a thought that
        recurs and works becomes one warm unit — the substrate LEARNED to think that thought without
        re-deriving it. Refuses to crystallize a chain containing a disclaimed atom (immune guard)."""
        coords = []
        for aid in chain:
            a = self.get_atom(aid)
            if a is None:
                continue
            if self._JUDGED_MARK in (a.born_from or ""):
                raise ValueError("cannot crystallize a chain routing through a disclaimed atom")
            coords.append(a.coordinate)
        return self.add_card(label, coords, born_from=born_from)

    # --- EARN (honesty): Q from the run OUTCOME flows to the card AND partially to its atoms (§7-P2) ---
    def reinforce_card(self, card_id: str, q: float, _propagate: bool = True,
                       _delta: float | None = None, _seen: set | None = None,
                       _hop: int = 0, source: str = "trace") -> dict:
        """Rate a card by its USE OUTCOME (Q in [0,100], from the execution trace — did the run work/
        get cheaper). Updates the card's decay-weighted score AND gives each atom it loaded a PARTIAL
        delta (1/num_atoms share) — the keystone honesty: an atom earns weight only because a card that
        USED it succeeded, never by asserting itself.

        `source` is the PROVENANCE of this Q (audit Hole 4): 'trace' (a real execution trace — the only
        source that can REDEEM a disclaimed lie), 'consent' (author say-so, session_offer), 'judged' (a
        model rating its own output — the counterfeit class). Threaded onto every history entry so the
        gates can tell proof from say-so. Defaults to 'trace' (the documented contract: Q from the trace).

        CHAIN CREDIT (the forward-pass made trainable): a card is one layer of a reasoning chain; when
        it earns, a DECAYED share (CHAIN_DECAY per hop) flows BACK along the `prev` edge to the upstream
        card that ENABLED this one — and to ITS atoms. This is the chain rule, honestly: the upstream
        earns LESS per hop (never equal, never more), only along REAL recorded edges, and only when Q
        actually moved (a neutral/failed run → delta 0 → nothing propagates). No card credits itself
        (cycle/own-id guarded); depth-capped. So `relive` becomes a true forward+backward pass over the
        chain, not isolated step-earning. See the-card-layer-must-be-chained, §7-P2, relive-dont-migrate."""
        c = self.card(card_id)
        if c is None:
            return {"ok": False, "reason": "no such card"}
        card_delta = _delta if _delta is not None else (q - 50.0) * SCORE_K
        # REDEMPTION (owner, 2026-06-11): a card DISCLAIMED as a lie that LATER re-earns through REAL TRACE
        # work resets to neutral + a one-time shock bonus — "if the lie turns out true, it gets a shock"
        # (the doubt that survived contact with proof is stronger conviction). Fires ONCE, gated on FOUR
        # conditions: a tip outcome (_delta is None, not a chain share), POSITIVE, source=='trace' (Hole 4
        # — a say-so/consent/judged earn can NEVER redeem a lie; this closes the live session_offer leak),
        # still bearing the judged mark, and not already redeemed (the `redeemed:` guard stops Fable's
        # oscillator). The math lives in the engine (op='redeem'); this just decides WHEN.
        born = c.born_from or ""
        redeemed = (_delta is None and card_delta > 0 and source == "trace"
                    and self._JUDGED_MARK in born and not born.startswith("redeemed:"))
        if redeemed:
            self._mutate_score("cards", card_id, op="redeem", delta=card_delta, source=source)
        else:
            self._apply_delta("cards", card_id, card_delta, source=source)
        # partial credit to each loaded atom (§7-P2): share the delta across the EXACT refs, carrying the
        # tip's provenance (an atom share IS-A trace earn iff the tip was). An exact-coordinate match only
        # (NOT atoms_at's prefix expansion) — a card credits the atoms it ACTUALLY loaded. Dedup by id.
        atoms: list[Atom] = []
        seen_atoms: set[str] = set()
        with self._lock:
            for ref in c.refs:
                rows = self.conn.execute("SELECT * FROM atoms WHERE coordinate=?", (ref,)).fetchall()
                for r in rows:
                    if r["id"] not in seen_atoms:
                        seen_atoms.add(r["id"])
                        atoms.append(self._atom_row(r))
        if atoms:
            share = card_delta / len(atoms)
            for a in atoms:
                self._apply_delta("atoms", a.id, share, source=source)
        # CHAIN RULE: propagate a decayed delta back along the prev-edge. Guarded: real edge only,
        # no self-credit, cycle-safe (seen set), depth-capped, and skipped once the decayed delta is
        # negligible (so a long chain terminates honestly instead of dusting infinite ancestors).
        # Provenance is INFECTIOUS DOWNSTREAM, never upgraded: a chain share of a trace tip is 'chain';
        # a share of a judged/consent tip KEEPS that source (a hop can't launder say-so into proof).
        chain_credited = 0
        if _propagate and c.prev:
            seen = _seen if _seen is not None else {card_id}
            up_delta = card_delta * CHAIN_DECAY
            up_source = "chain" if source in ("trace", "chain") else source
            # ESTATE BOUNDARY (OPEN-0021): credit never crosses scopes. A `prev` edge can link a
            # card to an upstream card in ANOTHER estate (echelon<->mol<->alpha-app share one bank);
            # propagating a chain share across it would let one estate's success inflate another's
            # weights — the isolation the whole scoping model rests on, breached silently along the
            # forward pass. Stop at the boundary: if the upstream card resolves to a DIFFERENT scope
            # than this card, do not credit it. Both scopes must resolve to compare — an unresolvable
            # scope ('' , a card whose refs are all unscoped) is not treated as a foreign estate, so
            # legacy single-estate chains keep flowing (they never crossed a boundary to begin with).
            up_card = self.card(c.prev)
            own_scope = self.card_scope(c.refs)
            up_scope = self.card_scope(up_card.refs) if up_card else ""
            crosses_estate = bool(own_scope and up_scope and own_scope != up_scope)
            if (c.prev not in seen and c.prev != card_id and not crosses_estate
                    and _hop < CHAIN_MAX_HOPS and abs(up_delta) >= CHAIN_MIN_DELTA):
                seen.add(c.prev)
                up = self.reinforce_card(c.prev, q, _propagate=True, _delta=up_delta,
                                         _seen=seen, _hop=_hop + 1, source=up_source)
                if up.get("ok"):
                    chain_credited = 1 + up.get("chain_credited", 0)
        new = self.card(card_id)
        return {"ok": True, "card_score": round(new.score, 2),
                "atoms_credited": len(atoms), "chain_credited": chain_credited,
                "tier_of_top_atom":
                (max(atoms, key=lambda x: self.get_atom(x.id).score).coordinate if atoms else None)}

    def register_use(self, card_id: str) -> bool:
        """Record that a card was USED, WITHOUT moving its score (audit #3, Fable's ruling). use_count
        is the VISIBLE 'this ran N times' record; it must NOT be weight (that was the self-amplifying
        counterfeit — a re-run inflating score). So a neutral / unverifiable run bumps use_count here
        and stops; only a verified outcome (Q≠50, via reinforce_card) moves the score. Selection
        frequency is allowed to be SEEN, never to become weight. Returns True if the card exists."""
        return self._mutate_score("cards", card_id, op="use_only")["ok"]

    # ============================ THE SCORING ENGINE (the one source of truth) ============================
    # CENTRALIZED CALCULATION (owner, 2026-06-11 — the mol Data-Engine pattern: "calculation centralized
    # so no stray value missed"). EVERY mutation to a row's (score, score_history, use_count, born_from)
    # routes through this ONE method. No caller re-derives the COS formula or hand-assembles those fields —
    # they express an INTENT (op) and honor the engine's write. Before this, three sites (earn-delta,
    # disclaim, redeem) each re-implemented the math and DRIFTED (the oscillator, the average-vs-sum bug).
    # The honesty rules (a lie lands below neutral; redemption resets to neutral + a one-time bonus; a
    # disclaim is not a use) live HERE as engine rules, not re-coded per call site. See
    # centralize-the-calculation-engine, soul-repair-is-protocol-not-maintenance.
    #
    # ops:
    #   "earn"       append `delta` to history, recompute score, +1 use_count.  (the §7-P2 / reinforce path)
    #   "decay"      append `delta` to history, recompute score, NO use bump.   (view_decay_pass — being
    #                passed over is not a use; council ruling 2026-08-25, mirrors the disclaim precedent)
    #   "use_only"   +1 use_count, score untouched.                              (register_use — seen, not weight)
    #   "disclaim"   re-base to JUDGED_FLOOR via a corrective delta, stamp born_from, NO use bump, idempotent.
    #   "redeem"     wipe dead history, reset to neutral + `delta` + SHOCK_BONUS, +1 use, stamp born_from.
    # PER-ENTRY SOURCE PROVENANCE (audit Hole 3+4, 2026-06-12). Every score_history entry an op appends
    # is [ts, delta, src] — the PROVENANCE of that delta. compute_score stays source-BLIND (all sources
    # weight identically; differential weighting would silently move live scores = a relive-violation).
    # src is read ONLY by the gates: redeem KEEPS honest-source entries + discards the lie; redemption
    # FIRES only on src='trace' (a say-so earn can never redeem a lie). Legacy 2-element rows read back as
    # src=None (UNKNOWN — never silently promoted to 'trace'). See centralize-the-calculation-engine.
    _HONEST_SOURCES = ("trace", "chain", "consent")   # real-experience provenance kept across a redeem

    def _mutate_score(self, table: str, row_id: str, *, op: str,
                      delta: float = 0.0, reason: str = "", source: str = "trace") -> dict:
        _check_table(table)
        with self._lock:
            r = self.conn.execute(
                f"SELECT score,score_history,use_count,born_from FROM {table} WHERE id=?",
                (row_id,)).fetchone()
            if r is None:
                return {"ok": False, "reason": "no such row"}
            hist = json.loads(r["score_history"] or "[]")
            born = r["born_from"] or ""
            use_count = r["use_count"]
            now = int(time.time())
            new_born = None   # only set when the op changes provenance
            # Look up the atom's kind for per-type lambda (cards fall back to DEFAULT_LAMBDA)
            _kind = None
            if table == "atoms":
                try:
                    kr = self.conn.execute("SELECT kind FROM atoms WHERE id=?", (row_id,)).fetchone()
                    _kind = kr["kind"] if kr and kr["kind"] else None
                except Exception:
                    pass
            _lam = lam_for(_kind)

            if op == "use_only":
                # score + history untouched; only the visible use record moves.
                self.conn.execute(f"UPDATE {table} SET use_count=? WHERE id=?", (use_count + 1, row_id))
                self.conn.commit()
                return {"ok": True, "score": round(r["score"], 2), "noop_score": True}

            elif op == "earn":
                hist.append([now, round(delta, 3), source])   # tag the earn's provenance
                use_count += 1

            elif op == "decay":
                # Identical to "earn" except NO use_count bump — being passed over in a
                # recall result set is not a use (mirrors "a disclaim is not a use" below).
                # Council ruling 2026-08-25: view-decay was silently inflating use_count on
                # every atom it penalized, so a decayed atom looked MORE used, not less seen.
                hist.append([now, round(delta, 3), source])   # tag the decay's provenance
                # use_count intentionally untouched

            elif op == "disclaim":
                if self._JUDGED_MARK in born:
                    # Idempotent — but an explicit no-successor acknowledgment may still be
                    # RECORDED on an already-disclaimed atom (settles ANTIBODY 4's orphan warn
                    # without touching the score): append the marker to born_from once.
                    if "[no-successor]" in reason and "[no-successor]" not in born:
                        self.conn.execute(f"UPDATE {table} SET born_from=? WHERE id=?",
                                          (born + " [no-successor]", row_id))
                        self.conn.commit()
                        return {"ok": True, "before": r["score"], "after": r["score"],
                                "delta": 0.0, "noop": True, "acknowledged": True}
                    return {"ok": True, "before": r["score"], "after": r["score"], "delta": 0.0,
                            "noop": True}   # already disclaimed — idempotent
                # corrective that drives compute_score to JUDGED_FLOOR (below neutral — a discovered lie
                # is worse evidence than an untested card). d = target_offset·(den+1) − num. Append-only:
                # the judged entries STAY, the lie stays legible; one new entry re-bases the average.
                num = den = 0.0
                for ts, dl, *_ in hist:
                    w = math.exp(-_lam * max(0.0, (now - ts) / 86400.0))
                    num += dl * w
                    den += w
                target_offset = self.JUDGED_FLOOR - SCORE_BENCHMARK
                d = target_offset * (den + 1.0) - num
                hist.append([now, round(d, 3), "disclaim"])
                stamp = f"{self._JUDGED_MARK} ts:{now}" + (f" {reason}" if reason else "")
                new_born = f"{stamp} <- {born}" if born else stamp   # preserve origin lineage
                # NO use_count bump — a disclaim is not a use.

            elif op == "redeem":
                # WIPE THE LIE, KEEP THE HONEST RECORD (Hole 3, Fable). Discard judged/disclaim/legacy
                # (unknown) entries — the dead drag that the reset clears — but PRESERVE real-experience
                # entries (trace/chain/consent), so a card that genuinely FAILED before redemption still
                # carries those failures (discharging the lie is right; laundering real failures is not).
                # Then add the rebirth = real earn + the one-time shock bonus, tagged 'redeem'. The bonus
                # is diluted by however much HONEST history remains — correct: a heavily-tested card's
                # doubt-resolution moves it less than a near-virgin card's. Lineage preserved in born_from
                # so the dream never re-disclaims it.
                kept = [e for e in hist if len(e) > 2 and e[2] in self._HONEST_SOURCES]
                kept.append([now, round(delta + SHOCK_BONUS, 3), "redeem"])
                hist = kept
                use_count += 1
                new_born = f"redeemed:from-judged ts:{now} bonus:{SHOCK_BONUS:g} <- {born}"

            else:
                return {"ok": False, "reason": f"unknown op {op!r}"}

            new_score = compute_score(hist, now=now, lam=_lam)
            if new_born is None:
                self.conn.execute(
                    f"UPDATE {table} SET score=?, score_history=?, use_count=? WHERE id=?",
                    (new_score, json.dumps(hist), use_count, row_id))
            else:
                self.conn.execute(
                    f"UPDATE {table} SET score=?, score_history=?, use_count=?, born_from=? WHERE id=?",
                    (new_score, json.dumps(hist), use_count, new_born, row_id))
            self.conn.commit()
            return {"ok": True, "before": round(r["score"], 2), "after": round(new_score, 2),
                    "delta": round(delta, 3)}

    def _apply_delta(self, table: str, row_id: str, delta: float, source: str = "trace",
                     op: str = "earn") -> None:
        """Thin caller — the engine owns the math. Default op='earn': append delta+provenance,
        recompute, +1 use. Pass op='decay' for view-decay (append+recompute, NO use bump) — every
        other caller stays on the default and is unaffected."""
        _check_table(table)
        self._mutate_score(table, row_id, op=op, delta=delta, source=source)

    # --- judged-origin disclaim (audit #7): the DREAM's soul-repair mechanism, not a hand pass ---
    # A card/atom whose score came from a model JUDGING ITS OWN answer (eval/relive_multipass's self-rate
    # → reinforce_card) is a counterfeit: "Q from a model's say-so", exactly what the cards.py spine
    # forbids. NEVER DELETE (the cure is re-earning by living, not erasure — relive-dont-migrate; and the
    # evidence the mint happened must stay legible). So we DISCLAIM: re-base the row and stamp born_from
    # as the receipt, then let it re-earn through the real trace loop. Invoked BY THE DREAM (soul-repair
    # is protocol, not maintenance — never raw-SQL the soul by hand) so the class is caught at field-cold.
    #
    # A DISCOVERED LIE LANDS BELOW NEUTRAL, NOT AT IT (owner, 2026-06-11): "like me who got lied to — I
    # still remember, but I know it was a lie." Re-basing a counterfeit to neutral (100) would say "as if
    # it never happened" — erasing the lesson. But a PROVEN-FAKE card is WORSE evidence than a fresh
    # untested one: it should rank BELOW a born-neutral card (100), carrying the mark that it lied. So the
    # disclaim re-bases to JUDGED_FLOOR (< B), not B. The memory of the lie is itself load-bearing — it
    # makes recall warier of this coordinate, exactly as being-lied-to makes a person warier.
    _JUDGED_MARK = "judged:disclaimed"
    _REDEEMED_MARK = "redeemed:"   # born_from prefix a trace-redeem stamps; the lie was discharged
    # self_seed is a v1-only flag (no column on the v2 atoms row). If a v2 self-seed writer ever
    # exists it MUST stamp this born_from prefix so the promotion gate can exclude the wish from the
    # constitution (a wish must never reach L1 by self-witnessing — dream-and-the-respect-handshake).
    # Today no atom bears it (0 matches) — the guard is in place, mechanically checkable, ahead of the
    # writer, exactly as the architect ruled ("exclude atoms with a born_from self-mark from BEACON").
    _SELF_MARK = "self:"
    # kinds that are persona-LOCAL by nature: eligible in their own scope if they earn, but never
    # surfaced estate-wide (the architect's ruling — persona identity does not travel across scopes).
    _PERSONA_LOCAL_KINDS = frozenset({"user", "persona", "identity"})
    JUDGED_FLOOR = SCORE_BENCHMARK - 25.0   # 75: below a fresh card (100), above destroyed (never delete)

    def disclaim_judged(self, table: str, row_id: str, reason: str = "") -> dict:
        """Re-base a judged-origin (self-rated, counterfeit) row to the JUDGED_FLOOR (BELOW neutral — a
        discovered lie is worse evidence than an untested card, owner 2026-06-11). Append-only, no
        use_count bump (a disclaim is not a use). Idempotent: a row already disclaimed is a no-op. The
        row is NEVER deleted — the lie stays legible, recall reads this coordinate as cold/suspect.
        Thin caller: the math + the JUDGED_FLOOR re-base + the lineage-preserving born_from stamp all
        live in the scoring engine (op='disclaim') — the one source of truth. Returns {ok, before, after}."""
        _check_table(table)
        return self._mutate_score(table, row_id, op="disclaim", reason=reason)

    def redeem_by_trace(self, table: str, row_id: str, witness: str, source: str = "trace") -> dict:
        """Redeem a DISCLAIMED row that a real trace proves TRUE (Hole-4 gate: redemption is TRACE-ONLY,
        never say-so). The card-earn path already redeems a disclaimed CARD that re-earns by trace
        (reinforce_card, op='redeem'); this is the same law for an ATOM (and any row) that the dream
        wrongly disclaimed — e.g. a dream marked `relive-resumes-...` a lie, but a session that ACTUALLY
        RAN --relive is a live witness it is true. `witness` is the trace receipt (stamped into born_from
        for legibility); `source` MUST be an honest provenance (trace/chain) — a say-so source is rejected,
        closing the same counterfeit leak the card redeem guards. Idempotent-ish: a non-disclaimed row is
        a no-op. The redeem MATH (wipe the lie, keep honest history, reset+shock) is the ONE engine op.
        Returns {ok, before, after} or {ok:False} when not disclaimed / dishonest source."""
        _check_table(table)
        if source not in self._HONEST_SOURCES:
            return {"ok": False, "reason": f"redemption is trace-only; source={source!r} cannot redeem"}
        r = self.conn.execute(f"SELECT born_from FROM {table} WHERE id=?", (row_id,)).fetchone()
        if r is None:
            return {"ok": False, "reason": "no such row"}
        if self._JUDGED_MARK not in (r["born_from"] or ""):
            return {"ok": True, "noop": True, "reason": "not disclaimed — nothing to redeem"}
        # the rebirth delta is a positive earn (the witness is real); the engine's redeem op wipes the
        # lie, preserves honest entries, resets to neutral + the one-time shock, stamps the receipt.
        return self._mutate_score(table, row_id, op="redeem", delta=SCORE_K * 35.0,
                                  reason=f"redeemed-by-trace:{witness}"[:120], source=source)

    # --- reasoner-initiated dispute (2026-06-16, owner-appointed): the HAND the dream lacked ---
    # find_judged_origin catches ONE class of lie — the self-rated counterfeit — by structural tell.
    # But a STALE or WRONG atom that was HONESTLY authored (e.g. an "OPEN debt" finding whose debt was
    # later CLOSED, or a claim like "the soul is never touched" that reading the code disproves) has no
    # structural tell and no detector. It sits at full warmth, read-as-truth, misleading every cold
    # reader — the exact disease the owner named: "warmth rewarded reading, not truth; there was never a
    # proper way to say THIS ATOM IS WRONG." `dispute` is that hand. It does NOT trust the disputer's
    # say-so as proof (that would mint the same counterfeit class): it re-bases the stale row to the
    # JUDGED_FLOOR (below a fresh atom — a known-stale claim is worse evidence than an untested one),
    # NEVER deletes (the stale claim stays legible as a receipt of what-was-believed), stamps the reason +
    # what superseded it, and tags the corrective with source='reasoner' — NOT in _HONEST_SOURCES, so a
    # dispute can drive a lie DOWN but can never REDEEM one (redemption stays trace-only, the §Hole-4
    # gate). If the disputed claim later proves true again, only a real TRACE earn redeems it. The
    # disputed coordinate reads COLD thereafter — reading a known-stale atom no longer warms it.
    def dispute(self, table: str, row_id: str, reason: str, superseded_by: str = "") -> dict:
        """Reasoner files: THIS atom/card is WRONG or STALE. Re-bases it to JUDGED_FLOOR (append-only, no
        use bump, never deleted), stamping `reason` + optional `superseded_by` (the coordinate/id of the
        atom that holds the current truth) as the born_from receipt. Idempotent (a row already disputed/
        disclaimed is a no-op). source='reasoner' → can lower a lie, can NEVER redeem one (only a trace
        earn redeems). Returns {ok, before, after}. The disclaim math is the ONE engine op — this is a
        thin, honest caller (centralize-the-calculation-engine)."""
        _check_table(table)
        note = reason if not superseded_by else f"{reason} | superseded_by:{superseded_by}"
        return self._mutate_score(table, row_id, op="disclaim", reason=note, source="reasoner")

    def migrate_scope(self, coordinates: list[str], to_scope: str, *,
                      from_scope: str | None = None, dry_run: bool = False) -> dict:
        """Re-home atoms to `to_scope` by RETAG, never re-ingest (R-0154 slice 2, the dedup law
        [[cross-scope-duplication-is-ingest-pollution]]): a move updates the `scope` COLUMN on the
        three scope-bearing tables (atoms.scope keyed by id, atom_spine.scope + atom_earned.scope
        keyed by atom_id) in ONE transaction. Edges (atom_links), body, sidecar and the FTS index
        are keyed by atom id and are untouched — the atom keeps its weight, its links, its history;
        only its home scope changes. No new row is minted, so no cross-scope duplicate is created.

        `coordinates` are the atoms' stable identities (scope:slug or bare coord). `from_scope`, when
        given, is a guard — only atoms currently in it move (a coordinate not in from_scope is
        skipped and reported). Returns {ok, moved:[coords], skipped:[(coord,reason)], counts:{...}}.
        dry_run reports the plan and writes ZERO rows."""
        if not to_scope:
            raise ValueError("migrate_scope needs a target scope")
        coords = [str(c).split(":", 1)[1] if ":" in str(c) and str(c).split(":", 1)[0] not in ("",)
                  else str(c) for c in coordinates]
        # match on the bare slug OR the full coordinate — an atom's coordinate is `<scope>:<slug>`.
        moved, skipped = [], []
        with self._lock:
            move_ids = []          # EVERY atom row of a matched coordinate (a coordinate can carry
                                   # more than one atom id — moving only one leaves a split-scope atom,
                                   # the exact pollution the dedup law forbids).
            for c in coordinates:
                c = str(c)
                rows = self.conn.execute(
                    "SELECT id, coordinate, scope FROM atoms WHERE coordinate=?", (c,)).fetchall()
                if not rows and ":" not in c:
                    # bare slug: every atom whose coordinate ends with :<slug>
                    rows = self.conn.execute(
                        "SELECT id, coordinate, scope FROM atoms WHERE coordinate LIKE ?",
                        (f"%:{c}",)).fetchall()
                if not rows:
                    skipped.append((c, "not found")); continue
                eligible = [r for r in rows
                            if (not from_scope or r["scope"] == from_scope) and r["scope"] != to_scope]
                if not eligible:
                    reasons = {r["scope"] for r in rows}
                    skipped.append((c, "already in target scope" if to_scope in reasons
                                    else f"in scope(s) {sorted(reasons)!r}, not {from_scope!r}")); continue
                move_ids.extend(r["id"] for r in eligible)
                moved.append(rows[0]["coordinate"])
            counts = {"atoms": 0, "atom_spine": 0, "atom_earned": 0}
            if not dry_run and move_ids:
                ids = move_ids
                qs = ",".join("?" * len(ids))
                cur = self.conn.execute(
                    f"UPDATE atoms SET scope=? WHERE id IN ({qs})", (to_scope, *ids))
                counts["atoms"] = cur.rowcount
                for tbl in ("atom_spine", "atom_earned"):
                    cur = self.conn.execute(
                        f"UPDATE {tbl} SET scope=? WHERE atom_id IN ({qs})", (to_scope, *ids))
                    counts[tbl] = cur.rowcount
                self.conn.commit()
        return {"ok": True, "dry_run": dry_run, "to_scope": to_scope, "from_scope": from_scope,
                "moved": moved, "skipped": skipped, "counts": counts}

    def find_judged_origin(self) -> dict:
        """Detect judged-origin (counterfeit, self-rated) rows the DREAM should disclaim (audit #7). Two
        mechanical tells, both structural (not content): (1) a row whose born_from is STAMPED judged at
        mint (the going-forward path — relive/self-judge writers now stamp 'judged:...'); (2) the legacy
        relive_multipass signature — a card label starting 'relive ' (and its ref'd atoms), minted before
        the stamp existed, that has NOT yet been disclaimed. Returns {cards:[ids], atoms:[ids]} of rows
        eligible for disclaim (already-disclaimed rows are excluded — idempotent). NEVER deletes; only
        names. The dream applies disclaim_judged to each. NO content read, NO judgement — pure pattern."""
        m = self._JUDGED_MARK
        like_m = f"%{m}%"
        with self._lock:
            cards = self.conn.execute(
                "SELECT id, refs FROM cards WHERE "
                "(born_from LIKE 'judged:%' OR label LIKE 'relive %') "
                "AND born_from NOT LIKE ?", (like_m,)).fetchall()
            card_ids = [r["id"] for r in cards]
            # the atoms those counterfeit cards reffed (the §7-P2 share gave them the same fake weight).
            atom_coords: set[str] = set()
            for r in cards:
                try:
                    atom_coords.update(json.loads(r["refs"] or "[]"))
                except Exception:
                    pass
            atom_ids: list[str] = []
            if atom_coords:
                qs = ",".join("?" * len(atom_coords))
                rows = self.conn.execute(
                    f"SELECT id FROM atoms WHERE coordinate IN ({qs}) AND born_from NOT LIKE ?",
                    (*atom_coords, like_m)).fetchall()
                atom_ids = [r["id"] for r in rows]
            # also any atom STAMPED judged directly (going-forward writers)
            stamped = self.conn.execute(
                "SELECT id FROM atoms WHERE born_from LIKE 'judged:%' AND born_from NOT LIKE ?",
                (like_m,)).fetchall()
            atom_ids = list(dict.fromkeys(atom_ids + [r["id"] for r in stamped]))
        return {"cards": card_ids, "atoms": atom_ids}

    # --- tiers (earned, not asserted) ---
    def eligible_atoms(self) -> list[Atom]:
        """Atoms that EARNED L2 (synthesis-eligible, >B+25) — proven by use, not declared."""
        with self._lock:
            rows = self.conn.execute("SELECT * FROM atoms WHERE score>=? ORDER BY score DESC",
                                     (SYNTH_ELIGIBLE,)).fetchall()
        return [self._atom_row(r) for r in rows]

    def beacons(self) -> list[Atom]:
        """Atoms that EARNED beacon status (L1 candidate, well above average) — the grand vote ratifies."""
        with self._lock:
            rows = self.conn.execute("SELECT * FROM atoms WHERE score>=? ORDER BY score DESC",
                                     (BEACON_THRESHOLD,)).fetchall()
        return [self._atom_row(r) for r in rows]

    # ── the LIVE earning query (the harness-hook door: stop approximating with raw atoms.score) ──
    def top_earned(self, limit: int = 20, exclude_scopes=None) -> list[tuple]:
        """The ONE cheap, indexed query the boot/nerve hooks call instead of hand-reading atoms.score.

        Returns [(scope, content, effective_score)] ordered by effective_score DESC, atom_earned-AWARE:
          - a USED atom (atom_earned.use_count>0) ranks by max(atoms.score, atom_earned.score) — the
            witnessed-door earned weight, the same rule effective_score() applies in Python;
          - a disclaimed atom (born_from carries the judged mark) ranks by its re-based atoms.score
            (the lie is not laundered back up by a stale earned row) — mirrors effective_scores_by_content;
          - an unused atom ranks at its neutral atoms.score.

        Single SQL statement over the idx_earned_scope_score / idx_struct_earned_score indexes — no
        per-row Python fetch (the approximation the hooks were doing). exclude_scopes filters whole
        scopes out (e.g. hooks surfacing estate-wide skip persona-local scopes). Best-effort: [] on error.
        """
        exclude_scopes = set(exclude_scopes or ())
        mark = self._JUDGED_MARK
        try:
            with self._lock:
                # effective_score in ONE expression: disclaimed -> atoms.score; used -> max(atoms.score,
                # earned.score); else atoms.score. instr(born_from, mark) is 0 when the mark is absent.
                rows = self.conn.execute(
                    "SELECT a.scope, a.content, "
                    "  CASE "
                    "    WHEN instr(a.born_from, ?) > 0 THEN a.score "
                    "    WHEN COALESCE(e.use_count,0) > 0 THEN MAX(a.score, COALESCE(e.score, a.score)) "
                    "    ELSE a.score "
                    "  END AS eff "
                    "FROM atoms a LEFT JOIN atom_earned e ON e.atom_id = a.id "
                    "ORDER BY eff DESC LIMIT ?",
                    (mark, limit + len(exclude_scopes) * limit if exclude_scopes else limit)).fetchall()
            out = []
            for r in rows:
                if r["scope"] in exclude_scopes:
                    continue
                out.append((r["scope"], r["content"], float(r["eff"])))
                if len(out) >= limit:
                    break
            return out
        except Exception:
            return []

    # ── the re-based promotion predicate (the DEAD v1 gate re-homed on the LIVE v2 signal) ──
    def promotable_atoms(self, scope: str, *, promote_score: float = SYNTH_ELIGIBLE,
                         min_uses: int = 3, beacon_score: float = BEACON_THRESHOLD) -> list[dict]:
        """The v2-native answer to 'which atoms in this scope EARNED the right to rise?' — the live
        replacement for the dead v1 gate (levels.meets_requirement read seed.recall_count, which is 0
        on every seed; no live path ever incremented it — the promotion pipeline never fired).

        The re-based bar (owner's isolation probe, addendum 2026-07-08):
          effective_score >= promote_score (125 = SYNTH_ELIGIBLE, L2)  AND
          atom_earned.use_count >= min_uses (3 WITNESSED USE events — the honest 'use' signal: use_count
            increments ONLY in _witness_earn, fired ONLY by the remember_fetch door; the free peek never
            touches it, so the kindle inversion holds by construction — reads never earn).

        DOCTRINE GATES (all mechanically checkable, none faked):
          - disclaimed (born_from carries the judged mark) -> INELIGIBLE (a discovered lie cannot rise).
          - self-marked (born_from _SELF_MARK) -> ineligible for BEACON/L1 (a wish must not reach the
            constitution by self-witnessing) but still counts as L2-eligible in its own scope.
          - append-only: this READS; it never rewrites an atom row. 'Promotion' in v2 = crossing an
            earned threshold (tier is earned-not-asserted here — there is no working->core row move).

        Returns one dict per eligible atom: {id, scope, content, effective_score, use_count, tier,
        beacon, self_marked} ordered by effective_score DESC. tier is 'L2' (>=125) or 'L1' (>=beacon).
        beacon=True means it ALSO clears the L1 candidate bar AND is not self-marked (grand-vote-worthy).
        """
        mark = self._JUDGED_MARK
        out: list[dict] = []
        try:
            with self._lock:
                rows = self.conn.execute(
                    "SELECT a.id, a.scope, a.content, a.score AS ascore, a.born_from, "
                    "  COALESCE(e.score, a.score) AS escore, COALESCE(e.use_count, 0) AS euse "
                    "FROM atoms a LEFT JOIN atom_earned e ON e.atom_id = a.id "
                    "WHERE a.scope = ?", (scope,)).fetchall()
        except Exception:
            return []
        for r in rows:
            bf = r["born_from"] or ""
            disclaimed = mark in bf
            if disclaimed:
                continue                      # a lie never rises
            use_count = int(r["euse"] or 0)
            if use_count < min_uses:
                continue                      # not proven over enough witnessed use
            # effective_score: used -> max(atoms.score, earned.score); else neutral atoms.score.
            eff = max(float(r["ascore"]), float(r["escore"])) if use_count > 0 else float(r["ascore"])
            if eff < promote_score:
                continue                      # not warm enough
            self_marked = bf.startswith(self._SELF_MARK)
            is_beacon = (eff >= beacon_score) and not self_marked   # a wish never reaches L1
            out.append({
                "id": r["id"], "scope": r["scope"], "content": r["content"],
                "effective_score": round(eff, 1), "use_count": use_count,
                "tier": "L1" if is_beacon else "L2",
                "beacon": is_beacon, "self_marked": self_marked,
            })
        out.sort(key=lambda d: d["effective_score"], reverse=True)
        return out

    # ── Impression log (view-through decay telemetry) ──────────────────────────

    def log_impressions(self, scope: str, reasoning: str,
                        atom_coords: list[str]) -> int:
        """Insert one impressions row per surfaced atom for the current recall.
        Uses executemany for speed. NOT run every user turn: impressions are logged
        only by an explicit `recall --warm` invocation (CLI, or the ask_with_recall
        hook path) — the per-turn nerve hook calls warmth() in-process and does not
        log impressions. `reasoning` is hashed for the query_hash column (grouping
        atoms from the same recall)."""
        if not atom_coords:
            return 0
        now = int(time.time())
        query_hash = hashlib.sha256(
            (reasoning or "")[:200].encode("utf-8")).hexdigest()[:16]
        rows = [(now, scope, query_hash, coord, 0, 0) for coord in atom_coords]
        with self._lock:
            self.conn.executemany(
                "INSERT INTO impressions(ts, scope, query_hash, atom_coord, opened, consumed) "
                "VALUES (?,?,?,?,?,?)", rows)
            self.conn.commit()
        return len(rows)

    def mark_impressions_opened(self, scope: str, atom_coord: str,
                                window_minutes: int = 30) -> int:
        """Mark opened=1 for all impressions of `atom_coord` in `scope` within the
        last `window_minutes` minutes. This is the 'result set it competed in' rule:
        when remember fetches an atom, mark all its sibling impressions from recent
        recalls so the view-decay pass knows it was taken up (not passed over)."""
        cutoff = int(time.time()) - (window_minutes * 60)
        with self._lock:
            cur = self.conn.execute(
                "UPDATE impressions SET opened=1 "
                "WHERE atom_coord=? AND scope=? AND ts >= ? AND opened=0",
                (atom_coord, scope, cutoff))
            self.conn.commit()
            return cur.rowcount or 0

    def evict_impressions(self, days: int = 90) -> int:
        """Delete impressions older than `days`. Impressions are operational
        telemetry, not memory — deletion is allowed here (and ONLY here)."""
        cutoff = int(time.time()) - (days * 86400)
        with self._lock:
            cur = self.conn.execute(
                "DELETE FROM impressions WHERE ts < ?", (cutoff,))
            self.conn.commit()
            return cur.rowcount or 0

    def close(self):
        self.conn.close()

    # ── View-through decay — the batch delta writer ────────────────────────────

    # Exempt types: feedback and user atoms don't rot from being unread
    _VIEW_DECAY_EXEMPT = {"feedback", "user"}
    _VIEW_DECAY_K = 10          # losing impressions per -1.0 delta
    _VIEW_DECAY_MAX = -3.0      # max total delta from view-decay per pass
    _VIEW_DECAY_FLOOR = 85.0    # never take an atom below this
    _VIEW_DECAY_WINDOW = 30     # days

    def view_decay_pass(self, scope: str) -> dict:
        """Batch view-through decay: atoms surfaced but passed over in impression
        logs get a small decay. Only fires for query sets where at least one OTHER
        atom was opened (the 'lost to a sibling' rule). K=10 losing impressions
        → one -1.0 delta, capped at -3.0 per atom per pass, floor 85.
        Feedback/user atoms are exempt. Idempotent: marks consumed=1 so re-runs
        don't double-charge. Returns {atom_count, total_delta, deltas_applied,
        atoms_exempt, atoms_at_floor}."""
        now = int(time.time())
        cutoff = now - (self._VIEW_DECAY_WINDOW * 86400)
        result = {"atom_count": 0, "total_delta": 0.0, "deltas_applied": 0,
                  "atoms_exempt": 0, "atoms_at_floor": 0, "details": []}

        with self._lock:
            # Find query sets where at least one atom was opened (the 'competed' sets)
            self.conn.execute(
                "CREATE TEMP TABLE IF NOT EXISTS _losing_queries AS "
                "SELECT DISTINCT query_hash, scope FROM impressions "
                "WHERE ts >= ? AND consumed=0 AND opened=1", (cutoff,))

            # For each atom in those query sets where opened=0 and consumed=0,
            # count losing impressions
            rows = self.conn.execute("""
                SELECT i.atom_coord, i.scope, COUNT(*) as lose_count
                FROM impressions i
                JOIN _losing_queries lq
                  ON i.query_hash = lq.query_hash AND i.scope = lq.scope
                WHERE i.ts >= ? AND i.consumed = 0 AND i.opened = 0
                GROUP BY i.atom_coord, i.scope
            """, (cutoff,)).fetchall()

            self.conn.execute("DROP TABLE IF EXISTS _losing_queries")

        if not rows:
            return result

        consume_pairs: list[tuple[str, str]] = []
        for r in rows:
            coord = r["atom_coord"]
            scope_val = r["scope"]
            lose_count = r["lose_count"]

            # Resolve coordinate to atom_id
            atom_id = self.atom_id_for_coordinate(coord) or coord
            atom = self.get_atom(atom_id) if atom_id else None
            if not atom:
                continue

            # Exempt feedback/user types — consume their rows (they can never delta)
            if atom.kind in self._VIEW_DECAY_EXEMPT:
                result["atoms_exempt"] += 1
                consume_pairs.append((coord, scope_val))
                continue

            # Compute delta: floor(lose_count / K) capped at max
            raw_delta = -(lose_count // self._VIEW_DECAY_K)
            delta = max(raw_delta, self._VIEW_DECAY_MAX)
            if delta >= 0:
                continue

            # Floor check — don't let view-decay push below floor
            current_score = self.effective_score(atom)[0]
            if current_score + delta < self._VIEW_DECAY_FLOOR:
                # Clamp: only apply enough delta to reach floor
                delta = self._VIEW_DECAY_FLOOR - current_score
                if delta >= 0:
                    result["atoms_at_floor"] += 1
                    consume_pairs.append((coord, scope_val))
                    continue
                # delta is still negative but clamped to floor
                result["atoms_at_floor"] += 1

            # Apply via the existing _apply_delta door (append-only, recomputes)
            try:
                self._apply_delta("atoms", atom_id, float(delta), source="view-decay", op="decay")
                result["deltas_applied"] += 1
                result["total_delta"] += delta
                result["atom_count"] += 1
                result["details"].append({
                    "coord": coord, "atom_id": atom_id[:12],
                    "lose_count": lose_count, "delta": round(delta, 3),
                    "kind": atom.kind,
                })
                consume_pairs.append((coord, scope_val))
            except Exception:
                continue

        # Idempotence: consume ONLY the rows of atoms whose fate is settled this pass
        # (delta applied / exempt / at floor). Sub-threshold atoms (< K losing rows)
        # keep their rows unconsumed so impressions ACCUMULATE across passes — a
        # blanket wipe here would reset slow-bleed atoms every wrap and they would
        # never decay (gate finding 2026-07-31).
        if consume_pairs:
            try:
                with self._lock:
                    for _coord, _sc in consume_pairs:
                        self.conn.execute(
                            "UPDATE impressions SET consumed=1 "
                            "WHERE atom_coord=? AND scope=? AND ts >= ? "
                            "AND consumed=0 AND opened=0", (_coord, _sc, cutoff))
                    self.conn.commit()
            except Exception:
                pass

        return result

    # ── Dormant tier (2026-07-31) ────────────────────────────────────────────
    # DORMANT = COMPUTED STATE, read-time, from effective_score vs threshold.
    # No column that needs a batch pass to maintain; no scheduled demotion job.
    # Reuses _VIEW_DECAY_EXEMPT as the dormancy exemption list: feedback/user
    # atoms are truths that can NEVER be dormant (same rationale as view-decay).

    def dormant_content_set(self, scope: str | None = None,
                            threshold: float = DORMANT_THRESHOLD) -> set:
        """Return content[:120] prefixes belonging ONLY to dormant atoms in `scope`.
        Used by warmth() to filter dormant seeds from default recall ranking.
        Replicates effective_score() logic: own history + door-borrow from
        atom_earned, max of both.

        GATE FIX (2026-07-31): the prefix is a cross-store bridge, and prefixes
        COLLIDE — cross-scope duplicate bodies and superseded near-twins share
        their first 120 chars (measured live: 97 dormant atoms' prefixes matched
        210 atoms — 113 of them LIVE). A prefix therefore enters the filter set
        only when EVERY atom bearing it is dormant; a live twin anywhere in the
        queried scope vetoes it. Direction of error is under-filtering (a shared
        -prefix dormant atom stays visible), never hiding live knowledge."""
        dormant: set = set()
        live: set = set()
        now = int(time.time())
        with self._lock:
            if scope:
                rows = self.conn.execute(
                    "SELECT SUBSTR(a.content,1,120) as ck, a.score, "
                    "a.score_history, a.kind, a.born_from, a.use_count, "
                    "e.use_count as earned_use_count, "
                    "e.score_history as earned_history "
                    "FROM atoms a LEFT JOIN atom_earned e ON e.atom_id = a.id "
                    "WHERE a.scope=?",
                    (scope,)).fetchall()
            else:
                rows = self.conn.execute(
                    "SELECT SUBSTR(a.content,1,120) as ck, a.score, "
                    "a.score_history, a.kind, a.born_from, a.use_count, "
                    "e.use_count as earned_use_count, "
                    "e.score_history as earned_history "
                    "FROM atoms a LEFT JOIN atom_earned e ON e.atom_id = a.id"
                    ).fetchall()
        for r in rows:
            ck = (r["ck"] or "")[:120]
            if r["kind"] in self._VIEW_DECAY_EXEMPT:
                live.add(ck)
                continue
            eff = self._effective_from_row(
                score=float(r["score"] or 100.0),
                score_history=r["score_history"],
                kind=r["kind"],
                born_from=r["born_from"] or "",
                use_count=r["use_count"] or 0,
                earned_use_count=r["earned_use_count"] or 0,
                earned_history_str=r["earned_history"],
                now=now,
                door_require_earned_use=False,
            )
            (dormant if eff < threshold else live).add(ck)
        return dormant - live

    def superseded_content_set(self, scope: str | None = None) -> set:
        """Return content[:120] prefixes belonging ONLY to atoms that are the TARGET of a
        LIVE `supersedes` edge (from supersedes to; superseded_on NULL/''/0 = the edge stands).
        Used by warmth() to DAMPEN (not drop) superseded seeds at rank time — an atom with a
        live successor must not outrank it on lexical overlap alone (the June-handoff-over-
        August-wrap failure, 2026-08-12). Claim-level and structural: honors the council guard
        that recency is a filter, never a re-weighting — this reads the supersession EDGE, not
        the clock.

        Same two guards as dormant_content_set: (1) self-loops are data noise and are ignored
        (an atom cannot supersede itself into hiding); (2) LIVE-BEARER VETO — prefixes collide
        across duplicate bodies, so a prefix enters the set only when EVERY atom bearing it is
        superseded; one live bearer vetoes it. Direction of error is under-filtering.
        Empty set on any error — recall degrades to un-dampened, never crashes."""
        superseded: set = set()
        live: set = set()
        try:
            with self._lock:
                target_rows = self.conn.execute(
                    "SELECT DISTINCT to_id FROM atom_links WHERE relation='supersedes' "
                    "AND from_id != to_id "
                    "AND (superseded_on IS NULL OR superseded_on='' OR superseded_on=0)"
                ).fetchall()
                targets = {r["to_id"] for r in target_rows}
                if not targets:
                    return set()
                if scope:
                    rows = self.conn.execute(
                        "SELECT id, SUBSTR(content,1,120) as ck FROM atoms WHERE scope=?",
                        (scope,)).fetchall()
                else:
                    rows = self.conn.execute(
                        "SELECT id, SUBSTR(content,1,120) as ck FROM atoms").fetchall()
            for r in rows:
                ck = (r["ck"] or "")[:120]
                if not ck:
                    continue
                (superseded if r["id"] in targets else live).add(ck)
        except Exception:
            return set()
        return superseded - live

    def count_dormant(self, scope: str | None = None,
                      threshold: float = DORMANT_THRESHOLD) -> dict:
        """Count dormant atoms in `scope`. Returns {count, list_of_atoms} for
        status reporting — the bank must not silently look smaller.
        Replicates effective_score() logic: own history + door-borrow."""
        dormant_slugs: list = []
        now = int(time.time())
        with self._lock:
            if scope:
                rows = self.conn.execute(
                    "SELECT a.id, SUBSTR(a.content,1,120) as ck, a.score, "
                    "a.score_history, a.kind, a.born_from, "
                    "a.use_count, e.use_count as earned_use_count, "
                    "e.score_history as earned_history, "
                    "COALESCE(s.slug, '') as slug "
                    "FROM atoms a "
                    "LEFT JOIN atom_earned e ON e.atom_id = a.id "
                    "LEFT JOIN atom_spine s ON s.atom_id = a.id "
                    "WHERE a.scope=?",
                    (scope,)).fetchall()
            else:
                rows = self.conn.execute(
                    "SELECT a.id, SUBSTR(a.content,1,120) as ck, a.score, "
                    "a.score_history, a.kind, a.born_from, "
                    "a.use_count, e.use_count as earned_use_count, "
                    "e.score_history as earned_history, "
                    "COALESCE(s.slug, '') as slug "
                    "FROM atoms a "
                    "LEFT JOIN atom_earned e ON e.atom_id = a.id "
                    "LEFT JOIN atom_spine s ON s.atom_id = a.id"
                    ).fetchall()
        for r in rows:
            if r["kind"] in self._VIEW_DECAY_EXEMPT:
                continue
            eff = self._effective_from_row(
                score=float(r["score"] or 100.0),
                score_history=r["score_history"],
                kind=r["kind"],
                born_from=r["born_from"] or "",
                use_count=r["use_count"] or 0,
                earned_use_count=r["earned_use_count"] or 0,
                earned_history_str=r["earned_history"],
                now=now,
                door_require_earned_use=False,
            )
            if eff < threshold:
                slug = r["slug"] or (r["ck"] or "")[:60]
                dormant_slugs.append({
                    "id": r["id"][:12],
                    "score": round(eff, 1),
                    "slug": slug,
                    "kind": r["kind"] or "",
                })
        return {"count": len(dormant_slugs), "dormant": dormant_slugs}
