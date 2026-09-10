"""Focused unit tests for echelon_engine.atoms.ingest — plant in-repo memory/*.md.

Covers three surfaces:
  - parse_atom(): the no-YAML frontmatter splitter (flat keys + nested metadata.type).
  - resolve_mem_dirs()/_config_dirs(): the --root-vs-config dir resolution + dedup.
  - ingest_folder(): end-to-end plant of a tmp memory dir into an isolated store,
    incl. the scaffolding-skip rules (MEMORY.md, _underscore, no-frontmatter).

ISOLATION NOTE / MIGRATION FINDING: ingest_folder() constructs `SeedStore(db_path)`
WITHOUT threading a `v2_db`, so the v2 (PRIMARY) writes go to the GLOBAL soul bank
even when db_path is a scratch file (same gap store.py documents for its own
constructor). To test without polluting the real bank we monkeypatch the module-level
DEFAULT_V2_DB to tmp_path. This is a real isolation seam ingest_folder does not yet
expose — reported, not worked around in code.
"""
import json

import pytest

from echelon_engine.atoms import ingest
from echelon_engine.atoms import store as store_mod


@pytest.fixture
def isolated_v2(tmp_path, monkeypatch):
    # redirect the global v2 bank so ingest_folder's hardwired primary store hits scratch
    monkeypatch.setattr(store_mod, "DEFAULT_V2_DB", tmp_path / "core_v2.db")
    monkeypatch.setattr(store_mod, "DEFAULT_DB", tmp_path / "core.db")
    # tmp_path leaf ≠ the test's hardcoded scope — the canonicalization gate would
    # refuse. These tests verify ingest mechanics, not the gate; the gate's own tests
    # are in test_refill_guards.py. (2026-07-09, slice-1.5 hygiene refill guards)
    monkeypatch.setenv("ECHELON_INGEST_SCOPE", "1")
    return tmp_path


# ── parse_atom ────────────────────────────────────────────────────────────
def test_parse_atom_reads_flat_and_nested_keys():
    text = ('---\n'
            'name: my-atom\n'
            'description: a one-line handle\n'
            'metadata:\n'
            '  type: project\n'
            '---\n'
            'the body lesson here')
    fm, body = parse = ingest.parse_atom(text)
    assert fm["name"] == "my-atom"
    assert fm["description"] == "a one-line handle"
    assert fm["metadata.type"] == "project"
    assert body == "the body lesson here"


def test_parse_atom_no_frontmatter_returns_whole_body():
    fm, body = ingest.parse_atom("just a body, no dashes")
    assert fm == {}
    assert body == "just a body, no dashes"


# ── config / dir resolution ───────────────────────────────────────────────
def test_resolve_mem_dirs_root_overrides_and_dedups(tmp_path):
    out = ingest.resolve_mem_dirs("scope", [str(tmp_path), str(tmp_path)])
    assert out == [str(tmp_path)]  # explicit root, deduped


def test_config_dirs_reads_scope_entry(tmp_path):
    cfg = tmp_path / "mem_dirs.json"
    cfg.write_text(json.dumps({"myscope": ["dirA", "dirB"]}), encoding="utf-8")
    dirs = ingest._config_dirs("myscope", config_path=str(cfg))
    assert [p.split("\\")[-1].split("/")[-1] for p in dirs] == ["dirA", "dirB"]


def test_resolve_mem_dirs_falls_back_to_config(tmp_path):
    cfg = tmp_path / "mem_dirs.json"
    cfg.write_text(json.dumps({"s": "only_dir"}), encoding="utf-8")
    out = ingest.resolve_mem_dirs("s", None, config_path=str(cfg))
    assert out and out[0].endswith("only_dir")


def test_config_dirs_missing_file_is_empty(tmp_path):
    assert ingest._config_dirs("x", config_path=str(tmp_path / "nope.json")) == []


def test_ingest_relative_memory_uses_declared_room_scope(tmp_path, monkeypatch):
    project = tmp_path / 'echelon' / 'nested-project'
    memory = project / 'memory'
    memory.mkdir(parents=True)
    room = project / '.echelon'
    room.mkdir()
    (room / 'room.json').write_text(json.dumps({'scope': 'declared-bank'}), encoding='utf-8')
    monkeypatch.chdir(project)
    monkeypatch.delenv('ECHELON_INGEST_SCOPE', raising=False)
    class ReachedStore(Exception): pass
    monkeypatch.setattr(ingest, 'SeedStore', lambda *a, **k: (_ for _ in ()).throw(ReachedStore()))
    with pytest.raises(ReachedStore):
        ingest.ingest_folder('memory', 'declared-bank')
    with pytest.raises(ValueError, match='disagrees with the canonical scope'):
        ingest.ingest_folder('memory', 'echelon')
    (room / 'room.json').write_text('{}', encoding='utf-8')
    with pytest.raises(ValueError, match='no valid declared scope'):
        ingest.ingest_folder('memory', 'declared-bank')


