"""mirror.py -- own ONE server-side browser surface for its whole life (re-mint made impossible).

THE LOAD-BEARING LEAF. The Flux mirror runs a real browser server-side; a session owns ONE
persistent tab for life (FLUX_STEERING_PROTOCOL.md s5b, [[flux-server-as-browser-session-is-forever]]).
The recurring sin across sessions: mint a session, then mint ANOTHER per loop iteration, look at the
fresh blank surface, and declare "it didn't work" -- abandoning the surface where the result landed.

A protocol DOC could not stop this; this class makes it STRUCTURALLY IMPOSSIBLE:

  1. session-is-forever  -- the session is minted ONCE, in __init__. There is NO public create() to
     call twice. You hold a MirrorSession; you cannot re-mint through it. To get a second surface you
     must deliberately construct a second object -- the sin is no longer a one-character mistake, it
     is an explicit, visible act.
  2. anchor-before-drive -- drive() RAISES NotAnchored until anchor() has pointed the eye off
     about:blank. The protocol's bug #1 (act-before-anchor: click hits about:blank, finds nothing,
     "looks broken") cannot happen -- the type system enforces the order.
  3. drain-not-stream    -- observe() reads the bus drain (the llm-visible channel). The human iframe
     MutationObserver stream is never consulted; llm-driven CDP mutations land in the drain.

Use it as a context manager so the tab is always torn down:

    node = FluxNode("http://127.0.0.1:8920")
    with MirrorSession(node, target="https://example.com/", mode="verify-fix") as m:
        m.anchor()                      # FORCE the eye onto the target (off about:blank)
        m.drive("click", tag="nav-001") # real click in the persistent tab
        events = m.observe()            # read the result through the drain (rev incremented)
"""
from __future__ import annotations

from typing import Any

from echelon_engine.agent.flux.node import FluxNode, FluxNodeError


class ReMintForbidden(RuntimeError):
    """Raised if anything tries to mint a second session through an existing MirrorSession.
    The cardinal sin (re-mint-per-loop) made loud. There is no legitimate path that triggers this --
    its existence is the guard, not a recoverable condition."""


class NotAnchored(RuntimeError):
    """Raised by drive()/observe() before anchor() has run. The tab is still on about:blank;
    driving it now is the protocol's bug #1 (act-before-anchor). Call anchor() first."""


