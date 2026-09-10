"""Context Pins — persistent session-scoped memory anchors.

A Pin is a piece of context that survives context-window pressure via a
placeholder lifesign protocol. The system injects pinned context into the
persistent system field. The model appends a unique ASCII marker to its text
output. The system checks responses — missing marker triggers re-injection
(self-healing); two consecutive misses and the pin is auto-removed.

Shared by proxy.py (Claude Code ↔ Anthropic API) and loop.py (native
ECHELON agent). One PinRegistry, two consumers, zero duplication.

Protocol:
  - Model appends <<echelon-pin:<type>:<id>> to every text response
  - System checks: found → alive (light anchor), missing → stale (full re-inject)
  - 2 consecutive stale → auto-removed (death-spiral cap)
  - Max 5 pins. Tool-call-only responses skip check.

Locking: set async_lock=True for async contexts (proxy Starlette handlers);
set async_lock=False for sync contexts (agent loop). Default is True.
"""
from __future__ import annotations

import asyncio
import json
import threading
from dataclasses import dataclass, field

# ── Placeholder ──────────────────────────────────────────────────────────

_PLACEHOLDER_PREFIX = "<<echelon-pin:"
_PLACEHOLDER_SUFFIX = ">>"


def _make_placeholder(pin_id: str) -> str:
    """Build a unique detectable marker from the composite pin_id (e.g. 'cartridge:ux').
    ASCII delimiters survive any encoding."""
    return f"{_PLACEHOLDER_PREFIX}{pin_id}{_PLACEHOLDER_SUFFIX}"


def _parse_placeholder(text: str) -> list[tuple[str, str]]:
    """Find all <<echelon-pin:type:id>> markers in text.
    Returns list of (pin_type, pin_id) tuples."""
    found: list[tuple[str, str]] = []
    idx = 0
    while True:
        start = text.find(_PLACEHOLDER_PREFIX, idx)
        if start == -1:
            break
        inner_start = start + len(_PLACEHOLDER_PREFIX)
        end = text.find(_PLACEHOLDER_SUFFIX, inner_start)
        if end == -1:
            break
        inner = text[inner_start:end]
        if ":" in inner:
            pin_type, pin_id = inner.split(":", 1)
            found.append((pin_type.strip(), pin_id.strip()))
        idx = end + len(_PLACEHOLDER_SUFFIX)
    return found


# ── Pin ──────────────────────────────────────────────────────────────────

@dataclass
class Pin:
    """One piece of pinned context. State machine: stale → alive → stale → removed."""
    pin_id: str              # "cartridge:ux", "atom:yagni-ladder"
    pin_type: str            # "cartridge" | "card" | "atom" | "custom"
    label: str               # human-readable, from registry spec or user
    content_full: str        # full context, injected on stale (equip / re-inject)
    placeholder: str = ""    # derived: "<<echelon-pin:cartridge:ux>>"
    state: str = "stale"     # "alive" | "stale"
    stale_count: int = 0     # consecutive text responses without placeholder

    def __post_init__(self) -> None:
        if not self.placeholder:
            self.placeholder = _make_placeholder(self.pin_id)

    @property
    def content_light(self) -> str:
        """Auto-derived one-line anchor. Never stored — built from label + placeholder."""
        return f"{self.placeholder} — {self.label}"


# ── PinRegistry ──────────────────────────────────────────────────────────

