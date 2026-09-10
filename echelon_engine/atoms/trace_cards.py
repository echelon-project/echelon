"""trace_cards — the execution-trace → reinforce_card SINK (the v2 'unbuilt heart').

This is what makes "v1 source-only, everything updates v2" HONEST. The agent run loop already
produces the materials (which atoms each step loaded, the run status, token cost, the act→observe
transcript); this module turns a finished run into CARDS and rates them from the EXECUTION TRACE —
never from a model's say-so (the §7-P2 / relive-dont-migrate honesty law).

THE GRAIN (AMD-T1 audit, 2026-06-10): a card = ONE agent ACTION (one step that loaded atoms);
refs = the atom coordinates that step recalled. §6: "the command forecasts the next action".

Q FROM THE TRACE (OS honesty corrections over the council's first draft):
  - status != 'completed'  → Q = 50.0 (NEUTRAL). A failed run earns NOTHING; it does NOT punish the
    atoms it loaded (Q=0 would be (0-50)*k = -50 to every atom — punishing presence in a failure they
    may not have caused). On failure the cards are still CREATED (the episode happened) but NOT rated.
  - cost dimension is only scored when a baseline exists; a COLD run with no baseline EXCLUDES cost
    (unmeasurable ≠ bad). The rolling per-goal cost mean is persisted so the next run has a baseline.
  - Q is centred at 50 (zero §7-P2 delta); Q>50 only when the run completed AND was low-bad-ratio
    and/or cheaper than its own history.

Built by AMD-T1 (Qwen3-32B council) as T1+T2+T3, gated + corrected by the OS. See
runs/v2-cos-audit-20260610/ (the audit + build trail), [[v2-cos-card-reconstruction-and-cutover]],
[[relive-dont-migrate]], [[honest-seeder-v2-token-weight-forecast]].
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .cards import CardStore, DEFAULT_V2_DB

# Persist the rolling per-goal cost baseline next to the v2 db (not in the package tree).
BASELINE_FILE = Path(DEFAULT_V2_DB).parent / "trace_cost_baselines.json"

# A bad step is detected by anchored markers, NOT a loose substring scan — a benign result that merely
# MENTIONS the word "error" (e.g. a successful grep for 'error handling') must not count as a failure.
# We match a tool result whose START signals failure, or that carries a hard runtime-error marker.
import re as _re
_BAD_RESULT_PREFIX = _re.compile(
    r"^\s*(error\b|failed\b|fatal\b|exception\b|cannot\b|could not\b|no such\b|permission denied)",
    _re.IGNORECASE)
_BAD_RESULT_HARD = _re.compile(
    r"traceback \(most recent call last\)|[A-Za-z_]+Error:|[A-Za-z_]+Exception:"
    r"|\bexit code [1-9]|\bcommand failed\b|\btimed out\b", _re.IGNORECASE)
_GATE_BLOCK_SIGNALS = ("block", "deny")


BASELINE_WINDOW = 20   # rolling window: keep only the last N run-costs per goal (bounded growth +
                       # an avg that tracks the agent's CURRENT efficiency, not its whole history).


def _read_baselines() -> dict[str, list[float]]:
    """Load the per-goal cost history. Schema-guarded: anything not a {str: [num,...]} mapping is
    treated as absent (a corrupt/partial file never poisons Q — it just means no baseline yet)."""
    try:
        data = json.loads(BASELINE_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}
    if not isinstance(data, dict):
        return {}
    clean: dict[str, list[float]] = {}
    for k, v in data.items():
        if isinstance(k, str) and isinstance(v, list):
            nums = [float(x) for x in v if isinstance(x, (int, float))]
            if nums:
                clean[k] = nums[-BASELINE_WINDOW:]
    return clean


def _write_baselines(data: dict[str, list[float]]) -> None:
    BASELINE_FILE.parent.mkdir(parents=True, exist_ok=True)
    BASELINE_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")


def _goal_key(goal: str) -> str:
    return (goal or "")[:40].strip().replace(" ", "_") or "unnamed"


def count_bad_steps(transcript: list[dict] | None) -> int:
    """A BAD step (from the REAL transcript shape, loop.py): a role=='tool' entry whose `result`
    string carries an error signal, or a role=='gate' entry whose `decision` is a block/deny."""
    if not transcript:
        return 0
    bad = 0
    for step in transcript:
        role = step.get("role")
        if role == "tool":
            res = str(step.get("result", ""))
            if _BAD_RESULT_PREFIX.match(res) or _BAD_RESULT_HARD.search(res):
                bad += 1
        elif role == "gate":
            dec = str(step.get("decision", "")).lower()
            if any(sig in dec for sig in _GATE_BLOCK_SIGNALS):
                bad += 1
    return bad


# VERIFIED-FAILURE Q (audit #3): a predicate that RAN and returned False is a witnessed task-falseness —
# the same parser that pays the reward charges the failure (symmetric evidence gate). Below neutral but
# not punitive: a recorded-but-cold card, distinct from an unverifiable run (Q=50). Only attributed
# failure (the rule fired) docks weight; infra/transport failure never does (that's the weather).
VERIFIED_FAILURE_Q = 35.0


def compute_q(result, bad_steps: int, baseline_cost: float | None) -> float:
    """Q ∈ [0,100] from the execution trace. Centred at 50 (zero delta). NEVER a model self-rating.
    A non-completed run returns 50.0 (caller skips reinforcement). Pure + deterministic.

    VERIFY GATE (audit #3, gates-by-mechanism): if the result carries a `verify_outcome`, the TASK is
    graded by a MECHANICAL predicate, not transport success — the fix for "Q = the model answered
    something". Three states:
      - verify_outcome is False -> the rule RAN and FAILED -> Q = 35 (verified-failure, below neutral).
      - verify_outcome is None  -> UNVERIFIABLE (no predicate, or it couldn't run) -> Q capped at 50
        (a clean transport earns NEUTRAL, never the old +25 'it returned text' bonus).
      - verify_outcome is True  -> verified -> the clean-run / cheaper bonuses apply (Q may rise > 50).
    A result with NO `verify_outcome` attribute (a normal agent run) is unchanged — the gate is opt-in
    per result. This is what stops the self-amplifying script counterfeit (transport success -> Q=75)."""
    if getattr(result, "status", None) != "completed":
        return 50.0
    verify = getattr(result, "verify_outcome", "absent")
    if verify is False:
        return VERIFIED_FAILURE_Q          # witnessed task-falseness — the gate fired with the other sign
    if verify is None:
        return 50.0                        # unverifiable: transport ≠ task; neutral, no unearned bonus
    # verify is True (verified) OR absent (a normal agent run, gate not engaged): grade by the trace.
    steps = result.steps or 1
    bad_ratio = min(1.0, bad_steps / steps)
    good_bonus = (1.0 - bad_ratio) * 25.0          # up to +25 for a clean run
    cost_bonus = 0.0
    cost = (result.tokens_in or 0) + (result.tokens_out or 0)
    if baseline_cost is not None and baseline_cost > 0 and cost > 0 and cost < baseline_cost:
        cost_bonus = 25.0 * (1.0 - (cost / baseline_cost))   # up to +25 for getting cheaper
    q = 50.0 + good_bonus + cost_bonus
    return max(0.0, min(100.0, q))


def _set_current_run_prev(store: CardStore, card_id: str, prev_id: str) -> None:
    """Make a reused trace card point at this run's predecessor, not an older run's chain."""
    with store._lock:
        store.conn.execute("UPDATE cards SET prev=? WHERE id=?", (prev_id, card_id))
        store.conn.commit()


def _scan_current_run_cards(store: CardStore, cards: list[str]) -> dict[str, Any] | None:
    if not cards:
        return None
    # MIGRATION NOTE (2026-06-18): the original reached `from .. import scanner` for an optional
    # card-CHAIN validator (scan_card_chains) — that module is the GPT-floor immune-layer code that
    # was NOT migrated into ECHELON-STRUCTURED (audited suspect; the real card-graph immune scan is
    # CardStore.scan(), already migrated). The trace-credit value of this module (compute_q / record_run /
    # record_chain_run) does NOT depend on the chain-order scan, so here it degrades to a clean "not
    # wired" marker instead of catching an ImportError on every call. If the chain-order check is wanted
    # later, point this at the migrated equivalent — don't resurrect the upward `from ..` reach.
    return {"fail": [], "warn": [], "stats": {"chain_scan": "not wired in echelon_engine yet"}}


def record_run(store: CardStore, goal: str, result,
               loaded_coords_per_step: list[list[str]],
               baseline_cost: float | None = None) -> dict[str, Any]:
    """Turn a finished run into action-cards and (on a completed run) rate them by the trace Q.
    Cards are ALWAYS created (the episode happened). Reinforcement only when status=='completed'
    AND Q != 50 (a neutral run moves nothing — honest). Returns a summary dict."""
    key = _goal_key(goal)
    cards: list[str] = []
    prev_id = ""   # the card→card edge source: the LAST card actually created (skips empty steps)
    for i, coords in enumerate(loaded_coords_per_step):
        if not coords:
            continue
        # WIRE THE CHAIN: this step's card chains FROM the previous step's card (prev edge). The chain
        # of step-cards is now a traversable reasoning path, not just sequential naming — so credit
        # flows back along it (chain rule) and `relive` is a real forward pass. the-card-layer-must-be-chained.
        cid = store.add_card(f"{key}:step{i}", coords, born_from="trace", prev=prev_id)
        _set_current_run_prev(store, cid, prev_id)
        cards.append(cid)
        prev_id = cid

    bad_steps = count_bad_steps(getattr(result, "transcript", None))
    cost = (getattr(result, "tokens_in", 0) or 0) + (getattr(result, "tokens_out", 0) or 0)

    # Resolve the baseline: explicit param wins; else the rolling mean for this goal; else None.
    baselines = _read_baselines()
    if baseline_cost is not None:
        baseline_used: float | None = baseline_cost
    else:
        prior = baselines.get(key, [])
        baseline_used = (sum(prior) / len(prior)) if prior else None

    q = compute_q(result, bad_steps, baseline_used)
    reinforced = False
    if getattr(result, "status", None) == "completed" and cards:
        if q != 50.0:
            # BACKPROP, not per-card spray: reinforce ONLY the chain TIP (the last card = the run's
            # outcome). reinforce_card then propagates the DECAYED delta back along the prev-edges to
            # every upstream step (chain rule). Reinforcing every card here AND letting each propagate
            # would credit ancestors many times over (dishonest double-count). The outcome enters at
            # the tip and flows back — exactly how error enters a network at the output. reinforce_card
            # ALSO bumps use_count, so this counts the run's use.
            store.reinforce_card(cards[-1], q, source="trace")   # explicit: Q from the execution trace
            reinforced = True
        else:
            # NEUTRAL / unverifiable run (audit #3): record the USE (use_count is the visible 'this ran'
            # record) WITHOUT moving the score — selection frequency is seen, never weight. This is the
            # other half of killing the self-amplifying script counterfeit. (register_use, not
            # reinforce_card, so no score delta and no chain propagation on a neutral run.)
            store.register_use(cards[-1])
        # Record this run's cost into the rolling baseline (completed runs only), capped to the
        # last BASELINE_WINDOW so the file stays bounded and the avg tracks CURRENT efficiency.
        if cost > 0:
            hist = baselines.setdefault(key, [])
            hist.append(float(cost))
            baselines[key] = hist[-BASELINE_WINDOW:]
            _write_baselines(baselines)

    chain_scan = _scan_current_run_cards(store, cards)

    return {"cards": cards, "reinforced": reinforced, "q": q,
            "baseline_used": baseline_used, "bad_steps": bad_steps,
            "chain_scan": chain_scan}


def _coords_from_step_record(step: Any) -> list[str]:
    if isinstance(step, dict):
        raw = (
            step.get("loaded_coords")
            or step.get("coords")
            or step.get("refs")
            or step.get("coordinates")
            or []
        )
    else:
        raw = (
            getattr(step, "loaded_coords", None)
            or getattr(step, "coords", None)
            or getattr(step, "refs", None)
            or getattr(step, "coordinates", None)
            or []
        )
    if isinstance(raw, (str, bytes)):
        raw = [raw]
    return [str(c) for c in raw if c]


def record_chain_run(store: CardStore, goal: str, result, step_records: list[Any],
                     baseline_cost: float | None = None) -> dict[str, Any]:
    """Record richer chain step records through the existing trace-card sink.

    This is an adapter, not a new scoring path: it extracts explicit coordinates
    from step records, then lets record_run create, chain, score, and immune-scan
    the cards using the same execution-trace Q rules as normal runs.
    """
    loaded_coords_per_step = [_coords_from_step_record(step) for step in step_records]
    return record_run(store, goal, result, loaded_coords_per_step, baseline_cost)
