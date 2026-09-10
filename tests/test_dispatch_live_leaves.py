"""The dispatch chain END-TO-END on ALL real leaves — warm + run + verify + earn.

Builds on test_dispatch_real_leaves (warm+earn real, run+verify dry). Here run is a REAL
floor_chat model call and verify is a REAL on-disk artifact check. To stay network-free
AND deterministic, the floor_chat._chat primitive is monkeypatched with a fake completion
(the LEAF is real — it's the same code path production uses; only the transport is stubbed,
exactly how a unit test isolates I/O). The verify gate is graded on a REAL file on disk —
that part is NOT faked (it's the whole point: outcome-by-mechanism, not transport success).
"""
import pytest

from echelon_engine.atoms.store import SeedStore
from echelon_engine.atoms.cards import CardStore
from echelon_engine.atoms.providers import floor_chat
from echelon_engine.services_dispatch_atoms import make_run_fn, make_verify_fn
from echelon_engine.services import dispatch_live
from echelon_engine.atoms.uame import SCORE_BENCHMARK


@pytest.fixture
def stores(tmp_path):
    store = SeedStore(tmp_path / "core.db", v2_db=tmp_path / "core_v2.db")
    cards = CardStore(tmp_path / "core_v2.db")
    return store, cards


@pytest.fixture
def fake_chat(monkeypatch):
    """Stub the floor_chat transport — the run LEAF stays real, only the network is faked."""
    calls = []
    def _fake(model, system, user, **kw):
        calls.append({"model": model, "system": system, "user": user})
        return "I did the thing. Result written."
    monkeypatch.setattr(floor_chat, "_chat", _fake)
    return calls


# ── the run leaf actually calls the model primitive ───────────────────────
def test_run_fn_calls_the_model_and_reports_completed(fake_chat):
    run_fn = make_run_fn(model="test-model")
    out = run_fn({"goal": "do X", "guidance": "RULES: be terse"})
    assert out["status"] == "completed"
    assert "did the thing" in out["answer"]
    assert fake_chat[0]["model"] == "test-model"
    assert fake_chat[0]["user"] == "do X"          # the goal is the user turn


def test_run_fn_reports_error_not_raise_on_transport_failure(monkeypatch):
    def _boom(*a, **k):
        raise ConnectionError("floor down")
    monkeypatch.setattr(floor_chat, "_chat", _boom)
    out = make_run_fn()({"goal": "do X", "guidance": ""})
    assert out["status"] == "error"                # reported, not raised (the weather, not a wrote-nothing)
    assert out["steps"] == 0


# ── the verify leaf grades a REAL on-disk artifact (not faked) ────────────
def test_verify_fn_passes_only_when_artifact_exists_nonempty(tmp_path):
    artifact = tmp_path / "out.txt"
    vf = make_verify_fn(str(artifact))
    ok, _ = vf({"run": {"status": "completed", "answer": "x"}})
    assert ok is False                              # file not written yet -> verify FAILS
    artifact.write_text("real output", encoding="utf-8")
    ok, detail = vf({"run": {"status": "completed", "answer": "x"}})
    assert ok is True and "exists_nonempty=True" in detail


def test_verify_fn_transport_check_when_no_artifact():
    vf = make_verify_fn(None)
    ok, detail = vf({"run": {"status": "completed", "answer": "something"}})
    assert ok is True
    bad, _ = vf({"run": {"status": "error", "answer": ""}})
    assert bad is False
    assert "transport-check" in detail              # honestly flagged as the weaker check


# ── the WHOLE chain end-to-end on all real leaves ─────────────────────────
def test_dispatch_live_end_to_end_green(stores, fake_chat, tmp_path):
    store, cards = stores
    store.remember("echelon", "the chain runs end to end on real atoms", coordinate="tooling:e2e")
    artifact = tmp_path / "delivered.txt"
    artifact.write_text("the work landed on disk", encoding="utf-8")   # the run "produced" this
    result = dispatch_live("fix the e2e bug", store, cards, model="m",
                           artifact_path=str(artifact))
    assert result.ok
    ctx = result.value
    # real warm verdict, real model answer, real on-disk verify, real earned credit
    assert ctx["warm"]["verdict"] in ("warm", "lukewarm", "cold")
    assert "did the thing" in ctx["run"]["answer"]
    assert ctx["outcome"]["ok"] is True                       # artifact exists+nonempty
    assert ctx["earned"]["credited"] is True
    assert ctx["earned"]["card_score"] > SCORE_BENCHMARK      # green outcome earned weight


def test_dispatch_live_red_when_artifact_missing(stores, fake_chat, tmp_path):
    store, cards = stores
    missing = tmp_path / "never_written.txt"
    result = dispatch_live("fix something", store, cards, model="m",
                           artifact_path=str(missing))
    # run completes (the model answered) but verify FAILS (no artifact) -> earn marks the stall
    ctx = result.value
    assert ctx["run"]["status"] == "completed"
    assert ctx["outcome"]["ok"] is False                      # the gate caught the wrote-nothing
    assert ctx["earned"]["card_score"] < SCORE_BENCHMARK      # red outcome, below neutral
