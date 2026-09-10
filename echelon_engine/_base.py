"""BoardBase — the capability-surface contract (ported from chainboard-atom-framework).

Every Board in echelon_engine inherits from BoardBase. The base enforces two things:

  1. **Gate state**: a board is `open` or `closed`. Public methods call
     `_assert_gate_open()` before doing work. A closed board may expose a
     `_fallback_*` method instead of failing (intentional degraded mode, e.g.
     cache-only reads when an upstream provider is unreachable). The gate is the
     operator KILL-SWITCH for a whole capability (model calls, file writes,
     memory mutation, world rounds) — first-class, not a buried conditional.

  2. **Dependency boundary**: a board declares other boards as `deps`. On each
     method entry, `_assert_deps_open()` confirms all deps are also `open`,
     preventing silent cascades through a board that "happens to still work."

A board does NOT contain orchestration logic. Boards are capability surfaces that
delegate to `echelon_engine.services` (the stable service gate). See PROTOCOL.md.
"""
from __future__ import annotations

from echelon_sdk.exceptions import FrameworkError


class BoardBase:
    def __init__(self, deps=None):
        self.gate = "open"
        self.deps = deps or []

    def _assert_gate_open(self):
        if self.gate != "open":
            raise FrameworkError(f"{self.__class__.__name__} gate is closed")

    def _assert_deps_open(self):
        for dep in self.deps:
            if dep.gate != "open":
                raise FrameworkError(
                    f"{self.__class__.__name__}: dependency "
                    f"{dep.__class__.__name__} gate is closed"
                )
