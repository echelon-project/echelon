"""Tests for echelon_sdk.gitflow — pure seam: guard_target protection, PROTECTED set."""
from __future__ import annotations
import pytest

from echelon_sdk.gitflow import guard_target, PROTECTED, INTEGRATION, SocietyWorktree, _git


# ── master protection guard ──────────────────────────────────────────────────

def test_guard_target_blocks_master():
    assert guard_target("master") is False

def test_guard_target_blocks_main():
    assert guard_target("main") is False

def test_guard_target_blocks_master_uppercase():
    assert guard_target("MASTER") is False

def test_guard_target_allows_feature_branch():
    assert guard_target("society/abc123") is True

def test_guard_target_allows_integration():
    assert guard_target("society/integration") is True

def test_guard_target_allows_generic():
    assert guard_target("feature/my-fix") is True


# ── PROTECTED set contents ───────────────────────────────────────────────────

def test_protected_contains_master_and_main():
    assert "master" in PROTECTED
    assert "main" in PROTECTED

def test_integration_not_in_protected():
    # integration is writable — society branch merges into it
    assert INTEGRATION not in PROTECTED


# ── SocietyWorktree construction (pure, no git calls) ────────────────────────

def test_society_worktree_init():
    wt = SocietyWorktree("/fake/repo", "run-001")
    assert wt.branch == "society/run-001"
    assert "run-001" in wt.path
    assert not wt.created
    assert wt.prs == []

def test_society_worktree_open_pr_records():
    wt = SocietyWorktree("/fake/repo", "run-002")
    result = wt.open_pr("Fix the thing", "This PR fixes X.")
    assert result["ok"] is True
    assert len(wt.prs) == 1
    assert wt.prs[0]["from"] == "society/run-002"
    assert wt.prs[0]["to"] == INTEGRATION
    assert wt.prs[0]["title"] == "Fix the thing"

def test_society_worktree_open_pr_truncates_long_title():
    wt = SocietyWorktree("/fake/repo", "run-003")
    long_title = "x" * 300
    result = wt.open_pr(long_title, "body")
    assert len(wt.prs[0]["title"]) <= 200

def test_society_worktree_merge_refuses_protected():
    """merge_to_integration should refuse if INTEGRATION were in PROTECTED (structural guard)."""
    # We test the guard logic directly: patch INTEGRATION to 'master' temporarily
    import echelon_sdk.gitflow as gf
    orig = gf.INTEGRATION
    gf.INTEGRATION = "master"
    try:
        wt = SocietyWorktree("/fake/repo", "run-004")
        result = wt.merge_to_integration()
        assert result["ok"] is False
        assert "refused" in result["error"]
    finally:
        gf.INTEGRATION = orig


# ── _git helper (real subprocess, safe non-destructive ops) ──────────────────

def test_git_version():
    rc, out = _git(".", "version")
    assert rc == 0
    assert "git version" in out.lower()

def test_git_bad_repo_nonzero():
    rc, _ = _git("/nonexistent/path/xyz", "status")
    assert rc != 0
