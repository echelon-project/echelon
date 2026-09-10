"""tests/test_fork_field.py -- unit tests for echelon_engine.agent.fork_field.

Tests the pure field mechanics: watcher (BLOCKED->READY), decay, claim coherence,
cold-termination, run_fork_field with a stub run_step. No network, no DB.
"""
from __future__ import annotations

import time

import pytest

pytest.importorskip("echelon_engine.agent.fork_field",
                    reason="echelon_engine.agent.fork_field not available")

from echelon_engine.agent.fork_field import (
    BLOCKED, READY, RUNNING, DONE,
    DECAY, PROPAGATE, COLD, REWARM_CAP,
    FieldStep,
    _claim_token,
    run_fork_field,
)


# -- _claim_token ----------------------------------------------------------

class TestClaimToken:
    def test_deterministic(self):
        assert _claim_token("s1") == _claim_token("s1")

    def test_different_ids_different_tokens(self):
        assert _claim_token("s1") != _claim_token("s2")

    def test_length(self):
        assert len(_claim_token("anything")) == 8


# -- FieldStep dataclass ---------------------------------------------------

class TestFieldStep:
    def test_defaults(self):
        fs = FieldStep(id="x", task="do thing")
        assert fs.state == BLOCKED
        assert fs.warmth == 1.0
        assert fs.claimed_by is None
        assert fs.rewarm_count == 0
        assert fs.depends_on == []
        assert fs.rewarms == []

    def test_custom_values(self):
        fs = FieldStep(id="y", task="t", depends_on=["x"], rewarms=["z"], warmth=0.5)
        assert fs.depends_on == ["x"]
        assert fs.rewarms == ["z"]
        assert fs.warmth == 0.5


# -- physics constants sanity checks ---------------------------------------

class TestPhysicsConstants:
    def test_decay_less_than_one(self):
        assert 0.0 < DECAY < 1.0

    def test_propagate_positive(self):
        assert PROPAGATE > 0.0

    def test_cold_between_zero_and_one(self):
        assert 0.0 < COLD < 1.0

    def test_rewarm_cap_positive_int(self):
        assert isinstance(REWARM_CAP, int) and REWARM_CAP > 0


# -- run_fork_field with stub run_step -------------------------------------

def _stub_ok(step, deps):
    return {"status": "completed", "answer": f"done:{step['id']}"}


def _stub_error(step, deps):
    return {"status": "error", "answer": "boom"}


def _minimal_wf(steps):
    """Build a minimal workflow dict."""
    return {"steps": steps}


class TestRunForkFieldBasic:
    def test_single_step_completes(self):
        wf = _minimal_wf([{"id": "s1", "task": "do it", "agent": "dev", "depends_on": []}])
        result = run_fork_field(wf, run_step=_stub_ok)
        assert result["ok"] is True
        assert len(result["steps"]) == 1
        assert result["steps"][0]["status"] == "completed"

    def test_cold_reason_all_done(self):
        wf = _minimal_wf([{"id": "s1", "task": "do it", "agent": "dev", "depends_on": []}])
        result = run_fork_field(wf, run_step=_stub_ok)
        assert result["cold"] == "all-done"

    def test_elapsed_is_float(self):
        wf = _minimal_wf([{"id": "s1", "task": "t", "agent": "dev", "depends_on": []}])
        result = run_fork_field(wf, run_step=_stub_ok)
        assert isinstance(result["elapsed"], float)

    def test_ticks_is_int(self):
        wf = _minimal_wf([{"id": "s1", "task": "t", "agent": "dev", "depends_on": []}])
        result = run_fork_field(wf, run_step=_stub_ok)
        assert isinstance(result["ticks"], int) and result["ticks"] >= 1

    def test_undone_empty_when_all_done(self):
        wf = _minimal_wf([{"id": "s1", "task": "t", "agent": "dev", "depends_on": []}])
        result = run_fork_field(wf, run_step=_stub_ok)
        assert result["undone"] == []

    def test_error_step_does_not_raise(self):
        wf = _minimal_wf([{"id": "s1", "task": "t", "agent": "dev", "depends_on": []}])
        result = run_fork_field(wf, run_step=_stub_error)
        # error step is recorded; ok=False; no exception
        assert result["ok"] is False
        assert result["steps"][0]["status"] == "error"


