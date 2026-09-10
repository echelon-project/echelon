"""Skeleton smoke + scanner-has-teeth tests.

Proves three things the skeleton must guarantee from day one:
  1. the canonical ChainResult works (fail-fast + step records + observers),
  2. the scanner PASSES on the clean skeleton (boot law green),
  3. the scanner BITES a real import-law violation (teeth, not theater) — the
     property that makes every future green trustworthy.
"""
import textwrap
from pathlib import Path

import pytest

from echelon_sdk.chain import ChainResult
from echelon_sdk.exceptions import ScannerError
from echelon_engine import _scanner


# ── 1. ChainResult works ──────────────────────────────────────────────────
def test_chain_happy_path():
    r = (ChainResult.of(2)
         .pipe(lambda x: x + 3)
         .pipe(lambda x: x * 10)
         .collect())
    assert r.ok
    assert r.value == 50
    assert [s["name"] for s in r.steps][0] == "of"


def test_chain_fail_fast_short_circuits():
    def boom(_):
        raise ValueError("nope")
    r = (ChainResult.of(1)
         .pipe(boom)
         .pipe(lambda x: x + 100)   # must be SKIPPED, not run
         .collect())
    assert not r.ok
    assert r.errors and isinstance(r.errors[0]["error"], ValueError)
    skipped = [s for s in r.steps if s["skipped"]]
    assert skipped, "steps after a failure must be recorded as skipped"


def test_chain_where_skips_rest():
    r = (ChainResult.of(5)
         .where(lambda x: x > 10)        # false -> skip rest
         .pipe(lambda x: x * 2)
         .collect())
    assert r.ok                          # where-skip is ok, not error
    assert all(s["value"] != 10 for s in r.steps)  # the *2 never ran


# ── 2. scanner passes clean skeleton ──────────────────────────────────────
def test_scanner_passes_on_clean_skeleton():
    assert _scanner.validate_chains() is True


# ── 3. scanner BITES a real violation (teeth) ─────────────────────────────
def test_scanner_bites_upward_import(tmp_path, monkeypatch):
    """Plant an echelon_sdk file that imports echelon_engine (forbidden upward),
    point the scanner at a fake root, and assert it RAISES. If this passes, the
    scanner is real; if a violation slips through green, the law is theater."""
    root = tmp_path
    (root / "echelon_sdk").mkdir()
    # a pure-library file illegally reaching UP into the engine
    (root / "echelon_sdk" / "bad.py").write_text(
        "from echelon_engine.services import nothing\n", encoding="utf-8")
    monkeypatch.setattr(_scanner, "_ROOT", Path(root))
    with pytest.raises(ScannerError) as ei:
        _scanner.validate_chains()
    assert "import-law" in str(ei.value) and "echelon_sdk" in str(ei.value)
