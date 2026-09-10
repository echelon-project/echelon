"""Tests for the echelon_cli MCP tool gate (owner 2026-08-16).

The raw-argv door is the widest surface the substrate has — it must be
visible/callable ONLY for the owner, admin-role accounts, and owner@example.com.
Every other identity must not even SEE the tool in tools/list.
"""
from __future__ import annotations

import pytest

from echelon_engine import mcp_server as m


def _req(is_owner=False, role=None, email=None):
    req = m._REQ
    req.is_owner = is_owner
    req.role = role
    req.email = email
    return req


class TestCliToolGate:
    def test_owner_sees_it(self):
        _req(is_owner=True)
        assert "echelon_cli" in m._TOOLS
        assert m._tool_allowed("echelon_cli")

    def test_admin_role_sees_it(self):
        _req(is_owner=False, role="admin", email="someone@else.dev")
        assert m._tool_allowed("echelon_cli")

    def test_explicitly_allowlisted_email_sees_it(self, monkeypatch):
        """ECHELON_CLI_ALLOWED_EMAILS is an explicit opt-in ON TOP of owner/admin.
        It is EMPTY by default — raw argv is never granted implicitly."""
        monkeypatch.setattr(m, "_CLI_ALLOWED_EMAILS",
                            frozenset({"owner@example.com"}))
        _req(is_owner=False, role="member", email="owner@example.com")
        assert m._tool_allowed("echelon_cli")

    def test_allowlist_is_empty_by_default(self):
        """With no ECHELON_CLI_ALLOWED_EMAILS set, no plain member reaches raw argv."""
        assert m._CLI_ALLOWED_EMAILS == frozenset()
        _req(is_owner=False, role="member", email="owner@example.com")
        assert not m._tool_allowed("echelon_cli")

    def test_member_does_not_see_it(self):
        _req(is_owner=False, role="member", email="member@example.com")
        assert not m._tool_allowed("echelon_cli")
        # and tools/list hides it entirely
        listed = [n for n in m._TOOLS if m._tool_allowed(n)]
        assert "echelon_cli" not in listed

    def test_anonymous_member_does_not_see_it(self):
        _req(is_owner=False, role="member", email=None)
        assert not m._tool_allowed("echelon_cli")

    def test_schema_requires_args_list(self):
        out = m._t_cli({"args": "not-a-list"})
        assert "ERROR" in out
        out = m._t_cli({})
        assert "ERROR" in out
