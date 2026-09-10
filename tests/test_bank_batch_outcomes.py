import pytest

from echelon_engine.atoms.store import SeedStore, BatchRememberError


@pytest.mark.parametrize('second_scope,second_coordinate', [
    ('other', 'shared:first'),
    ('fixture', 'shared:second'),
])
def test_content_id_collision_cannot_claim_wrong_bank_location(tmp_path, second_scope, second_coordinate):
    store = SeedStore(tmp_path / 'legacy.db', v2_db=tmp_path / 'primary.db')
    try:
        store.remember('fixture', 'identical content', coordinate='shared:first')
        first = store.last_batch_report[0]['atom_id']
        with pytest.raises(BatchRememberError) as caught:
            store.remember(second_scope, 'identical content', coordinate=second_coordinate)
        assert caught.value.report[0]['write_status'] == 'conflict'
        assert caught.value.report[0]['link_status'] == 'not_attempted'
        actual = store.cards.get_atom(first)
        assert (actual.scope, actual.coordinate) == ('fixture', 'shared:first')
        assert store.cards.conn.execute('SELECT COUNT(*) FROM atoms').fetchone()[0] == 1
    finally:
        store.cards.conn.close()
        store.u.conn.close()


def test_single_write_failure_cannot_return_success_id(tmp_path, monkeypatch):
    store = SeedStore(tmp_path / 'legacy.db', v2_db=tmp_path / 'primary.db')
    try:
        def fail(*args, **kwargs):
            raise OSError('private storage detail')
        monkeypatch.setattr(store.cards, 'bank_atom', fail)
        with pytest.raises(BatchRememberError) as caught:
            store.remember('fixture', 'new claim', coordinate='fixture:new')
        assert caught.value.report[0]['write_status'] == 'unknown'
        assert store.cards.count_atoms_in_scope('fixture') == 0
        assert 'private storage detail' not in str(caught.value.report)
    finally:
        store.cards.conn.close()
        store.u.conn.close()


def test_colliding_batch_entry_does_not_erase_prior_link_outcome(tmp_path):
    store = SeedStore(tmp_path / 'legacy.db', v2_db=tmp_path / 'primary.db')
    try:
        store.remember('fixture', 'neighbor', coordinate='shared:neighbor')
        with pytest.raises(BatchRememberError) as caught:
            store.remember_many([
                {'scope': 'fixture', 'coordinate': 'shared:first', 'content': 'claim [[neighbor]]'},
                {'scope': 'fixture', 'coordinate': 'shared:second', 'content': 'claim [[neighbor]]'},
            ])
        first, second = caught.value.report
        assert first['seed_id'] == second['seed_id']
        assert [first['input_index'], second['input_index']] == [0, 1]
        assert first['write_status'] == 'committed'
        assert first['link_status'] == 'complete'
        assert len(store.cards.edges_of(first['atom_id'])) == 1
        assert second['write_status'] == 'conflict'
        assert second['link_status'] == 'not_attempted'
    finally:
        store.cards.conn.close()
        store.u.conn.close()


@pytest.mark.parametrize('failure_at', ['link', 'resolve'])
def test_link_failure_is_partial_without_losing_committed_atoms(tmp_path, monkeypatch, failure_at):
    store = SeedStore(tmp_path / 'legacy.db', v2_db=tmp_path / 'primary.db')
    try:
        def fail(*args, **kwargs):
            raise OSError('link storage unavailable')
        if failure_at == 'link':
            monkeypatch.setattr(store.cards, 'link', fail)
        else:
            monkeypatch.setattr(store, '_v2_ref_target', fail)
        with pytest.raises(BatchRememberError) as caught:
            store.remember_many([
                {'scope': 'fixture', 'coordinate': 'fixture:first', 'content': 'first [[second]]'},
                {'scope': 'fixture', 'coordinate': 'fixture:second', 'content': 'second'},
            ])
        assert all(r['write_status'] == 'committed' for r in caught.value.report)
        assert caught.value.report[0]['link_status'] == 'partial'
        assert caught.value.report[0]['link_error_type'] == 'OSError'
        assert store.last_batch_report == caught.value.report
        assert store.cards.count_atoms_in_scope('fixture') == 2
    finally:
        store.cards.conn.close()
        store.u.conn.close()


def test_primary_failure_cannot_return_success_ids(tmp_path, monkeypatch):
    store = SeedStore(tmp_path / 'legacy.db', v2_db=tmp_path / 'primary.db')
    try:
        def fail(*args, **kwargs):
            raise OSError('private storage detail')
        monkeypatch.setattr(store.cards, 'bank_atom', fail)
        with pytest.raises(BatchRememberError) as caught:
            store.remember_many([{'scope': 'fixture', 'coordinate': 'fixture:new', 'content': 'new claim'}])
        assert caught.value.report[0]['write_status'] == 'unknown'
        assert store.cards.count_atoms_in_scope('fixture') == 0
        assert 'private storage detail' not in str(caught.value.report)
    finally:
        store.cards.conn.close()
        store.u.conn.close()


