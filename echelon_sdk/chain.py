"""ChainResult — fail-fast multi-step workflow primitive with execution history.

This is the CANONICAL chainboard ChainResult (the proven primitive used across
AlphaApp / Mol / chainboard-atom-framework), ported into echelon_sdk verbatim
except for the exception import (foreign `sdk.exceptions` -> `echelon_sdk`).
It REPLACES the orphaned reinvention that the GPT-floor pass left in
the old engine/chain.py (which was never wired into the runtime).

Use when a flow has:
  - multiple ordered steps
  - fail-fast semantics (one failure short-circuits the rest)
  - named steps
  - structured per-step errors
  - observer support (subscribe to ok/skipped/err transitions)

Canonical pattern:

    from echelon_sdk.chain import ChainResult

    result = (
        ChainResult.of(payload)
        .pipe(validate_input)
        .pipe(load_state)
        .where(lambda s: s.is_active)     # short-circuit if predicate false
        .pipe(transform_state)
        .pipe(save_state)
        .collect()
    )
    if result.ok:
        return result.value
    for err in result.errors:
        log.error(err)

The scanner (`echelon_engine/_scanner.py`) enforces chain shape at import time:
  - chains must start with `.of(...)`
  - chains must be straight-line (no construction inside if/for/while/with)
  - step names must be unique within a chain
  - `.on(...)` observers must be registered before the first `.pipe(...)`

If your flow doesn't benefit from execution history, use plain function calls.
"""
from __future__ import annotations

from echelon_sdk.exceptions import FrameworkError


def _unwrap(value):
    while isinstance(value, ChainResult):
        value = value.value
    return value


class ChainResult:
    def __init__(self):
        self._steps = []
        self._value = None
        self._skipping = False
        self._observers = []

    @classmethod
    def of(cls, value, name=None):
        c = cls()
        c._value = value
        step = {
            "name": name or "of",
            "ok": True,
            "skipped": False,
            "value": value,
            "error": None,
            "observer_error": None,
            "sub_steps": None,
        }
        c._steps.append(step)
        c._fire_observers(step)
        return c

    def on(self, flag, fn):
        self._observers.append((flag, fn))
        for step in self._steps:
            matched = (
                flag == "*"
                or (flag == "ok" and step["ok"] and not step["skipped"])
                or (flag == "skipped" and step["skipped"])
                or (flag == "err" and not step["ok"])
            )
            if matched:
                try:
                    fn(step)
                except Exception as exc:
                    step["observer_error"] = exc
        return self

    def _fire_observers(self, step):
        for flag, fn in self._observers:
            matched = (
                flag == "*"
                or (flag == "ok" and step["ok"] and not step["skipped"])
                or (flag == "skipped" and step["skipped"])
                or (flag == "err" and not step["ok"])
            )
            if matched:
                try:
                    fn(step)
                except Exception as exc:
                    step["observer_error"] = exc

    def _record(self, name, fn, *args, **kwargs):
        if self._skipping:
            step = {
                "name": name,
                "ok": True,
                "skipped": True,
                "value": self._value,
                "error": None,
                "observer_error": None,
                "sub_steps": None,
            }
            self._steps.append(step)
            self._fire_observers(step)
            return self
        try:
            result = fn(self._value, *args, **kwargs)
            if isinstance(result, ChainResult):
                unwrapped = _unwrap(result)
                step = {
                    "name": name,
                    "ok": result.ok,
                    "skipped": False,
                    "value": unwrapped,
                    "error": None,
                    "observer_error": None,
                    "sub_steps": result.steps,
                }
                self._value = unwrapped
            else:
                step = {
                    "name": name,
                    "ok": True,
                    "skipped": False,
                    "value": result,
                    "error": None,
                    "observer_error": None,
                    "sub_steps": None,
                }
                self._value = result
        except Exception as exc:
            self._skipping = True
            step = {
                "name": name,
                "ok": False,
                "skipped": False,
                "value": self._value,
                "error": exc,
                "observer_error": None,
                "sub_steps": None,
            }
        self._steps.append(step)
        self._fire_observers(step)
        return self

    def pipe(self, fn, *args, **kwargs):
        name = getattr(fn, "__name__", None) or getattr(fn, "__qualname__", "pipe")
        return self._record(name, fn, *args, **kwargs)

    def select(self, fn):
        name = getattr(fn, "__name__", None) or getattr(fn, "__qualname__", "select")
        return self._record(name, fn)

    def where(self, pred):
        if self._skipping:
            step = {
                "name": "where",
                "ok": True,
                "skipped": True,
                "value": self._value,
                "error": None,
                "observer_error": None,
                "sub_steps": None,
            }
            self._steps.append(step)
            self._fire_observers(step)
            return self
        try:
            passed = bool(pred(self._value))
            if not passed:
                self._skipping = True
            step = {
                "name": "where",
                "ok": True,
                "skipped": not passed,
                "value": self._value,
                "error": None,
                "observer_error": None,
                "sub_steps": None,
            }
        except Exception as exc:
            step = {
                "name": "where",
                "ok": False,
                "skipped": False,
                "value": self._value,
                "error": exc,
                "observer_error": None,
                "sub_steps": None,
            }
        self._steps.append(step)
        self._fire_observers(step)
        return self

    def where_ok(self):
        if self._skipping:
            step = {
                "name": "where_ok",
                "ok": True,
                "skipped": True,
                "value": self._value,
                "error": None,
                "observer_error": None,
                "sub_steps": None,
            }
            self._steps.append(step)
            self._fire_observers(step)
            return self
        all_ok = all(s["ok"] for s in self._steps)
        if not all_ok:
            self._skipping = True
        step = {
            "name": "where_ok",
            "ok": True,
            "skipped": not all_ok,
            "value": self._value,
            "error": None,
            "observer_error": None,
            "sub_steps": None,
        }
        self._steps.append(step)
        self._fire_observers(step)
        return self

    def collect(self):
        return self

    def require(self):
        for step in self._steps:
            if not step["ok"]:
                raise FrameworkError(
                    f"ChainResult step '{step['name']}' failed: {step['error']}"
                )
        return self

    @property
    def ok(self):
        return all(s["ok"] for s in self._steps)

    @property
    def value(self):
        for step in reversed(self._steps):
            if step["ok"] and not step["skipped"]:
                return step["value"]
        return None

    @property
    def steps(self):
        return list(self._steps)

    def first(self):
        if self._steps:
            return self._steps[0]["value"]
        return None

    def at(self, name):
        for step in self._steps:
            if step["name"] == name:
                return step["value"]
        raise FrameworkError(f"No step named '{name}' in chain")

    @property
    def errors(self):
        return [s for s in self._steps if not s["ok"]]

    @property
    def observer_errors(self):
        return [s for s in self._steps if s["observer_error"] is not None]

    def errors_deep(self):
        result = []
        self._collect_errors_recursive(self._steps, result)
        return result

    def _collect_errors_recursive(self, steps, result):
        for step in steps:
            if not step["ok"]:
                result.append(step)
            if step.get("sub_steps"):
                self._collect_errors_recursive(step["sub_steps"], result)
