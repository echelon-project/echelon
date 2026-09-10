"""driver — ONE resolver for "which LLM drives this command?" (hardening, 2026-07-03).

THE FRAGILITY THIS CLOSES (owner): the same question was answered 20+ different ways
across the CLI — `--brain default="deepseek"`, `--model default="grok-4.3"`,
`--model default="deepseek-v4-pro"`, `--provider choices=["deepseek","gemini"]` vs
`["upstream","gemini"]` vs `"auto"` — each command hardcoding its own default and
failing its own way when a key is missing or a model retires.

THE LAW: every money-costing verb resolves its driver through THIS chain, and every
failure names the fix:

    1. explicit flag        (--brain grok / --model X — the user always wins)
    2. environment          (ECHELON_BRAIN / ECHELON_<PURPOSE>)
    3. config               (~/.echelon/config.json: {"brain": "deepseek", ...}
                             set via `echelon config set brain <name>`)
    4. auto-detect          first provider in _AUTO_ORDER whose keys actually load
    5. actionable error     names the purpose, the providers it tried, and the two
                            commands that fix it — never a bare KeyError/404.

PURPOSES are config keys: "brain" (agent loops), "judge" (recall escalation),
"scribe", "compact", ... A purpose may carry its own model override via
`<purpose>_model` in config (e.g. {"brain": "deepseek", "brain_model": "deepseek-v4-pro"}).

Usage in a verb's CLI:
    ap.add_argument("--brain", default=None, help="driver provider (default: resolved — "
                    "flag > ECHELON_BRAIN > config 'brain' > first live provider)")
    ...
    d = resolve_driver("brain", flag_provider=a.brain, flag_model=a.model)
    provider = d.instantiate()          # or route on d.provider / d.model yourself
"""
from __future__ import annotations

import importlib
import os
from dataclasses import dataclass

from .providers_cmd import _PROVIDER_REGISTRY

# Deterministic availability order for auto-detection (step 4). Cheap tiers first —
# the compute-tiering doctrine: reach for the cheapest tier that can do the work.
_AUTO_ORDER = ["deepseek", "grok", "gemini", "local", "bridge", "copilot", "codex", "venice", "eos"]


class DriverResolutionError(RuntimeError):
    """No provider could be resolved. The message always carries the fix."""


@dataclass(frozen=True)
class Driver:
    provider: str          # registry name, e.g. "deepseek"
    model: str             # concrete model id, e.g. "deepseek-v4-pro[1m]"
    source: str            # which chain step decided: flag | env | config | auto

    def instantiate(self):
        """Construct the provider instance (loads keys; raises only if the registry
        entry itself is broken — availability was already proven at resolve time)."""
        pkg, cls, _default = _PROVIDER_REGISTRY[self.provider]
        return getattr(importlib.import_module(pkg), cls)()


def _available(name: str) -> bool:
    """CHEAP availability probe: can the provider construct (keys load)? No network."""
    spec = _PROVIDER_REGISTRY.get(name)
    if spec is None:
        return False
    pkg, cls, _ = spec
    try:
        getattr(importlib.import_module(pkg), cls)()
        return True
    except Exception:
        return False


def resolve_driver(purpose: str, flag_provider: str | None = None,
                   flag_model: str | None = None) -> Driver:
    """Resolve which provider+model drives `purpose`, through the one chain.

    Raises DriverResolutionError with an actionable message — never a bare stack
    trace about a missing key three modules deep.
    """
    from echelon_sdk.config import get as config_get

    # 1. explicit flag — the user always wins, but an unknown name fails LOUDLY here,
    #    at the door, not later as a mid-run import error.
    chain: list[tuple[str, str | None]] = [
        ("flag", flag_provider),
        ("env", os.environ.get(f"ECHELON_{purpose.upper().replace('-', '_')}")),
        ("config", config_get(purpose, None)),
    ]
    for source, name in chain:
        if not name:
            continue
        if name not in _PROVIDER_REGISTRY:
            raise DriverResolutionError(
                f"unknown provider {name!r} for {purpose} (from {source}). "
                f"known: {', '.join(_PROVIDER_REGISTRY)}"
            )
        if not _available(name):
            raise DriverResolutionError(
                f"provider {name!r} for {purpose} (from {source}) has no working keys. "
                f"fix: add its key (see `echelon providers`), or pick another: "
                f"`echelon config set {purpose} <name>`"
            )
        return Driver(name, _pick_model(purpose, name, flag_model), source)

    # 4. auto-detect: first provider whose keys actually load, in deterministic order
    for name in _AUTO_ORDER:
        if _available(name):
            return Driver(name, _pick_model(purpose, name, flag_model), "auto")

    # 5. the actionable dead-end
    raise DriverResolutionError(
        f"no LLM provider available for {purpose!r} — none of "
        f"{', '.join(_AUTO_ORDER)} has working keys.\n"
        f"  fix 1: `echelon providers --test`   (see what's configured and what's missing)\n"
        f"  fix 2: `echelon config set {purpose} <provider>` after adding a key"
    )


def _pick_model(purpose: str, provider: str, flag_model: str | None) -> str:
    """Model precedence: flag > config '<purpose>_model' > registry default."""
    from echelon_sdk.config import get as config_get
    if flag_model:
        return flag_model
    cfg = config_get(f"{purpose}_model", None)
    if cfg:
        return str(cfg)
    return _PROVIDER_REGISTRY[provider][2]


def driver_help(purpose: str) -> str:
    """One consistent --help string for every verb's driver flag."""
    return (f"driver provider for {purpose} (default: resolved — flag > "
            f"ECHELON_{purpose.upper()} > config {purpose!r} > first provider with live keys; "
            f"see `echelon providers`)")
