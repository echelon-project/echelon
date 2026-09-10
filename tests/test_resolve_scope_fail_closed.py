"""resolve_scope fails CLOSED on an unknown directory (OPEN-0036).

The old step 6 MINTED the leaf kebab as a fresh bank scope, so a typo'd cd, a nested
leaf dir, or a detached worktree silently created a scope nobody meant to exist. Now an
unowned dir raises UnknownScopeError, and the mint is an EXPLICIT door (allow_new=True).

Every test pins `live_scopes` so nothing here touches the real bank.
"""
from __future__ import annotations

import pytest

from echelon_engine.atoms.resolve_scope import (
    UnknownScopeError,
    resolve_scope,
    resolve_scope_root,
)

LIVE = {"echelon", "mol", "alpha-app"}


@pytest.fixture
def banked_repo(tmp_path):
    """A dir whose kebab IS a live scope and which carries a project-root marker."""
    d = tmp_path / "alpha-app"
    d.mkdir()
    (d / ".git").mkdir()
    return d


class TestFailsClosed:
    def test_unknown_dir_raises_instead_of_minting(self, tmp_path):
        d = tmp_path / "totally-unbanked-project"
        d.mkdir()
        with pytest.raises(UnknownScopeError):
            resolve_scope(str(d), live_scopes=LIVE)

    def test_error_carries_the_cwd_and_the_would_be_scope(self, tmp_path):
        d = tmp_path / "totally-unbanked-project"
        d.mkdir()
        with pytest.raises(UnknownScopeError) as ei:
            resolve_scope(str(d), live_scopes=LIVE)
        assert ei.value.candidate == "totally-unbanked-project"
        assert "totally-unbanked-project" in ei.value.cwd
        # the message must name the explicit door, or the caller cannot act on it
        assert "allow_new" in str(ei.value)

    def test_it_is_a_lookup_error(self, tmp_path):
        """Callers that already catch LookupError/Exception keep degrading, not crashing."""
        d = tmp_path / "unbanked"
        d.mkdir()
        with pytest.raises(LookupError):
            resolve_scope(str(d), live_scopes=LIVE)

    def test_a_nested_leaf_under_an_unbanked_root_also_raises(self, tmp_path):
        """The scope-leak shape: web/ under a repo nobody banked. Must NOT mint 'web'."""
        d = tmp_path / "some-app" / "web"
        d.mkdir(parents=True)
        with pytest.raises(UnknownScopeError):
            resolve_scope(str(d), live_scopes=LIVE)


class TestExplicitCreateDoor:
    def test_allow_new_mints_the_leaf_kebab(self, tmp_path):
        d = tmp_path / "brand-new-project"
        d.mkdir()
        assert resolve_scope(str(d), live_scopes=LIVE, allow_new=True) == "brand-new-project"

    def test_allow_new_does_not_bypass_the_normal_resolution_order(self, banked_repo):
        """The door only affects step 6 — a dir a live scope already owns still resolves
        to that scope, never to a fresh mint."""
        assert resolve_scope(str(banked_repo), live_scopes=LIVE, allow_new=True) == "alpha-app"

    def test_allow_new_defaults_to_false(self, tmp_path):
        """Mutation guard: if the default ever flips to True, this test fails."""
        d = tmp_path / "unbanked"
        d.mkdir()
        with pytest.raises(UnknownScopeError):
            resolve_scope(str(d), live_scopes=LIVE)   # no allow_new kwarg at all


class TestKnownScopesStillResolve:
    def test_irregular_map_still_wins(self, tmp_path):
        """The irregular map short-circuits before live_scopes. The public engine ships
        only its OWN entry (estate-specific aliases belong in ~/.echelon/mem_dirs.json),
        so this asserts the mechanism using the entry that is actually shipped."""
        d = tmp_path / "echelon-agent"
        d.mkdir()
        assert resolve_scope(str(d), live_scopes=LIVE) == "echelon"

    def test_irregular_map_needs_no_live_bank(self, tmp_path):
        """An irregular mapping resolves before live_scopes is consulted, so it must
        survive an EMPTY bank rather than fail closed."""
        d = tmp_path / "echelon-agent"
        d.mkdir()
        assert resolve_scope(str(d), live_scopes=set()) == "echelon"

    def test_live_leaf_that_is_a_project_root(self, banked_repo):
        assert resolve_scope(str(banked_repo), live_scopes=LIVE) == "alpha-app"

    def test_ancestor_walk_still_routes_a_nested_leaf(self, banked_repo):
        nested = banked_repo / "web" / "pages"
        nested.mkdir(parents=True)
        assert resolve_scope(str(nested), live_scopes=LIVE) == "alpha-app"


