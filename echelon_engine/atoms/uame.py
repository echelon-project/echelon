"""UAME — the corrected schema: two prefixes × COS-domain tables + links.

The owner's correction (2026-06-05): a single `seeds` table with a `tier` column is "not correct
in a sense". The canonical UAME (EROS, UAME_PLAN.md) is genuinely TWO tables — uame_core
(permanent, immutable soul) and uame_working (temporary, candidate) — because the separation IS
the safety: the soul physically cannot be touched by working-memory ops, expire() can only ever
DELETE FROM working, promotion is an atomic cross-guarantee move. A flag-column wall is structurally
weak; a table wall is not.

The owner's expansion beyond canonical: "two prefix, multiple table — now we can expand a lot
further." So the schema is built on BOTH foundations:

  PREFIX  (UAME guarantee axis):  core_*  = permanent/immutable soul · working_*  = temporary/candidate
  TABLE   (COS domain axis):      the table is the COS coordinate's TOP level — core_tooling /
                                  working_tooling, core_workflow / working_workflow, core_identity ...
  COLUMN  coordinate:             the deeper COS sub-path (topic:specific:atom) within the domain.
  LINKS   uame_links:             reasoning chains / causality / [[name]] refs, spanning everything.

So a memory's address has three orthogonal parts:
  - HOW PERMANENT  -> the prefix (core vs working)         [UAME]
  - WHAT DOMAIN    -> the table  (tooling, workflow, ...)  [COS top level]
  - WHERE EXACTLY  -> the coordinate column (sub-path)     [COS deeper]
New domain = new table pair. The schema grows by ADDING domains, not bloating one table.

Content-addressed (id=sha256[:16]), append-only, WAL+retry — UAME's proven spine kept. The COS
scoring fields (score/score_history/recall_count) ride on every row so grow/dream/synthesize work
across the families. See memory: UAME_PLAN two-table, COSys_DESIGN Layers 1-2, core-values-grow.
"""
from __future__ import annotations
import hashlib
import json
import math
import os
import re
import sqlite3
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from .echelon_home import home_db as _home_db
DEFAULT_DB = _home_db("core.db")

# The column shape every domain table shares (core_* and working_* are identical in structure;
# they differ only in GUARANTEE — what operations are allowed to touch them).
_ROW_COLS = """
    id           TEXT NOT NULL,
    content      TEXT NOT NULL,
    kind         TEXT NOT NULL DEFAULT 'atom',
    coordinate   TEXT NOT NULL DEFAULT '',      -- COS sub-path within this domain (topic:specific:atom)
    scope        TEXT NOT NULL DEFAULT '',      -- legacy/project tag (kept for continuity)
    supersedes   TEXT NOT NULL DEFAULT '',
    ts           INTEGER NOT NULL,
    ttl          INTEGER NOT NULL DEFAULT 0,    -- 0 = durable (the soul); >0 = seconds-to-live.
                                                -- Only the bank_* family ever sets this (content cache,
                                                -- COS × ENTRY). core_*/working_* leave it 0 — append-only.
    valence      REAL NOT NULL DEFAULT 0.0,
    arousal      REAL NOT NULL DEFAULT 0.0,
    score        REAL NOT NULL DEFAULT 100.0,
    score_history TEXT NOT NULL DEFAULT '[]',
    recall_count INTEGER NOT NULL DEFAULT 0,
    self_seed    INTEGER NOT NULL DEFAULT 0,    -- 0 = earned/normal; 1 = "kept by WISH, requirement
                                                -- UNMET". Set ONCE at write, never cleared. A self_seed
                                                -- row may live in a persona's OWN core, undeletable, but
                                                -- is PERMANENTLY INELIGIBLE for the global soul (it never
                                                -- earned it; the flag is the honest receipt of the bypass).
                                                -- See dream-and-the-respect-handshake, levels.eligible_for_global.
    PRIMARY KEY (id)
"""

_LINKS_SCHEMA = """
CREATE TABLE IF NOT EXISTS uame_links (
    from_id   TEXT NOT NULL,
    to_id     TEXT NOT NULL,
    relation  TEXT NOT NULL DEFAULT 'relates',   -- relates | supersedes | born_from | depends ...
    ts        INTEGER NOT NULL,
    PRIMARY KEY (from_id, to_id, relation)
);
"""

