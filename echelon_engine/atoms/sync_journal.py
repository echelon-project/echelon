"""sync_journal — the CHANGE-CAPTURE organ for two-way bank sync (wave 1, 2026-08-02).

THE DESIGN (gated: brainstorm council -> verdict; independent code-grounded skeptic ->
FAIL-WITH-AMENDMENTS, all folded in. See ECHELON/DELTA-SYNC-DESIGN.md):

  Two-way sync between the LOCAL bank (~/.echelon/echelon.db) and the CLOUD bank
  (box4 per-member db, served to claude.ai over MCP). Neither side may lose the other's
  earned weight, so the merge unit is the EVENT, not the score.

WHY TRIGGERS AND NOT A CODE CHOKE POINT
  Earned-state writers are scattered — cards.py `_witness_earn`/`fire_lower`/`_apply_delta`,
  hygiene.py's merge, store.py's kind flip. A code-level hook would have to be re-applied at
  every future write site (and would be forgotten once). SQLite AFTER triggers catch every
  path, including ones not written yet.

WHAT IS JOURNALED (skeptic amendment #4)
  SOURCE tables only: atoms, atom_earned, atom_links, cards, wrap_reviews.
  NEVER the compiled/derived surfaces (atom_spine / atom_body / atom_sidecar / *_fts) — a bulk
  recompile of 7.5k atoms would fire ~15k junk ops, and peers recompile locally anyway.
  EXCEPT: `atom_spine.witness` is AUTHORED provenance parsed from .md frontmatter, NOT derivable
  from atoms.content (cards.py:759-768 carries it across recompile) — so witness gets ONE narrow
  UPDATE-OF trigger. It is the single legitimate exception to "never journal compiled tables".
  EXCLUDED: `impressions` (routine bulk eviction = tombstone noise; it is telemetry, and its
  consumer — the view-decay pass — is single-runner by law anyway) and `session_offers`
  (cross-peer consume races; Law 3 `--consume` is inherently local).

THE MERGE LAW (skeptic amendment #1 — the council's original F2a was REFUTED)
  Decay in this engine is READ-TIME: effective_score recomputes B + sum(d*w)/sum(w) with
  w=e^(-lambda*age) from the FULL history at serve time (scoring.py:54-69). The stored `score`
  column is a fallback, near-vestigial. So replaying "new-old" onto `score` is the WRONG law —
  it would double-count against a history that is already authoritative.
  The right law: replay the HISTORY EVENTS (exactly-once by journal seq), add use_count, and let
  read-time recompute derive the score. That is commutative and needs no clock.
  BOTH histories travel: atoms.score_history AND atom_earned.score_history (effective score is
  the max of the two paths, cards.py:988-1038).
  NEVER content-dedup history entries — the live bank legitimately holds byte-identical rows
  (e.g. [1781821560, 2.0, 'fetch'] twice). Exactly-once is a SEQ property, never a content one.

ECHO SUPPRESSION (skeptic amendment #3 — mandatory; the design FAILED without it)
  Applying a remote batch fires these same triggers, so the op would bounce back to the peer
  forever. Every trigger carries
      WHEN (SELECT COALESCE(MAX(v),0) FROM temp._sync_applying) = 0
  and the applier sets that TEMP table inside its apply transaction. TEMP is connection-local,
  so a concurrent CLI or mcp_server writer on another connection is never muted. The trigger
  references temp._sync_applying unqualified as _sync_applying; we create the temp table on
  EVERY connection (see attach()) so the subquery always resolves.

GENERATION GUARD (skeptic amendment #5)
  `backup --restore` and vault heals are whole-file shutil.copy2 — trigger-INVISIBLE, and they
  rewind seq under a peer's cursor (a cursor would then point into rewritten history and silently
  skip ops). sync_meta carries a generation UUID minted at journal creation; any restore/heal
  re-mints it. Peers exchange generation before cursors; a mismatch forces a full re-baseline,
  never a cursor replay.

WAVE 1 IS LOCAL-ONLY and OFF BY DEFAULT: set ECHELON_SYNC=1 to attach the journal. Nothing
ships anywhere yet — wave 2 is the `echelon sync` client verb, wave 3 the gateway routes.

THE JOURNAL DIET (2026-08-27, measured on the live bank: sync_journal was 314MB/465MB = 68%,
102,410 UPDATE rows = 294MB, 93,736 of them a pure weight tick on atoms journaling the FULL
row — content ~2.5KB mean + ever-growing score_history, hub max 13.5KB — on every score tick.
TICK_COLUMNS tables (atoms, atom_earned) split their UPDATE trigger: `_tick` carries just the
PK + weight columns (score/score_history/use_count[/last_fetch_ts]), `_upd` still carries the
full row but only fires when a NON-tick column (kind/scope on atoms; scope on atom_earned)
actually changed. `content` is NOT a watched column on either table and never was (verified
against 14306d6^, pre-diet) — a content-only edit journals nothing, and content+score together
still journal a TICK, content absent. This is not a diet-introduced gap: atoms are
content-addressed by id, so an edit to `content` mints a new atom id and travels through the
journal as an INSERT on the new row, not an UPDATE on the old one — the old id's content is
immutable in practice, so the watched-column list never needed to include it. v1 STILL sends
the FULL score_history on every tick (dropping content already cuts ~70% of the bytes); a v2
candidate — sending only the TAIL entries appended since the last tick — needs an efficient
"what's new" expression that a trigger body cannot cheaply compute (no way to diff against the
last-sent state from inside SQLite), so it is deferred to the apply/push layer, not this
module. See `echelon sync --gc` (sync_cmd.py) for the other half of the diet: prune-on-ack so
acked journal rows do not accumulate forever.
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
import uuid
from pathlib import Path

# ── what the journal watches ──────────────────────────────────────────────────
# (table, [mutable columns worth an UPDATE trigger]) — INSERT/DELETE are always watched.
# Column lists keep UPDATE triggers narrow: an UPDATE OF (a,b) never fires for an unrelated
# column, which is what keeps recall's hot path free of journal writes.
SOURCE_TABLES: dict[str, list[str]] = {
    "atoms":        ["score", "score_history", "use_count", "kind", "scope"],
    "atom_earned":  ["score", "score_history", "use_count", "last_fetch_ts", "scope"],
    "atom_links":   ["superseded_on"],
    "cards":        ["score", "score_history", "use_count", "prev", "kind"],
    "wrap_reviews": ["verdict", "reason"],
}

# THE JOURNAL DIET (2026-08-27, measured: sync_journal was 68% of a 465MB bank — 93,736 of
# 102,410 UPDATE rows were pure weight ticks on atoms, journaling the FULL row — content
# (~2.5KB mean) + score_history (hub max 13.5KB) — every time score/score_history/use_count
# moved a hair. A tick never changes content/kind/scope; there is no reason to re-carry them.
#
# Tables listed here get a SECOND, narrower trigger (`trg_sync_{table}_tick`) that fires ONLY
# when every mutable column outside this list is unchanged (guarded via NEW.<col> IS OLD.<col>
# on the OTHER mutable columns) — i.e. a pure weight tick. Its payload carries just the PK +
# these columns, no content. A real edit (content/kind/scope also changed) still falls through
# to the full `_upd` trigger, which fires on ANY of the table's mutable columns changing — SQLite
# does not let one row fire two triggers off ONE compound condition partition cleanly, so `_upd`
# stays exactly as before (any mutable col) and `_tick` ADDS the narrow op; apply_ops treats
# TICK as authoritative for its own columns, so a tick right after a full UPDATE for the same
# write (impossible: they're mutually exclusive by the WHEN guard) is not a concern.
TICK_COLUMNS: dict[str, list[str]] = {
    "atoms":        ["score", "score_history", "use_count"],
    "atom_earned":  ["score", "score_history", "use_count", "last_fetch_ts"],
}

# The FULL column list an INSERT op must carry. An UPDATE payload can be narrow (only the
# mutable columns changed), but an INSERT has to reconstruct the whole row on the peer —
# these tables have NOT NULL columns (atoms.coordinate/content/ts) with no default, and an
# `INSERT OR IGNORE` missing them fails the constraint and SILENTLY writes nothing.
# (Caught by the suite, 2026-08-02: rows vanished on apply with no error.)
INSERT_COLUMNS: dict[str, list[str]] = {
    "atoms":        ["id", "coordinate", "content", "score", "score_history", "use_count",
                     "born_from", "ts", "scope", "kind", "valence", "arousal"],
    "atom_earned":  ["atom_id", "score", "use_count", "score_history", "last_fetch_ts", "scope"],
    "atom_links":   ["from_id", "to_id", "relation", "ts", "superseded_on"],
    "cards":        ["id", "label", "refs", "score", "score_history", "use_count",
                     "born_from", "prev", "ts", "kind"],
    "wrap_reviews": ["atom_id", "session_window_start", "verdict", "reason", "ts"],
}

# Primary-key columns per table — the journal records the PK as JSON so a composite key
# (atom_links, wrap_reviews) round-trips as faithfully as a single-column one.
PK_COLUMNS: dict[str, list[str]] = {
    "atoms":        ["id"],
    "atom_earned":  ["atom_id"],
    "atom_links":   ["from_id", "to_id", "relation"],
    "cards":        ["id"],
    "wrap_reviews": ["atom_id", "session_window_start"],
    "atom_spine":   ["atom_id"],
}

# DELIBERATELY EXCLUDED — documented so a future reader does not "fix" the omission:
#   impressions    : telemetry; bulk-evicted (cards.py:1801) -> tombstone noise. Its only
#                    consumer (view_decay_pass) is single-runner by law.
#   session_offers : Law 3 --consume is a local race; syncing it double-consumes offers.
#   atom_spine/body/sidecar, *_fts : compiled, deterministic from atoms.content -> peers
#                    recompile. (witness is the one authored field; see WITNESS_TRIGGER.)
EXCLUDED_TABLES = ("impressions", "session_offers", "atom_spine_fts",
                   "atom_body", "atom_sidecar")

_JOURNAL_SCHEMA = """
CREATE TABLE IF NOT EXISTS sync_journal (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    tbl TEXT NOT NULL,
    pk TEXT NOT NULL,
    op TEXT NOT NULL,
    payload TEXT NOT NULL DEFAULT '{}',
    scope TEXT NOT NULL DEFAULT '',
    ts INTEGER NOT NULL,
    origin TEXT NOT NULL DEFAULT '');
