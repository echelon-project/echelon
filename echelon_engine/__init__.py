"""echelon_engine — the ENGINE layer (the substrate machinery).

Importing this package RUNS THE SCANNER (validate_chains). If any architecture
law is broken — an upward import, a malformed chain, a service-gate violation —
`import echelon_engine` raises ScannerError and the app will not start. This is
the AlphaApp/Mol discipline: the boundary is a boot gate, not a lint warning.

Layers below this package: atoms (leaf I/O) -> echelon_sdk (pure library).
The one public surface for callers (apps/) is echelon_engine.services (the gate).
"""
from echelon_engine._scanner import validate_chains

validate_chains()

from echelon_engine._base import BoardBase

__all__ = ["BoardBase"]
