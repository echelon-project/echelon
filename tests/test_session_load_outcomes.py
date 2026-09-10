import json
import time
from echelon_sdk.session import Session


def test_session_load_distinguishes_missing_corrupt_expired_bloated_and_resumed(tmp_path):
    path = tmp_path / 'session.json'
    assert Session.load_result(path)['status'] == 'missing'
    path.write_bytes(b'{private broken bytes')
    assert Session.load_result(path)['status'] == 'corrupt'
    assert path.read_bytes() == b'{private broken bytes'
    path = tmp_path / 'valid-session.json'
    session = Session(base=[{'role': 'system', 'content': 'boot'}], id='fixture')
    session.save(path)
    result = Session.load_result(path)
    assert result['status'] == 'resumed' and result['session'].id == 'fixture'
    session.created_at = time.time() - 100
    session.save(path)
    assert Session.load_result(path, max_age_seconds=1)['status'] == 'expired'
    session.bloat_tokens = 1
    session.save(path)
    assert Session.load_result(path, max_age_seconds=None)['status'] == 'bloated'
    assert Session.load(path, max_age_seconds=None) is None


def test_malformed_history_is_corrupt_instead_of_raising(tmp_path):
    path = tmp_path / 'session.json'
    data = Session(base=[]).to_dict()
    data['history'] = ['invalid message']
    path.write_text(json.dumps(data), encoding='utf-8')
    assert Session.load_result(path)['status'] == 'corrupt'
    assert Session.load(path) is None


def test_dispatch_emits_sanitized_continuity_and_preserves_corrupt_file(tmp_path):
    import pytest
    from echelon_engine.agent.workflow import _load_session_for_dispatch
    path = tmp_path / 'session.json'
    events = []
    session, metadata = _load_session_for_dispatch(path, lambda kind, data: events.append((kind, data)))
    assert session is None and metadata['status'] == 'missing'
    path.write_bytes(b'{private broken context')
    with pytest.raises(RuntimeError, match='preserve and diagnose'):
        _load_session_for_dispatch(path, lambda kind, data: events.append((kind, data)))
    assert events[-1][0] == 'session_load'
    assert events[-1][1]['status'] == 'corrupt'
    assert 'private' not in str(events)
    assert path.read_bytes() == b'{private broken context'


def test_session_flush_failure_preserves_previous_bytes(tmp_path, monkeypatch):
    import os
    import pytest
    path = tmp_path / 'session.json'
    session = Session(base=[{'role': 'system', 'content': 'first'}])
    session.save(path)
    previous = path.read_bytes()
    session.begin_task('new task')
    def fail(fd):
        raise OSError('fixture fsync failure')
    monkeypatch.setattr(os, 'fsync', fail)
    with pytest.raises(OSError):
        session.save(path)
    assert path.read_bytes() == previous
    assert list(tmp_path.glob('*.tmp')) == []


def test_session_replace_failure_preserves_previous_bytes(tmp_path, monkeypatch):
    import os
    import pytest
    path = tmp_path / 'session.json'
    session = Session(base=[])
    session.save(path)
    previous = path.read_bytes()
    def fail(*args):
        raise OSError('fixture replace failure')
    monkeypatch.setattr(os, 'replace', fail)
    session.begin_task('new task')
    from echelon_sdk.session import SessionCommitUnknown
    with pytest.raises(SessionCommitUnknown):
        session.save(path)
    assert path.read_bytes() == previous
    assert list(tmp_path.glob('*.tmp')) == []


def test_session_process_exit_before_replace_preserves_previous_session(tmp_path):
    import subprocess
    import sys
    path = tmp_path / 'session.json'
    Session(base=[{'role': 'system', 'content': 'original'}], id='previous').save(path)
    original = path.read_bytes()
    script = """
import os,sys
from echelon_sdk.session import Session
def die(*args): os._exit(87)
os.replace = die
previous = Session.load_result(sys.argv[1])
Session(base=[{'role':'system','content':'replacement'}], id='replacement').save(sys.argv[1], expected_digest=previous['observed_digest'])
"""
    proc = subprocess.run([sys.executable, '-X', 'utf8', '-c', script, str(path)],
                          capture_output=True, timeout=20)
    assert proc.returncode == 87, proc.stderr
    assert path.read_bytes() == original
    assert Session.load_result(path)['session'].id == 'previous'
    staged = list(tmp_path.glob('*.tmp'))
    assert len(staged) == 1
    assert json.loads(staged[0].read_text(encoding='utf-8'))['id'] == 'replacement'


