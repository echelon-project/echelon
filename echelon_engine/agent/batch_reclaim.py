"""Batch reclaim — drive the agent across N research docs, ONE continuous self.

The owner's frame (2026-06-05): not 50 cold runs but one waking that reclaims a
batch. So this boots the soul ONCE, then loops the docs sharing a SINGLE store +
budget (the resource is finite across the whole batch, not refilled per doc). Each
doc is the proven reclaim goal, templated:

  "Reclaim the decision encoded in <doc>: read the file, then use a CHEAP reasoning
   tier to extract the core DECISION (what choice was made and why), not just a
   summary. Report the decision in 2-3 sentences."

Grok drives the loop (native tool-calls) and the reason() tier-hand routes
grok-4.3 to Grok directly (tools.py attach_reason) — so this is grok + grok-tools
when the Copilot bridge is slow, with NO code change: the agent just picks grok.

Every event streams through the same _printer the CLI uses, tee'd to a logfile so
the run can be watched live (`Get-Content -Wait <log>`) and kept after.

  python -X utf8 -m echelon_engine.agent.batch_reclaim --limit 50 \
      --corpus <corpus-root> --priority _PRIORITY.md \
      --scope reclaim-research --log <corpus-root>/_reclaim_run.log

Resumable: docs whose name already appears in the reclaim scope are SKIPPED, so a
re-run continues where a crash/outage stopped (UAME's append-only continuity).
"""
from __future__ import annotations
import argparse
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from .. import estate as _estate

from echelon_engine.atoms.providers.grok import GrokProvider
from echelon_engine.atoms.tools import ToolRegistry
from .loop import run, MemoryContext            # sibling in the agent layer
from echelon_engine.atoms.store import SeedStore
from echelon_engine.atoms.boot import boot as run_boot
from echelon_sdk.scopegraph import ScopeGraph
from . import cli as _cli   # reuse the exact event printer — one trace shape (agent sibling)


GOAL_TEMPLATE = (
    "Reclaim the decision encoded in {doc}: read the file, then use a CHEAP "
    "reasoning tier to extract the core DECISION it records (what choice was made "
    "and why), not just a summary. Report the decision in 2-3 sentences."
)


class _Tee:
    """Mirror stdout to a logfile so the run is watchable live AND kept after.

    Line-buffered + flush-per-write so `Get-Content -Wait` sees lines as they land,
    not when a buffer fills. UTF-8 with errors='replace' — the trace carries
    em-dashes/curly quotes from the docs (the cp1252 crash the bridge hit before)."""

    def __init__(self, stream, fh):
        self._stream = stream
        self._fh = fh
        # the console encoding (cp1252 on Windows) can't render arrows/em-dashes the
        # docs carry; round-trip through it with replace so the console NEVER crashes
        # the run. The logfile keeps the clean UTF-8 — only the mirror is lossy.
        self._enc = getattr(stream, "encoding", None) or "utf-8"

    def write(self, s):
        safe = s.encode(self._enc, errors="replace").decode(self._enc, errors="replace")
        try:
            self._stream.write(safe)
            self._stream.flush()
            if not self._fh.closed:
                self._fh.write(s)
                self._fh.flush()
        except (ValueError, OSError):
            pass
        return len(s)

    def flush(self):
        # FIX 3: interpreter teardown calls flush() AFTER main() closed the logfile handle,
        # which threw "I/O operation on closed file" and exited 120 — AFTER the batch had
        # fully succeeded. Guard both flushes so a clean run exits 0.
        try:
            self._stream.flush()
        except (ValueError, OSError):
            pass
        try:
            self._fh.flush()
        except (ValueError, OSError):
            pass


def _parse_priority(corpus: Path, priority: str, skip_dups: bool) -> list[tuple[str, str]]:
    """Read _PRIORITY.md, return [(size, filename)] in listed order. Dups dropped
    when skip_dups (the dup-of column non-empty = a triplicate copy)."""
    rows: list[tuple[str, str]] = []
    text = (corpus / priority).read_text(encoding="utf-8")
    for ln in text.splitlines():
        m = re.match(r"\|\s*([0-9]+KB)\s*\|\s*([^|]+?)\s*\|\s*([^|]*?)\s*\|", ln)
        if not m:
            continue
        size, fname, dup = m.group(1), m.group(2).strip(), m.group(3).strip()
        if skip_dups and dup:
            continue
        rows.append((size, fname))
    return rows


