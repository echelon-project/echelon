"""tests/test_workflow_agent.py -- unit tests for echelon_engine.agent.workflow.

Tests: compute_waves, validate_workflow, run_workflow with stub, _parse_plan.
make_agent_runner is network/loop-bound (tested at smoke level only).
"""
from __future__ import annotations

import json

import pytest

pytest.importorskip("echelon_engine.agent.workflow",
                    reason="echelon_engine.agent.workflow not available")

from echelon_engine.agent.workflow import (
    compute_waves,
    validate_workflow,
    run_workflow,
    _parse_plan,
    available_agents,
)


# -- compute_waves --------------------------------------------------------

class TestComputeWaves:
    def test_single_step_one_wave(self):
        wf = {"steps": [{"id": "s1", "depends_on": []}]}
        assert compute_waves(wf) == [["s1"]]

    def test_two_independent_steps_one_wave(self):
        wf = {"steps": [
            {"id": "a", "depends_on": []},
            {"id": "b", "depends_on": []},
        ]}
        waves = compute_waves(wf)
        assert len(waves) == 1
        assert set(waves[0]) == {"a", "b"}

    def test_linear_chain_three_waves(self):
        wf = {"steps": [
            {"id": "a", "depends_on": []},
            {"id": "b", "depends_on": ["a"]},
            {"id": "c", "depends_on": ["b"]},
        ]}
        waves = compute_waves(wf)
        assert len(waves) == 3
        assert waves[0] == ["a"]
        assert waves[1] == ["b"]
        assert waves[2] == ["c"]

    def test_diamond_two_waves(self):
        wf = {"steps": [
            {"id": "a", "depends_on": []},
            {"id": "b", "depends_on": ["a"]},
            {"id": "c", "depends_on": ["a"]},
            {"id": "d", "depends_on": ["b", "c"]},
        ]}
        waves = compute_waves(wf)
        assert waves[0] == ["a"]
        assert set(waves[1]) == {"b", "c"}
        assert waves[2] == ["d"]

    def test_cycle_raises_value_error(self):
        wf = {"steps": [
            {"id": "a", "depends_on": ["b"]},
            {"id": "b", "depends_on": ["a"]},
        ]}
        with pytest.raises(ValueError, match="cycle"):
            compute_waves(wf)

    def test_stable_order_within_wave(self):
        """steps within a wave are sorted alphabetically."""
        wf = {"steps": [
            {"id": "z", "depends_on": []},
            {"id": "a", "depends_on": []},
            {"id": "m", "depends_on": []},
        ]}
        waves = compute_waves(wf)
        assert waves[0] == ["a", "m", "z"]


# -- validate_workflow ----------------------------------------------------

class TestValidateWorkflow:
    def _known_agent(self):
        """Return the first known agent id from roles, or 'dev' as fallback."""
        try:
            agents = available_agents()
            if agents:
                return agents[0]["agent"]
        except Exception:
            pass
        return "dev"

    def test_no_steps_is_error(self):
        problems = validate_workflow({"steps": []})
        assert problems

    def test_missing_steps_key_is_error(self):
        problems = validate_workflow({})
        assert problems

    def test_duplicate_ids_detected(self):
        ag = self._known_agent()
        wf = {"steps": [
            {"id": "s1", "agent": ag, "task": "t1", "depends_on": []},
            {"id": "s1", "agent": ag, "task": "t2", "depends_on": []},
        ]}
        problems = validate_workflow(wf)
        assert any("duplicate" in p.lower() for p in problems)

    def test_unknown_agent_flagged(self):
        wf = {"steps": [{"id": "s1", "agent": "BOGUS_AGENT_NEVER_EXISTS",
                         "task": "t", "depends_on": []}]}
        problems = validate_workflow(wf)
        assert any("unknown agent" in p.lower() for p in problems)

    def test_missing_task_flagged(self):
        ag = self._known_agent()
        wf = {"steps": [{"id": "s1", "agent": ag, "depends_on": []}]}
        problems = validate_workflow(wf)
        assert any("task" in p.lower() for p in problems)

    def test_bad_dep_ref_flagged(self):
        ag = self._known_agent()
        wf = {"steps": [
            {"id": "s1", "agent": ag, "task": "t", "depends_on": ["NONEXISTENT"]},
        ]}
        problems = validate_workflow(wf)
        assert any("depends_on" in p.lower() or "nonexistent" in p.lower() for p in problems)

    def test_cycle_detected(self):
        ag = self._known_agent()
        wf = {"steps": [
            {"id": "a", "agent": ag, "task": "t", "depends_on": ["b"]},
            {"id": "b", "agent": ag, "task": "t", "depends_on": ["a"]},
        ]}
        problems = validate_workflow(wf)
        assert problems  # cycle or unknown-dep should be flagged


# -- run_workflow with stub -----------------------------------------------