def test_stale_session_writer_cannot_replace_newer_progress(tmp_path):
    import pytest
    from echelon_sdk.session import SessionConflict
    path = tmp_path / 'session.json'
    Session(base=[]).save(path)
    first = Session.load(path)
    second = Session.load(path)
    first.begin_task('winning progress')
    first.save(path)
    saved = path.read_bytes()
    second.begin_task('stale progress')
    with pytest.raises(SessionConflict):
        second.save(path)
    assert path.read_bytes() == saved
    assert second.history[-1]['content'] == 'GOAL: stale progress'


def test_retirement_archives_exact_observed_bytes_and_refuses_stale_owner(tmp_path):
    import pytest
    from pathlib import Path
    from echelon_sdk.session import SessionConflict
    path = tmp_path / 'session.json'
    session = Session(base=[])
    session.save(path)
    original = Session.load_result(path)
    session.begin_task('newer progress')
    session.save(path)
    current = path.read_bytes()
    with pytest.raises(SessionConflict):
        Session.retire(path, expected_digest=original['observed_digest'])
    assert path.read_bytes() == current
    latest = Session.load_result(path)
    retired = Session.retire(path, expected_digest=latest['observed_digest'])
    assert retired['status'] == 'retired'
    assert not path.exists()
    assert Path(retired['archive']).read_bytes() == current


def test_two_processes_cannot_both_save_same_predecessor(tmp_path):
    import subprocess
    import sys
    path = tmp_path / 'session.json'
    Session(base=[]).save(path)
    script = """
import json,sys,time
from pathlib import Path
from echelon_sdk.session import Session,SessionConflict,SessionBusy
path=Path(sys.argv[1]); label=sys.argv[2]
session=Session.load(path)
(path.parent/(label+'.ready')).write_text('ready')
deadline=time.monotonic()+10
while not all((path.parent/(n+'.ready')).exists() for n in ('one','two')):
    if time.monotonic()>deadline: raise RuntimeError('barrier timed out')
    time.sleep(.01)
session.begin_task(label)
try:
    session.save(path)
    status='saved'
except SessionConflict:
    status='conflict'
except SessionBusy:
    status='lock_busy'
print(json.dumps({'status':status,'label':label}))
"""
    workers = [subprocess.Popen([sys.executable, '-X', 'utf8', '-c', script, str(path), label],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE) for label in ('one', 'two')]
    results = []
    for worker in workers:
        stdout, stderr = worker.communicate(timeout=20)
        assert worker.returncode == 0, stderr
        results.append(json.loads(stdout))
    assert sum(r['status'] == 'saved' for r in results) == 1
    winner = next(r['label'] for r in results if r['status'] == 'saved')
    assert Session.load(path).history[-1]['content'] == 'GOAL: ' + winner
    assert len(list(tmp_path.glob('session.json.*.tmp'))) == 0


def test_boot_digest_detects_saved_and_in_memory_boot_changes(tmp_path):
    import pytest
    path = tmp_path / 'session.json'
    session = Session(base=[{'role': 'system', 'content': 'original boot'}])
    session.save(path)
    assert Session.load_result(path)['boot_integrity'] == 'verified'
    data = json.loads(path.read_text(encoding='utf-8'))
    data['base'][0]['content'] = 'changed boot'
    path.write_text(json.dumps(data), encoding='utf-8')
    assert Session.load_result(path)['status'] == 'corrupt'
    session.base[0]['content'] = 'in-memory change'
    with pytest.raises(ValueError, match='boot context changed'):
        session.to_dict()


def test_legacy_boot_is_explicitly_unverified(tmp_path):
    path = tmp_path / 'session.json'
    data = Session(base=[]).to_dict()
    del data['boot_sha256']
    path.write_text(json.dumps(data), encoding='utf-8')
    result = Session.load_result(path)
    assert result['status'] == 'resumed'
    assert result['boot_integrity'] == 'legacy_unverified'


