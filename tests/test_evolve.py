"""Tests for echelon_engine/atoms/evolve.py -- OPEN-0112 the first machine turn.

All fixtures are synthetic tmp_path files; no network, no real ~/.echelon.
"""
from __future__ import annotations

import importlib.util
import json
import time
from pathlib import Path

import pytest

from echelon_engine.atoms import evolve


NOW = int(time.time())
DAY = 86400


def _fire(ts, reflex, action="warn", tool="Bash", payload="abc"):
    return json.dumps({"ts": ts, "reflex": reflex, "tier": "scope", "action": action,
                        "tool": tool, "payload": payload})


def _write_fires(path: Path):
    lines = []
    # reflex A: 30 fires in-window, 2 distinct payload shapes -> low entropy, HOT #1
    for i in range(30):
        shape = "shape-one some text here" if i % 2 == 0 else "shape-two other text xx"
        lines.append(_fire(NOW - i * 60, "echelon:reflex-a", action="warn", payload=shape))
    # reflex B: 5 fires in-window, 5 distinct shapes -> high entropy
    for i in range(5):
        lines.append(_fire(NOW - i * 60, "echelon:reflex-b", action="warn", payload=f"unique shape {i} abcdef"))
    # reflex C: fired once, 40 days ago -> DORMANT, not in window
    lines.append(_fire(NOW - 40 * DAY, "echelon:reflex-c", action="block", payload="old"))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _rule(name, scope="echelon", event="PreToolUse", tool="Bash", action="warn"):
    return {"name": name, "scope": scope, "tier": "scope", "event": event,
            "tool": tool, "match": ".*", "action": action, "guard": "g"}


def _write_rules(path: Path):
    rules = [
        _rule("reflex-a"),
        _rule("reflex-b"),
        _rule("reflex-c", action="block"),
        _rule("reflex-never"),  # never fired
        _rule("not-pretooluse", event="PostToolUse"),
    ]
    path.write_text(json.dumps({"version": 1, "rules": rules}), encoding="utf-8")


@pytest.fixture
def fires_path(tmp_path):
    p = tmp_path / "reflex_fires.jsonl"
    _write_fires(p)
    return p


@pytest.fixture
def rules_path(tmp_path):
    p = tmp_path / "reflexes.json"
    _write_rules(p)
    return p


def test_hot_ranks_low_entropy_first(fires_path, rules_path):
    fires = evolve._read_fires(fires_path)
    hot = evolve._compute_hot(fires, NOW - 30 * DAY, hot_threshold=5)
    assert hot[0]["reflex"] == "echelon:reflex-a"
    assert abs(hot[0]["entropy_proxy"] - 2 / 30) < 1e-3
    assert "L0" in hot[0]["proposal"]


def test_hot_threshold_filters(fires_path):
    fires = evolve._read_fires(fires_path)
    hot10 = evolve._compute_hot(fires, NOW - 30 * DAY, hot_threshold=10)
    assert "echelon:reflex-b" not in [r["reflex"] for r in hot10]
    hot5 = evolve._compute_hot(fires, NOW - 30 * DAY, hot_threshold=5)
    assert "echelon:reflex-b" in [r["reflex"] for r in hot5]


def test_cold_lifecycle_and_never_fired(fires_path, rules_path):
    fires = evolve._read_fires(fires_path)
    rules = evolve._read_rules(rules_path)
    assert len(rules) == 4  # non-PreToolUse rule excluded
    cold, counts = evolve._compute_cold(rules, fires, NOW, cool_days=14, dormant_days=30)
    by_name = {r["reflex"]: r for r in cold}
    assert by_name["echelon:reflex-a"]["lifecycle"] == "ACTIVE"
    assert by_name["echelon:reflex-c"]["lifecycle"] == "DORMANT"
    assert by_name["echelon:reflex-c"]["never_fired"] is False
    assert by_name["echelon:reflex-never"]["lifecycle"] == "DORMANT"
    assert by_name["echelon:reflex-never"]["never_fired"] is True
    assert counts["ACTIVE"] >= 2
    assert counts["DORMANT"] >= 2


