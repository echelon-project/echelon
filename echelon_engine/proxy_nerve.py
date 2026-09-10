"""proxy_nerve — the NERVE: the severable pathway that carries autonomic signals on the trusted channel.

Anatomy (owner's naming law — ECHELON organs are named from human anatomy): the proxy is the NERVE.
A nerve carries the signal from the autonomic organs (dream/reflect/ingest/commit) to where it acts —
but a nerve can be SEVERED without killing the organism. That severability is the whole design: cut
the nerve and the organ dies, but the owner's connection to Claude (the life) is untouched. A cut
nerve is survivable; only a cut spine is not — so the nerve is exactly where the autonomic layer
belongs, because it is the one pathway you can make fail safely.

The problem this solves (owner, 2026-07-02): the ECHELON nervous system (dream/reflect/ingest/
commit — the autonomic organs from the protocol-as-tool north star) is safe to fire on claude-deep
and claude-gem because on those channels a failure just ends in an ERROR — an errored worker is a
dead worker, no loss. But the ORIGINAL Claude harness is different: it is the owner's trusted channel
to Claude itself. If an autonomic organ throws in the request/response critical path there, the proxy
returns a 500 and the owner is LOCKED OUT from Claude. That single failure mode is why the owner dares
wire the nervous system into the proxied workers but NOT into the original harness.

The law that removes the fear (not "we're careful" — the nerve structurally CAN'T break the channel):

  1. AN ORGAN NEVER SITS IN THE CRITICAL PATH. Autonomic organs OBSERVE the completed turn; they fire
     AFTER the response is already committed to the client, fire-and-forget. The response reaches the
     owner FIRST; the organ fires into the void after. It physically cannot delay or break the reply.

  2. EVERY SIGNAL IS SEVERABLE. `safe_fire` runs an organ inside a guard that can NEVER propagate
     — any exception (or timeout) is swallowed and logged, and the channel is untouched. Same contract
     the intent-gate already relies on ("the gate must never break a request", proxy.py), lifted into
     a reusable law so every future organ inherits it by construction — the nerve severs, the life holds.

This module is the NERVE, not the organs. It knows nothing about what dream/reflect/ingest do — it
only guarantees that whatever they do, the trusted channel survives them. Organs plug into the nerve
as callables; the law holds regardless of the organ. That is the whole point: severability is a
property of the PATHWAY, not a discipline asked of each organ.

Pure-ish: the nerve itself does no model calls and no request mutation. Unit-testable in isolation.
"""
from __future__ import annotations

import sys
import time
from typing import Any, Callable


def _nerve_log(msg: str) -> None:
    """Log a nerve event without EVER risking the channel — even at interpreter shutdown.

    A daemon organ thread that writes to stderr while the interpreter is finalizing can deadlock on
    the stderr buffer lock (a C-level fatal, not a catchable Python exception). Severability must hold
    even there: if the interpreter is shutting down, we drop the log rather than touch the lock. The
    signal is advisory; the life is not.
    """
    try:
        if getattr(sys, "is_finalizing", lambda: False)():
            return  # interpreter tearing down — do not touch the stderr lock
        sys.stderr.write(msg + "\n")
    except Exception:
        pass  # logging must never break the channel


# ── the trusted-channel test ─────────────────────────────────────────────────
# The nerve carries autonomic signals ONLY behind the original Claude harness (the trusted channel)
# when the owner has armed it. On the proxied workers (claude-deep/gem) the existing error-is-fine
# contract already holds, so the nerve is a no-op there unless explicitly enabled. Default: OFF — the
# nerve does not fire into the trusted channel until the owner flips it on, and even then only severably.
def nerve_armed(app_state: Any) -> bool:
    """True only if the owner has explicitly armed the nerve for this proxy instance."""
    return bool(getattr(app_state, "nerve_armed", False))


# ── the severability guarantee ───────────────────────────────────────────────
def safe_fire(
    organ: Callable[..., Any],
    *args: Any,
    _organ_name: str | None = None,
    _budget_s: float = 5.0,
    **kwargs: Any,
) -> dict:
    """Fire an autonomic organ under the severability law: it can NEVER break the caller.

    Returns a verdict dict {organ, fired, ok, error, ms} for logging/observability — but the RETURN
    is advisory only. The contract that matters is the one this function GUARANTEES: no exception
    from `organ` ever escapes, and no organ result is required for the channel to proceed.

    `_budget_s` bounds how long the organ may run — an organ that hangs is as dangerous as one that
    throws, so a soft wall-clock check on cooperative organs is part of severability. (Hard timeout
    of truly-blocking organs is the caller's job via off-thread dispatch; see fire_and_forget.)
    """
    name = _organ_name or getattr(organ, "__name__", "organ")
    started = time.monotonic()
    verdict: dict = {"organ": name, "fired": True, "ok": False, "error": None, "ms": 0}
    try:
        organ(*args, **kwargs)
        verdict["ok"] = True
    except BaseException as e:  # noqa: BLE001 — severability: NOTHING propagates, not even KeyboardInterrupt-class
        # A broken organ must be invisible to the channel. Log it, swallow it, move on.
        verdict["error"] = f"{type(e).__name__}: {e}"
        _nerve_log(f"[echelon nerve] organ '{name}' failed (severed, channel intact): "
                   f"{verdict['error']}")
    finally:
        verdict["ms"] = int((time.monotonic() - started) * 1000)
        if verdict["ms"] > _budget_s * 1000 and verdict["ok"]:
            _nerve_log(f"[echelon nerve] organ '{name}' overran budget "
                       f"({verdict['ms']}ms > {int(_budget_s*1000)}ms) — consider off-thread")
    return verdict


def fire_and_forget(
    organ: Callable[..., Any],
    *args: Any,
    _organ_name: str | None = None,
    **kwargs: Any,
) -> None:
    """Fire an organ OFF the critical path, in a daemon thread, under the severability law.

    This is clause 1 made concrete: the response has already been committed to the client before this
    is called, and the organ runs in a background daemon thread so it cannot delay the next request
    either. The thread inherits `safe_fire`'s guarantee — even a crash in the thread is swallowed and
    the process (hence the channel) is untouched. A daemon thread will not keep the process alive on
    shutdown, so a mid-flight organ at exit is simply abandoned, never a hang.
    """
    import threading
    name = _organ_name or getattr(organ, "__name__", "organ")

    def _run() -> None:
        safe_fire(organ, *args, _organ_name=name, **kwargs)

    try:
        threading.Thread(target=_run, name=f"nerve:{name}", daemon=True).start()
    except Exception as e:  # even spawning must not break the channel
        _nerve_log(f"[echelon nerve] could not spawn organ '{name}' (severed): "
                   f"{type(e).__name__}: {e}")
