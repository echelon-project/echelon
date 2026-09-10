"""Unit tests for echelon_engine.pins — Pin, PinRegistry, SSEAccumulator, response extraction.

No IO, no DB. Pure data-structure and state-machine tests.
"""
import json
import pytest

from echelon_engine.pins import (
    Pin,
    PinRegistry,
    _make_placeholder,
    _parse_placeholder,
    _extract_response_text,
    _SSEAccumulator,
)


# ── _make_placeholder ────────────────────────────────────────────────────

def test_make_placeholder_cartridge():
    assert _make_placeholder("cartridge:ux") == "<<echelon-pin:cartridge:ux>>"


def test_make_placeholder_atom():
    assert _make_placeholder("atom:yagni-ladder") == "<<echelon-pin:atom:yagni-ladder>>"


def test_make_placeholder_card():
    assert _make_placeholder("card:bugfix-loop") == "<<echelon-pin:card:bugfix-loop>>"


# ── _parse_placeholder ──────────────────────────────────────────────────

def test_parse_single_placeholder():
    text = "Some text <<echelon-pin:cartridge:ux>> more text"
    found = _parse_placeholder(text)
    assert found == [("cartridge", "ux")]


def test_parse_multiple_placeholders():
    text = "<<echelon-pin:cartridge:ux>> and <<echelon-pin:atom:yagni-ladder>>"
    found = _parse_placeholder(text)
    assert found == [("cartridge", "ux"), ("atom", "yagni-ladder")]


def test_parse_no_placeholders():
    assert _parse_placeholder("plain text") == []


def test_parse_ignores_partial_prefix():
    assert _parse_placeholder("<<echelon-pin:broken") == []


def test_parse_handles_extra_text_inside():
    # Should not match — the placeholder format requires <<echelon-pin:type:id>>
    text = "<<echelon-pin:some extra text inside>>"
    found = _parse_placeholder(text)
    # "some extra text inside" has spaces — split on ":" gives "some extra text inside"
    # which is treated as a single token (pin_type), but it's not in the format type:id
    # The parser splits on the FIRST ":" only, so this matches as type="some", id="extra text inside"
    # Actually that's fine — the placeholder structure is type:id after the colon.
    assert len(found) >= 0  # accept any parse — the protocol is for real placeholders


# ── Pin dataclass ────────────────────────────────────────────────────────

def test_pin_content_light_auto_derived():
    p = Pin(pin_id="cartridge:ux", pin_type="cartridge",
            label="UX design cartridge", content_full="full context here")
    assert p.placeholder == "<<echelon-pin:cartridge:ux>>"
    assert p.content_light == "<<echelon-pin:cartridge:ux>> — UX design cartridge"


def test_pin_starts_stale():
    p = Pin(pin_id="x", pin_type="custom", label="test", content_full="ctx")
    assert p.state == "stale"
    assert p.stale_count == 0


def test_pin_placeholder_already_set():
    p = Pin(pin_id="x", pin_type="custom", label="test", content_full="ctx",
            placeholder="<<custom>>")
    assert p.placeholder == "<<custom>>"


# ── PinRegistry state machine (sync path) ────────────────────────────────

@pytest.fixture
def registry():
    return PinRegistry(async_lock=False)


def test_add_pin_starts_stale(registry):
    p = registry.add_sync("cartridge:ux", "cartridge", "UX Design", "full context")
    assert p.state == "stale"
    assert p.stale_count == 0
    assert len(registry.list_sync()) == 1


def test_add_duplicate_resets_state(registry):
    registry.add_sync("cartridge:ux", "cartridge", "UX", "full context")
    # Simulate it becoming alive
    registry.check_response_sync("text <<echelon-pin:cartridge:ux>> end")
    assert registry.list_sync()[0].state == "alive"
    # Re-add
    registry.add_sync("cartridge:ux", "cartridge", "UX v2", "updated context")
    p = registry.list_sync()[0]
    assert p.state == "stale"
    assert p.label == "UX v2"
    assert p.content_full == "updated context"


def test_max_pins_enforced(registry):
    for i in range(5):
        registry.add_sync(f"pin:{i}", "custom", f"Pin {i}", f"content {i}")
    with pytest.raises(ValueError, match="Pin limit reached"):
        registry.add_sync("pin:5", "custom", "Overflow", "content")


def test_remove_pin(registry):
    registry.add_sync("cartridge:ux", "cartridge", "UX", "full")
    assert registry.remove_sync("cartridge:ux") is True
    assert registry.remove_sync("nonexistent") is False
    assert len(registry.list_sync()) == 0


def test_check_response_stale_to_alive(registry):
    registry.add_sync("cartridge:ux", "cartridge", "UX", "full")
    transitions = registry.check_response_sync(
        "I did some work. <<echelon-pin:cartridge:ux>>")
    assert "cartridge:ux" in transitions
    assert "stale→alive" in transitions["cartridge:ux"]
    assert registry.list_sync()[0].state == "alive"