def test_dispatch_persistence_failure_records_task_effects_without_private_data(tmp_path):
    import pytest
    from echelon_engine.agent.workflow import _persist_dispatch_session
    from echelon_sdk.session import SessionConflict
    path = tmp_path / 'session.json'
    current = Session(base=[])
    current.save(path)
    stale = Session(base=[])
    events = []
    with pytest.raises(SessionConflict):
        _persist_dispatch_session(path, stale, {'observed_digest': None},
            lambda kind, data: events.append((kind, data)))
    assert events == [('session_persistence_failed', {'error_type': 'SessionConflict',
                       'task_effects': 'not_rolled_back', 'retry_safe': False,
                       'predecessor_id': None, 'expected_digest': None})]
    observed = Session.load_result(path)
    outcome = _persist_dispatch_session(path, observed['session'], observed,
        lambda kind, data: events.append((kind, data)))
    assert outcome['status'] == 'saved'
    assert events[-1][0] == 'session_persistence'
    assert len(outcome['sha256']) == 64


def test_changed_history_boot_prefix_is_not_verified_by_unchanged_base(tmp_path):
    path = tmp_path / 'session.json'
    Session(base=[{'role': 'system', 'content': 'original boot'}]).save(path)
    data = json.loads(path.read_text(encoding='utf-8'))
    data['history'][0]['content'] = 'altered instructions'
    path.write_text(json.dumps(data), encoding='utf-8')
    before = path.read_bytes()
    result = Session.load_result(path)
    assert result['status'] == 'corrupt'
    assert result['session'] is None
    assert path.read_bytes() == before


def test_lock_io_failure_is_not_mislabeled_as_contention(tmp_path, monkeypatch):
    import errno
    import os
    import pytest
    if os.name == 'nt':
        import msvcrt as locking_module
        function = 'locking'
    else:
        import fcntl as locking_module
        function = 'flock'
    def fail(*args):
        raise OSError(errno.EIO, 'fixture disk error')
    monkeypatch.setattr(locking_module, function, fail)
    with pytest.raises(OSError) as caught:
        Session(base=[]).save(tmp_path / 'session.json')
    assert caught.value.errno == errno.EIO


def test_session_lost_replace_ack_reconciles_candidate(tmp_path, monkeypatch):
    import os
    import pytest
    from echelon_sdk.session import SessionCommitUnknown
    path = tmp_path / 'session.json'
    session = Session(base=[])
    session.save(path)
    session.begin_task('completed task')
    real = os.replace
    def lost_ack(*args):
        real(*args)
        raise OSError('fixture lost acknowledgement')
    monkeypatch.setattr(os, 'replace', lost_ack)
    with pytest.raises(SessionCommitUnknown) as caught:
        session.save(path)
    assert caught.value.operation == 'save'
    assert Session.reconcile(path, caught.value.candidate_digest)['status'] == 'candidate_present'
    assert Session.load(path).history[-1]['content'] == 'GOAL: completed task'


def test_retirement_lost_ack_exposes_archive_for_exact_reconciliation(tmp_path, monkeypatch):
    import os
    import pytest
    from echelon_sdk.session import SessionCommitUnknown
    from echelon_engine.agent.workflow import _persist_dispatch_session
    path = tmp_path / 'session.json'
    Session(base=[]).save(path)
    continuity = Session.load_result(path)
    real = os.rename
    def lost_ack(*args):
        real(*args)
        raise OSError('fixture lost rename acknowledgement')
    monkeypatch.setattr(os, 'rename', lost_ack)
    events = []
    with pytest.raises(SessionCommitUnknown) as caught:
        _persist_dispatch_session(path, None, continuity,
            lambda kind, data: events.append((kind, data)))
    failure = events[-1][1]
    assert failure['operation'] == 'retire'
    assert failure['expected_digest'] == continuity['observed_digest']
    assert failure['predecessor_id'] == continuity['predecessor_id']
    assert Session.reconcile(failure['archive'], failure['candidate_digest'])['status'] == 'candidate_present'
    assert not path.exists()


def test_legacy_session_resave_cannot_promote_boot_integrity(tmp_path):
    import pytest
    from echelon_engine.agent.workflow import _load_session_for_dispatch
    path = tmp_path / 'legacy.json'
    data = Session(base=[]).to_dict()
    del data['boot_sha256']
    path.write_text(json.dumps(data), encoding='utf-8')
    session = Session.load(path)
    session.begin_task('preserve legacy provenance')
    session.save(path)
    result = Session.load_result(path)
    assert result['boot_integrity'] == 'legacy_unverified'
    before = path.read_bytes()
    events = []
    with pytest.raises(RuntimeError, match='boot integrity unverified'):
        _load_session_for_dispatch(path, lambda kind, data: events.append((kind, data)))
    assert path.read_bytes() == before
    assert events[-1][1]['boot_integrity'] == 'legacy_unverified'