def test_recurrence_across_rooms(tmp_path):
    room_a = tmp_path / "room_a"
    room_b = tmp_path / "room_b"
    for room in (room_a, room_b):
        (room / ".echelon").mkdir(parents=True)
    text = "the reflex fired repeatedly on this trap again"
    receipts_a = [json.dumps({"v": 1, "ts": NOW, "kind": "note", "text": text, "origin": "x", "ref": "", "project": "p"})]
    receipts_b = [json.dumps({"v": 1, "ts": NOW, "kind": "note", "text": text, "origin": "x", "ref": "", "project": "p"})]
    receipts_a.append(json.dumps({"v": 1, "ts": NOW, "kind": "note", "text": text, "origin": "x", "ref": "", "project": "p"}))
    (room_a / ".echelon" / "receipts.jsonl").write_text("\n".join(receipts_a) + "\n", encoding="utf-8")
    (room_b / ".echelon" / "receipts.jsonl").write_text("\n".join(receipts_b) + "\n", encoding="utf-8")

    def room_dir_fn(name):
        # mirror workcycle.room_dir(): it returns the STATE dir (<repo>/.echelon), not the repo
        return (room_a if name == "room_a" else room_b) / ".echelon"

    rows, skipped = evolve._compute_recurrence(["room_a", "room_b"], room_dir_fn, NOW - 30 * DAY)
    assert skipped == 0
    assert rows
    top = rows[0]
    assert top["count"] == 3
    assert set(top["rooms"]) == {"room_a", "room_b"}


def test_recurrence_skips_missing_receipts(tmp_path):
    room_a = tmp_path / "room_a"
    (room_a / ".echelon").mkdir(parents=True)

    def room_dir_fn(name):
        return room_a / ".echelon"

    rows, skipped = evolve._compute_recurrence(["room_a"], room_dir_fn, NOW - 30 * DAY)
    assert rows == []
    assert skipped == 1


def test_json_output_has_expected_keys(tmp_path, fires_path, rules_path, monkeypatch):
    import echelon_engine.workcycle as wc
    monkeypatch.setattr(wc, "registered_rooms", lambda: [])
    rc = evolve._main(["scan", "--fires", str(fires_path), "--rules", str(rules_path),
                        "--json", "--hot", "5"])
    assert rc == 0


def test_cli_json_stdout_parses(tmp_path, fires_path, rules_path, capsys, monkeypatch):
    import echelon_engine.workcycle as wc
    monkeypatch.setattr(wc, "registered_rooms", lambda: [])
    rc = evolve._main(["scan", "--fires", str(fires_path), "--rules", str(rules_path),
                        "--json", "--hot", "5"])
    assert rc == 0
    out = capsys.readouterr().out
    doc = json.loads(out)
    assert set(("hot", "cold", "recurrence", "summary")) <= set(doc.keys())


def test_write_health_preserves_demoted(tmp_path, fires_path, rules_path):
    health_path = tmp_path / "reflex_health.json"
    health_path.write_text(json.dumps({
        "version": 1, "as_of": "x", "window_days": 30,
        "reflexes": {"echelon:reflex-a": {"demoted": True}},
    }), encoding="utf-8")

    rules = evolve._read_rules(rules_path)
    fires = evolve._read_fires(fires_path)
    cold, _ = evolve._compute_cold(rules, fires, NOW, cool_days=14, dormant_days=30)
    doc = evolve._write_health(health_path, cold, 30)
    assert doc["reflexes"]["echelon:reflex-a"]["demoted"] is True
    assert doc["reflexes"]["echelon:reflex-never"]["demoted"] is False


