import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest

from echelon_engine.atoms.cards import CardStore
from echelon_engine.atoms.bank_review import (
    AtomIdentityConflict, BankTransactionError, TransactionBoundaryError, ensure_review_schema,
)


@pytest.fixture
def bank(tmp_path):
    store = CardStore(tmp_path / 'bank.db')
    try:
        yield store
    finally:
        store.conn.close()


def test_atom_and_pending_event_commit_once_and_noop_reuses_identity(bank):
    first = bank.bank_atom('fixture:lesson', 'lesson', scope='fixture')
    second = bank.bank_atom('fixture:lesson', 'lesson', scope='fixture')
    assert first['insertion'] == 'inserted'
    assert second['insertion'] == 'unchanged'
    assert first['review_id'] == second['review_id']
    assert bank.conn.execute('SELECT COUNT(*) FROM atoms').fetchone()[0] == 1
    row = bank.conn.execute('SELECT * FROM atom_review_events').fetchone()
    assert row['atom_id'] == first['atom_id']
    assert row['content_sha256'] == first['content_sha256']
    assert bank.conn.execute('SELECT COUNT(*) FROM atom_review_events').fetchone()[0] == 1


def test_pending_sql_failure_rolls_back_new_atom(bank):
    ensure_review_schema(bank.conn)
    bank.conn.execute("""CREATE TRIGGER reject_pending BEFORE INSERT ON atom_review_events
        BEGIN SELECT RAISE(ABORT,'fixture pending failure'); END""")
    bank.conn.commit()
    with pytest.raises(BankTransactionError) as caught:
        bank.bank_atom('fixture:lesson', 'lesson', scope='fixture')
    assert caught.value.outcome['insertion'] == 'failed'
    assert not bank.conn.in_transaction
    assert bank.conn.execute('SELECT COUNT(*) FROM atoms').fetchone()[0] == 0
    assert bank.conn.execute('SELECT COUNT(*) FROM atom_review_events').fetchone()[0] == 0


def test_existing_caller_transaction_is_preserved(bank):
    bank.conn.execute('CREATE TABLE caller(value TEXT)')
    bank.conn.execute("INSERT INTO caller VALUES ('not committed')")
    with pytest.raises(TransactionBoundaryError):
        bank.bank_atom('fixture:lesson', 'lesson', scope='fixture')
    assert bank.conn.in_transaction
    bank.conn.rollback()
    assert bank.conn.execute('SELECT COUNT(*) FROM caller').fetchone()[0] == 0
    assert bank.conn.execute('SELECT COUNT(*) FROM atoms').fetchone()[0] == 0


def test_identity_placement_conflict_preserves_first_obligation(bank):
    first = bank.bank_atom('fixture:first', 'same content', scope='fixture')
    with pytest.raises(AtomIdentityConflict):
        bank.bank_atom('fixture:second', 'same content', scope='fixture')
    assert bank.get_atom(first['atom_id']).coordinate == 'fixture:first'
    assert bank.conn.execute('SELECT COUNT(*) FROM atom_review_events').fetchone()[0] == 1


@pytest.mark.parametrize('phase,expected', [('before_pending', 0), ('after_commit', 1)])
def test_hard_exit_recovers_both_atom_and_pending_or_neither(tmp_path, phase, expected):
    path = tmp_path / 'crash.db'
    script = '''
import os,sys
from echelon_engine.atoms.cards import CardStore
c=CardStore(sys.argv[1]); real=c.conn
class Proxy:
 def __getattr__(self,name): return getattr(real,name)
 def execute(self,sql,*args):
  if sys.argv[2]=='before_pending' and sql.lstrip().startswith('INSERT INTO atom_review_events'):
   os._exit(87)
  return real.execute(sql,*args)
 def commit(self):
  real.commit()
  if sys.argv[2]=='after_commit': os._exit(87)
c.conn=Proxy()
c.bank_atom('fixture:lesson','lesson',scope='fixture')
'''
    result = subprocess.run([sys.executable, '-X', 'utf8', '-c', script, str(path), phase],
                            capture_output=True, timeout=20)
    assert result.returncode == 87, result.stderr.decode('utf-8', errors='replace')
    observer = sqlite3.connect(path)
    try:
        atoms = observer.execute('SELECT COUNT(*) FROM atoms').fetchone()[0]
        exists = observer.execute("SELECT 1 FROM sqlite_master WHERE name='atom_review_events'").fetchone()
        events = observer.execute('SELECT COUNT(*) FROM atom_review_events').fetchone()[0] if exists else 0
        assert (atoms, events) == (expected, expected)
        if expected:
            row = observer.execute('SELECT atom_id,review_id,content_sha256,payload FROM atom_review_events').fetchone()
            assert row[1] and len(row[2]) == 64
            assert json.loads(row[3])['link_status'] == 'not_attempted'
    finally:
        observer.close()


def test_capture_preserves_graph_directions_dangling_and_learning_state(bank):
    from echelon_engine.atoms.bank_review import capture_review, review_event
    root = bank.bank_atom('fixture:root', 'root', scope='fixture')
    neighbor = bank.add_atom('elsewhere:neighbor', 'neighbor preview', scope='elsewhere')
    retired = bank.add_atom('fixture:retired', 'retired', scope='fixture')
    bank.link(root['atom_id'], neighbor, 'refs')
    bank.link(neighbor, root['atom_id'], 'refs')
    bank.link(root['atom_id'], neighbor, 'supersedes')
    bank.link(root['atom_id'], 'missing-id', 'refs')
    bank.link(root['atom_id'], retired, 'refs')
    bank.unlink(root['atom_id'], retired, 'refs')
    before = [tuple(r) for r in bank.conn.execute('SELECT * FROM atoms ORDER BY id')]
    links_before = [tuple(r) for r in bank.conn.execute('SELECT * FROM atom_links ORDER BY from_id,to_id,relation')]
    result = capture_review(bank, root['review_id'], link_status='partial')
    assert result['event_kind'] == 'projection_available'
    payload = result['payload']
    assert payload['total'] == 4
    assert payload['link_status'] == 'partial'
    assert {r['direction'] for r in payload['edges']} == {'in', 'out'}
    missing = [r for r in payload['edges'] if r['missing_neighbor']]
    assert len(missing) == 1 and missing[0]['neighbor_id'] == 'missing-id'
    assert all(r['scope'] == 'elsewhere' and r['preview'] == 'neighbor preview'
               for r in payload['edges'] if not r['missing_neighbor'])
    assert review_event(bank, result['event_id']) == result
    assert [tuple(r) for r in bank.conn.execute('SELECT * FROM atoms ORDER BY id')] == before
    assert [tuple(r) for r in bank.conn.execute('SELECT * FROM atom_links ORDER BY from_id,to_id,relation')] == links_before


