"""EchelonBus — a native message bus for the role society (the Flux bus pattern, RECLAIMED).

Ported from the Flux bus (session-scoped channels + cursor drain) into ECHELON-AGENT as its OWN
feature — pattern kept, dependency dropped (reclaim-the-method). No server, no HTTP: an in-process,
append-only, lock-safe SQLite bus, the SAME discipline as the soul store (append-only + a lock =
N threads cross-pollinate, never race; uame-makes-parallel-swarm-safe).

CHANNELS are first-class and OPEN: any role posts to any channel name it wants (role.reviewer,
news.media, bug.reports, ...). Roles BUILD their own channels by posting to them — the bus does not
predefine them. The base is only: post a message, and read what's new for a reader since its cursor.

THE FLUSTER GUARD (owner: "the watcher for pooling drain etc are automatic, so agent wont get
flustered"): a ROLE never calls drain() or tracks a cursor. That bookkeeping narrows the model's
vision (staying-free / slim-trunk). The WATCHER (see society runner) does the pooling+draining and
delivers new messages INTO the role's next turn. The role only ever sees an inbox; the plumbing is
the substrate's job, not the model's. post() is the only thing a role calls.

A JSONL mirror is written alongside (every message appended) so a human can `tail -f` the whole
society without a server — watchable, source-of-truth stays the db.
"""
from __future__ import annotations
import json
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    seq      INTEGER PRIMARY KEY AUTOINCREMENT,
    channel  TEXT NOT NULL,
    sender   TEXT NOT NULL,
    body     TEXT NOT NULL,
    meta     TEXT NOT NULL DEFAULT '{}',
    ts       REAL NOT NULL);
CREATE INDEX IF NOT EXISTS idx_msg_channel ON messages(channel, seq);
CREATE TABLE IF NOT EXISTS cursors (
    reader   TEXT NOT NULL,
    channel  TEXT NOT NULL,
    last_seq INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (reader, channel));
CREATE TABLE IF NOT EXISTS presence (
    role       TEXT PRIMARY KEY,
    pid        INTEGER NOT NULL,
    started_ts REAL NOT NULL,
    beat_ts    REAL NOT NULL);
CREATE TABLE IF NOT EXISTS reserved_readers (
    reader     TEXT PRIMARY KEY,
    note       TEXT NOT NULL DEFAULT '',
    ts         REAL NOT NULL);
