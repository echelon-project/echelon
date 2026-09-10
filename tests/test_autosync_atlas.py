"""Tests for echelon_engine/hooks_staged/echelon_autosync.py -- OPEN-0075 atlas-regen trigger.

The hook's module-level thread only starts under `if __name__ == "__main__"`, so importing it
via spec_from_file_location (same pattern as test_cgraph.py's _load_hook) is safe: we call its
internal functions directly instead of feeding it stdin/subprocess.
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
import time
from pathlib import Path

import pytest


def _load_hook():
    hook_path = Path(__file__).resolve().parents[1] / "echelon_engine" / "hooks_staged" / "echelon_autosync.py"
    spec = importlib.util.spec_from_file_location("echelon_autosync_staged_test", hook_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _init_repo(root: Path):
    root.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)


def _graphed_repo(root: Path, pkgs=("pkg_a",), stale=True):
    _init_repo(root)
    define = root / "docs" / "c-atlas" / "define"
    define.mkdir(parents=True)
    (define / "_meta.json").write_text(
        json.dumps({"packages": list(pkgs), "node_count": 1}), encoding="utf-8")
    (define / "pkg-a-mod.json").write_text(
        json.dumps({"id": "pkg-a-mod", "path": "pkg_a/mod.py", "members": [],
                    "depends_on": []}), encoding="utf-8")
    for pkg in pkgs:
        pkg_dir = root / pkg
        pkg_dir.mkdir(parents=True, exist_ok=True)
        (pkg_dir / "mod.py").write_text("x = 1\n", encoding="utf-8")
    import os
    if stale:
        # make the card look older than the source file
        old = time.time() - 1000
        os.utime(define / "pkg-a-mod.json", (old, old))
    else:
        # make the card look newer than every source file (nothing to regenerate)
        future = time.time() + 1000
        os.utime(define / "pkg-a-mod.json", (future, future))
    return root


def test_commit_with_stale_atlas_invokes_regen_with_meta_packages(tmp_path, monkeypatch):
    hook = _load_hook()
    root = _graphed_repo(tmp_path / "repo", pkgs=("pkg_a", "pkg_b"), stale=True)
    monkeypatch.setattr(hook, "_repo_root", lambda cwd: str(root))
    captured = {}

    def fake_run(argv, **kw):
        captured["argv"] = argv
        return type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    monkeypatch.setattr(hook.subprocess, "run", fake_run)
    hook.OUT.clear()
    hook._atlas_regen(str(root), "git commit -m x")
    assert "argv" in captured
    argv = captured["argv"]
    assert "gen_code_atlas" in " ".join(argv)
    assert argv.count("--pkg") == 2
    idx_a = argv.index("--pkg")
    assert "pkg_a" in argv[idx_a + 1:] and "pkg_b" in argv[idx_a + 1:]
    assert "additionalContext" in hook.OUT.get("hookSpecificOutput", {})


def test_commit_with_fresh_atlas_no_invocation(tmp_path, monkeypatch):
    hook = _load_hook()
    root = _graphed_repo(tmp_path / "repo", pkgs=("pkg_a",), stale=False)
    monkeypatch.setattr(hook, "_repo_root", lambda cwd: str(root))
    called = []
    monkeypatch.setattr(hook.subprocess, "run", lambda *a, **k: called.append(1))
    logged = []
    monkeypatch.setattr(hook, "_log", lambda msg: logged.append(msg))
    hook.OUT.clear()
    hook._atlas_regen(str(root), "git commit -m x")
    assert not called
    assert any("fresh" in m for m in logged)


def test_non_graphed_repo_does_nothing(tmp_path, monkeypatch):
    hook = _load_hook()
    root = tmp_path / "bare"
    _init_repo(root)
    monkeypatch.setattr(hook, "_repo_root", lambda cwd: str(root))
    called = []
    monkeypatch.setattr(hook.subprocess, "run", lambda *a, **k: called.append(1))
    logged = []
    monkeypatch.setattr(hook, "_log", lambda msg: logged.append(msg))
    hook.OUT.clear()
    hook._atlas_regen(str(root), "git commit -m x")
    assert not called
    assert not logged


def test_dry_run_commit_does_nothing(tmp_path, monkeypatch):
    hook = _load_hook()
    root = _graphed_repo(tmp_path / "repo", stale=True)
    monkeypatch.setattr(hook, "_repo_root", lambda cwd: str(root))
    called = []
    monkeypatch.setattr(hook.subprocess, "run", lambda *a, **k: called.append(1))
    hook.OUT.clear()
    hook._atlas_regen(str(root), "git commit --dry-run -m x")
    assert not called


def test_harness_sync_trigger_still_fires_on_ingest(tmp_path, monkeypatch):
    """Regression: the pre-existing harness-sync trigger must keep working after the atlas
    trigger was added alongside it in _body()."""
    hook = _load_hook()
    contract = tmp_path / "echelon-harness-contract.json"
    contract.write_text("{}", encoding="utf-8")
    captured = {}

    def fake_run(argv, **kw):
        captured["argv"] = argv
        return type("R", (), {"returncode": 0, "stdout": "harness synced\n", "stderr": ""})()

    monkeypatch.setattr(hook.subprocess, "run", fake_run)
    hook.OUT.clear()
    hook._harness_sync(str(tmp_path), "python -m echelon_engine ingest --root memory --scope echelon")
    assert "argv" in captured
    assert "harness-sync" in captured["argv"]
    assert "additionalContext" in hook.OUT.get("hookSpecificOutput", {})
