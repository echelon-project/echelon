"""The dispatch chain with a REAL AGENTIC run leaf — the model drives a tool-loop that
writes real files, then the verify gate grades the on-disk artifact.

This is the fullest end-to-end composition: warm(recall) → AGENTIC run(model + real
ToolRegistry hands → artifacts ON DISK) → verify(on-disk) → earn(credit). The provider
is a FAKE that emits a write_file tool call then finishes — so the loop, the real tool
execution against a real sandbox, and the on-disk verify are all EXERCISED FOR REAL
(only the model's token generation is stubbed; the writes and the gate are genuine).
"""
import pytest

from echelon_engine.atoms.store import SeedStore
from echelon_engine.atoms.cards import CardStore
from echelon_engine.atoms.tools import ToolRegistry
from echelon_engine.atoms.providers.base import LLMResponse, ToolCall
from echelon_engine.services import dispatch_agentic
from echelon_engine.atoms.uame import SCORE_BENCHMARK


@pytest.fixture
def stores(tmp_path):
    store = SeedStore(tmp_path / "core.db", v2_db=tmp_path / "core_v2.db")
    cards = CardStore(tmp_path / "core_v2.db")
    return store, cards


class ScriptedProvider:
    """A provider that plays a fixed script of LLMResponses, one per send() turn."""
    def __init__(self, turns):
        self._turns = list(turns)
        self.sends = 0

    def send(self, messages, model_id, tools=None, **kwargs):
        self.sends += 1
        return self._turns.pop(0)


def test_agentic_run_writes_a_real_file_then_verify_passes(stores, tmp_path):
    store, cards = stores
    sandbox = tmp_path / "work"
    sandbox.mkdir()
    registry = ToolRegistry(root=sandbox, allow_write=True, allow_bash=False)
    artifact = sandbox / "result.txt"

    # turn 1: the model calls write_file; turn 2: it returns plain text (done)
    provider = ScriptedProvider([
        LLMResponse(content="", status="success",
                    tool_calls=[ToolCall(name="write_file",
                                         args={"path": "result.txt", "content": "the work landed"},
                                         id="c1")]),
        LLMResponse(content="Done — wrote result.txt", status="success"),
    ])

    result = dispatch_agentic("create result.txt with the work", store, cards,
                              registry=registry, provider=provider, model="m",
                              artifact_path=str(artifact))
    assert result.ok
    ctx = result.value
    # the AGENTIC loop actually executed the write against the real sandbox
    assert artifact.is_file()
    assert artifact.read_text(encoding="utf-8") == "the work landed"
    assert ctx["run"]["status"] == "completed"
    assert ctx["run"]["tool_calls"] == 1
    # verify graded the REAL on-disk artifact, earn credited the green trace
    assert ctx["outcome"]["ok"] is True
    assert ctx["earned"]["credited"] is True
    assert ctx["earned"]["card_score"] > SCORE_BENCHMARK


def test_agentic_run_that_writes_nothing_fails_verify(stores, tmp_path):
    store, cards = stores
    sandbox = tmp_path / "work"
    sandbox.mkdir()
    registry = ToolRegistry(root=sandbox, allow_write=True, allow_bash=False)
    missing = sandbox / "result.txt"

    # the model just talks, never calls a tool -> nothing on disk
    provider = ScriptedProvider([
        LLMResponse(content="I think the answer is 42.", status="success"),
    ])
    result = dispatch_agentic("create result.txt", store, cards,
                              registry=registry, provider=provider, model="m",
                              artifact_path=str(missing))
    ctx = result.value
    assert ctx["run"]["status"] == "completed"     # the model 'finished'...
    assert ctx["run"]["tool_calls"] == 0
    assert not missing.exists()                     # ...but wrote NOTHING
    assert ctx["outcome"]["ok"] is False            # the on-disk gate catches the claim-done-wrote-nothing
    assert ctx["earned"]["card_score"] < SCORE_BENCHMARK   # red trace marks the stall


def test_agentic_run_reports_provider_error(stores, tmp_path):
    store, cards = stores
    sandbox = tmp_path / "work"; sandbox.mkdir()
    registry = ToolRegistry(root=sandbox, allow_write=True, allow_bash=False)
    provider = ScriptedProvider([LLMResponse(content="", status="error")])
    result = dispatch_agentic("do X", store, cards, registry=registry, provider=provider, model="m")
    ctx = result.value
    assert ctx["run"]["status"] == "error"          # transport failure reported, chain didn't crash