"""

# a registration is LIVE if its heartbeat is younger than this (watcher beats every poll)
PRESENCE_TTL_S = 30.0


@dataclass
class Message:
    seq: int
    channel: str
    sender: str
    body: str
    meta: dict
    ts: float


class EchelonBus:
    """Append-only channel bus with per-reader cursors. Thread-safe (one RLock, like the store)."""

    def __init__(self, db_path: Path | str, mirror_jsonl: Path | str | None = None):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.mirror = Path(mirror_jsonl) if mirror_jsonl else None
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(str(self.db_path), timeout=10, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        with self._lock:
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.executescript(_SCHEMA)
            self.conn.commit()

    # --- the ONLY thing a role calls: post a message to a channel (builds the channel if new) ---
    def post(self, channel: str, sender: str, body: str, meta: dict | None = None) -> int:
        ts = time.time()
        with self._lock:
            cur = self.conn.execute(
                "INSERT INTO messages (channel, sender, body, meta, ts) VALUES (?,?,?,?,?)",
                (channel, sender, body, json.dumps(meta or {}), ts))
            self.conn.commit()
            seq = cur.lastrowid
        if self.mirror:
            with self.mirror.open("a", encoding="utf-8") as f:
                f.write(json.dumps({"seq": seq, "channel": channel, "sender": sender,
                                    "body": body, "meta": meta or {}, "ts": ts},
                                   ensure_ascii=False) + "\n")
        return seq

    # --- PRESENCE (foolproofing 2026-07-03): role registration + heartbeat -------------------
    # Kills the duplicate-reader mail-theft: drain() advances a shared cursor, so two watchers
    # holding the same role name on one bus silently STEAL each other's mail. register() makes
    # that collision LOUD at spawn time instead of silent at mail time.

    class RoleTaken(RuntimeError):
        """Another live watcher already holds this role on this bus."""

    def register(self, role: str, pid: int, ttl: float = PRESENCE_TTL_S) -> None:
        """Claim `role` on this bus. Raises RoleTaken if a LIVE registration (heartbeat younger
        than ttl) holds it. A stale registration (dead watcher) is reclaimed silently."""
        now = time.time()
        with self._lock:
            row = self.conn.execute("SELECT pid, beat_ts FROM presence WHERE role=?", (role,)).fetchone()
            if row and (now - row["beat_ts"]) < ttl and row["pid"] != pid:
                raise EchelonBus.RoleTaken(
                    f"role {role!r} is LIVE on this bus (pid {row['pid']}, beat "
                    f"{now - row['beat_ts']:.0f}s ago). Pick another role name or session.")
            self.conn.execute(
                "INSERT INTO presence (role, pid, started_ts, beat_ts) VALUES (?,?,?,?) "
                "ON CONFLICT(role) DO UPDATE SET pid=excluded.pid, started_ts=excluded.started_ts, "
                "beat_ts=excluded.beat_ts", (role, pid, now, now))
            self.conn.commit()

    def beat(self, role: str) -> None:
        with self._lock:
            self.conn.execute("UPDATE presence SET beat_ts=? WHERE role=?", (time.time(), role))
            self.conn.commit()

    def unregister(self, role: str) -> None:
        with self._lock:
            self.conn.execute("DELETE FROM presence WHERE role=?", (role,))
            self.conn.commit()

    def alive(self, ttl: float = PRESENCE_TTL_S) -> list[dict]:
        """Live roles on this bus (heartbeat younger than ttl) — the `society status` truth."""
        now = time.time()
        with self._lock:
            rows = self.conn.execute("SELECT * FROM presence ORDER BY role").fetchall()
        return [{"role": r["role"], "pid": r["pid"], "up_s": round(now - r["started_ts"]),
                 "beat_age_s": round(now - r["beat_ts"]),
                 "live": (now - r["beat_ts"]) < ttl} for r in rows]

    # --- RESERVED READERS (the cursor-poison trap, ENFORCED not warned) ----------------------
    # A wake-hook's cursor id is load-bearing: one hand-run `bus check-mail <id>` advances it
    # and the next real message silently fails to wake the agent (learned 2026-07-02, was only
    # a docstring warning). reserve_reader() makes the mistake impossible instead of forbidden.

    def reserve_reader(self, reader: str, note: str = "") -> None:
        with self._lock:
            self.conn.execute(
                "INSERT INTO reserved_readers (reader, note, ts) VALUES (?,?,?) "
                "ON CONFLICT(reader) DO UPDATE SET note=excluded.note", (reader, note, time.time()))
            self.conn.commit()

    def is_reserved(self, reader: str) -> str | None:
        with self._lock:
            row = self.conn.execute("SELECT note FROM reserved_readers WHERE reader=?", (reader,)).fetchone()
        return (row["note"] or "reserved") if row else None

    # --- WATCHER-ONLY (a role never calls these): drain new messages for a reader, advance cursor ---
    def drain(self, reader: str, channels: list[str], *, reserved_ok: bool = False) -> list[Message]:
        """Return messages on `channels` newer than `reader`'s cursor, and ADVANCE the cursor.
        A reader never sees its OWN posts (no echo). Called by the watcher, never by the role.
        A RESERVED reader (a wake cursor) refuses to drain unless reserved_ok=True — the caller
        proving it IS the owner of that cursor, not a hand-run command."""
        note = self.is_reserved(reader)
        if note and not reserved_ok:
            raise PermissionError(
                f"reader {reader!r} is RESERVED ({note}) — draining it by hand poisons the wake "
                f"cursor. Read the thread with `bus tail` or a different reader id.")
        out: list[Message] = []
        with self._lock:
            for ch in channels:
                last = self.conn.execute(
                    "SELECT last_seq FROM cursors WHERE reader=? AND channel=?",
                    (reader, ch)).fetchone()
                last_seq = last["last_seq"] if last else 0
                rows = self.conn.execute(
                    "SELECT * FROM messages WHERE channel=? AND seq>? AND sender!=? ORDER BY seq",
                    (ch, last_seq, reader)).fetchall()
                max_seq = last_seq
                for r in rows:
                    out.append(Message(r["seq"], r["channel"], r["sender"], r["body"],
                                       json.loads(r["meta"] or "{}"), r["ts"]))
                    max_seq = max(max_seq, r["seq"])
                # advance the cursor past everything that EXISTS on the channel now (so own-posts and
                # filtered rows don't re-surface), not just past what we returned.
                tip = self.conn.execute(
                    "SELECT COALESCE(MAX(seq),0) AS m FROM messages WHERE channel=?", (ch,)).fetchone()["m"]
                new_last = max(max_seq, tip)
                self.conn.execute(
                    "INSERT INTO cursors (reader, channel, last_seq) VALUES (?,?,?) "
                    "ON CONFLICT(reader, channel) DO UPDATE SET last_seq=excluded.last_seq",
                    (reader, ch, new_last))
            self.conn.commit()
        out.sort(key=lambda m: m.seq)
        return out

    def channels(self) -> list[str]:
        with self._lock:
            return [r["channel"] for r in self.conn.execute(
                "SELECT DISTINCT channel FROM messages ORDER BY channel").fetchall()]

    def history(self, channel: str | None = None) -> list[Message]:
        with self._lock:
            if channel:
                rows = self.conn.execute(
                    "SELECT * FROM messages WHERE channel=? ORDER BY seq", (channel,)).fetchall()
            else:
                rows = self.conn.execute("SELECT * FROM messages ORDER BY seq").fetchall()
        return [Message(r["seq"], r["channel"], r["sender"], r["body"],
                        json.loads(r["meta"] or "{}"), r["ts"]) for r in rows]

    def stats(self) -> dict:
        with self._lock:
            total = self.conn.execute("SELECT COUNT(*) AS c FROM messages").fetchone()["c"]
            per = {r["channel"]: r["c"] for r in self.conn.execute(
                "SELECT channel, COUNT(*) AS c FROM messages GROUP BY channel ORDER BY c DESC").fetchall()}
        return {"total": total, "per_channel": per}

    def close(self):
        self.conn.close()
