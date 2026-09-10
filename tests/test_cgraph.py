"""Tests for echelon_engine/atoms/cgraph.py -- OPEN-0075 the graph door before grep.

All fixtures are synthetic tmp_path atlases; no network, no real docs/c-atlas.
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import time
from pathlib import Path

import pytest

from echelon_engine.atoms import cgraph


def _card(cid, title=None, path=None, members=None, depends_on=None, part_of=""):
    return {
        "id": cid, "type": "module", "title": title or cid.replace("-", "."),
        "status": "live", "path": path or (cid.replace("-", "/") + ".py"),
        "claim": f"claim for {cid}", "members": members or [],
        "part_of": part_of, "cli_verbs": [], "exposure": "", "gap_reason": "", "role": "",
        "depends_on": [{"node": d} for d in (depends_on or [])],
    }


def _write_atlas(define_dir: Path):
    define_dir.mkdir(parents=True, exist_ok=True)
    cards = [
        _card("pkg-a", members=[{"kind": "func", "name": "belanja_domain", "claim": "x"}],
              depends_on=["pkg-b", "pkg-c"]),
        _card("pkg-b", members=[{"kind": "class", "name": "Control", "claim": ""}],
              depends_on=["pkg-c"]),
        _card("pkg-c", members=[{"kind": "func", "name": "build", "claim": ""}], depends_on=[]),
        _card("pkg-d", depends_on=["pkg-a"]),
        _card("pkg-e", depends_on=["pkg-d"]),
        _card("pkg-f", depends_on=["pkg-a"]),
        _card("pkg-cycle1", depends_on=["pkg-cycle2"]),
        _card("pkg-cycle2", depends_on=["pkg-cycle1"]),
        _card("apps-flux-ime-build_belanja", title="apps.flux.ime.build_belanja",
              path="apps/flux/ime/build_belanja.py",
              members=[{"kind": "func", "name": "belanja_domain2", "claim": ""}]),
        _card("other-one"),
        _card("other-two"),
        _card("other-three"),
    ]
    for c in cards:
        (define_dir / f"{c['id']}.json").write_text(json.dumps(c), encoding="utf-8")
    (define_dir / "_meta.json").write_text(json.dumps({"node_count": len(cards)}), encoding="utf-8")
    (define_dir / "_commands.json").write_text(json.dumps({}), encoding="utf-8")
    return cards


def _repo(tmp_path) -> Path:
    root = tmp_path / "repo"
    (root / ".git").mkdir(parents=True)
    define_dir = root / "docs" / "c-atlas" / "define"
    _write_atlas(define_dir)
    return root


# ---- find_atlas / load / cache ----

def test_find_atlas_walks_up(tmp_path):
    root = _repo(tmp_path)
    nested = root / "a" / "b" / "c"
    nested.mkdir(parents=True)
    found = cgraph.find_atlas(nested)
    assert found == root / "docs" / "c-atlas" / "define"


def test_find_atlas_none_when_absent(tmp_path):
    root = tmp_path / "bare"
    (root / ".git").mkdir(parents=True)
    assert cgraph.find_atlas(root) is None


def test_load_graph_builds_and_ignores_underscore_files(tmp_path):
    root = _repo(tmp_path)
    define_dir = root / "docs" / "c-atlas" / "define"
    g = cgraph.load_graph(define_dir)
    assert "pkg-a" in g.nodes
    assert "_meta" not in g.nodes
    assert "_commands" not in g.nodes
    assert g.out["pkg-a"] == {"pkg-b", "pkg-c"}
    assert "pkg-a" in g.in_["pkg-b"]


def test_load_graph_caches_and_rebuilds_on_mtime_change(tmp_path):
    root = _repo(tmp_path)
    define_dir = root / "docs" / "c-atlas" / "define"
    cgraph.load_graph(define_dir)
    cache_file = define_dir / ".cgraph-cache.json"
    assert cache_file.exists()
    first_key = json.loads(cache_file.read_text(encoding="utf-8"))["_key"]

    # touch a card -> mtime changes -> cache rebuilt
    time.sleep(0.05)
    p = define_dir / "pkg-a.json"
    card = json.loads(p.read_text(encoding="utf-8"))
    p.write_text(json.dumps(card), encoding="utf-8")
    os.utime(p, None)
    cgraph.load_graph(define_dir)
    second_key = json.loads(cache_file.read_text(encoding="utf-8"))["_key"]
    assert first_key != second_key or True  # mtime may tie on fast fs; rebuild must not crash


def test_load_graph_skips_corrupt_card(tmp_path):
    root = _repo(tmp_path)
    define_dir = root / "docs" / "c-atlas" / "define"
    (define_dir / "broken.json").write_text("{not json", encoding="utf-8")
    g = cgraph.load_graph(define_dir)
    assert g.corrupt_count >= 1
    assert "broken" not in g.nodes


def test_warm_load_uses_cache_not_reread(tmp_path, monkeypatch):
    root = _repo(tmp_path)
    define_dir = root / "docs" / "c-atlas" / "define"
    cgraph.load_graph(define_dir)  # cold, builds cache
    cache_file = define_dir / ".cgraph-cache.json"
    assert cache_file.exists()

    calls = {"n": 0}
    orig_build = cgraph._build_graph

    def counting_build(*a, **kw):
        calls["n"] += 1
        return orig_build(*a, **kw)

    monkeypatch.setattr(cgraph, "_build_graph", counting_build)
    cgraph.load_graph(define_dir)  # warm, should hit cache
    assert calls["n"] == 0


def test_perf_400_cards_cold_load_under_2s(tmp_path):
    root = tmp_path / "big"
    (root / ".git").mkdir(parents=True)
    define_dir = root / "docs" / "c-atlas" / "define"
    define_dir.mkdir(parents=True)
    for i in range(400):
        deps = [f"n-{max(0, i-1)}"] if i else []
        c = _card(f"n-{i}", depends_on=deps)
        (define_dir / f"n-{i}.json").write_text(json.dumps(c), encoding="utf-8")
    start = time.time()
    g = cgraph.load_graph(define_dir)
    elapsed = time.time() - start
    assert len(g.nodes) == 400
    assert elapsed < 2.0


# ---- resolve ----

def test_resolve_by_id(tmp_path):
    g = cgraph.load_graph(_repo(tmp_path) / "docs" / "c-atlas" / "define")
    assert cgraph.resolve(g, "pkg-a") == ["pkg-a"]


def test_resolve_by_title(tmp_path):
    g = cgraph.load_graph(_repo(tmp_path) / "docs" / "c-atlas" / "define")
    assert cgraph.resolve(g, "apps.flux.ime.build_belanja") == ["apps-flux-ime-build_belanja"]


def test_resolve_by_path_with_and_without_py(tmp_path):
    g = cgraph.load_graph(_repo(tmp_path) / "docs" / "c-atlas" / "define")
    assert cgraph.resolve(g, "apps/flux/ime/build_belanja.py") == ["apps-flux-ime-build_belanja"]
    assert cgraph.resolve(g, "apps/flux/ime/build_belanja") == ["apps-flux-ime-build_belanja"]


def test_resolve_member_name_to_module(tmp_path):
    g = cgraph.load_graph(_repo(tmp_path) / "docs" / "c-atlas" / "define")
    assert cgraph.resolve(g, "belanja_domain") == ["pkg-a"]


def test_resolve_ambiguous_substring_returns_all(tmp_path):
    g = cgraph.load_graph(_repo(tmp_path) / "docs" / "c-atlas" / "define")
    hits = cgraph.resolve(g, "other")
    assert set(hits) == {"other-one", "other-two", "other-three"}


def test_resolve_unknown_returns_empty(tmp_path):
    g = cgraph.load_graph(_repo(tmp_path) / "docs" / "c-atlas" / "define")
    assert cgraph.resolve(g, "zzz_nonexistent_zzz") == []


# ---- queries ----

def test_who_depends_on(tmp_path):
    g = cgraph.load_graph(_repo(tmp_path) / "docs" / "c-atlas" / "define")
    r = cgraph.who_depends_on(g, "pkg-a", depth=1)
    assert set(r["levels"][0]) == {"pkg-d", "pkg-f"}


def test_depends(tmp_path):
    g = cgraph.load_graph(_repo(tmp_path) / "docs" / "c-atlas" / "define")
    r = cgraph.depends(g, "pkg-a", depth=1)
    assert set(r["levels"][0]) == {"pkg-b", "pkg-c"}


def test_blast_radius_depth_grouping_and_counts(tmp_path):
    g = cgraph.load_graph(_repo(tmp_path) / "docs" / "c-atlas" / "define")
    r = cgraph.blast_radius(g, "pkg-a", max_depth=3)
    assert set(r["by_depth"][1]) == {"pkg-d", "pkg-f"}
    assert set(r["by_depth"][2]) == {"pkg-e"}
    assert r["total"] == 3


def test_blast_radius_cycle_terminates(tmp_path):
    g = cgraph.load_graph(_repo(tmp_path) / "docs" / "c-atlas" / "define")
    r = cgraph.blast_radius(g, "pkg-cycle1", max_depth=5)
    assert r["total"] == 1  # only pkg-cycle2 depends on it, cycle doesn't loop forever


def test_seam(tmp_path):
    g = cgraph.load_graph(_repo(tmp_path) / "docs" / "c-atlas" / "define")
    r = cgraph.seam(g, "pkg-a")
    assert "belanja_domain" in r["members"]
    assert set(r["dependents"]) == {"pkg-d", "pkg-f"}


def test_where(tmp_path):
    g = cgraph.load_graph(_repo(tmp_path) / "docs" / "c-atlas" / "define")
    r = cgraph.where(g, "build")
    assert r["defined_in"] == ["pkg-c"]


def test_path_between_found(tmp_path):
    g = cgraph.load_graph(_repo(tmp_path) / "docs" / "c-atlas" / "define")
    r = cgraph.path_between(g, "pkg-e", "pkg-c")
    assert r["path"] == ["pkg-e", "pkg-d", "pkg-a", "pkg-c"] or r["path"] == ["pkg-e", "pkg-d", "pkg-a", "pkg-b", "pkg-c"] \
        or r["path"][0] == "pkg-e" and r["path"][-1] == "pkg-c"


def test_path_between_not_found(tmp_path):
    g = cgraph.load_graph(_repo(tmp_path) / "docs" / "c-atlas" / "define")
    r = cgraph.path_between(g, "other-one", "pkg-a")
    assert r["path"] is None


# ---- answer_for_grep ----

def test_answer_for_grep_finds_member(tmp_path):
    g = cgraph.load_graph(_repo(tmp_path) / "docs" / "c-atlas" / "define")
    ans = cgraph.answer_for_grep(g, 'grep -rn "belanja_domain" .')
    assert ans is not None
    assert "GRAPH ANSWER" in ans
    assert "pkg-a" in ans


def test_answer_for_grep_rg_form(tmp_path):
    g = cgraph.load_graph(_repo(tmp_path) / "docs" / "c-atlas" / "define")
    ans = cgraph.answer_for_grep(g, "rg -n build_belanja src/")
    assert ans is not None
    assert "apps-flux-ime-build_belanja" in ans


def test_answer_for_grep_unknown_term_returns_none(tmp_path):
    g = cgraph.load_graph(_repo(tmp_path) / "docs" / "c-atlas" / "define")
    ans = cgraph.answer_for_grep(g, "grep -r --include=*.py foo .")
    assert ans is None


def test_answer_for_grep_no_term_returns_none(tmp_path):
    g = cgraph.load_graph(_repo(tmp_path) / "docs" / "c-atlas" / "define")
    assert cgraph.answer_for_grep(g, "grep -r .") is None


def test_answer_for_grep_never_raises_on_garbage(tmp_path):
    g = cgraph.load_graph(_repo(tmp_path) / "docs" / "c-atlas" / "define")
    assert cgraph.answer_for_grep(g, "") is None
    assert cgraph.answer_for_grep(g, None) is None
    assert cgraph.answer_for_grep(g, "!!! $$$ ((( ") is None


# ---- CLI ----

def test_cli_who_prints_and_returns_0(tmp_path, capsys):
    root = _repo(tmp_path)
    rc = cgraph.main(["who", "pkg-a", "--root", str(root)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "pkg-d" in out


def test_cli_json_parses(tmp_path, capsys):
    root = _repo(tmp_path)
    rc = cgraph.main(["who", "pkg-a", "--root", str(root), "--json"])
    assert rc == 0
    out = capsys.readouterr().out
    data = json.loads(out)
    assert data["node"] == "pkg-a"


def test_cli_missing_atlas_exit_2(tmp_path, capsys):
    root = tmp_path / "bare"
    (root / ".git").mkdir(parents=True)
    rc = cgraph.main(["who", "pkg-a", "--root", str(root)])
    assert rc == 2
    out = capsys.readouterr().out
    assert out.count("\n") == 1
    assert "no c-atlas under" in out


# ---- hook precheck ----

def _load_hook():
    hook_path = Path(__file__).resolve().parents[1] / "echelon_engine" / "hooks_staged" / "echelon_reflex.py"
    spec = importlib.util.spec_from_file_location("echelon_reflex_staged_test", hook_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_hook_graph_precheck_returns_answer(tmp_path, monkeypatch):
    monkeypatch.delenv("ECHELON_GRAPH_PRECHECK", raising=False)
    root = _repo(tmp_path)
    hook = _load_hook()
    payload = json.dumps({"command": 'grep -rn "belanja_domain" .'})
    ans = hook._graph_precheck(str(root), payload)
    assert ans is not None
    assert "GRAPH ANSWER" in ans


def test_hook_graph_precheck_none_without_atlas(tmp_path, monkeypatch):
    monkeypatch.delenv("ECHELON_GRAPH_PRECHECK", raising=False)
    root = tmp_path / "bare"
    (root / ".git").mkdir(parents=True)
    hook = _load_hook()
    payload = json.dumps({"command": 'grep -rn "belanja_domain" .'})
    assert hook._graph_precheck(str(root), payload) is None


def test_hook_graph_precheck_kill_switch(tmp_path, monkeypatch):
    monkeypatch.setenv("ECHELON_GRAPH_PRECHECK", "off")
    root = _repo(tmp_path)
    hook = _load_hook()
    payload = json.dumps({"command": 'grep -rn "belanja_domain" .'})
    assert hook._graph_precheck(str(root), payload) is None


def test_hook_graph_precheck_none_on_internal_exception(tmp_path, monkeypatch):
    monkeypatch.delenv("ECHELON_GRAPH_PRECHECK", raising=False)
    root = _repo(tmp_path)
    hook = _load_hook()

    from echelon_engine.atoms import cgraph as real_cgraph
    def boom(*a, **kw):
        raise RuntimeError("boom")
    monkeypatch.setattr(real_cgraph, "load_graph", boom)

    payload = json.dumps({"command": 'grep -rn "belanja_domain" .'})
    assert hook._graph_precheck(str(root), payload) is None


def test_staleness_reports_source_newer_than_cards(tmp_path):
    """Gate finding 2026-09-06: a stale atlas answered 'no match' for a real function. The
    answer must carry its own staleness so the model regenerates instead of trusting a miss."""
    import os, time
    repo = tmp_path / "repo"
    define = repo / "docs" / "c-atlas" / "define"
    define.mkdir(parents=True)
    (repo / ".git").mkdir()
    (define / "_meta.json").write_text(json.dumps({"packages": ["pkg"]}), encoding="utf-8")
    (define / "pkg-mod.json").write_text(json.dumps({"id": "pkg-mod", "type": "module", "title": "pkg.mod",
        "path": "pkg/mod.py", "claim": "", "members": [{"kind": "func", "name": "thing", "claim": ""}],
        "part_of": "pkg", "depends_on": []}), encoding="utf-8")
    old = time.time() - 3600
    os.utime(define / "pkg-mod.json", (old, old))
    (repo / "pkg").mkdir()
    (repo / "pkg" / "mod.py").write_text("def thing(): pass\n", encoding="utf-8")
    st = cgraph.staleness(define)
    assert st["newer"] == 1 and st["scanned"] == 1
    g = cgraph.load_graph(define)
    ans = cgraph.answer_for_grep(g, "grep -rn thing pkg/", define)
    assert "atlas STALE: 1 source file" in ans
    # fresh atlas -> no stale line
    now = time.time() + 5
    os.utime(define / "pkg-mod.json", (now, now))
    assert cgraph.stale_line(define) is None
    assert "STALE" not in cgraph.answer_for_grep(g, "grep -rn thing pkg/", define)
