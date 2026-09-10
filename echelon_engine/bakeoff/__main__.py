"""echelon bakeoff — the CLI door.

    echelon bakeoff preflight                 the 10 checks; sealed set stays locked until pass
    echelon bakeoff validate --items FILE     schema + severity-distribution gate
    echelon bakeoff estimate --items FILE     cold-cost projection BEFORE spending anything
    echelon bakeoff run --items FILE --out D  Phase 1 candidate generation
    echelon bakeoff score --run FILE          Phase 2/3 grade, gate, compare, report

Exit codes are a contract here, because a bakeoff is run by scripts:
    0 = ok / unlocked / eligible configuration found
    1 = blocked, invalid, or no configuration cleared the gates
    2 = usage error
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path


def _cmd_preflight(a) -> int:
    from .preflight import run_preflight
    pf = run_preflight()
    print(pf.render())
    return 0 if pf.passed else 1


def _cmd_validate(a) -> int:
    from .items import load_items, seal_hash, total_weight, REQUIRED_COUNTS
    items, problems = load_items(a.items)
    if problems:
        print(f"SEALED SET REJECTED — {len(problems)} problem(s):")
        for p in problems:
            print(f"  ✗ {p}")
        return 1
    counts = {s: sum(1 for i in items if i.severity == s) for s in REQUIRED_COUNTS}
    print(f"SEALED SET OK — {len(items)} items, seal={seal_hash(items)}")
    print(f"  severity: {counts}  (weight sum {total_weight(items)})")
    print(f"  graders : {sorted({i.grader for i in items})}")
    return 0


def _cmd_estimate(a) -> int:
    """Project cold cost before spending. Uses the item prompts' real token counts."""
    from .items import load_items
    from .cells import CELLS
    from echelon_engine.atoms.providers.cost import usd_cold
    from echelon_sdk.tokenizer import count_tokens

    items, problems = load_items(a.items)
    if problems:
        print("cannot estimate — sealed set is invalid; run `validate` first")
        return 1

    in_tok = sum(count_tokens(i.prompt) for i in items)
    out_guess = {"low": 900, "high": 2200, "max": 4200}
    total = 0.0
    print(f"COLD-COST ESTIMATE — {len(items)} items, {in_tok:,} prompt tokens total")
    print("  (all input priced as cache MISS at PEAK list rates — the upper bound)")
    for k, c in CELLS.items():
        if c.is_swarm:
            w = c.workers * usd_cold(c.worker_model, in_tok,
                                     out_guess[c.worker_effort] * len(items))
            arb_in = in_tok + c.workers * out_guess[c.worker_effort] * len(items)
            arb = usd_cold(c.arbiter_model, arb_in, out_guess[c.arbiter_effort] * len(items))
            cost, calls = w + arb, len(items) * c.calls_per_item
        else:
            cost = usd_cold(c.model, in_tok, out_guess[c.effort] * len(items))
            calls = len(items) * c.calls_per_item
        total += cost
        print(f"  {k} {c.label:<12} {calls:>4} calls  ${cost:.4f}")
    print(f"\n  TOTAL ${total:.4f} (off-peak ~${total / 2:.4f}); judge cost is separate")
    return 0


def _cmd_run(a) -> int:
    from .items import load_items, seal_hash
    from .preflight import run_preflight
    from .run import run_bakeoff

    items, problems = load_items(a.items)
    if problems:
        print("REFUSING TO RUN — sealed set invalid:")
        for p in problems:
            print(f"  ✗ {p}")
        return 1

    pf = run_preflight()
    if not pf.passed and not a.force:
        print(pf.render())
        print("\nREFUSING TO RUN — preflight blocked (--force to override, and say why).")
        return 1

    seal = seal_hash(items)
    print(f"BAKEOFF — {len(items)} items · seal={seal} · cells={a.cells or 'ABCDE'}")

    def ev(kind, d):
        print(f"   [{d['cell']}] {d['item']:<14} "
              f"{'ok' if d['ok'] else 'FAIL':<5} "
              f"in {d['tokens_in']:>6,} out {d['tokens_out']:>6,}")

    doc = run_bakeoff(items, cells=list(a.cells) if a.cells else None,
                      seed=a.seed, timeout=a.timeout, on_event=ev)
    doc["seal"] = seal
    doc["preflight"] = {"passed": pf.passed,
                        "checks": [c.__dict__ for c in pf.checks], "env": pf.env}

    out = Path(a.out or f"bakeoff-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}.json")
    out.write_text(json.dumps(doc, indent=1, ensure_ascii=False), encoding="utf-8")
    print(f"\n   raw results -> {out}")
    return 0


