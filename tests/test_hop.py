"""Tests for the HOP — the continuous twin of reboot_at_tokens (loop_hop + loop integration).

Proves: window math, the spine forecast, the in-place flush (head kept, spine+relive carried,
noise dropped), the stash hold, and the structural guard that NEITHER spine NOR stash has any
path to the bank (`pin != earned weight`).
"""
from __future__ import annotations

from echelon_engine.agent import loop_hop as lh
from echelon_engine.pins import PinRegistry


# ── window math ────────────────────────────────────────────────────────────

def test_window_fraction_scales_with_chars():
    small = [{"role": "user", "content": "x" * 4000}]
    big = [{"role": "user", "content": "x" * 400_000}]
    assert lh.window_fraction(small, 128_000) < 0.05
    assert lh.window_fraction(big, 128_000) > 0.5


def test_window_fraction_zero_window_is_safe():
    assert lh.window_fraction([{"role": "user", "content": "x"}], 0) == 0.0


def test_forecast_only_counts_head_plus_spine_plus_relive():
    head = [{"role": "system", "content": "s" * 1000}]
    light = lh.forecast_post_hop_fraction(head, "spine" * 10, "relive" * 10, 128_000)
    heavy = lh.forecast_post_hop_fraction(head, "spine" * 10_000, "relive" * 10, 128_000)
    assert heavy > light


# ── the hop flush ────────────────────────────────────────────────────────────

def _noisy_messages():
    return [
        {"role": "system", "content": "SYSTEM PROMPT"},
        {"role": "system", "content": "ENV BLOCK"},
        {"role": "user", "content": "GOAL: do the thing"},
        {"role": "assistant", "content": "thinking " * 100},
        {"role": "tool", "tool_call_id": "1", "content": "tool output " * 200},
        {"role": "assistant", "content": "more " * 100},
    ]


def test_base_messages_keeps_only_leading_system_head():
    head = lh.base_messages(_noisy_messages())
    assert [m["role"] for m in head] == ["system", "system"]


def test_hop_flushes_noise_keeps_head_carries_spine_and_relive():
    pins = PinRegistry(async_lock=False)
    pins.add_sync("custom:spine", "custom", "session spine",
                  "STATE: step 5; did A,B; left C,D")
    msgs = _noisy_messages()
    before = len(msgs)
    relive = lh.hop(msgs, pins, goal="do the thing", step=5, drift=0, plan=[],
                    sequence=[{"seq": 1, "action": "read_file(x)", "summary": "found it"}])
    # head preserved
    assert msgs[0]["content"] == "SYSTEM PROMPT"
    assert msgs[1]["content"] == "ENV BLOCK"
    # the spine rode along in the system field
    assert any("STATE: step 5" in (m.get("content") or "") for m in msgs)
    # the relive landed as the re-entry user turn
    assert msgs[-1]["role"] == "user" and "RELIVE" in msgs[-1]["content"]
    assert "RELIVE" in relive
    # the assistant/tool NOISE is gone
    assert not any(m.get("role") in ("assistant", "tool") for m in msgs)
    assert len(msgs) < before


def test_hop_includes_stash_index_when_present():
    pins = PinRegistry(async_lock=False)
    pins.add_sync("custom:spine", "custom", "spine", "state")
    stash = lh.StashStore()
    stash.put("raw-dump", "x" * 500)
    msgs = _noisy_messages()
    lh.hop(msgs, pins, goal="g", step=2, drift=0, plan=[], sequence=[], stash=stash)
    assert any("raw-dump" in (m.get("content") or "") for m in msgs)


# ── stash ────────────────────────────────────────────────────────────────────

def test_stash_round_trip_and_index():
    s = lh.StashStore()
    s.put("k1", "hello")
    assert s.get("k1") == "hello"
    assert s.get("missing") is None
    assert "k1" in s.index_text()
    assert s.index_text().startswith("STASHED")


def test_empty_stash_index_is_blank():
    assert lh.StashStore().index_text() == ""


# ── the structural guard: neither channel can reach the bank ──────────────────

def test_stash_store_has_no_bank_path():
    """StashStore must expose no method that writes a memory/atom — the excuse-killer
    only works if stash is structurally incapable of polluting the bank."""
    forbidden = {"remember", "store", "ingest", "plant", "seed", "commit", "save"}
    methods = {m for m in dir(lh.StashStore) if not m.startswith("_")}
    assert not (methods & forbidden), f"stash leaked a bank-ish method: {methods & forbidden}"


