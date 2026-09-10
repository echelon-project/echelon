"""DispatchBoard — the capability surface for handing a goal to an equipped partner.

A Board is a bounded capability with a GATE (the operator kill-switch): close the gate and
ALL dispatch is hard-stopped, regardless of caller. The board holds NO orchestration logic —
it asserts its gate, then delegates to echelon_engine.services (the gate), which runs the
dispatch_chain. This is the AlphaApp discipline: transport -> Board(gate) -> services -> chain.

Naming protocol (scanner-enforced later): one class per board file, named <Stem>Board.
"""
from __future__ import annotations

from typing import Any

from echelon_engine._base import BoardBase


class DispatchBoard(BoardBase):
    """Dispatch a goal to an equipped partner. gate='closed' hard-stops every dispatch."""

    def dispatch(self, goal: str, **kwargs: Any):
        self._assert_gate_open()
        self._assert_deps_open()
        # delegate to the service gate (imported lazily to keep the board free of chain internals)
        from echelon_engine import services
        return services.run_dispatch(goal, **kwargs)