CREATE INDEX IF NOT EXISTS idx_sync_journal_scope ON sync_journal(scope, seq);
CREATE INDEX IF NOT EXISTS idx_sync_journal_tbl ON sync_journal(tbl, seq);
CREATE TABLE IF NOT EXISTS sync_meta (
    k TEXT PRIMARY KEY,
    v TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS sync_peers (
    peer TEXT PRIMARY KEY,
    peer_generation TEXT NOT NULL DEFAULT '',
    pulled_through INTEGER NOT NULL DEFAULT 0,
    pushed_through INTEGER NOT NULL DEFAULT 0,
    last_sync_ts INTEGER NOT NULL DEFAULT 0);
"""

# ── THE APPLIER'S MUTE SWITCH ────────────────────────────────────────────────
# The design (and the skeptic gate) prescribed a TEMP TABLE sentinel read by the trigger's
# WHEN clause. That is IMPOSSIBLE in SQLite: a trigger body may not reference the temp schema
# at all — "trigger trg_sync_atoms_ins cannot reference objects in database temp" — and an
# unqualified name inside a trigger resolves against `main`, not temp. Both variants were tried
# and both fail at CREATE TRIGGER / first write. (Caught by the suite, 2026-08-02.)
#
# The working mechanism: a connection-local PRAGMA the trigger reads through a pragma
# table-valued function. `cache_size` is per-connection, freely writable, semantically harmless
# at this magnitude, and readable from a trigger body. Setting it to the sentinel value mutes
# THIS connection's triggers only — verified: a concurrent connection keeps journaling while
# the applier is muted, and the mute reverses cleanly.
_MUTE_MAGIC = -99000001          # a cache_size no sane config would set (~96GB of page cache)
_MUTE_NORMAL = -2000             # SQLite's default (2MB), restored on exit
_MUTE = f"(SELECT cache_size FROM pragma_cache_size()) <> {_MUTE_MAGIC}"

# Kept for callers that want the (now vestigial) temp table; harmless and cheap.
_SENTINEL_SCHEMA = "CREATE TEMP TABLE IF NOT EXISTS _sync_applying (v INTEGER NOT NULL)"


def _json_obj(pairs: list[tuple[str, str]], row: str = "NEW") -> str:
    """SQL fragment building a json object of {col: NEW.col} for the payload."""
    inner = ", ".join(f"'{c}', {row}.{c}" for _, c in pairs)
    return f"json_object({inner})"


def _pk_json(table: str, row: str = "NEW") -> str:
    cols = PK_COLUMNS[table]
    inner = ", ".join(f"'{c}', {row}.{c}" for c in cols)
    return f"json_object({inner})"


def _scope_expr(table: str, row: str = "NEW") -> str:
    """Where the row's scope comes from. atoms/atom_earned carry it natively; cards and
    atom_links do not, so they resolve through the atom they reference. A row whose scope
    cannot be resolved journals as '' and is handled by the scope-disposition sweep — never
    silently pushed (the gateway filters by the token's scope, skeptic amendment #6)."""
    if table in ("atoms", "atom_earned"):
        return f"COALESCE({row}.scope,'')"
    if table == "atom_links":
        return f"COALESCE((SELECT a.scope FROM atoms a WHERE a.id={row}.from_id),'')"
    if table == "wrap_reviews":
        return f"COALESCE((SELECT a.scope FROM atoms a WHERE a.id={row}.atom_id),'')"
    if table == "atom_spine":
        return f"COALESCE({row}.scope,'')"
    # cards reference atoms by refs JSON — no cheap SQL resolution; '' is honest here.
    return "''"


def _unchanged_guard(cols: list[str]) -> str:
    """SQL fragment: TRUE iff none of `cols` changed on this row (NEW IS OLD, NULL-safe via
    SQLite's IS). Empty list -> '1' (vacuously true)."""
    if not cols:
        return "1"
    return " AND ".join(f"NEW.{c} IS OLD.{c}" for c in cols)


def _trigger_sql(table: str, cols: list[str]) -> list[str]:
    """The triggers for one source table. Every one carries the mute clause.

    Tables in TICK_COLUMNS split their UPDATE trigger in two, mutually exclusive by WHEN guard:
      - `_tick`: fires on UPDATE OF the tick columns (score/score_history/use_count/...) WHEN
        every OTHER mutable column is unchanged — a pure weight tick. Payload carries the PK +
        tick columns only; NO content, NO kind, NO scope. This is the journal-diet trigger (the
        95% win: 93,736 of 102,410 UPDATE rows on `atoms` were exactly this shape).
      - `_upd`: fires on UPDATE OF ALL mutable columns (unchanged from before the diet) WHEN at
        least one NON-tick column actually changed — a real kind/scope edit still carries the
        full payload, same as today. `content` is not a mutable/watched column on either
        TICK_COLUMNS table, so it never reaches this trigger's WHEN at all: a content-only edit
        journals nothing, and content+score together still take the `_tick` branch with content
        absent (pre-existing, unchanged by this diet — atoms are content-addressed by id, so a
        content edit mints a new id and travels as an INSERT, not an UPDATE). Because `_tick`'s
        WHEN requires the non-tick columns unchanged and `_upd`'s WHEN requires at least one
        changed, the two never both fire for the same UPDATE statement.
    Tables NOT in TICK_COLUMNS are unaffected: one `_upd` trigger, exactly as before.
    """
    payload_cols = [(c, c) for c in cols]
    insert_cols = [(c, c) for c in INSERT_COLUMNS[table]]
    tick_cols = TICK_COLUMNS.get(table, [])
    other_cols = [c for c in cols if c not in tick_cols]
    stmts = []
    # INSERT — carries the FULL row (NOT NULL columns have no defaults; a narrow payload makes
    # the peer's INSERT OR IGNORE fail silently). Replay is idempotent by content-addressed id.
    stmts.append(f"""
CREATE TRIGGER IF NOT EXISTS trg_sync_{table}_ins AFTER INSERT ON {table}
WHEN {_MUTE}
BEGIN
    INSERT INTO sync_journal (tbl, pk, op, payload, scope, ts, origin)
    VALUES ('{table}', {_pk_json(table)}, 'INSERT', {_json_obj(insert_cols)},
            {_scope_expr(table)}, CAST(strftime('%s','now') AS INTEGER), '');
END;""")
    if tick_cols:
        # TICK — the delta trigger. Fires only when the row's non-tick mutable columns are all
        # unchanged (a pure weight tick never touches content/kind/scope).
        tick_of = ", ".join(tick_cols)
        tick_payload_cols = [(c, c) for c in tick_cols]
        stmts.append(f"""
CREATE TRIGGER IF NOT EXISTS trg_sync_{table}_tick AFTER UPDATE OF {tick_of} ON {table}
WHEN {_MUTE} AND ({_unchanged_guard(other_cols)})
BEGIN
    INSERT INTO sync_journal (tbl, pk, op, payload, scope, ts, origin)
    VALUES ('{table}', {_pk_json(table)}, 'TICK', {_json_obj(tick_payload_cols)},
            {_scope_expr(table)}, CAST(strftime('%s','now') AS INTEGER), '');
END;""")
        # UPDATE — the full-payload trigger, now guarded to skip pure ticks (they went to
        # `_tick` above). Fires on ANY mutable column (unchanged trigger surface) but only when
        # at least one NON-tick column actually changed.
        if cols:
            of = ", ".join(cols)
            stmts.append(f"""
CREATE TRIGGER IF NOT EXISTS trg_sync_{table}_upd AFTER UPDATE OF {of} ON {table}
WHEN {_MUTE} AND NOT ({_unchanged_guard(other_cols)})
BEGIN
    INSERT INTO sync_journal (tbl, pk, op, payload, scope, ts, origin)
    VALUES ('{table}', {_pk_json(table)}, 'UPDATE', {_json_obj(payload_cols)},
            {_scope_expr(table)}, CAST(strftime('%s','now') AS INTEGER), '');
END;""")
    elif cols:
        # UPDATE — narrow (OF the mutable columns only), so unrelated writes never journal.
        # No tick split for this table: full payload every time, as before the diet.
        of = ", ".join(cols)
        stmts.append(f"""
CREATE TRIGGER IF NOT EXISTS trg_sync_{table}_upd AFTER UPDATE OF {of} ON {table}
WHEN {_MUTE}
BEGIN
    INSERT INTO sync_journal (tbl, pk, op, payload, scope, ts, origin)
    VALUES ('{table}', {_pk_json(table)}, 'UPDATE', {_json_obj(payload_cols)},
            {_scope_expr(table)}, CAST(strftime('%s','now') AS INTEGER), '');
END;""")
    # DELETE — NOT JOURNALED (no-delete law, owner 2026-08-18).
    #
    # The original design emitted an explicit tombstone here ("never infer deletion from
    # absence"). That is the right law for a replicated store whose peers trust each other
    # completely; it is the wrong law for THIS bank. A tombstone is a destructive instruction
    # that outlives the moment of judgement, and the applier on the far side cannot tell a
    # deliberate retirement from a `rm` fired by mistake.
    #
    # Sync is now APPEND-ONLY IN BOTH DIRECTIONS: we do not emit tombstones, and we refuse
    # inbound ones (see apply_ops). A local delete stays local. Retirement that SHOULD travel
    # is expressed as an UPDATE — disclaim the atom, flip its kind — which is auditable,
    # reversible, and carries its reason.
    #
    # The trigger is not merely omitted: any tombstone left in an existing journal from before
    # this law is inert, because the applier refuses the op regardless of who emitted it.
    return stmts


# The ONE authored field on a compiled table (see module docstring). Narrow UPDATE-OF so a
# recompile — which REPLACEs the whole spine row — does not fire it as an UPDATE at all.
_WITNESS_TRIGGER = f"""
CREATE TRIGGER IF NOT EXISTS trg_sync_spine_witness AFTER UPDATE OF witness ON atom_spine
WHEN {_MUTE} AND COALESCE(NEW.witness,'') <> COALESCE(OLD.witness,'')
BEGIN
    INSERT INTO sync_journal (tbl, pk, op, payload, scope, ts, origin)
    VALUES ('atom_spine', json_object('atom_id', NEW.atom_id), 'WITNESS',
            json_object('witness', NEW.witness),
            COALESCE(NEW.scope,''), CAST(strftime('%s','now') AS INTEGER), '');
END;"""


def enabled() -> bool:
    """Wave 1 ships OFF. ECHELON_SYNC=1 attaches the journal."""
    return os.environ.get("ECHELON_SYNC", "").strip() not in ("", "0", "false", "no")


# ── attach / detach ───────────────────────────────────────────────────────────

def attach(conn: sqlite3.Connection, *, force: bool = False) -> bool:
    """Idempotently install the journal schema + triggers on an open connection. Safe to call
    on every CardStore construction.

    Returns True if the journal is active on this connection; no-op (False) unless
    ECHELON_SYNC=1 or force=True. Note the triggers live in the FILE, so once installed they
    fire for every process opening this bank — the mute is a per-connection pragma (see _Muted)
    and needs no setup on connections that never apply a remote batch."""
    if not (force or enabled()):
        return False
    conn.executescript(_JOURNAL_SCHEMA)
    # Only watch tables this bank actually HAS. An older bank predates some of them
    # (box4's bank has no wrap_reviews) and CREATE TRIGGER on a missing table raises
    # "no such table", aborting attach() half-way with triggers partially installed.
    # Caught 2026-08-02 testing against a copy of the live cloud bank before deploying.
    present = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    # NO-DELETE LAW retro-fit: triggers live in the FILE, and attach() uses CREATE TRIGGER IF
    # NOT EXISTS — which cannot REMOVE a trigger installed under the old design. A bank attached
    # before 2026-08-18 still carries trg_sync_*_del and would keep emitting tombstones for
    # every local delete. Drop them here so re-attaching is a real migration, not a no-op.
    for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger' "
            "AND name LIKE 'trg_sync_%_del'").fetchall():
        conn.execute(f"DROP TRIGGER IF EXISTS {r[0]}")
    # JOURNAL-DIET MIGRATION (2026-08-27): a bank attached before the tick split carries the
    # OLD fat `_upd` trigger (UPDATE OF all mutable cols, no non-tick WHEN guard) for every
    # table now in TICK_COLUMNS. CREATE TRIGGER IF NOT EXISTS cannot replace a trigger's BODY,
    # so re-attaching would silently keep journaling full payloads forever. Drop the old `_upd`
    # (and any stale `_tick`, in case a partial migration was interrupted) before recreating —
    # same law as the `_del` migration above: re-attach is a real migration, not a no-op.
    for table in TICK_COLUMNS:
        conn.execute(f"DROP TRIGGER IF EXISTS trg_sync_{table}_upd")
        conn.execute(f"DROP TRIGGER IF EXISTS trg_sync_{table}_tick")
    for table, cols in SOURCE_TABLES.items():
        if table not in present:
            continue
        for stmt in _trigger_sql(table, cols):
            conn.execute(stmt)
    if "atom_spine" in present:
        conn.execute(_WITNESS_TRIGGER)
    _ensure_generation(conn)
    conn.commit()
    return True


def detach(conn: sqlite3.Connection) -> int:
    """Drop every sync trigger (the journal TABLE and its history are kept — dropping data is
    never a rollback path). Returns how many triggers were dropped."""
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='trigger' AND name LIKE 'trg_sync_%'"
    ).fetchall()
    for r in rows:
        conn.execute(f"DROP TRIGGER IF EXISTS {r[0]}")
    conn.commit()
    return len(rows)


