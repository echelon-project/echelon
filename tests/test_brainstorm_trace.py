import json
import subprocess
import sys
from types import SimpleNamespace

import pytest

from echelon_engine.atoms import brainstorm as council


def rows(path):
    return [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines()]


def seat(name):
    return council.Seat(name, 'audit', 'fixture', 'fixture-model')


def test_completed_seat_is_durable_before_next_dispatch(tmp_path, monkeypatch):
    path = tmp_path / 'trace.jsonl'
    calls = []

    def ask(s, *args, **kwargs):
        calls.append(s.name)
        if s.name == 'SECOND':
            assert any(r['kind'] == 'seat_response' and r['verdict'] == 'first evidence'
                       for r in rows(path))
            raise RuntimeError('private provider detail')
        return 'fixture-model', 'first evidence'

    monkeypatch.setattr(council, '_ask', ask)
    with pytest.raises(RuntimeError):
        council.brainstorm('fixture', seats=[seat('FIRST'), seat('SECOND')],
                           chair=seat('CHAIR'), trace_path=path)
    assert calls == ['FIRST', 'SECOND']
    events = rows(path)
    assert events[-1]['kind'] == 'council_interrupted'
    assert events[-1]['retry_safe'] is False
    assert 'private provider detail' not in path.read_text(encoding='utf-8')
    assert not any(r['kind'] == 'council_finished' for r in events)


def test_trace_reuse_refuses_before_provider_call(tmp_path, monkeypatch):
    path = tmp_path / 'trace.jsonl'
    path.write_text('prior evidence\n', encoding='utf-8')
    monkeypatch.setattr(council, '_ask', lambda *a, **k: pytest.fail('provider called'))
    with pytest.raises(FileExistsError):
        council.brainstorm('fixture', seats=[seat('FIRST')], chair=seat('CHAIR'), trace_path=path)
    assert path.read_text(encoding='utf-8') == 'prior evidence\n'


def test_failed_trace_flush_stops_dispatch(tmp_path, monkeypatch):
    def fail(*args):
        raise OSError('storage failure')

    monkeypatch.setattr(council.os, 'fsync', fail)
    monkeypatch.setattr(council, '_ask', lambda *a, **k: pytest.fail('provider called'))
    with pytest.raises(OSError):
        council.brainstorm('fixture', seats=[seat('FIRST')], chair=seat('CHAIR'),
                           trace_path=tmp_path / 'trace.jsonl')


def test_response_completion_is_not_acceptance(tmp_path, monkeypatch):
    path = tmp_path / 'trace.jsonl'
    monkeypatch.setattr(council, '_ask', lambda *a, **k: ('fixture-model', 'BLOCK: missing evidence'))
    result = council.brainstorm('fixture', seats=[seat('FIRST')], chair=seat('CHAIR'), trace_path=path)
    assert result.chair == 'BLOCK: missing evidence'
    assert result.trace_path == str(path.resolve())
    events = rows(path)
    assert events[-1]['acceptance_status'] == 'not_assessed'
    assert len([r for r in events if r['kind'] == 'seat_response']) == 2


def test_process_exit_retains_completed_seat_and_unknown_active_seat(tmp_path):
    path = tmp_path / 'crash.jsonl'
    script = '''
import os, sys
from echelon_engine.atoms import brainstorm as c
def ask(seat, *args, **kwargs):
    if seat.name == 'SECOND':
        os._exit(91)
    return 'fixture-model', 'durable first response'
c._ask = ask
c.brainstorm('fixture', seats=[c.Seat('FIRST','audit','fixture','fixture-model'),
                              c.Seat('SECOND','audit','fixture','fixture-model')],
             chair=c.Seat('CHAIR','audit','fixture','fixture-model'), trace_path=sys.argv[1])
'''
    result = subprocess.run([sys.executable, '-X', 'utf8', '-c', script, str(path)],
                            capture_output=True, timeout=30)
    assert result.returncode == 91, result.stderr.decode('utf-8', errors='replace')
    events = rows(path)
    assert any(r['kind'] == 'seat_response' and r['verdict'] == 'durable first response'
               for r in events)
    assert events[-1]['kind'] == 'seat_started'
    assert events[-1]['name'] == 'SECOND'
    assert not any(r['kind'] == 'council_finished' for r in events)


def test_provider_fallback_is_recorded_and_errors_are_sanitized(tmp_path, monkeypatch):
    from echelon_engine.atoms import routing
    monkeypatch.setattr(routing, 'model_chain', lambda floor: ['first-model', 'second-model'])
    calls = []

    def provider(model):
        calls.append(model)
        if model == 'first-model':
            raise OSError('secret provider credential')
        return SimpleNamespace(send=lambda *a, **k: SimpleNamespace(status='success', content='response'))

    monkeypatch.setattr(council, 'provider_for', provider)
    path = tmp_path / 'fallback.jsonl'
    council.brainstorm('fixture', seats=[council.Seat('FIRST', 'audit', 'fixture')],
                       chair=seat('CHAIR'), trace_path=path)
    events = [r for r in rows(path) if r['kind'].startswith('provider_') and r['seat_index'] == 0]
    assert [(r['kind'], r['requested_model']) for r in events] == [
        ('provider_started', 'first-model'), ('provider_finished', 'first-model'),
        ('provider_started', 'second-model'), ('provider_finished', 'second-model')]
    assert events[1]['error_type'] == 'OSError'
    assert 'secret provider credential' not in path.read_text(encoding='utf-8')


def test_attempt_recorder_failure_cannot_trigger_provider_fallback(monkeypatch):
    from echelon_engine.atoms import routing
    monkeypatch.setattr(routing, 'model_chain', lambda floor: ['first-model', 'second-model'])
    calls = []

    def provider(model):
        calls.append(model)
        raise OSError('provider unavailable')

    def recorder(kind, **fields):
        if kind == 'provider_finished':
            raise IOError('trace unavailable')

    monkeypatch.setattr(council, 'provider_for', provider)
    with pytest.raises(IOError, match='trace unavailable'):
        council._ask(council.Seat('FIRST', 'audit', 'fixture'), 'brief', '', on_attempt=recorder)
    assert calls == ['first-model']


def test_failed_provider_seats_make_cli_fail(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(council, '_ask', lambda *a, **k: ('fixture-model', '[FAILED after 1 model(s): OSError]'))
    path = tmp_path / 'failed.jsonl'
    rc = council._main(['fixture', '--seats', 'OPTIONS', '--json', '--trace', str(path)])
    result = json.loads(capsys.readouterr().out)
    assert rc == 1
    assert result['execution_status'] == 'failed'
    assert rows(path)[-1]['failed_seats'] == 2


def test_review_block_is_not_provider_failure(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(council, '_ask', lambda *a, **k: ('fixture-model', 'BLOCK: missing crash evidence'))
    rc = council._main(['fixture', '--seats', 'OPTIONS', '--json', '--trace', str(tmp_path / 'block.jsonl')])
    result = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert result['execution_status'] == 'responses_recorded'
    assert result['chair'].startswith('BLOCK:')
