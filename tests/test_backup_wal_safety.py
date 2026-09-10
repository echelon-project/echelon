"""Backup must be WAL-safe — a plain file copy of a live bank saves NOTHING.

THE TRAP (measured 2026-08-02, found by the two-way sync round trip): the bank runs in WAL
mode (cards.py sets PRAGMA journal_mode=WAL). While a process holds it open, the .db file can
be a 4KB stub with every byte of real data sitting in the sibling -wal. A `shutil.copy2` of
just the .db then produces a file that is not merely stale — it has NO TABLES AT ALL
("no such table: atoms").

It looks fine whenever the source happens to be checkpointed (a clean close), which is exactly
NOT the case when another process holds the bank open — i.e. precisely when you need a backup.

These tests pin the real failure mode: write through the real doors, do NOT close the store,
then take a backup and read it back.
"""
import sqlite3

import pytest

from echelon_engine.atoms.cards import CardStore
from echelon_engine.atoms import backup_cmd


@pytest.fixture
def live_bank(tmp_path):
    """An OPEN bank with uncheckpointed writes in the WAL — the dangerous state."""
    db = tmp_path / "echelon.db"
    store = CardStore(db)
    ids = []
    for i in range(5):
        aid = store.add_atom(f"lesson:wal{i}", f"# wal{i}\nclaim {i}", scope="waltest")
        store.compile_atom_struct(aid)
        store.remember_fetch(aid, depth="spine")
        ids.append(aid)
    return store, db, ids


def test_the_trap_is_real_plain_copy_loses_everything(live_bank, tmp_path):
    """Guard the PREMISE: if this ever stops failing, the WAL hazard changed and the rest of
    this file's reasoning needs revisiting."""
    import shutil
    store, db, _ = live_bank
    assert (tmp_path / "echelon.db-wal").exists(), "expected an uncheckpointed WAL"
    stub = tmp_path / "plain_copy.db"
    shutil.copy2(db, stub)
    with pytest.raises(sqlite3.OperationalError, match="no such table"):
        sqlite3.connect(f"file:{stub}?mode=ro", uri=True).execute(
            "SELECT COUNT(*) FROM atoms").fetchone()


def test_safe_copy_captures_uncheckpointed_writes(live_bank, tmp_path):
    store, db, ids = live_bank
    snap = tmp_path / "safe.db"
    backup_cmd._safe_file_copy(db, snap)
    c = sqlite3.connect(f"file:{snap}?mode=ro", uri=True)
    assert c.execute("SELECT COUNT(*) FROM atoms").fetchone()[0] == 5
    assert c.execute("SELECT COUNT(*) FROM atom_earned").fetchone()[0] == 5
    # the earned weight — the part that cannot be re-earned by re-ingest — must be in there
    uc = c.execute("SELECT SUM(use_count) FROM atom_earned").fetchone()[0]
    assert uc == 5, f"witnessed uses lost in the snapshot: {uc}"
    c.close()


def test_snapshot_works_without_serialize(live_bank, tmp_path, monkeypatch):
    """PORTABILITY: Connection.serialize() is Python 3.11+. Box4 runs 3.10, where it raises
    AttributeError — which would break `backup --push` there. Simulate the older interpreter
    by hiding serialize, and prove the online-backup fallback produces the same content."""
    store, db, _ = live_bank

    # Subclass, so the object still IS a real sqlite3.Connection (the C backup API requires
    # that of its argument) while hiding serialize() the way python 3.10 does.
    class NoSerialize(sqlite3.Connection):
        @property
        def serialize(self):
            raise AttributeError("serialize")

    real_connect = sqlite3.connect
    monkeypatch.setattr(sqlite3, "connect",
                        lambda *a, **k: real_connect(*a, factory=NoSerialize, **k))
    assert not hasattr(sqlite3.connect(f"file:{db}?mode=ro", uri=True), "serialize")

    blob = backup_cmd._consistent_snapshot(db)
    assert blob.startswith(b"SQLite format 3"), "fallback did not produce a sqlite image"
    out = tmp_path / "fallback.db"
    out.write_bytes(blob)
    c = sqlite3.connect(f"file:{out}?mode=ro", uri=True)
    assert c.execute("SELECT COUNT(*) FROM atoms").fetchone()[0] == 5
    assert c.execute("SELECT SUM(use_count) FROM atom_earned").fetchone()[0] == 5
    c.close()


def test_backup_verb_writes_a_readable_bank(live_bank, tmp_path, monkeypatch):
    """The `echelon backup` default path, end to end, against an open bank."""
    store, db, _ = live_bank
    outdir = tmp_path / "backups"
    monkeypatch.setattr(backup_cmd, "_BACKUP_DIR", outdir)
    rc = backup_cmd._main(["--bank", str(db)])
    assert rc == 0
    snaps = list(outdir.glob("echelon_*.db"))
    assert len(snaps) == 1
    c = sqlite3.connect(f"file:{snaps[0]}?mode=ro", uri=True)
    assert c.execute("SELECT COUNT(*) FROM atoms").fetchone()[0] == 5
    c.close()


def test_restore_clears_stale_wal_sidecars(live_bank, tmp_path, monkeypatch):
    """A restore drops a new image over the file; the OLD bank's -wal would otherwise be
    replayed over it and silently resurrect pre-restore state."""
    store, db, _ = live_bank
    outdir = tmp_path / "backups"
    monkeypatch.setattr(backup_cmd, "_BACKUP_DIR", outdir)
    backup_cmd._main(["--bank", str(db)])
    snap = next(outdir.glob("echelon_*.db"))

    # more work AFTER the snapshot, left uncheckpointed in the WAL
    extra = store.add_atom("lesson:after", "# after\nnot in the snapshot", scope="waltest")
    store.compile_atom_struct(extra)
    store.conn.close()

    rc = backup_cmd._main(["--restore", str(snap), "--bank", str(db)])
    assert rc == 0
    assert not (tmp_path / "echelon.db-wal").exists(), "stale WAL survived the restore"
    c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    n = c.execute("SELECT COUNT(*) FROM atoms").fetchone()[0]
    assert n == 5, f"restore did not land the snapshot state (got {n} atoms)"
    assert c.execute("SELECT COUNT(*) FROM atoms WHERE id=?", (extra,)).fetchone()[0] == 0
    c.close()


def test_pre_restore_safety_copy_is_readable(live_bank, tmp_path, monkeypatch):
    """The safety copy taken before a restore is the ONLY way back if the restore was a
    mistake — it must not be a table-less stub."""
    store, db, _ = live_bank
    outdir = tmp_path / "backups"
    monkeypatch.setattr(backup_cmd, "_BACKUP_DIR", outdir)
    backup_cmd._main(["--bank", str(db)])
    snap = next(outdir.glob("echelon_*.db"))
    store.conn.close()
    backup_cmd._main(["--restore", str(snap), "--bank", str(db)])
    safety = db.with_suffix(".db.pre_restore")
    assert safety.exists()
    c = sqlite3.connect(f"file:{safety}?mode=ro", uri=True)
    assert c.execute("SELECT COUNT(*) FROM atoms").fetchone()[0] == 5
    c.close()