def installed_triggers(conn: sqlite3.Connection) -> list[str]:
    return [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='trigger' AND name LIKE 'trg_sync_%' "
        "ORDER BY name").fetchall()]


# ── generation (the restore guard) ────────────────────────────────────────────

def _ensure_generation(conn: sqlite3.Connection) -> str:
    r = conn.execute("SELECT v FROM sync_meta WHERE k='generation'").fetchone()
    if r:
        return r[0]
    gen = uuid.uuid4().hex
    conn.execute("INSERT OR REPLACE INTO sync_meta (k,v) VALUES ('generation',?)", (gen,))
    conn.execute("INSERT OR REPLACE INTO sync_meta (k,v) VALUES ('generation_ts',?)",
                 (str(int(time.time())),))
    return gen


def generation(conn: sqlite3.Connection) -> str:
    """This bank's journal generation. Peers compare this BEFORE cursors; a mismatch means the
    journal was rewound underneath them (restore / vault heal / fresh bank) and the only safe
    move is a full re-baseline."""
    try:
        r = conn.execute("SELECT v FROM sync_meta WHERE k='generation'").fetchone()
        return r[0] if r else ""
    except sqlite3.OperationalError:
        return ""


def remint_generation(db_path: Path | str) -> str:
    """Re-mint after a whole-file restore/heal. Those paths are shutil.copy2 — trigger-invisible
    (the journal never sees them), so the generation stamp is the ONLY detector. Called by
    backup --restore."""
    conn = sqlite3.connect(str(db_path), timeout=10)
    try:
        conn.executescript(_JOURNAL_SCHEMA)
        gen = uuid.uuid4().hex
        conn.execute("INSERT OR REPLACE INTO sync_meta (k,v) VALUES ('generation',?)", (gen,))
        conn.execute("INSERT OR REPLACE INTO sync_meta (k,v) VALUES ('generation_ts',?)",
                     (str(int(time.time())),))
        # A restored file carries the SOURCE bank's peer cursors, which are meaningless here.
        conn.execute("DELETE FROM sync_peers")
        conn.commit()
        return gen
    finally:
        conn.close()


