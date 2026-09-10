"""Tests for echelon_engine.propose_scan — the in-process symbol scanner.

Fixture-test law (PROPOSAL-GATE §7): the scanner runs over COPIES of REAL
estate files (tests/fixtures/propose_scan/) and the expected symbols are
asserted with their real lines — a regex scanner that mis-parses the estate
collapses the gate. Parser-feature tests not present in the estate (js object
literals, exports, Flask routes, JSON .des) use small hand-written inputs and
are marked as such.
"""
import json
from pathlib import Path

from echelon_engine import propose_scan as ps

FX = Path(__file__).parent / "fixtures" / "propose_scan"
REQUIRED_KEYS = {"v", "id", "name", "kind", "path", "line", "signature",
                 "canonical", "deprecated", "replaced_by", "active_change",
                 "semantic_tags", "domain", "source"}


def _rows(path: str):
    return ps.scan_file(FX / path, root=FX)


def _find(rows, name, kind):
    return next((r for r in rows if r["name"] == name and r["kind"] == kind), None)


# ── the fixture-test law: real estate files ───────────────────────────────────
def test_js_buku_stok_real_file():
    rows = _rows("buku_stok.js")
    assert _find(rows, "$", "function")["line"] == 27
    text = _find(rows, "text", "function")
    assert text["line"] == 32 and text["signature"] == "(value)"
    const = _find(rows, "UNMEASURED", "const")
    assert const["line"] == 23 and "belum terukur" in const["signature"]
    # comment-only mentions produce NO rows (stripper proof):
    assert _find(rows, "ledger_retry_confirm", "function") is None
    # `uniquifyClone` is mentioned in a comment at line ~13 AND declared at 75:
    assert _find(rows, "uniquifyClone", "function")["line"] == 75


def test_py_routes_real_file():
    rows = _rows("belanja_routes.py")
    ep = _find(rows, "GET /api/belanja/overview", "endpoint")
    decorator_line = next(i + 1 for i, ln in enumerate(
        (FX / "belanja_routes.py").read_text(encoding="utf-8").splitlines())
        if ln.strip() == '@router.get("/api/belanja/overview")')
    assert ep is not None
    assert ep["line"] == decorator_line and ep["signature"] == "belanja_overview"
    assert _find(rows, "belanja_overview", "function") is not None
    assert _find(rows, "_board", "function") is not None


def test_py_session_state_real_file():
    rows = _rows("session_state.py")
    assert _find(rows, "transcript_tail", "function") is not None
    cls = _find(rows, "SessionLedger", "class")
    assert cls is not None and cls["line"] == 67
    cp = _find(rows, "SessionLedger.checkpoint", "function")
    assert cp is not None and cp["signature"].startswith("(self")
    const = _find(rows, "VALID_WORTH", "const")
    assert const is not None and "carry" in const["signature"]


def test_des_real_file():
    rows = _rows("wave1-substrate.des")
    assert _find(rows, "clock", "component")["line"] == 5
    assert _find(rows, "boot_hardener", "component")["line"] == 35
    assert len(rows) == 2


def test_row_schema_complete_on_all_fixtures():
    for name in ("buku_stok.js", "belanja_routes.py", "session_state.py",
                 "wave1-substrate.des"):
        for row in _rows(name):
            assert set(row) == REQUIRED_KEYS, name
            assert row["v"] == 1 and row["source"] == "scan"
            assert row["id"] == f"{row['kind']}:{row['path']}#{row['name']}"
            assert row["path"] == name and row["line"] >= 0


# ── parser features absent from the estate (hand-written inputs) ──────────────
def test_js_object_literal_methods_and_arrows(tmp_path):
    src = tmp_path / "obj.js"
    src.write_text("""const api = {
  load: function() { return 1; },
  save(x) { return x; },
  deeper: {
    nested: function() {}
  },
      ignored: function() {}
};
const fetchData = () => 1;
""", encoding="utf-8")
    rows = ps.scan_file(src, root=tmp_path)
    for name in ("load", "save", "nested", "fetchData"):
        assert _find(rows, name, "function") is not None, name
    assert _find(rows, "ignored", "function") is None      # col 6 > 4
    assert _find(rows, "if", "function") is None           # keyword guard


def test_js_exports(tmp_path):
    src = tmp_path / "exp.js"
    src.write_text("""export function helper() {}
export { thing, other };
module.exports.run = run;
module.exports = main;
""", encoding="utf-8")
    rows = ps.scan_file(src, root=tmp_path)
    for name in ("helper", "thing", "other", "run", "main"):
        assert _find(rows, name, "export") is not None, name
    assert _find(rows, "helper", "function") is not None   # decl row too


