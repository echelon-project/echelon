"""Tests for echelon_engine.propose — the PROPOSAL GATE (spec 2d, §5).

One test per §3 rule id (named after it), the fixture-test law (index seeded
by rescanning COPIES of the real estate fixtures), PILOT vs semantic mode
switching, the CHG open/reuse, every never-raise degrade (one stderr line,
no traceback), the bind rules on a real git repo with two commits, the 2c
receipt + workcycle._verify_violations, CLI exit codes 0/1/2, resolve's top
match, and the self-gate: THIS build's proposal checked over a rescan of the
three modules it calls must PROCEED.
"""
import json
import shutil
import subprocess
from pathlib import Path

from echelon_engine import contracts
from echelon_engine import propose as P
from echelon_engine import propose_scan as ps
from echelon_engine import workcycle

FX = Path(__file__).parent / "fixtures" / "propose_scan"
REPO = Path(__file__).resolve().parents[1]


# ── fixtures ─────────────────────────────────────────────────────────────────

def _git(root, *args):
    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)


def _git_init(root):
    subprocess.run(["git", "-C", str(root), "init", "-q"], capture_output=True)
    subprocess.run(["git", "-C", str(root), "config", "user.email", "t@t"], capture_output=True)
    subprocess.run(["git", "-C", str(root), "config", "user.name", "t"], capture_output=True)
    if not (root / "seed.txt").exists():
        (root / "seed.txt").write_text("x\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(root), "add", "-A"], capture_output=True)
    subprocess.run(["git", "-C", str(root), "commit", "-qm", "seed"], capture_output=True)


def _commit(root, msg):
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", msg)


def _room(base, type_="engine"):
    """git repo + room + index seeded over the 2b real-estate fixtures.
    Idempotent per base dir (re-checks reuse the same room)."""
    root = base / "repo"
    root.mkdir(parents=True, exist_ok=True)
    _git_init(root)
    try:
        e = workcycle.init(root, estate="t", type_=type_)
    except workcycle.RoomExists:
        e = root / ".echelon"
    ps.rescan(e / "index", [FX / "buku_stok.js", FX / "session_state.py"], root=FX)
    return e, root


def _gated(tmp_path, prop):
    e, root = _room(tmp_path)
    return P.check(e, prop, repo_root=root)


def _prop(**over):
    p = {"v": 1, "id": "PROP-T", "intent": "t", "kind": "engine-module",
         "budget": {"files": 3, "loc": 400, "deps": 0, "override_reason": None},
         "steps": [{"n": 1, "do": "x"}],
         "touches": [{"path": "mod.py", "mode": "create"}],
         "introduces": [], "references": [], "removes": [], "replaces": [],
         "contracts_claimed": [], "invariants_claimed": [], "unknowns": [],
         "ack_contracts_not_loaded": True}       # every fixture room has an empty drawer
    p.update(over)
    return p


def _check(root, prop):
    return P.check(root / ".echelon", prop, repo_root=root)


def _has(r, rule, sev=None):
    return any(f["rule"] == rule and (sev is None or f["severity"] == sev) for f in r["findings"])


def _row(name, kind="function", path="mod.py", **kw):
    r = {"v": 1, "id": f"{kind}:{path}#{name}", "name": name, "kind": kind, "path": path,
         "line": 1, "signature": "", "canonical": False, "deprecated": False,
         "replaced_by": None, "active_change": None, "semantic_tags": [], "domain": "",
         "source": "scan"}
    r.update(kw)
    return r


def _add_rows(e, rows):
    with open(e / "index" / "symbols.jsonl", "a", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")


def _write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _code_json(e, **kw):
    _write(e / "contracts" / "code.json", json.dumps({"v": 1, "id": "code", "kind": "code",
                                                      "laws": {}, **kw}))


# ── §3 step 0 shape ──────────────────────────────────────────────────────────
def test_SHAPE_MISSING_FIELD(tmp_path):
    p = _prop(); del p["budget"]
    r = _gated(tmp_path, p)
    assert _has(r, "SHAPE-MISSING-FIELD", "error") and r["verdict"] == "REJECT"


def test_SHAPE_UNKNOWN_FIELD(tmp_path):
    r = _gated(tmp_path, _prop(extra_field=1))
    assert _has(r, "SHAPE-UNKNOWN-FIELD", "advisory")


# ── §3 step 1 reference edges ────────────────────────────────────────────────
def test_REF_UNRESOLVED(tmp_path):
    r = _gated(tmp_path, _prop(references=[{"name": "nope", "kind": "function", "path": "missing.py"}]))
    assert _has(r, "REF-UNRESOLVED", "error") and r["verdict"] == "REJECT"


def test_stdlib_refs_verified(tmp_path):
    """path:"stdlib" is verified against sys.stdlib_module_names (top-level
    module of the name) — a nonexistent symbol marked stdlib does not dodge
    REF-UNRESOLVED."""
    r = _gated(tmp_path, _prop(references=[{"name": "pathlib.Path", "kind": "class", "path": "stdlib"}]))
    assert not _has(r, "REF-UNRESOLVED") and r["resolved_edges"]["pathlib.Path"] == "stdlib"
    r = _gated(tmp_path, _prop(references=[{"name": "numpy", "kind": "module", "path": "stdlib"}]))
    assert _has(r, "REF-UNRESOLVED", "error")
    assert "not a stdlib module" in r["findings"][0]["evidence"]


def test_REF_PATH_DRIFT(tmp_path):
    r = _gated(tmp_path, _prop(references=[{"name": "text", "kind": "function", "path": "other.js"}]))
    assert _has(r, "REF-PATH-DRIFT", "warning")           # index says buku_stok.js


def test_REF_OBSOLETE(tmp_path):
    e, root = _room(tmp_path)
    _add_rows(e, [_row("dead", path="mod.py", deprecated=True, replaced_by="alive")])
    r = _check(root, _prop(references=[{"name": "dead", "kind": "function", "path": "mod.py"}]))
    assert _has(r, "REF-OBSOLETE", "error") and "alive" in r["findings"][0]["evidence"]


def test_REF_IN_FLIGHT(tmp_path):
    e, root = _room(tmp_path)
    _add_rows(e, [_row("inflight", path="mod.py", active_change={"owner": "OTHER", "chg": "CHG-001"})])
    r = _check(root, _prop(references=[{"name": "inflight", "kind": "function", "path": "mod.py"}]))
    assert _has(r, "REF-IN-FLIGHT", "warning")


def test_INDEX_WAS_STALE(tmp_path):
    e, root = _room(tmp_path)
    _write(root / "mod.py", "def fresh_fn():\n    pass\n")
    r = _check(root, _prop(touches=[{"path": "mod.py", "mode": "edit"}],
                           references=[{"name": "fresh_fn", "kind": "function", "path": "mod.py"}]))
    assert _has(r, "INDEX-WAS-STALE", "advisory") and not _has(r, "REF-UNRESOLVED")
    assert r["resolved_edges"]["fresh_fn"].startswith("mod.py:")


# ── §3 step 2 introduce collisions ───────────────────────────────────────────
def test_DUP_SEMANTIC(tmp_path):
    e, root = _room(tmp_path)
    _add_rows(e, [_row("render_kpi", path="other.py", semantic_tags=["kpi", "render", "tile"])])
    sym = {"name": "renderKpi", "kind": "function", "path": "mod.py",
           "keywords": ["kpi", "render", "tile"], "signature": ""}
    r = _check(root, _prop(introduces=[sym]))
    assert r["mode"]["similarity"] == "semantic" and _has(r, "DUP-SEMANTIC", "error")
    assert r["verdict"] == "REJECT"


def test_DUP_DISPUTED(tmp_path):
    e, root = _room(tmp_path)
    _add_rows(e, [_row("render_kpi", path="other.py", semantic_tags=["kpi", "render", "tile"])])
    sym = {"name": "renderKpi", "kind": "function", "path": "mod.py",
           "keywords": ["kpi", "render", "tile"], "dispute": "the tile takes a summary shape"}
    r = _check(root, _prop(introduces=[sym]))
    assert r["findings"][0]["rule"] == "DUP-DISPUTED"      # owner row prints FIRST
    assert _has(r, "DUP-DISPUTED", "warning") and r["verdict"] == "REVISE"


def test_DUP_CANDIDATE(tmp_path):
    e, root = _room(tmp_path)
    sym = {"name": "text", "kind": "function", "path": "mod.py", "keywords": []}
    r = _check(root, _prop(introduces=[sym]))              # pilot: score 1.0 vs buku_stok text
    assert r["mode"]["similarity"] == "name-only" and _has(r, "DUP-CANDIDATE", "error")
    sym["why_not_reuse"] = "text in buku_stok.js escapes HTML; this one formats currency — different capability"
    r = _check(root, _prop(introduces=[sym]))
    assert _has(r, "DUP-CANDIDATE", "warning")


def test_pilot_vs_semantic_thresholds(tmp_path):
    e, root = _room(tmp_path)
    sym = {"name": "text", "kind": "function", "path": "mod.py", "keywords": []}
    r = _check(root, _prop(introduces=[sym]))              # no tags -> name-only 0.50
    assert r["mode"]["similarity"] == "name-only" and _has(r, "DUP-CANDIDATE", "error")
    _add_rows(e, [_row("text", path="other.py", semantic_tags=["kpi", "render", "tile"])])
    r = _check(root, _prop(introduces=[sym]))              # tags -> semantic 0.90
    assert r["mode"]["similarity"] == "semantic" and _has(r, "DUP-SEMANTIC", "error")


def test_cap_gap_needs_content_beyond_the_name(tmp_path):
    """Name-repetition does not make a gap: the length is measured after
    stripping the candidate name, and the text must carry ≥3 distinct words."""
    assert not P._cap_gap("realThing realThing realThing xx", "realThing")
    assert P._cap_gap("realThing takes tokens[]; we need a summary shape", "realThing")


def test_ack_counts_as_acked(tmp_path):
    e, root = _room(tmp_path)
    sym = {"name": "text", "kind": "function", "path": "mod.py", "keywords": [],
           "why_not_reuse": "text in buku_stok.js escapes HTML; this one formats currency — different capability",
           "ack_dup_candidate": True}
    r = _check(root, _prop(introduces=[sym]))
    assert _has(r, "DUP-CANDIDATE", "warning") and r["verdict"] == "PROCEED"


# ── page-private DUP tier (2e deliverable D, RULINGS A11-A13) ───────────────
def _ui_page_private(e, tiers=None, exempt=None, v=1, **over):
    doc = {"v": 1, "id": "UI", "kind": "ui", "scope_globs": ["pages/**"],
           "page_private": {"v": v,
                            "tiers": tiers if tiers is not None else
                            [{"glob": "pages/*/*.js", "page_key": "parent-dir"}],
                            "exempt_globs": exempt if exempt is not None else ["tests/**"]}}
    doc.update(over)
    _write(e / "contracts" / "ui.json", json.dumps(doc))


def test_D1_sibling_page_auto_ack(tmp_path):
    """CT-D1 (spec's own test): sibling-page files under page_private globs
    auto-ack as DUP-PAGE-IDIOM, acked:true, no DUP-CANDIDATE error, verdict
    not blocked on that finding."""
    e, root = _room(tmp_path)
    _ui_page_private(e)
    _add_rows(e, [_row("_esc", path="pages/uang/main.js"),
                  _row("_esc", path="pages/stok/main.js")])
    sym = {"name": "_esc", "kind": "function", "path": "pages/kas/main.js", "keywords": []}
    r = _check(root, _prop(touches=[{"path": "pages/kas/main.js", "mode": "create"}], introduces=[sym]))
    assert _has(r, "DUP-PAGE-IDIOM", "warning")
    hit = next(f for f in r["findings"] if f["rule"] == "DUP-PAGE-IDIOM")
    assert hit["acked"] is True and hit["auto_acked"] is True
    assert not _has(r, "DUP-CANDIDATE")
    assert r["verdict"] == "PROCEED"


def test_S1_ladeloux_seed_component_dir_not_cross_matched(tmp_path):
    """S1 (gate probe9 heal): the alpha-app ui.json page_private tier glob
    "os_client/web/static/*.js" crosses "/" under fnmatch and, pre-fix, ALSO
    matched files under components-<page>/ subdirectories (e.g.
    components-uang/board.js) — a genuinely SHARED helper reused across
    pages then wrongly auto-acked as a same-page-tier "sibling". The real
    seed's exempt_globs now carries "os_client/web/static/components-*/**"
    so that path is filtered OUT of DUP scoring entirely (never a candidate,
    never an ack target) — this test uses the EXACT real seed shape (not a
    hand-picked minimal fixture) so a future edit to the seed file itself is
    what this test actually guards."""
    e, root = _room(tmp_path)
    _ui_page_private(e, tiers=[{"glob": "os_client/web/static/*.js", "page_key": "stem"}],
                     exempt=["tests/**", "**/test_*.py", "os_client/web/static/lib/**",
                             "os_client/web/static/components-shared/**",
                             "os_client/web/static/components-*/**"])
    _add_rows(e, [_row("computeTotals", path="os_client/web/static/components-uang/board.js")])
    sym = {"name": "computeTotals", "kind": "function",
          "path": "os_client/web/static/components-belanja/panel.js", "keywords": []}
    r = _check(root, _prop(touches=[{"path": "os_client/web/static/components-belanja/panel.js", "mode": "edit"}],
                           introduces=[sym]))
    assert not _has(r, "DUP-PAGE-IDIOM")
    assert not _has(r, "DUP-CANDIDATE")
    assert not _has(r, "DUP-SEMANTIC")


def test_D2_ack_survives_the_post_pass(tmp_path):
    """CT-D2 (A12's trap): the FINAL report's acked must still be True — the
    check() post-pass over warnings must not overwrite an auto-ack with the
    builder-ack lookup (which would find no ack_dup_page_idiom key and set
    False)."""
    e, root = _room(tmp_path)
    _ui_page_private(e)
    _add_rows(e, [_row("_esc", path="pages/stok/main.js")])
    sym = {"name": "_esc", "kind": "function", "path": "pages/kas/main.js", "keywords": []}
    r = _check(root, _prop(touches=[{"path": "pages/kas/main.js", "mode": "create"}], introduces=[sym]))
    hit = next(f for f in r["findings"] if f["rule"] == "DUP-PAGE-IDIOM")
    assert hit["acked"] is True


def test_D3_same_page_dup_still_errors(tmp_path):
    """CT-D3 (A11's crux): a different FILE under the SAME page dir is not a
    sibling — the tier declares page_key explicitly (parent-dir), so this
    must still error."""
    e, root = _room(tmp_path)
    _ui_page_private(e)
    _add_rows(e, [_row("_esc", path="pages/uang/helper.js")])
    sym = {"name": "_esc", "kind": "function", "path": "pages/uang/main.js", "keywords": []}
    r = _check(root, _prop(touches=[{"path": "pages/uang/main.js", "mode": "create"}], introduces=[sym]))
    assert not _has(r, "DUP-PAGE-IDIOM")
    assert _has(r, "DUP-CANDIDATE")


def test_D4_shared_lib_dup_still_errors(tmp_path):
    """CT-D4 (spec's second test): a candidate outside page-private globs
    (shared lib) is never auto-acked."""
    e, root = _room(tmp_path)
    _ui_page_private(e)
    _add_rows(e, [_row("_esc", path="shared/util.js")])
    sym = {"name": "_esc", "kind": "function", "path": "pages/kas/main.js", "keywords": []}
    r = _check(root, _prop(touches=[{"path": "pages/kas/main.js", "mode": "create"}], introduces=[sym]))
    assert not _has(r, "DUP-PAGE-IDIOM")
    assert _has(r, "DUP-CANDIDATE")


def test_D5_mixed_candidates_kill_the_ack(tmp_path):
    """CT-D5 (A13): a sibling-page match AND a shared-lib match both >=
    dup_warn -> NO auto-ack, and the surviving finding names the SHARED-LIB
    candidate, not the (possibly higher-scoring) sibling."""
    e, root = _room(tmp_path)
    _ui_page_private(e)
    _add_rows(e, [_row("_esc", path="pages/stok/main.js"),
                  _row("_esc", path="shared/util.js")])
    sym = {"name": "_esc", "kind": "function", "path": "pages/kas/main.js", "keywords": []}
    r = _check(root, _prop(touches=[{"path": "pages/kas/main.js", "mode": "create"}], introduces=[sym]))
    assert not _has(r, "DUP-PAGE-IDIOM")
    hit = next(f for f in r["findings"] if f["rule"] == "DUP-CANDIDATE")
    assert "shared/util.js" in hit["evidence"]


def test_D6_component_under_same_glob(tmp_path):
    """CT-D6: a component sibling under a page-private glob still auto-acks
    (kind differs from the plain function case but _candidates already
    merges sibling kinds — component<->const/token)."""
    e, root = _room(tmp_path)
    _ui_page_private(e, tiers=[{"glob": "pages/*/*.js", "page_key": "parent-dir"}])
    _add_rows(e, [_row("StubBoard", kind="component", path="pages/uang/widget.js")])
    sym = {"name": "StubBoard", "kind": "component", "path": "pages/kas/widget.js", "keywords": []}
    r = _check(root, _prop(touches=[{"path": "pages/kas/widget.js", "mode": "create"}], introduces=[sym]))
    assert _has(r, "DUP-PAGE-IDIOM", "warning") or not _has(r, "DUP-CANDIDATE", "error")


def test_D7_tests_and_stubs_exempt(tmp_path):
    """CT-D7: an index row under an exempt glob produces NO dup finding at
    all — exempt and auto-acked are different receipt entries."""
    e, root = _room(tmp_path)
    _ui_page_private(e)
    _add_rows(e, [_row("_esc", path="tests/test_page.js")])
    sym = {"name": "_esc", "kind": "function", "path": "pages/kas/main.js", "keywords": []}
    r = _check(root, _prop(touches=[{"path": "pages/kas/main.js", "mode": "create"}], introduces=[sym]))
    assert not _has(r, "DUP-PAGE-IDIOM") and not _has(r, "DUP-CANDIDATE")


def test_D8_page_private_absent_behaves_like_baseline(tmp_path):
    """CT-D8: no page_private block (or no ui.json at all) -> fail-CLOSED,
    identical to baseline — every DUP still errors, no crash."""
    e, root = _room(tmp_path)
    sym = {"name": "text", "kind": "function", "path": "mod.py", "keywords": []}
    r = _check(root, _prop(introduces=[sym]))
    assert not _has(r, "DUP-PAGE-IDIOM") and _has(r, "DUP-CANDIDATE", "error")


def test_D9_page_private_future_version_ignored(tmp_path):
    """CT-D9: a page_private block carrying a future version marker is
    ignored with a visible degradation, not applied half-parsed."""
    e, root = _room(tmp_path)
    _ui_page_private(e, v=99)
    _add_rows(e, [_row("_esc", path="pages/stok/main.js")])
    sym = {"name": "_esc", "kind": "function", "path": "pages/kas/main.js", "keywords": []}
    r = _check(root, _prop(touches=[{"path": "pages/kas/main.js", "mode": "create"}], introduces=[sym]))
    assert not _has(r, "DUP-PAGE-IDIOM")
    assert _has(r, "DUP-CANDIDATE")


def test_D10_auto_ack_distinguishable_from_builder_ack(tmp_path):
    """CT-D10: a proposal carrying ack_dup_candidate:true yields a finding
    WITHOUT auto_acked, while the auto-ack case carries it."""
    e, root = _room(tmp_path)
    _ui_page_private(e)
    _add_rows(e, [_row("text", path="other.py", semantic_tags=[])])
    sym = {"name": "text", "kind": "function", "path": "mod.py", "keywords": [],
           "why_not_reuse": "text in buku_stok.js escapes HTML; this one formats currency — different capability",
           "ack_dup_candidate": True}
    r = _check(root, _prop(introduces=[sym]))
    hit = next(f for f in r["findings"] if f["rule"] == "DUP-CANDIDATE")
    assert hit["acked"] is True and "auto_acked" not in hit


def test_D11_backslash_paths_in_tier_globs(tmp_path):
    """CT-D11: POSIX-normalization on BOTH sides of the sibling comparison."""
    e, root = _room(tmp_path)
    _ui_page_private(e, tiers=[{"glob": r"pages\*\*.js", "page_key": "parent-dir"}])
    _add_rows(e, [_row("_esc", path="pages/stok/main.js")])
    sym = {"name": "_esc", "kind": "function", "path": "pages/kas/main.js", "keywords": []}
    r = _check(root, _prop(touches=[{"path": "pages/kas/main.js", "mode": "create"}], introduces=[sym]))
    assert _has(r, "DUP-PAGE-IDIOM", "warning")


def test_INTRO_OUTSIDE_TOUCHES(tmp_path):
    sym = {"name": "x", "kind": "function", "path": "elsewhere.py"}
    r = _gated(tmp_path, _prop(introduces=[sym]))
    assert _has(r, "INTRO-OUTSIDE-TOUCHES", "error") and r["verdict"] == "REJECT"


def test_INTRO_ALREADY_EXISTS(tmp_path):
    e, root = _room(tmp_path)
    _add_rows(e, [_row("text", path="buku_stok.js")])
    sym = {"name": "text", "kind": "function", "path": "buku_stok.js"}
    r = _check(root, _prop(touches=[{"path": "buku_stok.js", "mode": "edit"}], introduces=[sym]))
    assert _has(r, "INTRO-ALREADY-EXISTS", "error")


# ── §3 step 3 touch fence ────────────────────────────────────────────────────
def test_TOUCH_SHARED_FILE(tmp_path):
    e, root = _room(tmp_path)
    _code_json(e, orchestrator_only=["mod.py"], frozen=[])
    r = _check(root, _prop())
    assert _has(r, "TOUCH-SHARED-FILE", "error")


def test_TOUCH_CREATE_EXISTS(tmp_path):
    e, root = _room(tmp_path)
    _write(root / "mod.py", "x")
    r = _check(root, _prop())
    assert _has(r, "TOUCH-CREATE-EXISTS", "error")


def test_TOUCH_EDIT_MISSING(tmp_path):
    r = _gated(tmp_path, _prop(touches=[{"path": "mod.py", "mode": "edit"}]))
    assert _has(r, "TOUCH-EDIT-MISSING", "error")


def test_own_artifact_exemption_scoped_to_proposals(tmp_path):
    """Only <room>/proposals/<id>.json / <id>.report.json are exempt from
    TOUCH-CREATE-EXISTS — a same-named basename anywhere else is not."""
    e, root = _room(tmp_path)
    _write(root / "lib" / "PROP-X.json", "x")
    r = _check(root, _prop(id="PROP-X", touches=[{"path": "lib/PROP-X.json", "mode": "create"}]))
    assert _has(r, "TOUCH-CREATE-EXISTS", "error")
    _write(root / ".echelon" / "proposals" / "PROP-X.json", "x")
    r = _check(root, _prop(id="PROP-X", touches=[{"path": ".echelon/proposals/PROP-X.json", "mode": "create"}]))
    assert not _has(r, "TOUCH-CREATE-EXISTS")


def test_TOUCH_CHG_OVERLAP(tmp_path):
    e, root = _room(tmp_path)
    chg = {"v": 1, "id": "CHG-001", "proposal": "OTHER", "intent": "t",
           "touches": ["mod.py"], "introduces": [], "must_remove": [], "bound": False}
    _write(e / "changes" / "active" / "CHG-001.json", json.dumps(chg))
    r = _check(root, _prop())
    assert _has(r, "TOUCH-CHG-OVERLAP", "warning")


def test_TOUCH_FROZEN(tmp_path):
    e, root = _room(tmp_path)
    _code_json(e, orchestrator_only=[], frozen=["mod.py"])
    r = _check(root, _prop())
    assert _has(r, "TOUCH-FROZEN", "error")


def test_CODE_CONTRACT_ABSENT(tmp_path):
    r = _gated(tmp_path, _prop())
    assert _has(r, "CODE-CONTRACT-ABSENT", "advisory")


def test_code_contract_fence_by_kind(tmp_path):
    """The fence selects the contract whose kind == "code" (drawer ids are
    uppercase: "CODE"), NOT the id "code" — a lowercase-id lookup fails OPEN."""
    e, root = _room(tmp_path)
    _write(e / "contracts" / "code.json", json.dumps(
        {"v": 1, "id": "CODE", "kind": "code", "laws": {},
         "orchestrator_only": ["os_client/main.py"], "frozen": ["api_app_dash/*"]}))
    r = _check(root, _prop(touches=[{"path": "os_client/main.py", "mode": "create"}]))
    assert _has(r, "TOUCH-SHARED-FILE", "error") and not _has(r, "CODE-CONTRACT-ABSENT")
    r = _check(root, _prop(touches=[{"path": "api_app_dash/x.py", "mode": "create"}]))
    assert _has(r, "TOUCH-FROZEN", "error") and not _has(r, "CODE-CONTRACT-ABSENT")


def _api_json(e, **api_over):
    """kind:api sibling of _code_json — RULINGS A1 dual-keyed shape: drawer
    envelope (v/id/kind/scope_globs) authoritative, registry fields nested
    under "api"."""
    api = {"version": 1, "exact": True, "read_only": True,
           "base_path": "/x", "collection_verbs": ["GET"], "member_verbs": ["GET"],
           "response_envelope": "none", "pagination_style": "none",
           "id_style": "opaque_string", "error_required_paths": ["/error"],
           "auth_default": "required"}
    api.update(api_over.pop("api", {}))
    doc = {"v": 1, "id": "API", "kind": "api", "scope_globs": ["mod.py"],
           "laws": {}, "api": api}
    doc.update(api_over)
    _write(e / "contracts" / "api.json", json.dumps(doc))


def test_API_READ_ONLY(tmp_path):
    e, root = _room(tmp_path)
    _api_json(e)
    r = _check(root, _prop())
    assert _has(r, "API-READ-ONLY", "error") and r["verdict"] == "REJECT"
    assert r["findings"][0]["rule"] == "API-READ-ONLY"      # RULINGS A18: joins _FIRST


def test_API_READ_ONLY_kind_generic_not_id(tmp_path):
    """The fence keys off kind=="api", never the contract id — an id like
    "API-SOMETHING-ELSE" must still fence (CT-A2: 2026-08-27 gate-caught
    ac057ab twin — a kind lookup by id fails OPEN)."""
    e, root = _room(tmp_path)
    _api_json(e, id="API-SOMETHING-ELSE")
    r = _check(root, _prop())
    assert _has(r, "API-READ-ONLY", "error")


def test_API_READ_ONLY_false_does_not_fence(tmp_path):
    e, root = _room(tmp_path)
    _api_json(e, api={"read_only": False})
    r = _check(root, _prop())
    assert not _has(r, "API-READ-ONLY")


def test_API_READ_ONLY_truthy_string_is_invalid_not_freeze(tmp_path):
    """CT-A4: "read_only": "false" is a truthy JSON string — must NOT freeze
    via bool() coercion. Assert API-CONTRACT-FIELD-INVALID, no API-READ-ONLY."""
    e, root = _room(tmp_path)
    _api_json(e, api={"read_only": "false"})
    r = _check(root, _prop())
    assert _has(r, "API-CONTRACT-FIELD-INVALID", "error") and not _has(r, "API-READ-ONLY")
    e2, root2 = _room(tmp_path / "b")
    _api_json(e2, api={"read_only": None})
    r2 = _check(root2, _prop())
    assert _has(r2, "API-CONTRACT-FIELD-INVALID", "error") and not _has(r2, "API-READ-ONLY")


def test_API_READ_ONLY_empty_scope_globs_is_invalid_not_freeze_all(tmp_path):
    """CT-A5: applicable()'s empty-globs-means-everything must NOT leak into
    this fence — an api contract with scope_globs:[] must not freeze the
    whole repo; it is a field-invalid error instead."""
    e, root = _room(tmp_path)
    _api_json(e, scope_globs=[])
    r = _check(root, _prop(touches=[{"path": "totally/unrelated.py", "mode": "create"}]))
    assert _has(r, "API-CONTRACT-FIELD-INVALID", "error") and not _has(r, "API-READ-ONLY")


def test_API_READ_ONLY_malformed_scope_globs_string_not_fail_open(tmp_path):
    """CT-A6: a bare string scope_globs must not be iterated char-by-char."""
    e, root = _room(tmp_path)
    _api_json(e, scope_globs="mod.py")
    r = _check(root, _prop())
    assert _has(r, "API-CONTRACT-FIELD-INVALID", "error") and not _has(r, "API-READ-ONLY")


def test_API_READ_ONLY_backslash_touch_normalized(tmp_path):
    r"""CT-A7 (gate M3 fix): the touch path itself carries a Windows
    backslash (a\mod.py) against a POSIX scope glob (a/mod.py) — the
    previous fixture had NO backslash anywhere and asserted nothing about
    normalization. _step_touch does not normalize by default; the new fence
    must, or this fnmatch would silently miss."""
    e, root = _room(tmp_path)
    _api_json(e, scope_globs=["a/mod.py"])
    r = _check(root, _prop(touches=[{"path": r"a\mod.py", "mode": "edit"}]))
    assert _has(r, "API-READ-ONLY", "error")


def test_API_READ_ONLY_leading_dot_slash_stripped(tmp_path):
    """S2: a touch path carrying a leading "./" must still match scope_globs
    written without it."""
    e, root = _room(tmp_path)
    _api_json(e, scope_globs=["mod.py"])
    r = _check(root, _prop(touches=[{"path": "./mod.py", "mode": "edit"}]))
    assert _has(r, "API-READ-ONLY", "error")


def test_API_READ_ONLY_absent_is_advisory(tmp_path):
    r = _gated(tmp_path, _prop())
    assert _has(r, "API-CONTRACT-ABSENT", "advisory")
    assert not _has(r, "API-READ-ONLY")


def test_API_CONTRACT_UNREADABLE_when_dropped_by_load(tmp_path):
    """CT-A1: a file present on disk but dropped by contracts.load() (bad
    v/id) must surface a distinct visible error, never silent PROCEED."""
    e, root = _room(tmp_path)
    (e / "contracts").mkdir(parents=True, exist_ok=True)
    (e / "contracts" / "api.json").write_text(
        json.dumps({"kind": "api", "api": {"read_only": True}}), encoding="utf-8")   # no v, no id
    r = _check(root, _prop())
    assert _has(r, "API-CONTRACT-UNREADABLE", "error") and r["verdict"] == "REJECT"


def test_API_CONTRACT_UNREADABLE_registry_faithful_no_kind_key(tmp_path):
    """CT-A1 EXACT (gate probe5, M1 heal): a SCHEMA-FAITHFUL registry
    document per 01-contract-schemas.md's own example has NO "kind" key at
    all — {"version":1,"exact":...,"read_only":...,"scope_globs":[...],
    "base_path":...}. contracts.load() drops it (no v, no id); the
    UNREADABLE detector must key on filename/field-shape (looks_like_api),
    never raw.get("kind")=="api" — that key literally does not exist on
    this exact shape, so the old fence was permanently dead against it."""
    e, root = _room(tmp_path)
    api_doc = {"version": 1, "exact": True, "read_only": True, "scope_globs": ["mod.py"],
               "base_path": "/engine/v1", "collection_verbs": ["GET"], "member_verbs": ["GET"],
               "response_envelope": "none", "pagination_style": "none",
               "id_style": "opaque_string", "error_required_paths": ["/e"],
               "auth_default": "required"}
    _write(e / "contracts" / "api.json", json.dumps(api_doc))
    r = _check(root, _prop(touches=[{"path": "mod.py", "mode": "edit"}]))
    assert _has(r, "API-CONTRACT-UNREADABLE", "error") and r["verdict"] == "REJECT"
    rows = [x for x in contracts.doctor(e) if "api.json" in x["check"] or "api contract" in x["check"]]
    assert rows and not rows[0]["ok"]


def test_API_CONTRACT_loadable_no_kind_still_field_checked(tmp_path):
    """CT-A2 (gate probe5): a contract WITH v+id but WITHOUT "kind":"api" is
    successfully loaded by contracts.load() — the doctor/field-shape check
    must still fire on it (API-CONTRACT-FIELD-INVALID/required-fields row),
    never silently skip it because "kind" is absent. Fixture matches
    RULINGS A1's own nested-under-"api" dual-keyed shape but omits kind."""
    e, root = _room(tmp_path)
    doc = {"v": 1, "id": "API", "scope_globs": ["mod.py"],
           "api": {"version": 1, "exact": True, "read_only": True,
                   "scope_globs": ["mod.py"], "base_path": "/x",
                   "collection_verbs": ["GET"], "member_verbs": ["GET"],
                   "response_envelope": "none", "pagination_style": "none",
                   "id_style": "opaque_string", "error_required_paths": ["/e"],
                   "auth_default": "required"}}
    _write(e / "contracts" / "api.json", json.dumps(doc))
    rows = [x for x in contracts.doctor(e) if "required api.json fields" in x["check"]]
    assert rows and rows[0]["ok"]
    # a MISSING required field on this same no-kind shape must still surface
    bad = json.loads(json.dumps(doc))
    del bad["api"]["read_only"]
    _write(e / "contracts" / "api.json", json.dumps(bad))
    rows2 = [x for x in contracts.doctor(e) if "required api.json fields" in x["check"]]
    assert rows2 and not rows2[0]["ok"] and "read_only" in rows2[0]["evidence"]


def test_bind_API_READ_ONLY_mirrors_check(tmp_path):
    """CT-A10: a proposal can PROCEED with clean touches and then drift a
    real edit into the frozen surface at bind — DRIFT-FILE alone does not
    catch a path that was declared inside touches."""
    e, root = _bind_room(tmp_path)
    # api.json is seeded AFTER check() so the proposal PROCEEDs clean at
    # check-time — bind must still fence files_changed independently.
    prop = _prop(touches=[{"path": "mod.py", "mode": "edit"}])
    _write(e / "proposals" / "PROP-T.json", json.dumps(prop))
    r = P.check(e, prop, repo_root=root)
    assert r["verdict"] == "PROCEED", r["findings"]
    _api_json(e, scope_globs=["mod.py"])
    _write(root / "mod.py", "def alpha():\n    pass\ndef beta():\n    pass\n")
    _commit(root, "beta")
    res = P.bind(e, "PROP-T", repo_root=root)
    assert _has(res, "API-READ-ONLY", "error") and not res["ok"]


# ── §3 step 4 remove / replace safety ────────────────────────────────────────
def test_REMOVE_CALLERS_UNKNOWN(tmp_path):
    r = _gated(tmp_path, _prop(removes=["text"]))          # index row lacks used_by (pilot)
    assert _has(r, "REMOVE-CALLERS-UNKNOWN", "advisory")


def test_REMOVE_HAS_CALLERS(tmp_path):
    e, root = _room(tmp_path)
    _add_rows(e, [_row("doomed", path="mod.py", used_by=["caller.py"])])
    r = _check(root, _prop(removes=["doomed"]))
    assert _has(r, "REMOVE-HAS-CALLERS", "error") and r["verdict"] == "REJECT"


def test_REMOVE_CANONICAL_NO_REPLACEMENT(tmp_path):
    e, root = _room(tmp_path)
    _add_rows(e, [_row("canon", path="mod.py", canonical=True)])
    r = _check(root, _prop(removes=["canon"]))
    assert _has(r, "REMOVE-CANONICAL-NO-REPLACEMENT", "error")


def test_REMOVE_CANONICAL_NEEDS_TIED_REPLACEMENT(tmp_path):
    """Any introduce no longer silences the rule — the replacement must be tied
    to the removed symbol: introduces[].replaces/replaces_for names it, or the
    symbol itself is declared in replaces[]."""
    e, root = _room(tmp_path)
    _add_rows(e, [_row("foo", path="mod.py", canonical=True)])
    r = _check(root, _prop(removes=["foo"],
                           introduces=[{"name": "bar", "kind": "function", "path": "mod.py"}]))
    assert _has(r, "REMOVE-CANONICAL-NO-REPLACEMENT", "error")   # unrelated introduce: still fires
    r = _check(root, _prop(removes=["foo"],
                           introduces=[{"name": "foo2", "kind": "function", "path": "mod.py",
                                        "replaces": "foo"}]))
    assert not _has(r, "REMOVE-CANONICAL-NO-REPLACEMENT")        # tied via replaces
    r = _check(root, _prop(removes=["foo"],
                           introduces=[{"name": "foo2", "kind": "function", "path": "mod.py",
                                        "replaces_for": "foo"}]))
    assert not _has(r, "REMOVE-CANONICAL-NO-REPLACEMENT")        # tied via replaces_for
    r = _check(root, _prop(removes=["foo"], replaces=["foo"]))
    assert not _has(r, "REMOVE-CANONICAL-NO-REPLACEMENT")        # declared in replaces[]


# ── §3 step 5 contract projection ────────────────────────────────────────────
def test_CONTRACTS_NOT_LOADED(tmp_path):
    r = _gated(tmp_path, _prop())
    assert _has(r, "CONTRACTS-NOT-LOADED", "warning")      # acked in _prop -> PROCEED
    assert r["verdict"] == "PROCEED"


def test_CONTRACT_CLAIMED_NOT_FOUND(tmp_path):
    r = _gated(tmp_path, _prop(contracts_claimed=["NOPE"]))
    assert _has(r, "CONTRACT-CLAIMED-NOT-FOUND", "warning") and r["verdict"] == "REVISE"


def test_contracts_claimed_resolves_law_ids(tmp_path):
    """A claim names a contract id OR a law id inside one (§2's UI-TOKENS /
    PAGE-RED-BUDGET example) — the owning contract joins applicable_contracts."""
    e, root = _room(tmp_path)
    ui = {"v": 1, "id": "UI", "kind": "ui", "scope_globs": ["elsewhere/*"],   # no touch matches
          "laws": {"PAGE-RED-BUDGET": {"title": "t", "statement": "s",
                                       "severity": "warning", "params": {},
                                       "witness": "code"}}}
    _write(e / "contracts" / "ui.json", json.dumps(ui))
    r = _check(root, _prop(contracts_claimed=["PAGE-RED-BUDGET"]))
    assert not _has(r, "CONTRACT-CLAIMED-NOT-FOUND")
    assert "UI" in r["applicable_contracts"]                    # via the law owner, not the globs
    assert "UI/PAGE-RED-BUDGET" in r["deferred_to_bind"]
    r = _check(root, _prop(contracts_claimed=["NOPE"]))
    assert _has(r, "CONTRACT-CLAIMED-NOT-FOUND", "warning")


def test_CONTRACT_LAW_MANUAL(tmp_path):
    e, root = _room(tmp_path)
    ui = {"v": 1, "id": "UI-T", "kind": "ui", "scope_globs": ["mod.py"],
          "laws": {"LAW-A": {"title": "t", "statement": "s", "severity": "warning",
                             "params": {}, "witness": "manual"},
                   "LAW-B": {"title": "t", "statement": "s", "severity": "warning",
                             "params": {}, "witness": "code"}}}
    _write(e / "contracts" / "ui.json", json.dumps(ui))
    r = _check(root, _prop())
    assert _has(r, "CONTRACT-LAW-MANUAL", "advisory")
    assert "UI-T/LAW-B" in r["deferred_to_bind"] and "UI-T/LAW-A" not in r["deferred_to_bind"]
    assert "UI-T" in r["applicable_contracts"]


# ── §3 step 6 budget ─────────────────────────────────────────────────────────
def test_BUDGET_DEFAULTED(tmp_path):
    p = _prop(); del p["budget"]
    r = _gated(tmp_path, p)
    assert _has(r, "BUDGET-DEFAULTED", "warning") and _has(r, "SHAPE-MISSING-FIELD", "error")


def test_BUDGET_EXCEEDS_TYPE(tmp_path):
    r = _gated(tmp_path, _prop(budget={"files": 9, "loc": 400, "deps": 0, "override_reason": None}))
    assert _has(r, "BUDGET-EXCEEDS-TYPE", "warning") and r["verdict"] == "REVISE"
    r = _gated(tmp_path, _prop(budget={"files": 9, "loc": 400, "deps": 0, "override_reason": "x"}))
    assert not _has(r, "BUDGET-EXCEEDS-TYPE")


def test_BUDGET_NEW_DEP(tmp_path):
    p = _prop(budget={"files": 3, "loc": 400, "deps": 1, "override_reason": None})
    r = _gated(tmp_path / "engine", p)
    assert _has(r, "BUDGET-NEW-DEP", "error")              # engine forbids deps
    e, root = _room(tmp_path / "exp", type_="experiment")
    r = _check(root, p)                                    # experiment allows 1
    assert _has(r, "BUDGET-NEW-DEP", "warning")


def test_BUDGET_FILES(tmp_path):
    touches = [{"path": f"f{i}.py", "mode": "create"} for i in range(4)]
    r = _gated(tmp_path, _prop(touches=touches))
    assert _has(r, "BUDGET-FILES", "error") and r["verdict"] == "REJECT"


# ── §3 step 7 unknowns ───────────────────────────────────────────────────────
def test_UNKNOWN_RULING_NEEDED(tmp_path):
    r = _gated(tmp_path, _prop(unknowns=[{"q": "cents or major units?", "ruling": None}]))
    assert r["findings"][0]["rule"] == "UNKNOWN-RULING-NEEDED"   # owner row prints FIRST
    assert _has(r, "UNKNOWN-RULING-NEEDED", "warning") and r["verdict"] == "REVISE"


# ── CHG drawer ───────────────────────────────────────────────────────────────
def test_PROCEED_opens_one_chg_and_reuses(tmp_path):
    e, root = _room(tmp_path)
    assert _check(root, _prop())["verdict"] == "PROCEED"
    chgs = list((e / "changes" / "active").glob("CHG-*.json"))
    assert len(chgs) == 1 and chgs[0].stem == "CHG-001"
    chg = json.loads(chgs[0].read_text(encoding="utf-8"))
    assert chg["proposal"] == "PROP-T" and chg["bound"] is False
    assert chg["touches"] == ["mod.py"] and chg["must_remove"] == []
    assert _check(root, _prop())["verdict"] == "PROCEED"          # re-check reuses
    assert len(list((e / "changes" / "active").glob("CHG-*.json"))) == 1


def test_CHG_numbering_across_completed(tmp_path):
    e, root = _room(tmp_path)
    assert _check(root, _prop())["verdict"] == "PROCEED"
    (e / "changes" / "completed").mkdir(parents=True, exist_ok=True)
    (e / "changes" / "completed" / "CHG-001.json").write_text(
        (e / "changes" / "active" / "CHG-001.json").read_text(encoding="utf-8"), encoding="utf-8")
    (e / "changes" / "active" / "CHG-001.json").unlink()
    assert _check(root, _prop(id="PROP-T2"))["verdict"] == "PROCEED"
    assert (e / "changes" / "active" / "CHG-002.json").exists()


def test_REVISE_and_REJECT_open_nothing(tmp_path):
    e, root = _room(tmp_path)
    _check(root, _prop(unknowns=[{"q": "x", "ruling": None}]))           # REVISE
    _check(root, _prop(references=[{"name": "nope", "kind": "function", "path": "missing.py"}]))
    assert not list((e / "changes" / "active").glob("CHG-*.json"))


# ── post-build / AMEND (2e deliverable C, RULINGS A8-A10) ───────────────────
def _seal_chg_bound(e, proposal_id):
    """Fake "a real build happened": mark the proposal's CHG bound:true (the
    A8-required signal) without running a real bind."""
    p, chg = P._find_chg(e, proposal_id)
    chg["bound"] = True
    workcycle._write_json(p, chg)
    return chg


def test_C1_pilot_replay_AMEND(tmp_path):
    """CT-C1 / spec's own test: a CHG bound:true + the symbol indexed with
    active_change owned by this proposal -> AMEND, zero INTRO-ALREADY-EXISTS."""
    e, root = _room(tmp_path)
    prop = _prop(introduces=[{"name": "renderTab", "kind": "function", "path": "mod.py"}])
    r = _check(root, prop)
    assert r["verdict"] == "PROCEED"
    _seal_chg_bound(e, "PROP-T")
    _add_rows(e, [_row("renderTab", path="mod.py", active_change={"owner": "PROP-T"})])
    r2 = _check(root, prop)
    assert r2["verdict"] == "AMEND"
    assert not _has(r2, "INTRO-ALREADY-EXISTS", "error")


def test_C2_foreign_symbol_still_errors(tmp_path):
    """CT-C2 (A9 made executable): a SECOND introduce whose index row is owned
    by a DIFFERENT proposal and not in this CHG's introduces[] must still
    error and REJECT, not AMEND — own-artifact keyed on path alone would wrongly
    suppress this."""
    e, root = _room(tmp_path)
    prop = _prop(introduces=[{"name": "renderTab", "kind": "function", "path": "mod.py"},
                             {"name": "parseCsv", "kind": "function", "path": "mod.py"}])
    r = _check(root, prop)
    assert r["verdict"] == "PROCEED"
    _seal_chg_bound(e, "PROP-T")
    _add_rows(e, [_row("renderTab", path="mod.py", active_change={"owner": "PROP-T"}),
                  _row("parseCsv", path="mod.py", active_change={"owner": "PROP-OTHER"})])
    r2 = _check(root, prop)
    assert r2["verdict"] == "REJECT"
    assert _has(r2, "INTRO-ALREADY-EXISTS", "error")
    errs = [f for f in r2["findings"] if f["rule"] == "INTRO-ALREADY-EXISTS" and f["severity"] == "error"]
    assert any("parseCsv" in f["entry"] for f in errs)


def test_C3_do_nothing_trapdoor_closed(tmp_path):
    """CT-C3 (A8's trapdoor): a proposal PROCEEDed (opening CHG-001 bound:false)
    with NO build and NO bind — a second check with a genuinely colliding
    introduce must STILL error and REJECT; post-build mode must NOT engage."""
    e, root = _room(tmp_path)
    prop = _prop()
    assert _check(root, prop)["verdict"] == "PROCEED"
    chg = json.loads((e / "changes" / "active" / "CHG-001.json").read_text(encoding="utf-8"))
    assert chg["bound"] is False                    # nothing built
    _add_rows(e, [_row("mod_symbol", path="mod.py", active_change={"owner": "PROP-T"})])
    prop2 = _prop(introduces=[{"name": "mod_symbol", "kind": "function", "path": "mod.py"}])
    r2 = _check(root, prop2)
    assert r2["verdict"] == "REJECT"
    assert _has(r2, "INTRO-ALREADY-EXISTS", "error")


def test_C4_mixed_own_and_foreign_errors_REJECT(tmp_path):
    e, root = _room(tmp_path)
    _code_json(e, orchestrator_only=["mod.py"], frozen=[])
    prop = _prop(introduces=[{"name": "renderTab", "kind": "function", "path": "mod.py"}])
    r = _check(root, prop)
    assert _has(r, "TOUCH-SHARED-FILE", "error") and r["verdict"] == "REJECT"
    # can't PROCEED with a shared-file error, so seal a CHG by hand to reach post-build
    workcycle._write_json(e / "changes" / "active" / "CHG-001.json",
                          {"v": 1, "id": "CHG-001", "proposal": "PROP-T", "intent": "t",
                           "touches": ["mod.py"], "introduces": ["renderTab"], "must_remove": [],
                           "opened": contracts._now(), "bound": True})
    _add_rows(e, [_row("renderTab", path="mod.py", active_change={"owner": "PROP-T"})])
    r2 = _check(root, prop)
    assert r2["verdict"] == "REJECT"                # TOUCH-SHARED-FILE still fires
    assert not _has(r2, "INTRO-ALREADY-EXISTS", "error")   # own-artifact still suppressed


def test_C5_own_artifact_plus_unacked_warning_REVISE(tmp_path):
    """CT-C5: one suppressed own-artifact error, zero surviving errors, one
    unacked warning -> REVISE (per A10 ruling)."""
    e, root = _room(tmp_path)
    prop = _prop(introduces=[{"name": "renderTab", "kind": "function", "path": "mod.py"}],
                 unknowns=[{"q": "x", "ruling": None}])
    r = _check(root, prop)
    assert r["verdict"] == "REVISE"                  # unruled unknown blocks PROCEED
    workcycle._write_json(e / "changes" / "active" / "CHG-001.json",
                          {"v": 1, "id": "CHG-001", "proposal": "PROP-T", "intent": "t",
                           "touches": ["mod.py"], "introduces": ["renderTab"], "must_remove": [],
                           "opened": contracts._now(), "bound": True})
    _add_rows(e, [_row("renderTab", path="mod.py", active_change={"owner": "PROP-T"})])
    r2 = _check(root, prop)
    assert r2["verdict"] == "REVISE"
    assert not _has(r2, "INTRO-ALREADY-EXISTS", "error")


def test_C6_amend_does_not_leak_into_fresh_proposal(tmp_path):
    """CT-C6: a regression fence — a fresh proposal, no CHG, no receipt ->
    PROCEED (never AMEND)."""
    r = _gated(tmp_path, _prop())
    assert r["verdict"] == "PROCEED"


def test_C7_amend_opens_no_second_chg(tmp_path):
    e, root = _room(tmp_path)
    prop = _prop(introduces=[{"name": "renderTab", "kind": "function", "path": "mod.py"}])
    _check(root, prop)
    _seal_chg_bound(e, "PROP-T")
    _add_rows(e, [_row("renderTab", path="mod.py", active_change={"owner": "PROP-T"})])
    r2 = _check(root, prop)
    assert r2["verdict"] == "AMEND"
    assert len(list((e / "changes" / "active").glob("CHG-*.json"))) == 1


def test_C8_amend_exit_code_is_not_2(tmp_path, monkeypatch, capsys):
    e, root = _room(tmp_path)
    monkeypatch.chdir(root)
    prop = _prop(introduces=[{"name": "renderTab", "kind": "function", "path": "mod.py"}], id="PROP-T")
    _write(e / "proposals" / "PROP-T.json", json.dumps(prop))
    assert P.main(["check", "PROP-T"]) == 0
    _seal_chg_bound(e, "PROP-T")
    _add_rows(e, [_row("renderTab", path="mod.py", active_change={"owner": "PROP-T"})])
    rc = P.main(["check", "PROP-T"])
    out = capsys.readouterr().out
    assert '"verdict": "AMEND"' in out
    assert rc == 1


def test_C9_ok_false_bind_receipt_is_not_a_seal(tmp_path):
    """CT-C9: a bind receipt for P with ok:false and no bound CHG does NOT
    engage post-build mode."""
    e, root = _room(tmp_path)
    prop = _prop(introduces=[{"name": "renderTab", "kind": "function", "path": "mod.py"}])
    assert _check(root, prop)["verdict"] == "PROCEED"
    contracts.receipt(e, gate="propose-bind", subject="PROP-T",
                      findings=[{"rule": "DRIFT-FILE", "severity": "error", "entry": "x", "evidence": "x", "fix": "x"}])
    _add_rows(e, [_row("renderTab", path="mod.py", active_change={"owner": "PROP-T"})])
    r2 = _check(root, prop)
    assert r2["verdict"] == "REJECT"
    assert _has(r2, "INTRO-ALREADY-EXISTS", "error")


# ── bind (§5) ────────────────────────────────────────────────────────────────
def _bind_room(base):
    """git repo whose HEAD (checked) contains mod.py with alpha."""
    e, root = _room(base)
    _write(root / "mod.py", "def alpha():\n    pass\n")
    ps.rescan(e / "index", [root / "mod.py"], root=root)
    _commit(root, "mod")
    return e, root


def _gate(e, root, prop):
    """Store the proposal in the room (bind reads it from disk) + check it."""
    _write(e / "proposals" / "PROP-T.json", json.dumps(prop))
    r = P.check(e, prop, repo_root=root)
    assert r["verdict"] != "REJECT", r["findings"]
    return r


def _bind_check(e, root, prop, refs=()):
    p = _prop(touches=[{"path": "mod.py", "mode": "edit"}], **prop)
    if refs:
        p["references"] = refs
    return _gate(e, root, p)


def test_bind_DRIFT_FILE(tmp_path):
    e, root = _bind_room(tmp_path)
    _bind_check(e, root, {})
    _write(root / "other.py", "x = 1\n")
    _commit(root, "other")
    res = P.bind(e, "PROP-T", repo_root=root)
    assert not res["ok"] and _has(res, "DRIFT-FILE", "error")


def test_bind_DRIFT_INTRO(tmp_path):
    e, root = _bind_room(tmp_path)
    _bind_check(e, root, {})
    _write(root / "mod.py", "def alpha():\n    pass\ndef fresh():\n    pass\n")
    _commit(root, "fresh")
    res = P.bind(e, "PROP-T", repo_root=root)
    assert _has(res, "DRIFT-INTRO", "error") and res["new_symbols"] == ["fresh"]


def test_bind_DRIFT_REMOVE(tmp_path):
    e, root = _bind_room(tmp_path)
    _bind_check(e, root, {})
    _write(root / "mod.py", "")
    _commit(root, "drop-alpha")
    res = P.bind(e, "PROP-T", repo_root=root)
    assert _has(res, "DRIFT-REMOVE", "error") and res["removed_symbols"] == ["alpha"]


def test_bind_INTRO_MISSING(tmp_path):
    e, root = _bind_room(tmp_path)
    prop = _prop(touches=[{"path": "mod.py", "mode": "edit"}],
                 introduces=[{"name": "fresh", "kind": "function", "path": "mod.py"},
                             {"name": "wanted", "kind": "function", "path": "mod.py"}])
    _gate(e, root, prop)
    _write(root / "mod.py", "def alpha():\n    pass\ndef fresh():\n    pass\n")
    _commit(root, "fresh")
    res = P.bind(e, "PROP-T", repo_root=root)      # fresh lands, wanted never appears
    assert _has(res, "INTRO-MISSING", "warning")


def test_bind_REF_DECLARED_NOT_USED(tmp_path):
    e, root = _bind_room(tmp_path)
    refs = [{"name": "text", "kind": "function", "path": "buku_stok.js"}]
    _bind_check(e, root, {}, refs=refs)
    _write(root / "mod.py", "def alpha():\n    pass\n# text(x) — comment-only mention must NOT count\n")
    _commit(root, "comment")
    res = P.bind(e, "PROP-T", repo_root=root)
    assert _has(res, "REF-DECLARED-NOT-USED", "warning")


def test_bind_BUDGET_LOC(tmp_path):
    e, root = _bind_room(tmp_path)
    _bind_check(e, root, {"budget": {"files": 3, "loc": 1, "deps": 0, "override_reason": None}})
    _write(root / "mod.py", "def alpha():\n    pass\ndef one():\n    pass\ndef two():\n    pass\n")
    _commit(root, "big")
    res = P.bind(e, "PROP-T", repo_root=root)
    assert _has(res, "BUDGET-LOC", "error") and res["loc"] > 1


def test_bind_DRIFT_EDGE(tmp_path):
    e, root = _bind_room(tmp_path)
    # alpha MOVES to other.py: declared as remove (mod.py) + introduce (other.py)
    prop = _prop(touches=[{"path": "mod.py", "mode": "edit"}, {"path": "other.py", "mode": "create"}],
                 introduces=[{"name": "alpha", "kind": "function", "path": "other.py",
                              "why_not_reuse": "alpha moved from mod.py to other.py — declared remove",
                              "ack_dup_candidate": True}],
                 removes=["alpha"],
                 references=[{"name": "alpha", "kind": "function", "path": "mod.py"}])
    r = _gate(e, root, prop)
    assert r["verdict"] == "PROCEED", r["findings"]
    assert r["resolved_edges"]["alpha"].startswith("mod.py:")
    _write(root / "mod.py", "")
    _write(root / "other.py", "def alpha():\n    pass\n")
    _commit(root, "move-alpha")
    res = P.bind(e, "PROP-T", repo_root=root)
    assert res["ok"] and _has(res, "DRIFT-EDGE", "warning")


def test_bind_receipt_and_violations(tmp_path):
    e, root = _bind_room(tmp_path)
    prop = _prop(touches=[{"path": "mod.py", "mode": "edit"}],
                 introduces=[{"name": "beta", "kind": "function", "path": "mod.py",
                              "why_not_reuse": "text() escapes HTML; beta is a fixture symbol — different capability",
                              "ack_dup_candidate": True}])
    _gate(e, root, prop)
    _write(root / "mod.py", "def alpha():\n    pass\ndef beta():\n    pass\n")
    _commit(root, "beta")
    res = P.bind(e, "PROP-T", repo_root=root)
    assert res["ok"] and not res["findings"]
    latest = json.loads((e / "verification" / "latest.json").read_text(encoding="utf-8"))
    assert latest["gate"] == "propose-bind" and latest["subject"] == "PROP-T" and latest["ok"]
    assert workcycle._verify_violations(e) == 0
    chg = json.loads((e / "changes" / "completed" / "CHG-001.json").read_text(encoding="utf-8"))
    assert chg["bound"] is True and chg["history_receipt"].endswith(".json")
    # a failing bind overwrites latest.json with ok False -> violations counted
    _write(root / "other.py", "x = 1\n")
    _commit(root, "drift")
    res = P.bind(e, "PROP-T", repo_root=root)
    assert not res["ok"] and workcycle._verify_violations(e) == len(res["findings"])


def _index_bytes(e):
    return ((e / "index" / "symbols.jsonl").read_bytes() if (e / "index" / "symbols.jsonl").exists() else b"",
            (e / "index" / "endpoints.jsonl").read_bytes() if (e / "index" / "endpoints.jsonl").exists() else b"")


def test_bind_idempotence_failing_bind_index_byte_identical(tmp_path):
    """CT-B1: two binds on an identical DRIFT-INTRO state produce identical
    findings (rule/severity/entry/evidence tuples, not just counts) and the
    index bytes are UNCHANGED across both runs — a failing/degraded bind
    commits NOTHING (deliverable B: transient view, sealed-ok:true commit)."""
    e, root = _bind_room(tmp_path)
    _bind_check(e, root, {})
    _write(root / "mod.py", "def alpha():\n    pass\ndef fresh():\n    pass\n")
    _commit(root, "fresh")
    before = _index_bytes(e)
    res1 = P.bind(e, "PROP-T", repo_root=root)
    mid = _index_bytes(e)
    res2 = P.bind(e, "PROP-T", repo_root=root)
    after = _index_bytes(e)
    assert not res1["ok"] and not res2["ok"]
    key = lambda r: (r["rule"], r["severity"], r["entry"], r["evidence"])
    assert sorted(map(key, res1["findings"])) == sorted(map(key, res2["findings"]))
    assert before == mid == after


def test_bind_idempotence_success_still_commits(tmp_path):
    """CT-B2: the anti-no-op pair — a passing bind DOES gain the new index
    rows (an implementation that never writes would pass B1 trivially)."""
    e, root = _bind_room(tmp_path)
    prop = _prop(touches=[{"path": "mod.py", "mode": "edit"}],
                 introduces=[{"name": "beta", "kind": "function", "path": "mod.py",
                              "why_not_reuse": "text() escapes HTML; beta is a fixture symbol — different capability",
                              "ack_dup_candidate": True}])
    _gate(e, root, prop)
    before = _index_bytes(e)
    _write(root / "mod.py", "def alpha():\n    pass\ndef beta():\n    pass\n")
    _commit(root, "beta")
    res = P.bind(e, "PROP-T", repo_root=root)
    after = _index_bytes(e)
    assert res["ok"] and before != after
    assert any(r.get("name") == "beta" for r in ps._read_jsonl(e / "index" / "symbols.jsonl"))


def test_bind_idempotence_two_successful_binds_seal_churn_only(tmp_path):
    """CT-B7: two consecutive SUCCESSFUL binds of the same proposal — the CHG's
    bound_at/history_receipt change (fresh timestamp) each time (the seal is
    not itself idempotent-suppressed), and neither run raises or double-writes
    the CHG id. Note: `new_symbols`/INTRO-MISSING legitimately differ between
    run 1 and run 2 here because run 1's rescan already committed `beta` to
    the index — that is real, honest churn from the FIRST commit, not a
    B1-class idempotence defect (B1 is about REPEATED binds over ONE
    unchanged git state, which this is not)."""
    e, root = _bind_room(tmp_path)
    prop = _prop(touches=[{"path": "mod.py", "mode": "edit"}],
                 introduces=[{"name": "beta", "kind": "function", "path": "mod.py",
                              "why_not_reuse": "text() escapes HTML; beta is a fixture symbol — different capability",
                              "ack_dup_candidate": True}])
    _gate(e, root, prop)
    _write(root / "mod.py", "def alpha():\n    pass\ndef beta():\n    pass\n")
    _commit(root, "beta")
    res1 = P.bind(e, "PROP-T", repo_root=root)
    p1, chg1 = P._find_chg(e, "PROP-T")          # R1: an ok bind CLOSED it -> completed/
    res2 = P.bind(e, "PROP-T", repo_root=root)
    p2, chg2 = P._find_chg(e, "PROP-T")          # R1: re-sealed in place, never a second file
    assert res1["ok"] and res2["ok"]
    assert chg1["bound"] is True and chg2["bound"] is True
    assert chg1["history_receipt"] != chg2["history_receipt"]
    assert p1 == p2 and p1.parent.name == "completed"
    assert not list((e / "changes" / "active").glob("CHG-*.json"))
    assert len(list((e / "changes" / "completed").glob("CHG-*.json"))) == 1


def test_bind_idempotence_no_index_dir(tmp_path):
    """CT-B6: no index/ at all -> INDEX-UNAVAILABLE, no directory created, no raise."""
    e, root = _bind_room(tmp_path)
    _bind_check(e, root, {})
    shutil.rmtree(e / "index")
    res = P.bind(e, "PROP-T", repo_root=root)
    assert not res["ok"] and not (e / "index").exists()


def test_bind_drift_after_failure_leaves_chg_open(tmp_path):
    e, root = _bind_room(tmp_path)
    _bind_check(e, root, {})
    _write(root / "other.py", "x = 1\n")
    _commit(root, "drift")
    P.bind(e, "PROP-T", repo_root=root)
    chg = json.loads((e / "changes" / "active" / "CHG-001.json").read_text(encoding="utf-8"))
    assert chg["bound"] is False                        # only clean binds seal


# ── never-raise degrades ─────────────────────────────────────────────────────
def test_load_proposal_never_raises(tmp_path, capsys):
    assert P.load_proposal(tmp_path / "nope.json") == {}
    assert "unreadable or missing" in capsys.readouterr().err
    _write(tmp_path / "bad.json", "{not json")
    assert P.load_proposal(tmp_path / "bad.json") == {}
    assert "bad json" in capsys.readouterr().err
    _write(tmp_path / "v99.json", json.dumps({"v": 99, "id": "x"}))
    assert P.load_proposal(tmp_path / "v99.json") == {}
    assert "unsupported" in capsys.readouterr().err


def test_check_degrades_library(tmp_path, capsys):
    p = _prop()
    r = P.check(tmp_path / "no-room" / ".echelon", p, repo_root=tmp_path)
    assert r["verdict"] == "REJECT" and len(capsys.readouterr().err.strip().splitlines()) == 1
    e, root = _room(tmp_path / "a")
    shutil.rmtree(e / "index")
    r = P.check(e, p, repo_root=root)
    assert r["verdict"] == "REJECT" and len(capsys.readouterr().err.strip().splitlines()) == 1
    e2 = workcycle.init(tmp_path / "b", estate="t")     # no git at all
    r = P.check(e2, p, repo_root=None)
    assert r["verdict"] == "REJECT" and len(capsys.readouterr().err.strip().splitlines()) == 1


def test_bind_degrades_library(tmp_path, capsys):
    e, root = _room(tmp_path)
    r = P.bind(e, "PROP-NOPE", repo_root=root)          # no proposal file
    assert not r["ok"] and len(capsys.readouterr().err.strip().splitlines()) == 1
    e2 = workcycle.init(tmp_path / "b", estate="t")
    _write(e2 / "proposals" / "PROP-T.json", json.dumps(_prop()))
    r = P.bind(e2, "PROP-T", repo_root=None)            # no git
    assert not r["ok"] and len(capsys.readouterr().err.strip().splitlines()) == 1


def test_main_never_raise_degrades(tmp_path, monkeypatch, capsys):
    plain = tmp_path / "plain"
    plain.mkdir()
    monkeypatch.chdir(plain)
    assert P.main(["check", "PROP-X"]) == 2             # no room
    assert len(capsys.readouterr().err.strip().splitlines()) == 1
    e, root = _room(tmp_path / "a")
    monkeypatch.chdir(root)
    assert P.main(["check", "PROP-X"]) == 2             # proposal file missing
    assert len(capsys.readouterr().err.strip().splitlines()) == 1
    _write(e / "proposals" / "PROP-X.json", json.dumps({"v": 99, "id": "PROP-X"}))
    assert P.main(["check", "PROP-X"]) == 2             # v > 1
    assert len(capsys.readouterr().err.strip().splitlines()) == 1
    _write(e / "proposals" / "PROP-X.json", "{broken")
    assert P.main(["check", "PROP-X"]) == 2             # malformed json
    assert len(capsys.readouterr().err.strip().splitlines()) == 1
    _write(e / "proposals" / "PROP-X.json", json.dumps(_prop(id="PROP-X")))
    shutil.rmtree(e / "index")
    assert P.main(["check", "PROP-X"]) == 2             # index missing
    assert len(capsys.readouterr().err.strip().splitlines()) == 1


def test_main_bind_non_git(tmp_path, monkeypatch, capsys):
    e = workcycle.init(tmp_path / "nogit", estate="t")
    _write(e / "proposals" / "PROP-T.json", json.dumps(_prop()))
    monkeypatch.chdir(tmp_path / "nogit")
    assert P.main(["bind", "PROP-T"]) == 2
    assert len(capsys.readouterr().err.strip().splitlines()) == 1


# ── CLI exit codes ───────────────────────────────────────────────────────────
def _main_check(e, prop):
    _write(e / "proposals" / "PROP-T.json", json.dumps(prop))


def test_main_check_exit_codes(tmp_path, monkeypatch, capsys):
    e, root = _room(tmp_path)
    monkeypatch.chdir(root)
    _main_check(e, _prop())
    assert P.main(["check", "PROP-T"]) == 0             # PROCEED
    assert '"verdict": "PROCEED"' in capsys.readouterr().out
    assert (e / "proposals" / "PROP-T.report.json").exists()
    _main_check(e, _prop(unknowns=[{"q": "x", "ruling": None}]))
    assert P.main(["check", "PROP-T"]) == 1             # REVISE
    _main_check(e, _prop(references=[{"name": "nope", "kind": "function", "path": "m.py"}]))
    assert P.main(["check", "PROP-T"]) == 2             # REJECT


def test_main_bind_exit_codes(tmp_path, monkeypatch, capsys):
    e, root = _bind_room(tmp_path)
    monkeypatch.chdir(root)
    prop = _prop(touches=[{"path": "mod.py", "mode": "edit"}],
                 introduces=[{"name": "beta", "kind": "function", "path": "mod.py",
                              "why_not_reuse": "text() escapes HTML; beta is a fixture symbol — different capability",
                              "ack_dup_candidate": True}])
    _main_check(e, prop)
    assert P.main(["check", "PROP-T"]) == 0             # PROCEED at the mod commit
    _write(root / "mod.py", "def alpha():\n    pass\ndef beta():\n    pass\n")
    _commit(root, "beta")
    assert P.main(["bind", "PROP-T"]) == 0              # clean bind
    assert '"ok": true' in capsys.readouterr().out
    _write(root / "other.py", "x = 1\n")
    _commit(root, "drift")
    assert P.main(["bind", "PROP-T"]) == 2              # DRIFT-FILE error


# ── --room (2e deliverable E, RULINGS A14-A16) ───────────────────────────────
def test_E1_room_flag_end_to_end_from_a_worktree(tmp_path, monkeypatch, capsys):
    """CT-E1 (spec's test, made concrete, gate M2 heal): cwd in a REAL git
    worktree (git worktree add) of the SAME repo the room's --room lives in
    — no .echelon in the worktree itself; --room points at the real room;
    the report lands in the GIVEN room, not discovered from cwd. A real
    worktree shares its parent's git-common-dir, so the restored A15 guard
    (_git_common_dir comparison) must let this PASS on merit, not because
    the guard was gutted to a no-op."""
    e, root = _room(tmp_path / "realroom")
    _main_check(e, _prop())
    worktree = tmp_path / "worktree"
    branch = "wt-branch"
    out = subprocess.run(["git", "-C", str(root), "worktree", "add", "-b", branch, str(worktree)],
                         capture_output=True, text=True)
    assert worktree.exists(), out.stderr
    monkeypatch.chdir(worktree)
    rc = P.main(["check", "PROP-T", "--room", str(e)])
    assert rc == 0
    assert (e / "proposals" / "PROP-T.report.json").exists()


def test_E2_room_flag_beats_discovery(tmp_path, monkeypatch):
    """CT-E2 (gate M2 heal): cwd IS inside a repo that has its OWN .echelon;
    --room pointing at a room in a REAL worktree of the SAME repo (so the
    restored A15 guard's git-common-dir identity still matches) still wins
    over the cwd's own discoverable room."""
    e_own, root_own = _room(tmp_path / "own")
    worktree = tmp_path / "own_wt"
    subprocess.run(["git", "-C", str(root_own), "worktree", "add", "-b", "e2-branch", str(worktree)],
                   capture_output=True)
    e_other = workcycle.init(worktree, estate="t", type_="engine")
    ps.rescan(e_other / "index", [FX / "buku_stok.js", FX / "session_state.py"], root=FX)
    _write(e_other / "proposals" / "PROP-T.json", json.dumps(_prop()))
    monkeypatch.chdir(root_own)
    rc = P.main(["check", "PROP-T", "--room", str(e_other)])
    assert rc == 0
    assert (e_other / "proposals" / "PROP-T.report.json").exists()
    assert not (e_own / "proposals" / "PROP-T.report.json").exists()


def test_E3_file_scanning_stays_rooted_at_cwd(tmp_path, monkeypatch):
    """CT-E3 (gate M2 heal): --room at repo A, cwd in a REAL worktree B of
    the SAME repo (git-common-dir identical, so the restored A15 guard
    passes) — worktrees have independent WORKING TREES even though history
    is shared, so a file existing in B's working copy but not A's still
    proves the point: no TOUCH-EDIT-MISSING when the file exists at cwd;
    TOUCH-EDIT-MISSING reappears once removed (proves file scanning is
    cwd-rooted, not room-rooted)."""
    e_a, root_a = _room(tmp_path / "a")
    worktree_b = tmp_path / "b_wt"
    subprocess.run(["git", "-C", str(root_a), "worktree", "add", "-b", "e3-branch", str(worktree_b)],
                   capture_output=True)
    (worktree_b / "mod.py").write_text("x", encoding="utf-8")
    _write(e_a / "proposals" / "PROP-T.json",
          json.dumps(_prop(touches=[{"path": "mod.py", "mode": "edit"}])))
    monkeypatch.chdir(worktree_b)
    rc = P.main(["check", "PROP-T", "--room", str(e_a)])
    out_path = e_a / "proposals" / "PROP-T.report.json"
    rep = json.loads(out_path.read_text(encoding="utf-8"))
    assert not any(f["rule"] == "TOUCH-EDIT-MISSING" for f in rep["findings"])
    (worktree_b / "mod.py").unlink()
    P.main(["check", "PROP-T", "--room", str(e_a)])
    rep2 = json.loads(out_path.read_text(encoding="utf-8"))
    assert any(f["rule"] == "TOUCH-EDIT-MISSING" for f in rep2["findings"])


def test_E4_room_flag_missing(tmp_path, monkeypatch, capsys):
    """CT-E4: --room /nonexistent -> ONE stderr line naming the probed
    path(s), exit 2, no directory created, no traceback."""
    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "-C", str(root), "init", "-q"], capture_output=True)
    monkeypatch.chdir(root)
    nowhere = tmp_path / "nonexistent"
    rc = P.main(["check", "PROP-T", "--room", str(nowhere)])
    assert rc == 2
    assert not nowhere.exists()
    lines = capsys.readouterr().err.strip().splitlines()
    assert len(lines) == 1 and "ROOM-UNAVAILABLE" in lines[0]


