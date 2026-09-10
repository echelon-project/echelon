"""Gateway sync routes (wave 3) — /sync/hello, /sync/pull, /sync/push.

The route bodies are inside serve()'s closure, so these tests exercise the two module-level
helpers that carry the security-critical decisions (_sync_identity, _sync_open) plus the
end-to-end merge behaviour through sync_journal, which is what the routes actually call.

THE LOAD-BEARING PROPERTY (cross-tenant-leaks-hide-in-in-process-writes): /sync/push is an
IN-PROCESS WRITE on a multi-user gateway — the exact shape that leaked into the owner bank in
the 2026-07-02 build. It must open the CALLER'S OWN bank, resolved from their token, never the
module-global default. A member must never be able to read or write another identity's scope.
"""
import json
import os
from pathlib import Path

import pytest

from echelon_engine.atoms.cards import CardStore
from echelon_engine.atoms import sync_journal as sj


@pytest.fixture(autouse=True)
def _sync_on(monkeypatch):
    monkeypatch.setenv("ECHELON_SYNC", "1")


@pytest.fixture
def gateway(tmp_path, monkeypatch):
    """A gateway home with an owner bank and one registered member."""
    import echelon_engine.mcp_server as m
    from echelon_engine.atoms import mcp_users

    home = tmp_path / "gwhome"
    (home / "banks").mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("ECHELON_HOME", str(home))
    monkeypatch.setenv("ECHELON_SCOPE", "echelon")
    monkeypatch.setattr(mcp_users, "_root_dir", lambda: home / "banks")
    monkeypatch.setattr(mcp_users, "registry_path", lambda: home / "mcp_users.json")

    rec = mcp_users.add_user(db="tenantb", scope="tenantb", email="b@x.com")
    # reset the per-request thread-local between tests
    for attr in ("home", "scope", "role", "is_owner"):
        setattr(m._REQ, attr, None)
    return m, home, rec


class _H(dict):
    """Minimal headers object: .get() is all the helpers use."""


def _hdrs(token=None):
    return _H({"Authorization": f"Bearer {token}"} if token else {})


# ── identity resolution: the cross-tenant boundary ────────────────────────────

def test_member_token_resolves_to_their_own_home(gateway, monkeypatch):
    m, home, rec = gateway
    monkeypatch.setenv("ECHELON_MCP_TOKEN", "the-owner-token")
    ident = m._sync_identity(_hdrs(rec["token"]))
    assert ident is not None
    assert ident["scope"] == "tenantb"
    assert Path(ident["home"]) == Path(rec["home"])
    assert "gwhome" in str(ident["home"]), "member home must live under the gateway root"


# ── rotate: fresh token, old dies ─────────────────────────────────────────────

def test_rotate_issues_fresh_token_and_kills_old(gateway):
    from echelon_engine.atoms import mcp_users
    m, home, rec = gateway
    old = rec["token"]
    new = mcp_users.rotate_user(rec["db"])
    assert new["token"] != old
    assert mcp_users.resolve(old) is None, "old token must be dead after rotate"
    assert mcp_users.resolve(new["token"])["db"] == rec["db"]


def test_rotate_preserves_scope_email_role(gateway):
    from echelon_engine.atoms import mcp_users
    m, home, rec = gateway
    new = mcp_users.rotate_user(rec["db"])
    assert new["scope"] == rec["scope"]
    assert new["email"] == rec["email"]
    assert new["role"] == rec["role"]
    assert new["home"] == rec["home"]


def test_rotate_unknown_db_raises(gateway):
    from echelon_engine.atoms import mcp_users
    m, home, rec = gateway
    with pytest.raises(ValueError):
        mcp_users.rotate_user(db="nobody-registered")


def test_rotate_by_email(gateway):
    from echelon_engine.atoms import mcp_users
    m, home, rec = gateway
    new = mcp_users.rotate_user(email=rec["email"])
    assert new["token"] != rec["token"]
    assert new["db"] == rec["db"]
    assert new["email"] == rec["email"]
    assert mcp_users.resolve(rec["token"]) is None, "old token must be dead after rotate"


def test_rotate_unknown_email_raises(gateway):
    from echelon_engine.atoms import mcp_users
    m, home, rec = gateway
    with pytest.raises(ValueError):
        mcp_users.rotate_user(email="nobody@nowhere.invalid")


def test_rotate_requires_a_selector(gateway):
    from echelon_engine.atoms import mcp_users
    m, home, rec = gateway
    with pytest.raises(ValueError):
        mcp_users.rotate_user()


