"""fw — the framework door: pure pass-through to the `echelon-fw` console entry.

OPEN-0060 phase A, deliverable 4 (owner 2026-09-02): echelon-fw (the framework CLI,
D:\\WORK\\echelon-framework, console entry `echelon-fw` = `cli.main:main`) needs to be
reachable from the engine's verb surface — `python -m echelon_engine fw <args...>` — so a
session never has to remember a second command name. This carries NO framework logic: it
forwards argv verbatim to the real console entry (found via shutil.which so the engine
never imports or hardcodes framework code — three-home law, the engine stays clean of
estate paths) and mirrors its exit code. If the console entry is not on PATH, it falls back
to `python -X utf8 -m cli.main` run with cwd/PYTHONPATH set to the framework path recorded
in the harness contract (`echelon-harness-contract.json`'s `framework.path`), so the door
still works in an environment where `pip install -e .` hasn't happened yet.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from .. import estate as _estate


def _contract_framework_path() -> str:
    """Read the framework.path out of the harness contract, best-effort.

    The contract is ECHELON-side state (<estate-root>/echelon-harness-contract.json);
    this only reads it as a fallback locator, never as a hardcoded path.
    """
    candidates = [
        os.environ.get("ECHELON_HARNESS_CONTRACT", ""),
        os.path.join(os.getcwd(), "echelon-harness-contract.json"),
        str(_estate.estate_root_for("command_root") / "echelon-harness-contract.json"),
    ]
    for c in candidates:
        if not c or not os.path.isfile(c):
            continue
        try:
            with open(c, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            path = data.get("framework", {}).get("path", "")
            if path:
                return path
        except Exception:
            continue
    return ""


def _main(argv=None) -> int:
    """Pass-through: forward argv verbatim to `echelon-fw`, mirror its exit code.

    No argparse here on purpose — every flag (including --help) belongs to the framework's
    own parser, not this door's.
    """
    argv = sys.argv[1:] if argv is None else list(argv)

    env = dict(os.environ)
    env["PYTHONUTF8"] = "1"
    env.setdefault("PYTHONIOENCODING", "utf-8")

    exe = shutil.which("echelon-fw")
    if exe:
        cmd = [exe, *argv]
        cwd = None
    else:
        fw_path = _contract_framework_path()
        if not fw_path or not os.path.isdir(fw_path):
            print(
                "echelon fw: `echelon-fw` is not on PATH and the framework path could not be "
                "resolved from the harness contract. Install it with "
                "`pip install -e <framework path>` or set ECHELON_HARNESS_CONTRACT.",
                file=sys.stderr,
            )
            return 1
        cmd = [sys.executable, "-X", "utf8", "-m", "cli.main", *argv]
        cwd = fw_path
        env["PYTHONPATH"] = fw_path + os.pathsep + env.get("PYTHONPATH", "")

    proc = subprocess.run(cmd, cwd=cwd, env=env)
    return proc.returncode


if __name__ == "__main__":
    sys.exit(_main())
