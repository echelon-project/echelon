"""preflight — the ten checks that must pass BEFORE the sealed set is unlocked.

Every check here is a way a run can look valid and be meaningless. The point is to fail
LOUDLY at minute zero rather than discover at analysis time that all five cells ran at the
same effective effort, or that the backend rolled mid-experiment.

v1's own protocol contradicted itself here — it called the env unset "load-bearing" and then
later said "no env override needed". Both statements cannot be true, so this module decides:
the unset is checked, and a stray override is a HARD FAIL.
"""
from __future__ import annotations

import os
import platform
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


@dataclass
class Check:
    name: str
    ok: bool
    detail: str
    hard: bool = True     # hard failures block the run


@dataclass
class Preflight:
    checks: list[Check] = field(default_factory=list)
    env: dict[str, Any] = field(default_factory=dict)

    @property
    def passed(self) -> bool:
        return all(c.ok for c in self.checks if c.hard)

    def render(self) -> str:
        lines = ["PREFLIGHT — sealed set stays locked until every hard check passes", ""]
        for c in self.checks:
            mark = "PASS" if c.ok else ("FAIL" if c.hard else "warn")
            lines.append(f"  [{mark}] {c.name}: {c.detail}")
        lines.append("")
        lines.append("  VERDICT: " + ("UNLOCK" if self.passed else "BLOCKED — do not run"))
        return "\n".join(lines)


def _effort_mapping_check() -> Check:
    """The load-bearing one. If requested efforts collapse on the wire, cells A/B/C are not
    distinct treatments and the whole effort question is unanswerable."""
    try:
        from echelon_engine.atoms.providers.deepseek import _thinking_fields
    except Exception as e:  # noqa: BLE001
        return Check("effort mapping", False, f"provider import failed: {e}")

    got = {}
    for req in ("low", "medium", "high", "xhigh", "max"):
        fields, _on = _thinking_fields("deepseek-v4-flash", {"reasoning_effort": req})
        got[req] = fields.get("reasoning_effort")
    want = {"low": "low", "medium": "high", "high": "high",
            "xhigh": "high", "max": "max"}
    if got != want:
        return Check("effort mapping", False, f"wire mapping {got} != vendor table {want}")
    distinct = {got["low"], got["high"], got["max"]}
    if len(distinct) != 3:
        return Check("effort mapping", False,
                     f"cells A/B/C collapse to {distinct} — not three treatments")
    return Check("effort mapping", True,
                 "low/high/max are distinct on the wire; medium+xhigh correctly fold to high")


def _env_override_check() -> Check:
    """CLAUDE_CODE_EFFORT_LEVEL takes precedence over every other effort mechanism, so a stray
    value silently overrides all five cells into one."""
    bad = {k: v for k, v in os.environ.items()
           if k in ("CLAUDE_CODE_EFFORT_LEVEL", "ANTHROPIC_MODEL",
                    "ANTHROPIC_DEFAULT_OPUS_MODEL", "DEEPSEEK_MODEL")}
    if bad:
        return Check("env overrides", False,
                     f"unset these before running: {bad}")
    return Check("env overrides", True, "no effort/model override in the environment")


def _thinking_enabled_check() -> Check:
    try:
        from echelon_engine.atoms.providers.deepseek import _thinking_fields
    except Exception as e:  # noqa: BLE001
        return Check("thinking mode", False, f"import failed: {e}")
    for m in ("deepseek-v4-flash", "deepseek-v4-pro"):
        _f, on = _thinking_fields(m, {"reasoning_effort": "high"})
        if not on:
            return Check("thinking mode", False, f"{m}: thinking OFF at effort=high")
    return Check("thinking mode", True, "enabled on both candidate models")


def _catalog_check() -> Check:
    try:
        from echelon_engine.atoms.providers.deepseek import model_info
    except Exception as e:  # noqa: BLE001
        return Check("model catalog", False, f"import failed: {e}")
    out = []
    for m in ("deepseek-v4-flash", "deepseek-v4-pro"):
        info = model_info(m)
        if not info:
            return Check("model catalog", False, f"{m}: no catalog entry")
        out.append(f"{m}={info.get('version')}")
    return Check("model catalog", True, " ".join(out))


def _pricing_check() -> Check:
    """Cold repricing must exist and must ignore the clock — otherwise a cell scored at 09:00
    UTC costs 2x one scored at 11:00 and the cost column measures time of day."""
    try:
        from echelon_engine.atoms.providers.cost import usd_cold, usd_of
    except Exception as e:  # noqa: BLE001
        return Check("cold pricing", False, f"import failed: {e}")
    a = usd_cold("deepseek-v4-flash", 1_000_000, 0)
    b = usd_cold("deepseek-v4-flash", 1_000_000, 0)
    if a != b or a <= 0:
        return Check("cold pricing", False, f"usd_cold unstable/zero: {a} vs {b}")
    warm = usd_of("deepseek-v4-flash", 1_000_000, 0, 1_000_000)
    if warm >= a:
        return Check("cold pricing", False, "cache-hit pricing is not cheaper than cold")
    return Check("cold pricing", True,
                 f"cold ${a:.4f}/1M-in is clock-independent; cached path is cheaper")


def _key_check() -> Check:
    try:
        from echelon_sdk.keys import load_deepseek_key
        k = load_deepseek_key()
    except Exception as e:  # noqa: BLE001
        return Check("api key", False, f"key load failed: {e}")
    return Check("api key", bool(k), "present" if k else "MISSING — every call will fail")


def _usage_capture_check() -> Check:
    """A bakeoff whose cost column is empty is a bakeoff with no cost answer."""
    try:
        from echelon_engine.atoms.providers.base import LLMResponse
    except Exception as e:  # noqa: BLE001
        return Check("usage capture", False, f"import failed: {e}")
    fields = LLMResponse.__dataclass_fields__
    need = ("tokens_in", "tokens_out", "tokens_cached", "reasoning_content")
    missing = [f for f in need if f not in fields]
    if missing:
        return Check("usage capture", False, f"LLMResponse lacks {missing}")
    return Check("usage capture", True, "tokens in/out/cached + reasoning_content captured")


def _git_check() -> Check:
    try:
        sha = subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                             capture_output=True, text=True, timeout=15).stdout.strip()
        dirty = subprocess.run(["git", "status", "--porcelain"],
                               capture_output=True, text=True, timeout=15).stdout.strip()
    except Exception as e:  # noqa: BLE001
        return Check("engine commit", False, f"git unavailable: {e}", hard=False)
    if dirty:
        return Check("engine commit", True,
                     f"{sha} (WORKING TREE DIRTY — the run is not reproducible from a commit)",
                     hard=False)
    return Check("engine commit", True, sha)


def run_preflight() -> Preflight:
    pf = Preflight()
    pf.checks = [
        _env_override_check(),
        _effort_mapping_check(),
        _thinking_enabled_check(),
        _catalog_check(),
        _pricing_check(),
        _key_check(),
        _usage_capture_check(),
        _git_check(),
    ]
    pf.env = {
        "utc": datetime.now(timezone.utc).isoformat(),
        "python": platform.python_version(),
        "platform": platform.platform(),
    }
    return pf
