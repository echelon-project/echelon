"""Exit protocol and scanner-heal helpers for loop finish handling."""
from __future__ import annotations

import enum
from typing import Any, Mapping


class ExitState(enum.Enum):
    """Three-state exit protocol."""

    STATE_SAFE = "safe"
    STATE_PENDING = "pending"
    STATE_BLOCKED = "blocked"

    def __str__(self) -> str:
        return self.value

    @property
    def can_exit(self) -> bool:
        return self == ExitState.STATE_SAFE

    @property
    def needs_operator(self) -> bool:
        return self == ExitState.STATE_BLOCKED


def _count(value: Any) -> int:
    """Normalise pending/error inputs into a non-negative count."""
    if isinstance(value, bool):
        return 1 if value else 0
    if isinstance(value, int):
        return max(0, value)
    if isinstance(value, (list, tuple, set, dict)):
        return len(value)
    return 0


def compact_chain_scan(scan: Mapping[str, Any] | None) -> dict[str, Any]:
    """Compact scanner output for event payloads."""
    report = scan or {}
    fail = report.get("fail") or []
    warn = report.get("warn") or []

    def _rules(rows: Any) -> list[str]:
        out: list[str] = []
        if not isinstance(rows, list):
            return out
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            rule = str(row.get("rule", "")).strip()
            if rule and rule not in out:
                out.append(rule)
        return out

    stats = report.get("stats")
    return {
        "fail": len(fail) if isinstance(fail, list) else _count(fail),
        "warn": len(warn) if isinstance(warn, list) else _count(warn),
        "fail_rules": _rules(fail),
        "warn_rules": _rules(warn),
        "stats": dict(stats) if isinstance(stats, Mapping) else {},
    }


def resolve_state(
    ledger: Any | None = None,
    pending_operations: int = 0,
    unresolved_votes: int | bool = 0,
    in_flight_operations: int | bool = 0,
    scanner_report: Mapping[str, Any] | None = None,
    scanner_fail: int | bool = 0,
    scanner_warn: int | bool = 0,
    has_errors: bool = False,
    has_conflicts: bool = False,
    **kwargs: Any,
) -> ExitState:
    """Resolve exit state with scanner-aware precedence rules."""
    del ledger, kwargs  # compatibility with existing callers
    scan = compact_chain_scan(scanner_report)
    fail_count = _count(scanner_fail) + _count(scan.get("fail"))

    # BLOCKED: hard failures always deny exit.
    if has_errors or has_conflicts or fail_count > 0:
        return ExitState.STATE_BLOCKED

    # PENDING: explicit unresolved votes or in-flight work.
    pending = _count(pending_operations) + _count(unresolved_votes) + _count(in_flight_operations)
    if pending > 0:
        return ExitState.STATE_PENDING

    # SAFE: clean or warn-only scanner report.
    _ = scanner_warn  # accepted for forward compatibility
    return ExitState.STATE_SAFE


def describe(state: ExitState, detail: str = "", warnings: list[str] | None = None) -> dict[str, Any]:
    """Structured description for partner-facing event payloads."""
    descriptions = {
        ExitState.STATE_SAFE: "Clean exit. All checks passed, no pending operations.",
        ExitState.STATE_PENDING: "Operations in flight. Waiting for completion or timeout.",
        ExitState.STATE_BLOCKED: "Unresolved conflict or error. Operator intervention required.",
    }
    warns = [str(w) for w in (warnings or []) if str(w).strip()]
    desc = descriptions[state]
    if state == ExitState.STATE_SAFE and warns:
        desc = "Clean exit with scanner warnings."
    return {
        "exit_state": state.value,
        "can_exit": state.can_exit,
        "needs_operator": state.needs_operator,
        "description": desc,
        "detail": detail,
        "warnings": warns,
    }


def apply_heal_verb(
    store: Any,
    verb: str,
    *,
    disclaimed: str = "",
    replacement: str = "",
) -> dict[str, Any]:
    """Apply a manual scanner-heal verb. Only supports orphan_disclaim."""
    if verb != "orphan_disclaim":
        return {"ok": False, "verb": verb, "reason": "unsupported_heal_verb"}
    if not disclaimed or not replacement:
        return {"ok": False, "verb": verb, "reason": "disclaimed_and_replacement_required"}
    linked = bool(store.link(disclaimed, replacement, "supersedes"))
    return {
        "ok": True,
        "verb": verb,
        "disclaimed": disclaimed,
        "replacement": replacement,
        "relation": "supersedes",
        "linked": linked,
    }
