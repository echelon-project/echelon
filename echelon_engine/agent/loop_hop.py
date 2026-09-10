"""The HOP — unbounded-run continuity by reconstruction, not persistence.

The economic law `reboot_at_tokens` already encodes ("continuity is reconstruction,
not persistence") has two lifecycles:

  - REBOOT (existing): a wake-act-SLEEP role crosses the token threshold, FINISHES,
    and the society BUS re-wakes it fresh from the bank. Terminal. The bank re-forms it.
  - HOP (here): a CONTINUOUS long run crosses a WINDOW fraction, flushes its transcript
    noise IN-PLACE, and re-enters by RELIVING its own spine. The agent never leaves the
    process; its self-prepared spine (not the bank) re-forms it. The run continues.

Two answers to the same law, for two lifecycles. A run sets one or the other (or neither);
they never both fire. This module is the HOP half — the missing twin.

THE THREE DURABLE-ACROSS-HOP CHANNELS (none touches the bank — `pin != earned weight`):

  - SPINE (update_spine): an ephemeral pin the agent prepares before a hop — whatever it
    deems load-bearing for the run. Rides the system field across the flush. NEVER an atom.
  - STASH (stash): a pure in-memory one-pass hold for raw transcript/scratch the agent wants
    to keep alive but is NOT a transferable lesson. Survives a hop (same process); dies with
    the process (fine — the unbounded run dies with it too, nothing to resume into).
  - LEDGER (session state): STRUCTURED continuity — tasks, decisions, files, goals — that the
    agent maintains DURING operation via the session state ledger. Injected at hop re-entry as
    machine-readable state (not narrative to parse). Survives the PROCESS (JSONL on disk) —
    a HOP from a fresh process can still resume open work.

These three FREE channels exist to KILL THE EXCUSE: an agent must never write a low-value atom
"because there was no other way to keep it." There is a way (three ways), and none is the
bank. So an atom write must clear the real bar: a load-bearing, transferable lesson. The
guard is structural — no channel has any code path to store.remember().
"""
from __future__ import annotations

# Rough chars-per-token; matches the loop's existing size-split heuristic (alt_driver).
_CHARS_PER_TOKEN = 4.0
# Default model context window (tokens) when the caller doesn't pass one. Conservative.
_DEFAULT_WINDOW_TOKENS = 128_000

# Reserved pin id for the agent's spine. One spine per run; updating replaces it.
# Defined in echelon_sdk (the pure library root) so atoms can import them without
# violating the atoms→agent architectural boundary. Re-exported here for backward compat.
from echelon_sdk.spine_constants import SPINE_PIN_ID, SPINE_PIN_TYPE  # noqa: F401 — re-export


# ── window measurement ───────────────────────────────────────────────────

def messages_chars(messages: list[dict]) -> int:
    """Total character count of string message contents (the size-split heuristic)."""
    return sum(len(m.get("content") or "") for m in messages
               if isinstance(m.get("content"), str))


def window_fraction(messages: list[dict], window_tokens: int) -> float:
    """Live context as a fraction of the model's window (0.0–1.0+)."""
    if window_tokens <= 0:
        return 0.0
    est_tokens = messages_chars(messages) / _CHARS_PER_TOKEN
    return est_tokens / window_tokens


def forecast_post_hop_fraction(base_messages: list[dict], pins_system_text: str,
                               relive_text: str, window_tokens: int) -> float:
    """Projected window fraction AFTER a hop: what the fresh transcript will weigh.

    A hop keeps only [system, env] (base_messages) + the pins' system text (spine rides
    along) + the relive replay. This is what the agent will wake holding. If it projects
    too heavy, the hop buys little runway — feed this back so the agent TRIMS its spine."""
    chars = messages_chars(base_messages) + len(pins_system_text or "") + len(relive_text or "")
    est_tokens = chars / _CHARS_PER_TOKEN
    return est_tokens / window_tokens if window_tokens > 0 else 0.0


# ── stash: pure in-memory one-pass hold (NEVER the bank) ──────────────────

class StashStore:
    """A process-lifetime scratch hold. Survives a hop (same process); not an atom, ever.

    The excuse-killer: the agent has a legitimate home for raw blocks it wants to keep
    alive that ISN'T store.remember(). No method here writes to the bank — by construction.
    """
    __slots__ = ("_items",)

    def __init__(self) -> None:
        self._items: dict[str, str] = {}

    def put(self, key: str, text: str) -> str:
        self._items[key] = text
        return key

    def get(self, key: str) -> str | None:
        return self._items.get(key)

    def keys(self) -> list[str]:
        return list(self._items)

    def index_text(self) -> str:
        """A one-line-per-stash index for the system field, so the agent knows what it holds."""
        if not self._items:
            return ""
        lines = ["STASHED (raw holds — call stash_get(key) to read; NOT memories, NOT atoms):"]
        for k, v in self._items.items():
            lines.append(f"  {k} — {len(v)} chars")
        return "\n".join(lines)