def test_py_flask_route(tmp_path):
    src = tmp_path / "app.py"
    src.write_text("""from flask import Flask
app = Flask(__name__)
@app.route("/x", methods=["POST"])
def do_x(): pass
@app.get("/y")
def get_y(): pass
""", encoding="utf-8")
    rows = ps.scan_file(src, root=tmp_path)
    assert _find(rows, "POST /x", "endpoint")["signature"] == "do_x"
    assert _find(rows, "GET /y", "endpoint")["signature"] == "get_y"


def test_des_json_shape(tmp_path):
    src = tmp_path / "page.des"
    src.write_text('{"nodes": [{"id": "btn", "component": "Button"}, {"op": "merge"}]}',
                   encoding="utf-8")
    rows = ps.scan_file(src, root=tmp_path)
    names = {r["name"] for r in rows}
    assert names == {"Button", "merge"}


# ── usages ────────────────────────────────────────────────────────────────────
def test_usages_call_attr_import_only():
    diff = """+++ b/pages/buku_stok.js
@@ -1,0 +1,5 @@
+ window.LX.fmt.money(value)
+ renderX(data)
+ // renderX(stale) — a comment must not leak
+ import { debounce } from "./lib/utils.js"
- gone(removed)
+ total = 3.14
"""
    assert ps.usages(diff) == {"LX", "fmt", "money", "renderX", "debounce"}


def test_usages_python_diff():
    diff = """+from echelon_sdk.chain import ChainResult
+def wrap():
+    return ChainResult.of(ctx).pipe(build)  # renderX() in a comment
"""
    # `of` is keyword-filtered (JS for..of); ctx/build are bare args, not
    # calls/attrs; the comment's renderX() never leaks.
    assert ps.usages(diff) == {"echelon_sdk", "chain", "ChainResult", "wrap", "pipe"}


# ── rescan: drop vs preserve, merge vs clobber ────────────────────────────────
def _write_mod(path: Path, body: str):
    path.write_text(body, encoding="utf-8")


def test_rescan_drop_vs_preserve(tmp_path):
    mod = tmp_path / "mod.py"
    _write_mod(mod, "def alpha():\n    pass\ndef beta():\n    pass\ndef gamma():\n    pass\n")
    assert ps.rescan(tmp_path / "idx", [mod], root=tmp_path) == {"added": 3, "updated": 0, "dropped": 0}
    # an imported row whose path is the rescanned one must SURVIVE the drop:
    idx = tmp_path / "idx"
    imported = ps._row(kind="token", name="kept_token", rel="mod.py", line=0,
                       source="_index/atlas.json#tokens/x")
    with open(idx / "symbols.jsonl", "a", encoding="utf-8") as fh:
        fh.write(json.dumps(imported) + "\n")
    _write_mod(mod, "def alpha():\n    pass\ndef omega():\n    pass\n")
    result = ps.rescan(idx, [mod], root=tmp_path)
    assert result == {"added": 1, "updated": 1, "dropped": 2}
    names = {r["name"] for r in ps.lookup(idx, "alpha") + ps.lookup(idx, "beta")}
    assert "alpha" in names and "beta" not in names
    assert ps.lookup(idx, "kept_token", "token")            # imported row survived