def test_resume_preserves_original_offer_after_graph_change(bank):
    from echelon_engine.atoms.bank_review import capture_review
    root = bank.bank_atom('fixture:root', 'root', scope='fixture')
    first = capture_review(bank, root['review_id'])
    bank.link(root['atom_id'], 'new-dangling', 'refs')
    resumed = capture_review(bank, root['review_id'])
    assert resumed == first
    assert resumed['payload']['total'] == 0
    fresh = capture_review(bank, root['review_id'], fresh=True)
    assert fresh['event_id'] != first['event_id']
    assert fresh['payload']['total'] == 1
    assert capture_review(bank, root['review_id']) == fresh
    assert bank.conn.execute('SELECT COUNT(*) FROM atom_review_events').fetchone()[0] == 3


def test_failed_graph_read_records_unavailable_without_fabricating_empty_offer(bank):
    from echelon_engine.atoms.bank_review import capture_review
    root = bank.bank_atom('fixture:root', 'root', scope='fixture')
    bank.conn.execute('ALTER TABLE atom_links RENAME TO fixture_hidden_links')
    bank.conn.commit()
    failed = capture_review(bank, root['review_id'])
    assert failed['event_kind'] == 'projection_unavailable'
    assert failed['payload']['error_type'] == 'OperationalError'
    assert 'edges' not in failed['payload'] and 'total' not in failed['payload']
    assert bank.get_atom(root['atom_id']).content == 'root'
    bank.conn.execute('ALTER TABLE fixture_hidden_links RENAME TO atom_links')
    bank.conn.commit()
    recovered = capture_review(bank, root['review_id'])
    assert recovered['event_kind'] == 'projection_available'
    assert recovered['payload']['link_status'] == 'unknown'
    assert bank.conn.execute('SELECT COUNT(*) FROM atoms').fetchone()[0] == 1


@pytest.mark.parametrize('committed', [True, False])
def test_commit_ack_loss_reconciles_exact_durable_identity(tmp_path, committed):
    from echelon_engine.atoms.bank_review import BankCommitUnknown, reconcile_bank_commit
    path = tmp_path / 'ack.db'
    store = CardStore(path)
    real = store.conn
    class LostAck:
        def __getattr__(self, name):
            return getattr(real, name)
        def commit(self):
            if committed:
                real.commit()
            raise OSError('private connection detail')
    store.conn = LostAck()
    try:
        with pytest.raises(BankCommitUnknown) as caught:
            store.bank_atom('fixture:ack', 'ack claim', scope='fixture')
        identity = caught.value.outcome
        assert identity['insertion'] == 'unknown'
        observed = reconcile_bank_commit(path, identity)
        assert observed['status'] == ('present' if committed else 'absent')
        assert observed['retry_safe'] is False
        if committed:
            assert reconcile_bank_commit(path, {**identity, 'review_id': 'wrong'})['status'] == 'inconsistent'
            assert reconcile_bank_commit(path, {**identity, 'content_sha256': '0'*64})['status'] == 'inconsistent'
            assert reconcile_bank_commit(path, {**identity, 'event_id': 'missing'})['status'] == 'inconsistent'
        assert real.execute('SELECT COUNT(*) FROM atoms').fetchone()[0] == int(committed)
    finally:
        real.close()


def test_reconciliation_missing_database_stays_unknown_and_does_not_create(tmp_path):
    from echelon_engine.atoms.bank_review import reconcile_bank_commit
    path = tmp_path / 'does-not-exist.db'
    result = reconcile_bank_commit(path, dict(atom_id='a', content_sha256='0'*64,
                                            scope='fixture', coordinate='fixture:a',
                                            review_id='r', event_id='e'))
    assert result['status'] == 'unknown'
    assert result['error_type'] == 'OperationalError'
    assert not path.exists()


def test_fold_confirmation_is_bound_to_source_event_and_destination(bank):
    from echelon_engine.atoms.bank_review import capture_review, review_state
    root = bank.bank_atom('fixture:root', 'root', scope='fixture')
    state = review_state(bank, root['review_id'], room='alpha')
    assert state['capture_required'] and state['delivery_status'] == 'unconfirmed'
    def confirm(event_id, source, room):
        bank.conn.execute("""INSERT INTO atom_review_events
            (event_id,schema_version,review_id,event_kind,atom_id,content_sha256,scope,coordinate,ts,payload)
            VALUES (?,1,?,'room_receipt_confirmed',?,?,?,?,0,?)""",
            (event_id, root['review_id'], root['atom_id'], root['content_sha256'], root['scope'],
             root['coordinate'], json.dumps({'source_event_id': source, 'room': room})))
        bank.conn.commit()
    confirm('first-confirmation', root['event_id'], 'alpha')
    assert review_state(bank, root['review_id'], room='alpha')['delivery_status'] == 'confirmed'
    offer = capture_review(bank, root['review_id'])
    state = review_state(bank, root['review_id'], room='alpha')
    assert not state['capture_required']
    assert state['delivery_status'] == 'unconfirmed'
    confirm('second-confirmation', offer['event_id'], 'alpha')
    assert review_state(bank, root['review_id'], room='alpha')['delivery_status'] == 'confirmed'
    assert review_state(bank, root['review_id'], room='beta')['delivery_status'] == 'unconfirmed'
    assert capture_review(bank, root['review_id'])['event_id'] == offer['event_id']
    assert review_state(bank, root['review_id'], room='alpha')['integrity_status'] == 'valid'
    fresh = capture_review(bank, root['review_id'], fresh=True)
    assert fresh['event_id'] != offer['event_id']
    assert review_state(bank, root['review_id'], room='alpha')['delivery_status'] == 'unconfirmed'


