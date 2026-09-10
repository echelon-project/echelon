"""ship — GATED auto-deploy of the engine to a remote box (owner 2026-08-02: "if you
already build the versioning detection, then bump it to make sure it synced right?").

THE CONTRACT: a version bump IS the ship trigger. `echelon ship` reconciles the box to
local — but ONLY after the exact tree it is about to install has passed every gate on a
STAGING copy. Any gate fails → nothing on the box is touched.

WHY EACH GATE EXISTS — every one is a failure that ACTUALLY HAPPENED on 2026-08-02, and
each was invisible to the gate before it:

  1. DEPENDENCY ORDER — local `cards.py` imports `lam_for` from `scoring.py`; the box's
     older `scoring.py` lacked it. Copying in arbitrary order = ImportError, engine dead
     on load. Files are ordered so a module's deps land before it.
  2. MISSING MODULE — local `__main__.py` imports `wrap_review`, absent on the box. This
     killed EVERY echelon verb. `py_compile` PASSED. Importing the changed module alone
     PASSED. Only RUNNING A VERB caught it → gate 4 exists because gates 2-3 are not enough.
  3. COMPILE + FULL IMPORT CHAIN on the target venv (never locally: box4 is python 3.10.12,
     local is 3.14 — `Connection.serialize()` exists here and not there).
  4. VERB EXECUTION against a SNAPSHOT of the real bank — the only gate that catches a
     missing module reachable solely through the CLI route table.
  5. STRICT-SUPERSET check — never overwrite a drifted remote file that holds work local
     lacks (box-local-drift-surgical-patch-not-wholesale).
  6. POST-RESTART HEALTH + AUTO-ROLLBACK — the box reports `active` before the port binds,
     so health is polled through the boot window; a failure restores the backup and restarts.

Staging lives at <remote_tmp>/ship_staging. NOTHING under the live tree is written until
every gate above is green.

RUN IT DIRECTLY WHEN THE TREE IS BROKEN:
    python -X utf8 -m echelon_engine.atoms.ship_cmd
`echelon ship` routes through __main__'s route table, which imports every atoms module — so
a broken file anywhere makes the DEPLOYER itself unrunnable, exactly when you need it. This
module therefore imports ONLY the stdlib and can always be invoked directly. The scheduler
uses the direct form for that reason.
"""
from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
import time
from pathlib import Path

# Modules whose import order matters: a key must land BEFORE its dependents. Derived from
# real breakage, not guessed — extend when a new intra-package dep appears.
_DEP_FIRST = ("scoring.py", "earn_law.py", "cards.py", "export_import.py", "sync_journal.py")

# Verbs the staging gate must be able to RUN. `status` walks the whole route table (this is
# what caught the missing wrap_review); `recall` exercises the hot read path.
_GATE_VERBS = (["status", "--scope", "echelon"], ["recall", "--scope", "echelon", "--warm", "ship gate probe"])


def _run(cmd: list[str], timeout: int = 600) -> tuple[int, str]:
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                       encoding="utf-8", errors="replace")
    return p.returncode, (p.stdout or "") + (p.stderr or "")


def _ssh(host: str, script: str, timeout: int = 600) -> tuple[int, str]:
    return _run(["ssh", "-o", "BatchMode=yes", host, script], timeout=timeout)


def _scp(src: Path, host: str, dest: str, timeout: int = 600) -> tuple[int, str]:
    return _run(["scp", "-q", str(src), f"{host}:{dest}"], timeout=timeout)


# Non-.py files that must travel with the code. pyproject.toml carries the VERSION, which is
# the ship signal itself — without it the box keeps reporting the old version forever and every
# sync re-triggers a ship that changes nothing (an infinite deploy loop). Caught 2026-08-02.
_EXTRA_FILES = ("pyproject.toml",)