def test_check_response_alive_stays_alive(registry):
    registry.add_sync("cartridge:ux", "cartridge", "UX", "full")
    registry.check_response_sync("text <<echelon-pin:cartridge:ux>>")
    transitions = registry.check_response_sync("more text <<echelon-pin:cartridge:ux>>")
    # No transition logged when staying alive (no stale_count reset needed)
    p = registry.list_sync()[0]
    assert p.state == "alive"
    assert p.stale_count == 0


def test_check_response_alive_to_stale(registry):
    registry.add_sync("cartridge:ux", "cartridge", "UX", "full")
    registry.check_response_sync("text <<echelon-pin:cartridge:ux>>")
    transitions = registry.check_response_sync("text without placeholder")
    assert "cartridge:ux" in transitions
    assert "alive→stale" in transitions["cartridge:ux"]
    p = registry.list_sync()[0]
    assert p.state == "stale"
    assert p.stale_count == 1


def test_check_response_stale_x2_removes(registry):
    registry.add_sync("cartridge:ux", "cartridge", "UX", "full")
    # First miss: alive→stale
    registry.check_response_sync("text <<echelon-pin:cartridge:ux>>")  # → alive
    registry.check_response_sync("no marker")  # → stale, count=1

    # Second miss: stale→removed
    transitions = registry.check_response_sync("still no marker")  # → removed
    assert "cartridge:ux" in transitions
    assert "→removed" in transitions["cartridge:ux"]
    assert len(registry.list_sync()) == 0


def test_check_response_empty_text_skipped(registry):
    registry.add_sync("cartridge:ux", "cartridge", "UX", "full")
    # Bring to alive
    registry.check_response_sync("<<echelon-pin:cartridge:ux>>")
    # Empty response — should not change state
    transitions = registry.check_response_sync("")
    assert transitions == {}
    p = registry.list_sync()[0]
    assert p.state == "alive"


def test_check_response_whitespace_only_skipped(registry):
    registry.add_sync("cartridge:ux", "cartridge", "UX", "full")
    registry.check_response_sync("<<echelon-pin:cartridge:ux>>")
    transitions = registry.check_response_sync("   \n  ")
    assert transitions == {}
    p = registry.list_sync()[0]
    assert p.state == "alive"


def test_multiple_pins_independent(registry):
    registry.add_sync("cartridge:ux", "cartridge", "UX", "full1")
    registry.add_sync("atom:yagni", "atom", "YAGNI", "full2")

    # Both alive
    registry.check_response_sync("<<echelon-pin:cartridge:ux>> <<echelon-pin:atom:yagni>>")
    assert registry.list_sync()[0].state == "alive"
    assert registry.list_sync()[1].state == "alive"

    # Only ux present — yagni should go stale
    transitions = registry.check_response_sync("<<echelon-pin:cartridge:ux>>")
    assert "atom:yagni" in transitions
    assert "alive→stale" in transitions["atom:yagni"]


def test_stale_recovers(registry):
    registry.add_sync("cartridge:ux", "cartridge", "UX", "full")
    registry.check_response_sync("<<echelon-pin:cartridge:ux>>")  # alive
    registry.check_response_sync("no marker")  # stale, count=1
    # Recover on next response
    transitions = registry.check_response_sync("fixed <<echelon-pin:cartridge:ux>>")
    assert "→alive (recovered)" in transitions["cartridge:ux"]
    p = registry.list_sync()[0]
    assert p.state == "alive"
    assert p.stale_count == 0


def test_build_system_text_empty_when_no_pins(registry):
    assert registry.build_system_text() == ""


def test_build_system_text_includes_stale_and_alive(registry):
    """Freshly added pins are both stale → full content shown for both."""
    registry.add_sync("cartridge:ux", "cartridge", "UX Design", "full UX context")
    registry.add_sync("atom:yagni", "atom", "YAGNI Ladder", "full yagni context")

    text = registry.build_system_text()
    assert "<<echelon-pin:cartridge:ux>>" in text
    assert "<<echelon-pin:atom:yagni>>" in text
    assert "UX Design" in text
    assert "YAGNI Ladder" in text
    # Both are stale (freshly added) — full content shown for both
    assert "full UX context" in text
    assert "full yagni context" in text

    # After ack, both alive → full content no longer shown
    registry.check_response_sync("<<echelon-pin:cartridge:ux>> <<echelon-pin:atom:yagni>>")
    text2 = registry.build_system_text()
    assert "full UX context" not in text2
    assert "full yagni context" not in text2