class TestRunWorkflow:
    def _wf(self, steps):
        return {"steps": steps}

    def _known_agent(self):
        try:
            return available_agents()[0]["agent"]
        except Exception:
            return "dev"

    def test_single_step_runs(self):
        ag = self._known_agent()
        wf = self._wf([{"id": "s1", "agent": ag, "task": "t", "depends_on": []}])

        def _stub(step, deps):
            return {"status": "completed", "answer": "ok"}

        result = run_workflow(wf, run_step=_stub)
        assert result["ok"] is True
        assert len(result["steps"]) == 1

    def test_result_has_required_keys(self):
        ag = self._known_agent()
        wf = self._wf([{"id": "s1", "agent": ag, "task": "t", "depends_on": []}])

        result = run_workflow(wf, run_step=lambda s, d: {"status": "completed"})
        for key in ("steps", "waves", "started", "finished", "elapsed", "ok"):
            assert key in result

    def test_error_step_does_not_crash(self):
        ag = self._known_agent()
        wf = self._wf([{"id": "s1", "agent": ag, "task": "t", "depends_on": []}])

        def _boom(step, deps):
            raise RuntimeError("step failure")

        result = run_workflow(wf, run_step=_boom)
        assert result["ok"] is False

    def test_parallel_steps_all_complete(self):
        ag = self._known_agent()
        wf = self._wf([
            {"id": "a", "agent": ag, "task": "ta", "depends_on": []},
            {"id": "b", "agent": ag, "task": "tb", "depends_on": []},
        ])
        result = run_workflow(wf, run_step=lambda s, d: {"status": "completed"})
        assert result["ok"] is True
        assert len(result["steps"]) == 2

    def test_on_event_receives_events(self):
        ag = self._known_agent()
        wf = self._wf([{"id": "s1", "agent": ag, "task": "t", "depends_on": []}])
        events = []
        run_workflow(wf, run_step=lambda s, d: {"status": "completed"},
                     on_event=lambda k, d: events.append(k))
        assert "workflow_start" in events
        assert "workflow_done" in events

    def test_deps_passed_to_downstream(self):
        ag = self._known_agent()
        wf = self._wf([
            {"id": "s1", "agent": ag, "task": "t1", "depends_on": []},
            {"id": "s2", "agent": ag, "task": "t2", "depends_on": ["s1"]},
        ])
        seen_deps = {}

        def _capture(step, deps):
            seen_deps[step["id"]] = list(deps.keys())
            return {"status": "completed", "answer": step["id"]}

        run_workflow(wf, run_step=_capture)
        assert "s1" in seen_deps.get("s2", [])

    def test_result_steps_include_id_agent_wave(self):
        ag = self._known_agent()
        wf = self._wf([{"id": "s1", "agent": ag, "task": "t", "depends_on": []}])
        result = run_workflow(wf, run_step=lambda s, d: {"status": "completed"})
        step_out = result["steps"][0]
        assert step_out["id"] == "s1"
        assert "agent" in step_out
        assert "wave" in step_out


# -- _parse_plan ----------------------------------------------------------

class TestParsePlan:
    def test_valid_json(self):
        raw = json.dumps({"steps": [{"id": "s1", "agent": "dev", "task": "t", "depends_on": []}]})
        result = _parse_plan(raw)
        assert "steps" in result
        assert result["steps"][0]["id"] == "s1"

    def test_fenced_json_block(self):
        raw = "```json\n{\"steps\": []}\n```"
        result = _parse_plan(raw)
        assert "steps" in result

    def test_fenced_without_language_tag(self):
        raw = "```\n{\"steps\": []}\n```"
        result = _parse_plan(raw)
        assert "steps" in result

    def test_json_embedded_in_prose(self):
        raw = "Here is the plan:\n{\"steps\": [{\"id\": \"x\"}]}\nDone."
        result = _parse_plan(raw)
        assert "steps" in result

    def test_invalid_json_returns_error_dict(self):
        result = _parse_plan("not json at all")
        assert "steps" in result  # returns {"steps": [], "_parse_error": ...}
        assert result["steps"] == []
        assert "_parse_error" in result

    def test_empty_string_returns_error_dict(self):
        result = _parse_plan("")
        assert "steps" in result


# -- import guard tests (post-port verification) -----------------------------

class TestImportGuards:
    """Verify that lazy import guards resolve to real ported engine modules,
    not stale fallback paths. The old duplicate try-blocks and echelon_agent
    fallbacks have been removed — these tests confirm the real imports work."""

    def test_loop_import_resolves(self):
        """The loop importer returns the real echelon_engine.agent.loop.run."""
        from echelon_engine.agent.loop import run
        assert callable(run), "loop.run must be a callable"
        assert run.__module__ == "echelon_engine.agent.loop"

    def test_memory_context_import_resolves(self):
        """MemoryContext is importable from echelon_engine.agent.loop."""
        from echelon_engine.agent.loop import MemoryContext
        assert MemoryContext is not None

    def test_tool_registry_import_resolves(self):
        """ToolRegistry is importable from echelon_engine.atoms.tools."""
        from echelon_engine.atoms.tools import ToolRegistry
        assert ToolRegistry is not None

    def test_make_agent_runner_resolves(self):
        """make_agent_runner resolves from echelon_engine.agent.workflow without ImportError."""
        from echelon_engine.agent.workflow import make_agent_runner
        assert callable(make_agent_runner), "make_agent_runner must be a callable"


