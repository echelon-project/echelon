"""plan — the PLAN SWARM: fan-out goal analysis with cache-optimized context.

Orchestrates a swarm of specialized agents that analyze a goal from multiple
angles simultaneously. Each agent gets the same frozen prefix (cartridge atoms +
output format — cached after the first call) and a lens-specific dynamic suffix.

LENSES (one agent per lens, running in parallel):
  architect   — system design, scalability, component decomposition
  security    — threat surface, data safety, auth, injection
  performance — bottlenecks, caching, resource limits, latency
  ux          — consumer experience, clarity, state handling
  ops         — deployability, observability, failure modes, rollback
  cost        — token spend, infrastructure cost, tier efficiency

After the swarm completes, the output includes SWARM-FOLLOW-UP instructions
telling the calling agent to run swarm --council and swarm --skeptic.

Usage:
  swarm plan "Build a rate limiter" --context "10K req/s, Redis available"
  swarm plan --goal "..." --files src/auth.py src/api.py
"""
from __future__ import annotations

import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path

from . import contract as _contract
from .context import SwarmContext


# ── The lenses ────────────────────────────────────────────────────────────────

LENSES = {
    "architect": {
        "cartridge": "architect",
        "prompt": "Analyze the GOAL from an ARCHITECTURE perspective. "
                  "Focus on: system design, component decomposition, data flow, "
                  "scaling strategy, integration points, technology choices, "
                  "and architectural risks. What are the load-bearing decisions?",
    },
    "security": {
        "cartridge": "architect",
        "prompt": "Analyze the GOAL from a SECURITY perspective. "
                  "Focus on: threat surface, authentication/authorization, "
                  "data sensitivity, injection risks, supply chain, "
                  "secrets management, and compliance concerns.",
    },
    "performance": {
        "cartridge": "architect",
        "prompt": "Analyze the GOAL from a PERFORMANCE perspective. "
                  "Focus on: bottlenecks, caching strategy, latency budgets, "
                  "throughput limits, resource utilization, database query "
                  "patterns, and cold-start costs.",
    },
    "ux": {
        "cartridge": "ux",
        "prompt": "Analyze the GOAL from a UX/CONSUMER perspective. "
                  "Focus on: who the consumer is, what they need to accomplish, "
                  "the state machine (loading/empty/error/edge), clarity of "
                  "information hierarchy, and the minimum viable surface.",
    },
    "ops": {
        "cartridge": "ops",
        "prompt": "Analyze the GOAL from an OPERATIONS perspective. "
                  "Focus on: deployability, observability (logs/metrics/alerts), "
                  "failure modes and recovery, rollback strategy, configuration "
                  "management, environment parity, and runbook requirements.",
    },
    "cost": {
        "cartridge": "architect",
        "prompt": "Analyze the GOAL from a COST perspective. "
                  "Focus on: token/model pricing tiers, infrastructure costs, "
                  "development time, maintenance burden, third-party service "
                  "costs, and cost optimization opportunities.",
    },
}


@dataclass
class LensResult:
    lens: str
    ok: bool
    findings: list[dict] = field(default_factory=list)
    recommendations: list[str] = field(default_factory=list)
    risks: list[str] = field(default_factory=list)
    narrative: str = ""
    raw: str = ""
    error: str = ""


# ── The orchestrator ──────────────────────────────────────────────────────────

