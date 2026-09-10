"""Focused unit test for echelon_engine.atoms.repo_ingest.

Surface under test: ingest_repo(bank, root, repo) — scans a repo's structure via
echelon_sdk.repo_graph.build_graph and stores it into a KnowledgeBank as versioned COS
entries (one 'file_outline' per file, one 'signature' per function/class), and the pure
coordinate helpers (_file_coord, _elem_coord, _seg).

Isolation: a tiny fake repo built under tmp_path (two .py files) + an isolated
KnowledgeBank(uame=UAME(tmp_path/'core.db')) — no shared default db touched.

Versioning is the load-bearing contract (owner: same coordinate, different content later):
a re-scan of unchanged code is a no-op ('unchanged'); a changed signature supersedes its prior
version ('versioned'). Both are asserted.
"""
from __future__ import annotations

import pytest

from echelon_engine.atoms.uame import UAME
from echelon_engine.atoms.bank import KnowledgeBank
from echelon_engine.atoms import repo_ingest
from echelon_engine.atoms.repo_ingest import ingest_repo, _seg, _file_coord, _elem_coord


@pytest.fixture
def bank(tmp_path):
    return KnowledgeBank(uame=UAME(tmp_path / "core.db"))


def _write_repo(root, alpha_body="def hello(name) -> str:\n    return name\n"):
    pkg = root / "pkg"
    pkg.mkdir(parents=True, exist_ok=True)
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "alpha.py").write_text(
        "import os\n\n" + alpha_body + "\n\nclass Widget:\n    pass\n", encoding="utf-8")
    (root / "beta.py").write_text(
        "def add(a, b):\n    return a + b\n", encoding="utf-8")


# --- pure helpers (no db) ---

def test_seg_sanitizes():
    assert _seg("Foo-Bar.Baz") == "foo_bar_baz"
    assert _seg("  A B  ") == "a_b"


def test_file_coord_drops_py_and_init():
    # __init__ is dropped from the taxonomy; .py stripped; dirs become depth.
    assert _file_coord("myrepo", "pkg/alpha.py") == "repo:myrepo:pkg:alpha"
    assert _file_coord("myrepo", "pkg/__init__.py") == "repo:myrepo:pkg"


def test_elem_coord_uses_name_only():
    fc = "repo:myrepo:pkg:alpha"
    assert _elem_coord(fc, "hello(name) -> str") == "repo:myrepo:pkg:alpha:hello"
    assert _elem_coord(fc, "Widget.run(self)") == "repo:myrepo:pkg:alpha:widget_run"


# --- ingest against a real tiny repo ---

def test_ingest_stores_files_and_signatures_at_coordinates(bank, tmp_path):
    repo_root = tmp_path / "myrepo"
    _write_repo(repo_root)
    counts = ingest_repo(bank, repo_root, repo="myrepo")

    assert counts["files"] >= 2          # alpha.py, beta.py (at least)
    assert counts["signatures"] >= 3     # hello, add, class Widget
    assert counts["errors"] == 0

    # the file outline landed at the file coordinate, kind='file_outline'.
    alpha_outline = bank._current_at("repo:myrepo:pkg:alpha", "file_outline", _now())
    assert alpha_outline is not None
    assert "alpha.py" in alpha_outline.content

    # the function signature landed at the element coordinate, kind='signature'.
    hello_sig = bank._current_at("repo:myrepo:pkg:alpha:hello", "signature", _now())
    assert hello_sig is not None
    assert "hello(name)" in hello_sig.content

    # the class landed too.
    widget_sig = bank._current_at("repo:myrepo:pkg:alpha:widget", "signature", _now())
    assert widget_sig is not None
    assert "class Widget" in widget_sig.content


def test_reingest_unchanged_is_noop(bank, tmp_path):
    repo_root = tmp_path / "myrepo"
    _write_repo(repo_root)
    ingest_repo(bank, repo_root, repo="myrepo")
    again = ingest_repo(bank, repo_root, repo="myrepo")
    # second pass: every element already at its current content -> all unchanged, none versioned.
    assert again["versioned"] == 0
    assert again["unchanged"] > 0


def test_changed_signature_versions(bank, tmp_path):
    repo_root = tmp_path / "myrepo"
    _write_repo(repo_root)
    ingest_repo(bank, repo_root, repo="myrepo")
    # change hello's signature; re-ingest must supersede the prior version at that coordinate.
    _write_repo(repo_root, alpha_body="def hello(name, loud) -> str:\n    return name\n")
    again = ingest_repo(bank, repo_root, repo="myrepo")
    assert again["versioned"] >= 1
    # history at the coordinate now holds >1 version.
    hist = bank.history("repo:myrepo:pkg:alpha:hello", "signature")
    assert len(hist) >= 2
    current = bank._current_at("repo:myrepo:pkg:alpha:hello", "signature", _now())
    assert "loud" in current.content


def _now():
    import time
    return int(time.time())