# ── ingest_folder end-to-end ──────────────────────────────────────────────
def _write_atom(d, fname, name, desc, body):
    (d / fname).write_text(
        f"---\nname: {name}\ndescription: {desc}\nmetadata:\n  type: project\n---\n{body}",
        encoding="utf-8")


def test_ingest_folder_plants_atoms_and_skips_scaffolding(isolated_v2):
    mem = isolated_v2 / "memory"
    mem.mkdir()
    _write_atom(mem, "real-one.md", "real-one", "first handle", "lesson one")
    _write_atom(mem, "real-two.md", "real-two", "second handle", "lesson two")
    # scaffolding that must be skipped:
    _write_atom(mem, "MEMORY.md", "index", "the index", "should be skipped")
    _write_atom(mem, "_template.md", "tmpl", "underscore meta", "should be skipped")
    (mem / "no-fm.md").write_text("a plain file with no frontmatter", encoding="utf-8")

    planted = ingest.ingest_folder(isolated_v2, "test-ingest",
                                   db_path=isolated_v2 / "core.db", verbose=False)
    names = sorted(n for n, _ in planted)
    assert names == ["real-one", "real-two"]
    assert all(seed_id for _, seed_id in planted)


def test_ingest_folder_is_idempotent(isolated_v2):
    mem = isolated_v2 / "memory"
    mem.mkdir()
    _write_atom(mem, "a.md", "a", "handle", "lesson body content")
    p1 = ingest.ingest_folder(isolated_v2, "test-idem",
                              db_path=isolated_v2 / "core.db", verbose=False)
    p2 = ingest.ingest_folder(isolated_v2, "test-idem",
                              db_path=isolated_v2 / "core.db", verbose=False)
    # content-addressed: same names + same ids both runs
    assert p1 == p2


@pytest.mark.parametrize('failure_stage', ['review', 'compile'])
def test_ingest_partial_failure_retains_commit_and_reports_compilation(isolated_v2, monkeypatch, failure_stage):
    import sqlite3
    from echelon_engine.atoms import bank_review
    from echelon_engine.atoms.cards import CardStore
    from echelon_engine.atoms.store import BatchRememberError
    mem = isolated_v2 / 'memory'
    mem.mkdir()
    _write_atom(mem, 'claim.md', 'claim', 'handle', 'A durable lesson with explanation.')
    def fail(*args, **kwargs):
        raise OSError('private storage detail')
    if failure_stage == 'review':
        monkeypatch.setattr(bank_review, 'capture_review', fail)
    else:
        monkeypatch.setattr(CardStore, 'compile_atom_struct', fail)
    with pytest.raises(BatchRememberError) as caught:
        ingest.ingest_folder(isolated_v2, 'test-ingest', db_path=isolated_v2 / 'core.db', verbose=False)
    error = caught.value
    assert len(error.committed) == 1
    assert error.report[0]['write_status'] == 'committed'
    observer = sqlite3.connect(isolated_v2 / 'core_v2.db')
    try:
        assert observer.execute('SELECT COUNT(*) FROM atoms').fetchone()[0] == 1
        assert observer.execute('SELECT COUNT(*) FROM atom_spine').fetchone()[0] == (1 if failure_stage == 'review' else 0)
    finally:
        observer.close()
    assert bool(error.compilation_errors) == (failure_stage == 'compile')
    assert 'private storage detail' not in str(error.compilation_errors)


def test_ingest_changed_version_counts_as_new(isolated_v2, capsys):
    mem = isolated_v2 / 'memory'
    mem.mkdir()
    _write_atom(mem, 'claim.md', 'claim', 'handle', 'First version.')
    ingest.ingest_folder(isolated_v2, 'test-ingest', db_path=isolated_v2 / 'core.db')
    capsys.readouterr()
    _write_atom(mem, 'claim.md', 'claim', 'handle', 'Second version.')
    ingest.ingest_folder(isolated_v2, 'test-ingest', db_path=isolated_v2 / 'core.db')
    assert '1 new, 0 unchanged' in capsys.readouterr().out
    ingest.ingest_folder(isolated_v2, 'test-ingest', db_path=isolated_v2 / 'core.db')
    assert '0 new, 1 unchanged' in capsys.readouterr().out


