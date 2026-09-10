"""worktype — the room TYPE anchor (ESTATE-CRAFT-CHARTER Part 4, first vertebra).

`room.json.type` names what KIND of work a repo is (engine, console-app, prod-service,
...). This module is the first consumer of that field: reflexes declare `worktype:` in
their frontmatter (compiled by atoms.reflex into rule["worktype"]) and fire only in rooms
of those types; a `type snooze` mutes one reflex for a bounded time, journaled in the
room. Every read here is severable — an unknown type or a broken room ARMS everything
(fail open): a spurious guard is recoverable, a missing one is not.

Room writes (type, snooze.json, journal) go through workcycle — the room's ONE WRITER.
The arming decision lives in atoms.reflex.room_gates — ONE law for the hook, `reflex
test` and `type show`.

CLI:  echelon type show | set <type> | snooze <reflex> [--hours N | --until ISO] [--why W]
              | unsnooze [<reflex> | --all] | list
"""
from __future__ import annotations

import argparse
import contextlib
import io
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from echelon_engine import workcycle
from echelon_engine.atoms import reflex as _reflex

# The charter's draft taxonomy — extendable; `set` warns (never refuses) off-list.
TAXONOMY = ("prod-service", "console-app", "static-site", "library", "data-pipeline",
            "engine", "client-report", "experiment")
DEFAULT_SNOOZE_HOURS = 8
# a mute longer than a working week is a type change or a retirement — an owned decision
SNOOZE_WARN_HOURS = 72


def session_gate(cwd: str | None = None) -> tuple[str, tuple[str, ...]]:
    """(room type, live snoozed names) for the room that owns `cwd`.
    ("", ()) when there is no room or anything fails — the hook fails open.
    stdout is captured while reading: the hook's stdout is the harness protocol
    channel, and one stray engine print there drops every guard (gate M-1)."""
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            room = workcycle.room_path(cwd or None)
            return workcycle.room_type(room), tuple(s["name"] for s in workcycle.snoozes(room))
    except Exception:
        return "", ()


def survey(room: Path | None, scope: str = "") -> dict:
    """What the ruleset looks like FROM this room: armed / hidden-by-type / snoozed."""
    rtype = workcycle.room_type(room)
    snoozed = tuple(s["name"] for s in workcycle.snoozes(room))
    rules = _reflex._load_ruleset().get("rules", [])
    scope = (scope or (workcycle._room_scope(room) if room else "")).lower()
    here = [r for r in rules if r.get("tier", "scope") in ("global", "portable")
            or (r.get("scope", "") or "").lower() == scope]
    out = {"type": rtype, "scope": scope, "rules_here": len(here),
           "armed": [], "hidden": [], "snoozed": []}
    for r in here:
        ok, why = _reflex.room_gates(r, rtype, snoozed)
        key = f"{r.get('scope', '')}:{r.get('name', '')}"
        (out["armed"] if ok else out["snoozed"] if why.startswith("snoozed") else out["hidden"]).append(key)
    return out


def _until(hours: float | None, until: str | None) -> tuple[str, str]:
    """(ISO until, warning-or-empty). Validation of the ISO form itself is workcycle's."""
    if until:
        return until, ""
    h = hours if hours is not None else DEFAULT_SNOOZE_HOURS
    if h <= 0:
        raise ValueError(f"--hours must be positive, got {h}")
    warn = (f"warn: {h:g}h is longer than {SNOOZE_WARN_HOURS}h — a mute that long is a type "
            f"change or a retirement, not a snooze") if h > SNOOZE_WARN_HOURS else ""
    return (datetime.now(timezone.utc) + timedelta(hours=h)).isoformat(), warn


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="echelon type",
                                 description="The room TYPE anchor — what kind of work this repo is")
    sub = ap.add_subparsers(dest="action", required=True)
    sub.add_parser("show", help="room type + which reflexes are armed / hidden / snoozed here")
    p_set = sub.add_parser("set", help="change the room's type (journaled)")
    p_set.add_argument("type_", metavar="type")
    p_sn = sub.add_parser("snooze", help="mute one reflex for a bounded time (journaled)")
    p_sn.add_argument("name", help="reflex name, or <scope>:<name>")
    p_sn.add_argument("--hours", type=float, default=None,
                      help=f"mute for N hours (default {DEFAULT_SNOOZE_HOURS} — one working session)")
    p_sn.add_argument("--until", default=None, help="mute until an ISO-8601 UTC timestamp")
    p_sn.add_argument("--why", default="", help="why (lands in the journal)")
    p_un = sub.add_parser("unsnooze", help="lift a snooze (or --all)")
    p_un.add_argument("name", nargs="?", default=None)
    p_un.add_argument("--all", action="store_true")
    sub.add_parser("list", help="the type taxonomy")
    a = ap.parse_args(argv)

    if a.action == "list":
        for t in TAXONOMY:
            print(t)
        return 0
    room = workcycle.room_path()
    if room is None:
        print("ROOM unavailable (no room — run `workcycle init` at the repo root)")
        return 1
    if a.action == "show":
        s = survey(room)
        print(f"ROOM type {s['type'] or '-'} · scope {s['scope'] or '-'} · "
              f"{s['rules_here']} reflex rule(s) reach this room")
        print(f"  armed {len(s['armed'])} · hidden-by-type {len(s['hidden'])} · snoozed {len(s['snoozed'])}")
        for k in s["hidden"]:
            print(f"  hidden  {k}")
        for sn in workcycle.snoozes(room):
            print(f"  snoozed {sn['name']} until {sn.get('until')}  ({sn.get('why') or '-'})")
        return 0
    if a.action == "set":
        if a.type_ not in TAXONOMY:
            print(f"warn: '{a.type_}' is not in the charter taxonomy ({', '.join(TAXONOMY)}) — set anyway")
        rec = workcycle.set_type(room, a.type_)
        print(json.dumps(rec, ensure_ascii=False))
        return 0
    if a.action == "snooze":
        try:
            until, warn = _until(a.hours, a.until)
            rec = workcycle.snooze(room, a.name, until, why=a.why)
        except ValueError as exc:
            print(f"type snooze: {exc}")
            return 1
        if warn:
            print(warn)
        print(json.dumps(rec, ensure_ascii=False))
        return 0
    if a.action == "unsnooze":
        if not a.all and not a.name:
            print("type unsnooze: give a name or --all")
            return 1
        lifted = workcycle.unsnooze(room, None if a.all else a.name)
        print(f"lifted: {', '.join(lifted) if lifted else '-'}")
        return 0
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
