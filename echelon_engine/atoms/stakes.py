"""stakes.py — the SESSION STAKES METER + the committed-but-unwrapped ORPHAN DETECTOR.

WHY THIS EXISTS
---------------
Two laws in the ECHELON spine were unimplementable because neither had an anchor in
anything measurable (both found by the 0GAP audit of the substrate's own control flow):

  * LAW 2 "ceremony is proportional to stakes" (SMALL / MEDIUM / LOAD-BEARING) had 59
    prose mentions across the engine and ZERO implementing code. The tier was a thing the
    model was TOLD to hold in context — which is why the audit graded that step's receipt
    `prose` (self-attestation, the weakest of six grades).
  * STOPPED-UNWRAPPED was reachable from every node with zero guards, because nothing
    could DETECT an unwrapped session after the fact.

The owner's move (2026-08-17) collapses both into one mechanism: stop DECLARING stakes and
MEASURE them, and put the session id in the commit trailer so an id that appears in commits
but never in an arc-card is a detectable orphan.

THE HONEST CLAIM — READ THIS BEFORE TRUSTING THE DETECTOR
---------------------------------------------------------
The council's honesty seat corrected an overclaim in the design brief, and the correction
is load-bearing: **`git log` does NOT prove a session was unwrapped.** It proves COMMIT
PRESENCE, not SESSION COMPLETENESS. A session can wrap without committing, or commit
without wrapping. So what this detector actually proves is the strictly narrower claim:

    COMMITTED BUT NEVER WRAPPED

Sessions that changed no files are INVISIBLE to it. That is exactly why the meter carries a
BANK-WRITE / decisions-reached floor — a research session that banked atoms and made no
commit still scores real stakes. Do not let this module's output be read as "every
unwrapped session", because it is not.

SCORING (settled by council 2026-08-17; see 0GAP/STAKES_METER_DECISION.md)
--------------------------------------------------------------------------
  1. OUTWARD-ACT OVERRIDE — any outward act (deploy / restart / publish / push) sets
     LOAD-BEARING regardless of every other signal. Irreversibility is a VETO, not a vote.
  2. BANK-WRITE / DECISIONS FLOOR — atoms written or a fork settled scores real stakes with
     zero commits. Uses the mutation class the lifecycle graph already tags.
  3. GIT FACTS ARE A CAPPED TIE-BREAKER — they separate SMALL from MEDIUM and can NEVER
     reach LOAD-BEARING alone. Line counts are LOG-TRANSFORMED and HARD-CAPPED, and commit
     count / file count weigh more than line count.

     WHY line count is demoted, from real measured data (2026-08-16): a 29,142-insertion /
     42-file commit was a FILE MOVE (vaulting an existing package) — near-zero intellectual
     stakes. A 34-insertion commit closed a real correctness gap in a prover. Raw line count
     INVERTS true stakes. Any meter that weights diff size linearly measures the wrong thing.

WHAT THIS METER CANNOT DO (measured, not speculated)
-----------------------------------------------------
On the calibration pair above, a 34-line commit that closed a REAL correctness gap and an
8-line docs typo-fix score 2.96 and 2.93 — indistinguishable. **Nothing in git separates
them.** That is not a calibration bug to be tuned away; it is the honest ceiling of
git-only signal. The meter measures BLAST RADIUS (how much was touched, how irreversibly),
never IMPORTANCE (whether it mattered). Blast radius is a usable proxy for "would losing
this session's lessons hurt", and it is emphatically not a code-quality or value judgment.

This is precisely why the tier is ADVISORY, and why the BANK-WRITE floor exists: a session
that reached a real conclusion says so by BANKING it, which is a signal the model emits
deliberately rather than one git can infer.

TEETH: ADVISORY ONLY. This module reports and nags. It does not block. Enforcement waits
until the meter has been watched scoring real sessions — the council named miscalibration
the #1 failure mode of every option considered.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import subprocess
import time

FACTS_DIR = os.path.expanduser("~/.echelon/session_facts")
TRAILER_KEY = "Echelon-Session"

# Tier thresholds over the capped git score. Deliberately coarse: a meter that pretends to
# more precision than its inputs support is the "false confidence" failure mode.
MEDIUM_AT = 3.0

# Hard caps so no single dimension can dominate (the line-count inversion guard).
#
# CALIBRATED against the real inversion pair (2026-08-16), not chosen by feel:
#   a 29,142-line / 42-file FILE MOVE   must NOT outrank
#   a     34-line /  1-file correctness fix
# by more than the commit term, because breadth-of-diff is genuinely weak evidence of
# stakes. So `lines` is capped BELOW one commit's worth and `files` only modestly above
# it: a bulk move can still register as "broad", but it can never dominate the score.
# Raising CAP_LINES re-introduces the exact inversion this module exists to prevent.
CAP_LINES = 0.8
CAP_FILES = 1.5
CAP_COMMITS = 4.0


# ───────────────────────────────────────────────────────────────────── facts
def load_all_facts(max_age_sec: int = 60 * 60 * 24 * 30) -> list[dict]:
    """Every session-facts file young enough to matter. Fails open to []."""
    out = []
    try:
        if not os.path.isdir(FACTS_DIR):
            return []
        now = time.time()
        for fn in os.listdir(FACTS_DIR):
            if not fn.endswith(".json"):
                continue
            p = os.path.join(FACTS_DIR, fn)
            try:
                if now - os.path.getmtime(p) > max_age_sec:
                    continue
                with open(p, encoding="utf-8") as f:
                    d = json.load(f)
                if isinstance(d, dict):
                    out.append(d)
            except Exception:
                continue
    except Exception:
        return []
    return out


def current_facts() -> dict | None:
    """The most recently updated session-facts file — this session, in practice."""
    fs = load_all_facts(max_age_sec=60 * 60 * 12)
    return max(fs, key=lambda d: d.get("updated", 0)) if fs else None


# ───────────────────────────────────────────────────────────────────── scoring
def score_session(facts: dict, git: dict | None = None) -> dict:
    """Score ONE session. Returns {tier, score, why:[...], outward:{...}}.

    The order below IS the decision procedure: override first, floor second, tie-break last.
    """
    why: list[str] = []
    git = git or {}

    # 1 ── OUTWARD-ACT OVERRIDE (veto, not vote)
    outward = {k: int(facts.get(k, 0)) for k in ("deploys", "restarts", "publishes", "pushes")
               if int(facts.get(k, 0)) > 0}
    if outward:
        detail = ", ".join(f"{k}={v}" for k, v in sorted(outward.items()))
        why.append(f"OUTWARD ACT ({detail}) — irreversible, overrides all other signals")
        return {"tier": "LOAD-BEARING", "score": None, "why": why, "outward": outward}

    # 2 ── BANK-WRITE / DECISIONS FLOOR (the no-git floor)
    banked = int(facts.get("atoms_written", 0)) + int(facts.get("bank_writes", 0))
    decisions = int(facts.get("decisions", 0)) + int(facts.get("councils", 0))
    if banked or decisions:
        why.append(f"BANK-WRITE/decisions floor (atoms={banked}, decisions={decisions}) "
                   f"— real stakes with no commits required")
        return {"tier": "MEDIUM", "score": None, "why": why, "outward": {}}

    # 3 ── GIT TIE-BREAKER (capped; can never reach LOAD-BEARING)
    commits = int(git.get("commits", 0))
    files = int(git.get("files", 0))
    lines = int(git.get("lines", 0))

    s_commits = min(CAP_COMMITS, float(commits) * 1.5)
    s_files = min(CAP_FILES, math.log10(files + 1) * 2.2)
    s_lines = min(CAP_LINES, math.log10(lines + 1) * 0.8)   # log + cap: the inversion guard
    score = round(s_commits + s_files + s_lines, 2)

    why.append(f"git tie-breaker: commits={commits} (+{s_commits:.2f}), "
               f"files={files} (+{s_files:.2f}), lines={lines} (+{s_lines:.2f}) "
               f"[log-capped — line count is the weakest term BY DESIGN]")

    if not commits and not int(facts.get("files_written", 0)):
        why.append("no commits, no writes, nothing banked — nothing to lose")
        return {"tier": "SMALL", "score": score, "why": why, "outward": {}}

    tier = "MEDIUM" if score >= MEDIUM_AT else "SMALL"
    if tier == "SMALL":
        why.append(f"score {score} < {MEDIUM_AT} threshold")
    return {"tier": tier, "score": score, "why": why, "outward": {}}


# ───────────────────────────────────────────────────────────────────── git side
def _git(args: list[str], cwd: str | None = None) -> str:
    try:
        r = subprocess.run(["git", *args], cwd=cwd or None, capture_output=True,
                           text=True, encoding="utf-8", errors="replace", timeout=30)
        return r.stdout if r.returncode == 0 else ""
    except Exception:
        return ""


def git_stats_for_session(sid: str, cwd: str | None = None, since: str = "") -> dict:
    """Commit/file/line totals for commits carrying this session's trailer."""
    rng = ["--since", since] if since else []
    out = _git(["log", *rng, f"--grep={TRAILER_KEY}: {sid}", "--pretty=format:%H", "--no-merges"], cwd)
    shas = [l.strip() for l in out.splitlines() if l.strip()]
    files = lines = 0
    for sha in shas:
        st = _git(["show", "--shortstat", "--pretty=format:", sha], cwd)
        m = re.search(r"(\d+) files? changed", st)
        if m:
            files += int(m.group(1))
        for m2 in re.finditer(r"(\d+) (insertions?|deletions?)", st):
            lines += int(m2.group(1))
    return {"commits": len(shas), "files": files, "lines": lines, "shas": shas}


