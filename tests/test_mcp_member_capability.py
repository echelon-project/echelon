"""Member capability surface on the single-port MCP gateway.

THE CORRECTION THESE PIN (owner 2026-08-15): a member used to be served 15 of 24
tools, on the reasoning that isolation made some verbs unsafe. That inverted the
intent — "echelon should be his, full capabilities on every user or member; the
reason for the isolation IS to ensure full capabilities served." Isolation is the
enabler, not the fence.

So the boundary moved from CAPABILITY to BILLING + HONESTY:
  - every role sees and may call all 24 tools
  - a member request never inherits the box's provider keys (they'd spend the owner's
    money); their own home/.env funds their calls
  - a paid verb the caller cannot fund returns an HONEST, capability-accurate reason —
    a DeepSeek key must not be told "you have no key" when asking for vision, because
    DeepSeek has no vision model
  - resolve_scope no longer falls back to the SERVER cwd for a member (the one real
    leak), fixed in the verb rather than by removing the verb
"""
from __future__ import annotations

import os

import pytest

from echelon_engine import mcp_server as m


@pytest.fixture(autouse=True)
def _clean_req():
    """Each test owns the thread-local request state."""
    for attr in ("home", "scope", "role", "is_owner"):
        if hasattr(m._REQ, attr):
            delattr(m._REQ, attr)
    yield
    for attr in ("home", "scope", "role", "is_owner"):
        if hasattr(m._REQ, attr):
            delattr(m._REQ, attr)


def _be_member(tmp_path, keys: str = ""):
    m._REQ.is_owner = False
    m._REQ.role = "member"
    m._REQ.home = str(tmp_path)
    m._REQ.scope = "member-scope"
    (tmp_path / ".env").write_text(keys, encoding="utf-8")


def _be_owner():
    m._REQ.is_owner = True
    m._REQ.role = "owner"


# ── the surface ──────────────────────────────────────────────────────────────

def test_no_tool_is_owner_only_anymore():
    assert m._OWNER_ONLY_TOOLS == frozenset()


def test_member_sees_structured_tools_but_not_raw_cli(tmp_path):
    _be_member(tmp_path)
    allowed = [n for n in m._TOOLS if m._tool_allowed(n)]
    assert "echelon_cli" in m._TOOLS
    assert set(allowed) == set(m._TOOLS) - {"echelon_cli"}


def test_member_may_invoke_the_previously_blocked_verbs(tmp_path):
    _be_member(tmp_path)
    for name in ("echelon_ingest", "echelon_check", "echelon_group_scope",
                 "echelon_clone_cartridges", "echelon_resolve_scope"):
        assert m._tool_allowed(name), f"{name} must be available to a member"


# ── billing boundary ─────────────────────────────────────────────────────────

def test_member_request_env_strips_owner_provider_keys(tmp_path, monkeypatch):
    """Defence in depth only — the REAL containment is test_key_resolution_* below."""
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-owner-key")
    monkeypatch.setenv("GEMINI_API_VERTEX", "owner-vertex")
    _be_member(tmp_path)
    env = m._request_env()
    assert "DEEPSEEK_API_KEY" not in env
    assert "GEMINI_API_VERTEX" not in env
    assert env["ECHELON_HOME"] == str(tmp_path)


def test_key_resolution_honors_echelon_home_not_the_box_operator(tmp_path, monkeypatch):
    """THE CRITICAL REGRESSION (skeptic gate 2026-08-15).

    keys.py used to compute ~/.echelon/.env at IMPORT time from Path.home(), so a
    member subprocess — env stripped, ECHELON_HOME set to its own bank — still read
    the OWNER's key off disk and spent the owner's money. Stripping the environment
    could never fix that. Reproduced live before the fix; this pins it closed.
    """
    from echelon_sdk import keys

    owner_home = tmp_path / "owner"
    (owner_home / ".echelon").mkdir(parents=True)
    (owner_home / ".echelon" / ".env").write_text("DEEPSEEK_API_KEY=sk-OWNER\n",
                                                  encoding="utf-8")
    member_home = tmp_path / "member"
    member_home.mkdir()
    (member_home / ".env").write_text("", encoding="utf-8")   # member has NO key

    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.setattr(keys.Path, "home", staticmethod(lambda: owner_home))
    monkeypatch.setattr(keys, "_REPO_APIKEY", tmp_path / "nonexistent" / ".apikey")
    monkeypatch.setenv("ECHELON_HOME", str(member_home))

    with pytest.raises(ValueError):
        keys.load_deepseek_key()          # must NOT fall through to the owner's file