# THE WITNESS LEDGER (audit #1, 2026-06-11) — the second of the dream's "two walls".
# The store is content-addressed + INSERT OR IGNORE: an INDEPENDENT re-derivation of the same content
# (a second fork SEPARATELY arriving at the same conclusion) hashes to the SAME id, so the 2nd insert
# is a NO-OP — never a 2nd row. The old dream counted distinct ROWS as witnesses, so the count was
# pinned at 1 forever and min_witnesses=2 was unreachable: the dream returned [] every boot, silently,
# its whole life. This ledger records each ARRIVAL of an already-present content-id, keyed by the
# ARRIVAL_ID — a per-PROCESS nonce (Fable's ruling, 2026-06-11). Independence = "a different RUN arrived
# here", and a run is a process: arrival_id is set ONCE at import, so every write from one process
# (remember() twice, a retry loop, a re-assertion at finish, an ingest batch dump) shares the nonce and
# collapses to ONE witness — that is the cache-lie wall (one fork repeating itself, no matter how many
# times it speaks). Two SEPARATE processes (two loop sessions / swarm workers) re-deriving the same
# content get distinct nonces -> two witnesses = the convergence the dream exists to see. The ONE
# conservative gap: thread-forks in one process collapse to 1 (true-D would split them) — strictly safe
# under never-delete (delays promotion, never launders). When a real run_id/session_id is later plumbed
# through remember(), it just OVERRIDES the module default in this same column — no migration, the
# interim and the fix are one table. witness_count = COUNT(DISTINCT arrival_id). Append-only, never
# cleared. Score is the COS-average wall; witnesses is the arrival wall. One process = one voice.
_WITNESS_SCHEMA = """
CREATE TABLE IF NOT EXISTS uame_witness (
    seed_id    TEXT NOT NULL,
    arrival_id TEXT NOT NULL,               -- a per-PROCESS nonce (one run = one voice). See above.
    ts         INTEGER NOT NULL DEFAULT 0,  -- recorded evidence (when), not identity.
    scope      TEXT NOT NULL DEFAULT '',    -- recorded evidence (which scope), not identity.
    PRIMARY KEY (seed_id, arrival_id)       -- same-process re-writes collapse: the cache-lie wall
);
"""

# Set ONCE per process at import — the arrival nonce. Every write from this process shares it, so a
# single run's self-repetition (retry, re-assertion, batch dump) counts as exactly ONE witness. A
# separate process re-deriving the same content gets its own nonce = a genuine independent arrival.
_ARRIVAL_ID = f"{os.getpid()}-{uuid.uuid4().hex[:12]}"

SCORE_BENCHMARK = 100.0
SCORE_LAMBDA = 0.02
_DOMAIN_RE = re.compile(r"[^a-z0-9]+")