class MirrorSession:
    """ONE server-side browser surface, minted once, driven for its whole life. The re-mint-proof
    handle the steering protocol s5b demands. Mint is in __init__ -- there is no create() method."""

    def __init__(self, node: FluxNode, *, target: str, mode: str = "verify-fix",
                 scope: str = "nav", actor: str = "llm") -> None:
        # Guardrail A (mirror.py:2004): a moded session REQUIRES a target -- a targetless moded
        # session is born blind. Refuse it here too, before the wire, with the same reason.
        if mode != "free" and not target:
            raise ValueError(f"mode {mode!r} requires a target URL "
                             "(only free mode may be targetless)")
        self._node = node
        self.target = target
        self.mode = mode
        self.scope = scope
        self.actor = actor
        self._anchored = False
        self._since = 0          # the bus event cursor; observe() advances it (drain semantics)
        self._ended = False

        # MINT ONCE. This is the only session.create this object will ever issue. The handle it
        # returns is immutable for the object's life -- session-is-forever, enforced by there being
        # no second call site anywhere in the class.
        env = self._node.bus({
            "kind": "session.create",
            "payload": {"target": target, "scope": scope, "mode": mode, "actor": actor},
        })
        payload = _unwrap(env)
        self.session: str = payload["session"]
        self.secret: str = payload["secret"]
        self.mirror_url: str = payload.get("mirror_url", "")

    # -- the three operations, in their only legal order ----------------------------------
    def anchor(self, *, settle: str = "dom", wait_for: str = "", settle_ms: int = 1500) -> dict:
        """Step 2 -- FORCE the tab off about:blank onto the target and wait for settle. Returns
        {tags[], html, rev}. Must run before any drive(). Idempotent: re-anchoring re-reads the
        live DOM (cheap), it does NOT re-mint -- same session, same surface."""
        result = self._guard_live(
            self._node.dom(self.session, settle=settle, wait_for=wait_for, settle_ms=settle_ms))
        self._anchored = True
        return result

    def drive(self, op: str, *, tag: str = "", value: str = "", **extra: Any) -> dict:
        """Step 3 -- run a REAL hand op (click/type/select/key/focus/scroll-to/...) in the persistent
        tab. RAISES NotAnchored if anchor() hasn't run (bug #1 guard). Same session+secret every time
        -- the drive lands on the ONE surface, never a fresh blank one."""
        if not self._anchored:
            raise NotAnchored(
                "drive() before anchor(): the tab is still on about:blank. Call anchor() first "
                "so the eye is on the target (protocol s5b, anchor-before-drive).")
        payload: dict = {"op": op}
        if tag:
            payload["tag"] = tag
        if value:
            payload["value"] = value
        payload.update(extra)
        return self._guard_live(
            self._node.bus({"kind": "hand", "payload": payload},
                           session=self.session, secret=self.secret))

    def observe(self, *, timeout: float = 2.0, kinds: str = "") -> list[dict]:
        """Step 4 -- read the RESULT through the bus drain (the llm-visible channel; NOT the human
        iframe stream). Advances the internal cursor so each call returns only NEW events. RAISES
        NotAnchored before the eye is anchored (nothing to observe yet)."""
        if not self._anchored:
            raise NotAnchored("observe() before anchor(): anchor the eye first.")
        out = self._guard_live(
            self._node.drain(self.session, since=self._since, timeout=timeout, kinds=kinds))
        self._since = out.get("next_since", self._since)
        return out.get("events", [])

    def end(self) -> None:
        """Step 6 -- close the tab and purge state. After this the session is dead; the object must
        not be reused (a new surface = a new, deliberately-constructed MirrorSession)."""
        if self._ended:
            return
        self._ended = True
        try:
            self._node.bus({"kind": "session.end"}, session=self.session, secret=self.secret)
        except FluxNodeError:
            pass  # best-effort teardown -- the server sweeper reaps idle sessions anyway

    # -- the structural guard against re-mint ---------------------------------------------
    def create(self, *args: Any, **kwargs: Any):  # noqa: D401 -- intentionally a trap
        """There is NO re-create. A MirrorSession is minted once in __init__ and is forever. Calling
        create() is the cardinal sin (re-mint-per-loop) -- so it raises, loudly, instead of silently
        abandoning the result surface. Want a second surface? Construct a second MirrorSession on
        purpose; you cannot do it by accident through this handle."""
        raise ReMintForbidden(
            "session-is-forever: a MirrorSession is minted once and never re-created. "
            "To open another surface, construct a new MirrorSession explicitly.")

    def _guard_live(self, result: dict) -> dict:
        """Every op asserts the session is still alive. A FluxNodeError already raised loudly upstream;
        this catches the logical 'used after end()' misuse."""
        if self._ended:
            raise ReMintForbidden("this session has ended; construct a new MirrorSession.")
        return result

    # -- context manager: the tab is always reaped ----------------------------------------
    def __enter__(self) -> "MirrorSession":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.end()

    def __repr__(self) -> str:
        state = "ended" if self._ended else ("anchored" if self._anchored else "blank")
        return f"MirrorSession(session={self.session!r}, target={self.target!r}, {state})"


def _unwrap(env: dict) -> dict:
    """The bus wraps replies as {ok, envelope:{..., payload}}. Pull the payload, raising on a
    non-ok or malformed envelope so a failed create can never present as a usable session."""
    if not isinstance(env, dict):
        raise FluxNodeError(f"bus returned non-dict: {env!r}")
    if env.get("ok") is False or "error" in env:
        raise FluxNodeError(f"bus rejected the call: {env}")
    payload = env.get("envelope", {}).get("payload")
    if not isinstance(payload, dict) or "session" not in payload:
        raise FluxNodeError(f"bus envelope missing session payload: {env}")
    return payload