def test_key_resolution_finds_the_members_own_key(tmp_path, monkeypatch):
    """The other half: a member WITH a key must still be able to fund their own call."""
    from echelon_sdk import keys

    member_home = tmp_path / "member"
    member_home.mkdir()
    (member_home / ".env").write_text("DEEPSEEK_API_KEY=sk-MEMBER-OWN\n", encoding="utf-8")
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.setenv("ECHELON_HOME", str(member_home))
    assert keys.load_deepseek_key() == "sk-MEMBER-OWN"


def test_isolated_mode_drops_repo_apikey_fallback(tmp_path, monkeypatch):
    """A shared gateway sets ECHELON_KEYS_ISOLATED=1 so a stray repo .apikey on the
    box can never fund a member's call."""
    from echelon_sdk import keys

    repo_key = tmp_path / ".apikey"
    repo_key.write_text("DEEPSEEK_API_KEY=sk-REPO\n", encoding="utf-8")
    member_home = tmp_path / "member"
    member_home.mkdir()
    (member_home / ".env").write_text("", encoding="utf-8")

    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.setattr(keys, "_REPO_APIKEY", repo_key)
    monkeypatch.setenv("ECHELON_HOME", str(member_home))

    monkeypatch.delenv("ECHELON_KEYS_ISOLATED", raising=False)
    assert keys.load_deepseek_key() == "sk-REPO"        # dev convenience still works

    monkeypatch.setenv("ECHELON_KEYS_ISOLATED", "1")
    with pytest.raises(ValueError):
        keys.load_deepseek_key()                        # gateway mode: contained


def test_seeding_a_home_never_hoists_provider_keys_into_the_process(tmp_path, monkeypatch):
    """THE MIRROR LEAK: os.environ is process-global, so seeding a member's home used
    to publish THAT MEMBER's keys into the server env (setdefault), where they outlived
    the request and became the de-facto key for every later tenant."""
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    home = tmp_path / "member"
    home.mkdir()
    (home / ".env").write_text(
        "DEEPSEEK_API_KEY=sk-MEMBER-SECRET\nECHELON_SCOPE=member-scope\n", encoding="utf-8")
    try:
        m._ensure_home(home)
    except Exception:
        pass                      # db/schema work may fail in a bare tmp dir; irrelevant
    assert os.environ.get("DEEPSEEK_API_KEY") != "sk-MEMBER-SECRET", \
        "a member's key must never reach process-global state"


def test_http_mode_turns_on_key_isolation_without_deploy_config(monkeypatch):
    """The fix must not depend on remembering a systemd Environment= line — a
    forgotten flag would fail OPEN and silently spend the owner's key."""
    import inspect
    src = inspect.getsource(m.main)
    assert "ECHELON_KEYS_ISOLATED" in src
    i_http = src.index("if args.http")
    assert src.index("ECHELON_KEYS_ISOLATED") > i_http, \
        "isolation must be enabled on the multi-tenant (--http) path"


# ── filesystem containment ───────────────────────────────────────────────────

def test_member_cannot_ingest_from_outside_their_home(tmp_path):
    """THE OTHER CRITICAL HOLE: --root was a raw caller string, so a member could
    plant the OWNER's private atoms into their own readable bank. ECHELON_HOME
    bounds where the bank is WRITTEN, never what the read can REACH."""
    member_home = tmp_path / "member"
    member_home.mkdir()
    victim = tmp_path / "owner_estate" / "memory"
    victim.mkdir(parents=True)
    _be_member(member_home)

    with pytest.raises(ValueError, match="outside your bank"):
        m._t_ingest({"root": str(victim)})
    with pytest.raises(ValueError, match="outside your bank"):
        m._t_check({"root": str(victim)})