def _local_files(root: Path) -> dict[str, str]:
    """Every engine .py (plus _EXTRA_FILES), keyed by repo-relative posix path ->
    sha256 of LF-normalized bytes."""
    import hashlib
    out = {}
    for base in ("echelon_engine", "apps"):
        d = root / base
        if not d.is_dir():
            continue
        for f in sorted(d.rglob("*.py")):
            if "__pycache__" in f.parts:
                continue
            rel = f.relative_to(root).as_posix()
            out[rel] = hashlib.sha256(f.read_bytes().replace(b"\r\n", b"\n")).hexdigest()[:16]
    for extra in _EXTRA_FILES:
        f = root / extra
        if f.is_file():
            out[extra] = hashlib.sha256(f.read_bytes().replace(b"\r\n", b"\n")).hexdigest()[:16]
    return out


def _remote_files(host: str, remote_root: str) -> dict[str, str]:
    extra_cmd = "; ".join(
        f"[ -f {e} ] && printf '%s %s\\n' \"$(python3 -c 'import hashlib,sys;"
        "print(hashlib.sha256(open(sys.argv[1],\"rb\").read().replace(b\"\\r\\n\",b\"\\n\"))"
        f".hexdigest()[:16])' {e})\" {e}" for e in _EXTRA_FILES)
    rc, out = _ssh(host,
                   f"cd {shlex.quote(remote_root)} && ({extra_cmd}); "
                   f"cd {shlex.quote(remote_root)} && find echelon_engine apps -name '*.py' -type f "
                   "-not -path '*__pycache__*' | sort | while read f; do "
                   "printf '%s %s\\n' \"$(python3 -c 'import hashlib,sys;"
                   "print(hashlib.sha256(open(sys.argv[1],\"rb\").read().replace(b\"\\r\\n\",b\"\\n\"))"
                   ".hexdigest()[:16])' \"$f\")\" \"$f\"; done")
    res = {}
    for line in out.splitlines():
        parts = line.split()
        if len(parts) == 2:
            res[parts[1]] = parts[0]
    return res


def _order_for_deploy(rels: list[str]) -> list[str]:
    """Dependency-first ordering (gate 1). Modules others import go first."""
    def key(rel: str):
        name = rel.rsplit("/", 1)[-1]
        try:
            return (_DEP_FIRST.index(name), rel)
        except ValueError:
            return (len(_DEP_FIRST) + (0 if rel.endswith("__init__.py") else 1), rel)
    return sorted(rels, key=key)


def _superset_check(host: str, remote_root: str, rels: list[str], root: Path) -> list[str]:
    """Gate 5: refuse to overwrite a remote file holding defs local lacks."""
    problems = []
    for rel in rels:
        rc, remote = _ssh(host, f"cat {shlex.quote(remote_root + '/' + rel)} 2>/dev/null || true")
        if rc != 0 or not remote.strip():
            continue                      # new file on the box: nothing to lose
        import re
        pat = re.compile(r"^\s*(?:def|class)\s+([A-Za-z_]\w*)", re.M)
        rem = set(pat.findall(remote))
        loc = set(pat.findall((root / rel).read_text(encoding="utf-8", errors="replace")))
        lost = rem - loc
        if lost:
            problems.append(f"{rel}: remote-only defs would be LOST: {sorted(lost)[:6]}")
    return problems


