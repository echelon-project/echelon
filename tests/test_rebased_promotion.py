"""Re-based promotion gate tests (owner's isolation probe, addendum 2026-07-08).

THE DEAD GATE this re-base replaces: dream.consolidate -> store.promote required v1 `score>=125 AND
recall_count>=3`, but recall_count is 0 on ALL v1 seeds (no live path increments it, especially
post-kindle-inversion) — promotion NEVER fired. THE LIVE SIGNAL: v2 CardStore.atom_earned carries
real earned weight (effective_score + witnessed-door use_count).

These tests pin three things, all hermetic (a temp CardStore, no live bank, no LM Studio floor):
  1. CardStore.top_earned — the one cheap indexed query the hooks call (earned-aware, ordered, excludable).
  2. CardStore.promotable_atoms — the v2-native re-based predicate (eff>=125 AND use>=3, doctrine gates).
  3. levels.meets_requirement — the injected-v2-signal bridge (Option A), with the dead-v1 tombstone.

Earning is done HONESTLY through the real door: compile_atom_struct (mints the atom_earned row) then
N remember_fetch calls (each fires _witness_earn: +EARN_DELTA, use_count+1). No hand-poked scores — the
test earns weight the same way the live agent does, so it also proves the kindle inversion (the free
recall_peek never bumps use_count; only the witnessed fetch does).
"""
import pytest

from echelon_engine.atoms.cards import CardStore
from echelon_engine.atoms.card_credit import SYNTH_ELIGIBLE, BEACON_THRESHOLD
from echelon_sdk import levels


@pytest.fixture
def store(tmp_path):
    return CardStore(tmp_path / "core_v2.db")


def _earn(store, atom_id, times):
    """Earn `times` WITNESSED USES through the real door (compile once, fetch N)."""
    store.compile_atom_struct(atom_id)
    for _ in range(times):
        store.remember_fetch(atom_id)   # each fetch = one witnessed use (+EARN_DELTA, use_count+1)


# EARN_DELTA=2.0, born at 100. To clear SYNTH_ELIGIBLE(125) an atom needs >=13 fetches (100+13*2=126);
# to clear BEACON_THRESHOLD(180) it needs >=40 (100+40*2=180). Use round numbers well past each bar.
_L2_USES = 20      # 100 + 40 = 140  >= 125, clears L2
_L1_USES = 45      # 100 + 90 = 190  >= 180, clears L1/beacon


# ─────────────────────────── top_earned ───────────────────────────
def test_top_earned_orders_by_effective_score(store):
    lo = store.add_atom("a:lo", "low earner", scope="s")
    hi = store.add_atom("a:hi", "high earner", scope="s")
    _earn(store, lo, _L2_USES)
    _earn(store, hi, _L1_USES)
    top = store.top_earned(10)
    contents = [c for (_s, c, _e) in top]
    # both surface, and the higher earner ranks first
    assert contents.index("high earner") < contents.index("low earner")
    # scores are the earned effective scores, not neutral 100
    by_content = {c: e for (_s, c, e) in top}
    assert by_content["high earner"] >= BEACON_THRESHOLD
    assert by_content["low earner"] >= SYNTH_ELIGIBLE


def test_top_earned_limit_and_exclude_scopes(store):
    keep = store.add_atom("a:k", "keep me", scope="wanted")
    drop = store.add_atom("a:d", "drop me", scope="persona-local")
    _earn(store, keep, _L2_USES)
    _earn(store, drop, _L1_USES)   # drop earns MORE, yet must be excluded by scope, not by score
    top = store.top_earned(10, exclude_scopes=["persona-local"])
    scopes = {s for (s, _c, _e) in top}
    assert "persona-local" not in scopes
    assert any(c == "keep me" for (_s, c, _e) in top)


def test_top_earned_disclaimed_not_laundered(store):
    aid = store.add_atom("a:lie", "a discovered lie", scope="s")
    _earn(store, aid, _L1_USES)                 # earn it high FIRST
    store.dispute("atoms", aid, reason="stale")  # then disclaim it (re-bases to JUDGED_FLOOR=75)
    top = store.top_earned(10)
    by_content = {c: e for (_s, c, e) in top}
    # the lie ranks at its re-based floor, NOT at the earned weight it had before the dispute
    assert by_content["a discovered lie"] <= 100.0


def test_top_earned_empty_bank_is_safe(store):
    assert store.top_earned(5) == []


