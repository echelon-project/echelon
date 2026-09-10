"""tests/test_governor.py -- unit tests for echelon_engine.agent.governor (GOVERNOR v0).

Tests the pure composition mechanics: facet decomposition from an EVENT, per-atom
max-score aggregation with the winning facet, the fovea + passed_over accounting,
canary determinism, and the ledger file. The bank is stubbed (an injected warmth_fn
and fake seeds) -- no network, no DB.
"""
from __future__ import annotations

import json

import pytest

pytest.importorskip("echelon_engine.agent.governor",
                    reason="echelon_engine.agent.governor not available")

from echelon_engine.agent.governor import (
    DUMP_CAP, PREVIEW_CLIP,
    GovernorPackage, StepEvent,
    compose, facets, write_ledger,
)


# -- stub bank -------------------------------------------------------------

class _Seed:
    def __init__(self, sid, slug, content):
        self.id = sid
        self.coordinate = f"echelon:{slug}"
        self.content = content


class _Hit:
    def __init__(self, seed, score):
        self.seed = seed
        self.score = score


def _seeds(n):
    return [_Seed(f"id{i}", f"atom-{i}", f"[atom-{i}] lesson number {i}\n\nbody text") for i in range(n)]


def _fixed_warmth(mapping):
    """warmth_fn stub: query -> [(seed, score)]. Signature matches (query, store, scope)."""
    def _fn(query, store, scope):
        return [_Hit(s, sc) for s, sc in mapping.get(query, [])]
    return _fn


# -- facets ----------------------------------------------------------------

class TestFacets:
    def test_task_facet_first(self):
        f = facets(StepEvent(task="fix the ingest gate"))
        assert f[0] == ("task", "fix the ingest gate")

    def test_empty_task_yields_no_task_facet(self):
        assert facets(StepEvent(task="   ")) == []

    def test_file_uses_basename(self):
        f = facets(StepEvent(task="t", files=["D:\\WORK\\x\\ingest.py"]))
        assert ("file:ingest.py", "ingest.py") in f

    def test_error_is_truncated_and_squeezed(self):
        long_err = "boom " * 200
        f = facets(StepEvent(task="t", errors=[long_err]))
        _, q = [x for x in f if x[0].startswith("error:")][0]
        assert len(q) <= 200 and "  " not in q

    def test_command_facet_keyed_on_head(self):
        f = facets(StepEvent(task="t", commands=["pytest tests/test_governor.py -q"]))
        names = [n for n, _ in f]
        assert "cmd:pytest" in names

    def test_facet_names_deduped(self):
        f = facets(StepEvent(task="t", files=["a/ingest.py", "b/ingest.py"]))
        assert len([n for n, _ in f if n == "file:ingest.py"]) == 1

    def test_order_is_stable(self):
        ev = StepEvent(task="t", files=["f.py"], commands=["ls"], errors=["e"])
        assert facets(ev) == facets(ev)


# -- compose: aggregation --------------------------------------------------

class TestComposeAggregation:
    def test_max_score_across_facets_wins(self):
        s = _seeds(1)[0]
        fn = _fixed_warmth({"t": [(s, 0.2)], "ingest.py": [(s, 0.8)]})
        pkg = compose(StepEvent(task="t", files=["ingest.py"]), None, "echelon", warmth_fn=fn)
        assert len(pkg.entries) == 1
        assert pkg.entries[0]["score"] == 0.8
        assert pkg.entries[0]["facet"] == "file:ingest.py"

    def test_zero_and_negative_scores_dropped(self):
        a, b = _seeds(2)
        fn = _fixed_warmth({"t": [(a, 0.0), (b, 0.5)]})
        pkg = compose(StepEvent(task="t"), None, "echelon", warmth_fn=fn)
        assert [e["slug"] for e in pkg.entries] == ["atom-1"]

    def test_slug_from_coordinate(self):
        fn = _fixed_warmth({"t": [(_seeds(1)[0], 0.5)]})
        pkg = compose(StepEvent(task="t"), None, "echelon", warmth_fn=fn)
        assert pkg.entries[0]["slug"] == "atom-0"

    def test_preview_is_clipped_single_line(self):
        s = _Seed("x", "long", "[long] " + ("word " * 200) + "\n\nbody")
        fn = _fixed_warmth({"t": [(s, 0.5)]})
        pkg = compose(StepEvent(task="t"), None, "echelon", warmth_fn=fn)
        prev = pkg.entries[0]["preview"]
        assert len(prev) <= PREVIEW_CLIP and "\n" not in prev

    def test_scoring_exception_does_not_wedge(self):
        def _boom(query, store, scope):
            raise RuntimeError("bank hiccup")
        pkg = compose(StepEvent(task="t"), None, "echelon", warmth_fn=_boom)
        assert pkg.entries == [] and pkg.passed_over == []

    def test_read_only_no_store_calls(self):
        """The store is never touched by compose itself (the warmth seam owns bank access)."""
        class _Explode:
            def __getattr__(self, name):
                raise AssertionError(f"compose touched the store: {name}")
        fn = _fixed_warmth({"t": [(_seeds(1)[0], 0.5)]})
        compose(StepEvent(task="t"), _Explode(), "echelon", warmth_fn=fn)


# -- compose: fovea + passed_over -----------------------------------------

