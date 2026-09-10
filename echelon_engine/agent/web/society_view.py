"""society_view.py — read a society run's REAL state for the web viewer.

The society (eval/society.py) writes its whole life to a run dir under eval/_out/society_<ts>/:
  bus.db          — the append-only bus: every message, FULL untruncated body (seq/channel/sender/body/ts)
  findings.jsonl  — the L2 insights + brand iterations + reviews
  events.jsonl    — role-round events (status, answer)
  summary.json    — the final tally (cost, soul growth, git, brand_best)
  cloud_soul.db   — the grown cloud soul (promoted CVs + L2 candidates)
  roles/<role>/   — each device's own core.db + bank.db + log.jsonl

This module turns that on-disk truth into the JSON the viewer renders. It is READ-ONLY — it never
writes a run dir (viewing a society must not perturb it). The viewer shows the FULL body straight from
bus.db (answering the truncation question: the log/console truncates for readability; the bus stores
everything, and the viewer reads the bus).

A run is LIVE if its bus.db was modified within ~12s (a society loop is actively posting). Else it's a
finished/static run you can still browse. Mirrors the bridge-liveness idea in server.py.
"""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

# Society run dirs live under the substrate home (~/.echelon/runs/society/<ts>/), NOT in the repo —
# centralised 2026-06-19 so a run never dirties the engine tree (the legacy path was <repo>/eval/_out).
# Override with ECHELON_SOCIETY_OUT for an alternate location.
import os as _os
from echelon_sdk.paths import RUNS as _RUNS
_OUT = Path(_os.environ.get("ECHELON_SOCIETY_OUT") or (_RUNS / "society"))

# stable accent per role so a lane keeps its color across frames (Flux agents-board style).
_ACCENTS = {
    "creator": "#ff6b35", "reviewer": "#ff78b9", "public": "#9b8cff", "supporter": "#7af3b0",
    "architect": "#35d0ff", "dev": "#ffd23f", "designer": "#ff9ed2", "tester": "#5ee6c4",
    "integrator": "#c0a3ff", "brand_strategist": "#ffb86b", "prompt_engineer": "#8be9fd",
    "art_director": "#f1fa8c", "auditor": "#a0a8bd", "partner": "#ffffff",
}
_DEFAULT_ACCENT = "#868ea2"
_LIVE_WINDOW_S = 12.0


def _accent(role: str) -> str:
    return _ACCENTS.get(role, _DEFAULT_ACCENT)


def run_dirs() -> list[Path]:
    """All society run dirs, newest first."""
    if not _OUT.exists():
        return []
    return sorted((d for d in _OUT.iterdir() if d.is_dir() and (d / "bus.db").exists()),
                  key=lambda d: (d.stat().st_mtime_ns, d.name), reverse=True)


def resolve_run(name: str | None) -> Path | None:
    """The run to read. Explicit name wins (the UI's run picker); else the newest run."""
    if name:
        p = Path(name)
        if not p.is_absolute():
            p = _OUT / name
        return p if (p / "bus.db").exists() else None
    dirs = run_dirs()
    return dirs[0] if dirs else None


def is_live(run: Path) -> bool:
    """LIVE = the bus.db moved within the live window (a society loop is posting right now)."""
    bus = run / "bus.db"
    try:
        return bus.exists() and (time.time() - bus.stat().st_mtime) <= _LIVE_WINDOW_S
    except OSError:
        return False


def _bus_conn(run: Path) -> sqlite3.Connection:
    # read-only URI so viewing never locks/writes the society's bus (it may be live).
    uri = f"file:{(run / 'bus.db').as_posix()}?mode=ro"
    c = sqlite3.connect(uri, uri=True, timeout=2.0)
    c.row_factory = sqlite3.Row
    return c


def list_runs() -> dict:
    """The run picker: each run with its headline stats + live flag."""
    runs = []
    for d in run_dirs():
        meta = {"name": d.name, "live": is_live(d), "messages": 0, "mtime": round(d.stat().st_mtime, 0)}
        sj = d / "summary.json"
        if sj.exists():
            try:
                s = json.loads(sj.read_text(encoding="utf-8"))
                meta["messages"] = s.get("bus_stats", {}).get("total", 0)
                meta["findings"] = s.get("findings", 0)
                meta["deepseek_spent"] = s.get("deepseek_spent", 0.0)
            except Exception:
                pass
        # if no summary yet (a live run mid-flight), count straight from the bus
        if not meta.get("messages"):
            try:
                with _bus_conn(d) as c:
                    meta["messages"] = c.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
            except Exception:
                pass
        runs.append(meta)
    return {"runs": runs, "active": runs[0]["name"] if runs else None}


def messages(run: Path, since: int = 0, limit: int = 5000) -> list[dict]:
    """Every bus message with seq > since, FULL body. Newest reads are cheap (indexed on seq)."""
    out = []
    try:
        with _bus_conn(run) as c:
            rows = c.execute(
                "SELECT seq, channel, sender, body, ts FROM messages WHERE seq > ? "
                "ORDER BY seq ASC LIMIT ?", (since, limit)).fetchall()
        for r in rows:
            out.append({"seq": r["seq"], "channel": r["channel"], "sender": r["sender"],
                        "body": r["body"], "ts": r["ts"], "accent": _accent(r["sender"])})
    except sqlite3.Error:
        pass
    return out