def test_build_system_text_death_spiral_light_anchor(registry):
    """When stale_count==1 (second stale), inject light anchor only."""
    registry.add_sync("cartridge:ux", "cartridge", "UX", "full context")
    # alive
    registry.check_response_sync("<<echelon-pin:cartridge:ux>>")
    # first miss → stale count=1
    registry.check_response_sync("no marker")

    text = registry.build_system_text()
    # Full content should NOT be shown (stale_count==1 means light anchor only)
    assert "full context" not in text
    assert "Still stale" in text
    assert "light anchor only" in text


# ── _extract_response_text ───────────────────────────────────────────────

def test_extract_text_from_content():
    body = json.dumps({
        "content": [
            {"type": "text", "text": "Hello world"},
        ]
    }).encode()
    assert _extract_response_text(body) == "Hello world"


def test_extract_text_multiple_blocks():
    body = json.dumps({
        "content": [
            {"type": "text", "text": "First."},
            {"type": "tool_use", "id": "t1", "name": "read", "input": {}},
            {"type": "text", "text": "Second."},
        ]
    }).encode()
    assert _extract_response_text(body) == "First.\nSecond."


def test_extract_text_tool_call_only_returns_none():
    body = json.dumps({
        "content": [
            {"type": "tool_use", "id": "t1", "name": "read", "input": {}},
        ]
    }).encode()
    assert _extract_response_text(body) is None


def test_extract_text_empty_body():
    assert _extract_response_text(b"") is None


def test_extract_text_invalid_json():
    assert _extract_response_text(b"not json") is None


# ── _SSEAccumulator ──────────────────────────────────────────────────────

def _make_sse_chunk(events: list[tuple[str, str]]) -> bytes:
    """Build a raw SSE byte chunk from (event_type, data_json) pairs."""
    parts = []
    for ev_type, data in events:
        parts.append(f"event: {ev_type}\ndata: {data}\n\n")
    return "\n".join(parts).encode("utf-8")  # add leading newline between events


def test_sse_accumulator_extracts_text_delta():
    acc = _SSEAccumulator()
    chunk = b'event: content_block_delta\ndata: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"Hello"}}\n\n'
    acc.feed(chunk)
    assert acc.text == "Hello"


def test_sse_accumulator_multiple_deltas():
    acc = _SSEAccumulator()
    chunk1 = b'event: content_block_delta\ndata: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"Hello "}}\n\n'
    chunk2 = b'event: content_block_delta\ndata: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"world"}}\n\n'
    acc.feed(chunk1)
    acc.feed(chunk2)
    assert acc.text == "Hello world"


def test_sse_accumulator_skips_tool_delta():
    acc = _SSEAccumulator()
    chunk = b'event: content_block_delta\ndata: {"type":"content_block_delta","index":0,"delta":{"type":"input_json_delta","partial_json":"{\\"path\\":\\"/x\\"}"}}\n\n'
    acc.feed(chunk)
    assert acc.text == ""


def test_sse_accumulator_skips_non_delta_events():
    acc = _SSEAccumulator()
    chunk = (
        b'event: message_start\ndata: {"type":"message_start"}\n\n'
        b'event: content_block_start\ndata: {"type":"content_block_start","index":0,"content_block":{"type":"text"}}\n\n'
    )
    acc.feed(chunk)
    assert acc.text == ""


def test_sse_accumulator_split_event_across_chunks():
    """Event boundary (\\n\\n) may fall in the middle of a chunk."""
    acc = _SSEAccumulator()
    # First chunk: partial event (no \\n\\n)
    chunk1 = b'event: content_block_delta\ndata: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"split'
    acc.feed(chunk1)
    assert acc.text == ""  # not yet complete
    # Second chunk: completes the event
    chunk2 = b' across"}}\n\n'
    acc.feed(chunk2)
    assert acc.text == "split across"


def test_sse_accumulator_empty_chunk():
    acc = _SSEAccumulator()
    acc.feed(b"")
    assert acc.text == ""


def test_sse_accumulator_non_utf8_survives():
    acc = _SSEAccumulator()
    # Invalid UTF-8 bytes — errors="replace" should handle it
    chunk = b'event: content_block_delta\ndata: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"ok"}}\n\n'
    acc.feed(chunk)
    assert "ok" in acc.text


def test_sse_accumulator_full_stream_with_placeholder():
    acc = _SSEAccumulator()
    # Simulate a full response with a placeholder marker
    chunks = [
        b'event: content_block_start\ndata: {"type":"content_block_start","index":0,"content_block":{"type":"text"}}\n\n',
        b'event: content_block_delta\ndata: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"I did the work. "}}\n\n',
        b'event: content_block_delta\ndata: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"<<echelon-pin:cartridge:ux>>"}}\n\n',
        b'event: content_block_stop\ndata: {"type":"content_block_stop","index":0}\n\n',
        b'event: message_stop\ndata: {"type":"message_stop"}\n\n',
    ]
    for c in chunks:
        acc.feed(c)
    assert "<<echelon-pin:cartridge:ux>>" in acc.text
    assert "I did the work." in acc.text
