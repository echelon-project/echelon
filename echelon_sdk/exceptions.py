"""echelon_sdk.exceptions — the framework's exception spine.

The chainboard primitives (ChainResult, BoardBase) and the scanner raise these.
This module is PURE: it imports nothing (not even within echelon). It is the
root of the library layer — everything may import it, it imports nothing upward.

Ported from the canonical chainboard-atom-framework (which used a foreign
`sdk.exceptions.FrameworkError`); here the SDK owns its own exceptions so the
engine/apps never reach into a foreign package.
"""
from __future__ import annotations


class EchelonError(Exception):
    """Root of all echelon framework errors."""


class FrameworkError(EchelonError):
    """A chainboard-framework contract violation at runtime (a closed gate, a
    failed required chain step). The canonical framework's FrameworkError, kept
    by name so ported chain/board code reads identically."""


class ScannerError(EchelonError):
    """An architecture-law violation caught by the scanner at import time.
    Raising this on `import echelon_engine` is intentional: the app will not
    start if a layer boundary is broken. See echelon_engine/_scanner.py."""


class ToolError(EchelonError):
    """Raised when a tool refuses (guard violation) or fails cleanly.

    Lives in the SDK to CUT WELD #4: in echelon-agent this class was defined in
    `tools.py`, and `tools_fileops.py` did `from .tools import ToolError`, while
    `tools.py` imported `_FileOpsMixin` back from `tools_fileops` — a cycle held
    together only by an import-ORDERING hack (the class had to be defined before
    the mixin import, with a `# noqa: E402`). The type is pure (a bare Exception),
    so it belongs in the library: once both tool modules import it from here, the
    cycle is gone and neither module depends on the other."""
