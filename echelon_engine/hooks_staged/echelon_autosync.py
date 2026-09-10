#!/usr/bin/env python
"""PostToolUse hook (Bash matcher) — AUTO HARNESS-SYNC after bank-mutating engine calls,
AND AUTO ATLAS-REGEN after a commit (OPEN-0075).

THIS FILE IS THE SOURCE OF TRUTH from now on (OPEN-0075, 2026-09-07): it used to live only
hand-installed at ~/.claude/hooks/echelon_autosync.py; that copy drifts from this staged one
unless someone remembers to sync it by hand. Edit HERE and reinstall via `install-hooks`.

Trigger 1 (harness-sync): the gap this closes — owner order 2026-08-31 "no more forgetting
to use echelon capabilities when it was automated and served." The projection (MEMORY.md
harness region, atom counts, banners) is DERIVED state; every `ingest` / `reflex compile` /
`cartridge equip|compose` leaves it stale until someone remembers `harness-sync`. Measured
same day: the moderator forgot it 3x in one session. This hook closes the loop mechanically.

Behavior: read the PostToolUse payload; if the Bash command was a bank-mutating echelon
verb (and NOT itself a harness-sync), walk up from cwd to find echelon-harness-contract.json
and run harness-sync against it. Silent on non-match; additionalContext note on success.

Trigger 2 (atlas regen, OPEN-0075): the atlas is DERIVED state too — a stale one answers "no
match" for a symbol born after it was generated (gate finding 2026-09-06: the first live grep
for `incident_resolve` missed because the cards predated it). If the Bash command is a
`git commit` (any flags, not `--dry-run`) and the repo resolved from the payload's cwd already
carries a `docs/c-atlas/define/_meta.json`, regenerate the atlas using EXACTLY the packages
recorded in that file's `packages` list (never the tool's own default package list — a
framework atlas must not be regenerated as if it were the engine). Skipped when the atlas is
already fresh. Never stages or commits the regenerated cards; the operator commits them with
the next change.

SAFETY: PostToolUse stalls the turn — whole body time-boxed (thread daemon, 40s total; the
atlas regen subprocess itself gets a 25s slice of that), ANY failure silent, exit ALWAYS 0.
Log to ~/.echelon/autosync.log for witnessing.
"""
import json
import os
import re
import subprocess
import sys
import threading
import time

PAT = re.compile(
    r"(echelon(_engine)?|echelon\.exe)\s+.*\b(ingest\b|reflex\s+compile\b|cartridge\s+(equip|compose)\b)",
    re.I,
)
COMMIT_PAT = re.compile(r"\bgit\s+commit\b", re.I)
LOG = os.path.expanduser("~/.echelon/autosync.log")
OUT = {}


def _log(msg):
    try:
        from datetime import datetime
        with open(LOG, "a", encoding="utf-8") as f:
            f.write("%s %s\n" % (datetime.now().isoformat(timespec="seconds"), msg))
    except Exception:
        pass


def _find_contract(start):
    d = os.path.abspath(start or os.getcwd())
    for _ in range(8):
        c = os.path.join(d, "echelon-harness-contract.json")
        if os.path.isfile(c):
            return c
        parent = os.path.dirname(d)
        if parent == d:
            return None
        d = parent
    return None


def _harness_sync(cwd, cmd):
    if "harness-sync" in cmd:
        return
    if not PAT.search(cmd):
        return
    contract = _find_contract(cwd)
    if not contract:
        _log("match but no contract from cwd=%s cmd=%.80s" % (cwd, cmd))
        return
    try:
        r = subprocess.run(
            [sys.executable, "-X", "utf8", "-m", "echelon_engine",
             "harness-sync", "--contract", contract],
            capture_output=True, text=True, timeout=35,
        )
        tail = (r.stdout or "").strip().splitlines()[-1:] or [""]
        _log("SYNCED rc=%s %s (after: %.80s)" % (r.returncode, tail[0], cmd))
        OUT["hookSpecificOutput"] = {
            "hookEventName": "PostToolUse",
            "additionalContext": "[echelon-autosync] harness projection refreshed automatically (%s). Do not run harness-sync by hand for this mutation." % tail[0],
        }
    except Exception as e:
        _log("FAIL %r (after: %.80s)" % (e, cmd))