def test_rotate_via_services_gate(gateway):
    from echelon_engine import services
    m, home, rec = gateway
    new = services.rotate_mcp_user(rec["db"])
    assert new["token"] != rec["token"]
    assert services.mcp_user_exists(rec["db"])
    assert services.mcp_user_by_email(rec["email"])["db"] == rec["db"]


def test_identity_comes_from_the_token_not_the_thread_local(gateway, monkeypatch):
    """_REQ is pinned in do_POST ONLY. A do_GET route (/sync/hello, /sync/pull) would
    otherwise see nothing — or, since worker threads are REUSED, a previous request's
    identity, which is a cross-tenant read. Caught by a live 403 on box4, 2026-08-02."""
    m, home, rec = gateway
    monkeypatch.setenv("ECHELON_MCP_TOKEN", "the-owner-token")
    # simulate a thread still carrying the PREVIOUS caller's pinned state
    m._REQ.home = str(home / "banks" / "someone-else")
    m._REQ.scope = "someone-else"
    m._REQ.is_owner = True

    ident = m._sync_identity(_hdrs(rec["token"]))
    assert ident["scope"] == "tenantb", "stale thread-local leaked into identity"
    assert "someone-else" not in str(ident["home"])

    # and with no _REQ pinned at all (the plain GET case), the member still resolves
    for attr in ("home", "scope", "role", "is_owner"):
        setattr(m._REQ, attr, None)
    assert m._sync_identity(_hdrs(rec["token"]))["scope"] == "tenantb"


def test_owner_token_is_never_shadowed_by_a_registry_row(gateway, monkeypatch):
    """A registry entry must not be able to claim the owner's identity."""
    m, home, rec = gateway
    monkeypatch.setenv("ECHELON_MCP_TOKEN", "the-owner-token")
    assert m._sync_identity(_hdrs("the-owner-token"))["who"] == "owner"
    assert m._sync_identity(_hdrs(rec["token"]))["who"] == "tenantb"


def test_member_home_is_not_the_owner_bank(gateway, monkeypatch):
    """THE leak shape: a member's write landing in the owner bank.

    Compare against the SERVER's own home (what the owner resolves to), not _current_home()
    while a member is pinned — that would compare the member's bank with itself and pass
    vacuously."""
    m, home, rec = gateway
    monkeypatch.setenv("ECHELON_MCP_TOKEN", "the-owner-token")
    owner_db = (Path(m._sync_identity(_hdrs("the-owner-token"))["home"]) / "echelon.db").resolve()
    member_db = (Path(m._sync_identity(_hdrs(rec["token"]))["home"]) / "echelon.db").resolve()

    assert member_db != owner_db, "member sync would write the OWNER bank"
    assert "tenantb" in str(member_db)


def test_module_default_bank_is_never_the_sync_target(gateway):
    """The precise mechanism of the 2026-07-02 leak: an in-process write opening the
    import-bound DEFAULT_V2_DB instead of the caller's home."""
    m, home, rec = gateway
    from echelon_engine.atoms.cards import DEFAULT_V2_DB
    m._REQ.home = rec["home"]
    m._REQ.is_owner = False
    ident = m._sync_identity(_hdrs(rec["token"]))
    target = (Path(ident["home"]) / "echelon.db").resolve()
    assert target != Path(DEFAULT_V2_DB).resolve(), \
        "sync would write the module-global default bank"


def test_owner_resolves_to_the_server_home(gateway, monkeypatch):
    m, home, rec = gateway
    monkeypatch.setenv("ECHELON_MCP_TOKEN", "the-owner-token")
    ident = m._sync_identity(_hdrs("the-owner-token"))
    assert ident["who"] == "owner"
    assert ident["scope"] == "echelon"


def test_unknown_token_gets_no_identity(gateway, monkeypatch):
    m, home, rec = gateway
    monkeypatch.setenv("ECHELON_MCP_TOKEN", "the-owner-token")
    assert m._sync_identity(_hdrs("not-a-real-token")) is None


def test_sync_open_creates_and_targets_the_identitys_bank(gateway, monkeypatch):
    m, home, rec = gateway
    monkeypatch.setenv("ECHELON_MCP_TOKEN", "the-owner-token")
    ident = m._sync_identity(_hdrs(rec["token"]))
    conn = m._sync_open(ident)
    try:
        got = conn.execute("PRAGMA database_list").fetchall()
        path = Path([r[2] for r in got if r[1] == "main"][0]).resolve()
        assert path == (Path(rec["home"]) / "echelon.db").resolve()
    finally:
        conn.close()