# ── reading the journal (the PUSH side of wave 2) ─────────────────────────────

def ops_since(conn: sqlite3.Connection, since: int = 0, *, scope: str = "",
              limit: int = 5000) -> list[dict]:
    """Journal ops after `since`, oldest first. Scope-filtered when asked — the gateway enforces
    this server-side from the token's scope (a local bank holds 100+ scopes including other
    clients' estates; an unfiltered push would leak them)."""
    sql = "SELECT seq, tbl, pk, op, payload, scope, ts, origin FROM sync_journal WHERE seq > ?"
    args: list = [since]
    if scope:
        sql += " AND scope = ?"
        args.append(scope)
    sql += " ORDER BY seq LIMIT ?"
    args.append(limit)
    return [dict(zip(("seq", "tbl", "pk", "op", "payload", "scope", "ts", "origin"), r))
            for r in conn.execute(sql, args).fetchall()]


def head(conn: sqlite3.Connection) -> int:
    r = conn.execute("SELECT COALESCE(MAX(seq),0) FROM sync_journal").fetchone()
    return int(r[0]) if r else 0


def stats(conn: sqlite3.Connection) -> dict:
    try:
        total = conn.execute("SELECT COUNT(*) FROM sync_journal").fetchone()[0]
    except sqlite3.OperationalError:
        return {"attached": False}
    by_tbl = {r[0]: r[1] for r in conn.execute(
        "SELECT tbl, COUNT(*) FROM sync_journal GROUP BY tbl ORDER BY 2 DESC").fetchall()}
    by_op = {r[0]: r[1] for r in conn.execute(
        "SELECT op, COUNT(*) FROM sync_journal GROUP BY op").fetchall()}
    scopes = {r[0] or "(none)": r[1] for r in conn.execute(
        "SELECT scope, COUNT(*) FROM sync_journal GROUP BY scope ORDER BY 2 DESC LIMIT 12"
    ).fetchall()}
    return {"attached": True, "generation": generation(conn), "head": head(conn),
            "total": total, "by_table": by_tbl, "by_op": by_op, "by_scope": scopes,
            "triggers": len(installed_triggers(conn))}


