"""The dispatch chain composed on REAL migrated atoms (not $0 stubs).

The slice in test_dispatch_slice.py proved the chain SHAPE with injected leaves. This
proves the next step: warm + earn are now the REAL engine atoms — warmth/store recall
and cards trace-credit — wired through the services gate (dispatch_with_atoms). run/verify
still use the dry default (the partner loop + on-disk check migrate later).

Isolated: SeedStore + CardStore on tmp dbs (the store.py v2_db fix makes this possible),
so the real bank is never touched.
"""
import pytest

from echelon_engine.atoms.store import SeedStore
from echelon_engine.atoms.cards import CardStore
from echelon_engine.services import dispatch_with_atoms
from echelon_engine.services_dispatch_atoms import make_warm_fn, make_earn_fn, _craft_label
from echelon_engine.atoms.uame import SCORE_BENCHMARK


@pytest.fixture
def stores(tmp_path):
    store = SeedStore(tmp_path / "core.db", v2_db=tmp_path / "core_v2.db")
    cards = CardStore(tmp_path / "core_v2.db")     # same v2 db the SeedStore writes
    return store, cards


# ── the REAL warm leaf surfaces a planted seed ────────────────────────────
def test_warm_leaf_is_real_recall(stores):
    store, _ = stores
    store.remember("echelon", "always pass python -X utf8 to dodge the cp1252 crash",
                   coordinate="tooling:utf8")
    warm_fn = make_warm_fn(store)
    w = warm_fn("echelon", "python -X utf8 avoids the cp1252 crash")
    assert w["verdict"] in ("warm", "lukewarm")    # it actually recognized the planted seed
    assert any("utf8" in m for m in w["warmest"])  # the move body came back (real recall)


# ── the REAL earn leaf moves a craft card by trace ────────────────────────
def test_earn_leaf_credits_on_green(stores):
    _, cards = stores
    earn_fn = make_earn_fn(cards)
    res = earn_fn("fix the broken parser", ok=True)
    assert res["card"] == "craft:bugfix-loop"      # routed by the goal words
    assert res["credited"] is True
    assert res["card_score"] > SCORE_BENCHMARK      # a green trace earned real weight


def test_earn_leaf_marks_stall_on_red(stores):
    _, cards = stores
    earn_fn = make_earn_fn(cards)
    res = earn_fn("implement the new feature", ok=False)
    assert res["card"] == "craft:feature-loop"
    assert res["card_score"] < SCORE_BENCHMARK      # a red trace marks the stall, below neutral


def test_craft_label_routing():
    assert _craft_label("fix the crash") == "craft:bugfix-loop"
    assert _craft_label("build a new dashboard") == "craft:feature-loop"


# ── the WHOLE chain composed on real leaves ───────────────────────────────
def test_dispatch_with_atoms_end_to_end(stores):
    store, cards = stores
    store.remember("echelon", "the dispatch chain composes real atoms",
                   coordinate="tooling:dispatch")
    result = dispatch_with_atoms("fix a bug in the dispatch chain", store=store, cards=cards)
    assert result.ok
    names = [s["name"] for s in result.steps]
    assert names == ["dispatch", "warm", "hands_check", "build_guidance",
                     "run", "where_ok", "verify", "earn"]
    ctx = result.value
    # the warm step carried a REAL verdict (not the stub 'cold')
    assert ctx["warm"]["verdict"] in ("warm", "lukewarm", "cold")
    assert "cartridges" in ctx["warm"]              # real-leaf shape, not the {'verdict':'cold'} stub
    # the earn step credited a REAL card (dry run => status 'dry-run' => verify ok => green earn)
    assert ctx["earned"]["card"] == "craft:bugfix-loop"
    assert ctx["earned"]["credited"] is True


def test_dispatch_with_atoms_respects_hands_gate(stores):
    store, cards = stores
    # a build goal handed to a NO-WRITE role must fail-fast at hands_check (run/verify/earn skipped)
    result = dispatch_with_atoms("implement a writer", store=store, cards=cards, can_write=False)
    assert not result.ok
    skipped = {s["name"] for s in result.steps if s["skipped"]}
    assert {"run", "verify", "earn"} <= skipped     # the claim-done-wrote-nothing trap stays impossible
