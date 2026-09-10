"""test_delta — the LOCAL delta backup (spec S8 V7, `echelon backup --delta`).

Every test runs under ECHELON_HOME=tmp (the substrate home relocates), a tmp
bank whose sync_journal is built through the REAL sync_journal.attach door, and
a tmp workcycle.init room registered via register_room; cwd is monkeypatched
into the room's repo so room_path() resolves by the ancestor walk. The three
staged hooks run as REAL subprocesses from stdin with the SessionStart/Stop/
Prompt JSON shapes (the test_doors.py pattern).
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import time
from datetime import date
from pathlib import Path

import pytest

from echelon_engine import workcycle
from echelon_engine.atoms import delta
from echelon_engine.atoms import sync_journal

REPO_ROOT = Path(__file__).resolve().parent.parent
OLD_ISO = "2026-08-01T00:00:00+00:00"


def _seed_journal(bank: Path, n: int) -> None:
    """Build the journal schema through the REAL attach door, then plant n rows
    directly (the journal table itself has no triggers)."""
    conn = sqlite3.connect(str(bank))
    try:
        sync_journal.attach(conn, force=True)
        for i in range(n):
            conn.execute(
                "INSERT INTO sync_journal (tbl, pk, op, payload, scope, ts, origin) "
                "VALUES (?,?,?,?,?,?,?)",
                ("atoms", json.dumps({"id": f"a{i}"}), "INSERT",
                 json.dumps({"content": f"atom {i}"}), "testscope",
                 1700000000 + i, ""))
        conn.commit()
    finally:
        conn.close()


@pytest.fixture
def delta_env(tmp_path, monkeypatch):
    """ECHELON_HOME=tmp + a seeded-capable bank + a registered room; cwd is the
    room's repo so room_path() resolves by the ancestor walk."""
    home = tmp_path / "home"
    monkeypatch.setenv("ECHELON_HOME", str(home))
    bank = home / "echelon.db"
    repo = tmp_path / "repo"
    workcycle.init(repo, estate="t", type_="engine", scope="testscope")
    workcycle.register_room(repo)
    monkeypatch.chdir(repo)
    out = tmp_path / "out"
    return type("Env", (), {"home": home, "bank": bank, "repo": repo, "out": out})


def test_delta_writes_one_file_with_exactly_three_rows(delta_env):
    _seed_journal(delta_env.bank, 3)
    res = delta.run_delta(delta_env.bank, delta_env.out)
    assert res["wrote"] and res["journal_rows"] == 3
    assert res["from_seq"] == 0 and res["to_seq"] == 3
    delta_dir = Path(res["delta_dir"])
    assert delta_dir.parent.name == date.today().isoformat()
    jfiles = sorted(delta_dir.glob("*.jsonl"))
    assert len(jfiles) == 1
    rows = [json.loads(l) for l in jfiles[0].read_text(encoding="utf-8").splitlines()]
    assert [r["seq"] for r in rows] == [1, 2, 3]          # seq order, exactly the 3 rows
    manifest = json.loads((delta_dir / "MANIFEST.json").read_text(encoding="utf-8"))
    assert manifest["journal_rows"] == 3
    assert manifest["from_seq"] == 0 and manifest["to_seq"] == 3
    assert manifest["bytes"] > 0
    assert jfiles[0].name in manifest["files"]
    assert "MANIFEST.json" not in manifest["files"]      # the manifest never hashes itself
    cursor = delta._read_cursor()
    assert cursor["seq"] == 3 and cursor["last_run"]     # cursor advanced to the max seq


def test_delta_no_change_writes_nothing_and_cursor_untouched(delta_env):
    _seed_journal(delta_env.bank, 2)
    res1 = delta.run_delta(delta_env.bank, delta_env.out)
    res2 = delta.run_delta(delta_env.bank, delta_env.out)
    assert res1["wrote"] and res2 == {"wrote": False}
    today_dir = delta_env.out / "delta" / date.today().isoformat()
    assert len(list(today_dir.iterdir())) == 1           # no second file
    assert delta._read_cursor()["seq"] == 2              # cursor untouched