# ── applying a remote batch (the PULL side of wave 2) ─────────────────────────

class _Muted:
    """Mute the journal for the applier's own writes. Without this, applying a peer's batch
    fires the local triggers and every op bounces back to the peer forever.

    Mechanism: set this CONNECTION's cache_size to the sentinel magic; every trigger's WHEN
    clause reads it via pragma_cache_size() and declines to fire. Connection-local by
    definition, so a concurrent CLI/mcp_server writer on another connection is never muted.
    Restores the prior value on exit — including when the batch raises."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        self._prior = _MUTE_NORMAL

    def __enter__(self):
        try:
            r = self.conn.execute("SELECT cache_size FROM pragma_cache_size()").fetchone()
            if r and int(r[0]) != _MUTE_MAGIC:
                self._prior = int(r[0])
        except Exception:
            pass
        self.conn.execute(f"PRAGMA cache_size = {_MUTE_MAGIC}")
        return self.conn

    def __exit__(self, *exc):
        self.conn.execute(f"PRAGMA cache_size = {self._prior}")
        return False


def _entry_origin(e: list) -> str:
    """The 4th element of a score_history entry: which BANK witnessed it.

    Entries are [ts, delta] (legacy) or [ts, delta, src] (provenance-tagged) — compute_score
    splat-unpacks `for ts, delta, *_` (scoring.py:65) and is deliberately source-blind, so a
    4th element rides along harmlessly. '' means 'this bank' (every pre-sync entry)."""
    return e[3] if len(e) > 3 else ""


def _stamp_origin(entries: list, origin: str) -> list:
    """Stamp un-originated entries with the bank they came from, so a peer can tell 'the same
    event, synced twice' (drop) from 'two banks earned identically in the same second' (keep
    both). Without this the two are indistinguishable — the live bank legitimately holds
    byte-identical entries, so content-dedup would erase real earned weight while
    count-bucketing would absorb a peer's genuine earns. Origin is the only honest tiebreak."""
    out = []
    for e in entries:
        e = list(e)
        while len(e) < 3:
            e.append(0 if len(e) < 2 else "")
        if len(e) < 4 or not e[3]:
            e = e[:3] + [origin]
        out.append(e)
    return out


