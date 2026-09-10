"""Tests for the ECHELON SOCIETY layer (echelon_sdk.society + echelon_engine.society).

Covers the spec §7 gate (all $0 — no model calls, no live `claude` harness):
  - bus round-trip (post -> tail -> stats)
  - check-mail is NON-destructive (peek advances a SEPARATE cursor, never the drain cursor;
    history still returns everything)
  - watcher FRESH-START skips stale control messages (the banked stale-STAND-DOWN trap)
  - a role resolves its cartridge + protocol; roles add round-trips
  - protocol seeds compose into the machine-generated brief (with the absolute post cmd + equip)
  - a 2-role convene SMOKE driven purely over the bus (post -> peer reacts -> posts back),
    simulating the watcher's drain/feed loop without spawning a real harness.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from echelon_sdk.bus import EchelonBus
from echelon_sdk import society as soc
from echelon_engine import society as society_verb


@pytest.fixture
def bus_db(tmp_path: Path) -> Path:
    return tmp_path / "bus.db"


@pytest.fixture
def isolated_roles(tmp_path: Path, monkeypatch) -> Path:
    """Point the role registry JSON at a temp file so tests never touch the real ~/.echelon."""
    home = tmp_path / "society"
    monkeypatch.setattr(soc, "SOCIETY_HOME", home)
    monkeypatch.setattr(soc, "ROLES_JSON", home / "roles.json")
    return home / "roles.json"


# ── bus round-trip ──────────────────────────────────────────────────────────


def test_bus_round_trip(bus_db):
    bus = EchelonBus(bus_db, mirror_jsonl=soc.jsonl_mirror_for(bus_db))
    s1 = bus.post("ch", "alice", "hello")
    s2 = bus.post("ch", "bob", "world")
    assert s2 == s1 + 1
    hist = bus.history("ch")
    assert [m.body for m in hist] == ["hello", "world"]
    assert bus.channels() == ["ch"]
    assert bus.stats()["per_channel"]["ch"] == 2
    # jsonl mirror written next to the db
    assert soc.jsonl_mirror_for(bus_db).exists()


# ── check-mail: non-destructive (FORK-2) ────────────────────────────────────


def test_check_mail_is_non_destructive(bus_db):
    bus = EchelonBus(bus_db)
    bus.post("ch", "alice", "m1")
    bus.post("ch", "bob", "m2")

    # first peek sees both, advances the PEEK cursor
    first = soc.check_mail(bus_db, "human", "ch")
    assert [m["body"] for m in first] == ["m1", "m2"]
    # second peek sees nothing new (peek cursor advanced)
    second = soc.check_mail(bus_db, "human", "ch")
    assert second == []
    # but the messages were NOT consumed: history still has both, and a fresh reader's
    # DRAIN cursor is untouched by the peek
    assert len(bus.history("ch")) == 2
    drained = bus.drain("watcher-role", ["ch"])
    assert [m.body for m in drained] == ["m1", "m2"]


def test_check_mail_no_advance_is_pure_read(bus_db):
    bus = EchelonBus(bus_db)
    bus.post("ch", "alice", "m1")
    a = soc.check_mail(bus_db, "human", "ch", advance=False)
    b = soc.check_mail(bus_db, "human", "ch", advance=False)
    assert [m["body"] for m in a] == ["m1"]
    assert [m["body"] for m in b] == ["m1"]  # no advance -> repeatable


def test_check_mail_peek_cursor_independent_of_drain(bus_db):
    """A human peek must never advance the watcher's drain cursor (separate tables)."""
    bus = EchelonBus(bus_db)
    bus.post("ch", "alice", "m1")
    soc.check_mail(bus_db, "builder", "ch")     # human peeks as 'builder'
    # the watcher draining for the SAME reader name still gets the message
    drained = bus.drain("builder", ["ch"])
    assert [m.body for m in drained] == ["m1"]


# ── watcher fresh-start skips stale (banked trap 2) ─────────────────────────


def test_watcher_fresh_start_skips_stale(bus_db):
    """On launch, a drain() advances the new peer's cursor to the channel tip, so a stale
    STAND DOWN posted before the peer existed is never consumed."""
    bus = EchelonBus(bus_db)
    bus.post("ch", "architect", "STAND DOWN")   # stale control msg from a prior society
    # fresh-start drain (what _run_watcher does first)
    stale = bus.drain("builder", ["ch"])
    assert [m.body for m in stale] == ["STAND DOWN"]
    # now a NEW real message arrives; the peer sees ONLY it, not the stale stand-down
    bus.post("ch", "architect", "build slice 1")
    fresh = bus.drain("builder", ["ch"])
    assert [m.body for m in fresh] == ["build slice 1"]


# ── role registry (FORK-1) ──────────────────────────────────────────────────