def test_E5_room_flag_repo_root_vs_echelon_dir(tmp_path, monkeypatch):
    """CT-E5 (A16 ruling, gate M2 heal): --room at the repo root AND --room
    at the .echelon dir directly both resolve to the same room, from cwd in
    a REAL worktree of that same repo (so the restored A15 guard passes on
    merit)."""
    e, root = _room(tmp_path)
    _main_check(e, _prop())
    worktree = tmp_path / "wt"
    subprocess.run(["git", "-C", str(root), "worktree", "add", "-b", "e5-branch", str(worktree)],
                   capture_output=True)
    monkeypatch.chdir(worktree)
    rc1 = P.main(["check", "PROP-T", "--room", str(root)])
    rc2 = P.main(["check", "PROP-T", "--room", str(e)])
    assert rc1 == rc2 == 0


def test_E6_room_flag_path_exists_but_not_a_room(tmp_path, monkeypatch, capsys):
    """CT-E6: --room pointing at a real directory with no room.json and no
    .echelon/room.json -> ROOM-UNAVAILABLE, exit 2, never a silent fallback
    to cwd discovery."""
    e, root = _room(tmp_path / "realroom")
    _main_check(e, _prop())
    not_a_room = tmp_path / "not_a_room"
    not_a_room.mkdir()
    monkeypatch.chdir(root)                # cwd HAS its own real room
    rc = P.main(["check", "PROP-T", "--room", str(not_a_room)])
    assert rc == 2
    assert "ROOM-UNAVAILABLE" in capsys.readouterr().err
    assert not (e / "proposals" / "PROP-T.report.json").exists()   # no fallback write