def _channel_stats(run: Path) -> list[dict]:
    try:
        with _bus_conn(run) as c:
            rows = c.execute("SELECT channel, COUNT(*) n, MAX(seq) last FROM messages "
                             "GROUP BY channel ORDER BY n DESC").fetchall()
        return [{"channel": r["channel"], "count": r["n"], "last_seq": r["last"]} for r in rows]
    except sqlite3.Error:
        return []


def _lanes(run: Path) -> list[dict]:
    """One lane per ROLE (Flux agents-board): its last message, post count, last-active ts, accent.
    The lane is how you watch a role 'live' — its text is its most recent bus post."""
    lanes: dict[str, dict] = {}
    try:
        with _bus_conn(run) as c:
            rows = c.execute(
                "SELECT sender, channel, body, seq, ts FROM messages ORDER BY seq ASC").fetchall()
    except sqlite3.Error:
        return []
    for r in rows:
        s = r["sender"]
        ln = lanes.setdefault(s, {"id": s, "name": s, "accent": _accent(s),
                                  "turns": 0, "text": "", "channel": "", "last_seq": 0, "ts": 0.0})
        ln["turns"] += 1
        ln["text"] = r["body"]            # FULL last message (the viewer may clamp height in CSS, not truncate)
        ln["channel"] = r["channel"]
        ln["last_seq"] = r["seq"]
        ln["ts"] = r["ts"]
    # status: a lane that posted within the live window is "active", else "idle"
    now = time.time()
    for ln in lanes.values():
        ln["status"] = "active" if (now - ln["ts"]) <= _LIVE_WINDOW_S else "idle"
    return sorted(lanes.values(), key=lambda x: x["last_seq"], reverse=True)


def _findings(run: Path, limit: int = 200) -> dict:
    """The L2 insights the society produced: text + up_votes + confirmed. Plus the brand iterations."""
    fj = run / "findings.jsonl"
    items, brand = [], []
    confirmed = 0
    if fj.exists():
        try:
            for ln in fj.read_text(encoding="utf-8").splitlines():
                ln = ln.strip()
                if not ln:
                    continue
                r = json.loads(ln)
                if r.get("type") == "brand_iteration":
                    brand.append({"iter": r.get("iter"), "score": r.get("score"),
                                  "png": r.get("png"), "critique": r.get("critique", "")})
                else:
                    confirmed += int(bool(r.get("confirmed")))
                    items.append({"type": r.get("type"), "role": r.get("role"),
                                  "text": r.get("text", ""), "up_votes": r.get("up_votes", 0),
                                  "confirmed": bool(r.get("confirmed")), "seq": r.get("seq")})
        except Exception:
            pass
    items.sort(key=lambda x: (-x["up_votes"], -(x["seq"] or 0)))
    return {"items": items[:limit], "confirmed": confirmed, "total": len(items),
            "brand": sorted(brand, key=lambda x: x.get("iter") or 0)}


def _soul(run: Path) -> dict:
    """The cloud soul growth: promoted CVs (the earned canon) + count of L2 candidates added."""
    sp = run / "cloud_soul.db"
    cvs, l2 = [], 0
    if sp.exists():
        try:
            uri = f"file:{sp.as_posix()}?mode=ro"
            c = sqlite3.connect(uri, uri=True, timeout=2.0); c.row_factory = sqlite3.Row
            tables = [r[0] for r in c.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'core_%' "
                "OR name LIKE 'working_%'").fetchall()]
            for t in tables:
                try:
                    if t.startswith("core_"):
                        for r in c.execute(f"SELECT content FROM {t} WHERE kind='cv'").fetchall():
                            cvs.append(r["content"])
                    else:
                        l2 += c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                except sqlite3.OperationalError:
                    continue
            c.close()
        except sqlite3.Error:
            pass
    return {"cvs": cvs, "cv_count": len(cvs), "l2_candidates": l2}


def overview(run: Path) -> dict:
    """The full society snapshot for a run: lanes + channels + findings + soul + cost + brand + git."""
    summary = {}
    sj = run / "summary.json"
    if sj.exists():
        try:
            summary = json.loads(sj.read_text(encoding="utf-8"))
        except Exception:
            summary = {}
    try:
        with _bus_conn(run) as c:
            total = c.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
            max_seq = c.execute("SELECT COALESCE(MAX(seq),0) FROM messages").fetchone()[0]
    except sqlite3.Error:
        total, max_seq = 0, 0
    fnd = _findings(run)
    return {
        "run": run.name,
        "live": is_live(run),
        "lanes": _lanes(run),
        "channels": _channel_stats(run),
        "findings": fnd,
        "soul": _soul(run),
        "totals": {
            "messages": total, "max_seq": max_seq,
            "findings": fnd["total"], "confirmed": fnd["confirmed"],
            "deepseek_spent": summary.get("deepseek_spent", 0.0),
            "deepseek_pool": summary.get("deepseek_pool", 0.0),
            "xai_spent": summary.get("xai_spent", 0.0),
            "partner_consults": summary.get("partner_consults", 0),
            "cloud_cvs": summary.get("cloud_cvs", 0),
            "l2_added": summary.get("l2_insights_added", 0),
            "growing_real_soul": summary.get("growing_real_soul", False),
        },
        "brand_best": summary.get("brand_best"),
        "git": summary.get("git", {}),
    }
