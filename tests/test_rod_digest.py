"""Focused unit tests for echelon_engine.atoms.rod_digest — the zero-model leaf.

rod_digest is a TRUE ATOM: pure stdlib, zero model calls, zero VRAM, single
responsibility (content string → feature string). These tests pin the leaf in
isolation, independent of warmth_update and the network stack.
"""
import pytest

from echelon_engine.atoms.rod_digest import (
    rod_digest,
    _RE_DATE,
    _RE_SUPERSEDE,
    _RE_ARCHIVE,
    _RE_LIVE,
    _RE_LEDGER,
)


# ── public function ────────────────────────────────────────────────────────────

def test_date_iso_format():
    out = rod_digest("decided on 2026-06-18 to migrate the store")
    assert "date=2026-06-18" in out


def test_date_compact_format():
    out = rod_digest("snapshot taken 20260618 for archival")
    assert "date=20260618" in out


def test_supersede_language():
    assert "mentions-supersede/revert" in rod_digest("this supersedes the old plan")
    assert "mentions-supersede/revert" in rod_digest("we revert that decision")
    assert "mentions-supersede/revert" in rod_digest("this formula is now obsolete")
    assert "mentions-supersede/revert" in rod_digest("the old path was deprecated")


def test_archive_language():
    assert "mentions-archive/destroy" in rod_digest("this was archived pre-cutover")
    assert "mentions-archive/destroy" in rod_digest("the old service was destroyed")
    assert "mentions-archive/destroy" in rod_digest("the endpoint was disabled")


def test_live_language():
    assert "mentions-still-in-force" in rod_digest("the rule is still in force")
    assert "mentions-still-in-force" in rod_digest("this is still applied today")
    assert "mentions-still-in-force" in rod_digest("live now in production")


def test_dated_session_ledger_in_first_90_chars():
    # marker must be within the first 90 chars of content
    assert "dated-session-ledger" in rod_digest("SESSION 2026-06-18 ledger: did X, Y, Z")
    assert "dated-session-ledger" in rod_digest("session-2026-06-18: work done here")


def test_ledger_beyond_90_chars_not_matched():
    # if the ledger marker appears only AFTER position 90, it is NOT flagged
    prefix = "x" * 91
    out = rod_digest(prefix + " session-20 ledger: work")
    assert "dated-session-ledger" not in out


def test_no_markers_returns_sentinel():
    assert rod_digest("a plain timeless fact about gzip") == "no structural markers"


def test_multiple_signals_combined():
    content = "SESSION 2026-05-01 ledger: we archived the old endpoint, now superseded"
    out = rod_digest(content)
    assert "date=2026-05-01" in out
    assert "dated-session-ledger" in out
    assert "mentions-archive/destroy" in out
    assert "mentions-supersede/revert" in out


def test_empty_string():
    assert rod_digest("") == "no structural markers"


# ── regex objects are exported (warmth_update re-exports them) ─────────────────

def test_regexes_are_compiled():
    import re
    for obj in (_RE_DATE, _RE_SUPERSEDE, _RE_ARCHIVE, _RE_LIVE, _RE_LEDGER):
        assert isinstance(obj, type(re.compile("")))


def test_re_date_matches_iso():
    assert _RE_DATE.search("2026-06-18")


def test_re_supersede_case_insensitive():
    assert _RE_SUPERSEDE.search("SUPERSEDED")
    assert _RE_SUPERSEDE.search("Reverts")


def test_re_archive_case_insensitive():
    assert _RE_ARCHIVE.search("Archived")


# ── re-export chain: rod_digest is also reachable from warmth_update ──────────

def test_warmth_update_reexports_rod_digest():
    from echelon_engine.atoms import warmth_update as wu
    from echelon_engine.atoms.rod_digest import rod_digest as rd_direct
    assert wu.rod_digest is rd_direct  # same object, re-exported not re-defined


def test_warmth_update_reexports_regexes():
    from echelon_engine.atoms import warmth_update as wu
    from echelon_engine.atoms import rod_digest as rd_mod
    assert wu._RE_DATE is rd_mod._RE_DATE
    assert wu._RE_LEDGER is rd_mod._RE_LEDGER
