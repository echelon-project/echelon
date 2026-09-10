"""echelon society — the multi-harness bus pipeline as first-class verbs.

Three verbs, promoted from the PROVEN scratchpad prototype (busctl.py / watcher.py):

  echelon bus       — the comms organ as a verb (post/tail/check-mail/drain/channels/stats)
  echelon watcher   — wrap a PERSISTENT claude harness as a bus-watching role
  echelon society   — plan (via board-plan) / convene / roles / status / stand-down

The bus is session-scoped (~/.echelon/society/<session>/bus.db + .jsonl mirror), so a society
is isolated and a human can `tail -f` the whole thing. See echelon_sdk/society.py for the
role→cartridge→protocol registry, protocol seeds, brief composer, and the non-destructive
check-mail peek; echelon_sdk/bus.py for the EchelonBus (do not rewrite).
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from echelon_sdk.bus import EchelonBus
from echelon_sdk import society as soc
from echelon_sdk import live_atlas

REPO_ROOT = Path(__file__).resolve().parents[2]   # <engine-checkout>


def _bus(session: str, bus_override: str | None) -> EchelonBus:
    db = soc.session_bus_path(session, bus_override)
    return EchelonBus(db, mirror_jsonl=soc.jsonl_mirror_for(db))


def _fmt(m) -> str:
    t = time.strftime("%H:%M:%S", time.localtime(m.ts))
    return f"[{m.seq:>3} {t}] ({m.channel}) <{m.sender}> {m.body}"


def _engine_cmd() -> list[str]:
    """The absolute command prefix to invoke this engine (so a role in another cwd can post)."""
    return [sys.executable, "-X", "utf8", "-m", "echelon_engine"]


# ── echelon bus ──────────────────────────────────────────────────────────────


def bus_main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="echelon bus",
                                 description="the society comms organ (post/tail/check-mail/drain/channels/stats)")
    ap.add_argument("--session", default="default", help="society session name (bus is session-scoped)")
    ap.add_argument("--bus", default=None, help="explicit bus db path (overrides --session)")
    sub = ap.add_subparsers(dest="cmd")

    p = sub.add_parser("post", help="append a message")
    p.add_argument("channel"); p.add_argument("sender"); p.add_argument("body", nargs="+")

    p = sub.add_parser("tail", help="NON-destructive: last N (default 20)")
    p.add_argument("channel"); p.add_argument("n", nargs="?", type=int, default=20)

    p = sub.add_parser("check-mail", help="NON-destructive peek-since-last-peek (humans)")
    p.add_argument("reader"); p.add_argument("channel")
    p.add_argument("--no-advance", action="store_true", help="pure read, do not move the peek cursor")
    p.add_argument("--reserved-ok", action="store_true",
                   help="I AM the owner of this reserved wake cursor (hooks only — a hand-run "
                        "advancing peek on a reserved reader poisons the wake)")

    p = sub.add_parser("reserve", help="mark a reader id as a RESERVED wake cursor — advancing "
                                       "reads then refuse without --reserved-ok (poison-proof)")
    p.add_argument("reader"); p.add_argument("--note", default="wake cursor")

    p = sub.add_parser("drain", help="DESTRUCTIVE cursor-read (watcher-internal use)")
    p.add_argument("reader"); p.add_argument("channel")

    sub.add_parser("channels", help="list channels")
    sub.add_parser("stats", help="per-channel counts")

    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 2

    bus = _bus(a.session, a.bus)
    db = soc.session_bus_path(a.session, a.bus)

    if a.cmd == "post":
        seq = bus.post(a.channel, a.sender, " ".join(a.body))
        print(f"posted seq={seq} to {a.channel}")
    elif a.cmd == "tail":
        msgs = bus.history(a.channel)[-a.n:]
        if not msgs:
            print(f"(no messages on {a.channel})")
        for m in msgs:
            print(_fmt(m))
    elif a.cmd == "check-mail":
        try:
            mail = soc.check_mail(db, a.reader, a.channel, advance=not a.no_advance,
                                  reserved_ok=a.reserved_ok)
        except PermissionError as e:
            print(f"bus check-mail: {e}", file=sys.stderr)
            return 2
        if not mail:
            print(f"(no new mail on {a.channel} for {a.reader})")
        for m in mail:
            t = time.strftime("%H:%M:%S", time.localtime(m["ts"]))
            print(f"[{m['seq']:>3} {t}] ({m['channel']}) <{m['sender']}> {m['body']}")
    elif a.cmd == "drain":
        try:
            msgs = bus.drain(a.reader, [a.channel])
        except PermissionError as e:
            print(f"bus drain: {e}", file=sys.stderr)
            return 2
        if not msgs:
            print(f"(nothing new on {a.channel} for {a.reader})")
        for m in msgs:
            print(_fmt(m))
    elif a.cmd == "channels":
        chans = bus.channels()
        if not chans:
            print("(no channels yet)")
        for c in chans:
            print(c)
    elif a.cmd == "reserve":
        bus.reserve_reader(a.reader, a.note)
        print(f"reader {a.reader!r} RESERVED ({a.note}) — advancing reads now require --reserved-ok")
    elif a.cmd == "stats":
        out = bus.stats()
        out["presence"] = bus.alive()
        print(json.dumps(out, indent=2))
    return 0


# ── echelon watcher ──────────────────────────────────────────────────────────


def _user_turn(text: str) -> str:
    """One stream-json user message line the harness reads from stdin."""
    return json.dumps({
        "type": "user",
        "message": {"role": "user", "content": [{"type": "text", "text": text}]},
    }) + "\n"


def watcher_main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="echelon watcher",
                                 description="wrap a PERSISTENT claude harness as a bus-watching role")
    ap.add_argument("--role", required=True, help="role name (resolves cartridge+protocol)")
    ap.add_argument("--channel", required=True, help="bus channel to watch")
    ap.add_argument("--cartridge", default=None,
                    help="override the role's cartridge(s), comma-separated")
    ap.add_argument("--protocol", default=None, help="override the role's protocol")
    ap.add_argument("--goal", default="", help="the slice/goal this role works")
    ap.add_argument("--scope", default="echelon")
    ap.add_argument("--repo", default=str(REPO_ROOT), help="repo cwd for the harness")
    ap.add_argument("--allow", default="Bash Read Edit Write Grep Glob",
                    help="allowedTools (the permission-gate trap — bus+pytest+git must be allowed)")
    ap.add_argument("--brief", default=None, help="extra context file folded into the seed")
    ap.add_argument("--session", default="default")
    ap.add_argument("--bus", default=None)
    ap.add_argument("--poll", type=float, default=4.0, help="bus poll interval (s)")
    ap.add_argument("--max-respawns", type=int, default=3,
                    help="STANDBY SUPERVISION: if the harness dies without a stand-down, respawn it "
                         "(re-seeded with the brief + a died-note) up to N times. 0 = die silently "
                         "(the old fragile behavior). Standby survives harness death by default.")
    ap.add_argument("--dry-run", action="store_true",
                    help="compose + print the brief and exit (no harness spawned) — for tests/inspection")
    a = ap.parse_args(argv)

    # PRIVACY GUARD (foolproofing 2026-07-03): the shared `default` session is the mixup vector —
    # every watcher without --session lands on ONE bus, where same-named roles STEAL each other's
    # mail (drain advances a shared per-role cursor). A watcher must join a CONVENED session.
    if a.session == "default" and not a.bus:
        print("[watcher] refusing --session default: the shared default bus is how societies mix "
              "up.\n  fix: join a convened session (`echelon society convene` mints a unique one), "
              "or pass an explicit --session <name> / --bus <path>.", file=sys.stderr)
        return 2

    cart = ([c.strip() for c in a.cartridge.split(",") if c.strip()]
            if a.cartridge is not None else None)
    extra = ""
    if a.brief:
        try:
            extra = Path(a.brief).read_text(encoding="utf-8")
        except OSError as e:
            print(f"[watcher] --brief unreadable: {e}", file=sys.stderr)

    db = soc.session_bus_path(a.session, a.bus)
    # the absolute post command the role uses (banked relative-path trap)
    busctl_cmd = " ".join(
        _engine_cmd() + ["bus"]
        + (["--bus", str(db)] if a.bus else ["--session", a.session])
        + ["post", a.channel, a.role])
    brief = soc.compose_brief(a.role, a.channel, busctl_cmd,
                              cartridge=cart, protocol=a.protocol, goal=a.goal, extra=extra)

    if a.dry_run:
        print(brief)
        return 0

    return _run_watcher(a, db, brief)


def _is_stand_down(m) -> bool:
    """STRUCTURED stand-down (foolproofing 2026-07-03). The old substring match killed a role
    on ANY message merely QUOTING 'stand down'. Now a stand-down is either an ANCHORED command
    (the body IS the order, not contains it) or an explicit meta flag."""
    if isinstance(m.meta, dict) and m.meta.get("control") == "stand_down":
        return True
    return m.body.strip().upper().startswith("STAND DOWN")


def _spawn_harness(a, sess_log, brief: str, note: str = ""):
    """Spawn the persistent claude harness and seed turn 1. Returns proc or None."""
    # TRAP 1 — pre-set --allowedTools so the backgrounded stream-json session never stalls at
    # the tool permission gate (bus/pytest/git must be allowed).
    cmd = ["claude", "--print", "--input-format", "stream-json",
           "--output-format", "stream-json", "--verbose",
           "--permission-mode", "acceptEdits",
           "--allowedTools", a.allow,
           "--add-dir", str(REPO_ROOT),
           "--add-dir", str(sess_log.parent),
           "--add-dir", a.repo]
    try:
        proc = subprocess.Popen(
            cmd, stdin=subprocess.PIPE, stdout=sess_log.open("a", encoding="utf-8"),
            stderr=subprocess.STDOUT, text=True, encoding="utf-8", cwd=a.repo)
    except FileNotFoundError:
        print("[watcher] cannot launch — `claude` CLI not on PATH.", file=sys.stderr)
        return None
    seed = brief if not note else f"{brief}\n\n[watcher note] {note}"
    try:
        proc.stdin.write(_user_turn(seed))
        proc.stdin.flush()
    except (BrokenPipeError, ValueError) as e:
        print(f"[watcher] {a.role} seed failed: {e}", file=sys.stderr)
        return None
    return proc


def _run_watcher(a, db: Path, brief: str) -> int:
    """The live persistent-session loop — SUPERVISED standby (foolproofing 2026-07-03):
    the role REGISTERS on the bus (loud RoleTaken beats silent mail-theft), HEARTBEATS every
    poll (so `society status` shows live/dead truthfully), and RESPAWNS the harness if it dies
    without a stand-down (standby survives harness death). Honors the original banked traps."""
    bus = EchelonBus(db, mirror_jsonl=soc.jsonl_mirror_for(db))
    sess_log = db.parent / f"{a.role}.session.log"
    sess_log.parent.mkdir(parents=True, exist_ok=True)

    # PRESENCE — claim the role or die LOUDLY (duplicate roles on one bus steal mail silently).
    import os as _os
    try:
        bus.register(a.role, _os.getpid())
    except EchelonBus.RoleTaken as e:
        print(f"[watcher] {e}", file=sys.stderr)
        return 2
    # LIVE ATLAS (2026-07-03): the watcher auto-maps itself — a role node, membered to its
    # session, speaking on its channel, working its repo. Severable: a dead atlas never
    # blocks a role.
    _atlas_name = f"{a.role}@{a.session}"
    live_atlas.register(
        "role", _atlas_name,
        meta={"channel": a.channel, "goal": a.goal, "repo": a.repo},
        edges=[("member_of", "session", a.session),
               ("speaks_on", "channel", a.channel),
               ("touches_repo", "repo", a.repo)])
    live_atlas.act("role", _atlas_name, "spawn", f"watcher live on {a.channel}")

    try:
        # TRAP 2a — FRESH-START: drain ONCE to advance this reader's cursor PAST all existing
        # history, so a new peer never consumes a STALE control message (e.g. an old STAND DOWN).
        stale = bus.drain(a.role, [a.channel])
        if stale:
            print(f"[watcher] {a.role}: skipped {len(stale)} pre-existing msg(s) (cursor at tip)", flush=True)

        proc = _spawn_harness(a, sess_log, brief)
        if proc is None:
            return 1
        respawns = 0
        print(f"[watcher] {a.role} live on {a.channel} (supervised, max-respawns={a.max_respawns}); "
              f"session log -> {sess_log.name}", flush=True)

        while True:
            if proc.poll() is not None:
                # SUPERVISION: harness died WITHOUT a stand-down. The old behavior returned 0
                # here — role silently gone. Standby now survives: respawn, re-seeded.
                if respawns >= a.max_respawns:
                    print(f"[watcher] {a.role} died (code {proc.returncode}) and respawn budget "
                          f"({a.max_respawns}) is spent — standing down for real.", flush=True)
                    bus.post(a.channel, a.role, f"[watcher] {a.role} DOWN — harness died "
                             f"{respawns + 1}x, respawn budget spent.", {"control": "role_down"})
                    return 1
                respawns += 1
                print(f"[watcher] {a.role} harness died (code {proc.returncode}) — respawn "
                      f"{respawns}/{a.max_respawns}.", flush=True)
                proc = _spawn_harness(
                    a, sess_log, brief,
                    note=f"your previous session died mid-standby (respawn {respawns}); check the "
                         f"bus with check-mail for anything you were working on, then continue.")
                if proc is None:
                    return 1
            bus.beat(a.role)  # the heartbeat: `society status`/`bus stats` show live truthfully
            live_atlas.beat("role", _atlas_name)
            msgs = bus.drain(a.role, [a.channel])
            for m in msgs:
                print(f"[watcher] -> {a.role}: seq {m.seq} <{m.sender}>", flush=True)
                if _is_stand_down(m):
                    live_atlas.act("role", _atlas_name, "stand_down", f"by {m.sender}")
                    _stand_down(proc, a.role, m)
                    return 0
                try:
                    proc.stdin.write(_user_turn(f"[bus seq {m.seq} from {m.sender}]: {m.body}"))
                    proc.stdin.flush()
                except (BrokenPipeError, ValueError):
                    print(f"[watcher] {a.role} stdin closed mid-delivery; will respawn on next "
                          f"poll.", flush=True)
                    break
            time.sleep(a.poll)
    finally:
        bus.unregister(a.role)


def _stand_down(proc, role: str, m) -> None:
    """Deliver a final turn, then proc.wait WRAPPED in try/except — TRAP 2b: a busy session
    must NOT crash the watcher. Guard stdin.write vs BrokenPipe."""
    try:
        proc.stdin.write(_user_turn(
            f"[bus seq {m.seq} from {m.sender}]: {m.body}\n\n"
            "STAND DOWN received — post a final summary to the bus and stop."))
        proc.stdin.flush()
    except (BrokenPipeError, ValueError):
        print(f"[watcher] {role} stdin already closed at stand-down.", flush=True)
    time.sleep(10)
    try:
        proc.stdin.close()
        proc.wait(timeout=180)
    except subprocess.TimeoutExpired:
        print(f"[watcher] {role} busy at stand-down; leaving session to finish.", flush=True)
    except Exception as e:  # a busy session must not crash the watcher
        print(f"[watcher] {role} stand-down note: {e}", flush=True)
    print(f"[watcher] {role} stood down.", flush=True)


# ── echelon society ──────────────────────────────────────────────────────────


def society_main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="echelon society",
                                 description="convene + manage the role society")
    sub = ap.add_subparsers(dest="cmd")

    p = sub.add_parser("plan", help="draw the slice DAG via board-plan -> SOCIETY_DESIGN.json")
    p.add_argument("--goal", required=True)
    p.add_argument("--scope", default="echelon")
    p.add_argument("--channel", default="echelon.build")
    p.add_argument("--output", default="SOCIETY_DESIGN.json")
    p.add_argument("--provider", default="auto")
    p.add_argument("--timeout", type=int, default=300)

    p = sub.add_parser("convene", help="launch all roles as persistent watched peers")
    p.add_argument("--design", default="SOCIETY_DESIGN.json")
    p.add_argument("--session", default="default")
    p.add_argument("--background", action="store_true",
                   help="spawn each watcher detached (default: print the launch commands)")

    sub.add_parser("roles", help="list/add the role registry").add_argument(
        "rest", nargs=argparse.REMAINDER)

    p = sub.add_parser("status", help="which peers live, last bus activity")
    p.add_argument("--session", default="default")
    p.add_argument("--bus", default=None)

    p = sub.add_parser("stand-down", help="post STAND DOWN to a role / all on a channel")
    p.add_argument("--role", default=None, help="target role (default: broadcast to channel)")
    p.add_argument("--channel", default="echelon.build")
    p.add_argument("--session", default="default")
    p.add_argument("--bus", default=None)

    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 2
    if a.cmd == "plan":
        return _society_plan(a)
    if a.cmd == "convene":
        return _society_convene(a)
    if a.cmd == "roles":
        return _society_roles(a.rest)
    if a.cmd == "status":
        return _society_status(a)
    if a.cmd == "stand-down":
        return _society_stand_down(a)
    return 2


def _society_plan(a) -> int:
    """Wrap board-plan to DRAW the slice DAG, then wrap it into SOCIETY_DESIGN.json."""
    from echelon_engine.swarm import main as swarm_main
    print(f"society plan: drawing the slice DAG via board-plan for: {a.goal}")
    rc = swarm_main(["--type", "board-plan", "--goal", a.goal, "--provider", a.provider,
                     "--timeout", str(a.timeout)])
    # board-plan emits BOARD_DESIGN.json (a list of task items). Wrap it into a society design.
    bd_path = Path("BOARD_DESIGN.json")
    slices: list[dict] = []
    if bd_path.is_file():
        try:
            items = json.loads(bd_path.read_text(encoding="utf-8"))
            for i, it in enumerate(items if isinstance(items, list) else []):
                slices.append({
                    "id": it.get("id", f"slice-{i+1}"),
                    "role": it.get("role", "builder"),
                    "spec_ref": it.get("task", ""),
                    "gate": ["auditor"],
                    "depends_on": [],
                })
        except json.JSONDecodeError:
            pass
    design = {
        "channel": a.channel,
        "roles": [
            {"name": "architect", **soc.resolve_role("architect"), "repo": str(REPO_ROOT)},
            {"name": "builder", **soc.resolve_role("builder"), "repo": str(REPO_ROOT)},
            {"name": "auditor", **soc.resolve_role("auditor"), "repo": str(REPO_ROOT)},
        ],
        "slices": slices,
        "goal": a.goal,
    }
    Path(a.output).write_text(json.dumps(design, indent=2, ensure_ascii=False) + "\n",
                              encoding="utf-8")
    print(f"society plan: {a.output} written ({len(slices)} slice(s), {len(design['roles'])} roles)")
    print(f"  next: echelon society convene --design {a.output}")
    return 0 if rc == 0 else rc


def _society_convene(a) -> int:
    design_path = Path(a.design)
    if not design_path.is_file():
        print(f"society convene: design not found: {a.design}", file=sys.stderr)
        return 1
    design = json.loads(design_path.read_text(encoding="utf-8"))
    channel = design.get("channel", "echelon.build")
    roles = design.get("roles", [])
    if not roles:
        print("society convene: design has no roles.", file=sys.stderr)
        return 1
    # PRIVACY (foolproofing 2026-07-03): NEVER convene on the shared `default` bus — that is
    # the mixup vector (two societies, one bus, same-named roles stealing each other's mail).
    # Mint a unique session id instead; the watchers below inherit it, and the id is printed
    # loudly so the owner can bus-tail / stand-down THIS society and no other.
    if a.session == "default":
        a.session = f"soc-{time.strftime('%Y%m%d-%H%M%S')}-{os.urandom(2).hex()}"
        print(f"society convene: minted PRIVATE session '{a.session}' "
              f"(the shared default bus is refused by watchers)")
    print(f"society convene: {len(roles)} role(s) on channel '{channel}' (session '{a.session}')")
    launched = 0
    for r in roles:
        name = r["name"]
        watch_cmd = _engine_cmd() + [
            "watcher", "--role", name, "--channel", channel,
            "--session", a.session, "--repo", r.get("repo", str(REPO_ROOT)),
            "--goal", design.get("goal", "")]
        if r.get("protocol"):
            watch_cmd += ["--protocol", r["protocol"]]
        if r.get("cartridge"):
            watch_cmd += ["--cartridge", ",".join(r["cartridge"])]
        if a.background:
            try:
                subprocess.Popen(watch_cmd, cwd=r.get("repo", str(REPO_ROOT)))
                launched += 1
                print(f"  launched watcher: {name}")
            except OSError as e:
                print(f"  FAILED to launch {name}: {e}", file=sys.stderr)
        else:
            print(f"  {name}:  " + " ".join(watch_cmd))
    if not a.background:
        print("\n(dry — re-run with --background to spawn the watchers detached)")
    else:
        print(f"society convene: {launched}/{len(roles)} watchers launched.")
    return 0


def _society_roles(rest: list[str]) -> int:
    if rest and rest[0] == "add":
        ap = argparse.ArgumentParser(prog="echelon society roles add")
        ap.add_argument("name")
        ap.add_argument("--cartridge", default="", help="comma-separated cartridge names")
        ap.add_argument("--protocol", default=soc.DEFAULT_PROTOCOL)
        a = ap.parse_args(rest[1:])
        spec = soc.add_role(a.name, a.cartridge, a.protocol)
        print(f"role added: {a.name} -> cartridge {spec['cartridge']}, protocol '{spec['protocol']}'")
        return 0
    # list
    roles = soc.list_roles()
    print(f"SOCIETY ROLES ({len(roles)}):  registry = {soc.ROLES_JSON}")
    for name, spec in roles.items():
        cart = ", ".join(spec["cartridge"]) or "(none)"
        print(f"  ◆ {name:<12} cartridge: [{cart}]   protocol: {spec['protocol']}")
    print(f"\nPROTOCOLS: {', '.join(soc.PROTOCOL_SEEDS)}")
    return 0


def _society_status(a) -> int:
    db = soc.session_bus_path(a.session, a.bus)
    if not db.exists():
        print(f"society status: no bus yet at {db}")
        return 0
    bus = EchelonBus(db, mirror_jsonl=soc.jsonl_mirror_for(db))
    stats = bus.stats()
    print(f"society status: session '{a.session}'  bus {db}")
    print(f"  total messages: {stats['total']}")
    for ch, n in stats["per_channel"].items():
        hist = bus.history(ch)
        last = hist[-1] if hist else None
        senders = sorted({m.sender for m in hist})
        when = time.strftime("%H:%M:%S", time.localtime(last.ts)) if last else "-"
        print(f"  ({ch}) {n} msgs, last {when}, senders: {', '.join(senders)}")
    return 0


def _society_stand_down(a) -> int:
    db = soc.session_bus_path(a.session, a.bus)
    bus = EchelonBus(db, mirror_jsonl=soc.jsonl_mirror_for(db))
    target = f" (role: {a.role})" if a.role else " (all roles on channel)"
    body = f"STAND DOWN{(' ' + a.role) if a.role else ''} — post a final summary and stop."
    seq = bus.post(a.channel, "architect", body)
    print(f"society stand-down: posted seq={seq} to {a.channel}{target}")
    return 0