def test_member_cannot_escape_with_dot_dot(tmp_path):
    member_home = tmp_path / "member"
    (member_home / "memory").mkdir(parents=True)
    _be_member(member_home)
    with pytest.raises(ValueError, match="outside your bank"):
        m._t_ingest({"root": str(member_home / "memory" / ".." / ".." / "elsewhere")})


def test_member_may_ingest_inside_their_own_home(tmp_path, monkeypatch):
    seen = {}
    monkeypatch.setattr(m, "_run_echelon",
                        lambda args, timeout=180: seen.setdefault("args", args) or "ok")
    member_home = tmp_path / "member"
    (member_home / "memory").mkdir(parents=True)
    _be_member(member_home)
    m._t_ingest({"root": str(member_home / "memory")})
    assert "--root" in seen["args"]


def test_member_ingest_scope_is_forced_to_their_own(tmp_path, monkeypatch):
    """A caller-supplied scope must not let a member label content as another estate."""
    seen = {}
    monkeypatch.setattr(m, "_run_echelon",
                        lambda args, timeout=180: seen.setdefault("args", args) or "ok")
    member_home = tmp_path / "member"
    (member_home / "memory").mkdir(parents=True)
    _be_member(member_home)                      # scope == "member-scope"
    m._t_ingest({"root": str(member_home / "memory"), "scope": "echelon"})
    assert "echelon" not in seen["args"]
    assert "member-scope" in seen["args"]


def test_member_cannot_clone_cartridges_from_another_bank(tmp_path):
    """from_bank is a BANK PATH — uncontained it is a cross-tenant read wearing a
    provisioning verb's clothes."""
    member_home = tmp_path / "member"
    member_home.mkdir()
    owner_bank = tmp_path / "owner_bank"
    owner_bank.mkdir()
    _be_member(member_home)
    with pytest.raises(ValueError, match="outside your bank"):
        m._t_clone_cartridges({"from_bank": str(owner_bank)})


def test_member_clone_destination_is_forced_to_own_home(tmp_path, monkeypatch):
    seen = {}
    monkeypatch.setattr(m, "_run_echelon",
                        lambda args, timeout=180: seen.setdefault("args", args) or "ok")
    member_home = tmp_path / "member"
    member_home.mkdir()
    _be_member(member_home)
    m._t_clone_cartridges({"to_bank": "/root/.echelon"})     # attacker-controlled
    i = seen["args"].index("--to")
    assert str(member_home.resolve()) == seen["args"][i + 1]


def test_member_cannot_group_a_scope_they_do_not_own(tmp_path):
    """Atlas edges ARE the cross-scope recall mechanism."""
    _be_member(tmp_path)                       # scope == "member-scope"
    out = m._t_group_scope({"parent": "echelon", "members": ["member-scope"], "apply": True})
    assert out.startswith("ERROR")
    assert "not yours" in out


def test_member_may_group_scopes_under_their_own(tmp_path, monkeypatch):
    seen = {}
    monkeypatch.setattr(m, "_run_echelon",
                        lambda args, timeout=180: seen.setdefault("args", args) or "ok")
    _be_member(tmp_path)
    m._t_group_scope({"parent": "member-scope", "members": ["member-scope-notes"]})
    assert "group-scope" in seen["args"]


def test_member_resolve_scope_ignores_an_explicit_cwd_probe(tmp_path, monkeypatch):
    """resolve-scope is a directory-existence + scope-name oracle; a member must not
    be able to point it at the owner's estate."""
    seen = {}
    monkeypatch.setattr(m, "_run_echelon",
                        lambda args, timeout=180: seen.setdefault("args", args) or "{}")
    _be_member(tmp_path)
    m._t_resolve_scope({"cwd": "/root/.echelon"})
    i = seen["args"].index("--cwd")
    assert seen["args"][i + 1] == str(tmp_path)
    assert "/root/.echelon" not in seen["args"]