# ── the hop itself ─────────────────────────────────────────────────────────

def base_messages(messages: list[dict]) -> list[dict]:
    """The durable head kept across a flush: the system prompt + any system-role env block.
    Everything after the first user turn (the GOAL and all step noise) is dropped."""
    head: list[dict] = []
    for m in messages:
        if m.get("role") == "system":
            head.append(m)
        else:
            break
    return head


def build_relive(goal: str, step: int, drift: int, plan, sequence) -> str:
    """The RELIVE replay — reuse checkpoint.offer_text on a LIVE in-memory snapshot.
    Same mechanism the brain-switch re-wake already uses; here it re-grounds the agent
    after a self-triggered flush. Rediscovery (re-choose into the walk), not blind replay."""
    from echelon_sdk import checkpoint as _ckpt
    live_ck = {"goal": goal, "step": step, "drift": drift, "plan": plan, "sequence": sequence}
    return _ckpt.offer_text(live_ck)


def build_continuity_block(scope: str = "echelon", *,
                           project_path: str = "",
                           session_id: str = "") -> str:
    """Build the continuity block for HOP re-entry. Uses the discoverable
    compact protocol:

    1. First, check for an UNCONSUMED compact (written by a prior session).
       If found, consume it and return its continuity block. This lets a
       fresh Claude Code session resume ECHELON work without the frozen spine.
    2. Otherwise, read the session state ledger for structured state.

    Returns empty string if nothing to inject.
    """
    try:
        from echelon_engine.session_state import (SessionLedger, discover_compact,
                                                   consume_compact)

        # Check for unconsumed compact first (discovery protocol)
        compact = discover_compact(scope, project_path=project_path or None)
        if compact:
            continuity = compact.get("continuity", "")
            token = compact.get("token", "")
            if continuity:
                # Consume it with its own token — prevents double-consumption
                # under concurrent access (race-condition proof)
                consume_compact(
                    scope, compact["ts"],
                    token=token,
                    consumed_by=session_id or "",
                    project_path=project_path or compact.get("project_path", ""),
                )
                # Mark it as discovered in the text
                header = f"⚡ ECHELON CONTINUITY — consumed compact from {compact.get('session_uuid','?')[:12]}\n"
                return header + continuity

        # Fall back to reading the ledger directly (HOP within same session)
        ledger = SessionLedger(scope)
        if not ledger._path.exists():
            return ""
        block = ledger.continuity_block()
        if block.startswith("(No continuity data"):
            return ""
        return block
    except Exception:
        return ""


def hop(messages: list[dict], pins, *, goal: str, step: int, drift: int,
        plan, sequence, stash: "StashStore | None" = None,
        scope: str = "",
        project_path: str = "",
        session_id: str = "") -> str:
    """Flush the transcript IN-PLACE and re-enter via the spine + relive.

    Mutates `messages`: truncates to [system, env], re-appends the session state
    continuity block (structured tasks/decisions/files/goals — from the ledger, or
    from a discovered compact written by a prior session) + the pins' system text
    (the spine rides along in the system field) + any stash index + the relive replay.
    Returns the relive text (for the on_event trace).

    The continuity block uses the DISCOVERABLE COMPACT PROTOCOL: first checks for an
    unconsumed compact (written by a prior session/process), consumes it if found.
    Otherwise reads the session state ledger directly (same-process HOP).
    """
    head = base_messages(messages)
    relive = build_relive(goal, step, drift, plan, sequence)

    # rebuild messages in place: keep the head object identity (callers may hold the list ref)
    messages.clear()
    messages.extend(head)

    # Third channel: structured session state continuity.
    # Uses discoverable compact protocol — first checks for unconsumed compacts
    # (across processes), then falls back to the ledger (same process).
    if scope:
        continuity = build_continuity_block(
            scope, project_path=project_path, session_id=session_id)
        if continuity:
            messages.append({"role": "system", "content": continuity})

    # the pins' system text already carries the spine (an ephemeral pin); refresh it as a
    # system message so the flushed window wakes holding it.
    if pins is not None:
        pins_text = pins.build_system_text()
        if pins_text:
            messages.append({"role": "system", "content": pins_text})
    if stash is not None:
        stash_idx = stash.index_text()
        if stash_idx:
            messages.append({"role": "system", "content": stash_idx})

    # the relive lands as a user turn — the agent re-chooses into the walk (rediscovery).
    messages.append({"role": "user", "content": relive})
    return relive