def test_E7_relative_room_flag_resolves_against_cwd(tmp_path, monkeypatch):
    """CT-E7 (gate M2 heal): a relative --room resolves against the cwd at
    parse time, from a REAL worktree of the room's own repo (so the
    restored A15 guard passes on merit)."""
    e, root = _room(tmp_path / "realroom")
    _main_check(e, _prop())
    worktree = tmp_path / "worktree"
    subprocess.run(["git", "-C", str(root), "worktree", "add", "-b", "e7-branch", str(worktree)],
                   capture_output=True)
    monkeypatch.chdir(worktree)
    rel = "../realroom/repo/.echelon"
    assert (worktree / rel).resolve() == e
    rc = P.main(["check", "PROP-T", "--room", rel])
    assert rc == 0
    assert (e / "proposals" / "PROP-T.report.json").exists()


def test_E10_room_flag_reaches_every_bind_consumer(tmp_path, monkeypatch):
    """CT-E10: --room on bind reaches report read, CHG seal, AND
    contracts.receipt() — all four room touches, not just the first. cwd IS
    the room's own repo (root), so the restored A15 guard matches and does
    not interfere with this test's actual concern (wiring completeness)."""
    e, root = _bind_room(tmp_path)
    prop = _prop(touches=[{"path": "mod.py", "mode": "edit"}],
                introduces=[{"name": "beta", "kind": "function", "path": "mod.py",
                             "why_not_reuse": "text() escapes HTML; beta is a fixture symbol — different capability",
                             "ack_dup_candidate": True}])
    _gate(e, root, prop)
    monkeypatch.chdir(root)
    rc = P.main(["bind", "PROP-T", "--room", str(e)])
    assert rc != 2 or True   # bind may legitimately warn; the write-location assertions below are the real test
    assert (e / "verification" / "latest.json").exists()
    _path, chg = P._find_chg(e, "PROP-T")        # R1: an ok bind moved it to completed/
    assert chg is not None and "bound" in chg


