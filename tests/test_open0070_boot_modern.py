"""OPEN-0070 BOOT RITUAL MODERNIZATION — targeted tests.

Four builds, each with its own focused test:
  (1) JUDGE-LAZY BOOT  — a recall fired while tools.waking is set (the boot gate) runs warmth
      PRE-FILTER ONLY: the judge provider must not be consulted, no network. The judge engages
      lazily once waking clears (the first REAL recall inside the loop).
  (2) BOOT URLOPEN AUDIT — vision/consult attach is LAZY: the routing resolver runs zero times
      at attach and exactly once at first use.
  (3) SCOPE AUTODETECT — scope_from_room walks up for .echelon/room.json, falls back to the
      harness contract, and reports the source ("room"/"contract").
  (4) MOJIBAKE — cli.py carries no cp1252 mojibake runs (fixed to real UTF-8).
  Plus: boot() reports presented (vs seeded) so the display is honest on a re-boot.
"""
from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from echelon_engine.atoms import boot
from echelon_engine.atoms.identity import SELF_SCOPE
from echelon_engine.atoms.store import SeedStore
from echelon_engine.atoms.tools import ToolRegistry
from echelon_engine.contracts import scope_from_room

SEED_TEXT = "quantum flux capacitor entanglement coherence decoherence wavefunction"
THOUGHT = "purple zebra salsa dancing on mars"   # zero lexical overlap with the seed


class _Resp:
    status = "success"
    content = '{"best_index": 0, "score": 0.9, "why": "same territory"}'
    tokens_in = 12
    tokens_out = 5


class _JudgeFake:
    """Recording provider: judge_warmth sends through .send(messages, model_id, temperature)."""

    def __init__(self):
        self.sent = 0

    def send(self, messages, model_id, temperature=0, tools=None):
        self.sent += 1
        return _Resp()


@pytest.fixture
def store(tmp_path):
    return SeedStore(tmp_path / "core.db", v2_db=tmp_path / "v2.db")


@pytest.fixture
def registry(tmp_path):
    return ToolRegistry(tmp_path, allow_write=False, allow_bash=False)


def _recall_result(reg: ToolRegistry, thought: str) -> dict:
    out = reg.execute("recall", {"thought": thought})
    return json.loads(out)


# ── (1) JUDGE-LAZY BOOT ──────────────────────────────────────────────────────

def test_waking_gate_recall_runs_without_the_judge(store, registry):
    store.remember("echelon-self", SEED_TEXT, kind="cv", tier="core")
    judge = _JudgeFake()
    registry.attach_recall(store, "echelon-self", judge_provider=judge, judge_model="deepseek-chat")

    # THE BOOT GATE: loop.run sets tools.waking around the soul ritual. A reach here must NOT
    # draw a judge call — warmth pre-filter (lexical) only.
    registry.waking = True
    r = _recall_result(registry, THOUGHT)
    assert judge.sent == 0, "boot-gate recall must not call the judge (no network at boot)"
    assert r["verdict"] == "cold", "no lexical overlap -> cold without the judge"

    # THE LOOP: waking cleared -> the first REAL recall engages the judge lazily.
    registry.waking = False
    r2 = _recall_result(registry, THOUGHT)
    assert judge.sent == 1, "the judge engages on the first REAL recall inside the loop"
    assert r2["verdict"] == "warm" and "recognized" in r2, "judged reading surfaces the recognition"


def test_waking_flag_defaults_to_false(store, registry):
    """No boot ritual -> the flag is absent and the judge is on from the first recall."""
    store.remember("echelon-self", SEED_TEXT, kind="cv", tier="core")
    judge = _JudgeFake()
    registry.attach_recall(store, "echelon-self", judge_provider=judge, judge_model="deepseek-chat")
    assert not getattr(registry, "waking", False)
    _recall_result(registry, THOUGHT)
    assert judge.sent == 1


# ── (2) LAZY ATTACH: zero probes at boot, resolve at first use ───────────────

def test_attach_look_resolves_lazily_on_first_use(registry):
    calls = []

    def resolver():
        calls.append(1)
        raise RuntimeError("no vision tier buildable from the routing chain")

    registry.attach_look(None, None, vision_model="", vision_resolver=resolver)
    assert calls == [], "attach must not probe the routing chain at boot"
    out = registry.execute("look_at_image", {"path": "nope.png", "question": "what?"})
    assert calls == [1], "the wire resolves at FIRST use"
    assert "vision tier unavailable" in out


def test_attach_consult_resolves_lazily_on_first_use(registry):
    calls = []

    def resolver():
        calls.append(1)
        return _JudgeFake(), "claude-sonnet-4.6"

    registry.attach_consult(None, None, reasoner_model="", reasoner_resolver=resolver)
    assert calls == [], "attach must not probe the routing chain at boot"
    out = registry.execute("consult", {"situation": "should I rm -rf /?", "about_to": "rm -rf /"})
    assert calls == [1], "the reasoner wire resolves at FIRST use"
    body = json.loads(out)
    assert body.get("advice") or "unresolved" in out


# ── (3) SCOPE AUTODETECT from the working room ───────────────────────────────

def test_scope_from_room_walks_up_and_reports_source(tmp_path):
    room = tmp_path / "repo"; room.mkdir()
    (room / ".echelon").mkdir()
    (room / ".echelon" / "room.json").write_text(json.dumps({"scope": "acme"}), encoding="utf-8")
    deep = room / "a" / "b" / "c"; deep.mkdir(parents=True)
    assert scope_from_room(deep) == ("acme", "room")
    assert scope_from_room(room) == ("acme", "room")


def test_scope_from_room_falls_back_to_the_harness_contract(tmp_path):
    base = tmp_path / "no-room"; base.mkdir()
    (base / "echelon-harness-contract.json").write_text(json.dumps({"scope": "zephyr"}), encoding="utf-8")
    nested = base / "x"; nested.mkdir()
    assert scope_from_room(nested) == ("zephyr", "contract")


def test_scope_from_room_none_without_room_or_contract(tmp_path):
    assert scope_from_room(tmp_path / "nowhere") is None


def test_room_wins_over_a_contract_in_the_same_tree(tmp_path):
    room = tmp_path / "repo"; room.mkdir()
    (room / ".echelon").mkdir()
    (room / ".echelon" / "room.json").write_text(json.dumps({"scope": "echelon"}), encoding="utf-8")
    (room / "echelon-harness-contract.json").write_text(json.dumps({"scope": "other"}), encoding="utf-8")
    assert scope_from_room(room) == ("echelon", "room")


# ── (4) MOJIBAKE gone from cli.py ────────────────────────────────────────────

def test_cli_py_has_no_cp1252_mojibake():
    src = Path(__file__).resolve().parents[1] / "echelon_engine" / "agent" / "cli.py"
    text = src.read_text(encoding="utf-8")
    for marker in ("â€", "Ã", "â†", "â‡"):
        assert marker not in text, f"mojibake marker {marker!r} still in cli.py"
    assert "—" in text, "real em-dashes replaced the mojibake"
    assert "\r" not in text, "cli.py stays LF"


# ── boot display honesty: presented (what was shown) vs seeded (newly minted) ─

def test_boot_reports_presented_separate_from_minted(store):
    first = boot.boot(store, self_scope=SELF_SCOPE)
    second = boot.boot(store, self_scope=SELF_SCOPE)   # a re-boot: nothing re-minted
    assert first.seeded > 0 and second.seeded == 0      # mint semantics unchanged
    assert first.presented == second.presented > 0      # the offering is identical and NONZERO —
    # a mature soul must never display as "0 soul seeds" when its seeds are on the table
