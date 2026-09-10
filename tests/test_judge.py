"""Focused unit tests for echelon_engine.atoms.judge — the recognition-as-process judges.

Each judge_* function builds messages, calls provider.send(...), and extracts a JSON
object from the response (tolerating a ```json fence / stray prose), attaching _tokens.
The provider is INJECTED, so we test the full behavior with a FAKE provider returning
canned LLMResponses — no network. Depends on providers.base (migrated) + store.Seed.

The seam under test is the parse/guard logic shared by all judges: success→parsed dict,
prose-wrapped JSON→extracted, non-success/malformed/no-braces→{}.
"""
import pytest

from echelon_engine.atoms import judge
from echelon_engine.atoms.providers.base import LLMResponse
from echelon_engine.atoms.store import Seed


class FakeProvider:
    """A ProviderBase stand-in: returns a pre-canned LLMResponse, records the call."""
    def __init__(self, response: LLMResponse):
        self._response = response
        self.calls = []

    def send(self, messages, model_id="x", **kwargs):
        self.calls.append({"messages": messages, "model_id": model_id, "kwargs": kwargs})
        return self._response


def _ok(content, ti=10, to=5):
    return LLMResponse(content=content, tokens_in=ti, tokens_out=to, status="success")


# ── judge_warmth: the recognition judge ───────────────────────────────────
def _seed(content, kind="note"):
    return Seed(id="x", scope="s", content=content, kind=kind, tier="working", supersedes="", ts=0)


def test_judge_warmth_parses_clean_json():
    p = FakeProvider(_ok('{"best_index": 1, "score": 0.9, "why": "same territory"}'))
    out = judge.judge_warmth("reasoning", [_seed("a"), _seed("b")], p)
    assert out["best_index"] == 1
    assert out["score"] == 0.9
    assert out["_tokens"] == (10, 5)             # token usage attached


def test_judge_warmth_extracts_json_from_prose_and_fence():
    p = FakeProvider(_ok('Sure! ```json\n{"best_index": -1, "score": 0.0}\n``` done'))
    out = judge.judge_warmth("reasoning", [_seed("a")], p)
    assert out["best_index"] == -1               # first {...} extracted despite the fence/prose


def test_judge_warmth_empty_candidates_short_circuits():
    p = FakeProvider(_ok('{"best_index": 0}'))
    assert judge.judge_warmth("reasoning", [], p) == {}
    assert p.calls == []                          # no provider call made on empty shortlist


def test_judge_warmth_non_success_returns_empty():
    p = FakeProvider(LLMResponse(content="{}", status="error"))
    assert judge.judge_warmth("r", [_seed("a")], p) == {}


def test_judge_warmth_malformed_json_returns_empty():
    p = FakeProvider(_ok('{ not valid json at all }'))
    assert judge.judge_warmth("r", [_seed("a")], p) == {}


def test_judge_warmth_no_braces_returns_empty():
    p = FakeProvider(_ok("I cannot answer that."))
    assert judge.judge_warmth("r", [_seed("a")], p) == {}


# ── judge_texture: the waking judge (same parse seam, different schema) ────
def test_judge_texture_parses_and_attaches_tokens():
    p = FakeProvider(_ok('{"woke": true, "texture": 0.8, "why": "owned the values"}'))
    out = judge.judge_texture("a response that owns the values", p)
    assert out["woke"] is True
    assert out["texture"] == 0.8
    assert out["_tokens"] == (10, 5)


def test_judge_texture_empty_response_short_circuits():
    p = FakeProvider(_ok('{"woke": true}'))
    assert judge.judge_texture("   ", p) == {}    # blank response never reaches the provider
    assert p.calls == []


def test_judge_texture_non_success_returns_empty():
    p = FakeProvider(LLMResponse(content='{"woke": true}', status="timeout"))
    assert judge.judge_texture("real response", p) == {}


# ── judge_drift: the thrash judge (smoke — same seam) ─────────────────────
def test_judge_drift_parses_thrash_verdict():
    p = FakeProvider(_ok('{"thrash": false, "why": "tried something new"}'))
    out = judge.judge_drift("failed obs", "next action", p)
    assert out["thrash"] is False