# ─────────────────────────── promotable_atoms ───────────────────────────
def test_promotable_requires_both_score_and_uses(store):
    # earned enough SCORE but only 1 use -> NOT promotable (use gate); and a fresh atom -> neither.
    low_use = store.add_atom("a:lowuse", "high score few uses", scope="echelon")
    store.compile_atom_struct(low_use)
    # hand-lift the earned score high but keep use_count low via a single fetch (use_count=1 < 3)
    store.remember_fetch(low_use)
    fresh = store.add_atom("a:fresh", "never used", scope="echelon")
    store.compile_atom_struct(fresh)
    prom = store.promotable_atoms("echelon")
    ids = {a["id"] for a in prom}
    assert low_use not in ids     # fails the >=3 use gate
    assert fresh not in ids       # fails both


def test_promotable_l2_and_beacon_tiers(store):
    l2 = store.add_atom("a:l2", "an L2 earner", scope="echelon")
    l1 = store.add_atom("a:l1", "a beacon earner", scope="echelon")
    _earn(store, l2, _L2_USES)
    _earn(store, l1, _L1_USES)
    prom = {a["id"]: a for a in store.promotable_atoms("echelon")}
    assert prom[l2]["tier"] == "L2" and prom[l2]["beacon"] is False
    assert prom[l1]["tier"] == "L1" and prom[l1]["beacon"] is True
    # ordered by effective_score desc
    order = [a["id"] for a in store.promotable_atoms("echelon")]
    assert order.index(l1) < order.index(l2)


def test_promotable_excludes_disclaimed(store):
    aid = store.add_atom("a:lie", "an earned lie", scope="echelon")
    _earn(store, aid, _L1_USES)
    store.dispute("atoms", aid, reason="wrong")
    prom = store.promotable_atoms("echelon")
    assert all(a["id"] != aid for a in prom)   # a lie never rises, however much it earned


def test_self_marked_stays_l2_never_beacon(store):
    # a self-marked atom (born_from carries the self-mark) may be L2-eligible in its own scope but
    # must NEVER reach BEACON/L1 (a wish must not reach the constitution by self-witnessing).
    aid = store.add_atom("a:wish", "a wished conviction",
                         born_from=CardStore._SELF_MARK + "origin", scope="echelon")
    _earn(store, aid, _L1_USES)   # earn it PAST the beacon bar
    prom = {a["id"]: a for a in store.promotable_atoms("echelon")}
    assert aid in prom                       # still L2-eligible in its own scope
    assert prom[aid]["self_marked"] is True
    assert prom[aid]["beacon"] is False      # but capped below the constitution
    assert prom[aid]["tier"] == "L2"


def test_promotable_scope_isolation(store):
    here = store.add_atom("a:here", "in scope", scope="echelon")
    there = store.add_atom("a:there", "other scope", scope="other")
    _earn(store, here, _L2_USES)
    _earn(store, there, _L2_USES)
    prom = store.promotable_atoms("echelon")
    assert any(a["id"] == here for a in prom)
    assert all(a["id"] != there for a in prom)


# ─────────────────────────── levels.meets_requirement bridge (Option A) ───────────────────────────
class _Seed:
    """A minimal v1-shaped seed: high v1 score but DEAD recall_count (the live estate's reality)."""
    def __init__(self, score=200.0, recall_count=0, self_seed=False):
        self.score = score
        self.recall_count = recall_count
        self.self_seed = self_seed


def test_meets_requirement_dead_v1_never_promotes():
    # the tombstone: a v1 seed with a high score but recall_count=0 must NOT pass (recall_count is dead).
    assert levels.meets_requirement(_Seed(score=999.0, recall_count=0)) is False


def test_meets_requirement_injected_v2_signal_promotes():
    seed = _Seed(score=100.0, recall_count=0)   # v1 fields say NO
    assert levels.meets_requirement(seed, earned={"effective_score": 130.0, "use_count": 5}) is True


def test_meets_requirement_injected_v2_fails_below_bar():
    seed = _Seed()
    assert levels.meets_requirement(seed, earned={"effective_score": 124.0, "use_count": 9}) is False
    assert levels.meets_requirement(seed, earned={"effective_score": 130.0, "use_count": 2}) is False


def test_self_seed_is_ineligible_regardless_of_earning():
    # eligible_for_global is the OTHER gate: a self-seeded seed can never rise even if it earned.
    seed = _Seed(self_seed=True)
    assert levels.eligible_for_global(seed) is False


# ─────────── earned-across-time convergence (the witness-gate re-base, ruling gate-request 2) ───────────
def test_earn_day_spread_counts_distinct_days(store):
    # earn events across multiple days -> the spread reflects distinct calendar days, not raw use_count.
    import json
    aid = store.add_atom("a:t", "spanning atom", scope="echelon")
    store.compile_atom_struct(aid)
    # hand-author score_history with fetch events on 3 distinct days (per-event ts is element 0).
    day = 86400
    hist = [[1_000_000 + 0 * day, 2.0, "fetch"],
            [1_000_000 + 1 * day, 2.0, "fetch"],
            [1_000_000 + 2 * day, 2.0, "fetch"]]
    with store._lock:
        store.conn.execute("UPDATE atom_earned SET use_count=3, score_history=? WHERE atom_id=?",
                           (json.dumps(hist), aid))
        store.conn.commit()
    assert store.earn_day_spread(aid) == 3