def test_unknown_boot_integrity_is_corrupt(tmp_path):
    path = tmp_path / 'session.json'
    data = Session(base=[]).to_dict()
    data['boot_integrity'] = 'trusted-by-guess'
    path.write_text(json.dumps(data), encoding='utf-8')
    assert Session.load_result(path)['status'] == 'corrupt'


def test_boot_manifest_roundtrip_and_explicit_incompatibility(tmp_path):
    import copy
    manifest = {'schema_version': 1, 'model': 'model-a', 'tools_sha256': 'a' * 64,
                'permissions': {'write': False}, 'doctrine_sha256': 'b' * 64}
    path = tmp_path / 'session.json'
    session = Session(base=[], boot_manifest=copy.deepcopy(manifest))
    session.save(path)
    loaded = Session.load(path)
    assert loaded.check_boot_manifest(manifest)['compatible'] is True
    for field, value in [('model', 'model-b'), ('tools_sha256', 'c' * 64),
                         ('permissions', {'write': True}), ('doctrine_sha256', 'd' * 64)]:
        candidate = dict(manifest, **{field: value})
        outcome = loaded.check_boot_manifest(candidate)
        assert outcome == {'status': 'incompatible', 'compatible': False, 'changed_fields': [field]}
    assert Session(base=[]).check_boot_manifest(manifest) == {'status': 'unbound', 'compatible': False}


def test_execution_check_rejects_unbound_and_changed_context_before_resume(tmp_path):
    import pytest
    from echelon_engine.agent.workflow import _execution_manifest, _check_session_execution
    from echelon_engine.atoms.tools import ToolRegistry
    tools = ToolRegistry(tmp_path, allow_write=False, allow_bash=False)
    manifest = _execution_manifest('model-a', tools, role='reviewer', doctrine={'rule': 'inspect'})
    continuity = {'predecessor_id': 'previous', 'observed_digest': 'a' * 64}
    events = []
    with pytest.raises(RuntimeError, match='unbound'):
        _check_session_execution(Session(base=[]), manifest, continuity,
                                 lambda k, d: events.append((k, d)))
    assert events[-1][1]['compatible'] is False
    assert events[-1][1]['predecessor_id'] == 'previous'
    session = Session(base=[], boot_manifest=manifest)
    assert _check_session_execution(session, manifest, continuity)['compatible']
    changed = _execution_manifest('model-b', tools, role='reviewer', doctrine={'rule': 'inspect'})
    with pytest.raises(RuntimeError, match='incompatible'):
        _check_session_execution(session, changed, continuity)
    assert manifest['model_provenance'] == 'requested_not_provider_attested'


def test_malformed_saved_manifest_is_corrupt_and_preserved(tmp_path):
    path = tmp_path / 'session.json'
    valid = {'schema_version': 1, 'model': 'model-a', 'tools_sha256': 'a' * 64,
             'permissions': {}, 'doctrine_sha256': 'b' * 64}
    malformed = [[], dict(valid, schema_version=True), dict(valid, model=''),
                 dict(valid, tools_sha256='not-a-hash'), dict(valid, permissions=None)]
    for manifest in malformed:
        data = Session(base=[]).to_dict()
        data['boot_manifest'] = manifest
        path.write_text(json.dumps(data), encoding='utf-8')
        before = path.read_bytes()
        result = Session.load_result(path)
        assert result['status'] == 'corrupt'
        assert result['session'] is None
        assert path.read_bytes() == before


def test_manifest_binding_is_once_and_detects_memory_and_disk_edits(tmp_path):
    import pytest
    manifest = {'schema_version': 1, 'model': 'model-a', 'tools_sha256': 'a' * 64,
                'permissions': {}, 'doctrine_sha256': 'b' * 64}
    session = Session(base=[])
    session.bind_boot_manifest(manifest)
    manifest['model'] = 'external mutation'
    assert session.boot_manifest['model'] == 'model-a'
    with pytest.raises(ValueError, match='new unsaved session'):
        session.bind_boot_manifest(manifest)
    path = tmp_path / 'session.json'
    session.save(path)
    session.boot_manifest['model'] = 'memory mutation'
    with pytest.raises(ValueError, match='changed after binding'):
        session.check_boot_manifest(manifest)
    with pytest.raises(ValueError, match='changed after binding'):
        session.save(path)
    data = json.loads(path.read_text(encoding='utf-8'))
    data['boot_manifest']['model'] = 'disk mutation'
    path.write_text(json.dumps(data), encoding='utf-8')
    assert Session.load_result(path)['status'] == 'corrupt'