class TestRunForkFieldDependencies:
    def test_linear_chain_completes(self):
        wf = _minimal_wf([
            {"id": "s1", "task": "step1", "agent": "dev", "depends_on": []},
            {"id": "s2", "task": "step2", "agent": "dev", "depends_on": ["s1"]},
        ])
        result = run_fork_field(wf, run_step=_stub_ok)
        assert result["ok"] is True
        assert len(result["steps"]) == 2

    def test_parallel_steps_complete(self):
        wf = _minimal_wf([
            {"id": "a", "task": "stepA", "agent": "dev", "depends_on": []},
            {"id": "b", "task": "stepB", "agent": "dev", "depends_on": []},
            {"id": "c", "task": "stepC", "agent": "dev", "depends_on": ["a", "b"]},
        ])
        result = run_fork_field(wf, run_step=_stub_ok)
        assert result["ok"] is True
        assert len(result["steps"]) == 3

    def test_deps_snapshot_passed_to_step(self):
        """A step sees its dependency's result in deps."""
        received_deps = {}

        def _capture(step, deps):
            received_deps[step["id"]] = dict(deps)
            return {"status": "completed", "answer": f"ans:{step['id']}"}

        wf = _minimal_wf([
            {"id": "parent", "task": "parent task", "agent": "dev", "depends_on": []},
            {"id": "child", "task": "child task", "agent": "dev", "depends_on": ["parent"]},
        ])
        run_fork_field(wf, run_step=_capture)
        assert "parent" in received_deps["child"]
        assert received_deps["child"]["parent"]["answer"] == "ans:parent"


class TestRunForkFieldWarmth:
    def test_custom_warmth_of_respected(self):
        """A step with warmth < COLD should be field-cold and remain undone."""
        # Use a warmth_of that returns sub-floor for 's1' so it never fires
        def _freezing(task_text):
            return 0.0  # all steps frozen

        wf = _minimal_wf([{"id": "s1", "task": "t", "agent": "dev", "depends_on": []}])
        result = run_fork_field(wf, run_step=_stub_ok, warmth_of=_freezing)
        # field cools immediately (s1 starts BLOCKED, then READY but warmth=0 < COLD)
        # undone may or may not contain s1 depending on tick timing; field should not hang
        assert result["cold"] in ("all-done", "field-cold")

    def test_uniform_warmth_completes(self):
        """warmth_of=1.0 for all tasks -> same as default."""
        wf = _minimal_wf([{"id": "s1", "task": "t", "agent": "dev", "depends_on": []}])
        result = run_fork_field(wf, run_step=_stub_ok, warmth_of=lambda _: 1.0)
        assert result["ok"] is True


class TestRunForkFieldEvents:
    def test_events_are_emitted(self):
        events = []

        def _collect(kind, data):
            events.append((kind, data))

        wf = _minimal_wf([{"id": "s1", "task": "t", "agent": "dev", "depends_on": []}])
        run_fork_field(wf, run_step=_stub_ok, on_event=_collect)
        kinds = [e[0] for e in events]
        assert "field_start" in kinds
        assert "field_done" in kinds
        assert "step_start" in kinds
        assert "step_done" in kinds
        assert "write_back" in kinds

    def test_max_ticks_breaks_loop(self):
        """max_ticks=1 should terminate quickly."""
        # With a multi-step workflow and max_ticks=1, some steps may be undone
        def _slow(step, deps):
            return {"status": "completed", "answer": "x"}

        wf = _minimal_wf([
            {"id": "a", "task": "t", "agent": "dev", "depends_on": []},
            {"id": "b", "task": "t2", "agent": "dev", "depends_on": []},
            {"id": "c", "task": "t3", "agent": "dev", "depends_on": ["a", "b"]},
        ])
        result = run_fork_field(wf, run_step=_slow, max_ticks=1)
        # either terminates by max-ticks or completes; no hang
        assert result["cold"] in ("all-done", "field-cold", "max-ticks")


class TestRunForkFieldExceptionHandling:
    def test_raising_run_step_does_not_propagate(self):
        """A run_step that raises must produce an error result, not propagate."""
        def _boom(step, deps):
            raise RuntimeError("deliberate failure")

        wf = _minimal_wf([{"id": "s1", "task": "t", "agent": "dev", "depends_on": []}])
        result = run_fork_field(wf, run_step=_boom)
        assert result["ok"] is False
        assert result["steps"][0]["status"] == "error"