def test_delta_room_change_only_writes_sweep(delta_env):
    _seed_journal(delta_env.bank, 1)
    delta.run_delta(delta_env.bank, delta_env.out)
    room = delta_env.repo / ".echelon"
    workcycle.open_item(room, "a room item")             # touches open/OPEN-0001.json
    res = delta.run_delta(delta_env.bank, delta_env.out)
    assert res["wrote"] and res["journal_rows"] == 0     # nothing journaled since the cursor
    assert res["to_seq"] == res["from_seq"] == 1
    delta_dir = Path(res["delta_dir"])
    assert not list(delta_dir.glob("*.jsonl"))           # no journal file
    sweep = delta_dir / "rooms" / "t" / "open" / "OPEN-0001.json"
    assert sweep.is_file()                               # the room sweep travelled
    assert (delta_dir / "rooms" / "t" / "cursor.json").is_file()


def test_delta_copies_changed_atoms_only(delta_env):
    mem = delta_env.repo / "memory"
    mem.mkdir()
    changed = mem / "changed.md"
    unchanged = mem / "unchanged.md"
    changed.write_text("# changed\n", encoding="utf-8")
    unchanged.write_text("# unchanged\n", encoding="utf-8")
    old_epoch = delta._to_epoch(OLD_ISO)                 # 2026-08-01T00:00:00Z = 1785542400
    os.utime(changed, (old_epoch - 100, old_epoch + 5))  # changed AFTER last_run
    os.utime(unchanged, (old_epoch - 200, old_epoch - 5))  # unchanged BEFORE last_run
    delta._write_cursor({"v": 1, "seq": 0, "last_run": OLD_ISO})
    _seed_journal(delta_env.bank, 1)
    res = delta.run_delta(delta_env.bank, delta_env.out)
    assert res["atoms"] == 1
    delta_dir = Path(res["delta_dir"])
    assert (delta_dir / "atoms" / "changed.md").is_file()
    assert not (delta_dir / "atoms" / "unchanged.md").exists()


def test_delta_cursor_not_advanced_when_manifest_fails(delta_env, monkeypatch):
    _seed_journal(delta_env.bank, 1)
    real_write_text = Path.write_text

    def boom(self, *a, **k):
        if "MANIFEST.json" in str(self):
            raise OSError("manifest write failed (test)")
        return real_write_text(self, *a, **k)

    monkeypatch.setattr(Path, "write_text", boom)
    with pytest.raises(OSError):
        delta.run_delta(delta_env.bank, delta_env.out)
    assert delta._read_cursor()["seq"] == 0              # ack-after-write: cursor untouched


def test_tick_armed_stale_spawns_without_advancing_archive_cursor(delta_env, monkeypatch):
    (delta_env.home / "delta.armed").write_text("x", encoding="utf-8")
    delta._write_cursor({"v": 1, "seq": 0, "last_run": OLD_ISO})
    calls = []

    def fake_popen(*a, **k):
        calls.append((a, k))
        return None

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    delta.tick()
    assert len(calls) == 1
    argv = calls[0][0][0]                                # Popen's positional args is a 1-tuple
    assert argv == [sys.executable, "-X", "utf8", "-m", "echelon_engine",
                    "backup", "--delta"]
    if os.name == "nt":
        assert calls[0][1]["creationflags"] & subprocess.DETACHED_PROCESS
    assert (delta_env.home / "delta.log").exists()
    assert delta._read_cursor()["last_run"] == OLD_ISO
    delta.tick()
    assert len(calls) == 1  # separate scheduling timestamp prevents a prompt storm


def test_tick_armed_fresh_no_spawn(delta_env, monkeypatch):
    (delta_env.home / "delta.armed").write_text("x", encoding="utf-8")
    delta._write_cursor({"v": 1, "seq": 0, "last_run": delta._now_iso()})
    calls = []
    monkeypatch.setattr(subprocess, "Popen",
                        lambda *a, **k: calls.append((a, k)))
    delta.tick()
    assert calls == []


