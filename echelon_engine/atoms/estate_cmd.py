"""estate — the CLI door for the estate config (R-0171 unit A, OPEN-0121).

    echelon estate init [--force] [--out FILE] [--estate-root D] [--engine-root D]
    echelon estate show [--json]
    echelon estate check

`init` writes the config for THIS machine by DETECTING what is actually here,
never by copying a template full of someone else's paths. Idempotent: writing
the same content twice is a no-op that reports `unchanged`; writing DIFFERENT
content over an existing file REFUSES without --force, because clobbering the
wiring of a live estate is exactly the kind of act that should need a word.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from ..estate import REQUIRED_KEYS, EstateError, config_path, load
from .echelon_home import echelon_home

#: Per-project roots the estate's code refers to by name. Detected, not assumed.
#:
#: There is deliberately NO built-in project allowlist: an estate's projects are
#: whatever directories exist under its root on THIS machine. Declare an explicit
#: roster (and any key -> on-disk-dirname aliases) in ~/.echelon/projects.json:
#:
#:   {"projects": ["my-app", "my-site"], "dirnames": {"my-app": "MyApp"}}
#:
#: With no such file every non-hidden subdirectory of the estate root is a project.
_PROJECTS_FILE = "projects.json"


def _roster(estate_root: Path) -> tuple[tuple[str, ...], dict]:
    """(project keys, key -> on-disk dirname). Config first, else every subdir."""
    cfg = echelon_home() / _PROJECTS_FILE
    if cfg.is_file():
        try:
            data = json.loads(cfg.read_text(encoding="utf-8"))
            names = tuple(str(x) for x in data.get("projects", ()))
            dirnames = {str(k): str(v) for k, v in (data.get("dirnames") or {}).items()}
            if names:
                return names, dirnames
        except Exception:
            pass
    if not estate_root.is_dir():
        return (), {}
    found = [e.name for e in estate_root.iterdir()
             if e.is_dir() and not e.name.startswith((".", "_"))]
    return tuple(sorted(n.lower() for n in found)), {n.lower(): n for n in found}


def _detect(estate_root: Path, engine_root: Path, command_root: Path) -> dict:
    """Build the config from what this machine actually has."""
    venv_python = engine_root / ".venv" / (
        "Scripts/python.exe" if os.name == "nt" else "bin/python")
    config = {
        "estate_root": str(estate_root),
        "engine_root": str(engine_root),
        "command_root": str(command_root),
        "python": str(venv_python if venv_python.exists() else Path(sys.executable)),
        "bank": str(echelon_home() / "echelon.db"),
        "rooms_registry": str(echelon_home() / "rooms.json"),
    }
    # Record the REAL on-disk name, not the key's casing: Windows would match
    # `myproject` against `MyProject` and hide the difference, but the config is
    # meant to be portable, and on a case-sensitive filesystem a wrong-cased
    # path is a dead path.
    actual = {}
    if estate_root.is_dir():
        actual = {entry.name.lower(): entry.name
                  for entry in estate_root.iterdir() if entry.is_dir()}
    known, dirnames = _roster(estate_root)
    found = {}
    for name in known:
        wanted = dirnames.get(name, name)
        on_disk = actual.get(wanted.lower())
        if on_disk:
            found[name] = str(estate_root / on_disk)
    config["projects"] = found
    return config


def _write(out: Path, config: dict, force: bool) -> dict:
    """Idempotent write. Returns a report; never clobbers a difference silently."""
    body = json.dumps(config, indent=2, sort_keys=True) + "\n"
    if out.exists():
        current = out.read_text(encoding="utf-8")
        if current == body:
            return {"ok": True, "path": str(out), "status": "unchanged"}
        if not force:
            return {
                "ok": False,
                "path": str(out),
                "status": "differs",
                "error": ("%s already exists with different content — inspect it, "
                          "then re-run with --force to overwrite." % out),
            }
    out.parent.mkdir(parents=True, exist_ok=True)
    # newline="" keeps LF as written: text mode on Windows would flip to CRLF.
    with open(out, "w", encoding="utf-8", newline="") as fh:
        fh.write(body)
    return {"ok": True, "path": str(out), "status": "written"}


def _init(argv) -> int:
    ap = argparse.ArgumentParser(prog="echelon estate init")
    ap.add_argument("--out", default=None, help="config path (default: the resolved one)")
    ap.add_argument("--estate-root", default=None,
                    help="parent dir holding the projects (default: engine repo's parent)")
    ap.add_argument("--engine-root", default=None,
                    help="the ECHELON-AGENT checkout (default: this package's repo)")
    ap.add_argument("--command-root", default=None,
                    help="the command center checkout (default: <estate>/ECHELON)")
    ap.add_argument("--force", action="store_true", help="overwrite a differing config")
    o = ap.parse_args(list(argv))

    engine_root = Path(o.engine_root).expanduser() if o.engine_root else \
        Path(__file__).resolve().parents[2]
    estate_root = Path(o.estate_root).expanduser() if o.estate_root else engine_root.parent
    command_root = Path(o.command_root).expanduser() if o.command_root else \
        estate_root / "ECHELON"
    out = Path(o.out).expanduser() if o.out else config_path()

    config = _detect(estate_root.resolve(), engine_root.resolve(), command_root.resolve())
    report = _write(out, config, o.force)
    report["projects"] = len(config["projects"])
    print(json.dumps(report, indent=2))
    return 0 if report["ok"] else 1


def _show(argv) -> int:
    ap = argparse.ArgumentParser(prog="echelon estate show")
    ap.add_argument("--json", action="store_true")
    o = ap.parse_args(list(argv))
    try:
        data = load()
    except EstateError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, indent=2))
        return 1
    if o.json:
        print(json.dumps(data, indent=2, sort_keys=True))
        return 0
    print("config: %s" % config_path())
    for key in sorted(data):
        if key == "projects":
            continue
        print("  %-16s %s" % (key, data[key]))
    for name, root in sorted(data.get("projects", {}).items()):
        print("  project.%-8s %s" % (name, root))
    return 0


def _check(argv) -> int:
    """Every REQUIRED key declared, and every declared root actually present."""
    ap = argparse.ArgumentParser(prog="echelon estate check")
    ap.parse_args(list(argv))
    try:
        data = load()
    except EstateError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, indent=2))
        return 1
    missing = [k for k in REQUIRED_KEYS if k not in data]
    absent = sorted(
        "%s=%s" % (k, data[k]) for k in REQUIRED_KEYS
        if k in data and k not in ("bank", "rooms_registry")
        and not Path(str(data[k])).exists()
    )
    report = {
        "ok": not missing and not absent,
        "config": str(config_path()),
        "missing_keys": missing,
        "absent_paths": absent,
        "projects": len(data.get("projects", {})),
    }
    print(json.dumps(report, indent=2))
    return 0 if report["ok"] else 1


USAGE = """usage: echelon estate <init|show|check> [options]

The estate config every hardcoded path resolves from (R-0171, OPEN-0121).
Resolved from $ECHELON_ESTATE_CONFIG, else <ECHELON_HOME>/estate.json,
else ~/.echelon/estate.json.

  init    write the config for THIS machine by detecting what is here.
          Idempotent; refuses to overwrite a DIFFERING file without --force.
          [--out FILE] [--estate-root D] [--engine-root D] [--command-root D] [--force]
  show    print the resolved config          [--json]
  check   verify every required key is declared and every declared root exists

Required keys: %s""" % ", ".join(REQUIRED_KEYS)


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    sub = argv[0] if argv else ""
    if sub == "init":
        return _init(argv[1:])
    if sub == "show":
        return _show(argv[1:])
    if sub == "check":
        return _check(argv[1:])
    # `--help` (and a bare invocation) must succeed: the CLI contract test
    # treats a non-zero help exit as "the verb is not invokable".
    if sub in ("", "-h", "--help", "help"):
        print(USAGE)
        return 0
    print("unknown estate subcommand %r\n\n%s" % (sub, USAGE), file=sys.stderr)
    return 2


_main = main