def test_partial_batch_reports_committed_entry_without_erasing_it(tmp_path, monkeypatch):
    store = SeedStore(tmp_path / 'legacy.db', v2_db=tmp_path / 'primary.db')
    original = store.cards.bank_atom
    try:
        def fail_second(coordinate, *args, **kwargs):
            if coordinate.endswith(':second'):
                raise OSError('disk failure')
            return original(coordinate, *args, **kwargs)
        monkeypatch.setattr(store.cards, 'bank_atom', fail_second)
        with pytest.raises(BatchRememberError) as caught:
            store.remember_many([{'scope': 'fixture', 'coordinate': 'fixture:' + name,
                                  'content': name} for name in ('first', 'second')])
        assert [r['write_status'] for r in caught.value.report] == ['committed', 'unknown']
        assert store.cards.get_atom(caught.value.report[0]['atom_id']).content == 'first'
    finally:
        store.cards.conn.close()
        store.u.conn.close()


def test_batch_exposes_durable_review_identity_and_unchanged_insertion(tmp_path):
    store = SeedStore(tmp_path / 'legacy.db', v2_db=tmp_path / 'primary.db')
    try:
        first_id = store.remember('fixture', 'review this claim', coordinate='fixture:review')
        first = dict(store.last_batch_report[0])
        second_id = store.remember('fixture', 'review this claim', coordinate='fixture:review')
        second = store.last_batch_report[0]
        assert first_id == second_id
        assert first['insertion'] == 'inserted'
        assert second['insertion'] == 'unchanged'
        assert first['review_id'] == second['review_id']
        assert first['event_id'] == second['event_id']
        assert len(first['content_sha256']) == 64
        assert store.cards.conn.execute('SELECT COUNT(*) FROM atom_review_events').fetchone()[0] == 2
    finally:
        store.cards.conn.close()
        store.u.conn.close()


def test_batch_offer_contains_in_batch_links_and_survives_review_failure(tmp_path, monkeypatch):
    from echelon_engine.atoms import bank_review
    store = SeedStore(tmp_path / 'legacy.db', v2_db=tmp_path / 'primary.db')
    try:
        store.remember_many([
            {'scope': 'fixture', 'coordinate': 'fixture:first', 'content': 'first [[second]]'},
            {'scope': 'fixture', 'coordinate': 'fixture:second', 'content': 'second'},
        ])
        first, second = store.last_batch_report
        assert first['review']['edges'][0]['neighbor_id'] == second['atom_id']
        assert second['review']['edges'][0]['direction'] == 'in'
        def fail(*args, **kwargs):
            raise OSError('private detail')
        monkeypatch.setattr(bank_review, 'capture_review', fail)
        with pytest.raises(BatchRememberError) as caught:
            store.remember('fixture', 'third', coordinate='fixture:third')
        result = caught.value.report[0]
        assert result['write_status'] == 'committed'
        assert result['review_status'] == 'unavailable'
        assert result['review_error_type'] == 'OSError'
        assert 'private detail' not in str(result)
        assert store.cards.get_atom(result['atom_id']).content == 'third'
        assert store.cards.conn.execute("SELECT COUNT(*) FROM atom_review_events WHERE review_id=? AND event_kind='pending'", (result['review_id'],)).fetchone()[0] == 1
    finally:
        store.cards.conn.close()
        store.u.conn.close()


def test_reference_outcomes_distinguish_missing_foreign_and_retired(tmp_path):
    from echelon_engine.atoms.bank_review import review_event
    store = SeedStore(tmp_path / 'legacy.db', v2_db=tmp_path / 'primary.db')
    try:
        store.remember('foreign', 'foreign memory', coordinate='foreign:elsewhere')
        store.remember('fixture', 'local memory', coordinate='fixture:local')
        content = 'claim [[local]] [[elsewhere]] [[missing]]'
        store.remember('fixture', content, coordinate='fixture:claim')
        result = store.last_batch_report[0]
        assert result['link_status'] == 'complete_with_unlinked'
        assert [r['status'] for r in result['references']] == ['linked', 'foreign_scope_skipped', 'unresolved']
        assert result['reference_counts']['linked'] == 1
        assert result['reference_counts']['unresolved'] == 1
        persisted = review_event(store.cards, result['review_event_id'])
        assert persisted['payload']['references'] == result['references']
        local = result['references'][0]['target_id']
        store.cards.unlink(result['atom_id'], local, 'refs')
        store.remember('fixture', content, coordinate='fixture:claim')
        repeated = store.last_batch_report[0]
        assert repeated['references'][0]['status'] == 'retired'
        assert repeated['review_event_id'] == result['review_event_id']
        assert repeated['review']['references'][0]['status'] == 'linked'
        assert store.cards.edges_of(result['atom_id']) == []
    finally:
        store.cards.conn.close()
        store.u.conn.close()