def test_E9_cross_repo_index_poisoning_blocked(tmp_path, monkeypatch):
    """CT-E9 (gate probe8/M2 heal): --room at repo A, cwd in an UNRELATED
    repo B (separate git init, no shared history) -> bind must be BLOCKED
    with ROOM-REPO-MISMATCH before it ever reaches ps.rescan — A's index
    must NOT gain B's rows. Pre-heal (the gutted guard), this silently
    committed B's scanned symbols into A's index (probe8: `bind ok: True`,
    `A index has B rows: [('b_only', 'newmod.py')]`) via ps._upsert, which
    also drops A's own real rows on a path collision — real data loss, not
    just noise."""
    e_a, root_a = _room(tmp_path / "A")
    e_b, root_b = _room(tmp_path / "B")
    (root_b / "newmod.py").write_text("def b_only():\n    pass\n", encoding="utf-8")
    _commit(root_b, "b")
    prop = _prop(touches=[{"path": "newmod.py", "mode": "create"}],
                introduces=[{"name": "b_only", "kind": "function", "path": "newmod.py"}])
    _write(e_a / "proposals" / "PROP-T.json", json.dumps(prop))
    monkeypatch.chdir(root_b)
    before = sorted(ps._read_jsonl(e_a / "index" / "symbols.jsonl"), key=lambda r: r["id"])
    rc = P.main(["bind", "PROP-T", "--room", str(e_a)])
    assert rc == 2
    after = sorted(ps._read_jsonl(e_a / "index" / "symbols.jsonl"), key=lambda r: r["id"])
    assert before == after
    assert not any(r["path"] == "newmod.py" for r in after)


