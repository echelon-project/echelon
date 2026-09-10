"""floor_degrade.py — the LOUD FLOOR (smart-recall slice 0).

THE BUG THIS KILLS (the silent-401 month): the local embedding floor requires a Bearer key. When the
key is missing/revoked, every /v1/embeddings call 401s and the semantic tier degraded to lexical
*SILENTLY* — `except Exception: return None` swallowed the auth failure, so recall ran wording-only for
weeks while looking healthy. A degrade is a legitimate fallback; a SILENT degrade is the bug. This
module makes the fall-through announce itself: on any floor-embed failure it prints ONE explicit
DEGRADED banner to stderr that NAMES the failing organ and the cause (auth-refused / unreachable /
bad-shape), then the caller returns None and falls to the lexical floor exactly as before.

WHY THE SDK LAYER: both callers of the floor embed live at/below the engine boundary
(`echelon_sdk.minilm_embed._floor_embed` and `echelon_engine.atoms.bank_embed.embed_text`). The SDK is
the bottom layer (stdlib only, no upward import), so the shared classify+banner helper lives here and
both callers reuse it — one banner shape, one place to tune. See minilm-embedder-is-the-one-jcode-harvest,
the dead-floor-embedder auth fix. Slice-0 constraint: this NEVER touches warmth.py or any verdict logic —
it only makes an existing degrade loud.
"""
from __future__ import annotations

import sys
import urllib.error


# One-shot suppression so a batch embed (ensure_embedded over hundreds of entries) prints the banner
# ONCE per organ+cause per process, not once per atom — a loud floor, not a screaming one. Keyed by
# (organ, cause); cleared only on a fresh process. The FIRST occurrence is always shown.
_ANNOUNCED: set[tuple[str, str]] = set()


def classify_floor_error(exc: BaseException) -> tuple[str, str]:
    """Map a floor-embed exception to (cause, detail). cause is one of:
      'auth-refused'  — HTTP 401/403 (the silent-401 bug: key missing/revoked/rejected)
      'http-error'    — any other HTTP status (400 no-model-loaded, 5xx, ...)
      'unreachable'   — connection refused / DNS / timeout (floor process down)
      'bad-shape'     — reached the floor but the response wasn't the OpenAI embedding shape
    detail is a short human string for the banner (status code / errno / exception class)."""
    if isinstance(exc, urllib.error.HTTPError):
        code = getattr(exc, "code", 0)
        if code in (401, 403):
            return "auth-refused", f"HTTP {code} (Bearer key missing/revoked/rejected)"
        return "http-error", f"HTTP {code}"
    if isinstance(exc, urllib.error.URLError):
        reason = getattr(exc, "reason", exc)
        return "unreachable", f"{type(reason).__name__ if isinstance(reason, BaseException) else reason}"
    if isinstance(exc, (KeyError, IndexError, TypeError, ValueError)):
        # reached the endpoint, got JSON, but not data[0].embedding — a shape/contract mismatch
        return "bad-shape", type(exc).__name__
    return "unreachable", type(exc).__name__


def announce_degrade(organ: str, exc: BaseException, *, once: bool = True,
                     stream=None) -> tuple[str, str]:
    """Print the DEGRADED banner naming the failing ORGAN and cause, then return (cause, detail) so the
    caller can log/act. A degrade is fine; a SILENT degrade is the bug — this is the noise that kills it.

    organ: the failing organ, e.g. 'floor-embedder (/v1/embeddings)'.
    once:  suppress repeats of the same (organ, cause) within this process (batch-embed friendliness).
           The first occurrence is ALWAYS printed.
    stream: defaults to sys.stderr (banner is diagnostic, not data — keeps stdout clean for pipes)."""
    cause, detail = classify_floor_error(exc)
    stream = stream if stream is not None else sys.stderr
    key = (organ, cause)
    if once and key in _ANNOUNCED:
        return cause, detail
    _ANNOUNCED.add(key)
    banner = (f"⚠ DEGRADED: {organ} unavailable [{cause}] — {detail}. "
              f"Falling back to the lexical floor (semantic tier OFF). "
              f"This recall runs wording-only until the organ is restored.")
    try:
        print(banner, file=stream, flush=True)
    except Exception:
        pass   # a broken stderr must never turn a degrade into a crash
    return cause, detail


def reset_announced() -> None:
    """Clear the one-shot suppression set (for tests, or a deliberate re-probe within one process)."""
    _ANNOUNCED.clear()
