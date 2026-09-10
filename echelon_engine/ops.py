"""echelon ops — the devops verbs (ESTATE-CRAFT-CHARTER Part 1).

A runbook that burned us twice stops being prose and becomes CODE with its gates
baked in. Every run appends one receipt to the room journal. Verbs are
estate-parameterized: `--estate <name>` resolves host/unit/paths from the room's
`services` rows (room.json), never hardcoded.

Exit codes (the two-free-gates law): 0 ok/PASS · 1 FAIL · 2 ABSTAIN/SUSPECT · 3 REFUSED.

Verbs (step 1 + 3 of the build order): `estate add|list`, `probe`, `shot`, `deploy`.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import re
import shlex
import struct
import subprocess
import sys
import zlib
from datetime import datetime, timezone
from pathlib import Path

from echelon_engine import workcycle

PROBE_HEADER = re.compile(r"^PROBE driver=(\S+) db=(\S+)$")
KEEP_RELEASES = 3
SUSPECT_MARKERS = ("cannot get", "internal server error", "502 bad gateway", "504 gateway",
                   "application error", "not found", "traceback", "econnrefused")


# ── plumbing ─────────────────────────────────────────────────────────────────

def _run(cmd: list[str], *, timeout: int = 120) -> tuple[int, str, str]:
    """The one process door — tests monkeypatch this; nothing else spawns."""
    p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=timeout)
    return p.returncode, p.stdout, p.stderr


def _ssh(host: str, script: str, *, timeout: int = 120) -> tuple[int, str, str]:
    return _run(["ssh", "-o", "BatchMode=yes", host, "bash", "-s"], timeout=timeout) if False else \
        _run(["ssh", "-o", "BatchMode=yes", host, script], timeout=timeout)


def _room() -> Path:
    room = workcycle.room_path()
    if room is None:
        raise SystemExit("ROOM unavailable (no room — run `workcycle init` at the repo root)")
    return room


def _receipt(room: Path, verb: str, **data) -> dict:
    return workcycle.journal(room, "ops", {"verb": verb, **data})


def estates(room: Path) -> dict[str, dict]:
    rj = workcycle._read_json(room / "room.json", {})
    return {s["name"]: s for s in (rj.get("services") or []) if s.get("name")}


def estate(room: Path, name: str) -> dict:
    e = estates(room).get(name)
    if not e:
        raise SystemExit(f"REFUSED: estate '{name}' is not in room.json services "
                         f"(have: {', '.join(estates(room)) or '-'}) — `ops estate add`")
    return e


def add_estate(room: Path, row: dict, *, derive: bool = False) -> dict:
    """Register a service row. `derive` reads the LIVE unit (`systemctl cat`) for
    EnvironmentFile / WorkingDirectory — derive from the live thing, never guess."""
    if derive and row.get("host") and row.get("unit"):
        rc, out, _ = _ssh(row["host"], f"systemctl cat {shlex.quote(row['unit'])}")
        if rc != 0:
            raise SystemExit(f"REFUSED: cannot read unit {row['unit']} on {row['host']} (rc={rc})")
        for line in out.splitlines():
            k, _, v = line.strip().partition("=")
            if k == "EnvironmentFile" and not row.get("env_file"):
                row["env_file"] = v.lstrip("-")
            if k == "WorkingDirectory" and not row.get("cwd"):
                row["cwd"] = v
    rj = workcycle._read_json(room / "room.json", {})
    services = [s for s in (rj.get("services") or []) if s.get("name") != row["name"]]
    services.append(row)
    rj["services"] = services
    workcycle._write_json(room / "room.json", rj)
    _receipt(room, "estate-add", estate=row["name"], host=row.get("host"), unit=row.get("unit"))
    return row


# ── ops probe ────────────────────────────────────────────────────────────────

PROBE_JS = r"""
const BASE = process.cwd();
const {loadBroker} = await import(BASE + "/" + %(secrets)s);
await loadBroker();
const {db} = await import(BASE + "/" + %(knex)s);
const drv = db.client.config.client;
const cur = await db.raw("select current_database() as d");
const dbname = (cur.rows ? cur.rows[0].d : (cur[0] && cur[0].d)) || "?";
console.log("PROBE driver=" + drv + " db=" + dbname);
%(body)s
await db.destroy();
"""


def probe_script(e: dict, sql: str, *, write: bool = False) -> str:
    """The remote command: source the unit env, cd to the service, boot the broker
    BEFORE the db import, print the header, then the query. Read-only unless --write."""
    if not write and re.match(r"\s*(insert|update|delete|drop|alter|truncate|create)\b", sql, re.I):
        raise SystemExit("REFUSED: write statement without --write --reason")
    body = ('const r = await db.raw(%s);\n'
            'console.log(JSON.stringify(r.rows !== undefined ? r.rows : r, null, 1));'
            % json.dumps(sql))
    js = PROBE_JS % {"secrets": json.dumps(e.get("probe_secrets", "framework/secrets.js")),
                     "knex": json.dumps(e.get("probe_db", "framework/knex.js")), "body": body}
    parts = []
    if e.get("env_file"):
        parts.append(f"set -a && . {shlex.quote(e['env_file'])} && set +a")
    parts.append(f"cd {shlex.quote(e.get('cwd') or '.')}")
    parts.append(f"node --input-type=module -e {shlex.quote(js)}")
    return " && ".join(parts)


def probe_validate(out: str) -> tuple[bool, str]:
    """The gate: no number is printed unless the driver+database header came first."""
    first = (out.strip().splitlines() or [""])[0]
    m = PROBE_HEADER.match(first)
    if not m:
        return False, "REFUSED to print: output lacks the `PROBE driver= db=` header " \
                      "(broker/env not booted — this is not the service's database)"
    return True, f"driver={m.group(1)} db={m.group(2)}"


def probe(room: Path, name: str, sql: str, *, write: bool = False, reason: str = "",
          dry_run: bool = False) -> int:
    e = estate(room, name)
    if write and not reason:
        print("REFUSED: --write needs --reason"); return 3
    script = probe_script(e, sql, write=write)
    if dry_run:
        print(script); return 0
    rc, out, err = _ssh(e["host"], script)
    ok, why = probe_validate(out)
    _receipt(room, "probe", estate=name, sql=sql[:200], write=write, reason=reason,
             rc=rc, ok=ok, header=why if ok else None)
    if not ok:
        print(why); print(err.strip()[-800:], file=sys.stderr); return 3
    print(out.rstrip())
    if rc != 0:
        print(err.strip()[-800:], file=sys.stderr)
    return 0 if rc == 0 else 1


# ── ops shot ─────────────────────────────────────────────────────────────────

def _png_size(path: Path) -> tuple[int, int]:
    return struct.unpack(">II", path.read_bytes()[16:24])


def suspect(text: str, styled: bool, w: int, h: int, want: tuple[int, int]) -> str | None:
    """SUSPECT = never judged: blank body, error page, unstyled render, wrong size."""
    t = (text or "").strip()
    if (w, h) != want:
        return f"png is {w}x{h}, wanted {want[0]}x{want[1]}"
    if len(t) < 20:
        return "blank body (<20 chars of text)"
    low = t[:2000].lower()
    for m in SUSPECT_MARKERS:
        if m in low:
            return f"error page marker '{m}'"
    if not styled:
        return "unstyled body (no stylesheet rules applied)"
    return None


def render(url: str, out: Path, *, width: int, height: int, cookies: list[dict],
           wait_ms: int = 1500) -> dict:
    from playwright.sync_api import sync_playwright  # lazy — already an estate dep
    with sync_playwright() as p:
        b = p.chromium.launch(headless=True)
        ctx = b.new_context(viewport={"width": width, "height": height}, device_scale_factor=1)
        if cookies:
            ctx.add_cookies(cookies)
        pg = ctx.new_page()
        pg.goto(url, wait_until="load", timeout=30000)
        pg.wait_for_timeout(wait_ms)
        text = pg.evaluate("document.body ? document.body.innerText : ''")
        styled = pg.evaluate("Array.from(document.styleSheets).some(s => { try { return s.cssRules.length > 0 } catch (e) { return true } })")
        pg.screenshot(path=str(out), full_page=False)
        b.close()
    w, h = _png_size(out)
    return {"text": text, "styled": bool(styled), "w": w, "h": h}


def _canary_png() -> bytes:
    """A 64x64 solid red PNG built from zlib alone — the known-answer canary image."""
    raw = b"".join(b"\x00" + b"\xff\x00\x00" * 64 for _ in range(64))
    def chunk(t, d):
        c = t + d
        return struct.pack(">I", len(d)) + c + struct.pack(">I", zlib.crc32(c) & 0xffffffff)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 64, 64, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


def _ask_vision(provider, prompt: str, png: bytes) -> str:
    msg = [{"role": "user", "content": [
        {"type": "text", "text": prompt},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64," + base64.b64encode(png).decode()}}]}]
    r = provider.send(msg, max_tokens=400)
    if getattr(r, "status", "ok") == "error":
        return ""
    return (getattr(r, "content", "") or "").strip()


def judge(png: bytes, brief: str, *, provider=None) -> tuple[str, str]:
    """The $0 minimax gate, canary-first. Returns (PASS|FAIL|ABSTAIN, why).
    A throttled/absent/wrong canary is an ABSTENTION — never a PASS."""
    if provider is None:
        from echelon_engine.atoms.providers.openai_compat import minimax_provider
        provider = minimax_provider()
    c = _ask_vision(provider, "One word: which single color fills this image?", _canary_png())
    if "red" not in c.lower():
        return "ABSTAIN", f"canary failed (answer: {c[:60]!r}) — judge not trusted this round"
    v = _ask_vision(provider, "You are a strict pixel gate. Brief: " + brief +
                    "\nAnswer on the first line exactly PASS or FAIL, then one line why.", png)
    head = (v.splitlines() or [""])[0].strip().upper()
    if head.startswith("PASS"):
        return "PASS", v
    if head.startswith("FAIL"):
        return "FAIL", v
    return "ABSTAIN", f"unparseable verdict: {v[:120]!r}"


def shot(room: Path | None, url: str, out: Path, *, width: int = 390, height: int = 844,
         cookies: list[dict] = (), brief: str | None = None, provider=None) -> int:
    info = render(url, out, width=width, height=height, cookies=list(cookies))
    sus = suspect(info["text"], info["styled"], info["w"], info["h"], (width, height))
    verdict, why = ("SUSPECT", sus) if sus else ("SHOT", f"{info['w']}x{info['h']}")
    if not sus and brief:
        verdict, why = judge(out.read_bytes(), brief, provider=provider)
    if room is not None:
        _receipt(room, "shot", url=url, out=str(out), verdict=verdict, why=why[:300],
                 judged=bool(brief and not sus))
    print(f"{verdict} {out} — {why}")
    return {"SHOT": 0, "PASS": 0, "FAIL": 1}.get(verdict, 2)


# ── ops deploy ───────────────────────────────────────────────────────────────

def release_name(sha: str, now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    return now.strftime("%Y%m%dT%H%M%SZ") + "-" + sha[:8]


def prune_cmd(releases: str, current: str, keep: int = KEEP_RELEASES) -> str:
    """Prune by DIRNAME sort, never mtime (cp -a preserves mtimes, `ls -t` deleted the
    live release 2026-08-26); always exclude the resolved `current` target explicitly."""
    r, c = shlex.quote(releases), shlex.quote(current)
    return (f"live=$(basename \"$(readlink -f {c})\") && cd {r} && "
            f"ls | sort | head -n -{keep} | grep -vx \"$live\" | xargs -r rm -rf && df -h {r} | tail -1")


def deploy_plan(e: dict, *, sha: str, changed: list[str], dist_built: bool,
                tarball: str, now: datetime | None = None) -> list[tuple[str, str]]:
    """The ordered plan: (label, remote-or-local command). Gates are the ORDER."""
    ui_dirs = tuple(e.get("ui_dirs") or ())
    if ui_dirs and any(f.startswith(ui_dirs) for f in changed) and not dist_built:
        raise SystemExit("REFUSED: UI files changed but no fresh dist (--dist-built after building) "
                         "— the deploy-dist trap")
    rel = release_name(sha, now)
    R, cur = e["releases"], e["current"]
    new = f"{R}/{rel}"
    js = [f for f in changed if f.endswith((".js", ".mjs"))]
    plan: list[tuple[str, str]] = []
    if js:
        plan.append(("local:check", " && ".join(f"node --check {shlex.quote(f)}" for f in js)))
    plan.append(("local:scp", f"scp {shlex.quote(tarball)} {e['host']}:/tmp/{rel}.tar.gz"))
    plan.append(("remote:extract", f"mkdir -p {shlex.quote(new)} && tar -xzf /tmp/{rel}.tar.gz -C {shlex.quote(new)} "
                                   f"&& test -e {shlex.quote(new)}/.extracted || touch {shlex.quote(new)}/.extracted"))
    plan.append(("remote:verify-extracted", f"test -f {shlex.quote(new)}/.extracted"))
    plan.append(("remote:flip", f"ln -sfn {shlex.quote(new)} {shlex.quote(cur)} && systemctl restart {shlex.quote(e['unit'])}"))
    health = e.get("health") or ""
    plan.append(("remote:health", f"for i in $(seq 1 {e.get('health_tries', 12)}); do "
                                  f"code=$(curl -s -o /dev/null -w '%{{http_code}}' {shlex.quote(health)}); "
                                  f"[ \"$code\" = 200 ] && echo HEALTH 200 && exit 0; sleep 5; done; echo HEALTH $code; exit 1"))
    plan.append(("remote:readlink", f"test \"$(readlink -f {shlex.quote(cur)})\" = \"$(readlink -f {shlex.quote(new)})\""))
    plan.append(("remote:prune", prune_cmd(R, cur)))
    return plan


def rollback_line(e: dict, previous: str) -> str:
    return (f"ROLLBACK: ssh {e['host']} 'ln -sfn {shlex.quote(previous)} {shlex.quote(e['current'])} "
            f"&& systemctl restart {shlex.quote(e['unit'])}'")


def deploy(room: Path, name: str, *, dist_built: bool = False, dry_run: bool = False,
           repo: Path | None = None) -> int:
    e = estate(room, name)
    for k in ("host", "unit", "releases", "current", "health"):
        if not e.get(k):
            print(f"REFUSED: estate '{name}' lacks '{k}' (ops deploy needs host/unit/releases/current/health)")
            return 3
    if workcycle.room_type(room) == "library":
        print("REFUSED: ops deploy on a `library` room"); return 3
    repo = repo or Path.cwd()
    rc, sha, _ = _run(["git", "-C", str(repo), "rev-parse", "HEAD"])
    sha = sha.strip() or "nosha"
    rc, prev, _ = _ssh(e["host"], f"readlink -f {shlex.quote(e['current'])}") if not dry_run else (0, "", "")
    prev = prev.strip()
    base = e.get("deployed_sha") or ""
    rc, diff, _ = _run(["git", "-C", str(repo), "diff", "--name-only", f"{base}..HEAD"]) if base else (0, "", "")
    changed = [l.strip() for l in diff.splitlines() if l.strip()]
    tarball = str(Path(os.environ.get("TEMP", "/tmp")) / f"{release_name(sha)}.tar.gz")
    plan = deploy_plan(e, sha=sha, changed=changed, dist_built=dist_built, tarball=tarball)
    if dry_run:
        print(f"PLAN {name} @ {sha[:8]} ({len(changed)} changed since {base[:8] or '?'})")
        for label, cmd in plan:
            print(f"  {label:<24} {cmd}")
        if prev:
            print(rollback_line(e, prev))
        return 0
    rc, _, err = _run(["git", "-C", str(repo), "archive", "--format=tar.gz", "-o", tarball, "HEAD"])
    if rc != 0:
        print(f"FAIL git archive: {err.strip()}"); return 1
    result = "ok"
    for label, cmd in plan:
        rc, out, err = _run(["bash", "-c", cmd]) if label.startswith("local:") else _ssh(e["host"], cmd)
        print(f"{label:<24} rc={rc} {out.strip()[-200:]}")
        if rc != 0:
            result = f"FAIL at {label}: {err.strip()[-300:]}"
            print(result)
            if prev:
                print(rollback_line(e, prev))
            break
    _receipt(room, "deploy", estate=name, sha=sha, previous=prev, result=result,
             rollback=rollback_line(e, prev) if prev else None)
    if result == "ok":
        e["deployed_sha"] = sha
        add_estate(room, e)
        print(f"DEPLOYED {name} @ {sha[:8]}"); print(rollback_line(e, prev) if prev else "")
        return 0
    return 1


# ── CLI ──────────────────────────────────────────────────────────────────────

def _cookie(s: str) -> dict:
    """name=value[@domain] — session-cookie injection for authenticated shots."""
    kv, _, domain = s.partition("@")
    k, _, v = kv.partition("=")
    return {"name": k, "value": v, "domain": domain or "127.0.0.1", "path": "/"}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="echelon ops", description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="verb", required=True)
    p = sub.add_parser("estate", help="register / list estate service rows in room.json")
    p.add_argument("action", choices=["add", "list"])
    for k in ("name", "host", "unit", "env-file", "cwd", "health", "base-url", "releases", "current"):
        p.add_argument("--" + k, default=None)
    p.add_argument("--ui-dir", action="append", default=[], help="path prefix whose change needs a fresh dist")
    p.add_argument("--derive", action="store_true", help="read EnvironmentFile/WorkingDirectory from the LIVE unit")
    p = sub.add_parser("probe", help="read-only SQL against a service's REAL db, booted the service's way")
    p.add_argument("--estate", required=True); p.add_argument("--sql", required=True)
    p.add_argument("--write", action="store_true"); p.add_argument("--reason", default="")
    p.add_argument("--dry-run", action="store_true")
    p = sub.add_parser("shot", help="render a URL to PNG; SUSPECT never judged; --judge = $0 minimax gate")
    p.add_argument("url"); p.add_argument("--out", required=True)
    p.add_argument("--width", type=int, default=390); p.add_argument("--height", type=int, default=844)
    p.add_argument("--cookie", action="append", default=[], help="name=value[@domain]")
    p.add_argument("--judge", default=None, metavar="BRIEF")
    p = sub.add_parser("deploy", help="stage release -> flip -> verify -> keep-last-3 prune")
    p.add_argument("--estate", required=True); p.add_argument("--dist-built", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)

    if a.verb == "shot":
        room = workcycle.room_path()
        return shot(room, a.url, Path(a.out), width=a.width, height=a.height,
                    cookies=[_cookie(c) for c in a.cookie], brief=a.judge)
    room = _room()
    if a.verb == "estate":
        if a.action == "list":
            for n, e in estates(room).items():
                print(f"{n:<16} {e.get('host','-')}:{e.get('unit','-')}  {e.get('health') or e.get('base_url') or ''}")
            return 0
        if not a.name:
            print("ops estate add: --name is required"); return 3
        row = {"name": a.name, "host": a.host, "unit": a.unit, "env_file": a.env_file, "cwd": a.cwd,
               "health": a.health, "base_url": a.base_url, "releases": a.releases, "current": a.current,
               "ui_dirs": a.ui_dir}
        row = {k: v for k, v in row.items() if v not in (None, [])}
        print(json.dumps(add_estate(room, row, derive=a.derive), ensure_ascii=False)); return 0
    if a.verb == "probe":
        return probe(room, a.estate, a.sql, write=a.write, reason=a.reason, dry_run=a.dry_run)
    if a.verb == "deploy":
        return deploy(room, a.estate, dist_built=a.dist_built, dry_run=a.dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(main())
