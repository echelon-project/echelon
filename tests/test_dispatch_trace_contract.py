import json

import pytest

from echelon_engine.agent import cli, partner


def test_dispatch_exception_is_recorded_and_prior_trace_is_preserved(tmp_path, monkeypatch):
    path = tmp_path / 'spec.json'
    trace = tmp_path / 'attempt.jsonl'
    path.write_text(json.dumps({'goal': 'inspect', 'scope': 'fixture', 'folder': str(tmp_path),
                                'model': 'named-model', 'out': str(trace)}))
    calls = []
    def fail(*args, **kwargs):
        calls.append(True)
        raise RuntimeError('password=private-value')
    monkeypatch.setattr(partner, 'dispatch', fail)
    with pytest.raises(RuntimeError):
        cli._run_dispatch_spec([str(path)])
    raw = trace.read_bytes()
    result = json.loads(raw.splitlines()[-1])
    assert result['status'] == 'unknown' and result['retry_safe'] is False
    assert b'private-value' not in raw
    with pytest.raises(FileExistsError):
        cli._run_dispatch_spec([str(path)])
    assert trace.read_bytes() == raw
    assert len(calls) == 1


def test_dispatch_preserves_existing_session_and_equipment_options(tmp_path, monkeypatch):
    spec = {'goal': 'inspect', 'scope': 'fixture', 'folder': str(tmp_path),
            'model': 'named-model', 'out': str(tmp_path / 'trace.jsonl'),
            'session_path': str(tmp_path / 'session.json'), 'role': 'reviewer',
            'cartridges': ['craft'], 'craft': False, 'tier': 'T1'}
    path = tmp_path / 'spec.json'
    path.write_text(json.dumps(spec))
    captured = {}
    def dispatch(*args, **kwargs):
        captured.update(kwargs)
        return {'status': 'completed', 'outcome': {'ok': True}}
    monkeypatch.setattr(partner, 'dispatch', dispatch)
    assert cli._run_dispatch_spec([str(path)]) == 0
    for key in ('session_path', 'role', 'cartridges', 'craft', 'tier'):
        assert captured[key] == spec[key]


def test_event_is_fsynced_before_dispatch_can_continue(tmp_path, monkeypatch):
    import os
    path = tmp_path / 'spec.json'
    trace = tmp_path / 'trace.jsonl'
    path.write_text(json.dumps({'goal': 'inspect', 'scope': 'fixture',
                                'folder': str(tmp_path), 'out': str(trace)}))
    real_fsync = os.fsync
    persisted = []
    def witness(fd):
        real_fsync(fd)
        persisted.append(trace.read_text(encoding='utf-8'))
    monkeypatch.setattr(os, 'fsync', witness)
    def dispatch(*args, **kwargs):
        kwargs['on_event']('grounding_failed', {'input_index': 0, 'worker_started': False})
        assert persisted and json.loads(persisted[-1].splitlines()[-1])['kind'] == 'grounding_failed'
        return {'status': 'refused', 'outcome': {'ok': False}}
    monkeypatch.setattr(partner, 'dispatch', dispatch)
    assert cli._run_dispatch_spec([str(path)]) == 1


def test_verify_import_failure_has_attempt_evidence_before_side_effect(tmp_path, monkeypatch):
    trace, spec, verifier = tmp_path / "trace.jsonl", tmp_path / "spec.json", tmp_path / "verify.py"
    verifier.write_text("raise RuntimeError('private-value')", encoding="utf-8")
    spec.write_text(json.dumps({"goal": "inspect", "scope": "fixture", "folder": str(tmp_path),
                                "out": str(trace), "verify_script": str(verifier)}))
    monkeypatch.setenv("ECHELON_RUN_ID", "run-fixture")
    monkeypatch.setenv("ECHELON_WORKER_ID", "worker-fixture")
    monkeypatch.setattr(partner, "dispatch", lambda *a, **k: pytest.fail("must not dispatch"))
    with pytest.raises(RuntimeError):
        cli._run_dispatch_spec([str(spec)])
    rows = [json.loads(line) for line in trace.read_text().splitlines()]
    assert [r["kind"] for r in rows] == ["dispatch_start", "result"]
    assert rows[0]["dispatch_attempt_id"] == rows[1]["dispatch_attempt_id"]
    assert rows[0]["command_center"]["run_id"] == "run-fixture"
    assert rows[0]["command_center"]["worker_id"] == "worker-fixture"
    assert rows[1]["status"] == "unknown" and rows[1]["retry_safe"] is False
    assert "private-value" not in trace.read_text()