def test_workflow_cold_warm_and_model_change_preserves_predecessor(tmp_path, monkeypatch):
    import pytest
    from types import SimpleNamespace
    from echelon_engine.agent import workflow, loop
    monkeypatch.setattr(workflow._roles, 'role_device_dir', lambda role: None)
    monkeypatch.setattr(workflow._roles, 'build_role', lambda *args: None)
    calls = []
    def worker(goal, provider, tools, model, *, session=None, **kwargs):
        calls.append((model, session is not None))
        if session is None:
            session = Session(base=[{'role': 'system', 'content': 'fixture boot'}])
        session.begin_task(goal)
        return SimpleNamespace(status='completed', answer='fixture', steps=1), session
    monkeypatch.setattr(loop, 'run_task', worker)
    path = tmp_path / 'session.json'
    events = []
    runner = workflow.make_agent_runner(object(), 'model-a', str(tmp_path),
        session_path=str(path), on_event=lambda k, d: events.append((k, d)))
    cold = runner({'agent': 'dev', 'task': 'inspect'}, {})
    assert cold['session_compatibility']['status'] == 'cold_start'
    saved = Session.load(path)
    assert saved.boot_manifest['model'] == 'model-a'
    assert saved.boot_manifest_sha256
    warm = runner({'agent': 'dev', 'task': 'inspect again'}, {})
    assert warm['session_compatibility']['status'] == 'compatible'
    before = path.read_bytes()
    changed = workflow.make_agent_runner(object(), 'model-b', str(tmp_path), session_path=str(path),
        on_event=lambda k, d: events.append((k, d)))
    with pytest.raises(RuntimeError, match='incompatible'):
        changed({'agent': 'dev', 'task': 'inspect differently'}, {})
    assert calls == [('model-a', False), ('model-a', True)]
    assert path.read_bytes() == before
    decision = [data for kind, data in events if kind == 'session_compatibility'][-1]
    assert decision['changed_fields'] == ['model']
    assert decision['compatible'] is False
    from echelon_sdk import environment
    original_probe = environment.probe
    def changed_environment(root):
        snapshot = original_probe(root)
        snapshot.python = 'changed-interpreter'
        return snapshot
    monkeypatch.setattr(environment, 'probe', changed_environment)
    with pytest.raises(RuntimeError, match='incompatible'):
        runner({'agent': 'dev', 'task': 'same model, changed runtime'}, {})
    assert path.read_bytes() == before
    assert calls == [('model-a', False), ('model-a', True)]
    assert [data for kind, data in events if kind == 'session_compatibility'][-1]['changed_fields'] == ['doctrine_sha256']


def test_saved_manifest_missing_digest_is_not_silently_certified(tmp_path):
    manifest = {'schema_version': 1, 'model': 'model-a', 'tools_sha256': 'a' * 64,
                'permissions': {}, 'doctrine_sha256': 'b' * 64}
    data = Session(base=[], boot_manifest=manifest).to_dict()
    del data['boot_manifest_sha256']
    path = tmp_path / 'session.json'
    path.write_text(json.dumps(data), encoding='utf-8')
    before = path.read_bytes()
    assert Session.load_result(path)['status'] == 'corrupt'
    assert path.read_bytes() == before


def test_explicit_absence_expectation_cannot_adopt_loaded_predecessor(tmp_path):
    import pytest
    from echelon_sdk.session import SessionConflict
    path = tmp_path / 'session.json'
    Session(base=[]).save(path)
    loaded = Session.load(path)
    before = path.read_bytes()
    loaded.begin_task('new work')
    with pytest.raises(SessionConflict):
        loaded.save(path, expected_digest=None)
    assert path.read_bytes() == before
    loaded.save(path)
    assert Session.load(path).history[-1]['content'] == 'GOAL: new work'


def test_simultaneous_cold_sessions_have_distinct_persistent_identities(tmp_path, monkeypatch):
    import time
    monkeypatch.setattr(time, 'strftime', lambda *args: 'same-second')
    first, second = Session(base=[]), Session(base=[])
    assert first.id != second.id
    for index, session in enumerate((first, second)):
        path = tmp_path / f'session-{index}.json'
        session.save(path)
        assert Session.load(path).id == session.id
    legacy = Session(base=[], id='sess-20260908-120000')
    assert Session.from_dict(legacy.to_dict()).id == legacy.id