def _merge_history(local_raw: str, remote_raw: str, *,
                   local_origin: str = "", remote_origin: str = "remote") -> str:
    """Union two score_history lists, keyed by ORIGIN so neither bank's earned weight is lost
    and a re-applied batch does not double-count.

    NEVER content-dedup: the live bank holds legitimately byte-identical entries (two 'fetch'
    events in the same second), so dropping by value erases real weight. Instead each entry
    carries its witnessing bank (see _stamp_origin); an entry is new iff this bank has not
    already recorded that (ts, delta, src, origin) as many times as the peer has."""
    try:
        local = json.loads(local_raw or "[]")
    except Exception:
        local = []
    try:
        remote = json.loads(remote_raw or "[]")
    except Exception:
        remote = []
    local = _stamp_origin(local, local_origin)
    remote = _stamp_origin(remote, remote_origin)
    if not remote:
        return json.dumps(local)
    if not local:
        return json.dumps(remote)

    def key(e):
        return (e[0], round(float(e[1]), 6), e[2], _entry_origin(e))

    counts: dict[tuple, int] = {}
    for e in local:
        counts[key(e)] = counts.get(key(e), 0) + 1

    merged = list(local)
    seen: dict[tuple, int] = {}
    for e in remote:
        k = key(e)
        seen[k] = seen.get(k, 0) + 1
        if seen[k] > counts.get(k, 0):
            merged.append(e)
    merged.sort(key=lambda e: e[0] if e else 0)
    return json.dumps(merged)


def _pk_where(table: str, pk: dict) -> tuple[str, list]:
    cols = PK_COLUMNS[table]
    return " AND ".join(f"{c}=?" for c in cols), [pk.get(c) for c in cols]


def apply_ops(conn: sqlite3.Connection, ops: list[dict], *, origin: str = "remote") -> dict:
    """Apply a peer's journal batch under the merge law. ALL-OR-NOTHING: the caller acks its
    cursor only after this returns, so a crash mid-batch re-sends and re-applies idempotently.

    The law (skeptic amendment #1): earned state merges as HISTORY EVENTS + use_count, never as
    a replayed score delta — read-time recompute derives the score from the merged history.
    Inserts are OR IGNORE (content-addressed ids). Deletes are explicit tombstones."""
    applied = {"insert": 0, "update": 0, "tick": 0, "delete": 0, "witness": 0, "skipped": 0}
    # This bank's identity, stamped onto its own un-originated history entries so a peer's
    # identical-looking earns stay distinguishable from ours (see _merge_history).
    local_gen = generation(conn) or "local"
    with _Muted(conn):
        conn.execute("BEGIN IMMEDIATE")
        try:
            for op in ops:
                tbl, kind = op["tbl"], op["op"]
                if tbl not in PK_COLUMNS or (tbl not in SOURCE_TABLES and tbl != "atom_spine"):
                    applied["skipped"] += 1
                    continue
                pk = json.loads(op["pk"] or "{}")
                payload = json.loads(op.get("payload") or "{}")

                if kind == "WITNESS":
                    conn.execute("UPDATE atom_spine SET witness=? WHERE atom_id=?",
                                 (payload.get("witness", ""), pk.get("atom_id")))
                    applied["witness"] += 1
                    continue

                where, args = _pk_where(tbl, pk)
                if kind == "DELETE":
                    # NO-DELETE LAW (owner, 2026-08-18): sync NEVER destroys local rows.
                    # A DELETE arriving from a peer is REFUSED, counted, and recorded — it is
                    # not applied and never will be. The bank is append-only across the wire.
                    #
                    # Why a refusal and not a tombstone-apply: a peer's DELETE is
                    # indistinguishable from a peer's ACCIDENT. The 2026-08-18 wipe destroyed
                    # 8,479 atoms locally in one command; had that bank been the pushing side
                    # with deletes enabled, the same command would have propagated. Recovery
                    # worked because atoms survive as FILES and weight as TRANSCRIPTS — neither
                    # of which helps if a peer deletes rows we still hold.
                    # Deliberate local retirement stays a LOCAL act (disclaim / retire an atom,
                    # then let the resulting UPDATE travel), which is auditable and reversible.
                    applied["delete_refused"] = applied.get("delete_refused", 0) + 1
                    refused = applied.setdefault("refused_pks", [])
                    if len(refused) < 50:          # bounded: a receipt, not a second journal
                        refused.append(f"{tbl}:{json.dumps(pk, sort_keys=True)}")
                    continue

                row = conn.execute(f"SELECT * FROM {tbl} WHERE {where}", args).fetchone()
                if row is None:
                    # The atom itself arrives as its own INSERT op; a dependent row (earned,
                    # link) whose parent has not landed yet is written anyway — append-only
                    # spirit: an edge to a not-yet-planted atom is legal (cards.py:397-403).
                    merged = dict(pk)
                    merged.update({c: v for c, v in payload.items() if c not in pk})
                    missing = [c for c in INSERT_COLUMNS.get(tbl, [])
                               if c not in merged]
                    if missing:
                        # A narrow UPDATE payload for a row we have never seen cannot be
                        # materialized (NOT NULL columns with no default). Skip it loudly
                        # rather than writing a corrupt half-row — the row's own INSERT op
                        # is earlier in the peer's journal and will arrive on a full resync.
                        applied["skipped"] += 1
                        applied.setdefault("incomplete", []).append(f"{tbl}:{missing}")
                        continue
                    cols = list(merged.keys())
                    placeholders = ",".join("?" * len(cols))
                    conn.execute(
                        f"INSERT OR IGNORE INTO {tbl} ({','.join(cols)}) VALUES ({placeholders})",
                        [merged[c] for c in cols])
                    applied["insert"] += 1
                    continue

                keys = row.keys() if hasattr(row, "keys") else []
                sets, vals = [], []
                # score_history FIRST — use_count is derived from the merged result, so the
                # dict's insertion order must not decide whether that derivation has run.
                ordered = sorted(payload.items(), key=lambda kv: kv[0] != "score_history")
                for col, remote_val in ordered:
                    if col in pk or col not in keys:
                        continue
                    local_val = row[col]
                    if col == "score_history":
                        merged_hist = _merge_history(
                            local_val, remote_val,
                            local_origin=local_gen, remote_origin=origin)
                        sets.append(f"{col}=?")
                        vals.append(merged_hist)
                        # use_count is DERIVED from the merged history, not from either peer's
                        # counter: counting 'fetch' events is the only reading that survives
                        # both banks earning between syncs. max() would discard one side's
                        # uses; a blind sum would double-count on a re-applied batch.
                        if "use_count" in keys:
                            fetches = sum(1 for e in json.loads(merged_hist)
                                          if len(e) > 2 and str(e[2]) == "fetch")
                            if fetches:
                                sets.append("use_count=?")
                                vals.append(fetches)
                    elif col == "use_count":
                        continue   # handled with score_history above (derived, not merged)
                    elif col == "score":
                        # Stored score is a FALLBACK (effective score recomputes from history).
                        # Keep the higher of the two so a peer that never recomputed does not
                        # read a stale-low number; the history merge above is the real truth.
                        sets.append(f"{col}=?")
                        vals.append(max(float(local_val or 0.0), float(remote_val or 0.0)))
                    elif col in ("last_fetch_ts", "superseded_on"):
                        sets.append(f"{col}=?")
                        vals.append(max(int(local_val or 0), int(remote_val or 0)))
                    elif col == "scope" and (local_val or ""):
                        continue          # never blank a resolved scope from a peer
                    else:
                        if remote_val is not None and remote_val != local_val:
                            sets.append(f"{col}=?")
                            vals.append(remote_val)
                if sets:
                    conn.execute(f"UPDATE {tbl} SET {','.join(sets)} WHERE {where}", vals + args)
                    applied["tick" if kind == "TICK" else "update"] += 1
                else:
                    applied["skipped"] += 1
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    applied["origin"] = origin
    return applied


