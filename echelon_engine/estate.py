"""estate — the ONE resolver for every path this estate used to hardcode.

R-0171 unit A (owner ruling 2026-09-09, board #5309, OPEN-0121). Before this,
~60 load-bearing files carried a `<estate-root>/...` literal, so the estate ran only
on the machine it was written on. This module is the PACKAGE that turns "runs
on D:" into "runs where the config says" (boundary-driven work map: this is
WIRING pulled out of code into a declarative file).

    ECHELON_ESTATE_CONFIG=<file>   →   that file
    (unset)                        →   <echelon_home()>/estate.json
                                       i.e. ~/.echelon/estate.json by default

NAMING (deliberate, do not "fix"): the R-0171 brief said `ECHELON_HOME`, but
that name was already taken — `atoms/echelon_home.py` uses it to relocate the
BANK (core.db / echelon.db / .env), with 14 dependents. Overloading one var to
mean both "where the bank lives" and "where the estate config lives" would let
a caller who wanted one silently move the other. So the config pointer is its
own var, and its DEFAULT sits inside the existing home — the two compose.

THE LAW THIS ENFORCES: a missing key FAILS LOUD. There is no silent fallback to
`<estate-root>/...`, because a fallback that quietly re-couples the process to one
machine is exactly the defect this unit removes. `EstateKeyError` names both the
missing key and the config file, so the fix is obvious from the traceback alone.

Kept stdlib-only and dependency-free (imports one leaf, echelon_home) so the
lowest modules and the standalone hooks can import it without cycles.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, Optional

from .atoms.echelon_home import echelon_home

#: Env var naming the config FILE (not the estate root). See NAMING above.
CONFIG_ENV = "ECHELON_ESTATE_CONFIG"

#: Keys every estate must declare. `estate init` writes all of them; the
#: contract test asserts the live config carries them.
REQUIRED_KEYS = (
    "estate_root",
    "engine_root",
    "command_root",
    "python",
    "bank",
    "rooms_registry",
)


class EstateError(RuntimeError):
    """Base for every estate-config failure."""


class EstateConfigMissing(EstateError):
    """No estate config file where the resolver looked."""


class EstateKeyError(EstateError):
    """A key was asked for and the config does not declare it."""


def config_path() -> Path:
    """The config file this process resolves from. Pure; does not read it."""
    override = os.environ.get(CONFIG_ENV)
    if override:
        return Path(override).expanduser()
    return echelon_home() / "estate.json"


def _read(path: Path) -> Dict[str, Any]:
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise EstateConfigMissing(
            "no estate config at %s — run `python -X utf8 -m echelon_engine "
            "estate init` to write one for this machine, or set %s to an "
            "existing config." % (path, CONFIG_ENV)
        ) from None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise EstateError("estate config %s is not valid JSON: %s" % (path, exc)) from None
    if not isinstance(data, dict):
        raise EstateError("estate config %s must be a JSON object" % path)
    return data


def load(path: Optional[Path] = None) -> Dict[str, Any]:
    """The whole config as a dict. Read fresh each call: the estate is long-lived
    and a config edit must not need a restart to be seen."""
    return _read(Path(path) if path is not None else config_path())


def _lookup(key: str, data: Dict[str, Any]) -> Any:
    """Resolve `key`, allowing `projects.<name>` to reach into the map."""
    if key in data:
        return data[key]
    if "." in key:
        head, _, tail = key.partition(".")
        section = data.get(head)
        if isinstance(section, dict) and tail in section:
            return section[tail]
    return None


def get(key: str, *, path: Optional[Path] = None) -> Any:
    """A declared value. Raises EstateKeyError (naming key AND file) if absent.

    This is the fail-loud door: no default argument, deliberately. A caller that
    wants a fallback must write it at the call site, where it is visible.
    """
    resolved = Path(path) if path is not None else config_path()
    data = _read(resolved)
    value = _lookup(key, data)
    if value is None:
        raise EstateKeyError(
            "estate config %s does not declare %r (declared: %s). Add it, or "
            "re-run `python -X utf8 -m echelon_engine estate init --force`."
            % (resolved, key, ", ".join(sorted(data)) or "nothing")
        )
    return value


def root(key: str, *, path: Optional[Path] = None) -> Path:
    """A declared value as an absolute Path. Same fail-loud contract as get()."""
    return Path(str(get(key, path=path))).expanduser()


#: Env vars that predate this module and already override a specific key. They
#: keep working: an operator who exported one gets what they asked for, and the
#: config answers only when they did not. Dropping them would break callers
#: silently, which is the failure mode this whole unit is against.
LEGACY_ENV = {
    "command_root": "ECHELON_ESTATE",
    "engine_root": "ECHELON_AGENT",
}


def sibling(name: str, dirname: Optional[str] = None, *,
            path: Optional[Path] = None) -> Path:
    """A project root by name, falling back to <estate_root>/<dirname>.

    For the projects that are OPTIONAL on a given machine (the archive, a
    research corpus): if the config names it, use that; else infer it from the
    estate root, which is itself declared. Never a machine literal either way.

    IMPORT-TIME SAFE for the same reason as estate_root_for(): callers bind this
    to module constants, so a missing config degrades to the repo layout instead
    of making the module unimportable.
    """
    found = optional("projects." + name, path=path)
    if found:
        return Path(str(found)).expanduser()
    try:
        base = root("estate_root", path=path)
    except EstateError:
        base = Path(__file__).resolve().parent.parent.parent
    return base / (dirname or name)


def estate_root_for(key: str, *, path: Optional[Path] = None) -> Path:
    """A root honoring its legacy env override first, then the config.

    Used by the sites that historically read ECHELON_ESTATE / ECHELON_AGENT.

    IMPORT-TIME SAFE, deliberately: several callers bind this to a module-level
    constant, and those modules sit under `workcycle`/`archive`/`delta`, which a
    hook imports in a fresh ECHELON_HOME where no config exists yet. Raising
    there would make the MODULE unimportable — a config problem must never break
    an import. So when nothing declares the key, this falls back to the repo
    layout (this file's own grandparent is the engine checkout; its sibling is
    the command center) rather than raising. Call `root()` directly where you
    want the fail-loud contract at USE time.
    """
    env = LEGACY_ENV.get(key)
    if env:
        override = os.environ.get(env)
        if override:
            return Path(override).expanduser()
    try:
        return root(key, path=path)
    except EstateError:
        engine = Path(__file__).resolve().parent.parent
        return engine if key == "engine_root" else engine.parent / "ECHELON"


def project(name: str, *, path: Optional[Path] = None) -> Path:
    """A per-project root from the `projects` map, e.g. project('alpha-app')."""
    return Path(str(get("projects." + name, path=path))).expanduser()


def projects(*, path: Optional[Path] = None) -> Dict[str, str]:
    """The whole projects map (name -> root). Empty dict if none declared."""
    data = load(path)
    found = data.get("projects")
    return dict(found) if isinstance(found, dict) else {}


def optional(key: str, default: Any = None, *, path: Optional[Path] = None) -> Any:
    """For genuinely optional wiring (an archive dir that may not exist here).

    Distinct from get() on purpose: reaching for this is a claim that absence is
    NORMAL, not that a missing key is tolerable.
    """
    try:
        return get(key, path=path)
    except (EstateKeyError, EstateConfigMissing):
        return default