def _cmd_stream(a) -> int:
    """Information-gated run: screen cheap, climb only on failure, retire settled questions."""
    from .items import load_items, seal_hash
    from .preflight import run_preflight
    from .stream import run_stream, render
    from .cells import CELLS

    items, problems = load_items(a.items)
    if problems:
        print("REFUSING TO RUN — sealed set invalid:")
        for p in problems:
            print(f"  ✗ {p}")
        return 1

    pf = run_preflight()
    if not pf.passed and not a.force:
        print(pf.render())
        return 1

    baseline = len(items) * sum(c.calls_per_item for c in CELLS.values())
    print(f"STREAM — {len(items)} items · seal={seal_hash(items)} · "
          f"full-sweep would cost {baseline} calls")
    print()

    def ev(kind, d):
        if kind == "screen":
            v = d["verdict"]
            print(f"   {d['item']:<14} [{d['sev']}] {d['cell']} score={d['score']} · {v}")
        elif kind == "climb":
            print(f"        └ climb {d['cell']} score={d['score']}")
        elif kind == "stop":
            print(f"   ⏹ RETIRED {d['comparison']}: {d['state']} — {d['reason']}")
        elif kind == "halt":
            print(f"   ⏹ HALT: {d['reason']}")
        elif kind == "budget":
            print(f"   ⏹ call budget reached ({d['calls']}/{d['cap']})")

    led = run_stream(items, seed=a.seed, timeout=a.timeout,
                     max_calls=a.max_calls or None, on_event=ev)
    print()
    print(render(led, items, baseline))
    return 0