# ── peer cursors ──────────────────────────────────────────────────────────────

def peer_state(conn: sqlite3.Connection, peer: str) -> dict:
    r = conn.execute(
        "SELECT peer, peer_generation, pulled_through, pushed_through, last_sync_ts "
        "FROM sync_peers WHERE peer=?", (peer,)).fetchone()
    if not r:
        return {"peer": peer, "peer_generation": "", "pulled_through": 0,
                "pushed_through": 0, "last_sync_ts": 0}
    return dict(zip(("peer", "peer_generation", "pulled_through", "pushed_through",
                     "last_sync_ts"), r))


def set_peer_state(conn: sqlite3.Connection, peer: str, *, peer_generation: str | None = None,
                   pulled_through: int | None = None, pushed_through: int | None = None) -> dict:
    """Advance a peer cursor. Called ONLY after a batch fully applies (ack-after-apply)."""
    cur = peer_state(conn, peer)
    gen = peer_generation if peer_generation is not None else cur["peer_generation"]
    pull = pulled_through if pulled_through is not None else cur["pulled_through"]
    push = pushed_through if pushed_through is not None else cur["pushed_through"]
    conn.execute(
        "INSERT OR REPLACE INTO sync_peers (peer, peer_generation, pulled_through, "
        "pushed_through, last_sync_ts) VALUES (?,?,?,?,?)",
        (peer, gen, pull, push, int(time.time())))
    conn.commit()
    return peer_state(conn, peer)


def compact_journal(conn: sqlite3.Connection, *, keep_days: int = 30,
                    below_seq: int | None = None) -> int:
    """Drop journal ops older than keep_days that every known peer has already pulled through.
    Tombstones must outlive their propagation window or a delete resurrects; keep_days is that
    window. Returns rows removed."""
    cutoff_ts = int(time.time()) - keep_days * 86400
    if below_seq is None:
        r = conn.execute("SELECT MIN(pulled_through) FROM sync_peers").fetchone()
        below_seq = int(r[0]) if r and r[0] is not None else 0
    cur = conn.execute("DELETE FROM sync_journal WHERE seq <= ? AND ts < ?",
                       (below_seq, cutoff_ts))
    conn.commit()
    return cur.rowcount


# ── gc: prune-on-ack (the journal-diet's other half) ──────────────────────────
# compact_journal (above) is the PULL-side, time-windowed compaction (a tombstone must outlive
# its propagation window). gc() is the PUSH-side prune: once every peer has ACKED (pulled)
# our ops through some seq, there is no reason to keep them at all — a peer never re-requests
# what it already pulled, and a fresh peer re-baselines from seq 0 anyway (generation guard).
# "a burned runbook becomes a verb" — this was the owner's manual prune-after-push habit,
# promoted to `echelon sync gc` so it runs by default instead of by memory.

def gc_prunable(conn: sqlite3.Connection, *, retain: int = 5000) -> tuple[int, int]:
    """Returns (min_pushed_through, cutoff_seq) without deleting anything. cutoff_seq is the
    highest seq safe to prune (seq <= cutoff_seq), or -1 if gc cannot run (see gc()).

    NOTE: sync_peers.pushed_through is NOT NULL DEFAULT 0 — a peer row that has never received
    a push (no sync has happened yet) reads as 0, not SQL NULL. 0 is the "nothing proven
    delivered" value the spec means by NULL here, so it is treated the same way: gc refuses."""
    r = conn.execute("SELECT COUNT(*), MIN(pushed_through) FROM sync_peers").fetchone()
    n_peers, min_pushed = (r[0] or 0), r[1]
    if n_peers == 0 or min_pushed is None or int(min_pushed) <= 0:
        return (0, -1)
    return (int(min_pushed), int(min_pushed) - retain)