def run_plan_swarm(
    goal: str,
    context: str = "",
    files: list[str] | None = None,
    lenses: list[str] | None = None,
    lens_defs: dict | None = None,
    provider: str = "auto",
    model: str = "",
    output_file: str = "",
    timeout: int = 300,
    effort: str = "",
    cartridges: list[str] | None = None,
) -> dict:
    """Run the plan swarm.

    Args:
        goal: The goal to analyze
        context: Additional context (constraints, existing infra, etc.)
        files: Paths to relevant files for context
        lenses: Which lenses to use (default: all)
        lens_defs: Optional per-lens definitions mapping name→{prompt, cartridge}.
                   When provided, merges over the hardcoded LENSES and becomes the
                   authoritative source for both lens selection and prompts.
                   Required for SwarmType lenses (e.g. --type audit).
        provider: LLM provider (auto = pick from routing)
        model: Model override
        output_file: Write the plan report to this file
        timeout: Per-lens timeout in seconds

    Returns:
        The complete plan report dict
    """
    # ── Build merged lens map: lens_defs overrides hardcoded LENSES ─────────
    if lens_defs:
        merged = dict(LENSES)  # hardcoded lenses as fallback
        for name, defn in lens_defs.items():
            merged[name] = defn  # lens_defs wins
        selected = [l for l in (lenses or list(merged)) if l in merged]
    else:
        merged = LENSES
        selected = [l for l in (lenses or LENSES) if l in LENSES]

    if not selected:
        selected = list(merged)

    print(f"⚡ ECHELON SWARM — plan")
    print(f"   goal: {goal[:100]}")
    print(f"   lenses: {', '.join(selected)}")
    print(f"   building frozen context...")

    # ── Build the cache-optimized context ──────────────────────────────────
    # The frozen block is byte-identical across all dispatches → cache hit.
    # An explicit --cartridge list REPLACES the default equip (architect+ops+ux);
    # the operator's choice of frame outranks the house default.
    if cartridges:
        ctx = SwarmContext(*cartridges)
    else:
        ctx = SwarmContext("architect")
        ctx.equip("ops", "ux")
    frozen = ctx.frozen_block()
    print(f"   frozen block: {len(frozen)} chars (hash={ctx.frozen_hash})")

    # ── Fan out: one worker per lens ───────────────────────────────────────
    results: dict[str, LensResult] = {}
    started = time.monotonic()

    def _worker(lens_name: str) -> LensResult:
        lens = merged[lens_name]
        dynamic = ctx.dynamic_block(
            goal=goal,
            context=context + f"\n\nLENS: {lens['prompt']}",
            files=files,
        )
        full_prompt = frozen + "\n\n" + dynamic

        try:
            raw = _send_to_provider(full_prompt, provider, model, timeout, effort,
                                    response_format=_contract.response_format())
            # KEY-BASED INGEST against contracts/lens_report.json — no pattern matching on
            # model prose. A missing field takes its declared default; a malformed one is
            # coerced or defaulted. Parse failure surfaces as `parse_error`, never as an
            # empty-but-successful result: that conflation is what let a dead lens read as
            # a clean bill of health.
            parsed = _contract.ingest(raw)
            # TWO different failures, and the reason must distinguish them or the reader
            # cannot decide whether a re-run would even help:
            #   parse_error  -> WE could not read the answer (retry, maybe another model)
            #   ok:false     -> the MODEL declared it could not do the lens (the goal/context
            #                   is the problem, not the transport)
            # Without this, a model-declared failure carried an EMPTY reason and the report
            # said only "no reason reported" — witnessed live 2026-08-18 on the style lens.
            perr = parsed.get("parse_error", "")
            declared_ok = bool(parsed.get("ok"))
            if perr:
                err = perr
            elif not declared_ok:
                err = ('the model returned ok=false — it declared it could NOT run this lens'
                       + (f' ({parsed["narrative"][:160]})' if parsed.get("narrative") else ''))
            else:
                err = ""
            return LensResult(
                lens=lens_name, ok=declared_ok and not perr,
                findings=parsed.get("findings", []),
                recommendations=parsed.get("recommendations", []),
                risks=parsed.get("risks", []),
                narrative=parsed.get("narrative", ""),
                raw=raw,
                error=err,
            )
        except Exception as e:
            return LensResult(lens=lens_name, ok=False, error=f"{type(e).__name__}: {e}")

    with ThreadPoolExecutor(max_workers=len(selected)) as pool:
        futures = {pool.submit(_worker, l): l for l in selected}
        for future in as_completed(futures):
            result = future.result()
            results[result.lens] = result
            # A FAILED lens and a CLEAN lens both used to print "(0 findings)" — the two
            # states are opposite in meaning and were indistinguishable on the console.
            if result.ok:
                print(f"   [✓] {result.lens} ({len(result.findings)} findings)")
            else:
                print(f"   [✗] {result.lens} DID NOT RUN — {result.error or 'no reason reported'}"
                      f"  (NOT 'found nothing')")

    elapsed = time.monotonic() - started

    # ── Merge + deduplicate ────────────────────────────────────────────────
    all_findings: list[dict] = []
    all_recommendations: list[str] = []
    all_risks: list[str] = []
    seen_titles: set[str] = set()

    for r in results.values():
        for f in r.findings:
            title = f.get("title", "").strip().lower()
            if title and title not in seen_titles:
                seen_titles.add(title)
                all_findings.append(f)
            elif not title:
                all_findings.append(f)
        all_recommendations.extend(r.recommendations)
        all_risks.extend(r.risks)

    # Sort by severity, then VERDICT (THE VERDICT LAW, 2026-08-18 — replaces the `confidence`
    # float, which was a feeling the worker assigned itself and nothing downstream could
    # check). Within one severity a witnessed finding outranks a hypothesis, so the reader
    # meets hard truth first and the PLAUSIBLE tail is where the gate spends re-derivation.
    severity_order = {"HIGH": 0, "MEDIUM": 1, "LOW": 2}
    verdict_order = {"CONFIRMED": 0, "PLAUSIBLE": 1}

    all_findings.sort(key=lambda f: (
        severity_order.get(f.get("severity", "LOW"), 2),
        # An unlabelled finding is a hypothesis — it must never sort as hard truth.
        verdict_order.get(f.get("verdict", "PLAUSIBLE"), 1),
    ))

    # ── Coverage: which lenses actually RAN ────────────────────────────────
    # The renderer has always looked for report["coverage"] to print its PARTIAL banner —
    # and NOTHING EVER WROTE IT (found 2026-08-18: consumer at swarm/__init__.py:451, zero
    # producers). So every partial audit this swarm has written silently claimed to be
    # complete: 4 of 6 lenses died in one run and the artifact said only "✗ (0 findings)".
    # A guard whose input is never populated is not a guard — it reads as coverage.
    failed = sorted(name for name, r in results.items() if not r.ok)
    coverage = {
        "lenses_requested": len(selected),
        "lenses_ok": len(selected) - len(failed),
        "lenses_failed": failed,
        "complete": not failed,
        # WHY each lens died — captured in LensResult.error all along and thrown away at the
        # report boundary. Without it a dead lens is indistinguishable from a clean one.
        "failures": {name: (results[name].error or "no reason reported") for name in failed},
    }

    # ── Build the plan report ──────────────────────────────────────────────
    report = {
        "goal": goal,
        "context": context,
        "lenses": selected,
        "coverage": coverage,
        "elapsed_secs": round(elapsed, 1),
        "cache_hash": ctx.frozen_hash,
        # A saved report must carry its own cost — otherwise a bakeoff comparing two runs
        # has the findings but not the price, which is how "which model?" became unanswerable.
        "tokenomics": _tokenomics_snapshot(),
        "findings": all_findings,
        "recommendations": _dedupe_ordered(all_recommendations),
        "risks": _dedupe_ordered(all_risks),
        "lens_details": {
            name: {
                "ok": r.ok,
                "findings_count": len(r.findings),
                "recommendations": r.recommendations,
                "risks": r.risks,
            }
            for name, r in results.items()
        },
        "swarm_follow_up": [
            "swarm --council --on <this plan>   (multi-model deliberation to critique)",
            "swarm --skeptic --on <this plan>   (red-team review to find flaws)",
        ],
    }

    # ── Write output ───────────────────────────────────────────────────────
    if output_file:
        _write_report(report, results, output_file)

    # ── Print summary ──────────────────────────────────────────────────────
    print(f"\n   {len(all_findings)} findings, {len(all_recommendations)} recommendations, "
          f"{len(all_risks)} risks in {elapsed:.1f}s")
    print(f"\n   SWARM FOLLOW-UP:")
    for step in report["swarm_follow_up"]:
        print(f"   → {step}")

    return report