def test_member_cannot_aim_scribe_at_another_estate(tmp_path):
    """WORSE than the ingest hole: scribe's `target` becomes an agent SANDBOX with
    read/write/edit/bash. Found by the skeptic re-gate — the first fix pass contained
    ingest/check/clone and missed this one."""
    member_home = tmp_path / "member"
    member_home.mkdir()
    victim = tmp_path / "owner_estate"
    victim.mkdir()
    _be_member(member_home)
    with pytest.raises(ValueError, match="outside your bank"):
        m._t_scribe({"target": str(victim), "goal": "document everything"})


def test_member_cannot_ground_a_swarm_on_another_estates_files(tmp_path):
    member_home = tmp_path / "member"
    member_home.mkdir()
    victim = tmp_path / "owner_estate"
    victim.mkdir()
    _be_member(member_home)
    with pytest.raises(ValueError, match="outside your bank"):
        m._t_swarm_subject({"subject": "audit", "files": [str(victim)]})


def test_member_cannot_write_the_graph_outside_their_home(tmp_path):
    member_home = tmp_path / "member"
    member_home.mkdir()
    _be_member(member_home)
    with pytest.raises(ValueError, match="outside your bank"):
        m._t_graph({"out": str(tmp_path / "elsewhere" / "atlas.html")})


def test_member_graph_write_inside_home_is_allowed(tmp_path, monkeypatch):
    seen = {}
    monkeypatch.setattr(m, "_run_echelon",
                        lambda args, timeout=180: seen.setdefault("args", args) or "ok")
    member_home = tmp_path / "member"
    member_home.mkdir()
    _be_member(member_home)
    m._t_graph({"out": str(member_home / "atlas.html")})     # file need not exist yet
    assert "--out" in seen["args"]


def test_member_vision_rejects_file_urls(tmp_path):
    """A screenshot verb pointed at file:// is a local-file reader."""
    _be_member(tmp_path)
    out = m._t_vision({"url": "file:///root/.echelon/.env"})
    assert out.startswith("ERROR") and "http" in out


def test_default_scope_prefers_the_request_over_the_server(tmp_path, monkeypatch):
    """THE CENTRAL BUG behind the whole class: _DEFAULT_SCOPE read the process env
    (the SERVER's scope), so every verb defaulting to it targeted the owner's scope."""
    monkeypatch.setenv("ECHELON_SCOPE", "echelon")
    _be_member(tmp_path)                        # request scope == "member-scope"
    assert m._DEFAULT_SCOPE() == "member-scope"


def test_member_cannot_wrap_from_a_spec_outside_their_home(tmp_path):
    """`spec_path` is read off disk by wrap.py — and it is UNDOCUMENTED (absent from
    the tool's published schema), while _dispatch passes the raw arguments dict with
    no schema filtering. A schema- or name-based audit cannot see this parameter; the
    handler body is the only source of truth for what a tool actually accepts."""
    member_home = tmp_path / "member"
    member_home.mkdir()
    victim = tmp_path / "owner_estate" / "echelon.db"
    victim.parent.mkdir(parents=True)
    victim.write_text("{}", encoding="utf-8")
    _be_member(member_home)
    with pytest.raises(ValueError, match="outside your bank"):
        m._t_wrap({"lessons": ["x"], "spec_path": str(victim)})


def test_no_tool_accepts_an_undocumented_parameter(tmp_path):
    """The REGRESSION GUARD for the class: every key a handler reads must appear in
    its published schema, so the next reviewer's audit can actually see it."""
    import inspect
    import re as _re

    offenders = {}
    for name, spec in m._TOOLS.items():
        fn = spec.get("handler")
        if not fn:
            continue
        try:
            src = inspect.getsource(fn)
        except (OSError, TypeError):
            continue
        used = (set(_re.findall(r"a\[[\"']([a-z_]+)[\"']\]", src))
                | set(_re.findall(r"a\.get\([\"']([a-z_]+)[\"']", src)))
        declared = set((spec.get("schema") or spec.get("inputSchema")
                        or {}).get("properties", {}))
        extra = used - declared
        if extra:
            offenders[name] = sorted(extra)
    assert not offenders, f"undocumented tool parameters: {offenders}"