def ship(*, host: str, remote_root: str, root: Path, python: str,
         service: str, dry_run: bool = False, verbose: bool = True) -> dict:
    log = (lambda m: print(m)) if verbose else (lambda m: None)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    staging = f"/tmp/ship_staging_{stamp}"
    backup = f"/root/ship_backup_{stamp}"
    res: dict = {"host": host, "gates": [], "deployed": [], "ok": False, "dry_run": dry_run}

    # ── what differs ─────────────────────────────────────────────────────────
    loc, rem = _local_files(root), _remote_files(host, remote_root)
    drift = sorted([f for f in loc if f not in rem or loc[f] != rem[f]])
    remote_only = sorted([f for f in rem if f not in loc])
    res["drift"] = drift
    res["remote_only"] = remote_only
    if not drift:
        log("  nothing to ship — box is already byte-identical")
        res["ok"] = True
        return res
    log(f"  {len(drift)} file(s) differ; {len(remote_only)} remote-only (never deleted)")

    # ── GATE 5: strict superset ──────────────────────────────────────────────
    lost = _superset_check(host, remote_root, drift, root)
    if lost:
        res["gates"].append(("superset", False, lost))
        log("  GATE FAILED (superset) — remote holds work local lacks:")
        for p in lost:
            log(f"    {p}")
        return res
    res["gates"].append(("superset", True, []))
    log("  [gate] strict-superset: OK")

    ordered = _order_for_deploy(drift)
    if dry_run:
        log("  DRY RUN — would deploy, in dependency order:")
        for r in ordered:
            log(f"    {r}")
        res["ok"] = True
        res["deployed"] = ordered
        return res

    # ── stage (nothing live is touched) ──────────────────────────────────────
    _ssh(host, f"rm -rf {staging} && mkdir -p {staging}")
    for rel in ordered:
        _ssh(host, f"mkdir -p {staging}/{shlex.quote(str(Path(rel).parent.as_posix()))}")
        rc, out = _scp(root / rel, host, f"{staging}/{rel}")
        if rc != 0:
            res["gates"].append(("stage", False, [f"{rel}: {out[:200]}"]))
            log(f"  GATE FAILED (stage) {rel}: {out[:200]}")
            return res
    log(f"  staged {len(ordered)} file(s) -> {staging}")

    # ── GATE 3a: compile every staged file on the TARGET interpreter ─────────
    rc, out = _ssh(host, f"cd {remote_root} && {python} -m py_compile "
                         f"$(find {staging} -name '*.py' | tr '\\n' ' ')")
    ok = rc == 0
    res["gates"].append(("compile", ok, [] if ok else [out[-400:]]))
    log(f"  [gate] compile on target venv: {'OK' if ok else 'FAILED'}")
    if not ok:
        log(f"    {out[-400:]}")
        return res

    # ── apply to a SHADOW tree, gate there, only then promote ────────────────
    shadow = f"/tmp/ship_shadow_{stamp}"
    _ssh(host, f"rm -rf {shadow} && cp -a {remote_root} {shadow} && "
               f"cd {staging} && find . -name '*.py' | while read f; do "
               f"mkdir -p {shadow}/$(dirname $f); cp $f {shadow}/$f; done && "
               f"find {shadow} -name __pycache__ -type d -exec rm -rf {{}} + 2>/dev/null; true")

    # ── GATE 3b: FULL import chain in the shadow tree ────────────────────────
    rc, out = _ssh(host, f"cd {shadow} && {python} -c "
                         "\"import echelon_engine.__main__, echelon_engine.mcp_server; "
                         "from echelon_engine import services; "
                         "from apps.web import app; app.make_app(); print('IMPORT_OK')\"")
    ok = "IMPORT_OK" in out
    res["gates"].append(("import_chain", ok, [] if ok else [out[-500:]]))
    log(f"  [gate] full import chain: {'OK' if ok else 'FAILED'}")
    if not ok:
        log(f"    {out[-500:]}")
        _ssh(host, f"rm -rf {shadow} {staging}")
        return res

    # ── GATE 4: RUN REAL VERBS against a bank snapshot ───────────────────────
    # The gate that catches a module reachable only through the CLI route table
    # (wrap_review, 2026-08-02) — compile and import-of-one-module both missed it.
    snap = f"/tmp/ship_bank_{stamp}"
    _ssh(host, f"mkdir -p {snap} && {python} -c \""
               f"import sqlite3;"
               f"live=sqlite3.connect('file:/root/.echelon/echelon.db?mode=ro',uri=True);"
               f"d=sqlite3.connect('{snap}/echelon.db');live.backup(d);d.close();live.close()\"")
    verb_fail = []
    for verb in _GATE_VERBS:
        rc, out = _ssh(host, f"cd {shadow} && ECHELON_HOME={snap} {python} -X utf8 "
                             f"-m echelon_engine {' '.join(shlex.quote(v) for v in verb)}")
        if rc != 0 or "Traceback" in out:
            verb_fail.append(f"{' '.join(verb)}: {out[-400:]}")
    ok = not verb_fail
    res["gates"].append(("verbs", ok, verb_fail))
    log(f"  [gate] real verbs on a bank snapshot: {'OK' if ok else 'FAILED'}")
    if not ok:
        for v in verb_fail:
            log(f"    {v}")
        _ssh(host, f"rm -rf {shadow} {staging} {snap}")
        return res

    # ── every gate green: back up, promote, restart, verify, rollback on fail ─
    _ssh(host, f"mkdir -p {backup} && cd {remote_root} && "
               + " && ".join(f"(mkdir -p {backup}/$(dirname {r}) && cp {r} {backup}/{r})"
                             for r in ordered))
    _ssh(host, f"{python} -c \"import sqlite3;"
               f"live=sqlite3.connect('file:/root/.echelon/echelon.db?mode=ro',uri=True);"
               f"d=sqlite3.connect('/root/.echelon/echelon.db.pre_ship_{stamp}');"
               f"live.backup(d);d.close();live.close()\"")
    log(f"  backed up {len(ordered)} file(s) + the bank -> {backup}")

    for rel in ordered:
        _ssh(host, f"mkdir -p {remote_root}/$(dirname {rel}) && cp {staging}/{rel} {remote_root}/{rel}")
    _ssh(host, f"cd {remote_root} && find echelon_engine apps -name __pycache__ -type d "
               "-exec rm -rf {} + 2>/dev/null; true")
    res["deployed"] = ordered
    log(f"  promoted {len(ordered)} file(s) to {remote_root}")

    if service:
        _ssh(host, f"systemctl restart {service}")
        healthy = False
        for i in range(15):
            time.sleep(3)
            rc, out = _ssh(host, "curl -s -o /dev/null -w '%{http_code}' "
                                 "http://127.0.0.1:8888/health || true")
            if "200" in out:
                healthy = True
                log(f"  health 200 at t+{(i+1)*3}s")
                break
        res["gates"].append(("post_health", healthy, [] if healthy else ["/health never returned 200"]))
        if not healthy:
            log("  POST-RESTART HEALTH FAILED — ROLLING BACK")
            _ssh(host, f"cd {backup} && find . -name '*.py' | while read f; do "
                       f"cp $f {remote_root}/$f; done; "
                       f"cd {remote_root} && find . -name __pycache__ -type d -exec rm -rf {{}} + 2>/dev/null; "
                       f"systemctl restart {service}")
            res["rolled_back"] = True
            _ssh(host, f"rm -rf {shadow} {staging} {snap}")
            return res

    _ssh(host, f"rm -rf {shadow} {staging} {snap}")
    res["ok"] = True
    res["backup"] = backup
    return res


