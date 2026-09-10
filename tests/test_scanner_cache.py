"""The import-time architecture scan is cached by tree signature (owner 2026-09-05: the
UserPromptSubmit hook blew its 12 s budget; 4-6 s of it was this scan re-parsing ~350 unchanged
files on every `import echelon_engine`). The gate keeps its teeth: any edit rescans, a failing
tree never caches, ECHELON_SCANNER_FORCE=1 always scans."""
import os
import time
from pathlib import Path

import pytest

from echelon_engine import _scanner
from echelon_sdk.exceptions import ScannerError


def _tree(tmp_path, monkeypatch, body="x = 1\n"):
    root = tmp_path / "root"
    (root / "echelon_sdk").mkdir(parents=True)
    f = root / "echelon_sdk" / "clean.py"
    f.write_text(body, encoding="utf-8")
    monkeypatch.setattr(_scanner, "_ROOT", root)
    monkeypatch.setenv("ECHELON_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("ECHELON_SCANNER_FORCE", raising=False)
    return f


def _spy(monkeypatch):
    calls = []
    real = _scanner._scan_file_imports
    monkeypatch.setattr(_scanner, "_scan_file_imports", lambda f, errors: calls.append(f) or real(f, errors))
    return calls


def test_second_import_of_an_unchanged_tree_skips_the_scan(tmp_path, monkeypatch):
    _tree(tmp_path, monkeypatch)
    calls = _spy(monkeypatch)
    assert _scanner.validate_chains() is True
    assert len(calls) == 1, "first call scans"
    assert _scanner.validate_chains() is True
    assert len(calls) == 1, "unchanged tree: cache hit, no AST scan"
    assert (tmp_path / "home" / "scanner_ok.json").exists()


def test_any_edit_rescans(tmp_path, monkeypatch):
    f = _tree(tmp_path, monkeypatch)
    calls = _spy(monkeypatch)
    _scanner.validate_chains()
    f.write_text("x = 2\n", encoding="utf-8")
    os.utime(f, ns=(time.time_ns(), time.time_ns() + 1_000_000))
    _scanner.validate_chains()
    assert len(calls) == 2, "a changed mtime/size invalidates the cache"


def test_a_failing_tree_never_caches(tmp_path, monkeypatch):
    f = _tree(tmp_path, monkeypatch, body="import echelon_engine\n")   # sdk importing upward = law broken
    calls = _spy(monkeypatch)
    with pytest.raises(ScannerError):
        _scanner.validate_chains()
    assert not (tmp_path / "home" / "scanner_ok.json").exists()
    with pytest.raises(ScannerError):
        _scanner.validate_chains()
    assert len(calls) == 2, "a red tree is scanned every time"


def test_force_env_bypasses_the_cache(tmp_path, monkeypatch):
    _tree(tmp_path, monkeypatch)
    calls = _spy(monkeypatch)
    _scanner.validate_chains()
    monkeypatch.setenv("ECHELON_SCANNER_FORCE", "1")
    _scanner.validate_chains()
    assert len(calls) == 2