def _cmd_score(a) -> int:
    from .items import load_items
    from .graders import grade
    from .score import CellScore, paired_bootstrap, decide, swarm_verdict
    from .cells import CELLS, COMPARISONS
    from echelon_engine.atoms.providers.cost import usd_cold

    doc = json.loads(Path(a.run).read_text(encoding="utf-8"))
    items, problems = load_items(a.items)
    if problems:
        print("sealed set invalid — cannot score")
        return 1
    by_id = {i.id: i for i in items}

    scores: dict[str, dict[str, int]] = {}
    costs: dict[str, float] = {}
    pending_rubric: list[tuple[str, str]] = []

    judge_tokens = {"in": 0, "out": 0}
    escalations: list[str] = []

    for r in doc["results"]:
        cell, iid = r["cell"], r["item_id"]
        it = by_id.get(iid)
        if it is None:
            continue
        s, why = grade(it, r.get("answer", ""))
        if s < 0:
            if a.judges:
                # BLIND rubric grading: two judges outside the candidate family.
                from .judges import grade_rubric
                rr = grade_rubric(it, cell, r.get("answer", ""))
                s = rr.score
                judge_tokens["in"] += sum(v.tokens_in for v in rr.votes)
                judge_tokens["out"] += sum(v.tokens_out for v in rr.votes)
                if rr.needs_human:
                    escalations.append(f"{cell}/{iid} votes={rr.scores}")
            else:
                pending_rubric.append((cell, iid))
                s = 0        # unscored until blind graders return; never silently a pass
        scores.setdefault(cell, {})[iid] = s
        t = r.get("totals", {})
        costs[cell] = costs.get(cell, 0.0) + usd_cold(
            CELLS[cell].model or CELLS[cell].arbiter_model,
            t.get("tokens_in", 0), t.get("tokens_out", 0))

    scored = {c: CellScore(c, sc, by_id) for c, sc in scores.items()}

    print(f"\n{'Cell':<14}{'S4':>6}{'S3':>6}{'Raw':>8}{'Q':>8}{'L':>8}"
          f"{'Cold $':>10}{'Elig':>7}")
    print("-" * 67)
    for k in sorted(scored):
        s = scored[k]
        print(f"{k} {CELLS[k].label:<12}"
              f"{s.passes('S4')}/{s.count('S4'):<4}"
              f"{s.passes('S3')}/{s.count('S3'):<4}"
              f"{s.raw_pass:>7.1%}{s.Q:>8.3f}{s.L:>8.3f}"
              f"{costs.get(k, 0):>10.4f}{'yes' if s.eligible else 'NO':>7}")

    print("\nPREDECLARED COMPARISONS (paired bootstrap over items, 95% CI):")
    for hi, lo, label in COMPARISONS:
        if hi in scored and lo in scored:
            d, l95, h95 = paired_bootstrap(scored[hi], scored[lo], seed=a.seed)
            sig = "" if l95 <= 0 <= h95 else "  *"
            print(f"  {hi}-{lo}  ΔQ {d:+.4f}  [{l95:+.4f}, {h95:+.4f}]  {label}{sig}")

    d = decide(scored, costs)
    print(f"\nDECISION: recommended = {d['recommended'] or 'NONE — no cell cleared the gates'}")
    if d["removed_by_gate"]:
        for k, why in d["removed_by_gate"].items():
            print(f"  gated out: {k} ({why})")
    if d["dominated"]:
        for k, why in d["dominated"].items():
            print(f"  {k}: {why}")

    # TIER MAP — the routing answer, not the winner answer. Printed before the swarm verdict
    # because once tiers exist the swarm is one RUNG, not a challenger.
    if a.tiers:
        from .tiers import tier_map, render as render_tiers
        tm = tier_map(by_id, scores, costs, by_family=not a.severity_only)
        print()
        print(render_tiers(tm, costs))

    sv = swarm_verdict(scored, costs, seed=a.seed)
    if sv.get("verdict") != "not run":
        print(f"\nSWARM: earns deployment = {sv['earns_deployment']} · "
              f"ΔQ {sv['delta_Q']:+.4f} {sv['ci95']} · "
              f"+${sv['incremental_usd']:.4f}"
              + (f" · ${sv['usd_per_Q_point']}/Q-point" if sv['usd_per_Q_point'] else ""))
        if sv["fixed_high_severity"]:
            print(f"  fixed  S4/S3: {sv['fixed_high_severity']}")
        if sv["broke_high_severity"]:
            print(f"  BROKE  S4/S3: {sv['broke_high_severity']}")

    if pending_rubric:
        print(f"\n⚠ {len(pending_rubric)} rubric item(s) scored 0 pending BLIND grading "
              f"— rerun with --judges (Gemini + Claude). The table above is INCOMPLETE.")
    if judge_tokens["in"]:
        # EVALUATION OVERHEAD — deliberately NOT added to any cell's cost, or a cell would
        # look expensive because of how it was graded rather than what it spent.
        print(f"\njudge overhead (excluded from cell cost): "
              f"in {judge_tokens['in']:,} out {judge_tokens['out']:,}")
    if escalations:
        print(f"⚠ {len(escalations)} judge disagreement(s) >1 point — held at the "
              f"conservative score, NOT averaged; needs human adjudication:")
        for e in escalations:
            print(f"    {e}")
    print("\nNOTE: an 8/8 S4 result means zero critical failures OBSERVED on this benchmark."
          "\n      It is not evidence of a zero production critical-error rate.")
    return 0 if d["recommended"] else 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="echelon bakeoff",
                                 description="paired, sealed, severity-gated model comparison")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("preflight", help="the 10 pre-run checks")

    v = sub.add_parser("validate", help="schema + severity gate on the sealed set")
    v.add_argument("--items", required=True)

    e = sub.add_parser("estimate", help="cold-cost projection before spending")
    e.add_argument("--items", required=True)

    r = sub.add_parser("run", help="Phase 1 candidate generation")
    r.add_argument("--items", required=True)
    r.add_argument("--out", default="")
    r.add_argument("--cells", default="", help="subset, e.g. ABD")
    r.add_argument("--seed", type=int, default=0)
    r.add_argument("--timeout", type=int, default=300)
    r.add_argument("--force", action="store_true", help="run despite a blocked preflight")

    st = sub.add_parser("stream", help="information-gated run: screen cheap, climb on "
                                       "failure, retire settled comparisons (fact-finding)")
    st.add_argument("--items", required=True)
    st.add_argument("--seed", type=int, default=0)
    st.add_argument("--timeout", type=int, default=300)
    st.add_argument("--max-calls", type=int, default=0, help="hard spend cap")
    st.add_argument("--force", action="store_true")

    s = sub.add_parser("score", help="Phase 2/3 grade, gate, compare")
    s.add_argument("--run", required=True)
    s.add_argument("--items", required=True)
    s.add_argument("--seed", type=int, default=0)
    s.add_argument("--judges", action="store_true",
                   help="blind-grade rubric items with Gemini + Claude (costs money; "
                        "counted as evaluation overhead, never as cell cost)")
    s.add_argument("--tiers", action="store_true",
                   help="emit the TIER MAP: cheapest sufficient cell per severity/family "
                        "bucket — the routing table, not a single winner")
    s.add_argument("--severity-only", action="store_true",
                   help="bucket by severity alone (bigger buckets, stronger evidence, "
                        "coarser routing) instead of severity x family")

    a = ap.parse_args(argv)
    return {"preflight": _cmd_preflight, "validate": _cmd_validate,
            "estimate": _cmd_estimate, "run": _cmd_run, "score": _cmd_score,
            "stream": _cmd_stream}[a.cmd](a)


if __name__ == "__main__":
    raise SystemExit(main())