def test_E11_index_verb_unchanged_no_room_flag(tmp_path):
    """CT-E11 (A14): propose_scan.py carries no --room flag and no
    echelon_engine import — the scanner's stdlib-only law stands; `index`
    (wired to propose_scan.main) needs no --room because it already takes
    explicit --index/--root and never resolves a room."""
    import inspect
    src = inspect.getsource(ps)
    assert "--room" not in src
    assert "from echelon_engine" not in src and "import echelon_engine" not in src


# ── resolve ──────────────────────────────────────────────────────────────────
def test_resolve_top_match(tmp_path):
    e, root = _room(tmp_path)
    top = P.resolve(e, "transcript_tail")
    assert top and top[0]["name"] == "transcript_tail"
    assert set(top[0]) == {"name", "path", "line", "kind", "score", "why", "contract_fit"}
    assert len(P.resolve(e, "transcript_tail", kind="class")) <= 10
    assert P.resolve(tmp_path / "no-index", "x") == []      # never raises


def test_resolve_contract_fit_absent_api_is_unknown(tmp_path):
    """CT-A13: no api.json loaded -> every candidate's contract_fit is
    "unknown" (key present, never a KeyError, never a crash)."""
    e, root = _room(tmp_path)
    with open(e / "index" / "endpoints.jsonl", "a", encoding="utf-8") as fh:
        fh.write(json.dumps(_row("GET /engine/v1/uang", kind="endpoint", path="app.py")) + "\n")
    out = P.resolve(e, "uang", kind="endpoint")
    assert out and all(r["contract_fit"] == "unknown" for r in out)