def test_spine_pin_id_is_reserved_and_stable():
    assert lh.SPINE_PIN_ID == "spine"
    assert lh.SPINE_PIN_TYPE == "custom"


# ── spine tools: peek/append keep re-prepare cheap (owner 2026-06-24) ─────────

import json as _json
from echelon_engine.atoms.tools import ToolRegistry as _TR
from echelon_engine.pins import PinRegistry as _PR


def _spine_registry():
    t = _TR(".")
    pins = _PR(async_lock=False)
    t.attach_spine(pins)
    return t, pins


def test_spine_peek_empty_then_create_then_peek():
    t, _ = _spine_registry()
    assert _json.loads(t.execute("spine_peek", {}))["spine"] is None
    t.execute("update_spine", {"content": "GOAL x\nDONE: a"})
    peek = _json.loads(t.execute("spine_peek", {}))
    assert peek["content"] == "GOAL x\nDONE: a" and peek["chars"] == len("GOAL x\nDONE: a")


def test_spine_append_adds_delta_and_keeps_one_pin():
    t, pins = _spine_registry()
    t.execute("update_spine", {"content": "GOAL x\nDONE: a"})
    t.execute("spine_append", {"line": "DONE: b"})
    peek = _json.loads(t.execute("spine_peek", {}))
    assert peek["content"] == "GOAL x\nDONE: a\nDONE: b"
    # append must not create a second spine pin — one spine per run
    assert sum(1 for p in pins.list_sync() if p.pin_id == "custom:spine") == 1


def test_spine_append_on_empty_creates_spine():
    t, _ = _spine_registry()
    t.execute("spine_append", {"line": "first line"})
    assert _json.loads(t.execute("spine_peek", {}))["content"] == "first line"


def test_spine_tools_never_reach_the_bank():
    """The spine lives in the PinRegistry (process memory) — attach_spine must not wire any
    bank writer. Guard the `pin != earned weight` law at the tool boundary."""
    t, _ = _spine_registry()
    # the only spine tools registered are the three process-memory ops — no remember/store/ingest.
    spine_tools = {s["function"]["name"] for s in t.schemas() if "spine" in s["function"]["name"]}
    assert spine_tools == {"update_spine", "spine_peek", "spine_append"}


# ── loop integration: the two-stage hop on a real run() ──────────────────────

from echelon_engine.atoms.providers.base import ProviderBase, LLMResponse, ToolCall
from echelon_engine.atoms.tools import ToolRegistry
from echelon_engine.agent.loop import run


def _resp(content, calls, ti=50000):
    return LLMResponse(content=content, tokens_in=ti, tokens_out=5, status="success", tool_calls=calls)


class _GradualFake(ProviderBase):
    """Grows the transcript gradually so the window crosses hop_at_window BEFORE the commit
    ceiling — exercising STAGE 1 (prepare) cleanly. Writes the spine when told, then finishes."""
    name = "fake"

    def __init__(self):
        self.n = 0

    def send(self, messages, model_id=None, tools=None, **kw):
        self.n += 1
        joined = " ".join(str(m.get("content", "")) for m in messages[-3:])
        if "PREPARE TO HOP" in joined:
            return _resp("preparing",
                         [ToolCall("update_spine", {"content": "STATE: mid-run, did steps"}, "c1")])
        if self.n >= 12:
            return _resp("done", [ToolCall("finish", {"answer": "complete"}, "cf")])
        return _resp("z" * 3000, [ToolCall("stash", {"key": f"k{self.n}", "text": "y" * 6000}, f"c{self.n}")])

    def look(self, *a, **k):
        raise NotImplementedError


class _BurstFake(ProviderBase):
    """Blows the window past the commit ceiling in ONE step — exercises the FORCED hop (no
    prepare turn). Survivable via the relive backstop; spine_prepared is False."""
    name = "fake"

    def __init__(self):
        self.n = 0

    def send(self, messages, model_id=None, tools=None, **kw):
        self.n += 1
        if self.n >= 5:
            return _resp("done", [ToolCall("finish", {"answer": "complete"}, "cf")])
        return _resp("x" * 200_000, [ToolCall("stash", {"key": f"k{self.n}", "text": "y"}, f"c{self.n}")])

    def look(self, *a, **k):
        raise NotImplementedError