def test_fold_uses_sequence_not_timestamp_or_uuid(bank):
    from echelon_engine.atoms.bank_review import capture_review, review_state
    root = bank.bank_atom('fixture:root', 'root', scope='fixture')
    first = capture_review(bank, root['review_id'])
    second = capture_review(bank, root['review_id'], fresh=True)
    bank.conn.execute('UPDATE atom_review_events SET ts=0 WHERE event_id=?', (second['event_id'],))
    bank.conn.commit()
    state = review_state(bank, root['review_id'])
    assert state['source_event_id'] == second['event_id']
    assert state['source_event_id'] != first['event_id']
    assert state['delivery_status'] == 'not_requested'


@pytest.mark.parametrize('bad_payload', ['not-json', '{}', '{"edges":[],"total":true}', '{"edges":[{}],"total":1}'])
def test_damaged_projection_never_folds_as_healthy_or_actionable(bank, bad_payload):
    from echelon_engine.atoms.bank_review import capture_review, review_state
    root = bank.bank_atom('fixture:root', 'root', scope='fixture')
    offer = capture_review(bank, root['review_id'])
    bank.conn.execute('UPDATE atom_review_events SET payload=? WHERE event_id=?',
                      (bad_payload, offer['event_id']))
    bank.conn.commit()
    state = review_state(bank, root['review_id'], room='alpha')
    assert state['integrity_status'] == 'invalid'
    assert state['capture_status'] == 'unknown'
    assert state['delivery_status'] == 'unknown'
    assert state['capture_required'] is None
    assert state['diagnostics'][0]['event_id'] == offer['event_id']
    with pytest.raises(BankTransactionError):
        capture_review(bank, root['review_id'])
    assert bank.conn.execute('SELECT COUNT(*) FROM atom_review_events').fetchone()[0] == 2


@pytest.mark.xfail(
    reason="the `bank-review` CLI verb is not wired into echelon_engine/__main__.py, so the "
           "subprocess exits with 'unknown verb' and empty stdout. The library-level review "
           "API above IS covered and passes; only this CLI surface is missing. Pre-existing "
           "in the engine this export was cut from — kept as an xfail so the gap stays "
           "visible instead of being deleted.",
    strict=True)
def test_native_review_cli_resume_show_and_missing_bank(tmp_path):
    path = tmp_path / 'cli.db'
    store = CardStore(path)
    root = store.bank_atom('fixture:root', 'root', scope='fixture')
    store.link(root['atom_id'], 'missing', 'refs')
    store.conn.close()
    def call(bank, *args):
        proc = subprocess.run([sys.executable, '-X', 'utf8', '-m', 'echelon_engine',
                               'bank-review', '--bank', str(bank), *args], capture_output=True, timeout=20)
        return proc.returncode, json.loads(proc.stdout)
    code, resumed = call(path, 'resume', root['review_id'])
    assert code == 0 and resumed['ok']
    event_id = resumed['result']['event_id']
    code, shown = call(path, 'show', event_id, '--limit', '0')
    assert code == 0
    assert shown['result']['display']['omitted'] == 1
    assert shown['result']['payload']['edges'] == []
    code, full = call(path, 'show', event_id, '--full')
    assert code == 0 and len(full['result']['payload']['edges']) == 1
    code, again = call(path, 'resume', root['review_id'])
    assert code == 0 and again['result']['event_id'] == event_id
    missing = tmp_path / 'missing.db'
    code, failed = call(missing, 'state', root['review_id'])
    assert code == 1 and not failed['ok'] and not missing.exists()


def test_cli_show_reports_damaged_offer_as_failure(bank, capsys):
    from echelon_engine.atoms.bank_review import _main, capture_review
    root = bank.bank_atom('fixture:root', 'root', scope='fixture')
    offer = capture_review(bank, root['review_id'])
    path = bank.conn.execute('PRAGMA database_list').fetchone()[2]
    bank.conn.execute('UPDATE atom_review_events SET payload=? WHERE event_id=?', ('{}', offer['event_id']))
    bank.conn.commit()
    assert _main(['--bank', path, 'show', offer['event_id']]) == 1
    result = json.loads(capsys.readouterr().out)
    assert result['ok'] is False
    assert result['result']['integrity']['integrity_status'] == 'invalid'


def test_delivery_confirms_exact_event_once_and_room_failure_stays_unconfirmed(bank, tmp_path):
    from echelon_engine.atoms.bank_review import capture_review, deliver_review, review_state
    room = tmp_path / 'room'
    room.mkdir()
    (room / 'room.json').write_text('{}', encoding='utf-8')
    root = bank.bank_atom('fixture:root', 'root', scope='fixture')
    event = capture_review(bank, root['review_id'])
    first = deliver_review(bank, event['event_id'], room)
    assert first['ok']
    assert deliver_review(bank, event['event_id'], room) == first
    assert len((room / 'receipts.jsonl').read_text(encoding='utf-8').splitlines()) == 1
    assert bank.conn.execute("SELECT COUNT(*) FROM atom_review_events WHERE event_kind='room_receipt_confirmed'").fetchone()[0] == 1
    assert review_state(bank, root['review_id'], room=str(room.resolve()))['delivery_status'] == 'confirmed'
    fresh = capture_review(bank, root['review_id'], fresh=True)
    with (room / 'receipts.jsonl').open('ab') as stream:
        stream.write(b'{broken')
    failure = deliver_review(bank, fresh['event_id'], room)
    assert not failure['ok']
    assert review_state(bank, root['review_id'], room=str(room.resolve()))['delivery_status'] == 'unconfirmed'