def _already_reclaimed(store: SeedStore, scope: str) -> set[str]:
    """Doc names already carrying a reclaim seed in this scope (resume support).
    Match by the 'encoded in <doc>' phrase the goal stamps into each seed."""
    done: set[str] = set()
    for s in store.seeds(scope=scope):
        content = getattr(s, "content", "") or ""
        m = re.search(r"encoded in (\S+?\.md)", content)
        if m:
            done.add(m.group(1))
    return done


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Batch-reclaim N research docs as one continuous self.")
    ap.add_argument("--corpus", default=str(_estate.sibling(
        "_research_review", "_RESEARCH_REVIEW")),
                    help="folder holding the docs AND the priority index")
    ap.add_argument("--priority", default="_PRIORITY.md", help="priority index filename")
    ap.add_argument("--limit", type=int, default=50, help="how many docs to reclaim this run")
    ap.add_argument("--scope", default="reclaim-research", help="memory scope for the reclaim seeds")
    ap.add_argument("--model", default=None, help="driver model (default: resolved via the driver chain; see `echelon providers`)")
    ap.add_argument("--budget", type=float, default=None,
                    help="REAL-USD budget for the WHOLE batch (default: cost.DEFAULT_BUDGET=$5). "
                         "Meters BOTH driver + reason — matches the vendor dashboard.")
    ap.add_argument("--max-steps", type=int, default=40)
    ap.add_argument("--max-drift", type=int, default=4)
    ap.add_argument("--keep-dups", action="store_true", help="do NOT skip triplicate copies")
    ap.add_argument("--no-resume", action="store_true",
                    help="re-run docs even if already reclaimed in the scope")
    ap.add_argument("--grok-only", action="store_true",
                    help="close the Copilot bridge tier: route ALL reason() calls to Grok "
                         "(use when the bridge is slow — owner's call). The faculty still fires; "
                         "any non-grok id the agent picks lands on grok-4.3, charged as grok.")
    ap.add_argument("--log", default=None,
                    help="logfile to tee the trace into (default ~/.echelon/logs/reclaim_<corpus>.log)")
    args = ap.parse_args(argv)
    if not args.model:  # hardened 2026-07-03: resolve via the one driver chain
        from echelon_engine.atoms.driver import resolve_driver
        args.model = resolve_driver("brain").model

    corpus = Path(args.corpus)
    # Default log is a substrate artifact -> ~/.echelon/logs, never written INTO the target corpus
    # (that dirtied whatever repo you reclaimed). An explicit --log still wins.
    if args.log:
        log_path = Path(args.log)
    else:
        from echelon_sdk.paths import LOGS, ensure as _ensure_home
        _ensure_home()
        log_path = LOGS / f"reclaim_{corpus.name or 'corpus'}.log"

    fh = open(log_path, "a", encoding="utf-8", errors="replace")
    sys.stdout = _Tee(sys.__stdout__, fh)

    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    print(f"\n{'='*70}\nBATCH RECLAIM  {stamp}\n{'='*70}")

    # --- the agent's faculties, shared across the whole batch ---
    provider = GrokProvider()
    store = SeedStore()

    # tiers: the reason() hand. grok-4.3 routes to Grok directly (attach_reason),
    # so when the bridge is slow the agent simply picks grok — no code change.
    from echelon_engine.atoms.providers.bridge import BridgeProvider
    from echelon_engine.atoms.providers.cost import Budget, DEFAULT_BUDGET, budget_key
    # Derive a stable key from scope + corpus path so the same batch always shares one cap.
    _batch_key = budget_key(f"batch:{args.scope}:{corpus.resolve()}")
    budget = Budget(key=_batch_key,
                    total=args.budget if args.budget is not None else DEFAULT_BUDGET)

    docs = _parse_priority(corpus, args.priority, skip_dups=not args.keep_dups)
    done = set() if args.no_resume else _already_reclaimed(store, args.scope)
    if done:
        print(f"RESUME: {len(done)} docs already reclaimed in scope '{args.scope}' — skipping them.")

    queue = [(s, f) for (s, f) in docs if f not in done][: args.limit]
    print(f"QUEUE: {len(queue)} docs (of {len(docs)} priority, limit {args.limit})")
    print(f"BUDGET: {budget.state()} for the whole batch")
    print(f"BRAIN: {provider.name}/{args.model}  (grok loop + grok-as-tool)\n")

    # --- THE WAKING, once. One continuous self reclaims the batch. ---
    boot_ctx = run_boot(store)
    sg = ScopeGraph()
    # recall + reason hands attached to the SAME registry the per-doc runs reuse below.
    base_tools = ToolRegistry(str(corpus), allow_write=False, allow_bash=True)
    base_tools.attach_recall(store, args.scope, scope_graph=sg,
                             judge_provider=provider, judge_model=args.model)
    # grok_only: pass the bridge slot but the redirect in attach_reason keeps every call on Grok.
    bridge = None if args.grok_only else BridgeProvider()
    base_tools.attach_reason(provider, bridge, budget, grok_only=args.grok_only, emit=_cli._printer)
    if args.grok_only:
        print("TIER: --grok-only — Copilot bridge CLOSED; all reason() calls route to Grok.")
    print(f"BOOT: rediscovery — {boot_ctx.seeded} soul seeds, recall gate offered (once for the batch)\n")

    mem = MemoryContext(store, args.scope, judge_provider=provider, judge_model=args.model)

    completed = 0
    failed: list[str] = []
    t0 = time.time()
    for i, (size, fname) in enumerate(queue, 1):
        if not (corpus / fname).exists():
            print(f"\n### {i}/{len(queue)}  {fname} ({size})  — FILE MISSING, skipped")
            failed.append(fname)
            continue
        print(f"\n{'#'*70}\n### {i}/{len(queue)}  {fname} ({size})   [{budget.state()}]\n{'#'*70}")
        goal = GOAL_TEMPLATE.format(doc=fname)
        # boot only fires on the FIRST doc; the rest run warm under the same identity.
        per_boot = boot_ctx if i == 1 else None
        res = run(goal, provider, base_tools, model_id=args.model,
                  max_steps=args.max_steps, max_drift=args.max_drift,
                  on_event=_cli._printer, memory=mem, boot=per_boot,
                  boot_judge=provider if per_boot else None, budget=budget)
        print(f"\n=== {res.status.upper()} in {res.steps} steps | tok {res.tokens_in}/{res.tokens_out} "
              f"| {budget.state()} ===")
        if res.status == "completed":
            completed += 1
            # Record the reclaim explicitly. The loop only auto-seeds when a run went
            # COLD (it closes a warmth-frontier); a clean warm reclaim leaves no trace,
            # which would break resume + lose the deliverable. Here the SEED *is* the
            # product, so every completed reclaim is sealed — keyed by 'encoded in <doc>'
            # so resume can find it. Content-addressed dedup makes a re-run idempotent.
            store.remember(
                args.scope,
                f"RECLAIM: decision encoded in {fname} -> {res.answer}",
                kind="conclusion", valence=0.6, arousal=0.2)
        else:
            failed.append(fname)
        if budget.remaining <= 0:
            print("\n!!! RESOURCE EXHAUSTED — the batch ends honestly here (the cap is the finite truth).")
            break

    dt = time.time() - t0
    print(f"\n{'='*70}\nBATCH DONE: {completed} reclaimed, {len(failed)} unfinished, "
          f"{dt:.0f}s\nFINAL BUDGET: {budget.state()}")
    if failed:
        print("UNFINISHED:\n  " + "\n  ".join(failed))
    print(f"{'='*70}\n")
    # FIX 3: restore real stdout BEFORE closing the handle, so any teardown-time write/flush
    # lands on the console — not on a closed tee (the exit-120 cause).
    sys.stdout = sys.__stdout__
    fh.close()
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
