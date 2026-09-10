"""Intent — the file-intent schema.

A single declarative record of what the compiler should write: a file to create
or edit, with its drafted body, dependency ordering, and optional behavioural
annotations.  The swarm proposes intents; the compiler resolves them into disk
writes.  This module is pure stdlib.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

# --- validation ------------------------------------------------------------------

_KEBAB_RE = re.compile(r'^[a-z][a-z0-9]*(-[a-z0-9]+)*$')
_VALID_ACTIONS = frozenset({'create', 'edit'})


def _require(v, name: str) -> None:
    """Raise ValueError if *v* is None or (for str) empty."""
    if v is None:
        raise ValueError(f"Intent.{name} is required")
    if isinstance(v, str) and not v.strip():
        raise ValueError(f"Intent.{name} must not be empty")


def _check_kebab(v: str, name: str) -> None:
    if not _KEBAB_RE.match(v):
        raise ValueError(
            f"Intent.{name} must be kebab-case (lowercase letters, digits, "
            f"single hyphens between segments), got {v!r}"
        )


# --- sentinel for "unknown" optional fields (absence ≠ default) -----------------

_UNSET = object()


# --- the dataclass ---------------------------------------------------------------

@dataclass
class Intent:
    """A single file-writing intent for the compiler.

    Required fields (constructor args without defaults):
        id            – kebab-case unique identifier for this intent
        part_of       – kebab-case identifier of the larger unit (plan, wave, …)
        target_path   – filesystem path the compiler should write to
        action        – ``"create"`` or ``"edit"``
        body          – the drafted source text
        deps          – list of ``id`` s this intent depends on (may be empty)

    Optional fields (default to *unknown* sentinel, not a falsey value):
        behavior      – optional behavioural hint (string or None / unset)
        canonical_for – optional canonical anchor path (string or None / unset)

    Validation in ``__post_init__`` rejects missing required fields, non-kebab
    ids, and unknown actions.
    """

    id: str = field(default=_UNSET)             # type: ignore[assignment]
    part_of: str = field(default=_UNSET)        # type: ignore[assignment]
    target_path: str = field(default=_UNSET)    # type: ignore[assignment]
    action: str = field(default=_UNSET)         # type: ignore[assignment]
    body: str = field(default=_UNSET)           # type: ignore[assignment]
    deps: list[str] = field(default_factory=list)

    behavior: Optional[str] = None
    canonical_for: Optional[str] = None

    def __post_init__(self) -> None:
        # --- required fields -------------------------------------------------
        for attr in ('id', 'part_of', 'target_path', 'action', 'body'):
            v = getattr(self, attr)
            if v is _UNSET:
                raise ValueError(f"Intent.{attr} is required")
            _require(v, attr)

        # --- kebab-case ids --------------------------------------------------
        _check_kebab(self.id, 'id')
        _check_kebab(self.part_of, 'part_of')

        # --- action enum -----------------------------------------------------
        if self.action not in _VALID_ACTIONS:
            raise ValueError(
                f"Intent.action must be 'create' or 'edit', got {self.action!r}"
            )

        # --- deps must be a list of kebab ids --------------------------------
        if not isinstance(self.deps, list):
            raise ValueError("Intent.deps must be a list")
        for i, d in enumerate(self.deps):
            if not isinstance(d, str):
                raise ValueError(f"Intent.deps[{i}] must be a str, got {type(d).__name__}")
            _check_kebab(d, f'deps[{i}]')

        # --- optional fields stay None when unset (None = unknown, not default)
        # behavior and canonical_for are Optional[str], default None – fine.

    def __repr__(self) -> str:
        return (f"Intent(id={self.id!r}, action={self.action!r}, "
                f"target_path={self.target_path!r})")
