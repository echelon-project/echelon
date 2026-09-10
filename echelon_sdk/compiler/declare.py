"""declare.py — the swarm agent's declare tool for compile-mode.

In compile-mode, agents DO NOT write files directly.  Instead they DECLARE
their intent: a drafted file body with dependency ordering and optional
behavioural annotations.  The compiler (compile.py) resolves the pool of
declarations into disk writes — NOT the agent's ToolRegistry.

``declare_impl()`` is the core function: it constructs an Intent, validates it
via the Intent schema, posts it to an IntentPool, and returns a confirmation
string.  It is called from the closure registered by
``_AttachMixin.attach_declare``.
"""

from __future__ import annotations

import json
from typing import Any, Callable, Optional

from .intent import Intent
from .intent_pool import IntentPool


def declare_impl(
    pool: IntentPool,
    id: str,
    part_of: str,
    target_path: str,
    action: str,
    body: str,
    deps: Optional[list[str]] = None,
    behavior: Optional[str] = None,
    canonical_for: Optional[str] = None,
    emit: Optional[Callable[[str, Any], None]] = None,
) -> str:
    """Declare a file-writing intent into the pool.  Does NOT write to disk.

    All required Intent fields are passed directly.  Returns a JSON string
    with the intent id and status on success, or an error string on failure.
    """
    # --- build the Intent (validates via __post_init__) ---------------------
    try:
        intent = Intent(
            id=id,
            part_of=part_of,
            target_path=target_path,
            action=action,
            body=body,
            deps=list(deps) if deps else [],
            behavior=behavior if behavior else None,
            canonical_for=canonical_for if canonical_for else None,
        )
    except ValueError as e:
        return f"ERROR: invalid intent: {e}"

    # --- post to the pool (validates collisions) ----------------------------
    try:
        pool.post(intent)
    except ValueError as e:
        return f"ERROR: {e}"

    if emit:
        emit("declare", {"id": intent.id, "target_path": intent.target_path,
                         "action": intent.action})

    return json.dumps({
        "declared": intent.id,
        "target_path": intent.target_path,
        "action": intent.action,
        "status": "posted",
    })
