"""Tests for echelon_engine/ops.py — the devops verbs (charter Part 1).

tmp_path rooms only; `_run` is monkeypatched so nothing ever spawns ssh/chrome.
"""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from echelon_engine import ops
from echelon_engine import workcycle as wc


@pytest.fixture
def room(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    return wc.init(root, estate="acme", type_="prod-service", scope="acme")


@pytest.fixture
def prelim(room):
    return ops.add_estate(room, {"name": "prelim", "host": "box2", "unit": "prelim.service",
                                 "env_file": "/etc/prelim/prelim.env", "cwd": "/srv/prelim/current",
                                 "health": "http://127.0.0.1:3000/health",
                                 "releases": "/srv/prelim/releases", "current": "/srv/prelim/current"})


def _journal(room, kind="ops"):
    rows = []
    for f in sorted((room / "journal").glob("*.jsonl")):
        rows += [json.loads(l) for l in f.read_text(encoding="utf-8").splitlines() if l.strip()]
    return [r for r in rows if r["kind"] == kind]


# ── estate rows ──────────────────────────────────────────────────────────────

def test_estate_add_lands_in_room_json_and_journals(room, prelim):
    assert ops.estates(room)["prelim"]["host"] == "box2"
    assert _journal(room)[-1]["verb"] == "estate-add"


def test_estate_add_derives_from_the_live_unit(room, monkeypatch):
    unit = "[Service]\nEnvironmentFile=-/etc/x/x.env\nWorkingDirectory=/srv/x/current\n"
    monkeypatch.setattr(ops, "_run", lambda cmd, **k: (0, unit, ""))
    row = ops.add_estate(room, {"name": "x", "host": "h", "unit": "x.service"}, derive=True)
    assert row["env_file"] == "/etc/x/x.env" and row["cwd"] == "/srv/x/current"


def test_unknown_estate_is_refused(room):
    with pytest.raises(SystemExit, match="not in room.json"):
        ops.estate(room, "ghost")


# ── ops probe ────────────────────────────────────────────────────────────────

def test_probe_script_boots_env_then_cwd_then_broker_before_knex(prelim):
    s = ops.probe_script(prelim, "select count(*) from users")
    assert s.index(". /etc/prelim/prelim.env") < s.index("cd /srv/prelim/current") < s.index("node --input-type=module")
    assert s.index("loadBroker()") < s.index("framework/knex.js")
    assert s.index("current_database()") < s.index("select count(*) from users")


def test_probe_refuses_a_write_without_the_flag(prelim):
    with pytest.raises(SystemExit, match="without --write"):
        ops.probe_script(prelim, "UPDATE users set x=1")
    assert "UPDATE" in ops.probe_script(prelim, "UPDATE users set x=1", write=True)


def test_probe_validate_refuses_output_without_the_header():
    ok, why = ops.probe_validate('[{"count": 42}]')
    assert not ok and "REFUSED to print" in why
    ok, why = ops.probe_validate("PROBE driver=pg db=molai\n[{\"count\": 42}]")
    assert ok and why == "driver=pg db=molai"


def test_probe_run_refuses_headerless_output_exit_3_and_journals(room, prelim, monkeypatch, capsys):
    monkeypatch.setattr(ops, "_run", lambda cmd, **k: (0, '[{"count":42}]', "no pg_hba.conf entry"))
    assert ops.probe(room, "prelim", "select 1") == 3
    assert "42" not in capsys.readouterr().out
    rec = _journal(room)[-1]
    assert rec["verb"] == "probe" and rec["ok"] is False


def test_probe_run_prints_header_then_rows(room, prelim, monkeypatch, capsys):
    monkeypatch.setattr(ops, "_run", lambda cmd, **k: (0, "PROBE driver=pg db=molai\n[{\"count\":42}]", ""))
    assert ops.probe(room, "prelim", "select 1") == 0
    out = capsys.readouterr().out
    assert out.startswith("PROBE driver=pg db=molai") and "42" in out
    assert _journal(room)[-1]["header"] == "driver=pg db=molai"


def test_probe_write_needs_a_reason(room, prelim):
    assert ops.probe(room, "prelim", "select 1", write=True) == 3


# ── ops shot ─────────────────────────────────────────────────────────────────

def test_suspect_rules():
    want = (390, 844)
    assert "wanted" in ops.suspect("x" * 50, True, 400, 844, want)
    assert "blank" in ops.suspect("", True, 390, 844, want)
    assert "error page" in ops.suspect("Cannot GET /dash " * 3, True, 390, 844, want)
    assert "unstyled" in ops.suspect("a perfectly normal page body text", False, 390, 844, want)
    assert ops.suspect("a perfectly normal page body text", True, 390, 844, want) is None


class _Fake:
    def __init__(self, answers):
        self.answers, self.calls = list(answers), []

    def send(self, messages, **k):
        self.calls.append(messages)
        a = self.answers.pop(0)
        if a is None:
            return SimpleNamespace(status="error", content="HTTP 429")
        return SimpleNamespace(status="ok", content=a)


def test_judge_canary_failure_is_abstain_never_pass():
    v, why = ops.judge(b"png", "no clipped buttons", provider=_Fake(["blue", "PASS\nfine"]))
    assert v == "ABSTAIN" and "canary" in why
    v, why = ops.judge(b"png", "b", provider=_Fake([None, "PASS"]))   # throttled canary
    assert v == "ABSTAIN"


def test_judge_verdicts_after_a_good_canary():
    assert ops.judge(b"png", "b", provider=_Fake(["Red.", "PASS\nclean"]))[0] == "PASS"
    assert ops.judge(b"png", "b", provider=_Fake(["red", "FAIL\nbutton clipped"]))[0] == "FAIL"
    assert ops.judge(b"png", "b", provider=_Fake(["red", "It looks okay I guess"]))[0] == "ABSTAIN"


def test_canary_png_is_a_real_64x64_png():
    png = ops._canary_png()
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    import struct
    assert struct.unpack(">II", png[16:24]) == (64, 64)


def test_shot_suspect_is_never_judged_and_journals(room, tmp_path, monkeypatch):
    out = tmp_path / "s.png"
    fake = _Fake(["red", "PASS"])
    def _render(url, o, **k):
        Path(o).write_bytes(ops._canary_png())
        return {"text": "", "styled": True, "w": 64, "h": 64}
    monkeypatch.setattr(ops, "render", _render)
    assert ops.shot(room, "http://x/", out, width=64, height=64, brief="anything", provider=fake) == 2
    assert fake.calls == []
    rec = _journal(room)[-1]
    assert rec["verb"] == "shot" and rec["verdict"] == "SUSPECT" and rec["judged"] is False


def test_shot_pass_and_fail_exit_codes(room, tmp_path, monkeypatch):
    out = tmp_path / "s.png"
    def _render(url, o, **k):
        Path(o).write_bytes(ops._canary_png())
        return {"text": "a real page with enough text on it", "styled": True, "w": 64, "h": 64}
    monkeypatch.setattr(ops, "render", _render)
    assert ops.shot(room, "http://x/", out, width=64, height=64) == 0
    assert ops.shot(room, "http://x/", out, width=64, height=64, brief="b", provider=_Fake(["red", "PASS"])) == 0
    assert ops.shot(room, "http://x/", out, width=64, height=64, brief="b", provider=_Fake(["red", "FAIL\nx"])) == 1
    assert ops.shot(room, "http://x/", out, width=64, height=64, brief="b", provider=_Fake(["green", "PASS"])) == 2


def test_cookie_parse():
    assert ops._cookie("sid=abc@dash.local") == {"name": "sid", "value": "abc", "domain": "dash.local", "path": "/"}


def test_cli_is_registered():
    from echelon_engine import __main__ as m
    src = Path(m.__file__).read_text(encoding="utf-8")
    assert '"ops": lambda av: __import__("echelon_engine.ops"' in src


# ── ops deploy (step 3) ──────────────────────────────────────────────────────

def test_prune_is_by_dirname_never_mtime_and_excludes_live():
    c = ops.prune_cmd("/srv/p/releases", "/srv/p/current")
    assert "ls -t" not in c and "ls | sort | head -n -3" in c
    assert 'readlink -f /srv/p/current' in c and 'grep -vx "$live"' in c
    assert c.index("readlink") < c.index("rm -rf") and "df -h" in c


def test_deploy_plan_order_is_the_gate(prelim):
    plan = ops.deploy_plan(prelim, sha="abcdef1234", changed=["services/x.js"], dist_built=False,
                           tarball="/tmp/t.tar.gz")
    labels = [l for l, _ in plan]
    assert labels == ["local:check", "local:scp", "remote:extract", "remote:verify-extracted",
                      "remote:flip", "remote:health", "remote:readlink", "remote:prune"]
    assert "node --check services/x.js" in dict(plan)["local:check"]
    assert "ln -sfn /srv/prelim/releases/" in dict(plan)["remote:flip"]
    assert "http_code" in dict(plan)["remote:health"] and "sleep 5" in dict(plan)["remote:health"]


def test_deploy_plan_refuses_ui_change_without_fresh_dist(prelim):
    e = dict(prelim, ui_dirs=["ui/"])
    with pytest.raises(SystemExit, match="deploy-dist trap"):
        ops.deploy_plan(e, sha="a" * 10, changed=["ui/app.js"], dist_built=False, tarball="t")
    assert ops.deploy_plan(e, sha="a" * 10, changed=["ui/app.js"], dist_built=True, tarball="t")


def test_release_name_is_a_dirname_timestamp_plus_sha():
    from datetime import datetime, timezone
    n = ops.release_name("0123456789ab", datetime(2026, 8, 30, 12, 0, 0, tzinfo=timezone.utc))
    assert n == "20260830T120000Z-01234567"


def test_deploy_refuses_library_rooms_and_incomplete_estates(tmp_path, capsys):
    root = tmp_path / "lib"; root.mkdir()
    room = wc.init(root, estate="lib", type_="library", scope="lib")
    ops.add_estate(room, {"name": "l", "host": "h", "unit": "u", "releases": "r", "current": "c", "health": "x"})
    assert ops.deploy(room, "l", dry_run=True) == 3
    assert "library" in capsys.readouterr().out
    ops.add_estate(room, {"name": "half", "host": "h"})
    assert ops.deploy(room, "half", dry_run=True) == 3


def test_deploy_dry_run_prints_plan_and_rollback_without_touching_the_box(room, prelim, monkeypatch, capsys):
    calls = []
    def _fake(cmd, **k):
        calls.append(cmd)
        if cmd[:3] == ["git", "-C", str(Path.cwd())] and "rev-parse" in cmd:
            return 0, "feedface0000\n", ""
        return 0, "", ""
    monkeypatch.setattr(ops, "_run", _fake)
    assert ops.deploy(room, "prelim", dry_run=True) == 0
    out = capsys.readouterr().out
    assert "PLAN prelim @ feedface" in out and "remote:prune" in out
    assert not any(c[0] == "ssh" for c in calls)
    assert _journal(room, "ops")[-1]["verb"] != "deploy"      # dry-run leaves no deploy receipt


def test_deploy_run_stops_at_the_failing_gate_and_prints_rollback(room, prelim, monkeypatch, capsys):
    seen = []
    def _fake(cmd, **k):
        seen.append(cmd)
        if "rev-parse" in cmd:
            return 0, "feedface0000\n", ""
        if cmd[0] == "ssh" and "readlink -f /srv/prelim/current" == cmd[-1]:
            return 0, "/srv/prelim/releases/20260829T000000Z-00000000\n", ""
        if cmd[0] == "ssh" and "http_code" in cmd[-1]:
            return 1, "HEALTH 502", ""
        return 0, "", ""
    monkeypatch.setattr(ops, "_run", _fake)
    assert ops.deploy(room, "prelim") == 1
    out = capsys.readouterr().out
    assert "FAIL at remote:health" in out and "ROLLBACK: ssh box2 'ln -sfn /srv/prelim/releases/20260829T000000Z-00000000" in out
    assert not any(c[0] == "ssh" and "rm -rf" in c[-1] for c in seen)   # prune never ran
    rec = _journal(room)[-1]
    assert rec["verb"] == "deploy" and rec["result"].startswith("FAIL at remote:health") and rec["rollback"]
    assert "deployed_sha" not in ops.estates(room)["prelim"]


def test_deploy_run_success_records_deployed_sha(room, prelim, monkeypatch):
    def _fake(cmd, **k):
        if "rev-parse" in cmd:
            return 0, "feedface0000\n", ""
        if cmd[0] == "ssh" and "readlink -f /srv/prelim/current" == cmd[-1]:
            return 0, "/srv/prelim/releases/old\n", ""
        return 0, "HEALTH 200", ""
    monkeypatch.setattr(ops, "_run", _fake)
    assert ops.deploy(room, "prelim") == 0
    assert ops.estates(room)["prelim"]["deployed_sha"] == "feedface0000"
    assert _journal(room)[-1]["verb"] == "estate-add"       # the sha write is itself receipted
