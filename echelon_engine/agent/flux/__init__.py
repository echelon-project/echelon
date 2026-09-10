"""echelon_engine.agent.flux -- ECHELON's own Flux steering engine (agent layer).

Layer: ... -> echelon_engine.agent.flux (HERE) -> echelon_sdk + stdlib urllib only.
(No upward imports; the scanner's forbidden-upward map leaves `agent` free to reach
echelon_sdk + atoms downward. This package reaches only echelon_sdk.config + urllib.)

WHY THIS EXISTS (the in-flight build, 2026-06-19). Flux's CODE is mature; the GAP was the
STEERING PROTOCOL -- scattered across skills and "sometimes wrong" (see FLUX_STEERING_PROTOCOL.md,
[[flux-protocol-was-scattered-not-the-code]]). The load-bearing failure it names
([[flux-server-as-browser-session-is-forever]]): a session "executes something, then mints a NEW
canvas, looks at the fresh blank one, and says it didn't work" -- the re-mint-per-loop CARDINAL SIN.

A protocol DOCUMENT cannot stop the next session from re-minting; only CODE can. So this package
encodes the three load-bearing rules as STRUCTURE, not prose:
  - session-is-forever  -> MirrorSession mints ONCE in __init__; there is NO create() to call twice.
  - anchor-before-drive -> drive() refuses until anchor() has pointed the eye off about:blank.
  - drain-not-stream     -> observe() reads /api/v1/bus/drain (the llm-visible channel), never the
                            human iframe MutationObserver stream.

Two leaves (true-atom discipline -- tiny, single-job):
  node.py    -- FluxNode: own the connection to ONE running Flux node (host+port, the bus wire).
  mirror.py  -- MirrorSession: own ONE server-side browser surface for its whole life.
"""
from echelon_engine.agent.flux.node import FluxNode
from echelon_engine.agent.flux.mirror import MirrorSession, ReMintForbidden, NotAnchored

__all__ = ["FluxNode", "MirrorSession", "ReMintForbidden", "NotAnchored"]