def _main(argv=None):
    ap = argparse.ArgumentParser(
        prog="echelon ship",
        description="Gated deploy of the engine to a remote box. Every gate runs on a "
                    "staging/shadow copy; the live tree is touched only if all pass.")
    ap.add_argument("--host", default=os.environ.get("ECHELON_SHIP_HOST", "box4"))
    ap.add_argument("--remote-root", default=os.environ.get("ECHELON_SHIP_ROOT", "/opt/echelon"))
    ap.add_argument("--python", default=os.environ.get("ECHELON_SHIP_PYTHON",
                                                       "/opt/echelon/.venv/bin/python"))
    ap.add_argument("--service", default=os.environ.get("ECHELON_SHIP_SERVICE", "echelon-mcp"))
    ap.add_argument("--root", default=None, help="local repo root (default: this engine's repo)")
    ap.add_argument("--dry-run", action="store_true", help="report what would deploy; touch nothing")
    a = ap.parse_args(argv)

    root = Path(a.root) if a.root else Path(__file__).resolve().parent.parent.parent
    print(f"ship: {root} -> {a.host}:{a.remote_root}")
    r = ship(host=a.host, remote_root=a.remote_root, root=root, python=a.python,
             service=a.service, dry_run=a.dry_run)
    print()
    for name, ok, detail in r["gates"]:
        print(f"  gate {name:<14} {'PASS' if ok else 'FAIL'}")
    if r.get("rolled_back"):
        print("\n  ROLLED BACK — the box is on its previous engine.")
        sys.stdout.flush()
        return 2
    if not r["ok"]:
        print("\n  NOT DEPLOYED — a gate failed; the box was not touched.")
        sys.stdout.flush()
        return 1
    print(f"\n  {'would deploy' if a.dry_run else 'deployed'}: {len(r['deployed'])} file(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