def test_ingest_automatically_delivers_to_project_room(isolated_v2):
    import sqlite3
    mem = isolated_v2 / 'memory'
    mem.mkdir()
    room = isolated_v2 / '.echelon'
    room.mkdir()
    (room / 'room.json').write_text('{"scope":"test-ingest"}', encoding='utf-8')
    _write_atom(mem, 'claim.md', 'claim', 'handle', 'A banked claim.')
    ingest.ingest_folder(isolated_v2, 'test-ingest', db_path=isolated_v2 / 'core.db', verbose=False)
    rows = (room / 'receipts.jsonl').read_text(encoding='utf-8').splitlines()
    assert len(rows) == 1 and json.loads(rows[0])['origin'] == 'bank-review'
    observer = sqlite3.connect(isolated_v2 / 'core_v2.db')
    try:
        assert observer.execute("SELECT COUNT(*) FROM atom_review_events WHERE event_kind='room_receipt_confirmed'").fetchone()[0] == 1
        assert observer.execute('SELECT COUNT(*) FROM atom_spine').fetchone()[0] == 1
    finally:
        observer.close()


def test_ingest_failed_room_delivery_preserves_compiled_atom_and_pending_confirmation(isolated_v2):
    import sqlite3
    from echelon_engine.atoms.store import BatchRememberError
    mem = isolated_v2 / 'memory'
    mem.mkdir()
    room = isolated_v2 / '.echelon'
    room.mkdir()
    (room / 'room.json').write_text('{"scope":"test-ingest"}', encoding='utf-8')
    receipt_path = room / 'receipts.jsonl'
    damaged = b'{incomplete'
    receipt_path.write_bytes(damaged)
    _write_atom(mem, 'claim.md', 'claim', 'handle', 'A durable claim.')
    with pytest.raises(BatchRememberError) as caught:
        ingest.ingest_folder(isolated_v2, 'test-ingest', db_path=isolated_v2 / 'core.db', verbose=False)
    result = caught.value
    assert len(result.committed) == 1
    assert result.compilation_errors == []
    assert result.delivery_errors[0]['error_type'] == 'ReceiptIncompleteRecord'
    assert result.report[0]['write_status'] == 'committed'
    assert result.report[0]['delivery_status'] == 'unconfirmed'
    assert receipt_path.read_bytes() == damaged
    observer = sqlite3.connect(isolated_v2 / 'core_v2.db')
    try:
        assert observer.execute('SELECT COUNT(*) FROM atom_spine').fetchone()[0] == 1
        assert observer.execute("SELECT COUNT(*) FROM atom_review_events WHERE event_kind='projection_available'").fetchone()[0] == 1
        assert observer.execute("SELECT COUNT(*) FROM atom_review_events WHERE event_kind='room_receipt_confirmed'").fetchone()[0] == 0
    finally:
        observer.close()


def test_ingest_scope_override_cannot_deliver_to_different_scope_room(isolated_v2):
    import sqlite3
    from echelon_engine.atoms.store import BatchRememberError
    mem = isolated_v2 / 'memory'
    mem.mkdir()
    room = isolated_v2 / '.echelon'
    room.mkdir()
    (room / 'room.json').write_text('{"scope":"other-scope"}', encoding='utf-8')
    _write_atom(mem, 'claim.md', 'claim', 'handle', 'A claim for the requested scope.')
    with pytest.raises(BatchRememberError) as caught:
        ingest.ingest_folder(isolated_v2, 'test-ingest', db_path=isolated_v2 / 'core.db', verbose=False)
    assert caught.value.delivery_errors[0]['error_type'] == 'ValueError'
    error = caught.value.delivery_errors[0]
    assert error['event_id'] and error['review_id']
    assert error['delivery_status'] == 'unconfirmed'
    assert not (room / 'receipts.jsonl').exists()
    observer = sqlite3.connect(isolated_v2 / 'core_v2.db')
    try:
        assert observer.execute('SELECT scope FROM atoms').fetchone()[0] == 'test-ingest'
        assert observer.execute('SELECT COUNT(*) FROM atom_spine').fetchone()[0] == 1
    finally:
        observer.close()