def gc(conn: sqlite3.Connection, *, retain: int = 5000, vacuum: bool = False) -> dict:
    """Prune sync_journal rows every known peer has already pulled (seq <= min(pushed_through)
    over sync_peers - retain). Refuses (raises ValueError) when sync_peers is empty or no peer
    has a proven pushed_through (never synced, reads 0) — there is nothing yet proven
    delivered, so pruning would risk a peer's own re-send window.

    Deletes rows a peer has PUSHED to (pushed_through = what WE have sent and the peer has
    ack'd receiving), never rows still needed for an as-yet-unpulled peer — retain is the
    safety margin below that floor. --vacuum reclaims the freed pages; never run automatically
    (a VACUUM rewrites the whole file and holds an exclusive lock)."""
    min_pushed, cutoff = gc_prunable(conn, retain=retain)
    if cutoff < 0:
        raise ValueError("no sync_peers row with pushed_through set — "
                          "nothing has been proven delivered yet")
    size_before = _db_byte_size(conn)
    cur = conn.execute("DELETE FROM sync_journal WHERE seq <= ?", (cutoff,))
    pruned = cur.rowcount
    conn.commit()
    if vacuum and pruned:
        conn.execute("VACUUM")
    size_after = _db_byte_size(conn)
    return {"pruned": pruned, "cutoff_seq": cutoff, "min_pushed_through": min_pushed,
            "retain": retain, "vacuumed": bool(vacuum and pruned),
            "size_before": size_before, "size_after": size_after}


def _db_byte_size(conn: sqlite3.Connection) -> int:
    try:
        r = conn.execute("PRAGMA page_count").fetchone()
        p = conn.execute("PRAGMA page_size").fetchone()
        if r and p:
            return int(r[0]) * int(p[0])
    except Exception:
        pass
    return 0


def journal_status(conn: sqlite3.Connection, *, retain: int = 5000) -> dict:
    """Doctor-visible journal health: rows, bytes, and the prunable count — so the swell is
    visible before it hurts (charter law: a runbook that has to be re-discovered is a defect)."""
    s = stats(conn)
    if not s.get("attached"):
        return s
    _, cutoff = gc_prunable(conn, retain=retain)
    prunable = 0
    if cutoff >= 0:
        r = conn.execute("SELECT COUNT(*) FROM sync_journal WHERE seq <= ?", (cutoff,)).fetchone()
        prunable = int(r[0]) if r else 0
    s["prunable"] = prunable
    s["db_bytes"] = _db_byte_size(conn)
    return s


# ── CLI ───────────────────────────────────────────────────────────────────────

def _open(db: str | None) -> sqlite3.Connection:
    from .cards import DEFAULT_V2_DB
    path = Path(db) if db else DEFAULT_V2_DB
    conn = sqlite3.connect(str(path), timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def _main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(
        prog="echelon sync-journal",
        description="Change-capture journal for two-way bank sync (wave 1: local only).")
    ap.add_argument("action", choices=["attach", "detach", "status", "ops", "compact"],
                    help="attach/detach triggers, show status, dump ops, compact old ops")
    ap.add_argument("--db", default=None, help="bank path (default: the live bank)")
    ap.add_argument("--since", type=int, default=0, help="ops: journal seq to read after")
    ap.add_argument("--scope", default="", help="ops: filter to one scope")
    ap.add_argument("--limit", type=int, default=50, help="ops: max rows")
    ap.add_argument("--keep-days", type=int, default=30, help="compact: tombstone retention")
    a = ap.parse_args(argv)

    conn = _open(a.db)
    try:
        if a.action == "attach":
            attach(conn, force=True)
            trigs = installed_triggers(conn)
            print(f"journal attached: {len(trigs)} triggers, generation {generation(conn)}")
            for t in trigs:
                print(f"  {t}")
            print("\nNOTE: triggers live in the FILE, so they now fire for EVERY process "
                  "writing this bank — no restart and no ECHELON_SYNC needed on readers. "
                  "The applier's echo-mute is a per-connection pragma (see _Muted).")
            return 0
        if a.action == "detach":
            n = detach(conn)
            print(f"dropped {n} trigger(s) — journal rows kept")
            return 0
        if a.action == "status":
            s = journal_status(conn)
            if not s.get("attached"):
                print("journal NOT attached (no sync_journal table). Run: sync-journal attach")
                return 0
            print(f"generation : {s['generation']}")
            print(f"head seq   : {s['head']}   ops: {s['total']}   triggers: {s['triggers']}   "
                  f"bytes: {s['db_bytes']:,}   prunable: {s['prunable']}")
            if s["by_op"]:
                print(f"by op      : {s['by_op']}")
            if s["by_table"]:
                print(f"by table   : {s['by_table']}")
            if s["by_scope"]:
                print("by scope   :")
                for k, v in s["by_scope"].items():
                    print(f"    {k:<28} {v}")
            peers = conn.execute("SELECT * FROM sync_peers").fetchall()
            if peers:
                print("peers      :")
                for p in peers:
                    print(f"    {p['peer']:<20} pulled={p['pulled_through']} "
                          f"pushed={p['pushed_through']} gen={p['peer_generation'][:8]}")
            return 0
        if a.action == "ops":
            rows = ops_since(conn, a.since, scope=a.scope, limit=a.limit)
            if not rows:
                print("(no ops)")
                return 0
            for r in rows:
                pk = json.loads(r["pk"] or "{}")
                pkstr = ",".join(str(v)[:12] for v in pk.values())
                print(f"  {r['seq']:>6}  {r['op']:<7} {r['tbl']:<14} {pkstr:<28} "
                      f"scope={r['scope'] or '-'}")
            return 0
        if a.action == "compact":
            n = compact_journal(conn, keep_days=a.keep_days)
            print(f"compacted {n} op(s) older than {a.keep_days}d and pulled by every peer")
            return 0
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
