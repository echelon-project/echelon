"""IntentPool — append-only bus-style registry for Intent records.

Uses sqlite3 (in-memory by default) to store intents.  The pool is purely
declarative: no file writes.  Every post validates via the Intent schema,
detects target_path / canonical_for collisions, and marks conflicts.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Optional, Sequence, List, Dict


from .intent import Intent


def _serialize_deps(deps: list[str]) -> str:
    return json.dumps(deps)


def _deserialize_deps(raw: str) -> list[str]:
    return json.loads(raw)


def _serialize_conflict_with(ids: list[str]) -> str:
    return json.dumps(ids)


def _deserialize_conflict_with(raw: str | None) -> list[str]:
    if raw is None:
        return []
    return json.loads(raw)


_INTENT_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS intents (
    rowid       INTEGER PRIMARY KEY AUTOINCREMENT,
    id          TEXT NOT NULL UNIQUE,
    part_of     TEXT NOT NULL,
    target_path TEXT NOT NULL,
    action      TEXT NOT NULL,
    body        TEXT NOT NULL,
    deps        TEXT NOT NULL,
    behavior        TEXT,
    canonical_for   TEXT,
    conflict        INTEGER NOT NULL DEFAULT 0,
    conflict_with   TEXT,
    posted_at       TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

_STATUS_EVENT_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS status_events (
    rowid       INTEGER PRIMARY KEY AUTOINCREMENT,
    intent_id   TEXT NOT NULL,
    status      TEXT NOT NULL,
    stage       TEXT,
    note        TEXT DEFAULT '',
    at          REAL NOT NULL DEFAULT (julianday('now'))
);
"""


class IntentPool:
    """Append-only registry of Intents backed by sqlite3.

    By default the pool lives in ``:memory:``; pass a path for persistence.
    """

    def __init__(self, db_path: str = ":memory:") -> None:
        self._conn = sqlite3.connect(db_path)
        self._conn.row_factory = sqlite3.Row
        # Enable WAL mode for file-backed databases for safe concurrent access
        if db_path != ':memory:':
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
        # Create Intent table
        self._conn.execute(_INTENT_SCHEMA_SQL)
        # Create Status Events table (as required by spec)
        self._conn.execute(_STATUS_EVENT_SCHEMA_SQL)
        self._conn.commit()

    # -- public API ----------------------------------------------------------

    def post(self, intent: Intent) -> Intent:
        """Validate *intent* via its schema, check for collisions, store it.

        The *intent* is accepted by value; the returned Intent is the same
        object.  Collisions are recorded inside the pool (the ``conflict``
        column and ``conflict_with`` column are set on the stored row, and
        on any previously-stored rows that conflict with this one).
        """
        # --- duplicate id check first ---------------------------------------
        cur = self._conn.execute("SELECT 1 FROM intents WHERE id = ?", (intent.id,))
        if cur.fetchone() is not None:
            raise ValueError(
                f"IntentPool.post: duplicate intent id {intent.id!r}"
            )

        # --- collision detection --------------------------------------------
        conflict_ids: list[str] = []

        # target_path claim: any existing intent that targets the same path
        for row in self._conn.execute(
            "SELECT id FROM intents WHERE target_path = ?", (intent.target_path,)
        ):
            conflict_ids.append(row["id"])

        # canonical_for collisions when the new intent provides one
        if intent.canonical_for is not None:
            # existing row has the same canonical_for
            for row in self._conn.execute(
                "SELECT id FROM intents WHERE canonical_for = ?",
                (intent.canonical_for,),
            ):
                if row["id"] not in conflict_ids:
                    conflict_ids.append(row["id"])
            # existing row whose target_path *is* the new canonical_for
            for row in self._conn.execute(
                "SELECT id FROM intents WHERE target_path = ?",
                (intent.canonical_for,),
            ):
                if row["id"] not in conflict_ids:
                    conflict_ids.append(row["id"])

        # existing row whose canonical_for is the new target_path
        for row in self._conn.execute(
            "SELECT id FROM intents WHERE canonical_for = ?",
            (intent.target_path,),
        ):
            if row["id"] not in conflict_ids:
                conflict_ids.append(row["id"])

        conflict_flag = 1 if conflict_ids else 0
        conflict_json = (
            _serialize_conflict_with(conflict_ids) if conflict_ids else None
        )

        # --- insert ---------------------------------------------------------
        self._conn.execute(
            """INSERT INTO intents
               (id, part_of, target_path, action, body, deps,
                behavior, canonical_for, conflict, conflict_with)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                intent.id,
                intent.part_of,
                intent.target_path,
                intent.action,
                intent.body,
                _serialize_deps(intent.deps),
                intent.behavior,
                intent.canonical_for,
                conflict_flag,
                conflict_json,
            ),
        )

        # --- mark conflicting existing rows ---------------------------------
        if conflict_ids:
            for cid in conflict_ids:
                # append this intent's id to the existing row's conflict_with
                row = self._conn.execute(
                    "SELECT conflict_with FROM intents WHERE id = ?", (cid,)
                ).fetchone()
                existing_conflicts: list[str] = _deserialize_conflict_with(
                    row["conflict_with"]
                )
                if intent.id not in existing_conflicts:
                    existing_conflicts.append(intent.id)
                self._conn.execute(
                    "UPDATE intents SET conflict = 1, conflict_with = ? WHERE id = ?",
                    (_serialize_conflict_with(existing_conflicts), cid),
                )

        self._conn.commit()
        return intent

    def post_status(self, intent_id: str, status: str, stage: Optional[str] = None, note: str = '') -> None:
        """Records a single status event for a given Intent ID."""
        self._conn.execute(
            """INSERT INTO status_events 
               (intent_id, status, stage, note) 
               VALUES (?, ?, ?, ?)""",
            (intent_id, status, stage, note),
        )
        self._conn.commit()

    def read_status(self, intent_id: Optional[str] = None) -> list[dict]:
        """Reads all status events or filters by a specific Intent ID."""
        query = "SELECT * FROM status_events"
        params = []
        if intent_id is not None:
            query += " WHERE intent_id = ?"
            params.append(intent_id)

        query += " ORDER BY rowid ASC"
        
        rows = self._conn.execute(query, tuple(params)).fetchall()
        
        # Convert sqlite3.Row objects to standard dicts
        return [dict(row) for row in rows]


    def read(self) -> list[Intent]:
        """Return every registered intent, in insertion order."""
        rows = self._conn.execute(
            "SELECT * FROM intents ORDER BY rowid ASC"
        ).fetchall()
        return [_row_to_intent(r) for r in rows]

    # -- helper --------------------------------------------------------------

    def close(self) -> None:
        """Closes the underlying database connection."""
        self._conn.close()


def _row_to_intent(row: sqlite3.Row) -> Intent:
    """Reconstitute an Intent from a db row."""
    return Intent(
        id=row["id"],
        part_of=row["part_of"],
        target_path=row["target_path"],
        action=row["action"],
        body=row["body"],
        deps=_deserialize_deps(row["deps"]),
        behavior=row["behavior"],
        canonical_for=row["canonical_for"],
    )


# The triple-quoted string literal was missing a closing """ on line 2
