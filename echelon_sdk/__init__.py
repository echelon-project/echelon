"""echelon_sdk — the LIBRARY layer: pure primitives, zero upward imports.

This is the root of the dependency graph. Everything (engine, apps) may import
from echelon_sdk; echelon_sdk imports NOTHING from echelon_engine or apps. It has
no import-time I/O. Per the Mol "pure core" lesson, this is what makes echelon a
real library, reusable across other projects.

Contents (the modules gathered here as zero-import leaves from the restructure):
  - exceptions : EchelonError / FrameworkError / ScannerError / ToolError (the exception spine)
  - chain      : ChainResult (the canonical chainboard primitive)
  - (to migrate) config, keys, byte_read, roles, session, bus, checkpoint,
    gantt_pillars, convergence, repo_graph, environment, + pure memory leaves.

The scanner law: an sdk module may import only stdlib + other sdk modules. It must
never import echelon_engine or apps. Enforced at engine import time.
"""
from echelon_sdk.chain import ChainResult
from echelon_sdk.exceptions import EchelonError, FrameworkError, ScannerError, ToolError

__all__ = ["ChainResult", "EchelonError", "FrameworkError", "ScannerError", "ToolError"]
