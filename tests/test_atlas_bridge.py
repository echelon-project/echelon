"""Focused unit test for echelon_engine.atoms.atlas_bridge.

MINIMAL-SEAM module: one public function, seed_scope_from_atlas(), which reads *.json
component cards from a dir and mints one charged seed per card via store.remember(). So
this is a SMOKE test on the real surface — a tmp atlas dir + an ISOLATED SeedStore — plus
direct checks on the deterministic status->charge table and the line/dedup behaviour.
"""
from __future__ import annotations

import json

from echelon_engine.atoms.atlas_bridge import (
    seed_scope_from_atlas,
    _STATUS_CHARGE,
    _DEFAULT_CHARGE,
)
from echelon_engine.atoms.store import SeedStore


def _write_card(d, name, card):
    (d / f"{name}.json").write_text(json.dumps(card), encoding="utf-8")


def test_charge_table_is_deterministic():
    # The recorded judgment -> felt charge mapping ($0, no LLM). Spot-check the contract.
    assert _STATUS_CHARGE["live"] == (0.5, 0.2)
    assert _STATUS_CHARGE["empty_state"] == (-0.4, 0.5)
    assert _STATUS_CHARGE["degraded"] == (-0.5, 0.6)
    assert _DEFAULT_CHARGE == (0.0, 0.0)


def test_seeds_one_per_card(tmp_path):
    define_dir = tmp_path / "define"
    define_dir.mkdir()
    _write_card(define_dir, "card_a", {"type": "metric", "status": "live", "what_it_is": "AOV"})
    _write_card(define_dir, "card_b", {"type": "table", "status": "empty_state",
                                       "role": "orders", "gap_reason": "no feed yet"})
    store = SeedStore(tmp_path / "core.db", v2_db=tmp_path / "core_v2.db")

    written = seed_scope_from_atlas(define_dir, "dash-scope", store)
    assert written == 2
    seeds = store.seeds(scope="dash-scope")
    assert len(seeds) == 2
    # the empty_state gap_reason is folded into the felt seed line
    assert any("GAP: no feed yet" in s.content for s in seeds)


def test_unknown_status_uses_default_charge(tmp_path):
    define_dir = tmp_path / "define"
    define_dir.mkdir()
    _write_card(define_dir, "weird", {"type": "x", "status": "mystery", "what_it_is": "thing"})
    store = SeedStore(tmp_path / "core.db", v2_db=tmp_path / "core_v2.db")

    assert seed_scope_from_atlas(define_dir, "s", store) == 1
    s = store.seeds(scope="s")[0]
    assert s.valence == 0.0 and s.arousal == 0.0  # default charge


def test_bad_json_is_skipped(tmp_path):
    define_dir = tmp_path / "define"
    define_dir.mkdir()
    (define_dir / "broken.json").write_text("{ not valid json", encoding="utf-8")
    _write_card(define_dir, "good", {"type": "m", "status": "live", "what_it_is": "ok"})
    store = SeedStore(tmp_path / "core.db", v2_db=tmp_path / "core_v2.db")

    written = seed_scope_from_atlas(define_dir, "s", store)
    assert written == 1  # only the good card counted; broken one skipped


def test_empty_dir_writes_nothing(tmp_path):
    define_dir = tmp_path / "define"
    define_dir.mkdir()
    store = SeedStore(tmp_path / "core.db", v2_db=tmp_path / "core_v2.db")
    assert seed_scope_from_atlas(define_dir, "s", store) == 0