class TestFovea:
    def _pkg(self, budget=15, foveate=True, n=40):
        seeds = _seeds(n)
        # strictly descending, always > 0 (a zero score is legitimately dropped)
        fn = _fixed_warmth({"t": [(s, 0.9 - i * (0.8 / max(n, 1))) for i, s in enumerate(seeds)]})
        return compose(StepEvent(task="t"), None, "echelon", budget=budget,
                       foveate=foveate, warmth_fn=fn)

    def test_budget_caps_entries(self):
        assert len(self._pkg(budget=5).entries) == 5

    def test_default_budget_is_fifteen(self):
        assert len(self._pkg().entries) == 15

    def test_passed_over_holds_the_remainder(self):
        pkg = self._pkg(budget=5, n=40)
        assert len(pkg.passed_over) == 35
        assert set(pkg.passed_over[0]) == {"id", "slug", "score", "facet"}

    def test_no_foveate_returns_strictly_more_entries(self):
        few = self._pkg(budget=5, n=40)
        many = self._pkg(budget=5, foveate=False, n=40)
        assert len(many.entries) > len(few.entries) == 5
        assert len(many.entries) == 40 and many.passed_over == []

    def test_no_foveate_is_still_capped(self):
        pkg = self._pkg(budget=5, foveate=False, n=DUMP_CAP + 25)
        assert len(pkg.entries) == DUMP_CAP
        assert len(pkg.passed_over) == 25

    def test_entries_sorted_by_score_desc(self):
        scores = [e["score"] for e in self._pkg(budget=10).entries]
        assert scores == sorted(scores, reverse=True)

    def test_ties_broken_stably_by_slug(self):
        seeds = _seeds(3)
        fn = _fixed_warmth({"t": [(s, 0.5) for s in reversed(seeds)]})
        pkg = compose(StepEvent(task="t"), None, "echelon", warmth_fn=fn)
        assert [e["slug"] for e in pkg.entries] == ["atom-0", "atom-1", "atom-2"]


# -- package + canary ------------------------------------------------------

class TestPackage:
    def _mk(self, task="t"):
        fn = _fixed_warmth({task: [(s, 0.5 - i * 0.01) for i, s in enumerate(_seeds(3))]})
        return compose(StepEvent(task=task), None, "echelon", warmth_fn=fn)

    def test_canary_shape(self):
        c = self._mk().canary
        assert c.startswith("#ECH:") and c.endswith("#") and len(c) == 16

    def test_canary_deterministic_for_same_event(self):
        assert self._mk().canary == self._mk().canary

    def test_canary_rotates_with_the_event(self):
        assert self._mk("t").canary != self._mk("other task").canary

    def test_composed_text_has_canary_and_entry_lines(self):
        pkg = self._mk()
        lines = pkg.composed_text.splitlines()
        assert lines[0].startswith(pkg.canary)
        assert len(lines) == 1 + len(pkg.entries)
        assert all(l.startswith("[") for l in lines[1:])

    def test_event_round_trips_into_the_package(self):
        ev = StepEvent(task="t", files=["a.py"], step_id="s1", deps_done=["s0"])
        fn = _fixed_warmth({"t": [(_seeds(1)[0], 0.5)]})
        pkg = compose(ev, None, "echelon", warmth_fn=fn)
        assert pkg.event["step_id"] == "s1" and pkg.event["deps_done"] == ["s0"]

    def test_package_is_json_serialisable(self):
        json.dumps(self._mk().to_dict())

    def test_flags_recorded(self):
        pkg = compose(StepEvent(task="t"), None, "echelon", budget=7, foveate=False,
                      warmth_fn=_fixed_warmth({}))
        assert pkg.budget == 7 and pkg.foveate is False and pkg.scope == "echelon"

    def test_determinism_same_event_same_package(self):
        a, b = self._mk(), self._mk()
        assert a.entries == b.entries and a.composed_text == b.composed_text


# -- ledger ----------------------------------------------------------------

class TestLedger:
    def test_ledger_written_when_dir_given(self, tmp_path):
        fn = _fixed_warmth({"t": [(s, 0.5) for s in _seeds(4)]})
        pkg = compose(StepEvent(task="t"), None, "echelon", budget=2,
                      ledger_dir=str(tmp_path), warmth_fn=fn)
        files = list(tmp_path.glob("governor-*.json"))
        assert len(files) == 1
        data = json.loads(files[0].read_text(encoding="utf-8"))
        assert data["canary"] == pkg.canary
        assert len(data["entries"]) == 2 and len(data["passed_over"]) == 2
        assert data["budget"] == 2 and data["foveate"] is True

    def test_no_ledger_when_dir_absent(self, tmp_path):
        compose(StepEvent(task="t"), None, "echelon", warmth_fn=_fixed_warmth({}))
        assert list(tmp_path.glob("governor-*.json")) == []

    def test_write_ledger_returns_path_and_creates_dir(self, tmp_path):
        pkg = GovernorPackage(canary="#ECH:abc123#", event={}, entries=[], passed_over=[],
                              composed_text="", ts=1)
        target = tmp_path / "nested" / "led"
        path = write_ledger(pkg, str(target))
        assert path.endswith("governor-1-abc123.json")
        assert json.loads(open(path, encoding="utf-8").read())["canary"] == "#ECH:abc123#"


# -- CLI wiring ------------------------------------------------------------

class TestCliWiring:
    def test_govern_verb_registered(self):
        from echelon_engine.__main__ import _route
        assert "govern" in _route()
