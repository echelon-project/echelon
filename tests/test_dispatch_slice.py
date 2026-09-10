"""The DISPATCH vertical slice — proof the chainboard architecture works on a real flow.

Proves, at $0 (injected leaves, no model call):
  1. the dispatch chain runs end-to-end through the service gate (warm->...->earn) and the
     per-step record exists (the per-step evidence trace-cards will consume),
  2. fail-fast: a no-write-hands build goal makes hands_check FAIL and run/verify/earn are
     SKIPPED (not run) — the trap (claim-done-wrote-nothing) is structurally impossible,
  3. the DispatchBoard GATE is a real kill-switch: closed gate hard-stops dispatch,
  4. a green run earns (q=85), a red run earns the stall (q=35) — honesty by outcome.
"""
import pytest

from echelon_sdk.exceptions import FrameworkError
from echelon_engine import services
from echelon_engine.services_dispatch import dispatch_chain
from echelon_engine.boards.dispatch_board import DispatchBoard


# ── 1. end-to-end chain + per-step record ─────────────────────────────────
def test_dispatch_chain_runs_and_records_every_step():
    r = dispatch_chain("summarize the notes", scope="echelon")  # read goal, dry run
    assert r.ok
    names = [s["name"] for s in r.steps]
    # the real flow, in order, each a recorded step
    assert names == ["dispatch", "warm", "hands_check", "build_guidance",
                     "run", "where_ok", "verify", "earn"]
    assert r.value["outcome"]["ok"] is True
    assert r.value["earned"]["q"] == 85.0


# ── 2. fail-fast: no write hands on a build goal short-circuits ────────────
def test_no_write_hands_build_goal_fails_fast():
    r = dispatch_chain("implement slugify in slug.py", scope="echelon", can_write=False)
    assert not r.ok                      # the chain failed
    # hands_check is the failing step; run/verify/earn must be SKIPPED, never executed
    by_name = {s["name"]: s for s in r.steps}
    assert isinstance(by_name["hands_check"]["error"], PermissionError)
    assert by_name["run"]["skipped"] is True
    assert by_name["earn"]["skipped"] is True
    # nothing was earned because nothing ran (no false credit)
    assert "earned" not in (r.value or {}) or r.value.get("earned") is None


# ── 3. the gate is a real kill-switch ─────────────────────────────────────
def test_board_gate_hard_stops_dispatch():
    board = DispatchBoard()
    assert board.dispatch("read the readme")["outcome"]["ok"] is True   # open: works
    board.gate = "closed"
    with pytest.raises(FrameworkError):
        board.dispatch("read the readme")                              # closed: hard-stop


# ── 4. honesty by outcome: green earns, red earns the stall ───────────────
def test_green_earns_red_stalls():
    green = dispatch_chain("read x", run_fn=lambda c: {"status": "completed", "answer": "ok"})
    assert green.value["earned"]["q"] == 85.0
    red = dispatch_chain("read x", run_fn=lambda c: {"status": "failed", "answer": ""})
    assert red.value["earned"]["q"] == 35.0   # the stall, not a win


# ── the service gate require() raises on a failed chain ───────────────────
def test_service_gate_require_raises_on_failure():
    with pytest.raises(FrameworkError):
        services.run_dispatch("implement x in y.py", can_write=False)
    # but dispatch_result returns the record without raising (for the trace sink)
    rec = services.dispatch_result("implement x in y.py", can_write=False)
    assert not rec.ok and rec.errors