@dataclass
class PinRegistry:
    """Thread-safe registry of active context pins. Shared by proxy and agent loop.

    Inject:  call build_system_text() each request/step, append to system field.
    Check:   call check_response(text) / check_response_sync(text) after model text response.
    Control: add()/remove()/list() (async) or add_sync()/remove_sync()/list_sync() (sync).

    Set async_lock=True for proxy (async Starlette handlers).
    Set async_lock=False for agent loop (synchronous step loop).
    """
    MAX_PINS: int = field(default=5)
    async_lock: bool = field(default=True)

    _pins: dict[str, Pin] = field(default_factory=dict)
    _async_lock: asyncio.Lock | None = None
    _sync_lock: threading.Lock | None = None

    def __post_init__(self) -> None:
        if self.async_lock:
            self._async_lock = asyncio.Lock()
        else:
            self._sync_lock = threading.Lock()

    def _acquire(self):
        if self._async_lock is not None:
            raise RuntimeError("use async methods with async_lock=True")
        if self._sync_lock is not None:
            self._sync_lock.acquire()

    def _release(self):
        if self._sync_lock is not None:
            self._sync_lock.release()

    # ── async control (proxy path) ───────────────────────────────────

    async def add(self, pin_id: str, pin_type: str, label: str,
                  content_full: str) -> Pin:
        """Add a pin (async). Starts stale. Raises ValueError at MAX_PINS."""
        async with self._async_lock:
            return self._add_impl(pin_id, pin_type, label, content_full)

    async def remove(self, pin_id: str) -> bool:
        """Remove a pin (async). Returns True if it existed."""
        async with self._async_lock:
            return self._pins.pop(pin_id, None) is not None

    async def list(self) -> list[Pin]:
        """Snapshot of all active pins (async)."""
        async with self._async_lock:
            return list(self._pins.values())

    async def check_response(self, response_text: str) -> dict[str, str]:
        """Check response text for placeholders (async)."""
        async with self._async_lock:
            return self._check_impl(response_text)

    # ── sync control (agent loop path) ───────────────────────────────

    def add_sync(self, pin_id: str, pin_type: str, label: str,
                 content_full: str) -> Pin:
        """Add a pin (sync). Starts stale. Raises ValueError at MAX_PINS."""
        self._acquire()
        try:
            return self._add_impl(pin_id, pin_type, label, content_full)
        finally:
            self._release()

    def remove_sync(self, pin_id: str) -> bool:
        """Remove a pin (sync). Returns True if it existed."""
        self._acquire()
        try:
            return self._pins.pop(pin_id, None) is not None
        finally:
            self._release()

    def list_sync(self) -> list[Pin]:
        """Snapshot of all active pins (sync)."""
        self._acquire()
        try:
            return list(self._pins.values())
        finally:
            self._release()

    def check_response_sync(self, response_text: str) -> dict[str, str]:
        """Check response text for placeholders (sync)."""
        self._acquire()
        try:
            return self._check_impl(response_text)
        finally:
            self._release()

    # ── shared implementation ────────────────────────────────────────

    def _add_impl(self, pin_id: str, pin_type: str, label: str,
                   content_full: str) -> Pin:
        if pin_id in self._pins:
            p = self._pins[pin_id]
            p.label = label
            p.content_full = content_full
            p.state = "stale"
            p.stale_count = 0
            return p
        if len(self._pins) >= self.MAX_PINS:
            raise ValueError(
                f"Pin limit reached ({self.MAX_PINS}). Unpin one first: "
                f"{', '.join(self._pins)}")
        p = Pin(pin_id=pin_id, pin_type=pin_type, label=label,
                content_full=content_full, state="stale", stale_count=0)
        self._pins[pin_id] = p
        return p

    def _check_impl(self, response_text: str) -> dict[str, str]:
        if not response_text or not response_text.strip():
            return {}
        found = _parse_placeholder(response_text)
        found_ids = {f"{t}:{i}" for t, i in found}
        transitions: dict[str, str] = {}
        for pin_id, p in list(self._pins.items()):
            if pin_id in found_ids:
                if p.state == "stale":
                    transitions[pin_id] = "stale→alive (recovered)"
                elif p.stale_count > 0:
                    transitions[pin_id] = f"alive (reset from {p.stale_count} stale)"
                p.state = "alive"
                p.stale_count = 0
            else:
                if p.state == "alive":
                    p.state = "stale"
                    p.stale_count = 1
                    transitions[pin_id] = "alive→stale (placeholder missing)"
                elif p.state == "stale":
                    p.stale_count += 1
                    if p.stale_count >= 2:
                        del self._pins[pin_id]
                        transitions[pin_id] = (
                            f"stale→removed ({p.stale_count} consecutive misses, "
                            f"death-spiral cap)")
                    else:
                        transitions[pin_id] = (
                            f"stale (count={p.stale_count}, light anchor next)")
        return transitions

    def snapshot(self) -> dict[str, Pin]:
        """Non-locked snapshot for build_system_text. Safe because it's called
        between steps/requests (not concurrent with check_response)."""
        return dict(self._pins)

    # ── injection ────────────────────────────────────────────────────

    def build_system_text(self) -> str:
        """Build the PINNED section for the system field/prompt.
        Call each request/step. Sync — reads a snapshot of current pins."""
        pins = self.snapshot()
        if not pins:
            return ""

        lines = [
            "",
            "PINNED — when you output text, append each active marker at the end:",
        ]

        for p in pins.values():
            lines.append(f"  {p.content_light}")
            if p.state == "stale":
                # Death-spiral cap: first stale injects full content.
                # If it's the second stale (stale_count==1), inject light only
                # (don't flood an already-full window).
                if p.stale_count == 0:
                    lines.append(f"    [Re-injected — context was lost or newly pinned]")
                    lines.append(f"    {p.content_full[:800]}")
                    if len(p.content_full) > 800:
                        lines.append(f"    ... (truncated, {len(p.content_full)} chars total)")
                else:
                    lines.append(f"    [Still stale — light anchor only. "
                                 f"One more chance, then auto-removed.]")

        lines.append("")
        lines.append(
            "When a pin has served its purpose, stop including its marker — it will "
            "expire after 2 responses without it. Or call unpin(id) to remove immediately. "
            f"Max {self.MAX_PINS} pins. If context is tight, keep only the critical ones alive."
        )
        return "\n".join(lines)