def test_start_flush_failure_prevents_verifier_and_worker(tmp_path, monkeypatch):
    import os
    trace, spec = tmp_path / "trace.jsonl", tmp_path / "spec.json"
    spec.write_text(json.dumps({"goal": "inspect", "scope": "fixture", "folder": str(tmp_path), "out": str(trace)}))
    real = os.fsync
    calls = []
    def fail_once(fd):
        calls.append(fd)
        if len(calls) == 1:
            raise OSError("fixture")
        return real(fd)
    monkeypatch.setattr(os, "fsync", fail_once)
    monkeypatch.setattr(partner, "dispatch", lambda *a, **k: pytest.fail("worker must not start"))
    with pytest.raises(cli.DispatchTraceUnavailable) as raised:
        cli._run_dispatch_spec([str(spec)])
    error = raised.value
    assert error.phase == "start_record" and error.effects_possible is False
    assert error.retry_safe is False and len(calls) == 1
    assert len(trace.read_text().splitlines()) == 1  # no retry append to uncertain trace


def test_event_payload_cannot_replace_attempt_identity(tmp_path, monkeypatch):
    trace, spec = tmp_path / "trace.jsonl", tmp_path / "spec.json"
    spec.write_text(json.dumps({"goal": "inspect", "scope": "fixture", "folder": str(tmp_path), "out": str(trace)}))
    monkeypatch.setenv("ECHELON_RUN_ID", "run-fixture")
    def dispatch(*a, **kw):
        kw["on_event"]("step", {"dispatch_attempt_id": "forged", "command_center": {}, "kind": "forged", "sequence": -1, "recorded_at": "forged"})
        return {"status": "completed", "outcome": {"ok": True}}
    monkeypatch.setattr(partner, "dispatch", dispatch)
    assert cli._run_dispatch_spec([str(spec)]) == 0
    rows = [json.loads(line) for line in trace.read_text().splitlines()]
    from datetime import datetime
    assert [r["sequence"] for r in rows] == [1, 2, 3]
    assert all(datetime.fromisoformat(r["recorded_at"]).utcoffset().total_seconds() == 0 for r in rows)
    assert len({r["dispatch_attempt_id"] for r in rows}) == 1
    assert rows[1]["kind"] == "step" and rows[1]["dispatch_attempt_id"] != "forged"
    assert all(r["command_center"]["run_id"] == "run-fixture" for r in rows)


def test_persistent_trace_failure_after_dispatch_preserves_unknown_effects(tmp_path, monkeypatch):
    import os
    trace, spec = tmp_path / "trace.jsonl", tmp_path / "spec.json"
    spec.write_text(json.dumps({"goal": "inspect", "scope": "fixture", "folder": str(tmp_path), "out": str(trace)}))
    real = os.fsync
    calls = []
    def fail_after_start(fd):
        calls.append(fd)
        if len(calls) > 1:
            raise OSError("password=private-value")
        return real(fd)
    monkeypatch.setattr(os, "fsync", fail_after_start)
    def dispatch(*a, **kw):
        kw["on_event"]("step", {"status": "working"})
        pytest.fail("must stop on evidence failure")
    monkeypatch.setattr(partner, "dispatch", dispatch)
    with pytest.raises(cli.DispatchTraceUnavailable) as raised:
        cli._run_dispatch_spec([str(spec)])
    error = raised.value
    assert error.phase == "dispatch" and error.effects_possible is True
    assert error.retry_safe is False and error.trace_path == str(trace)
    assert len(calls) == 2
    assert "private-value" not in str(error)
    assert [json.loads(line)["kind"] for line in trace.read_text().splitlines()] == ["dispatch_start", "step"]