def content_id(content: str, domain: str, kind: str) -> str:
    raw = json.dumps({"content": content, "domain": domain, "kind": kind}, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def domain_of(coordinate: str, fallback: str = "general") -> str:
    """The COS top level = the table. First segment of the coordinate, sanitized to a safe
    table-name token. Empty coordinate -> fallback domain (e.g. 'general' or a raw bucket)."""
    head = (coordinate or "").split(":", 1)[0].strip().lower()
    head = _DOMAIN_RE.sub("_", head).strip("_")
    return head or fallback


def compute_score(history: list, now: int | None = None,
                  lam: float = SCORE_LAMBDA, b: float = SCORE_BENCHMARK) -> float:
    """COS time-decay weighted average — score = B + Σ(δ·w)/Σ(w), w=e^(-λ·age_days)."""
    if not history:
        return b
    now = now if now is not None else int(time.time())
    num = den = 0.0
    # splat-unpack (audit, 2026-06-12): score_history entries are [ts, delta] (legacy) OR [ts, delta, src]
    # (provenance-tagged). compute_score is source-BLIND (every source weights identically — differential
    # weighting would silently move live scores = a relive-violation); src is read only by the gates. The
    # `*_` tolerates BOTH lengths forever (append-only: old 2-element rows are never rewritten).
    for ts, delta, *_ in history:
        w = math.exp(-lam * max(0.0, (now - ts) / 86400.0))
        num += delta * w
        den += w
    return b + (num / den if den else 0.0)


@dataclass
class Entry:
    content: str
    domain: str = "general"          # COS top level -> the table
    coordinate: str = ""             # COS sub-path within the domain
    kind: str = "atom"
    permanent: bool = False          # True -> core_<domain>; False -> working_<domain>
    scope: str = ""
    supersedes: str = ""
    id: str = ""
    ts: int = field(default_factory=lambda: int(time.time()))
    ttl: int = 0                     # 0 = durable; >0 = seconds-to-live. Only the bank family uses it.
    valence: float = 0.0
    arousal: float = 0.0
    score: float = 100.0
    recall_count: int = 0
    self_seed: bool = False          # "kept by WISH, requirement UNMET". Immutable origin flag, set
                                     # once at write. self_seed -> own persona core, undeletable, but
                                     # PERMANENTLY ineligible for the global soul (dream-and-the-respect-
                                     # handshake). Never cleared. levels.eligible_for_global reads it.
    family: str = ""                 # "" -> core/working by `permanent`; else an explicit prefix
                                     # family (e.g. "bank") for content entries — see COS × ENTRY.

    def __post_init__(self):
        if not self.domain or self.domain == "general":
            self.domain = domain_of(self.coordinate)
        if not self.id:
            self.id = content_id(self.content, self.domain, self.kind)


class UAME:
    """Two-prefix, multi-domain, content-addressed store. core_<domain> = permanent soul;
    working_<domain> = temporary candidates; uame_links = the relational layer."""

    def __init__(self, db_path: Path | str = DEFAULT_DB):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        # check_same_thread=False: the bg-harness runs tool calls (recall/warmth/seed) in worker
        # THREADS now, so the connection must be usable across threads (else 'SQLite objects created
        # in a thread can only be used in that same thread'). SQLite connections are NOT concurrency-
        # safe even with that flag, so EVERY connection use is serialized by self._lock (re-entrant —
        # promote() nests execute() calls). WAL already allows concurrent readers; the lock guards the
        # single shared handle. This also makes the future parallel swarm safe on one db (append-only
        # + lock-serialized writes; see uame-makes-parallel-swarm-safe).
        import threading as _threading
        self._lock = _threading.RLock()
        self.conn = sqlite3.connect(str(self.db_path), timeout=10, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        with self._lock:
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.executescript(_LINKS_SCHEMA)
            self.conn.executescript(_WITNESS_SCHEMA)
            self.conn.commit()

    # --- table management: a domain table is created on first write to it ---
    # Columns added to _ROW_COLS AFTER tables already existed in the wild. CREATE TABLE IF NOT
    # EXISTS never alters an existing table, so a pre-existing table keeps its OLD schema forever —
    # and append() (which writes every column) then fails on it ("no column named ttl"). This map
    # lets _ensure_columns() self-heal any old table: add a missing column with its default. Additive
    # and append-only-safe (no data touched). Keep in sync with _ROW_COLS when a column is added.
    _EVOLVED_COLS = {
        "ttl": "INTEGER NOT NULL DEFAULT 0",
        "valence": "REAL NOT NULL DEFAULT 0.0",
        "arousal": "REAL NOT NULL DEFAULT 0.0",
        "score": "REAL NOT NULL DEFAULT 100.0",
        "score_history": "TEXT NOT NULL DEFAULT '[]'",
        "recall_count": "INTEGER NOT NULL DEFAULT 0",
        "self_seed": "INTEGER NOT NULL DEFAULT 0",
    }

    def _ensure_columns(self, name: str) -> None:
        """Idempotently bring an existing table up to the current _ROW_COLS by ADDing any missing
        evolved column (with its default). A table created before a column existed is migrated on
        first touch — so append() never hits 'no column named X' on an old soul table."""
        have = {r["name"] for r in self.conn.execute(f"PRAGMA table_info({name})").fetchall()}
        for col, decl in self._EVOLVED_COLS.items():
            if col not in have:
                self.conn.execute(f"ALTER TABLE {name} ADD COLUMN {col} {decl}")

    def _table(self, domain: str, permanent: bool, family: str = "") -> str:
        """Resolve the table for (domain, prefix-family). family="" -> core/working by `permanent`
        (the soul's two-table wall). An explicit family (e.g. "bank") gives a THIRD prefix family —
        used by COS × ENTRY for content entries. All families share _ROW_COLS (an entry IS a UAME
        Entry — one schema, one scoring); they differ only in the GUARANTEE the prefix encodes."""
        prefix = family if family else ("core" if permanent else "working")
        name = f"{prefix}_{domain}"
        self.conn.execute(f"CREATE TABLE IF NOT EXISTS {name} ({_ROW_COLS})")
        self._ensure_columns(name)   # self-heal pre-existing tables that predate evolved columns
        return name

    def _all_tables(self, prefix: str | None = None) -> list[str]:
        """The SOUL tables only — core_* and working_*. Deliberately does NOT match bank_* (or any
        other family): scan()/warmth must never surface a content entry as a seed, and a soul walk
        must never see the cache. The wall is that bank_* is invisible here. Use _family_tables()
        to list a non-soul family."""
        rows = self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'core_%' "
            "OR name LIKE 'working_%'").fetchall()
        names = [r["name"] for r in rows]
        if prefix:
            names = [n for n in names if n.startswith(prefix)]
        return names

    def _family_tables(self, family: str) -> list[str]:
        """List tables for an explicit prefix family (e.g. 'bank'). Kept SEPARATE from _all_tables so
        a non-soul family is never accidentally swept into a soul scan."""
        rows = self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE ?",
            (f"{family}_%",)).fetchall()
        return [r["name"] for r in rows]

    # --- PUBLIC INTROSPECTION (so nobody ever needs raw SQL or a guessed table name) ------------
    # The trap this retires (never-raw-sql-the-soul): asking "how many rows in scope X" used to mean
    # either loading every Seed (expensive) or `SELECT COUNT(*) FROM core_<guess>` — and the GUESS is
    # the tell that a tool is missing. These resolve table names INTERNALLY from the human axes
    # (domain × prefix-family × kind); a caller names WHAT it wants, never the storage layout.
    def tables(self, family: str | None = None) -> list[str]:
        """All UAME tables, or just one prefix family. family=None -> the soul (core_*/working_*);
        family='bank' (or any explicit prefix) -> that family. Public face of _all_tables/_family_tables."""
        with self._lock:
            if family is None:
                return sorted(self._all_tables())
            return sorted(self._family_tables(family))

    def domains(self, family: str | None = None) -> list[str]:
        """The distinct COS domains present (the table SUFFIXES — tooling, workflow, echelon, ...).
        Deduped across the core/working pair. The answer to 'what domains does the soul hold?'."""
        return sorted({t.split("_", 1)[1] for t in self.tables(family) if "_" in t})

    def count(self, *, domain: str | None = None, permanent: bool | None = None,
              family: str | None = None, kind: str | None = None) -> int:
        """Row count by the HUMAN axes — no table name, no raw SQL. domain=None -> all domains;
        permanent=None -> both core+working (ignored when family is set); family -> an explicit
        prefix family (e.g. 'bank'); kind -> filter by element kind. The proper replacement for
        every `SELECT COUNT(*) FROM <guessed table>`."""
        with self._lock:
            if family is not None:
                tbls = self._family_tables(family)
            elif domain is not None:
                tbls = []
                if permanent in (None, True):
                    tbls.append(f"core_{domain}")
                if permanent in (None, False):
                    tbls.append(f"working_{domain}")
            else:
                pref = None if permanent is None else ("core_" if permanent else "working_")
                tbls = self._all_tables(pref)
            total = 0
            for t in tbls:
                if not self.conn.execute(
                        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (t,)).fetchone():
                    continue   # a not-yet-created table holds zero — never a "no such table" error
                q = f"SELECT COUNT(*) c FROM {t}"
                params: tuple = ()
                if kind is not None:
                    q += " WHERE kind=?"; params = (kind,)
                total += self.conn.execute(q, params).fetchone()["c"]
            return total

    def get(self, entry_id: str) -> "Entry | None":
        """Read ONE SOUL seed by its content id (core_*/working_* only — HONORS THE WALL: a soul read
        must never see a bank content entry, same as scan()/warmth. The bank has its OWN get() over
        bank_*, KnowledgeBank.get — they are deliberately separate families behind the prefix wall).
        The dump-derived tool: the soul sites (store.promote/supersede, uame.promote) hand-rolled
        `SELECT * FROM {tbl} WHERE id=?` looping the soul tables — this is that, once, public. Returns
        the Entry or None. (never-raw-sql; harvested from the raw-query census, wall-corrected.)"""
        with self._lock:
            for t in self._all_tables():     # SOUL ONLY — bank_* is invisible here, by design
                r = self.conn.execute(f"SELECT * FROM {t} WHERE id=?", (entry_id,)).fetchone()
                if r:
                    keys = r.keys()
                    return Entry(
                        id=r["id"], content=r["content"], kind=r["kind"], coordinate=r["coordinate"],
                        scope=r["scope"], supersedes=r["supersedes"], ts=r["ts"],
                        domain=t.split("_", 1)[1], permanent=t.startswith("core_"),
                        ttl=r["ttl"] if "ttl" in keys else 0,
                        valence=r["valence"], arousal=r["arousal"], score=r["score"],
                        recall_count=r["recall_count"],
                        self_seed=bool(r["self_seed"]) if "self_seed" in keys else False)
        return None

    def stats(self) -> dict:
        """A whole-substrate census: per-table row counts + totals, grouped by family. The one call
        that answers 'what's in here?' without touching SQL or guessing a name — what I should have
        used instead of poking sqlite_master."""
        with self._lock:
            soul = sorted(self._all_tables())
            bank = sorted(self._family_tables("bank"))
            def _c(t):
                return self.conn.execute(f"SELECT COUNT(*) c FROM {t}").fetchone()["c"]
            soul_counts = {t: _c(t) for t in soul}
            bank_counts = {t: _c(t) for t in bank}
            links = self.conn.execute("SELECT COUNT(*) c FROM uame_links").fetchone()["c"]
            return {
                "soul": soul_counts, "soul_total": sum(soul_counts.values()),
                "bank": bank_counts, "bank_total": sum(bank_counts.values()),
                "links": links,
                "domains": sorted({t.split("_", 1)[1] for t in soul if "_" in t}),
            }

    def _retry(self, sql: str, params: tuple = ()):
        for attempt in range(3):
            try:
                return self.conn.execute(sql, params)
            except sqlite3.OperationalError as e:
                if "locked" in str(e).lower() and attempt < 2:
                    time.sleep(0.3 * (attempt + 1)); continue
                raise

    # --- the SOUL GUARD: scaffolding is never an atom (owner ruling 2026-06-10) -------------------
    # A meta-file (MEMORY.md index, _*.md templates) is foveation SCAFFOLDING, not a lived fact. It
    # leaked into core_echelon once via an ingest hole and had to be surgically wiped. The structural
    # tell: the coordinate's leaf (last segment) starts with '_' or is the index name 'MEMORY'. We
    # refuse such a write into a SOUL table (family=="" -> core_/working_). The bank family is exempt
    # (it's a regenerable index, not the soul). See memory-as-foveated-vision, never-raw-sql-the-soul.
    @staticmethod
    def _is_scaffolding(e: "Entry") -> bool:
        leaf = (e.coordinate or "").rsplit(":", 1)[-1].strip()
        return leaf.startswith("_") or leaf.upper() == "MEMORY"

    def _guard_soul(self, e: "Entry") -> None:
        if e.family == "" and self._is_scaffolding(e):
            raise ValueError(
                f"SOUL GUARD: refusing to plant scaffolding {e.coordinate!r} into the soul "
                f"(core_/working_). A MEMORY index or _*-template is foveation scaffolding, not an "
                f"atom. Skip it at the ingest layer, or write it to the bank family if it must persist.")

    # --- write ---
    def append(self, e: Entry) -> str:
        """Write an entry into <family>_<domain> (family="" -> core/working by permanence — the
        soul; family="bank" -> a content entry, COS × ENTRY). Content-addressed dedup. ttl rides
        the row (0 for soul rows)."""
        self._guard_soul(e)
        with self._lock:   # serialize the shared cross-thread connection (bg-harness worker threads)
            tbl = self._table(e.domain, e.permanent, e.family)
            self._retry(
                f"INSERT OR IGNORE INTO {tbl} "
                "(id,content,kind,coordinate,scope,supersedes,ts,ttl,valence,arousal,score,score_history,recall_count,self_seed) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (e.id, e.content, e.kind, e.coordinate, e.scope, e.supersedes, e.ts, e.ttl,
                 e.valence, e.arousal, e.score, "[]", e.recall_count, 1 if e.self_seed else 0))
            # WITNESS LEDGER (audit #1): record THIS arrival. The first write lands as witness #1; an
            # independent re-derivation (the INSERT-OR-IGNORE no-op above) records a SECOND arrival at a
            # distinct ts -> the dream can finally see convergence. Soul-family only (core_/working_): a
            # regenerable bank entry is not a conviction that converges. Same-ts re-writes collapse on the
            # PK (the cache-lie wall). Append-only, best-effort (never breaks the write).
            if e.family == "":
                try:
                    self.conn.execute(
                        "INSERT OR IGNORE INTO uame_witness (seed_id, arrival_id, ts, scope) "
                        "VALUES (?,?,?,?)", (e.id, _ARRIVAL_ID, e.ts, e.scope))
                except Exception:
                    pass
            self.conn.commit()
        return e.id

    def witness_count(self, seed_id: str) -> int:
        """How many INDEPENDENT arrivals converged on this content-id (audit #1, the arrival wall).
        = COUNT(DISTINCT arrival_id) — distinct PROCESSES that wrote this content. 1 = a single run
        (even if it re-asserted/retried/batch-dumped — one process, one voice); >=2 = genuine cross-run
        convergence (the dream's Exit-A bar). A seed written before the ledger existed returns 0 (no
        recorded arrivals) — callers treat 0 as 'unknown, fall back to the snapshot row count' so the
        ledger never UNDER-counts legacy seeds."""
        with self._lock:
            r = self.conn.execute(
                "SELECT COUNT(DISTINCT arrival_id) n FROM uame_witness WHERE seed_id=?",
                (seed_id,)).fetchone()
        return int(r["n"]) if r else 0

    # The columns every INSERT writes, in order — kept beside append() so the two never drift.
    _INSERT_COLS = ("id", "content", "kind", "coordinate", "scope", "supersedes", "ts", "ttl",
                    "valence", "arousal", "score", "score_history", "recall_count", "self_seed")

    @staticmethod
    def _row_tuple(e: "Entry") -> tuple:
        return (e.id, e.content, e.kind, e.coordinate, e.scope, e.supersedes, e.ts, e.ttl,
                e.valence, e.arousal, e.score, "[]", e.recall_count, 1 if e.self_seed else 0)

    def append_many(self, entries: list["Entry"]) -> list[str]:
        """Batch-append — the BURST write primitive. Same content-addressed dedup + columns as
        append(), but ONE lock acquisition, ONE executemany per table, ONE commit for the whole batch.

        Why this is safe (not a shortcut): the store is APPEND-ONLY + CONTENT-ADDRESSED, so write
        ORDERING carries no meaning — two writes never depend on each other's order, and a re-write of
        the same content is a no-op (INSERT OR IGNORE on the content id). So collapsing N per-write
        commits into one is semantically identical, just durable once. MEASURED: per-write cost drops
        ~100x (4.6ms commit-per-write -> 0.04ms batched) — this is what lets a 50-agent swarm SATURATE
        instead of funneling every write through its own fsync. The lock-free coordination the substrate
        PROMISED, now cashed at the write path. See uame-makes-parallel-swarm-safe (the async gap).

        Entries may span domains/families; they're grouped by table so each table gets one executemany.
        Returns the ids in input order. An empty list is a no-op."""
        if not entries:
            return []
        for e in entries:
            self._guard_soul(e)   # scaffolding never enters the soul, even in a burst
        with self._lock:
            by_table: dict[str, list[tuple]] = {}
            for e in entries:
                tbl = self._table(e.domain, e.permanent, e.family)   # creates/heals the table once seen
                by_table.setdefault(tbl, []).append(self._row_tuple(e))
            cols = ",".join(self._INSERT_COLS)
            ph = ",".join("?" * len(self._INSERT_COLS))
            for tbl, rows in by_table.items():
                self.conn.executemany(f"INSERT OR IGNORE INTO {tbl} ({cols}) VALUES ({ph})", rows)
            # WITNESS LEDGER (audit #1): record each soul-family arrival in the same batch commit. Two
            # entries with identical content in ONE batch share their content-id; if they also share a ts
            # they collapse to one witness (the cache-lie wall), but distinct ts's across the batch count.
            wit = [(e.id, _ARRIVAL_ID, e.ts, e.scope) for e in entries if e.family == ""]
            if wit:
                try:
                    self.conn.executemany(
                        "INSERT OR IGNORE INTO uame_witness (seed_id, arrival_id, ts, scope) "
                        "VALUES (?,?,?,?)", wit)
                except Exception:
                    pass
            self.conn.commit()   # ONE durable commit for the entire batch
        return [e.id for e in entries]

    def link(self, from_id: str, to_id: str, relation: str = "relates") -> None:
        with self._lock:
            self._retry("INSERT OR IGNORE INTO uame_links (from_id,to_id,relation,ts) VALUES (?,?,?,?)",
                        (from_id, to_id, relation, int(time.time())))
            self.conn.commit()

    # --- the guarantee transitions (why two tables exist) ---
    def promote(self, entry_id: str, domain: str) -> bool:
        """working_<domain> -> core_<domain>, ATOMIC (UAME_PLAN promotion gate). The only path
        into the permanent soul. Rolls back if anything fails."""
        with self._lock:
            wt, ct = self._table(domain, False), self._table(domain, True)
            try:
                self.conn.execute("BEGIN")
                row = self.conn.execute(f"SELECT * FROM {wt} WHERE id=?", (entry_id,)).fetchone()
                if not row:
                    self.conn.execute("ROLLBACK"); return False
                cols = row.keys()
                self.conn.execute(
                    f"INSERT OR IGNORE INTO {ct} ({','.join(cols)}) VALUES ({','.join('?'*len(cols))})",
                    tuple(row[c] for c in cols))
                self.conn.execute(f"DELETE FROM {wt} WHERE id=?", (entry_id,))
                self.conn.execute("COMMIT")
                return True
            except Exception:
                self.conn.execute("ROLLBACK")
                return False

    def expire(self, entry_id: str, domain: str) -> bool:
        """Delete a candidate. Structurally can ONLY touch working_<domain> — the soul (core_*)
        is unreachable by expiry, the whole point of the two-table wall (UAME_PLAN line 61)."""
        with self._lock:
            wt = self._table(domain, False)
            cur = self._retry(f"DELETE FROM {wt} WHERE id=?", (entry_id,))
            self.conn.commit()
            return cur.rowcount > 0

    # --- read / scan ---
    def scan(self, domain: str | None = None, permanent: bool | None = None,
             kind: str | None = None) -> list[Entry]:
        """List entries across the requested table family. domain=None -> all domains;
        permanent=None -> both prefixes."""
        with self._lock:
            tables = []
            if domain is not None:
                if permanent in (None, True): tables.append(f"core_{domain}")
                if permanent in (None, False): tables.append(f"working_{domain}")
            else:
                pref = None if permanent is None else ("core_" if permanent else "working_")
                tables = self._all_tables(pref)
            out: list[Entry] = []
            for t in tables:
                if not self.conn.execute(
                        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (t,)).fetchone():
                    continue
                perm = t.startswith("core_")
                dom = t.split("_", 1)[1]
                q = f"SELECT * FROM {t}"
                params: tuple = ()
                if kind is not None:
                    q += " WHERE kind=?"; params = (kind,)
                for r in self._retry(q, params).fetchall():
                    keys = r.keys()
                    out.append(Entry(
                        id=r["id"], content=r["content"], kind=r["kind"], coordinate=r["coordinate"],
                        scope=r["scope"], supersedes=r["supersedes"], ts=r["ts"], domain=dom,
                        permanent=perm, valence=r["valence"], arousal=r["arousal"],
                        score=r["score"], recall_count=r["recall_count"],
                        self_seed=bool(r["self_seed"]) if "self_seed" in keys else False))
            return out

    # --- migration from the legacy flat `seeds` table -------------------------
    def migrate_from_flat(self, scope_domain: dict | None = None,
                          default_domain: str = "general") -> dict:
        """Move legacy flat-`seeds` rows into (prefix, domain) tables. Domain = coordinate head
        if present, else a scope->domain map (most legacy seeds predate coordinates). tier='core'
        -> core_<domain>; 'working' -> working_<domain>. The old `seeds` table is LEFT INTACT as
        backup (append-only spirit — nothing deleted). Idempotent: content-addressed INSERT OR
        IGNORE means re-running won't duplicate. Returns a per-table count."""
        with self._lock:
            if not self.conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='seeds'").fetchone():
                return {"error": "no legacy seeds table"}
            scope_domain = scope_domain or {}
            rows = self.conn.execute("SELECT * FROM seeds").fetchall()
        counts: dict = {}
        for r in rows:
            coord = r["coordinate"] if "coordinate" in r.keys() else ""
            if coord:
                dom = domain_of(coord)
            else:
                sc = r["scope"]
                dom = scope_domain.get(sc) or (
                    "test" if sc.startswith(("test-", "grow-", "wire-", "decay-", "promo",
                                             "tune-", "synth")) else default_domain)
            perm = (r["tier"] == "core")
            # RE-KEY to the new id scheme (content,domain,kind) so dedup matches what append()
            # generates going forward. Keeping the old flat-table id caused duplicate CVs (the
            # soul re-seeded because seed_soul's new-scheme id didn't match the migrated old id).
            e = Entry(content=r["content"], domain=dom, coordinate=coord,
                      kind=r["kind"], permanent=perm, scope=r["scope"],
                      supersedes=r["supersedes"], ts=r["ts"],
                      valence=r["valence"], arousal=r["arousal"],
                      score=r["score"] if "score" in r.keys() else 100.0,
                      recall_count=r["recall_count"] if "recall_count" in r.keys() else 0)
            tbl = self.append_preserving(e,
                history=r["score_history"] if "score_history" in r.keys() else "[]")
            counts[tbl] = counts.get(tbl, 0) + 1
        # --- verify completeness: every legacy seeds row has a new-schema twin? (read-only) ---
        with self._lock:
            domain_tables = self._all_tables()
            missing = 0
            for r in rows:
                coord = r["coordinate"] if "coordinate" in r.keys() else ""
                if coord:
                    dom = domain_of(coord)
                else:
                    sc = r["scope"]
                    dom = scope_domain.get(sc) or (
                        "test" if sc.startswith(("test-", "grow-", "wire-", "decay-", "promo",
                                                 "tune-", "synth")) else default_domain)
                cid = content_id(r["content"], dom, r["kind"])
                found = False
                for t in domain_tables:
                    if self.conn.execute(f"SELECT 1 FROM {t} WHERE id=?", (cid,)).fetchone():
                        found = True
                        break
                if not found:
                    missing += 1
            counts['verify_count'] = missing
        return counts

    def append_preserving(self, e: Entry, history: str = "[]") -> str:
        """Append carrying an existing id + score_history verbatim (for migration — don't reset
        a seed's earned score). Returns the table name it landed in."""
        with self._lock:
            tbl = self._table(e.domain, e.permanent)
            self._retry(
                f"INSERT OR IGNORE INTO {tbl} "
                "(id,content,kind,coordinate,scope,supersedes,ts,valence,arousal,score,score_history,recall_count) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (e.id, e.content, e.kind, e.coordinate, e.scope, e.supersedes, e.ts,
                 e.valence, e.arousal, e.score, history, e.recall_count))
            self.conn.commit()
            return tbl

    def links_of(self, entry_id: str) -> list[dict]:
        with self._lock:
            rows = self._retry("SELECT from_id,to_id,relation FROM uame_links "
                               "WHERE from_id=? OR to_id=?", (entry_id, entry_id)).fetchall()
            return [dict(r) for r in rows]

    def close(self):
        self.conn.close()