def test_tick_disarmed_no_spawn(delta_env, monkeypatch):
    delta._write_cursor({"v": 1, "seq": 0, "last_run": OLD_ISO})
    calls = []
    monkeypatch.setattr(subprocess, "Popen",
                        lambda *a, **k: calls.append((a, k)))
    delta.tick()
    assert calls == []


def test_tick_raising_popen_reports_failure_without_acknowledging_archive(delta_env, monkeypatch, capsys):
    (delta_env.home / "delta.armed").write_text("x", encoding="utf-8")
    delta._write_cursor({"v": 1, "seq": 0, "last_run": OLD_ISO})

    def raise_popen(*a, **k):
        raise RuntimeError("spawn failed (test)")

    monkeypatch.setattr(subprocess, "Popen", raise_popen)
    delta.tick()                                          # must NOT raise into the hook
    assert delta._read_cursor()["last_run"] == OLD_ISO
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "scheduled delta unavailable (RuntimeError)" in captured.err
    assert "spawn failed (test)" not in captured.err


def test_delta_over_100mb_calls_archive_push(delta_env, monkeypatch):
    _seed_journal(delta_env.bank, 1)
    calls = []
    monkeypatch.setattr(delta, "_dir_bytes", lambda d: delta.PUSH_BYTES + 1)
    monkeypatch.setattr(delta.archive, "cmd_push",
                        lambda *a, **k: calls.append((a, k)) or {})
    res = delta.run_delta(delta_env.bank, delta_env.out)
    assert res["wrote"] and res["pushed"]
    (local_dir, prefix), kw = calls[0][0], calls[0][1]
    assert Path(local_dir) == Path(res["delta_dir"])
    assert prefix.startswith(f"delta/{date.today().isoformat()}/")
    assert kw["all_"] is True and kw["client"] is None


def test_delta_receipt_journaled_in_room(delta_env):
    _seed_journal(delta_env.bank, 1)
    delta.run_delta(delta_env.bank, delta_env.out)
    jfile = delta_env.repo / ".echelon" / "journal" / f"{date.today().isoformat()}.jsonl"
    lines = [json.loads(l) for l in jfile.read_text(encoding="utf-8").splitlines()]
    last = lines[-1]
    assert last["kind"] == "receipt" and last["delta"] == "run"
    assert last["journal_rows"] == 1 and last["to_seq"] == 1


def test_delta_force_writes_when_no_change(delta_env):
    _seed_journal(delta_env.bank, 1)
    delta.run_delta(delta_env.bank, delta_env.out)
    res = delta.run_delta(delta_env.bank, delta_env.out, force=True)
    assert res["wrote"] and res["journal_rows"] == 0


# ── the three staged hooks run end-to-end from stdin (test_doors.py shapes) ──

_HOOKS = Path(__file__).resolve().parent.parent / "echelon_engine" / "hooks_staged"


def _run_hook(name: str, payload: str, env: dict, cwd: str, timeout: int = 90):
    return subprocess.run(
        [sys.executable, "-X", "utf8", str(_HOOKS / name)],
        input=payload, capture_output=True, text=True,
        env={**os.environ, **env}, cwd=cwd, timeout=timeout)


def test_staged_sessionstart_hook_arms_delta(tmp_path):
    home = tmp_path / "home"
    env = {"ECHELON_HOME": str(home), "PYTHONPATH": str(REPO_ROOT),
           "ECHELON_ENGINE": str(REPO_ROOT)}
    proc = _run_hook("echelon_gate.py", json.dumps({"cwd": str(tmp_path)}),
                     env, str(tmp_path))
    assert proc.returncode == 0
    assert (home / "delta.armed").is_file()               # the SessionStart arm fired


