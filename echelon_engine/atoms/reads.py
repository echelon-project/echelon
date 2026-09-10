"""reads.py — ranked / coordinate-scoped READ helpers over UAME (the harvest-derived endpoints).

The session raw-query census (tool_harvest) surfaced `SELECT * FROM atoms WHERE score>=? ORDER BY
score DESC` and coordinate-prefix reads as un-toolified raw access. These are the public READ tools
that retire them — by the human axes (domain x min_score x coordinate-prefix), table names resolved
internally, honoring the soul wall (core_*/working_* only; never bank_*).

BUILT BY THE SWARM (2026-06-07c): T1 compiled this content, the T3 code-floor wrote the file ($0,
no driver). The first endpoints the agent built for itself via the /echelon-swarm path.
See harvest-raw-access-into-endpoints, never-raw-sql-the-soul.
"""
from __future__ import annotations

from .uame import UAME, Entry


def _row_to_entry(r, tbl):
    keys = r.keys()
    return Entry(
        id=r["id"], content=r["content"], kind=r["kind"], coordinate=r["coordinate"],
        scope=r["scope"], supersedes=r["supersedes"], ts=r["ts"],
        domain=tbl.split("_", 1)[1], permanent=tbl.startswith("core_"),
        ttl=r["ttl"] if "ttl" in keys else 0,
        valence=r["valence"], arousal=r["arousal"], score=r["score"],
        recall_count=r["recall_count"],
        self_seed=bool(r["self_seed"]) if "self_seed" in keys else False)


def top(u, *, domain=None, min_score=0.0, limit=20, kind=None):
    """Highest-scoring SOUL seeds, score-descending — the ranked read the census found hand-rolled.
    Human axes; soul-wall honored (core_*/working_* only). domain=None -> all domains, merged + re-ranked."""
    with u._lock:
        if domain is not None:
            tbls = [t for t in (f"core_{domain}", f"working_{domain}")
                    if u.conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                                      (t,)).fetchone()]
        else:
            tbls = u._all_tables()
        hits = []
        for t in tbls:
            q = f"SELECT * FROM {t} WHERE score>=?"
            params = (min_score,)
            if kind is not None:
                q += " AND kind=?"; params = (min_score, kind)
            for r in u.conn.execute(q, params).fetchall():
                hits.append(_row_to_entry(r, t))
    hits.sort(key=lambda e: (e.score, e.ts), reverse=True)
    return hits[:limit]


def at_coordinate(u, prefix, *, limit=20):
    """SOUL seeds whose coordinate is AT or UNDER a prefix (segment-aware), score-descending."""
    pfx = ":".join(s.strip().lower() for s in (prefix or "").split(":") if s.strip())
    with u._lock:
        out = []
        for t in u._all_tables():
            for r in u.conn.execute(f"SELECT * FROM {t}").fetchall():
                coord = (r["coordinate"] or "").lower()
                if not pfx or coord == pfx or coord.startswith(pfx + ":"):
                    out.append(_row_to_entry(r, t))
    out.sort(key=lambda e: (e.score, e.ts), reverse=True)
    return out[:limit]