class TestDeclaredMemDir:
    """OPEN-0052: a memory folder resolves to the scope that DECLARES it in mem_dirs.json.

    This replaces the old hardcoded `d-work-echelon -> echelon` exception with a general,
    data-driven rule: the owner adds a stray memory folder to its scope's mem_dirs.json list
    and it resolves — no code exception. Every test pins its own config, never the live one.
    """

    @pytest.fixture
    def cfg(self, tmp_path):
        """A mem_dirs.json declaring an OUT-OF-TREE memory folder for scope 'echelon' —
        the shape of the real d--WORK-ECHELON split (a memory dir far from its estate)."""
        import json
        stray = tmp_path / "elsewhere" / "d--WORK-ECHELON" / "memory"
        stray.mkdir(parents=True)
        p = tmp_path / "mem_dirs.json"
        p.write_text(json.dumps({
            "echelon": [str(stray).replace("\\", "/")],
            "gamma-support": [str(tmp_path / "gamma-support" / "memory").replace("\\", "/")],
        }), encoding="utf-8")
        return str(p), stray

    def test_declared_dir_resolves_to_its_scope(self, cfg):
        """The old exception's exact case: a memory folder whose leaf kebab is NOT its scope
        resolves via the declaration, not a hardcoded map entry."""
        config_path, stray = cfg
        assert resolve_scope(str(stray), live_scopes=set(), config_path=config_path) == "echelon"

    def test_a_subdir_of_a_declared_dir_also_resolves(self, cfg):
        config_path, stray = cfg
        nested = stray / "_atoms"
        nested.mkdir()
        assert resolve_scope(str(nested), live_scopes=set(), config_path=config_path) == "echelon"

    def test_declared_dir_survives_an_empty_bank(self, cfg):
        """The property the hardcoded exception guaranteed: it resolves BEFORE live_scopes is
        consulted, so an empty/absent bank never breaks the estate's own ingest."""
        config_path, stray = cfg
        assert resolve_scope(str(stray), live_scopes=set(), config_path=config_path) == "echelon"

    def test_general_case_any_scope_not_just_echelon(self, cfg):
        """The rule is general — it is not special-cased to echelon. Any declared folder
        resolves to its declaring scope."""
        config_path, _ = cfg
        import os
        estate_root = os.path.dirname(config_path)  # tmp_path
        target = os.path.join(estate_root, "gamma-support", "memory")
        os.makedirs(target, exist_ok=True)
        assert resolve_scope(target, live_scopes=set(), config_path=config_path) == "gamma-support"

    def test_longest_declared_dir_wins(self, tmp_path):
        """Nested declarations: the most specific (longest) declared dir owns the cwd."""
        import json
        base = (tmp_path / "shared" / "memory")
        sub = base / "cartridge"
        sub.mkdir(parents=True)
        p = tmp_path / "cfg.json"
        p.write_text(json.dumps({
            "parent": [str(base).replace("\\", "/")],
            "child": [str(sub).replace("\\", "/")],
        }), encoding="utf-8")
        assert resolve_scope(str(sub), live_scopes=set(), config_path=str(p)) == "child"
        assert resolve_scope(str(base), live_scopes=set(), config_path=str(p)) == "parent"

    def test_no_config_falls_through_to_fail_closed(self, tmp_path):
        """Absent config must not change the fail-closed contract for an unknown dir."""
        missing = str(tmp_path / "does-not-exist.json")
        d = tmp_path / "unbanked"
        d.mkdir()
        with pytest.raises(UnknownScopeError):
            resolve_scope(str(d), live_scopes=set(), config_path=missing)

    def test_declaration_does_not_override_the_irregular_map(self, tmp_path):
        """A repo-dir irregular mapping still wins (it is checked first) — the two rules
        never fight because they cover different kinds of dir."""
        import json
        p = tmp_path / "cfg.json"
        # even if someone mis-declares echelon-agent under a wrong scope, the irregular
        # repo-dir map resolves it first.
        d = tmp_path / "echelon-agent"
        d.mkdir()
        p.write_text(json.dumps({"wrongscope": [str(d).replace("\\", "/")]}), encoding="utf-8")
        assert resolve_scope(str(d), live_scopes=set(), config_path=str(p)) == "echelon"


class TestResolveScopeRoot:
    def test_unbanked_dir_is_its_own_root_not_an_exception(self, tmp_path):
        """resolve_scope_root is documented to answer for a fresh un-banked project, so it
        opens the create door internally and must NOT propagate UnknownScopeError."""
        d = tmp_path / "fresh-thing"
        d.mkdir()
        got = resolve_scope_root(str(d), live_scopes=LIVE)
        assert got.replace("\\", "/").lower().endswith("fresh-thing")

    def test_banked_repo_resolves_to_the_repo_root(self, banked_repo):
        nested = banked_repo / "web"
        nested.mkdir()
        got = resolve_scope_root(str(nested), live_scopes=LIVE)
        assert got.replace("\\", "/").lower().endswith("alpha-app")
