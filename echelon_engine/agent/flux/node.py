"""node.py -- own the connection to ONE running Flux node (the bus wire).

A "node" is one Flux server process (FLUX_STEERING_PROTOCOL.md s1). A FluxNode is the thin,
honest client to it: it holds host+port and speaks the bus wire (POST /api/v1/bus,
GET /api/v1/bus/dom, GET /api/v1/bus/drain). It does ONE job -- carry a request to the node and
return the parsed JSON -- so mirror.py can be pure session-lifecycle logic on top.

House style (matches providers/grok.py, local.py): stdlib urllib only -- no httpx/requests dep
keeps the cartridge light. Errors are explicit (FluxNodeError), never swallowed: a dead node must
fail loudly at the call site, not return a fake-blank that gets misread as "it didn't work" (the
exact failure mode the whole flux package exists to prevent).

This is a TINY LEAF ([[true-atom-is-a-tiny-leaf]]): connection + one POST helper + two GET helpers.
The protocol intelligence (anchor-before-drive, mint-once) lives in mirror.py, not here.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any


class FluxNodeError(RuntimeError):
    """A bus call to the Flux node failed (connection refused, non-2xx, or bad JSON).
    Raised loudly so a dead node never masquerades as an empty/blank surface."""


class FluxNode:
    """The wire to ONE running Flux node. Construct with the node's base URL; the methods
    speak the three bus endpoints the steering protocol uses. Stateless beyond the address --
    a FluxNode does NOT own a session (that is MirrorSession's job, deliberately separated so
    re-mint cannot be expressed at the node level either)."""

    def __init__(self, base_url: str = "http://127.0.0.1:8920", *, timeout: float = 12.0) -> None:
        # Normalize: strip trailing slash so path joins are clean. No default port assumed beyond
        # the constructor arg -- the protocol's Trap (s1): "there is no default port in source."
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    # -- the wire -------------------------------------------------------------------------
    def _request(self, method: str, path: str, *, params: dict | None = None,
                 body: dict | None = None, timeout: float | None = None) -> Any:
        """One HTTP round-trip to the node, returning parsed JSON. The single choke point so every
        bus call shares the same error contract."""
        url = self.base_url + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=timeout or self.timeout) as resp:
                raw = resp.read().decode("utf-8")
        except urllib.error.HTTPError as e:  # non-2xx -- read the body for the node's reason
            detail = e.read().decode("utf-8", "replace")[:500] if e.fp else ""
            raise FluxNodeError(f"{method} {path} -> HTTP {e.code}: {detail}") from e
        except urllib.error.URLError as e:  # connection refused / DNS / timeout
            raise FluxNodeError(f"{method} {path} -> unreachable: {e.reason} "
                                f"(is a Flux node running at {self.base_url}?)") from e
        if not raw:
            return {}
        try:
            return json.loads(raw)
        except json.JSONDecodeError as e:
            raise FluxNodeError(f"{method} {path} -> non-JSON response: {raw[:200]!r}") from e

    # -- bus verbs (the three the protocol s5b uses) --------------------------------------
    def bus(self, payload: dict, *, session: str = "", secret: str = "") -> dict:
        """POST /api/v1/bus -- the universal bus entry. Used for session.create / hand / session.end.
        session+secret go on the query string per the protocol (POST /api/v1/bus?session=&secret=)."""
        params = {}
        if session:
            params["session"] = session
        if secret:
            params["secret"] = secret
        return self._request("POST", "/api/v1/bus", params=params or None, body=payload)

    def dom(self, session: str, *, settle: str = "dom", wait_for: str = "",
            settle_ms: int = 1500) -> dict:
        """GET /api/v1/bus/dom -- ANCHOR the eye. Forces the tab off about:blank onto the target,
        waits for settle, returns {tags[], html, rev}. The protocol's step 2 (anchor-before-drive)."""
        params: dict = {"session": session, "settle": settle, "settle_ms": settle_ms}
        if wait_for:
            params["wait_for"] = wait_for
        # DOM anchoring may navigate + settle a real page -- give it room past the default timeout.
        return self._request("GET", "/api/v1/bus/dom", params=params,
                             timeout=max(self.timeout, settle_ms / 1000.0 + 5.0))

    def drain(self, session: str, *, since: int = 0, timeout: float = 2.0,
              kinds: str = "", max_events: int = 50) -> dict:
        """GET /api/v1/bus/drain -- OBSERVE the llm-visible result. Long-polls the bus for events
        newer than `since`, returns {events, next_since}. The protocol's step 4 (drain-not-stream):
        llm-driven CDP mutations land here, NOT on the human iframe MutationObserver stream."""
        params = {"session": session, "since": since, "timeout": timeout,
                  "max_events": max_events}
        if kinds:
            params["kinds"] = kinds
        # the drain itself blocks up to `timeout` server-side; give the client a margin over it.
        return self._request("GET", "/api/v1/bus/drain", params=params,
                             timeout=timeout + max(self.timeout, 5.0))

    def __repr__(self) -> str:
        return f"FluxNode({self.base_url!r})"