def _repo_root(cwd):
    try:
        r = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=cwd, capture_output=True, text=True, timeout=5,
        )
        if r.returncode == 0:
            return r.stdout.strip()
    except Exception:
        pass
    return None


def _acquire_lock(lock_path, stale_after=120):
    try:
        if os.path.isfile(lock_path):
            age = time.time() - os.path.getmtime(lock_path)
            if age < stale_after:
                return False
        with open(lock_path, "w", encoding="utf-8") as f:
            f.write(str(os.getpid()))
        return True
    except Exception:
        return False


def _release_lock(lock_path):
    try:
        os.remove(lock_path)
    except Exception:
        pass


def _atlas_regen(cwd, cmd):
    if not COMMIT_PAT.search(cmd):
        return
    if re.search(r"--dry-run\b", cmd):
        return
    root = _repo_root(cwd)
    if not root:
        return
    define_dir = os.path.join(root, "docs", "c-atlas", "define")
    meta_path = os.path.join(define_dir, "_meta.json")
    if not os.path.isfile(meta_path):
        return  # non-graphed repo: silent, no log line
    lock_path = os.path.join(define_dir, ".regen.lock")
    if not _acquire_lock(lock_path):
        _log("SKIP atlas regen for %s: locked" % root)
        return
    try:
        try:
            meta = json.loads(open(meta_path, "r", encoding="utf-8").read()) or {}
        except Exception:
            _log("FAIL atlas regen for %s: unreadable _meta.json" % root)
            return
        pkgs = meta.get("packages") or []
        if not pkgs:
            _log("SKIP atlas regen for %s: _meta.json has no packages" % root)
            return
        try:
            from echelon_engine.atoms import cgraph
            st = cgraph.staleness(define_dir)
        except Exception:
            st = {"newer": -1}
        if st.get("newer", -1) == 0:
            _log("fresh atlas for %s, nothing to do" % root)
            return
        argv = [sys.executable, "-X", "utf8", "-m", "echelon_engine.gen_code_atlas",
                "--root", root, "--out", "docs/c-atlas"]
        for pkg in pkgs:
            argv += ["--pkg", pkg]
        try:
            r = subprocess.run(argv, capture_output=True, text=True, timeout=25)
            was_stale = st.get("newer", 0)
            _log("REGEN rc=%s root=%s pkgs=%s (was_stale=%s)" % (r.returncode, root, pkgs, was_stale))
            if r.returncode == 0:
                new_st = cgraph.staleness(define_dir)
                OUT["hookSpecificOutput"] = {
                    "hookEventName": "PostToolUse",
                    "additionalContext": "[echelon-atlas] regenerated %d cards for %s (was %s stale); uncommitted" % (
                        meta.get("node_count") or 0, root, was_stale),
                }
        except subprocess.TimeoutExpired:
            _log("TIMEOUT atlas regen for %s after 25s" % root)
        except Exception as e:
            _log("FAIL atlas regen for %s: %r" % (root, e))
    finally:
        _release_lock(lock_path)


def _body():
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return
    if payload.get("tool_name") != "Bash":
        return
    cmd = (payload.get("tool_input") or {}).get("command", "") or ""
    cwd = payload.get("cwd") or os.getcwd()
    _harness_sync(cwd, cmd)
    if "hookSpecificOutput" not in OUT:
        _atlas_regen(cwd, cmd)


if __name__ == "__main__":
    t = threading.Thread(target=_body, daemon=True)
    t.start()
    t.join(40)
    if OUT:
        print(json.dumps(OUT))
    sys.exit(0)