def _run_with(fake):
    events = []
    pins = PinRegistry(async_lock=False)
    stash = lh.StashStore()
    tools = ToolRegistry(".")
    tools.attach_pins(pins)
    tools.attach_stash(stash)
    res = run("test goal", fake, tools, "fake", max_steps=30, pins=pins, stash=stash,
              hop_at_window=0.30, window_tokens=20_000,
              on_event=lambda k, d: events.append((k, d)))
    return res, events


def test_loop_two_stage_hop_prepares_spine_then_flushes():
    res, events = _run_with(_GradualFake())
    kinds = [k for k, _ in events]
    assert res.status == "completed"
    assert "hop_prepare" in kinds          # STAGE 1 fired
    hops = [d for k, d in events if k == "hop"]
    assert hops, "a hop must have committed"
    first = hops[0]
    assert first["spine_prepared"] is True  # the agent got its prepare turn and wrote the spine
    assert first["forced"] is False
    assert first["to_window"] < first["from_window"]  # the flush bought runway


def test_loop_forced_hop_when_window_bursts_past_ceiling():
    res, events = _run_with(_BurstFake())
    kinds = [k for k, _ in events]
    assert res.status == "completed"
    assert "hop" in kinds                  # committed even though it blew past the ceiling
    hops = [d for k, d in events if k == "hop"]
    assert hops[0]["to_window"] < 1.0      # the flush recovered the run from an over-full window


# ── snooze: the agent defers the hop, but never past a full window ────────────

class _SnoozeFake(ProviderBase):
    """Snoozes the FIRST prepare alert, then finishes — proving the defer works and the hop still
    commits after the snooze expires."""
    name = "fake"

    def __init__(self):
        self.n = 0
        self.snoozed = False

    def send(self, messages, model_id=None, tools=None, **kw):
        self.n += 1
        joined = " ".join(str(m.get("content", "")) for m in messages[-3:])
        if "PREPARE TO HOP" in joined and not self.snoozed:
            self.snoozed = True
            return _resp("not yet", [ToolCall("snooze_hop", {"steps": 3}, "cz")])
        if self.n >= 10:
            return _resp("done", [ToolCall("finish", {"answer": "complete"}, "cf")])
        return _resp("z" * 3000, [ToolCall("stash", {"key": f"k{self.n}", "text": "y" * 6000}, f"c{self.n}")])

    def look(self, *a, **k):
        raise NotImplementedError


class _SnoozeThenBurstFake(ProviderBase):
    """Snoozes, then BURSTS the window past the ceiling on the VERY NEXT step — proves a snooze
    cannot stop a hop when the window is genuinely full (the commit ceiling overrides the snooze)."""
    name = "fake"

    def __init__(self):
        self.n = 0
        self.snoozed = False
        self.burst = False

    def send(self, messages, model_id=None, tools=None, **kw):
        self.n += 1
        joined = " ".join(str(m.get("content", "")) for m in messages[-3:])
        if "PREPARE TO HOP" in joined and not self.snoozed:
            self.snoozed = True
            return _resp("defer", [ToolCall("snooze_hop", {"steps": 15}, "cz")])  # long snooze
        if self.snoozed and not self.burst:
            # the step right after the snooze: blow the window way past the ceiling
            self.burst = True
            return _resp("x" * 400_000, [ToolCall("stash", {"key": "burst", "text": "y"}, "cb")])
        if self.burst:
            # after the forced hop the window is small again — finish
            return _resp("done", [ToolCall("finish", {"answer": "complete"}, "cf")])
        # grow gradually toward the prepare threshold
        return _resp("z" * 3000, [ToolCall("stash", {"key": f"k{self.n}", "text": "y" * 6000}, f"c{self.n}")])

    def look(self, *a, **k):
        raise NotImplementedError


def test_loop_snooze_defers_hop_then_commits():
    res, events = _run_with(_SnoozeFake())
    kinds = [k for k, _ in events]
    acts = [d.get("tool") for k, d in events if k == "act"]
    assert res.status == "completed"
    assert "hop_prepare" in kinds
    assert "snooze_hop" in acts        # the agent actually deferred
    assert "hop_snoozed" in kinds      # the loop honored the defer


def test_loop_snooze_cannot_outrun_the_commit_ceiling():
    res, events = _run_with(_SnoozeThenBurstFake())
    kinds = [k for k, _ in events]
    acts = [d.get("tool") for k, d in events if k == "act"]
    assert "snooze_hop" in acts         # it snoozed for 15 steps
    assert "hop" in kinds               # but a full window forced the hop anyway
    forced = [d for k, d in events if k == "hop"]
    assert forced and forced[0]["from_window"] >= 0.9   # the ceiling override fired on a full window