def test_staged_stop_hook_disarms_and_runs_final_delta(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("ECHELON_HOME", str(home))
    home.mkdir(parents=True)
    bank = home / "echelon.db"
    _seed_journal(bank, 2)
    repo = tmp_path / "repo"
    workcycle.init(repo, estate="t", type_="engine", scope="testscope")
    workcycle.register_room(repo)
    (home / "delta.armed").write_text("x", encoding="utf-8")
    env = {"ECHELON_HOME": str(home), "PYTHONPATH": str(REPO_ROOT)}
    proc = _run_hook("echelon_stop.py", json.dumps({"cwd": str(repo)}), env, str(repo))
    assert proc.returncode == 0
    assert not (home / "delta.armed").exists()            # the Stop disarmed
    today_dir = home / "archive" / "delta" / date.today().isoformat()
    # the final flush is SPAWNED DETACHED (gate r1 V7 M-3): wait for the child, bounded
    import time as _t
    deadline = _t.time() + 60
    dirs = []
    while _t.time() < deadline and not dirs:
        dirs = [d for d in (today_dir.glob("*-seq0-2") if today_dir.is_dir() else [])
                if (d / "MANIFEST.json").exists()]
        if not dirs:
            _t.sleep(0.5)
    assert dirs, "the detached final delta wrote no delta dir within 60s"
    manifest = json.loads((dirs[0] / "MANIFEST.json").read_text(encoding="utf-8"))
    assert manifest["journal_rows"] == 2                  # the seeded rows travelled


def test_staged_prime_hook_ticks_without_crashing(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir(parents=True)
    monkeypatch.setenv("ECHELON_HOME", str(home))        # cursor reads/writes stay in tmp
    (home / "delta.armed").write_text("x", encoding="utf-8")
    delta._write_cursor({"v": 1, "seq": 0, "last_run": OLD_ISO})
    env = {"ECHELON_HOME": str(home), "PYTHONPATH": str(REPO_ROOT)}
    proc = _run_hook("echelon_prime.py",
                     json.dumps({"prompt": "please build the invoice module now",
                                 "cwd": str(tmp_path)}),
                     env, str(tmp_path))
    assert proc.returncode == 0
    # Unknown scope is severable: no fabricated memory offering, but delta still ticks.
    assert proc.stdout == ""
    assert (home / "delta.log").is_file()                  # the tick spawned the delta child
    assert (home / "delta_schedule.json").is_file()         # scheduling is separate from completion


def test_disarm_spawns_detached_never_inline(delta_env, monkeypatch):
    """The Stop flush outlives the hook; scheduling does not attest completion."""
    (delta_env.home / "delta.armed").write_text("x", encoding="utf-8")
    calls = []
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: calls.append((a, k)))
    monkeypatch.setattr(delta, "run_delta", lambda *a, **k: (_ for _ in ()).throw(AssertionError("inline run")))
    result = delta.disarm()
    assert result["scheduled"] is True and result["archive_complete"] is None
    assert not (delta_env.home / "delta.armed").exists()
    assert len(calls) == 1 and calls[0][0][0][-2:] == ["backup", "--delta"]
    assert calls[0][1]["stdout"] is not None and calls[0][1]["stdin"] is subprocess.DEVNULL


def test_delta_relative_out_root_resolves_once(delta_env, monkeypatch):
    """Gate r1 V7 M-2: a relative --out (or relative ECHELON_HOME) must not crash between the
    manifest and the cursor; the receipt's delta_dir is relative to the resolved root."""
    _seed_journal(delta_env.bank, 1)
    rel = Path(os.path.relpath(delta_env.out, delta_env.repo))        # cwd stays IN the room
    res = delta.run_delta(delta_env.bank, rel)                          # relative out_root
    assert res["wrote"] is True
    assert delta._read_cursor()["seq"] == 1                            # cursor advanced
    assert (Path(res["delta_dir"]) / "MANIFEST.json").exists()


def test_cli_delta_uses_the_echelon_home_bank_not_the_live_one(delta_env, capsys):
    """V7 r1 heal: `backup --delta` with no --bank reads <ECHELON_HOME>/echelon.db — the old
    argparse default (the live bank path) made every hook-spawned child read the live bank."""
    from echelon_engine.atoms import backup_cmd
    _seed_journal(delta_env.bank, 2)
    assert backup_cmd._main(["--delta"]) == 0
    assert delta._read_cursor()["seq"] == 2
    assert "seq0-2" in capsys.readouterr().out


def test_tick_preserves_room_and_atom_changes_without_journal_rows(delta_env, monkeypatch):
    _seed_journal(delta_env.bank, 0)
    delta._write_cursor({"v": 1, "seq": 0, "last_run": OLD_ISO})
    room = delta_env.repo / ".echelon"
    workcycle.open_item(room, "must survive the tick")
    mem = delta_env.repo / "memory"
    mem.mkdir()
    (mem / "pending.md").write_text("pending content", encoding="utf-8")
    delta.arm()
    monkeypatch.setattr(delta, "_spawn_delta", lambda home: 123)
    delta.tick()
    result = delta.run_delta(delta_env.bank, delta_env.out)
    assert result["wrote"] is True and result["journal_rows"] == 0
    folder = Path(result["delta_dir"])
    assert (folder / "atoms" / "pending.md").read_text(encoding="utf-8") == "pending content"
    assert (folder / "rooms" / "t" / "open" / "OPEN-0001.json").is_file()


def test_atom_only_edit_triggers_delta(delta_env):
    _seed_journal(delta_env.bank, 0)
    delta.run_delta(delta_env.bank, delta_env.out)
    mem = delta_env.repo / "memory"
    mem.mkdir()
    atom = mem / "only.md"
    atom.write_text("only change", encoding="utf-8")
    result = delta.run_delta(delta_env.bank, delta_env.out)
    assert result["wrote"] is True and result["atoms"] == 1


def test_edit_after_sweep_is_eligible_for_next_delta(delta_env, monkeypatch):
    _seed_journal(delta_env.bank, 0)
    mem = delta_env.repo / "memory"
    mem.mkdir()
    atom = mem / "during.md"
    atom.write_text("before", encoding="utf-8")
    original = delta._copy_atoms

    def copy_then_edit(*args, **kwargs):
        copied = original(*args, **kwargs)
        atom.write_text("after", encoding="utf-8")
        # Real Windows failure: a later edit had an mtime 8ms BEFORE scan start.
        # Deliberately backdate it: content evidence must catch it regardless.
        old = delta._to_epoch(OLD_ISO)
        os.utime(atom, (old, old))
        return copied

    monkeypatch.setattr(delta, "_copy_atoms", copy_then_edit)
    first = delta.run_delta(delta_env.bank, delta_env.out)
    monkeypatch.setattr(delta, "_copy_atoms", original)
    second = delta.run_delta(delta_env.bank, delta_env.out)
    assert second["wrote"] is True
    assert (Path(first["delta_dir"]) / "atoms" / "during.md").read_text(encoding="utf-8") == "before"
    assert (Path(second["delta_dir"]) / "atoms" / "during.md").read_text(encoding="utf-8") == "after"


def test_disarm_reports_unknown_spawn_outcome(delta_env, monkeypatch, capsys):
    delta.arm()

    def failed(home):
        raise OSError("private diagnostic must not leak")

    monkeypatch.setattr(delta, "_spawn_delta", failed)
    result = delta.disarm()
    assert result == {"disarmed": True, "scheduled": False, "archive_complete": None,
                      "schedule_error": "OSError", "retry_safe": False}
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "final delta scheduling unavailable (OSError)" in captured.err
    assert "private diagnostic" not in captured.err


def test_same_second_force_runs_preserve_both_archives(delta_env, monkeypatch):
    _seed_journal(delta_env.bank, 1)
    monkeypatch.setattr(delta.time, "strftime", lambda *a: "120000")
    first = delta.run_delta(delta_env.bank, delta_env.out, force=True)
    manifest = (Path(first["delta_dir"]) / "MANIFEST.json").read_bytes()
    second = delta.run_delta(delta_env.bank, delta_env.out, force=True)
    assert first["delta_dir"] != second["delta_dir"]
    assert (Path(first["delta_dir"]) / "MANIFEST.json").read_bytes() == manifest