def test_member_wrap_scope_is_their_own_not_the_servers(tmp_path, monkeypatch):
    """_t_wrap read os.environ directly, bypassing the _DEFAULT_SCOPE central fix, so
    a member's atoms were labelled under the OWNER's scope name in their own bank."""
    monkeypatch.setenv("ECHELON_SCOPE", "echelon")
    seen = {}
    monkeypatch.setattr(m, "_run_echelon",
                        lambda args, timeout=180: seen.setdefault("args", args) or "ok")
    member_home = tmp_path / "member"
    member_home.mkdir()
    spec = member_home / "spec.json"
    spec.write_text("{}", encoding="utf-8")
    _be_member(member_home)                      # scope == "member-scope"
    m._t_wrap({"spec_path": str(spec), "scope": "echelon"})
    i = seen["args"].index("--scope")
    assert seen["args"][i + 1] == "member-scope"


def test_owner_ingest_is_unconstrained(tmp_path, monkeypatch):
    seen = {}
    monkeypatch.setattr(m, "_run_echelon",
                        lambda args, timeout=180: seen.setdefault("args", args) or "ok")
    _be_owner()
    m._t_ingest({"root": str(tmp_path), "scope": "echelon"})
    assert "echelon" in seen["args"]


def test_owner_request_env_keeps_keys(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-owner-key")
    _be_owner()
    assert m._request_env().get("DEEPSEEK_API_KEY") == "sk-owner-key"


# ── honest guards ────────────────────────────────────────────────────────────

def test_no_key_refusal_names_the_providers_and_does_not_bill(tmp_path):
    _be_member(tmp_path)                      # empty .env
    msg = m._capability_guard("echelon_brainstorm")
    assert msg and "no provider key is configured" in msg
    assert "set-key" in msg
    assert "owner's\nkeys are never used" in msg or "never used for member" in msg


def test_deepseek_key_funds_brainstorm(tmp_path):
    _be_member(tmp_path, "DEEPSEEK_API_KEY=sk-his-own\n")
    assert m._capability_guard("echelon_brainstorm") is None


def test_deepseek_key_does_NOT_pretend_to_fund_vision(tmp_path):
    """THE HONESTY CASE the owner named: DeepSeek has no vision model, so a user
    holding a DeepSeek key must be told that — not the false 'you have no key'."""
    _be_member(tmp_path, "DEEPSEEK_API_KEY=sk-his-own\n")
    msg = m._capability_guard("echelon_vision")
    assert msg is not None
    assert "multimodal" in msg.lower()
    assert "deepseek" in msg.lower()
    assert "no provider key is configured" not in msg, \
        "must not claim the user has no key when they do"


def test_gemini_key_funds_vision(tmp_path):
    _be_member(tmp_path, "GEMINI_API_VERTEX=AQ.some-key\n")
    assert m._capability_guard("echelon_vision") is None


def test_owner_is_never_capability_guarded():
    _be_owner()
    for name in sorted(m._BUDGET_TOOLS):
        assert m._capability_guard(name) is None


def test_free_verbs_are_never_guarded(tmp_path):
    _be_member(tmp_path)                      # no keys at all
    for name in ("echelon_recall", "echelon_remember", "echelon_wrap",
                 "echelon_ingest", "echelon_scan"):
        assert m._capability_guard(name) is None, f"{name} costs nothing; never guard it"


# ── the one real leak, fixed in the verb ─────────────────────────────────────

def test_resolve_scope_for_a_member_never_uses_the_server_cwd(tmp_path, monkeypatch):
    seen = {}

    def _fake_run(args, timeout=180):
        seen["args"] = args
        return "{}"

    monkeypatch.setattr(m, "_run_echelon", _fake_run)
    _be_member(tmp_path)
    m._t_resolve_scope({})                     # no cwd supplied
    assert "--cwd" in seen["args"], "a member must never fall through to the server cwd"
    assert str(tmp_path) in seen["args"]


def test_resolve_scope_for_owner_may_use_the_server_cwd(tmp_path, monkeypatch):
    seen = {}
    monkeypatch.setattr(m, "_run_echelon",
                        lambda args, timeout=180: seen.setdefault("args", args) or "{}")
    _be_owner()
    m._t_resolve_scope({})
    assert "--cwd" not in seen["args"]