def test_seed_roles_resolve_cartridge_and_protocol(isolated_roles):
    builder = soc.resolve_role("builder")
    assert builder == {"cartridge": ["frontend-build"], "protocol": "persistent-gated-build"}
    architect = soc.resolve_role("architect")
    assert architect["cartridge"] == ["act-ready", "ux"]
    assert architect["protocol"] == "orchestrate-gate"
    assert soc.resolve_role("nonesuch") is None


def test_roles_seeded_to_json_on_first_read(isolated_roles):
    assert not isolated_roles.exists()
    soc.list_roles()
    assert isolated_roles.exists()
    data = json.loads(isolated_roles.read_text(encoding="utf-8"))
    assert set(data) == {"architect", "builder", "auditor", "scribe"}


def test_add_role_round_trips(isolated_roles):
    spec = soc.add_role("redteam", "intent, ux", "skeptic-verify", mirror_atom=False)
    assert spec == {"cartridge": ["intent", "ux"], "protocol": "skeptic-verify"}
    assert soc.resolve_role("redteam")["cartridge"] == ["intent", "ux"]
    # persisted to JSON
    data = json.loads(isolated_roles.read_text(encoding="utf-8"))
    assert data["redteam"]["protocol"] == "skeptic-verify"


# ── protocol seeds + brief composer ─────────────────────────────────────────


def test_protocol_seed_known_and_fallback():
    assert "BUILDER" in soc.protocol_seed("persistent-gated-build")
    assert "AUDITOR" in soc.protocol_seed("skeptic-verify")
    # unknown protocol degrades, never empty
    assert soc.protocol_seed("made-up").strip()


def test_compose_brief_injects_equip_and_absolute_post(isolated_roles):
    brief = soc.compose_brief(
        "builder", "echelon.build",
        busctl_cmd="/abs/python -m echelon_engine bus --session s post echelon.build builder",
        goal="build the thing")
    assert "build the thing" in brief
    assert "BUILDER" in brief                                  # protocol seed composed
    assert "cartridge equip frontend-build" in brief           # equip cmd injected (witnessed)
    assert "/abs/python -m echelon_engine bus" in brief        # absolute post cmd (relative-path trap)
    assert "context PERSISTS" in brief                          # persistence reminder
    assert "STAND DOWN" in brief


# ── verb-level wiring ───────────────────────────────────────────────────────


def test_bus_verb_post_and_check_mail(tmp_path, capsys):
    db = tmp_path / "v.db"
    assert society_verb.bus_main(["--bus", str(db), "post", "ch", "alice", "hi", "there"]) == 0
    out = capsys.readouterr().out
    assert "posted seq=1" in out
    assert society_verb.bus_main(["--bus", str(db), "check-mail", "human", "ch"]) == 0
    assert "hi there" in capsys.readouterr().out


def test_watcher_dry_run_emits_brief(tmp_path, isolated_roles, capsys):
    db = tmp_path / "w.db"
    rc = society_verb.watcher_main([
        "--role", "auditor", "--channel", "echelon.build",
        "--bus", str(db), "--goal", "verify slice 1", "--dry-run"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "AUDITOR" in out                       # auditor's skeptic-verify protocol seed
    assert "cartridge equip intent" in out        # auditor's cartridge
    assert "bus" in out and "post echelon.build auditor" in out


# ── 2-role convene SMOKE: post -> peer reacts -> posts back ──────────────────


def test_two_role_convene_smoke_over_bus(bus_db):
    """The spec's required 2-role smoke, simulated at the bus layer (no live harness): an
    architect posts a slice, a builder 'watcher' drains it and the builder posts a DONE back,
    the architect drains the DONE. This exercises the exact drain/feed contract the live
    watcher loop runs — own-posts don't echo, each peer sees only the other's messages."""
    bus = EchelonBus(bus_db, mirror_jsonl=soc.jsonl_mirror_for(bus_db))
    ch = "echelon.build"

    # fresh-start cursor advance for both peers (what the watcher does on launch)
    assert bus.drain("architect", [ch]) == []
    assert bus.drain("builder", [ch]) == []

    # 1) architect posts a slice
    bus.post(ch, "architect", "BUILD slice-1: add the foo")

    # 2) builder's watcher drains -> sees the slice (not its own posts)
    inbox = bus.drain("builder", [ch])
    assert [m.body for m in inbox] == ["BUILD slice-1: add the foo"]

    # builder 'reacts' and posts a DONE back (this is what the harness would do)
    bus.post(ch, "builder", "DONE slice-1: foo added, tests green")

    # 3) architect's watcher drains -> sees the builder's DONE (and not its own slice post)
    reply = bus.drain("architect", [ch])
    assert [m.body for m in reply] == ["DONE slice-1: foo added, tests green"]

    # the builder does NOT re-see its own DONE on its next drain
    assert bus.drain("builder", [ch]) == []