# ── the exchange itself ───────────────────────────────────────────────────────

def test_push_applies_only_the_callers_scope(tmp_path):
    """Server-side scope filter: an op labelled for another estate must not be applied,
    even if a buggy or hostile client includes it in the batch."""
    src = CardStore(tmp_path / "src.db")
    dst = CardStore(tmp_path / "dst.db")
    mine = src.add_atom("lesson:mine", "# mine\nours", scope="tenantb")
    other = src.add_atom("lesson:other", "# other\nsomeone else's estate", scope="alpha-app")
    ops = [o for o in sj.ops_since(src.conn) if o["tbl"] == "atoms"]
    assert len(ops) == 2

    # what the route does before applying
    scope = "tenantb"
    filtered = [o for o in ops if str(o.get("scope") or "") == scope]
    sj.apply_ops(dst.conn, filtered, origin="peer")

    assert dst.conn.execute("SELECT COUNT(*) FROM atoms WHERE id=?", (mine,)).fetchone()[0] == 1
    assert dst.conn.execute("SELECT COUNT(*) FROM atoms WHERE id=?", (other,)).fetchone()[0] == 0


def test_hello_reports_generation_head_and_engine(tmp_path):
    store = CardStore(tmp_path / "h.db")
    store.add_atom("lesson:h", "# h\nclaim", scope="echelon")
    from echelon_engine.atoms.export_import import _engine_version
    payload = {"generation": sj.generation(store.conn), "head": sj.head(store.conn),
               "engine": _engine_version()}
    assert len(payload["generation"]) == 32
    assert payload["head"] >= 1
    assert payload["engine"]


def test_pull_is_scope_filtered_server_side(tmp_path):
    store = CardStore(tmp_path / "p.db")
    store.add_atom("lesson:a", "# a\nclaim", scope="tenantb")
    store.add_atom("lesson:b", "# b\nclaim", scope="alpha-app")
    got = sj.ops_since(store.conn, 0, scope="tenantb")
    assert got and all(o["scope"] == "tenantb" for o in got)
    assert not any(o["scope"] == "alpha-app" for o in got)


def test_push_cursor_advances_only_through_applied_ops(tmp_path):
    """ack-after-apply: `through` is the max seq actually in the batch."""
    src = CardStore(tmp_path / "s2.db")
    dst = CardStore(tmp_path / "d2.db")
    for i in range(3):
        src.add_atom(f"lesson:{i}", f"# {i}\nclaim", scope="echelon")
    ops = sj.ops_since(src.conn, 0, scope="echelon")
    sj.apply_ops(dst.conn, ops, origin="peer")
    through = max(int(o["seq"]) for o in ops)
    sj.set_peer_state(dst.conn, "peer:x", peer_generation="g", pushed_through=through)
    assert sj.peer_state(dst.conn, "peer:x")["pushed_through"] == through


# ── version detection (the ship signal) ───────────────────────────────────────

def test_version_verdicts():
    from echelon_engine.atoms import sync_cmd
    assert sync_cmd._version_verdict("1.2", "1.2")[0] == "same"
    assert sync_cmd._version_verdict("1.2", "1.3")[0] == "drift"
    assert sync_cmd._version_verdict("1.2", "")[0] == "unknown"
    assert sync_cmd._version_verdict("1.2", "unknown")[0] == "unknown"


def test_drift_is_reported_not_auto_deployed():
    """Version drift must be a REPORT. Auto-deploying because a string differed is exactly
    the unattended action that should stay a human decision."""
    from echelon_engine.atoms import sync_cmd
    state, line = sync_cmd._version_verdict("1.2", "1.3")
    assert state == "drift"
    assert "ship" in line.lower()
    # The client may READ a version (git describe), but must never PUSH code: no file copy,
    # no service control, no shell-out beyond the read-only version probe.
    src = Path(sync_cmd.__file__).read_text(encoding="utf-8")
    for forbidden in ("scp ", "os.system", "systemctl", "rsync", "git push", "git pull"):
        assert forbidden not in src, f"sync client must not deploy code ({forbidden})"
    subproc_calls = [ln.strip() for ln in src.splitlines() if "subprocess.run" in ln]
    assert len(subproc_calls) == 1 and "git" in subproc_calls[0] and "describe" in subproc_calls[0], \
        f"the only shell-out may be the read-only version probe, got: {subproc_calls}"