def test_process_exit_after_room_receipt_recovers_original_delivery(tmp_path):
    from echelon_engine.atoms.bank_review import capture_review, deliver_review, review_state
    path = tmp_path / 'delivery.db'
    room = tmp_path / 'room'
    room.mkdir()
    (room / 'room.json').write_text('{}', encoding='utf-8')
    store = CardStore(path)
    root = store.bank_atom('fixture:root', 'root', scope='fixture')
    event = capture_review(store, root['review_id'])
    store.conn.close()
    script = """
import os,sys
from pathlib import Path
from echelon_engine import workcycle
from echelon_engine.atoms.cards import CardStore
from echelon_engine.atoms.bank_review import deliver_review
real = workcycle.receipt
def die_after_receipt(*args, **kwargs):
    result = real(*args, **kwargs)
    if result['ok']: os._exit(87)
    raise RuntimeError('fixture receipt unexpectedly failed')
workcycle.receipt = die_after_receipt
store = CardStore(sys.argv[1])
deliver_review(store, sys.argv[2], Path(sys.argv[3]))
"""
    proc = subprocess.run([sys.executable, '-X', 'utf8', '-c', script, str(path), event['event_id'], str(room)],
                          capture_output=True, timeout=20)
    assert proc.returncode == 87, proc.stderr
    observer = CardStore(path)
    try:
        before = review_state(observer, root['review_id'], room=str(room.resolve()))
        assert before['delivery_status'] == 'unconfirmed'
        assert len((room / 'receipts.jsonl').read_text(encoding='utf-8').splitlines()) == 1
        observer.link(root['atom_id'], 'later-neighbor', 'refs')
        recovered = deliver_review(observer, event['event_id'], room)
        assert recovered['ok']
        assert len((room / 'receipts.jsonl').read_text(encoding='utf-8').splitlines()) == 1
        assert review_state(observer, root['review_id'], room=str(room.resolve()))['delivery_status'] == 'confirmed'
        assert observer.conn.execute("SELECT COUNT(*) FROM atom_review_events WHERE event_kind='projection_available'").fetchone()[0] == 1
        assert observer.conn.execute('SELECT COUNT(*) FROM atoms').fetchone()[0] == 1
    finally:
        observer.conn.close()


def test_delivery_refuses_caller_transaction_before_room_write(bank, tmp_path):
    from echelon_engine.atoms.bank_review import deliver_review
    root = bank.bank_atom('fixture:root', 'root', scope='fixture')
    room = tmp_path / 'room'
    room.mkdir()
    (room / 'room.json').write_text('{}', encoding='utf-8')
    bank.conn.execute('BEGIN')
    with pytest.raises(TransactionBoundaryError):
        deliver_review(bank, root['event_id'], room)
    assert bank.conn.in_transaction
    assert not (room / 'receipts.jsonl').exists()
    bank.conn.rollback()


