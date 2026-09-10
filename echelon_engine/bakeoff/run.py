"""run — candidate generation. Phase 1 of the protocol.

Discipline enforced here, each because its absence invalidates the comparison:
  * SAME INPUT BYTES per item across every cell — one materialization, reused.
  * RANDOMIZED CELL ORDER per item — so cache warmth and backend drift do not ride one cell.
  * FRESH CONTEXT per candidate — every call is a new message list; nothing carries over.
  * ONE TRANSPORT — every call goes through the same provider method. v1 switched between
    argv and stdin by prompt length, making transport a hidden co-treatment.
  * NEVER RERUN A BAD ANSWER — only infrastructure failures retry, under one frozen policy.
    Re-rolling a weak answer is how a bakeoff quietly becomes best-of-N for one cell.
  * RECORD PROVIDER METADATA on every call — the returned model id is the ground truth for
    which backend actually answered.
"""
from __future__ import annotations

import json
import random
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .cells import CELLS, Cell, arbiter_brief
from .items import Item

RETRY_MAX = 2          # frozen policy: infrastructure only
RETRY_BACKOFF = 5.0    # seconds


@dataclass
class CallRecord:
    """One inference call. Everything needed to reprice or re-audit it later."""
    item_id: str
    cell: str
    role: str                     # candidate | worker | arbiter
    model_requested: str
    model_returned: str
    effort: str
    tokens_in: int
    tokens_out: int
    tokens_cached: int
    reasoning_chars: int
    latency_s: float
    status: str
    attempt: int
    utc: str
    content: str = ""
    error: str = ""


@dataclass
class ItemResult:
    item_id: str
    cell: str
    answer: str
    calls: list[CallRecord] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return bool(self.answer) and all(c.status == "success" for c in self.calls)

    def totals(self) -> dict[str, int]:
        return {
            "tokens_in": sum(c.tokens_in for c in self.calls),
            "tokens_out": sum(c.tokens_out for c in self.calls),
            "tokens_cached": sum(c.tokens_cached for c in self.calls),
            "calls": len(self.calls),
        }

    def latency(self) -> float:
        return sum(c.latency_s for c in self.calls)


def _send(provider, model: str, effort: str, prompt: str, item_id: str, cell: str,
          role: str, timeout: int = 300) -> CallRecord:
    """One call, fresh context, with the frozen retry policy applied to TRANSPORT failures."""
    last: CallRecord | None = None
    for attempt in range(1, RETRY_MAX + 1):
        t0 = time.monotonic()
        resp = provider.send(
            [{"role": "user", "content": prompt}],      # fresh context, always
            model_id=model,
            reasoning_effort=effort,
            timeout=timeout,
        )
        dt = time.monotonic() - t0
        rec = CallRecord(
            item_id=item_id, cell=cell, role=role,
            model_requested=model,
            model_returned=getattr(resp, "model_id", "") or "",
            effort=effort,
            tokens_in=getattr(resp, "tokens_in", 0) or 0,
            tokens_out=getattr(resp, "tokens_out", 0) or 0,
            tokens_cached=getattr(resp, "tokens_cached", 0) or 0,
            reasoning_chars=len(getattr(resp, "reasoning_content", "") or ""),
            latency_s=round(dt, 3),
            status=getattr(resp, "status", "error"),
            attempt=attempt,
            utc=datetime.now(timezone.utc).isoformat(),
            content=getattr(resp, "content", "") or "",
        )
        if rec.status == "success":
            return rec
        rec.error = rec.content[:400]
        last = rec
        # Retry TRANSPORT only. A model that answered badly is a RESULT, not a failure.
        if attempt < RETRY_MAX:
            time.sleep(RETRY_BACKOFF)
    return last  # type: ignore[return-value]


def run_cell(provider, cell: Cell, item: Item, *, rng: random.Random,
             timeout: int = 300) -> ItemResult:
    """Generate one cell's final answer for one item."""
    res = ItemResult(item_id=item.id, cell=cell.key, answer="")

    if not cell.is_swarm:
        rec = _send(provider, cell.model, cell.effort, item.prompt,
                    item.id, cell.key, "candidate", timeout)
        res.calls.append(rec)
        res.answer = rec.content if rec.status == "success" else ""
        return res

    # ── SWARM: N independent workers, then one blind arbiter ──────────────────
    drafts: list[str] = []
    for _ in range(cell.workers):
        rec = _send(provider, cell.worker_model, cell.worker_effort, item.prompt,
                    item.id, cell.key, "worker", timeout)
        res.calls.append(rec)
        if rec.status == "success" and rec.content.strip():
            drafts.append(rec.content)

    if not drafts:
        return res       # every worker failed; arbiter has nothing — record the hard fail

    # SHUFFLE so candidate order carries no signal, and the arbiter sees no labels at all.
    rng.shuffle(drafts)
    rec = _send(provider, cell.arbiter_model, cell.arbiter_effort,
                arbiter_brief(item.prompt, drafts), item.id, cell.key, "arbiter", timeout)
    res.calls.append(rec)
    res.answer = rec.content if rec.status == "success" else ""
    return res


def run_bakeoff(items: list[Item], *, provider=None, cells: list[str] | None = None,
                seed: int = 0, timeout: int = 300,
                on_event: Callable[[str, dict], None] | None = None) -> dict[str, Any]:
    """Phase 1 across the whole sealed set. Returns a raw result document."""
    if provider is None:
        from echelon_engine.atoms.providers.deepseek import DeepSeekProvider
        provider = DeepSeekProvider()

    keys = cells or list(CELLS)
    rng = random.Random(seed)
    results: list[ItemResult] = []

    for item in items:
        order = list(keys)
        rng.shuffle(order)          # per-item cell order: no cell owns the warm cache
        for k in order:
            cell = CELLS[k]
            r = run_cell(provider, cell, item, rng=rng, timeout=timeout)
            results.append(r)
            if on_event:
                on_event("candidate", {"item": item.id, "cell": k, "ok": r.ok,
                                       **r.totals()})

    return {
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "seed": seed,
        "cells": keys,
        "results": [
            {**asdict(r), "totals": r.totals(), "latency_s": round(r.latency(), 3)}
            for r in results
        ],
    }
