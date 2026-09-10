"""Tests for echelon_engine/slim.py — the budget lint (charter Part 3)."""
import subprocess
from pathlib import Path

import pytest

from echelon_engine import slim
from echelon_engine import workcycle as wc


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo"; r.mkdir()
    subprocess.run(["git", "init", "-q", str(r)], check=True)
    subprocess.run(["git", "-C", str(r), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(r), "config", "user.name", "t"], check=True)
    (r / "a.py").write_text("x = 1\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(r), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(r), "commit", "-q", "-m", "init"], check=True)
    return r


@pytest.fixture
def room(repo):
    room = wc.init(repo, estate="acme", type_="prod-service", scope="acme")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)       # init writes .gitignore
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "room"], check=True)
    return room


DIFF = """diff --git a/a.py b/a.py
--- a/a.py
+++ b/a.py
@@ -1 +1,4 @@
 x = 1
+# a comment does not count
+y = 2
+
+z = 3
diff --git a/requirements.txt b/requirements.txt
--- /dev/null
+++ b/requirements.txt
@@ -0,0 +1,2 @@
+requests>=2.0
+rich
"""


def test_measure_counts_files_source_loc_and_new_deps():
    m = slim.measure(DIFF)
    assert m["files"] == 2 and m["loc"] == 4 and m["deps"] == 2
    assert m["dep_list"] == ["requests", "rich"]


def test_budget_resolution_ladder(room):
    assert slim.resolve_budget(room, "files=1,loc=10,deps=0") == ({"files": 1, "loc": 10, "deps": 0}, "explicit")
    assert slim.resolve_budget(room, None) == ({"files": 2, "loc": 250, "deps": 0}, "type:prod-service")
    wc._write_json(room / "changes" / "active" / "CHG-001.json", {"id": "CHG-001", "budget": {"files": 5, "loc": 50}})
    assert slim.resolve_budget(room, None) == ({"files": 5, "loc": 50, "deps": 0}, "CHG-001")
    with pytest.raises(SystemExit, match="bad --budget"):
        slim.parse_budget("loc=lots")


def test_lint_refuses_the_overshoot_and_journals_the_ledger(repo, room, capsys):
    (repo / "a.py").write_text("x = 1\n" + "".join(f"v{i} = {i}\n" for i in range(12)), encoding="utf-8")
    (repo / "b.py").write_text("q = 1\n", encoding="utf-8")
    assert slim.lint(repo, room, budget="files=1,loc=10,deps=0", ledger=True) == 1
    out = capsys.readouterr().out
    assert "SLIM OVER" in out and "files 2 > 1" in out and "loc 13 > 10" in out
    rows = [l for f in (room / "journal").glob("*.jsonl") for l in f.read_text(encoding="utf-8").splitlines()]
    assert any('"kind": "ledger"' in l and '"over": ["files 2 > 1", "loc 13 > 10"]' in l for l in rows)
    assert slim.lint(repo, room, budget="files=2,loc=20,deps=0") == 0
    assert "SLIM ok" in capsys.readouterr().out


def test_lint_staged_reads_the_index_not_the_tree(repo, room, capsys):
    (repo / "a.py").write_text("x = 1\ny = 2\n", encoding="utf-8")
    assert slim.lint(repo, room, staged=True, budget="files=0,loc=0,deps=0") == 0   # nothing staged
    subprocess.run(["git", "-C", str(repo), "add", "a.py"], check=True)
    assert slim.lint(repo, room, staged=True, budget="files=0,loc=0,deps=0") == 1


def test_hook_install_and_refuse_foreign_hook(repo):
    p = slim.install_hook(repo)
    assert p.name == "pre-commit" and "slim lint --staged --ledger" in p.read_text(encoding="utf-8")
    slim.install_hook(repo)                                    # idempotent on our own hook
    p.write_text("#!/bin/sh\necho theirs\n", encoding="utf-8")
    with pytest.raises(SystemExit, match="not ours"):
        slim.install_hook(repo)


def test_propose_stamps_the_budget_into_the_chg():
    src = Path(__file__).resolve().parents[1] / "echelon_engine" / "propose.py"
    assert '"budget": proposal.get("budget")' in src.read_text(encoding="utf-8")


def test_cli_registered_and_bad_budget_exits_3(repo, capsys):
    from echelon_engine import __main__ as m
    assert '"slim": lambda av: __import__("echelon_engine.slim"' in Path(m.__file__).read_text(encoding="utf-8")
    assert slim.main(["lint", "--repo", str(repo), "--budget", "loc=lots"]) == 3