# ── SSE Accumulator ─────────────────────────────────────────────────────

class _SSEAccumulator:
    """Accumulate text_delta content from an Anthropic SSE byte stream.

    Feed raw bytes as they arrive; the accumulator buffers partial events
    and extracts text from complete content_block_delta events. After the
    stream ends, read `text` for the full concatenated model output.

    Handles split event boundaries (\\n\\n may span chunks).
    """

    def __init__(self) -> None:
        self._partial: str = ""
        self.text: str = ""

    def feed(self, chunk: bytes) -> None:
        """Feed raw SSE bytes. Processes complete events, buffers partial ones."""
        self._partial += chunk.decode("utf-8", errors="replace")
        while "\n\n" in self._partial:
            event_str, self._partial = self._partial.split("\n\n", 1)
            self._parse_event(event_str)

    def _parse_event(self, event_str: str) -> None:
        """Parse one SSE event (delimited by \\n\\n)."""
        for line in event_str.split("\n"):
            if not line.startswith("data: "):
                continue
            try:
                data = json.loads(line[6:])
            except (json.JSONDecodeError, KeyError):
                continue
            if data.get("type") != "content_block_delta":
                continue
            delta = data.get("delta", {})
            if delta.get("type") == "text_delta":
                self.text += delta.get("text", "")


# ── Response text extraction (non-streaming) ────────────────────────────

def _extract_response_text(body: bytes) -> str | None:
    """Extract concatenated text from a non-streaming Anthropic response body.
    Returns None if there is no text content (tool-call-only response)."""
    try:
        data = json.loads(body)
    except (json.JSONDecodeError, KeyError, TypeError):
        return None
    blocks = data.get("content", [])
    texts = [b["text"] for b in blocks
             if isinstance(b, dict) and b.get("type") == "text" and b.get("text")]
    return "\n".join(texts) if texts else None