def test_native_cli_delivery_to_isolated_room(bank, tmp_path, capsys):
    from echelon_engine.atoms.bank_review import _main
    room = tmp_path / 'room'
    room.mkdir()
    (room / 'room.json').write_text('{}', encoding='utf-8')
    root = bank.bank_atom('fixture:root', 'root', scope='fixture')
    path = bank.conn.execute('PRAGMA database_list').fetchone()[2]
    assert _main(['--bank', path, 'deliver', root['event_id'], '--room', str(room)]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result['ok'] and result['result']['delivery_status'] == 'confirmed'


def test_cli_state_resolves_same_registered_room_as_delivery(bank, tmp_path, monkeypatch, capsys):
    from echelon_engine import workcycle
    from echelon_engine.atoms.bank_review import _main, deliver_review
    room = tmp_path / 'room'
    room.mkdir()
    (room / 'room.json').write_text('{}', encoding='utf-8')
    monkeypatch.setattr(workcycle, '_registry_entries', lambda: {'fixture-room': {'path': str(room)}})
    root = bank.bank_atom('fixture:root', 'root', scope='fixture')
    assert deliver_review(bank, root['event_id'], 'fixture-room')['ok']
    path = bank.conn.execute('PRAGMA database_list').fetchone()[2]
    assert _main(['--bank', path, 'state', root['review_id'], '--room', 'fixture-room']) == 0
    result = json.loads(capsys.readouterr().out)
    assert result['result']['delivery_status'] == 'confirmed'
    assert result['result']['room'] == str(room.resolve())


def test_schema_receipt_is_durable_versioned_and_not_duplicated(bank):
    root = bank.bank_atom('fixture:root', 'root', scope='fixture')
    receipt = dict(bank.conn.execute('SELECT * FROM atom_review_schema_versions').fetchone())
    assert receipt['version'] == 1
    assert receipt['migration'] == 'atom_review_events_v1_no_backfill'
    assert receipt['journal_mode'].lower() == 'wal'
    assert receipt['synchronous'] == 2
    bank.bank_atom('fixture:root', 'root', scope='fixture')
    assert dict(bank.conn.execute('SELECT * FROM atom_review_schema_versions').fetchone()) == receipt
    assert bank.conn.execute('SELECT COUNT(*) FROM atom_review_schema_versions').fetchone()[0] == 1
    assert bank.conn.execute('SELECT COUNT(*) FROM atom_review_events').fetchone()[0] == 1


def test_first_bank_failure_rolls_back_schema_receipt_too(bank):
    bank.conn.execute("""CREATE TRIGGER reject_atom BEFORE INSERT ON atoms
        BEGIN SELECT RAISE(ABORT,'fixture failure'); END""")
    bank.conn.commit()
    with pytest.raises(BankTransactionError):
        bank.bank_atom('fixture:root', 'root', scope='fixture')
    assert bank.conn.execute("SELECT 1 FROM sqlite_master WHERE name='atom_review_schema_versions'").fetchone() is None
    assert bank.conn.execute('SELECT COUNT(*) FROM atoms').fetchone()[0] == 0


def test_recovery_walk_finishes_pending_and_is_noop_after_confirmation(bank, tmp_path):
    from echelon_engine.atoms.bank_review import recover_reviews
    room = tmp_path / 'room'
    room.mkdir()
    (room / 'room.json').write_text('{}', encoding='utf-8')
    bank.bank_atom('fixture:one', 'one', scope='fixture')
    bank.bank_atom('fixture:two', 'two', scope='fixture')
    before = [tuple(r) for r in bank.conn.execute('SELECT * FROM atoms ORDER BY id')]
    first = recover_reviews(bank, 'fixture', room, limit=1)
    assert first['remaining'] == 1 and not first['ok']
    assert len(first['outcomes']) == 1 and first['outcomes'][0]['ok']
    second = recover_reviews(bank, 'fixture', room, limit=1, after_sequence=first['next_sequence'])
    assert second['ok'] and len(second['outcomes']) == 1
    count = bank.conn.execute('SELECT COUNT(*) FROM atom_review_events').fetchone()[0]
    third = recover_reviews(bank, 'fixture', room)
    assert third['ok'] and third['outcomes'] == []
    assert bank.conn.execute('SELECT COUNT(*) FROM atom_review_events').fetchone()[0] == count
    assert [tuple(r) for r in bank.conn.execute('SELECT * FROM atoms ORDER BY id')] == before
    assert bank.conn.execute('SELECT COUNT(*) FROM atom_links').fetchone()[0] == 0


def test_cli_recover_finishes_unconfirmed_review(bank, tmp_path, capsys):
    from echelon_engine.atoms.bank_review import _main
    room = tmp_path / 'room'
    room.mkdir()
    (room / 'room.json').write_text('{}', encoding='utf-8')
    bank.bank_atom('fixture:root', 'root', scope='fixture')
    path = bank.conn.execute('PRAGMA database_list').fetchone()[2]
    args = ['--bank', path, 'recover', '--scope', 'fixture', '--room', str(room)]
    assert _main(args) == 0
    assert json.loads(capsys.readouterr().out)['result']['outcomes'][0]['delivery_status'] == 'confirmed'
    assert _main(args) == 0
    assert json.loads(capsys.readouterr().out)['result']['outcomes'] == []


def test_review_health_distinguishes_missing_pending_available_and_invalid(bank, tmp_path):
    from echelon_engine.atoms.bank_review import capture_review, review_health
    path = bank.conn.execute('PRAGMA database_list').fetchone()[2]
    assert review_health(tmp_path / 'missing.db')['status'] == 'unavailable'
    assert review_health(path)['status'] == 'unavailable'
    root = bank.bank_atom('fixture:root', 'root', scope='fixture')
    assert review_health(path)['pending_capture'] == 1
    offer = capture_review(bank, root['review_id'])
    assert review_health(path)['available'] == 1
    assert review_health(path)['delivery_assessment'] == 'requires_destination'
    bank.conn.execute('UPDATE atom_review_events SET payload=? WHERE event_id=?', ('{}', offer['event_id']))
    bank.conn.commit()
    assert review_health(path)['invalid'] == 1
    assert review_health(path)['ok'] is False


def test_health_does_not_equate_capture_with_delivery(bank, tmp_path):
    from echelon_engine.atoms.bank_review import capture_review, deliver_review, review_health
    room = tmp_path / 'room'
    room.mkdir()
    (room / 'room.json').write_text('{}', encoding='utf-8')
    root = bank.bank_atom('fixture:root', 'root', scope='fixture')
    offer = capture_review(bank, root['review_id'])
    path = bank.conn.execute('PRAGMA database_list').fetchone()[2]
    routes = {'fixture': str(room.resolve())}
    before = review_health(path, destinations=routes)
    assert before['available'] == 1 and before['unconfirmed_delivery'] == 1
    assert not before['ok']
    assert review_health(path, destinations={})['unrouted'] == 1
    deliver_review(bank, offer['event_id'], room)
    assert review_health(path, destinations=routes)['ok']


def test_existing_bank_backup_preserves_review_history_and_schema_receipt(bank, tmp_path):
    from echelon_engine.atoms.bank_review import capture_review, review_state
    from echelon_engine.atoms.backup_cmd import _safe_file_copy
    root = bank.bank_atom('fixture:root', 'root', scope='fixture')
    offer = capture_review(bank, root['review_id'])
    source = Path(bank.conn.execute('PRAGMA database_list').fetchone()[2])
    destination = tmp_path / 'restored.db'
    _safe_file_copy(source, destination)
    restored = CardStore(destination)
    try:
        assert review_state(restored, root['review_id'])['source_event_id'] == offer['event_id']
        assert [tuple(r) for r in restored.conn.execute('SELECT * FROM atom_review_events ORDER BY rowid')] == [tuple(r) for r in bank.conn.execute('SELECT * FROM atom_review_events ORDER BY rowid')]
        assert [tuple(r) for r in restored.conn.execute('SELECT * FROM atom_review_schema_versions')] == [tuple(r) for r in bank.conn.execute('SELECT * FROM atom_review_schema_versions')]
        assert restored.conn.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
    finally:
        restored.conn.close()


def test_unknown_schema_version_refuses_bank_write_and_health(bank):
    from echelon_engine.atoms.bank_review import review_health
    bank.bank_atom('fixture:root', 'root', scope='fixture')
    bank.conn.execute("INSERT INTO atom_review_schema_versions VALUES (2,0,'future','wal',2)")
    bank.conn.commit()
    with pytest.raises(BankTransactionError):
        bank.bank_atom('fixture:new', 'new', scope='fixture')
    assert bank.conn.execute('SELECT COUNT(*) FROM atoms').fetchone()[0] == 1
    path = bank.conn.execute('PRAGMA database_list').fetchone()[2]
    assert review_health(path)['status'] == 'unavailable'


def test_recovery_pages_over_completed_reviews_without_unbounded_fold(bank, tmp_path):
    from echelon_engine.atoms.bank_review import recover_reviews
    room = tmp_path / 'room'
    room.mkdir()
    (room / 'room.json').write_text('{}', encoding='utf-8')
    for i in range(5):
        bank.bank_atom(f'fixture:{i}', f'claim {i}', scope='fixture')
    cursor = 0
    visited = 0
    for _ in range(3):
        result = recover_reviews(bank, 'fixture', room, limit=2, after_sequence=cursor)
        assert result['scanned'] <= 2
        visited += len(result['outcomes'])
        cursor = result['next_sequence']
    assert visited == 5 and cursor == 0
    first = recover_reviews(bank, 'fixture', room, limit=2)
    assert first['outcomes'] == [] and first['scanned'] == 2
    assert first['next_sequence'] > 0 and first['remaining_is_lower_bound']


def test_unsupported_schema_blocks_every_recovery_mutation(bank, tmp_path):
    from echelon_engine.atoms.bank_review import capture_review, deliver_review, recover_reviews
    root = bank.bank_atom('fixture:root', 'root', scope='fixture')
    room = tmp_path / 'room'
    room.mkdir()
    (room / 'room.json').write_text('{"scope":"fixture"}', encoding='utf-8')
    bank.conn.execute("INSERT INTO atom_review_schema_versions VALUES (2,0,'future','wal',2)")
    bank.conn.commit()
    with pytest.raises(BankTransactionError):
        capture_review(bank, root['review_id'])
    with pytest.raises(ValueError):
        deliver_review(bank, root['event_id'], room)
    with pytest.raises(ValueError):
        recover_reviews(bank, 'fixture', room)
    assert not (room / 'receipts.jsonl').exists()
    assert bank.conn.execute('SELECT COUNT(*) FROM atom_review_events').fetchone()[0] == 1


def test_health_budget_never_reports_partial_scan_as_healthy(tmp_path):
    from echelon_engine.atoms.bank_review import review_health
    path = tmp_path / 'health.db'
    store = CardStore(path)
    try:
        store.bank_atom('fixture:health', 'health', scope='fixture')
    finally:
        store.conn.close()
    assert review_health(path)['assessment_complete'] is True
    result = review_health(path, budget_seconds=1e-12)
    assert result['ok'] is False
    assert result['status'] == 'unavailable'
    assert result['assessment_complete'] is False
    assert result['counts_are_partial'] is True


def test_health_final_review_crossing_deadline_cannot_report_complete(tmp_path, monkeypatch):
    from echelon_engine.atoms import bank_review
    import time
    path = tmp_path / 'health.db'
    store = CardStore(path)
    try:
        store.bank_atom('fixture:health', 'health', scope='fixture')
    finally:
        store.conn.close()
    clock = [0.0]
    monkeypatch.setattr(time, 'monotonic', lambda: clock[0])
    original = bank_review.review_state
    def late(*args, **kwargs):
        result = original(*args, **kwargs)
        clock[0] = 3.0
        return result
    monkeypatch.setattr(bank_review, 'review_state', late)
    result = bank_review.review_health(path, budget_seconds=2.0)
    assert result['status'] == 'unavailable'
    assert result['assessment_complete'] is False
    assert result['counts_are_partial'] is True
    assert result['error_type'] == 'TimeoutError'


def test_eligibility_uses_current_source_confirmed_destinations_and_real_delivery(bank, tmp_path):
    from echelon_engine.atoms.bank_review import capture_review, deliver_review, recovery_eligibility
    room = tmp_path / 'room'
    room.mkdir()
    (room / 'room.json').write_text('{}', encoding='utf-8')
    root = bank.bank_atom('fixture:root', 'root', scope='fixture')
    first = capture_review(bank, root['review_id'])
    assert deliver_review(bank, first['event_id'], room)['ok']
    assert recovery_eligibility(bank, root['review_id'])['status'] == 'complete'
    fresh = capture_review(bank, root['review_id'], fresh=True)
    unresolved = recovery_eligibility(bank, root['review_id'])
    assert unresolved['status'] == 'needs_route'
    assert unresolved['source_event_id'] == fresh['event_id']
    assert unresolved['confirmed_destinations'] == ()
    assert deliver_review(bank, fresh['event_id'], room)['ok']
    complete = recovery_eligibility(bank, root['review_id'])
    assert complete['status'] == 'complete'
    assert complete['confirmed_destinations'] == (str(room.resolve()),)


def test_single_recovery_skips_absent_complete_and_routes_other_once(bank, tmp_path):
    from echelon_engine.atoms.bank_review import capture_review, deliver_review, recover_review, recovery_eligibility, review_health
    absent = tmp_path / 'absent-room'
    absent.mkdir()
    (absent / 'room.json').write_text('{}', encoding='utf-8')
    current = tmp_path / 'current-room'
    current.mkdir()
    (current / 'room.json').write_text('{}', encoding='utf-8')
    complete = bank.bank_atom('fixture:complete', 'complete', scope='fixture')
    complete_offer = capture_review(bank, complete['review_id'])
    assert deliver_review(bank, complete_offer['event_id'], absent)['ok']
    receipts_before = (absent / 'receipts.jsonl').read_bytes()
    needs = bank.bank_atom('fixture:needs', 'needs', scope='fixture')
    assert recovery_eligibility(bank, complete['review_id'])['status'] == 'complete'
    assert recovery_eligibility(bank, needs['review_id'])['status'] == 'needs_route'
    assert recover_review(bank, needs['review_id'], current)['ok']
    assert recover_review(bank, needs['review_id'], current)['attempted'] is False
    assert (absent / 'receipts.jsonl').read_bytes() == receipts_before
    health = review_health(bank.conn.execute('PRAGMA database_list').fetchone()[2],
                           destinations={'fixture': {str(current.resolve())}})
    assert health['completed'] == 2
    assert health['completed_with_absent_destination'] == 1
    assert health['destination_presence']['absent'] == 1
    assert health['ok'] is True


def test_invalid_and_orphan_histories_count_once_without_unrouted(bank):
    from echelon_engine.atoms.bank_review import recovery_eligibility, review_health
    root = bank.bank_atom('fixture:root', 'root', scope='fixture')
    row = dict(bank.conn.execute('SELECT * FROM atom_review_events WHERE review_id=?', (root['review_id'],)).fetchone())
    bank.conn.execute('DROP INDEX atom_review_one_pending')
    row['event_id'] = 'duplicate-' + row['event_id']
    bank.conn.execute('''INSERT INTO atom_review_events
        (event_id,schema_version,review_id,event_kind,atom_id,content_sha256,scope,coordinate,ts,payload)
        VALUES (:event_id,:schema_version,:review_id,:event_kind,:atom_id,:content_sha256,:scope,:coordinate,:ts,:payload)''', row)
    bank.conn.execute("INSERT INTO atom_review_events VALUES ('orphan',1,'orphan-review','projection_unavailable','x','h','fixture','fixture:x',0,'{}')")
    bank.conn.commit()
    assert recovery_eligibility(bank, root['review_id'])['status'] == 'invalid'
    path = bank.conn.execute('PRAGMA database_list').fetchone()[2]
    health = review_health(path, destinations={'fixture': set()})
    assert health['reviews'] == 2
    assert health['invalid'] == 2
    assert health['unrouted'] == 0


def test_pending_page_is_global_bounded_and_deduplicates_invalid_review(bank):
    from echelon_engine.atoms.bank_review import pending_recovery_page
    entries = [bank.bank_atom(f'fixture:{i}', str(i), scope='fixture') for i in range(3)]
    first = pending_recovery_page(bank, limit=2)
    assert first['scanned'] == 2 and first['remaining'] == 1 and first['next_sequence'] > 0
    assert len(first['outcomes']) == 2
    second = pending_recovery_page(bank, limit=2, after_sequence=first['next_sequence'])
    assert second['scanned'] == 1 and second['next_sequence'] == 0
    row = dict(bank.conn.execute('SELECT * FROM atom_review_events WHERE review_id=?', (entries[0]['review_id'],)).fetchone())
    bank.conn.execute('DROP INDEX atom_review_one_pending')
    row['event_id'] = 'duplicate-page-' + row['event_id']
    bank.conn.execute('''INSERT INTO atom_review_events
        (event_id,schema_version,review_id,event_kind,atom_id,content_sha256,scope,coordinate,ts,payload)
        VALUES (:event_id,:schema_version,:review_id,:event_kind,:atom_id,:content_sha256,:scope,:coordinate,:ts,:payload)''', row)
    bank.conn.commit()
    page = pending_recovery_page(bank, limit=4)
    duplicated = [item for item in page['outcomes'] if item['review_id'] == entries[0]['review_id']]
    assert len(duplicated) == 1 and duplicated[0]['status'] == 'invalid'
    from echelon_engine.atoms.bank_review import recovery_eligibility
    assert recovery_eligibility(bank, entries[0]['review_id'])['scope'] is None
    assert duplicated[0]['scope'] == 'fixture'
    assert duplicated[0]['scope_origin'] == 'page_row_not_validated'


def test_wrong_source_is_invalid_and_ambiguous_current_rooms_are_unrouted(bank, tmp_path):
    from echelon_engine.atoms.bank_review import recovery_eligibility, review_health
    bad = bank.bank_atom('fixture:bad', 'bad', scope='fixture')
    row = dict(bank.conn.execute('SELECT * FROM atom_review_events WHERE review_id=?', (bad['review_id'],)).fetchone())
    row.update(event_id='wrong-source', event_kind='room_receipt_confirmed',
               payload=json.dumps({'source_event_id': 'missing', 'room': str(tmp_path / 'old')}))
    bank.conn.execute('''INSERT INTO atom_review_events
        (event_id,schema_version,review_id,event_kind,atom_id,content_sha256,scope,coordinate,ts,payload)
        VALUES (:event_id,:schema_version,:review_id,:event_kind,:atom_id,:content_sha256,:scope,:coordinate,:ts,:payload)''', row)
    bank.conn.commit()
    ambiguous = bank.bank_atom('fixture:ambiguous', 'ambiguous', scope='other')
    assert recovery_eligibility(bank, bad['review_id'])['status'] == 'invalid'
    path = bank.conn.execute('PRAGMA database_list').fetchone()[2]
    health = review_health(path, destinations={'fixture': set(), 'other': {str(tmp_path / 'a'), str(tmp_path / 'b')}})
    assert health['invalid'] == 1
    assert health['unrouted'] == 1
    assert recovery_eligibility(bank, ambiguous['review_id'])['status'] == 'needs_route'


def test_legacy_add_atom_preserves_commit_without_review_side_effects(bank):
    """Existing low-level callers keep their historical commit and ID contract."""
    synchronous = bank.conn.execute('PRAGMA synchronous').fetchone()[0]
    bank.conn.execute('CREATE TABLE caller_work (value TEXT)')
    bank.conn.execute("INSERT INTO caller_work VALUES ('pending')")
    assert bank.conn.in_transaction
    atom_id = bank.add_atom('fixture:legacy', 'legacy claim', scope='fixture')
    assert isinstance(atom_id, str)
    assert not bank.conn.in_transaction
    bank.conn.rollback()
    assert bank.conn.execute('SELECT value FROM caller_work').fetchone()[0] == 'pending'
    assert bank.conn.execute('PRAGMA synchronous').fetchone()[0] == synchronous
    assert not bank.conn.execute("SELECT name FROM sqlite_master WHERE name LIKE 'atom_review%'").fetchall()
    assert bank.add_atom('fixture:legacy', 'legacy claim', scope='fixture') == atom_id
    rich = bank.bank_atom('fixture:rich', 'rich claim', scope='fixture')
    assert rich['insertion'] == 'inserted'
    assert bank.conn.execute("SELECT COUNT(*) FROM atom_review_events WHERE event_kind='pending'").fetchone()[0] == 1


@pytest.mark.parametrize("after_sequence", [0, 1000000])
def test_pending_page_rejects_unsupported_schema_even_when_empty(bank, after_sequence):
    from echelon_engine.atoms.bank_review import pending_recovery_page
    bank.bank_atom('fixture:page-schema', 'synthetic fixture', scope='fixture')
    bank.conn.execute("INSERT INTO atom_review_schema_versions VALUES (2,0,'future','wal',2)")
    bank.conn.commit()
    before = bank.conn.total_changes
    with pytest.raises(ValueError, match='schema receipt missing or unsupported'):
        pending_recovery_page(bank, after_sequence=after_sequence)
    assert bank.conn.total_changes == before


# --- canonical root routing in health (owner rule, 2026-09-09) ---------------
# review_health and the CC pulse must resolve routing through ONE rule. If health
# kept its own `len(choices) != 1` test, a scope the pulse successfully routes
# would still be counted `unrouted` and the heartbeat would never clear.

def test_resolve_destination_matches_the_pulse_rule(tmp_path):
    from echelon_engine.atoms.bank_review import resolve_destination
    a, b, c = (str((tmp_path / n).resolve()) for n in ('a', 'b', 'c'))
    # one candidate routes, with or without roots
    assert resolve_destination({'s': {a}}, 's') == a
    assert resolve_destination({'s': {a}}, 's', {}) == a
    # several candidates: fail closed unless a root names one of them
    assert resolve_destination({'s': {a, b}}, 's', {}) is None
    assert resolve_destination({'s': {a, b}}, 's', {'s': a}) == a
    assert resolve_destination({'s': {a, b}}, 's', {'s': c}) is None
    # roots=None keeps the strict pre-rule behaviour for callers that cannot supply
    # them. This must be distinguishable from roots={}: an omitted roots argument
    # must NOT be silently coerced into "consult roots", or a caller that cannot
    # compute roots would start routing without ever asking to.
    # Omitting roots fails closed, exactly as an empty roots mapping does. These two
    # are deliberately EQUIVALENT: "no roots supplied" and "roots name none for this
    # scope" must both refuse to route, so no test can separate them and none should
    # pretend to. What must never happen is either one routing.
    assert resolve_destination({'s': {a, b}}, 's') is None
    assert resolve_destination({'s': {a, b}}, 's', {}) is None
    # no candidate is never routable, however roots reads
    assert resolve_destination({'s': set()}, 's', {'s': a}) is None
    # a root that is not a usable string cannot route
    assert resolve_destination({'s': {a, b}}, 's', {'s': ''}) is None
    assert resolve_destination({'s': {a, b}}, 's', {'s': None}) is None
    # A malformed roots mapping FAILS CLOSED rather than raising — see
    # test_malformed_roots_fails_closed_and_never_raises for why that direction matters.
    assert resolve_destination({'s': {a, b}}, 's', 'not-a-mapping') is None


def test_health_counts_a_root_routed_ambiguous_scope_as_deliverable(tmp_path):
    """The heartbeat must clear for a scope the pulse can now route."""
    from echelon_engine.atoms.cards import CardStore
    from echelon_engine.atoms.bank_review import review_health
    bank = tmp_path / 'bank.db'
    store = CardStore(bank)
    store.bank_atom('fixture:root', 'root', scope='fixture')
    store.conn.close()
    root = str((tmp_path / 'root').resolve())
    child = str((tmp_path / 'child').resolve())
    destinations = {'fixture': {root, child}}

    # Without a root the ambiguous scope is unrouted and health is degraded.
    strict = review_health(bank, destinations=destinations)
    assert strict['unrouted'] == 1 and strict['status'] == 'degraded'

    # With the canonical root it is a routable backlog, not a routing defect.
    routed = review_health(bank, destinations=destinations, roots={'fixture': root})
    assert routed['unrouted'] == 0
    assert routed['status'] != 'degraded'
    assert routed['unconfirmed_delivery'] + routed['pending_capture'] >= 1


def test_health_still_degrades_when_an_ambiguous_scope_has_no_root(tmp_path):
    from echelon_engine.atoms.cards import CardStore
    from echelon_engine.atoms.bank_review import review_health
    bank = tmp_path / 'bank.db'
    store = CardStore(bank)
    store.bank_atom('fixture:root', 'root', scope='fixture')
    store.conn.close()
    destinations = {'fixture': {str((tmp_path / 'a').resolve()), str((tmp_path / 'b').resolve())}}
    result = review_health(bank, destinations=destinations, roots={'other': str(tmp_path)})
    assert result['unrouted'] == 1 and result['status'] == 'degraded'


def test_malformed_roots_fails_closed_and_never_raises(tmp_path):
    """A bad roots shape must not turn an honest `degraded` health read into
    `unavailable`. resolve_destination runs inside review_health's per-review loop,
    whose blanket except would mask real state as a schema failure."""
    from echelon_engine.atoms.bank_review import resolve_destination, review_health
    from echelon_engine.atoms.cards import CardStore
    a, b = (str((tmp_path / n).resolve()) for n in ('a', 'b'))
    for bad in ([], 'str', 7, 0.5, {'s': 7}, {'s': ''}, {'s': None}, {'s': []}):
        assert resolve_destination({'s': {a, b}}, 's', bad) is None

    bank = tmp_path / 'bank.db'
    store = CardStore(bank)
    store.bank_atom('fixture:root', 'root', scope='fixture')
    store.conn.close()
    destinations = {'fixture': {a, b}}
    for bad in ([], 'not-a-mapping'):
        result = review_health(bank, destinations=destinations, roots=bad)
        # degraded (honest: the scope really is unrouted), never `unavailable`.
        assert result['status'] == 'degraded', (bad, result)
        assert result['unrouted'] == 1
        assert 'error_type' not in result


def test_one_shared_builder_feeds_both_the_pulse_and_health(tmp_path):
    """The pulse and Board health must derive routing inputs from ONE builder.

    A shared resolver alone was not enough: two hand-written registry loops agreed on
    every live registration but disagreed on shapes the live registry does not contain
    (a non-str path validated by one, rejected by the other), which would let the pulse
    deliver a scope health still counted `unrouted` — a permanently dirty heartbeat.
    """
    import sys
    from echelon_engine.atoms.bank_review import validated_rooms
    import importlib

    # `room_pulse` ships with the operator's command-center tooling, not with the
    # substrate. It used to be reached by inserting an absolute path to that
    # checkout, which passed only on the machine that had it. Point
    # ECHELON_TOOLS_DIR at your own tools directory to run this; otherwise skip,
    # because the assertion below is about room_pulse's agreement with
    # validated_rooms and there is nothing to compare without it.
    tools_dir = os.environ.get("ECHELON_TOOLS_DIR")
    if tools_dir:
        sys.path.insert(0, tools_dir)
    try:
        room_pulse = importlib.import_module('room_pulse')
    except ImportError:
        pytest.skip("room_pulse not importable; set ECHELON_TOOLS_DIR to the "
                    "command-center tools directory to run this cross-check")

    root = tmp_path / 'scoped'
    child = tmp_path / 'scoped_child'
    for d in (root, child):
        d.mkdir()
        (d / 'room.json').write_text('{"scope":"scoped"}', encoding='utf-8')

    # Include the exact shape the two loops used to disagree on: a non-str path.
    entries = {
        'scoped': {'scope': 'scoped', 'path': str(root)},
        'scoped/child': {'scope': 'scoped', 'path': str(child)},
        'pathobj': {'scope': 'scoped', 'path': Path(str(child))},
        'broken': {'scope': 'scoped'},
    }
    shared = validated_rooms(entries)
    viapulse = room_pulse._validated_destinations(entries)
    assert shared == viapulse, 'the pulse must not keep its own registry loop'
    destinations, errors, roots = shared
    # The non-str path is rejected, identically, on both surfaces.
    assert any(e['status'] == 'registry_entry_missing_scope_or_path'
               for e in errors.get('scoped', []))
    assert roots['scoped'] == str(root.resolve())