def test_rescan_merge_preserves_curated(tmp_path):
    idx = tmp_path / "idx"
    fx = FX / "session_state.py"
    ps.rescan(idx, [fx], root=FX)
    rows = [json.loads(ln) for ln in (idx / "symbols.jsonl").read_text(encoding="utf-8").splitlines()]
    for row in rows:
        if row["name"] == "transcript_tail":
            row["canonical"] = True
            row["semantic_tags"] = ["ledger", "tail"]
            row["domain"] = "engine"
            break
    with open(idx / "symbols.jsonl", "w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row) + "\n")
    ps.rescan(idx, [fx], root=FX)
    row = ps.lookup(idx, "transcript_tail")[0]
    assert row["canonical"] is True and row["domain"] == "engine"
    assert row["semantic_tags"] == ["ledger", "tail"] and row["line"] > 0


def test_rescan_endpoint_split(tmp_path):
    idx = tmp_path / "idx"
    fx = FX / "belanja_routes.py"
    ps.rescan(idx, [fx], root=FX)
    assert ps.lookup(idx, "GET /api/belanja/overview", "endpoint")
    assert not ps.lookup(idx, "GET /api/belanja/overview", "function")


# ── lookup / similar ──────────────────────────────────────────────────────────
def test_lookup_exact(tmp_path):
    idx = tmp_path / "idx"
    ps.rescan(idx, [FX / "session_state.py"], root=FX)
    hits = ps.lookup(idx, "transcript_tail")
    assert len(hits) == 1 and hits[0]["kind"] == "function"
    assert ps.lookup(idx, "transcript_tail", "class") == []
    assert ps.lookup(idx, "SessionLedger", "class")[0]["name"] == "SessionLedger"


def test_similar_ordering_hand_written_rows(tmp_path):
    idx = tmp_path / "idx"
    idx.mkdir()
    rows = [
        {"v": 1, "id": "function:mod.py#po_create", "name": "po_create", "kind": "function",
         "path": "mod.py", "line": 1, "signature": "(board, payload)", "canonical": False,
         "deprecated": False, "replaced_by": None, "active_change": None,
         "semantic_tags": ["po", "create", "order"], "domain": "po", "source": "scan"},
        {"v": 1, "id": "function:mod.py#po_list", "name": "po_list", "kind": "function",
         "path": "mod.py", "line": 2, "signature": "(q)", "canonical": False,
         "deprecated": False, "replaced_by": None, "active_change": None,
         "semantic_tags": ["po", "list"], "domain": "po", "source": "scan"},
        {"v": 1, "id": "function:mod.py#kpi_row", "name": "kpi_row", "kind": "function",
         "path": "mod.py", "line": 3, "signature": "(summary)", "canonical": False,
         "deprecated": False, "replaced_by": None, "active_change": None,
         "semantic_tags": ["kpi", "tile"], "domain": "po", "source": "scan"},
    ]
    (idx / "symbols.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    hits = ps.similar(idx, "create_po", "function", ["po", "create"], "(board, payload)")
    names = [h["row"]["name"] for h in hits]
    assert names[0] == "po_create"
    assert names[-1] == "kpi_row"
    scores = [h["score"] for h in hits]
    assert scores == sorted(scores, reverse=True)
    assert all(h["why"] for h in hits)
    # the same-kind + domain bonus is applied: name stems and keyword jaccard
    # both plateau at 2/3 ("create po" vs "po create" match one block), so
    # score = 2/3 + 0.1 bonus = 0.767
    assert hits[0]["score"] == 0.767 and "same kind/domain" in hits[0]["why"]


# ── import_index over the real (trimmed) `_index/` ────────────────────────────
def test_import_index_real_fixture(tmp_path):
    idx = tmp_path / "idx"
    counts = ps.import_index(idx, FX / "_index")
    assert counts == {"added": 21, "updated": 0}
    assert len(ps.lookup(idx, "dashboard_summary", "endpoint")) == 1
    seam = ps.lookup(idx, "dashboard_summary", "endpoint")[0]
    assert seam["source"].startswith("_index/seams.json#") and seam["path"] == "_index/seams.json"
    ep = ps.lookup(idx, "GET /health", "endpoint")[0]
    assert ep["source"].startswith("_index/endpoints.json#engine_wire_surface/")
    assert ps.lookup(idx, "kpi", "token")[0]["signature"].startswith("--sz-kpi")
    assert ps.lookup(idx, "alpha-app", "component")[0]["source"].startswith(
        "_index/atlas.json#atlas_nodes/cards/")
    assert ps.lookup(idx, "in", "token")[0]["signature"] == "input"
    for row in ps.lookup(idx, "dashboard_summary") + ps.lookup(idx, "kpi"):
        assert row["source"].startswith("_index/")
    # idempotent: a second import only updates
    assert ps.import_index(idx, FX / "_index") == {"added": 0, "updated": 21}


# ── CLI ───────────────────────────────────────────────────────────────────────
def test_main_cli(tmp_path, capsys):
    idx = tmp_path / "idx"
    rc = ps.main(["scan", str(FX / "buku_stok.js"), "--index", str(idx), "--root", str(FX)])
    assert rc == 0 and (idx / "symbols.jsonl").exists()
    diff_file = tmp_path / "d.diff"
    diff_file.write_text("+ renderX(ok)\n+ // renderX(no)\n", encoding="utf-8")
    assert ps.main(["usages", str(diff_file)]) == 0
    assert '"renderX"' in capsys.readouterr().out
    assert ps.main(["lookup", "text", "--index", str(idx)]) == 0
    assert '"name": "text"' in capsys.readouterr().out
    assert ps.main(["lookup", "text", "--kind", "class", "--index", str(idx)]) == 0
    assert "[]" in capsys.readouterr().out


def test_usages_multiline_block_comment_does_not_leak():
    # gate counter-test C2 (2026-08-27): strip-state must carry across added lines
    diff = "+/* start\n+ blockCall(x)\n+ end */\n+afterCall(y)\n"
    from echelon_engine.propose_scan import usages
    got = usages(diff)
    assert "afterCall" in got and "blockCall" not in got


def test_scan_file_on_directory_returns_empty(tmp_path):
    """A reference path naming a DIRECTORY (or a vanished file) must scan to
    nothing, not crash the whole check via read_text (hit live 2026-08-28:
    a proposal reference pointed at web/static/lib and check() died in the
    _step_refs self-heal rescan)."""
    import echelon_engine.propose_scan as ps
    d = tmp_path / "somedir"
    d.mkdir()
    assert ps.scan_file(d, root=tmp_path) == []
    assert ps.scan_file(tmp_path / "gone.py", root=tmp_path) == []