@pytest.mark.parametrize("kind", ["missing", "directory", "invalid_utf8"])
def test_required_grounding_failure_stops_before_worker(tmp_path, kind):
    from echelon_engine.agent.workflow import make_agent_runner
    path = tmp_path / 'required.txt'
    if kind == 'directory':
        path.mkdir()
    elif kind == 'invalid_utf8':
        path.write_bytes(bytes([255]))
    events = []
    with pytest.raises(RuntimeError, match='required grounding input 0 unavailable'):
        make_agent_runner(object(), 'unused', str(tmp_path), files=[str(path)],
                          on_event=lambda k, d: events.append((k, d)))
    assert len(events) == 1
    assert events[0][0] == 'grounding_failed'
    assert events[0][1]['worker_started'] is False
    assert 'content' not in events[0][1]


def test_grounding_receipt_identifies_exact_bytes_without_content(tmp_path):
    import hashlib
    from echelon_engine.agent.workflow import make_agent_runner
    path = tmp_path / 'required.txt'
    content = b'private grounding instructions'
    path.write_bytes(content)
    events = []
    runner = make_agent_runner(object(), 'unused', str(tmp_path), files=[str(path)],
                              on_event=lambda k, d: events.append((k, d)))
    assert callable(runner)
    assert events == [('grounding_loaded', {'input_index': 0,
                       'sha256': hashlib.sha256(content).hexdigest(), 'bytes': len(content)})]


def test_memory_boot_failure_is_explicit_before_worker(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from echelon_engine.agent import workflow, loop
    from echelon_engine.atoms import store, boot
    monkeypatch.setattr(workflow._roles, 'role_device_dir', lambda role: tmp_path)
    monkeypatch.setattr(workflow._roles, 'role_device_scope', lambda role: 'fixture')
    monkeypatch.setattr(store, 'SeedStore', lambda *args: object())
    monkeypatch.setattr(loop, 'MemoryContext', lambda *args, **kwargs: object())
    def fail(*args):
        raise OSError('private boot details')
    monkeypatch.setattr(boot, 'boot', fail)
    events = []
    def worker(*args, **kwargs):
        assert events and events[0][0] == 'memory_boot_degraded'
        assert kwargs['boot'] is None
        return SimpleNamespace(status='completed', answer='fixture', steps=1)
    monkeypatch.setattr(loop, 'run', worker)
    runner = workflow.make_agent_runner(object(), 'fixture-model', str(tmp_path),
                                        on_event=lambda k, d: events.append((k, d)))
    result = runner({'agent': 'dev', 'task': 'inspect'}, {})
    assert result['memory_boot']['status'] == 'degraded'
    assert events[0][1]['error_type'] == 'OSError'
    assert events[0][1]['fallback'] == 'without_memory_boot'
    assert 'private boot details' not in str(events)


@pytest.mark.parametrize('persistent', [False, True])
def test_delegated_worker_receives_actual_shell_environment(tmp_path, monkeypatch, persistent):
    from types import SimpleNamespace
    from echelon_engine.agent import workflow, loop
    from echelon_sdk import environment
    monkeypatch.setattr(workflow._roles, 'role_device_dir', lambda role: None)
    monkeypatch.setattr(workflow._roles, 'build_role', lambda *args: None)
    sensed = environment.Environment('nt', 'Windows', 'fixture', str(tmp_path),
                                     str(tmp_path), 'wrong-shell', 'wrong-path', 'fixture-python')
    monkeypatch.setattr(environment, 'probe', lambda root: sensed)
    captured = {}
    result = SimpleNamespace(status='completed', answer='fixture', steps=1)
    def worker(*args, **kwargs):
        captured.update(kwargs)
        return result
    monkeypatch.setattr(loop, 'run', worker)
    if persistent:
        monkeypatch.setattr(workflow, '_load_session_for_dispatch', lambda *a: (None, {}))
        monkeypatch.setattr(workflow, '_check_session_execution', lambda *a: captured.setdefault('manifest', a[1]))
        monkeypatch.setattr(workflow, '_persist_dispatch_session', lambda *a: {})
        monkeypatch.setattr(loop, 'run_task', lambda *a, **k: (worker(*a, **k), None))
    runner = workflow.make_agent_runner(object(), 'fixture-model', str(tmp_path),
                                        session_path=str(tmp_path / 'session.json') if persistent else None)
    assert runner({'agent': 'dev', 'task': 'inspect'}, {})['status'] == 'completed'
    block = captured['env_block']
    assert 'wrong-shell' not in block and 'wrong-path' not in block
    assert 'Shell run_bash uses:' in block and str(tmp_path) in block
    assert sensed.shell_label in {'cmd', 'posix'}