def sessions_in_git(cwd: str | None = None, since: str = "") -> dict[str, int]:
    """Every session id appearing in a commit trailer -> commit count."""
    rng = ["--since", since] if since else []
    out = _git(["log", *rng, "--pretty=format:%B", "--no-merges"], cwd)
    ids: dict[str, int] = {}
    for m in re.finditer(rf"^{TRAILER_KEY}:\s*([0-9a-fA-F-]{{6,}})\s*$", out, re.M):
        sid = m.group(1).strip()
        ids[sid] = ids.get(sid, 0) + 1
    return ids


def wrapped_session_ids(scope: str = "echelon") -> set[str]:
    """Session ids recorded on an arc-card (born_from carries the trailer id)."""
    ids: set[str] = set()
    try:
        import sqlite3
        db = os.path.expanduser("~/.echelon/echelon.db")
        if not os.path.exists(db):
            return ids
        con = sqlite3.connect(db)
        try:
            for (bf,) in con.execute("SELECT born_from FROM cards WHERE born_from IS NOT NULL"):
                for m in re.finditer(r"([0-9a-f]{8,})", str(bf or ""), re.I):
                    ids.add(m.group(1).lower())
        finally:
            con.close()
    except Exception:
        return ids
    return ids


def find_orphans(cwd: str | None = None, since: str = "") -> list[dict]:
    """Session ids that COMMITTED but never reached an arc-card.

    HONEST SCOPE: this is 'committed but never wrapped', NOT 'every unwrapped session'.
    A session that made no commit cannot appear here at all.
    """
    in_git = sessions_in_git(cwd, since)
    wrapped = wrapped_session_ids()
    out = []
    for sid, n in sorted(in_git.items(), key=lambda kv: -kv[1]):
        if sid.lower() in wrapped:
            continue
        out.append({"session_id": sid, "commits": n,
                    "git": git_stats_for_session(sid, cwd, since)})
    return out