def _tokenomics_snapshot() -> dict:
    """The meter's numbers as plain JSON for the saved report. `usd_estimated` marks a total
    that leaned on cost.py's _DEFAULT_USD guess for at least one unpriced model."""
    from .dispatch import METER
    usd, unpriced = METER.usd()
    return {
        "calls": METER.calls,
        "failures": METER.failures,
        "tokens_in": METER.tokens_in,
        "tokens_out": METER.tokens_out,
        "tokens_cached": METER.tokens_cached,
        "reasoning_chars": METER.reasoning_chars,
        "by_model": METER.by_model,
        "usd": usd,
        "usd_estimated": bool(unpriced),
        "unpriced_models": sorted(unpriced),
    }


# ── Response parsing ──────────────────────────────────────────────────────────

def _send_to_provider(prompt: str, provider: str, model: str,
                      timeout: int, effort: str = "",
                      response_format: dict | None = None) -> str:
    """Send a prompt to an LLM provider. Routes through the shared swarm dispatcher."""
    from .dispatch import send
    return send(prompt, provider=provider, model=model, timeout=timeout,
                effort=effort, response_format=response_format)


def _parse_response(raw: str) -> dict:
    """Extract the JSON block from a model response."""
    import re
    # Find ```json ... ``` block
    m = re.search(r"```json\s*\n(.*?)```", raw, re.S)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            pass
    # Try bare JSON
    m = re.search(r'\{[^{}]*"ok"\s*:\s*(true|false)[^{}]*\}', raw, re.S)
    if m:
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            pass
    return {"ok": False, "findings": [], "error": "could not parse response"}


def _extract_narrative(raw: str) -> str:
    """Extract the prose narrative after the JSON block."""
    import re
    # Remove the JSON block
    text = re.sub(r"```json.*?```", "", raw, flags=re.S)
    # Take the first meaningful paragraph after it
    paras = [p.strip() for p in text.split("\n\n") if len(p.strip()) > 40]
    return paras[0] if paras else ""


