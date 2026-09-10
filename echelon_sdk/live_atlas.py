"""live_atlas — the RUNTIME atlas: every harness, role, loop, and LLM action, auto-mapped.

THE IDEA (owner, 2026-07-03): the atlas verb maps what EXISTS (repos, components — static
relationship nodes). This is the same grammar extended to what's RUNNING: one topology where
every harness auto-registers, heartbeats, and streams its actions — the process table +
service map of the AI operating system.

    NODES    what's running (harness / role / loop / worker / session)
             + what exists (repo / component / scope — the static atlas plugs in as nodes)
    EDGES    spawned_by / speaks_on / works_scope / touches_repo / member_of
    ACTIONS  append-only stream: ts · node · kind (tool|dispatch|seed|recall) · summary

PRODUCERS = the nerve event inventory (2026-07-03): the same events that fire the memory
organs also drop atlas rows — no new instrumentation, the nerve IS the mapper:
    SessionStart hook      -> harness node
    society watcher        -> role node + member_of(session) + speaks_on(channel)
    claude-echelon run     -> session node + actions
    proxy worker request   -> worker beat
    world loops            -> loop node
    PostToolUse / seed     -> action rows

SEVERABILITY (the nerve law applies here too): every write is wrapped — a dead atlas can
never break a channel. Registration is fire-and-forget observability, not a dependency.

sdk-pure: stdlib only, no upward imports (the scanner law).
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from pathlib import Path

_ATLAS_PATH = Path(os.environ.get("ECHELON_ATLAS", str(Path.home() / ".echelon" / "atlas.db")))

_SCHEMA = """
CREATE TABLE IF NOT EXISTS nodes (
    id         TEXT PRIMARY KEY,          -- '<kind>:<name>' e.g. 'role:builder@soc-...'
    kind       TEXT NOT NULL,             -- harness|role|loop|worker|session|repo|component|scope
    name       TEXT NOT NULL,
    meta       TEXT NOT NULL DEFAULT '{}',
    pid        INTEGER,
    first_seen REAL NOT NULL,
    beat_ts    REAL NOT NULL);
CREATE TABLE IF NOT EXISTS edges (
    src        TEXT NOT NULL,
    rel        TEXT NOT NULL,             -- spawned_by|speaks_on|works_scope|touches_repo|member_of
    dst        TEXT NOT NULL,
    ts         REAL NOT NULL,
    PRIMARY KEY (src, rel, dst));
CREATE TABLE IF NOT EXISTS actions (
    seq        INTEGER PRIMARY KEY AUTOINCREMENT,
    node       TEXT NOT NULL,
    kind       TEXT NOT NULL,             -- tool|dispatch|seed|recall|post|spawn|stand_down|...
    summary    TEXT NOT NULL,
    ts         REAL NOT NULL);
CREATE INDEX IF NOT EXISTS idx_actions_node ON actions(node, seq);
"""

# a node is LIVE if its heartbeat is younger than this
LIVE_TTL_S = 60.0

_lock = threading.RLock()


def _conn() -> sqlite3.Connection:
    _ATLAS_PATH.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(str(_ATLAS_PATH), timeout=10, check_same_thread=False)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    c.executescript(_SCHEMA)
    return c


def node_id(kind: str, name: str) -> str:
    return f"{kind}:{name}"


# ── WRITE SIDE (producers) — every call severable: a dead atlas never breaks a channel ──

def register(kind: str, name: str, *, meta: dict | None = None,
             edges: list[tuple[str, str, str]] | None = None) -> str:
    """Upsert a node (+ optional edges [(rel, dst_kind, dst_name), ...]). Returns node id.
    Never raises — observability must not become a dependency."""
    nid = node_id(kind, name)
    try:
        now = time.time()
        with _lock:
            c = _conn()
            try:
                c.execute(
                    "INSERT INTO nodes (id, kind, name, meta, pid, first_seen, beat_ts) "
                    "VALUES (?,?,?,?,?,?,?) "
                    "ON CONFLICT(id) DO UPDATE SET meta=excluded.meta, pid=excluded.pid, "
                    "beat_ts=excluded.beat_ts",
                    (nid, kind, name, json.dumps(meta or {}), os.getpid(), now, now))
                for rel, dk, dn in (edges or []):
                    c.execute("INSERT OR REPLACE INTO edges (src, rel, dst, ts) VALUES (?,?,?,?)",
                              (nid, rel, node_id(dk, dn), now))
                c.commit()
            finally:
                c.close()
    except Exception:
        pass  # severed: the channel proceeds unmapped
    return nid


def beat(kind: str, name: str) -> None:
    try:
        with _lock:
            c = _conn()
            try:
                c.execute("UPDATE nodes SET beat_ts=? WHERE id=?", (time.time(), node_id(kind, name)))
                c.commit()
            finally:
                c.close()
    except Exception:
        pass


def act(kind: str, name: str, action_kind: str, summary: str) -> None:
    """Append one action to a node's stream (truncated — the atlas maps, it doesn't archive)."""
    try:
        with _lock:
            c = _conn()
            try:
                c.execute("INSERT INTO actions (node, kind, summary, ts) VALUES (?,?,?,?)",
                          (node_id(kind, name), action_kind, summary[:400], time.time()))
                c.commit()
            finally:
                c.close()
    except Exception:
        pass


# ── READ SIDE (`echelon atlas live` / map / trace) ───────────────────────────────────────

def live(ttl: float = LIVE_TTL_S) -> list[dict]:
    """Every node, liveness-flagged — the one 'who is up, everywhere' answer."""
    now = time.time()
    with _lock:
        c = _conn()
        try:
            rows = c.execute("SELECT * FROM nodes ORDER BY beat_ts DESC").fetchall()
        finally:
            c.close()
    return [{"id": r["id"], "kind": r["kind"], "name": r["name"], "pid": r["pid"],
             "meta": json.loads(r["meta"] or "{}"),
             "up_s": round(now - r["first_seen"]), "beat_age_s": round(now - r["beat_ts"]),
             "live": (now - r["beat_ts"]) < ttl} for r in rows]


def trace(kind: str, name: str, limit: int = 50) -> list[dict]:
    """What did that node actually DO — its recent action stream."""
    with _lock:
        c = _conn()
        try:
            rows = c.execute("SELECT * FROM actions WHERE node=? ORDER BY seq DESC LIMIT ?",
                             (node_id(kind, name), limit)).fetchall()
        finally:
            c.close()
    return [{"seq": r["seq"], "kind": r["kind"], "summary": r["summary"], "ts": r["ts"]}
            for r in reversed(rows)]


def graph() -> dict:
    """Nodes + edges for the force-directed HTML renderer (the existing `graph` verb's shape)."""
    with _lock:
        c = _conn()
        try:
            nodes = [dict(r) for r in c.execute("SELECT id, kind, name, beat_ts FROM nodes")]
            edges = [dict(r) for r in c.execute("SELECT src, rel, dst FROM edges")]
        finally:
            c.close()
    return {"nodes": nodes, "edges": edges}