def test_demote_and_undo(tmp_path, rules_path):
    health_path = tmp_path / "reflex_health.json"
    rc = evolve._main(["demote", "echelon:reflex-a", "--rules", str(rules_path), "--health", str(health_path)])
    assert rc == 0
    doc = json.loads(health_path.read_text(encoding="utf-8"))
    assert doc["reflexes"]["echelon:reflex-a"]["demoted"] is True

    rc = evolve._main(["demote", "echelon:reflex-a", "--undo", "--rules", str(rules_path), "--health", str(health_path)])
    assert rc == 0
    doc = json.loads(health_path.read_text(encoding="utf-8"))
    assert doc["reflexes"]["echelon:reflex-a"]["demoted"] is False


def test_demote_unknown_name_refuses(tmp_path, rules_path):
    health_path = tmp_path / "reflex_health.json"
    rc = evolve._main(["demote", "echelon:not-a-rule", "--rules", str(rules_path), "--health", str(health_path)])
    assert rc == 1
    assert not health_path.exists()


def test_health_json(tmp_path):
    health_path = tmp_path / "reflex_health.json"
    health_path.write_text(json.dumps({
        "version": 1, "as_of": "x", "window_days": 30,
        "reflexes": {"echelon:reflex-a": {"lifecycle": "ACTIVE", "demoted": False}},
    }), encoding="utf-8")
    rc = evolve._main(["health", "--health", str(health_path)])
    assert rc == 0


HOOK_PATH = Path(__file__).resolve().parents[1] / "echelon_engine" / "hooks_staged" / "echelon_reflex.py"


def _load_hook_module():
    spec = importlib.util.spec_from_file_location("echelon_reflex_staged_test", HOOK_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_hook_drops_demoted_rules(tmp_path):
    mod = _load_hook_module()
    health_path = tmp_path / "reflex_health.json"
    health_path.write_text(json.dumps({
        "reflexes": {"echelon:reflex-a": {"demoted": True}},
    }), encoding="utf-8")
    rules = [_rule("reflex-a"), _rule("reflex-b")]
    kept = mod._drop_demoted(rules, str(health_path))
    assert [r["name"] for r in kept] == ["reflex-b"]


def test_hook_corrupt_health_keeps_all_rules(tmp_path):
    mod = _load_hook_module()
    health_path = tmp_path / "reflex_health.json"
    health_path.write_text("{not json", encoding="utf-8")
    rules = [_rule("reflex-a"), _rule("reflex-b")]
    kept = mod._drop_demoted(rules, str(health_path))
    assert kept == rules


def test_hook_missing_health_keeps_all_rules(tmp_path):
    mod = _load_hook_module()
    rules = [_rule("reflex-a")]
    kept = mod._drop_demoted(rules, str(tmp_path / "nope.json"))
    assert kept == rules


def test_recurrence_reads_a_real_workcycle_room_and_honours_iso_window(tmp_path):
    """Regression for the live-gate finding (2026-09-06): the first build appended `.echelon` to
    workcycle.room_dir() (which already returns the state dir) so EVERY room was skipped and the
    live scan reported recurrence=0; and receipt `ts` is an ISO string, so the window never applied."""
    from echelon_engine import workcycle as wc
    root = tmp_path / "repo"
    root.mkdir()
    state = wc.init(root, estate="x", scope="s")
    text = "same trap named again and again in this room today"
    for _ in range(5):
        wc.receipt(state, "route", text, origin="t", fold_now=False)
    with open(state / "receipts.jsonl", "a", encoding="utf-8") as f:
        for _ in range(5):
            f.write(json.dumps({"v": 1, "ts": "2020-01-01T00:00:00+00:00", "kind": "route",
                                "text": "ancient trap that must fall outside the window ok", "origin": "t"}) + "\n")
    rows, skipped = evolve._compute_recurrence(["r"], lambda name: wc.room_dir(state), NOW - 30 * DAY)
    assert skipped == 0
    keys = [r["key"] for r in rows]
    assert any(k.startswith("same trap named again") for k in keys)
    assert not any(k.startswith("ancient trap") for k in keys)