def _tokenomics_line(t: dict) -> str:
    """One markdown line so a SAVED report carries its own price. `~` marks a total that
    leaned on cost.py's default guess for an unpriced model."""
    if not t or not t.get("calls"):
        return "**Cost:** (no LLM calls recorded)"
    usd = t.get("usd")
    if usd is None:
        price = "$UNKNOWN"
    else:
        price = f"{'~' if t.get('usd_estimated') else ''}${usd:.4f}"
    models = ", ".join(sorted(t.get("by_model") or {})) or "?"
    return (f"**Cost:** {price} · {t['calls']} call(s) · "
            f"in {t.get('tokens_in', 0):,} (cached {t.get('tokens_cached', 0):,}) · "
            f"out {t.get('tokens_out', 0):,} · model(s): {models}")


def _dedupe_ordered(items: list[str]) -> list[str]:
    """Deduplicate while preserving order."""
    seen = set()
    result = []
    for item in items:
        key = item.strip().lower()
        if key and key not in seen:
            seen.add(key)
            result.append(item)
    return result


def _write_report(report: dict, results: dict[str, LensResult],
                  path: str) -> None:
    """Write the full plan report to a markdown file."""
    cov = report.get("coverage") or {}
    lines = [
        f"# ECHELON SWARM — Plan Report",
        f"",
        f"**Goal:** {report['goal']}",
        f"**Lenses:** {', '.join(report['lenses'])}",
        f"**Elapsed:** {report['elapsed_secs']}s",
        f"**Cache hash:** {report['cache_hash']}",
        _tokenomics_line(report.get("tokenomics") or {}),
    ]
    # The PARTIAL banner must live in the ARTIFACT — a saved report outlives the console, and
    # a file headed "Findings (7)" with 4 dead lenses is read later as a complete audit.
    # (Kept identical to swarm/__init__.py:_format_plan_md — two renderers that disagree are
    # how the same run tells two different stories.)
    if cov and not cov.get("complete", True):
        lines += [
            f"",
            f"> ⚠ **PARTIAL COVERAGE — {cov.get('lenses_ok')}/{cov.get('lenses_requested')} "
            f"lenses succeeded.** FAILED: {', '.join(cov.get('lenses_failed', []))}.",
            f"> Those angles were NOT audited; absence of findings there is not evidence "
            f"of absence. Re-run before treating this report as complete.",
        ]
        for name, why in (cov.get("failures") or {}).items():
            lines.append(f"> - `{name}`: {why}")
    elif cov:
        lines.append(f"**Coverage:** {cov.get('lenses_ok')}/{cov.get('lenses_requested')} lenses OK")
    lines += [
        f"",
        f"## Findings ({len(report['findings'])}"
        + ("" if not cov or cov.get("complete", True) else " — PARTIAL")
        + ")",
    ]
    for f in report["findings"]:
        sev = f.get("severity", "?")
        cat = f.get("category", "?")
        title = f.get("title", "?")
        # THE VERDICT LAW is only useful if the READER sees the tier — collapsing it here
        # would throw the mechanism away at the last step. An unlabelled finding renders as
        # PLAUSIBLE, never as hard truth.
        verdict = f.get("verdict") or "PLAUSIBLE"
        lines.append(f"- **[{verdict}] [{sev}] [{cat}]** {title}")
        if f.get("detail"):
            lines.append(f"  {f['detail']}")
        # Evidence is what makes a CONFIRMED checkable without trusting the worker, and what
        # names the settling command on a PLAUSIBLE. It was collected by the contract but
        # never rendered — so the reader could not check anything.
        if f.get("evidence"):
            lines.append(f"  ⟐ Evidence: {f['evidence']}")
        if f.get("fix"):
            lines.append(f"  → Fix: {f['fix']}")

    lines.append("")
    lines.append(f"## Recommendations")
    for r in report["recommendations"]:
        lines.append(f"- {r}")

    lines.append("")
    lines.append(f"## Risks")
    for r in report["risks"]:
        lines.append(f"- {r}")

    lines.append("")
    lines.append("## Lens Details")
    failures = (report.get("coverage") or {}).get("failures") or {}
    for name, detail in report["lens_details"].items():
        if detail["ok"]:
            lines.append(f"### {name} ✓ ({detail['findings_count']} findings)")
        else:
            # NEVER render a dead lens as "(0 findings)" — that reads as a clean bill of
            # health for an angle that was never audited.
            lines.append(f"### {name} ✗ DID NOT RUN — NOT audited")
            lines.append(f"- reason: {failures.get(name, 'no reason reported')}")
        for r in detail.get("recommendations", []):
            lines.append(f"- {r}")

    lines.append("")
    lines.append("## Swarm Follow-Up")
    for step in report["swarm_follow_up"]:
        lines.append(f"- `{step}`")

    Path(path).write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\n   plan written to {path}")