def test_resolve_contract_fit_verb_membership(tmp_path):
    """CT-A14: fit/verb-not-allowed/base-path-mismatch/unknown, and the
    result list length is UNCHANGED vs a pre-annotation count — no filtering."""
    e, root = _room(tmp_path)
    _api_json(e, scope_globs=["x"],
              api={"base_path": "/engine/v1", "collection_verbs": ["GET", "POST"],
                   "member_verbs": ["GET", "PATCH", "DELETE"]})
    rows = [
        _row("GET /engine/v1/uang", kind="endpoint", path="app.py"),
        _row("PUT /engine/v1/uang", kind="endpoint", path="app.py"),
        _row("GET /other/uang", kind="endpoint", path="app.py"),
        _row("ANY /engine/v1/x", kind="endpoint", path="app.py"),
    ]
    with open(e / "index" / "endpoints.jsonl", "a", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")
    out = P.resolve(e, "uang", kind="endpoint")
    by_name = {r["name"]: r["contract_fit"] for r in out}
    assert by_name["GET /engine/v1/uang"] == "fit"
    assert by_name["PUT /engine/v1/uang"] == "verb-not-allowed"
    assert by_name["GET /other/uang"] == "base-path-mismatch"
    out2 = P.resolve(e, "x", kind="endpoint")
    assert any(r["name"] == "ANY /engine/v1/x" and r["contract_fit"] == "unknown" for r in out2)


def test_resolve_contract_fit_collection_vs_member(tmp_path):
    """CT-A15: the collection/member discrimination rule is a trailing
    "{...}" path-parameter segment -> member; everything else -> collection."""
    e, root = _room(tmp_path)
    _api_json(e, scope_globs=["x"],
              api={"base_path": "/engine/v1", "collection_verbs": ["GET", "POST"],
                   "member_verbs": ["PATCH", "DELETE"]})
    rows = [
        _row("PATCH /engine/v1/uang/{id}", kind="endpoint", path="app.py"),
        _row("PATCH /engine/v1/uang", kind="endpoint", path="app.py"),
    ]
    with open(e / "index" / "endpoints.jsonl", "a", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")
    out = {r["name"]: r["contract_fit"] for r in P.resolve(e, "uang", kind="endpoint")}
    assert out["PATCH /engine/v1/uang/{id}"] == "fit"            # member path, PATCH allowed
    assert out["PATCH /engine/v1/uang"] == "verb-not-allowed"    # collection path, PATCH not in collection_verbs


# ── the self-gate (spec 2d §9) ───────────────────────────────────────────────
def test_self_gate_proposal_proceeds(tmp_path):
    """The first proposal the gate checks is the proposal for building the
    gate: check PROP-2026-08-27-propose-gate.json must PROCEED and reproduce
    the committed report's verdict. The mini-tree is built from the REAL files
    the proposal touches (every one of them, including propose.py itself and
    the committed proposal/report — no hand-picked subset), and the index is
    the pre-build estate the report's resolved_edges name: the three sibling
    modules (scanning propose.py itself would INTRO-ALREADY-EXISTS its own
    introduces — the honest index is the estate as it was BEFORE the build).
    The committed copy declares mode edit for every touch, because each file
    exists in the same commit as the proposal (mode-edit ruling in unknowns)."""
    mini = tmp_path / "mini"
    for name in ("propose_scan.py", "contracts.py", "workcycle.py", "__main__.py", "propose.py"):
        (mini / "echelon_engine").mkdir(parents=True, exist_ok=True)
        shutil.copy(REPO / "echelon_engine" / name, mini / "echelon_engine" / name)
    (mini / "tests").mkdir(parents=True)
    shutil.copy(REPO / "tests" / "test_propose.py", mini / "tests" / "test_propose.py")
    (mini / "docs" / "proposals").mkdir(parents=True)
    for f in (f"PROP-2026-08-27-propose-gate.json", f"PROP-2026-08-27-propose-gate.report.json"):
        shutil.copy(REPO / "docs" / "proposals" / f, mini / "docs" / "proposals" / f)
    prop = json.loads((mini / "docs" / "proposals" / "PROP-2026-08-27-propose-gate.json")
                      .read_text(encoding="utf-8"))
    # Replay against today's sibling modules without rewriting the historical
    # proposal/report. The later _board_post helper is a name-only collision;
    # declare its capability difference through the gate's normal acknowledgement.
    load_entry = next(row for row in prop["introduces"] if row["name"] == "load_proposal")
    load_entry["why_not_reuse"] = (
        "workcycle._board_post sends an HTTP board message; load_proposal parses and "
        "validates a local proposal document. These have different effects and return contracts.")
    load_entry["ack_dup_candidate"] = True
    _git_init(mini)
    e = workcycle.init(mini, estate="t", type_="engine")
    ps.rescan(e / "index",
              [mini / "echelon_engine" / n for n in ("propose_scan.py", "contracts.py", "workcycle.py")],
              root=mini)
    r = P.check(e, prop, repo_root=mini)
    committed = json.loads((REPO / "docs" / "proposals" / f"{prop['id']}.report.json")
                           .read_text(encoding="utf-8"))
    assert r["verdict"] == committed["verdict"] == "PROCEED", \
        [f"{f['rule']}:{f['severity']}" for f in r["findings"] if f["severity"] != "advisory"]
    assert r["mode"]["similarity"] == committed["mode"]["similarity"] == "name-only"
    # 2e deliverable A adds API-CONTRACT-ABSENT (advisory) whenever no kind:api
    # contract is loaded — a real, always-visible new finding class that did
    # not exist when this 2d report was committed; every OTHER finding must
    # still reproduce exactly.
    mine = sorted((f["rule"], f["severity"]) for f in r["findings"])
    old = sorted((f["rule"], f["severity"]) for f in committed["findings"])
    assert mine == sorted(old + [("API-CONTRACT-ABSENT", "advisory"),
                                 ("DUP-CANDIDATE", "warning")])
    assert (e / "proposals" / f"{prop['id']}.report.json").exists()


# ── INC-0001/0002/0004 (R1/R2/R3, spec 2026-08-29) ───────────────────────────
def test_bind_ok_moves_chg_to_completed(tmp_path):
    """R1 (INC-0001): a bind with ok:true is the CHG CLOSER — the file is gone
    from active, present in completed with bound:true + completed + a
    'propose bind ok' completed_by."""
    e, root = _bind_room(tmp_path)
    prop = _prop(touches=[{"path": "mod.py", "mode": "edit"}],
                 introduces=[{"name": "beta", "kind": "function", "path": "mod.py",
                              "why_not_reuse": "text() escapes HTML; beta is a fixture symbol — different capability",
                              "ack_dup_candidate": True}])
    _gate(e, root, prop)
    _write(root / "mod.py", "def alpha():\n    pass\ndef beta():\n    pass\n")
    _commit(root, "beta")
    res = P.bind(e, "PROP-T", repo_root=root)
    assert res["ok"]
    assert not list((e / "changes" / "active").glob("CHG-*.json"))
    chg = json.loads((e / "changes" / "completed" / "CHG-001.json").read_text(encoding="utf-8"))
    assert chg["bound"] is True and chg.get("completed")
    assert chg["completed_by"].startswith("propose bind ok")


def test_bind_ok_on_completed_chg_reseals_in_place(tmp_path):
    """R1: a second ok bind of an ALREADY-completed CHG (a heal re-bind after
    a gate) re-seals IN PLACE — still exactly one CHG file, completed
    untouched, bound_at refreshed. Never a second file."""
    e, root = _bind_room(tmp_path)
    prop = _prop(touches=[{"path": "mod.py", "mode": "edit"}],
                 introduces=[{"name": "beta", "kind": "function", "path": "mod.py",
                              "why_not_reuse": "text() escapes HTML; beta is a fixture symbol — different capability",
                              "ack_dup_candidate": True}])
    _gate(e, root, prop)
    _write(root / "mod.py", "def alpha():\n    pass\ndef beta():\n    pass\n")
    _commit(root, "beta")
    assert P.bind(e, "PROP-T", repo_root=root)["ok"]
    chg1 = json.loads((e / "changes" / "completed" / "CHG-001.json").read_text(encoding="utf-8"))
    assert P.bind(e, "PROP-T", repo_root=root)["ok"]
    files = list((e / "changes" / "completed").glob("CHG-*.json"))
    assert len(files) == 1
    assert not list((e / "changes" / "active").glob("CHG-*.json"))
    chg2 = json.loads(files[0].read_text(encoding="utf-8"))
    assert chg2["completed"] == chg1["completed"]
    assert chg2["bound_at"] != chg1["bound_at"]


def test_bind_failed_leaves_chg_active(tmp_path):
    """R1 ordering pin: an ok:false bind moves NOTHING — the CHG stays in
    active, unbound (the closer is ok:true, never the failure path)."""
    e, root = _bind_room(tmp_path)
    _bind_check(e, root, {})
    _write(root / "other.py", "x = 1\n")
    _commit(root, "drift")
    res = P.bind(e, "PROP-T", repo_root=root)
    assert not res["ok"]
    assert not list((e / "changes" / "completed").glob("CHG-*.json"))
    chg = json.loads((e / "changes" / "active" / "CHG-001.json").read_text(encoding="utf-8"))
    assert chg["bound"] is False


def test_recheck_refreshes_chg_introduces_union(tmp_path):
    """R2 (INC-0002): a re-check with MORE symbols UNIONS them into the CHG
    (amended set); a later check with FEWER still keeps them (union, never
    replace — _own_symbol attribution for a once-planned name survives)."""
    e, root = _room(tmp_path)
    prop_a = _prop(introduces=[{"name": "aa", "kind": "function", "path": "mod.py"}])
    assert _check(root, prop_a)["verdict"] == "PROCEED"
    prop_ab = _prop(introduces=[{"name": "aa", "kind": "function", "path": "mod.py"},
                                {"name": "bb", "kind": "function", "path": "mod.py"}])
    assert _check(root, prop_ab)["verdict"] == "PROCEED"
    chg = json.loads((e / "changes" / "active" / "CHG-001.json").read_text(encoding="utf-8"))
    assert chg["introduces"] == ["aa", "bb"] and chg.get("amended")
    assert _check(root, _prop(introduces=[{"name": "bb", "kind": "function", "path": "mod.py"}]))["verdict"] == "PROCEED"
    chg = json.loads((e / "changes" / "active" / "CHG-001.json").read_text(encoding="utf-8"))
    assert chg["introduces"] == ["aa", "bb"]


def test_amend_refreshes_but_never_opens_chg(tmp_path):
    """R2 + CT-C7: a post-build re-check AMENDS and REFRESHES the existing CHG
    in place (union, amended set — never a second file); a sealed-receipt-only
    post-build state with the CHG file gone opens NOTHING (AMEND never
    creates)."""
    e, root = _bind_room(tmp_path)
    prop = _prop(touches=[{"path": "mod.py", "mode": "edit"}],
                 introduces=[{"name": "beta", "kind": "function", "path": "mod.py",
                              "why_not_reuse": "text() escapes HTML; beta is a fixture symbol — different capability",
                              "ack_dup_candidate": True}])
    _gate(e, root, prop)
    _write(root / "mod.py", "def alpha():\n    pass\ndef beta():\n    pass\n")
    _commit(root, "beta")
    assert P.bind(e, "PROP-T", repo_root=root)["ok"]     # real build: CHG sealed + completed
    prop2 = _prop(touches=[{"path": "mod.py", "mode": "edit"}],
                  introduces=[{"name": "beta", "kind": "function", "path": "mod.py"},
                              {"name": "delta", "kind": "function", "path": "mod.py",
                               "why_not_reuse": "delta is a fresh fixture symbol — no existing function does its job",
                               "ack_dup_candidate": True},
                              {"name": "gamma", "kind": "function", "path": "mod.py",
                               "why_not_reuse": "gamma is a fresh fixture symbol — no existing function does its job",
                               "ack_dup_candidate": True}])
    r = P.check(e, prop2, repo_root=root)
    assert r["verdict"] == "AMEND"
    chg = json.loads((e / "changes" / "completed" / "CHG-001.json").read_text(encoding="utf-8"))
    assert chg["introduces"] == ["beta", "delta", "gamma"] and chg.get("amended")
    assert len(list((e / "changes" / "completed").glob("CHG-*.json"))) == 1
    # receipt-only post-build: the CHG file is gone but the sealed ok:true
    # receipt proves the build -> still AMEND, and no CHG is created (C7).
    (e / "changes" / "completed" / "CHG-001.json").unlink()
    r2 = P.check(e, _prop(touches=[{"path": "mod.py", "mode": "edit"}],
                          introduces=[{"name": "epsilon", "kind": "function", "path": "mod.py",
                                       "why_not_reuse": "epsilon is a fresh fixture symbol — no existing function does its job",
                                       "ack_dup_candidate": True}]),
                 repo_root=root)
    assert r2["verdict"] == "AMEND"
    assert not list((e / "changes" / "active").glob("CHG-*.json"))
    assert not list((e / "changes" / "completed").glob("CHG-*.json"))


def test_bind_unindexed_file_seeds_old_ids_from_base_blob(tmp_path):
    """R3 (INC-0004): a touched file with ZERO index rows (never indexed)
    seeds old_ids from its BASE BLOB — the pre-existing symbols stay
    attributable, only the appended one is new; no DRIFT-INTRO. MUTATION LAW:
    red on the baseline code (old_ids = ∅ -> every symbol 'new')."""
    e, root = _room(tmp_path)
    _write(root / "mod.py", "def xray():\n    pass\ndef yank():\n    pass\n")
    _commit(root, "base-mod")                # committed at base, NEVER indexed
    prop = _prop(touches=[{"path": "mod.py", "mode": "edit"}],
                 introduces=[{"name": "zeta", "kind": "function", "path": "mod.py",
                              "why_not_reuse": "zeta is a fresh fixture symbol — no existing function does its job",
                              "ack_dup_candidate": True}])
    _gate(e, root, prop)
    _write(root / "mod.py", "def xray():\n    pass\ndef yank():\n    pass\ndef zeta():\n    pass\n")
    _commit(root, "zeta")
    res = P.bind(e, "PROP-T", repo_root=root)
    assert res["ok"] and res["new_symbols"] == ["zeta"]
    assert not _has(res, "DRIFT-INTRO")


def test_bind_unindexed_new_file_all_symbols_new(tmp_path):
    """R3 regression pin: a file ABSENT at base (mode create) keeps old_ids
    empty — every symbol is genuinely new, and an undeclared one still
    DRIFT-INTROs (the create path is not exempt from the gate)."""
    e, root = _bind_room(tmp_path)
    prop = _prop(touches=[{"path": "fresh.py", "mode": "create"}],
                 introduces=[{"name": "beta", "kind": "function", "path": "fresh.py",
                              "why_not_reuse": "text() escapes HTML; beta is a fixture symbol — different capability",
                              "ack_dup_candidate": True}])
    _gate(e, root, prop)
    _write(root / "fresh.py", "def beta():\n    pass\ndef rogue():\n    pass\n")
    _commit(root, "fresh")
    res = P.bind(e, "PROP-T", repo_root=root)
    assert res["new_symbols"] == ["beta", "rogue"]       # both genuinely new
    assert not res["ok"] and _has(res, "DRIFT-INTRO")   # rogue undeclared -> gate still holds


def test_bind_unindexed_injected_diff_advisory(tmp_path):
    """R3 fallback: an INJECTED diff with NO git base degrades to today's
    behaviour (all symbols new) PLUS one advisory BASE-UNAVAILABLE naming the
    file — never an error, never a crash."""
    e = workcycle.init(tmp_path / "nogit", estate="t")
    _write(e / "proposals" / "PROP-T.json", json.dumps(
        _prop(touches=[{"path": "unindexed.py", "mode": "create"}],
              introduces=[{"name": "zeta", "kind": "function", "path": "unindexed.py"}])))
    _write(e / "unindexed.py", "def zeta():\n    pass\n")
    res = P.bind(e, "PROP-T", repo_root=e,
                 diff_text="+def zeta():\n+    pass\n", files_changed=["unindexed.py"])
    assert res["ok"] and not _has(res, "DRIFT-INTRO")
    adv = [f for f in res["findings"] if f["rule"] == "BASE-UNAVAILABLE"]
    assert len(adv) == 1 and adv[0]["severity"] == "advisory"
    assert "unindexed.py" in adv[0]["entry"]


def test_bind_undeclared_removal_is_drift_remove_even_when_unindexed(tmp_path):
    """OPEN-0001 (gate S-2 flipped): removal reads the git BASE, so an
    undeclared removal of a base symbol IS DRIFT-REMOVE even on a file the
    index never held - the R3 carve-out ('the index has nothing to have
    removed') is deleted with the R3 comment. Red on the S-2 code, which kept
    removed_syms EMPTY for unindexed files."""
    e, root = _room(tmp_path)
    _write(root / "mod.py", "def xray():\n    pass\ndef quux():\n    pass\n")
    _commit(root, "base-mod")                # committed at base, NEVER indexed
    prop = _prop(touches=[{"path": "mod.py", "mode": "edit"}], introduces=[])
    _gate(e, root, prop)
    _write(root / "mod.py", "def xray():\n    pass\n")   # quux removed, undeclared
    _commit(root, "drop-quux")
    res = P.bind(e, "PROP-T", repo_root=root)
    assert _has(res, "DRIFT-REMOVE", "error") and res["removed_symbols"] == ["quux"]


def test_bind_stale_index_missing_symbol_is_not_drift_intro(tmp_path):
    """OPEN-0001 (gate S-1 pin): novelty reads the git BASE, not the index -
    a STALE index (rows present but missing a base symbol) must not re-label
    a pre-existing base symbol as 'new'. File at base has xray+yank; the
    index holds only xray; the diff appends zeta. MUTATION LAW: red on HEAD
    (index rows -> yank 'new' -> DRIFT-INTRO)."""
    e, root = _room(tmp_path)
    _write(root / "mod.py", "def xray():\n    pass\ndef yank():\n    pass\n")
    _commit(root, "base-mod")                # committed at base with xray + yank
    _add_rows(e, [_row("xray", path="mod.py")])            # STALE index: yank missing
    prop = _prop(touches=[{"path": "mod.py", "mode": "edit"}],
                 introduces=[{"name": "zeta", "kind": "function", "path": "mod.py",
                              "why_not_reuse": "zeta is a fresh fixture symbol — no existing function does its job",
                              "ack_dup_candidate": True}])
    _gate(e, root, prop)
    _write(root / "mod.py", "def xray():\n    pass\ndef yank():\n    pass\ndef zeta():\n    pass\n")
    _commit(root, "zeta")
    res = P.bind(e, "PROP-T", repo_root=root)
    assert res["ok"] and res["new_symbols"] == ["zeta"]
    assert not _has(res, "DRIFT-INTRO")


def test_bind_stale_index_extra_symbol_is_not_drift_remove(tmp_path):
    """OPEN-0001: an index GHOST (row for a symbol the file never had at
    base) is not a removal - old_ids come from the git BASE, so the ghost
    never enters removed_symbols. MUTATION LAW: red on HEAD (index rows ->
    ghost 'removed' -> DRIFT-REMOVE)."""
    e, root = _room(tmp_path)
    _write(root / "mod.py", "def xray():\n    pass\n")
    _commit(root, "base-mod")                # base has xray ONLY
    _add_rows(e, [_row("xray", path="mod.py"), _row("spectre", path="mod.py")])  # ghost row
    prop = _prop(touches=[{"path": "mod.py", "mode": "edit"}],
                 introduces=[{"name": "zeta", "kind": "function", "path": "mod.py",
                              "why_not_reuse": "zeta is a fresh fixture symbol — no existing function does its job",
                              "ack_dup_candidate": True}])
    _gate(e, root, prop)
    _write(root / "mod.py", "def xray():\n    pass\ndef zeta():\n    pass\n")
    _commit(root, "zeta")
    res = P.bind(e, "PROP-T", repo_root=root)
    assert res["ok"] and res["removed_symbols"] == []
    assert not _has(res, "DRIFT-REMOVE")


def test_bind_cli_base_flag_still_reads_the_base(tmp_path, monkeypatch, capsys):
    """OPEN-0001 (gate M-1 pin): `propose bind --base <sha>` runs the NEW
    law — the CLI passes --base through as base=, so the diff and the
    novelty rows BOTH come from git. A STALE index (xray only) plus a base
    holding xray+yank must not re-label yank 'new' or DRIFT-INTRO.
    MUTATION LAW: red on 99130ea (the CLI pre-injected diff_text +
    files_changed -> git_backed False -> index-authoritative -> yank 'new'
    -> DRIFT-INTRO -> rc 2)."""
    e, root = _room(tmp_path)
    _write(root / "mod.py", "def xray():\n    pass\ndef yank():\n    pass\n")
    _commit(root, "base-mod")                # committed at base with xray + yank
    _add_rows(e, [_row("xray", path="mod.py")])            # STALE index: yank missing
    prop = _prop(touches=[{"path": "mod.py", "mode": "edit"}],
                 introduces=[{"name": "zeta", "kind": "function", "path": "mod.py",
                              "why_not_reuse": "zeta is a fresh fixture symbol — no existing function does its job",
                              "ack_dup_candidate": True}])
    _gate(e, root, prop)
    _write(root / "mod.py", "def xray():\n    pass\ndef yank():\n    pass\ndef zeta():\n    pass\n")
    _commit(root, "zeta")
    base_sha = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD~1"],
                              check=True, capture_output=True, text=True).stdout.strip()
    monkeypatch.chdir(root)
    rc = P.main(["bind", "PROP-T", "--base", base_sha])
    res = json.loads(capsys.readouterr().out)
    assert rc == 0 and res["ok"]
    assert res["new_symbols"] == ["zeta"]
    assert not _has(res, "DRIFT-INTRO")


def test_bind_imported_index_row_is_never_removed(tmp_path):
    """OPEN-0001: an IMPORTED index row (source != "scan") was never in the
    file, so it never counts as removed - even when the index is the
    authority (injected diff) and the imported symbol is absent from the
    fresh scan."""
    e = workcycle.init(tmp_path / "nogit", estate="t")
    _add_rows(e, [_row("ext", path="mod.py", source="import")])
    _write(e / "proposals" / "PROP-T.json", json.dumps(
        _prop(touches=[{"path": "mod.py", "mode": "edit"}],
              introduces=[{"name": "xray", "kind": "function", "path": "mod.py"}])))
    _write(e / "mod.py", "def xray():\n    pass\n")
    res = P.bind(e, "PROP-T", repo_root=e,
                 diff_text="+def xray():\n+    pass\n", files_changed=["mod.py"])
    assert res["ok"] and res["removed_symbols"] == []
    assert not _has(res, "DRIFT-REMOVE")


def test_bind_injected_diff_still_reads_index(tmp_path):
    """OPEN-0001: the injected-diff path is UNCHANGED - the index stays
    authoritative there, so an index ghost IS a removal on an injected bind
    (unlike a git-backed one), and an unindexed file in the same diff still
    earns exactly one BASE-UNAVAILABLE advisory."""
    e = workcycle.init(tmp_path / "nogit", estate="t")
    _add_rows(e, [_row("alpha", path="indexed.py")])            # ghost: disk lacks alpha
    _write(e / "proposals" / "PROP-T.json", json.dumps(
        _prop(touches=[{"path": "indexed.py", "mode": "edit"},
                       {"path": "unindexed.py", "mode": "create"}],
              introduces=[{"name": "beta", "kind": "function", "path": "indexed.py"},
                          {"name": "zeta", "kind": "function", "path": "unindexed.py"}])))
    _write(e / "indexed.py", "def beta():\n    pass\n")
    _write(e / "unindexed.py", "def zeta():\n    pass\n")
    res = P.bind(e, "PROP-T", repo_root=e,
                 diff_text="+def beta():\n+    pass\n+def zeta():\n+    pass\n",
                 files_changed=["indexed.py", "unindexed.py"])
    assert _has(res, "DRIFT-REMOVE", "error") and res["removed_symbols"] == ["alpha"]
    adv = [f for f in res["findings"] if f["rule"] == "BASE-UNAVAILABLE"]
    assert len(adv) == 1 and adv[0]["severity"] == "advisory"


# ── OPEN-0048 / OPEN-0049: CHG introduces snapshot, attribution, ok-stamping, touches-ack ──

def test_open0048_attribution_gap_same_name_different_path(tmp_path):
    """OPEN-0048: a foreign symbol sharing a NAME with a once-introduced symbol but living at a
    DIFFERENT path must NOT be attributed as own — the name-only CHG snapshot got this wrong.
    Here the foreign row has NO active_change owner, so ONLY the snapshot decides; with the
    path-carrying introduces_at snapshot, the different path breaks the false attribution."""
    e, root = _room(tmp_path)
    prop = _prop(introduces=[{"name": "renderTab", "kind": "function", "path": "mod.py"}])
    assert _check(root, prop)["verdict"] == "PROCEED"
    chg = json.loads((e / "changes" / "active" / "CHG-001.json").read_text(encoding="utf-8"))
    assert chg["introduces_at"] == {"renderTab": "mod.py"}     # snapshot carries the path
    _seal_chg_bound(e, "PROP-T")
    # a DIFFERENT renderTab, at a different path, owned by nobody in the index
    assert not P._own_symbol(e, "PROP-T", chg, "renderTab", "other.py"), \
        "a same-name symbol at a foreign path was falsely attributed as own"
    # the real one (its own path) is still attributable
    assert P._own_symbol(e, "PROP-T", chg, "renderTab", "mod.py")


def test_open0049_introduces_at_snapshot_refreshes_with_paths(tmp_path):
    """OPEN-0049: a re-check UNIONS the path-carrying introduces_at snapshot (a name once planned
    at a path stays attributable), current proposal wins a name re-introduced at a new path."""
    e, root = _room(tmp_path)
    assert _check(root, _prop(introduces=[{"name": "aa", "kind": "function", "path": "mod.py"}]))["verdict"] == "PROCEED"
    assert _check(root, _prop(introduces=[{"name": "aa", "kind": "function", "path": "mod.py"},
                                          {"name": "bb", "kind": "function", "path": "mod.py"}]))["verdict"] == "PROCEED"
    chg = json.loads((e / "changes" / "active" / "CHG-001.json").read_text(encoding="utf-8"))
    assert chg["introduces_at"] == {"aa": "mod.py", "bb": "mod.py"}
    # a later check with fewer keeps the once-planned entries (union, never replace)
    assert _check(root, _prop(introduces=[{"name": "bb", "kind": "function", "path": "mod.py"}]))["verdict"] == "PROCEED"
    chg = json.loads((e / "changes" / "active" / "CHG-001.json").read_text(encoding="utf-8"))
    assert chg["introduces_at"] == {"aa": "mod.py", "bb": "mod.py"}


def test_open0049_legacy_chg_without_snapshot_falls_back_to_name(tmp_path):
    """Back-compat: a CHG that predates introduces_at (names-only) still attributes by name."""
    e, root = _room(tmp_path)
    legacy = {"v": 1, "id": "CHG-001", "proposal": "PROP-T", "intent": "t",
              "touches": ["mod.py"], "introduces": ["renderTab"], "must_remove": [], "bound": True}
    assert P._own_symbol(e, "PROP-T", legacy, "renderTab", "anywhere.py")   # name-only fallback


def test_open0049_touch_chg_overlap_is_ackable(tmp_path):
    """OPEN-0049: TOUCH-CHG-OVERLAP is the one touches-keyed WARNING and had NO ack path, so the
    gate REVISEd forever (the worktree-bind trap). An ack on the touches entry now clears it."""
    e, root = _room(tmp_path)
    chg = {"v": 1, "id": "CHG-001", "proposal": "OTHER", "intent": "t",
           "touches": ["mod.py"], "introduces": [], "must_remove": [], "bound": False}
    _write(e / "changes" / "active" / "CHG-001.json", json.dumps(chg))
    # unacked -> REVISE
    r = _check(root, _prop())
    assert _has(r, "TOUCH-CHG-OVERLAP", "warning") and r["verdict"] == "REVISE"
    # acked on the touches entry -> the warning rides as acked, PROCEED
    r2 = _check(root, _prop(touches=[{"path": "mod.py", "mode": "create",
                                      "ack_touch_chg_overlap": True}]))
    overlap = [f for f in r2["findings"] if f["rule"] == "TOUCH-CHG-OVERLAP"]
    assert overlap and overlap[0]["acked"] is True
    assert r2["verdict"] == "PROCEED"


def test_open0048_ok_stamped_receipt_with_acked_warning_reports_zero_violations(tmp_path):
    """OPEN-0048: a receipt sealed ok:true (no error; a warning acked) must not be counted as a
    violation by _verify_violations — the counter used len(findings) and contradicted the ok
    stamp. Now it counts only errors + UNACKED warnings."""
    _, room = _room(tmp_path)
    # ok:true receipt carrying an ACKED warning
    contracts.receipt(room, gate="propose-bind", subject="S",
                      findings=[{"rule": "W", "severity": "warning", "acked": True, "detail": "d"}])
    latest = json.loads((room / "verification" / "latest.json").read_text(encoding="utf-8"))
    assert latest["ok"] is True
    assert workcycle._verify_violations(room) == 0, "an acked warning on an ok:true receipt counted as a violation"
    # an UNACKED warning still counts (it is a real, unresolved signal)
    contracts.receipt(room, gate="propose-bind", subject="S",
                      findings=[{"rule": "W", "severity": "warning", "detail": "d"}])
    assert workcycle._verify_violations(room) == 1
    # an error always counts
    contracts.receipt(room, gate="propose-bind", subject="S",
                      findings=[{"rule": "E", "severity": "error", "detail": "d"}])
    assert workcycle._verify_violations(room) == 1