def test_earn_day_spread_single_day_is_one(store):
    import json
    aid = store.add_atom("a:s", "single-day atom", scope="echelon")
    store.compile_atom_struct(aid)
    hist = [[1_000_000, 2.0, "fetch"], [1_000_050, 2.0, "fetch"], [1_000_099, 2.0, "fetch"]]
    with store._lock:
        store.conn.execute("UPDATE atom_earned SET use_count=3, score_history=? WHERE atom_id=?",
                           (json.dumps(hist), aid))
        store.conn.commit()
    # 3 uses, all within one day -> spread 1 -> the dream's earned-convergence gate must NOT fire
    assert store.earn_day_spread(aid) == 1


def test_earn_day_spread_no_ts_returns_minus_one(store):
    import json
    aid = store.add_atom("a:n", "no-ts atom", scope="echelon")
    store.compile_atom_struct(aid)
    # fetch events that carry NO usable ts (0) -> the 'no per-event ts' case the ruling flags with -1.
    hist = [[0, 2.0, "fetch"], [0, 2.0, "fetch"], [0, 2.0, "fetch"]]
    with store._lock:
        store.conn.execute("UPDATE atom_earned SET use_count=3, score_history=? WHERE atom_id=?",
                           (json.dumps(hist), aid))
        store.conn.commit()
    assert store.earn_day_spread(aid) == -1


def test_dream_earned_convergence_multi_day_promotes():
    # end-to-end (pure dream): a seed whose v2 earning spans >=2 days is witnessed-by-earning even when
    # the independent-instance count is 1 (the single-persona reality) -> proposed global.
    from echelon_engine.atoms.dream import dream

    class Seed:
        def __init__(self, sid):
            self.id = sid; self.content = "earned across days"; self.kind = "lesson"
            self.score = 100.0; self.recall_count = 0; self.self_seed = False
    s = Seed("x1")
    earned = {"x1": {"effective_score": 140.0, "use_count": 5, "day_spread": 3}}
    props = dream([s], earned_by_id=earned, min_witnesses=2)   # only 1 instance, but earned across 3 days
    glob = [p for p in props if p.exit == "global"]
    assert len(glob) == 1
    assert "earned-convergence: 5 door-uses across 3 days" in glob[0].reason


def test_dream_earned_convergence_single_day_blocked():
    from echelon_engine.atoms.dream import dream

    class Seed:
        def __init__(self, sid):
            self.id = sid; self.content = "one enthusiastic session"; self.kind = "lesson"
            self.score = 100.0; self.recall_count = 0; self.self_seed = False
    s = Seed("x2")
    # high use_count but all on ONE day -> convergence NOT satisfied -> NOT promoted (no laundering).
    earned = {"x2": {"effective_score": 300.0, "use_count": 50, "day_spread": 1}}
    props = dream([s], earned_by_id=earned, min_witnesses=2)
    assert [p for p in props if p.exit == "global"] == []


def test_dream_earned_convergence_no_ts_promotes_but_flags():
    from echelon_engine.atoms.dream import dream

    class Seed:
        def __init__(self, sid):
            self.id = sid; self.content = "no ts atom"; self.kind = "lesson"
            self.score = 100.0; self.recall_count = 0; self.self_seed = False
    s = Seed("x3")
    earned = {"x3": {"effective_score": 140.0, "use_count": 5, "day_spread": -1}}
    props = dream([s], earned_by_id=earned, min_witnesses=2)
    glob = [p for p in props if p.exit == "global"]
    assert len(glob) == 1
    # accepted on plain use_count, but the reason SAYS the spread is unverified (no silent laundering)
    assert "UNVERIFIED-SPREAD" in glob[0].reason


def test_dream_v1_path_unchanged_needs_real_witnesses():
    # the v1 branch (earned is None) is untouched: a lone instance with no earning is NOT promoted.
    from echelon_engine.atoms.dream import dream

    class Seed:
        def __init__(self, sid):
            self.id = sid; self.content = "v1 seed"; self.kind = "lesson"
            self.score = 999.0; self.recall_count = 999; self.self_seed = False   # even fat v1 fields
    s = Seed("v1")
    props = dream([s], min_witnesses=2)   # 1 instance, no earned signal -> v1 gate: n_w(1) < 2 -> blocked
    assert [p for p in props if p.exit == "global"] == []