# ───────────────────────────────────────────────────────────────────── CLI
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="echelon stakes",
        description="Session stakes meter + committed-but-unwrapped orphan detector (ADVISORY).")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sc = sub.add_parser("score", help="score the current session (or --session <id>)")
    sc.add_argument("--session", default="")
    sc.add_argument("--cwd", default="")
    sc.add_argument("--json", action="store_true")

    orp = sub.add_parser("orphans", help="session ids in commits with no arc-card")
    orp.add_argument("--cwd", default="")
    orp.add_argument("--since", default="")
    orp.add_argument("--json", action="store_true")

    sub.add_parser("id", help="print this session's ECHELON session id (for the commit trailer)")

    a = ap.parse_args(argv)

    if a.cmd == "id":
        f = current_facts()
        sid = (f or {}).get("echelon_session_id", "")
        if not sid:
            print("(no active session id — the session-facts hook has not run yet)")
            return 1
        print(sid)
        return 0

    if a.cmd == "score":
        f = current_facts() or {}
        sid = a.session or f.get("echelon_session_id", "")
        git = git_stats_for_session(sid, a.cwd or None) if sid else {}
        res = score_session(f, git)
        if a.json:
            print(json.dumps({**res, "session_id": sid}, ensure_ascii=False, indent=1))
            return 0
        print(f"SESSION STAKES — {res['tier']}" + (f"  (score {res['score']})"
                                                   if res["score"] is not None else ""))
        print(f"  session: {sid or '(unknown)'}")
        for w in res["why"]:
            print(f"  · {w}")
        print("  [ADVISORY — this meter reports, it does not block]")
        return 0

    if a.cmd == "orphans":
        orphans = find_orphans(a.cwd or None, a.since)
        if a.json:
            print(json.dumps(orphans, ensure_ascii=False, indent=1))
            return 0
        if not orphans:
            print("no orphaned sessions: every session id in the log reached an arc-card")
            return 0
        print(f"ORPHANED SESSIONS — committed but never wrapped ({len(orphans)}):")
        for o in orphans:
            g = o["git"]
            print(f"  {o['session_id']}  commits={g['commits']} files={g['files']} lines={g['lines']}")
        print("\n  NOTE: this is 'committed but never wrapped' — sessions that made no")
        print("  commit are invisible here. `git log` proves commit presence, not session")
        print("  completeness.")
        return 0

    return 2


if __name__ == "__main__":
    raise SystemExit(main())
