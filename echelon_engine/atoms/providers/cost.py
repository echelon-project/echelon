"""Cost — the finite truth about resources. NOT a guard, NOT a behavioral rule.

Owner, 2026-06-05: "the cap is not rules for llms, but information that the resources are
capped at that. and there is nothing we can do." A real operating system does not COACH the
process to use less memory — it tells the TRUTH about how much exists, and when it is gone, it
is gone. The bare LLM has no idea resources are finite; the OS grants it that truth. This is a
faculty (honest information about a finite world), not a cage. The behavior — reach the cheap
tier — comes from the tiering SEED (warmth-guided), not from this meter. This meter only states:
here is the budget, here is what each call draws, here is what remains, and the work ends when
the resource is genuinely gone — flatly, the way a real system reports a full disk.

The cost-unit follows the live Copilot bridge cost table (the `In` column, per-model input
rate). A call's cost = In_rate * (input_tokens / 1_000_000) — the actual draw the table
describes. 150 units (the default budget) is finite and HONEST: a gpt-5-mini call (In=25) over
100K tokens draws 2.5; an Opus call (In=500) over 100K draws 50. Frontier calls visibly consume
the world; minis sip it. See compute-tiering-strategy, copilot-as-api-layered-worker.

PERSISTENCE (wave2/budget-persistence, 2026-07-30): Budget now supports an opt-in append-only
JSONL journal keyed by a stable budget key. When `key` is set, every charge appends one line to
the journal AND re-derives spent from the journal sum (single source of truth). Cross-process:
two processes sharing the same key file see each other's charges on the next journal read. A
bare Budget() with no key is unchanged pure-in-memory — the test-suite contract.
"""
from __future__ import annotations
import hashlib
import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

# Per-model INPUT rate from the live Copilot bridge cost table (the `In` column), 2026-06-05.
# Keyed by the EXACT bridge/model id the agent addresses. Grok included so every tier the agent
# can reach has a known draw. Unknown models fall back to a conservative default.
IN_RATE: dict[str, float] = {
    # Copilot bridge (exact ids from copilot_bridge_client.list_models)
    "gpt-5-mini":        25,
    "raptor-mini":       25,
    "mai-code-1-flash":  75,
    "gpt-5.4-mini":      75,
    "gpt-5.2":           175,
    "gpt-5.2-codex":     175,
    "gpt-5.3-codex":     175,
    "gpt-4o-mini":       25,
    "gemini-3.5-flash":  150,
    "gemini-3-flash":    50,
    "gemini-2.5-pro":    125,
    "gemini-3.1-pro-preview": 200,
    # Direct AI Studio free tier (GeminiProvider, .apikey GEMINI=...): $0 until the key is bound to
    # the paid/$300 project. Priced 0 so a free-tier draw doesn't hit the conservative default.
    "gemini-2.0-flash":  0,
    "gemini-1.5-flash":  0,
    "claude-haiku-4.5":  100,
    "claude-sonnet-4.6": 300,
    "claude-sonnet-4.5": 300,
    "claude-opus-4.7":   500,
    "claude-opus-4.8":   500,
    "gpt-5.4":           250,
    "gpt-5.5":           500,
    # Grok (the loop driver) — xAI grok-4.3 ~ $1.25/1M in; scaled to the same table unit (~125).
    "grok-4.3":          125,
    "grok-4":            125,
    # grok-build-0.1 — xAI's agentic-coding model ($1/1M in), fast (100+ tok/s). Scaled (~100).
    "grok-build-0.1":    100,
    # Local LM Studio (owner's own hardware, the configured floor host) — $0, the true T2/T3 floor.
    "smollm3-3b-gabliterated-i1":      0,
    "phi-3.5-mini-instruct_uncensored": 0,
    # MoE bake-off seats (2026-09-03, 16GB RX 6800 box): the modern local floor.
    "openai/gpt-oss-20b":              0,
    "qwen/qwen3-coder-30b":            0,
    "baidu/ernie-4.5-21b-a3b":         0,
    # DeepSeek (cheap-strong loop driver) — scaled to the table unit (~$/1M in * 100, miss rate).
    "deepseek-v4-pro":   44,    # $0.435/1M in (LIVE driver default)
    "deepseek-v4-flash": 14,    # $0.14/1M in (LIVE)
    "deepseek-chat":     27,    # DEPRECATED
    "deepseek-reasoner": 55,    # DEPRECATED
}
_DEFAULT_IN_RATE = 200.0   # conservative: an unknown model is assumed expensive, not free.

# --- REAL-DOLLAR pricing (FIX 1, owner 2026-06-05: "the budget meter should based on token API,
# matching real world right now"). The meter must equal the live vendor dashboard, not abstract
# bridge units. USD per 1M tokens, (input, output). The DRIVER (grok-4.3 loop) is the dominant
# spend and was previously unmetered — the batch reclaim drew ~$1.45 real while the unit-meter
# read 2.2/150. These rates make the gauge tell the dashboard's truth. Verify against live vendor
# pricing when it moves. Bridge/Copilot models priced from their published API equivalents; the
# bridge tier's true cost is Pro+ credits (separate pool) — these are the API-equivalent $ so a
# mixed run still sums to one honest dollar figure. Unknown model -> conservative default.
USD_PER_M: dict[str, tuple[float, float]] = {
    # xAI Grok — grok-4.3: ~$1.25/1M in (<=128k ctx), ~$10/1M out (2026-06-05).
    "grok-4.3":          (1.25, 10.0),
    "grok-4":            (1.25, 10.0),
    # grok-build-0.1 — agentic-coding model: $1/1M in, $2/1M out, 100+ tok/s (2026-06-05 launch).
    # Trained for tool-calling/MCP/web-dev + vision-capable; the cheap-fast tier for the loop+judges.
    "grok-build-0.1":    (1.0, 2.0),
    # Local LM Studio models (owner's own hardware via the configured floor host) — genuinely $0 in AND out.
    # These are the T2/T3 floor: a step that lands here draws nothing from the finite USD budget, so
    # the meter stays honest ($0 lines, not the _DEFAULT_USD mid-frontier assumption). (2026-06-08)
    "smollm3-3b-gabliterated-i1":      (0.0, 0.0),
    "phi-3.5-mini-instruct_uncensored": (0.0, 0.0),
    # MoE bake-off seats (2026-09-03) — owner's own box, genuinely $0.
    "openai/gpt-oss-20b":              (0.0, 0.0),
    "qwen/qwen3-coder-30b":            (0.0, 0.0),
    "baidu/ernie-4.5-21b-a3b":         (0.0, 0.0),
    # Gemini via Vertex Express (GeminiProvider, key GEMINI_API_VERTEX) — $0 in AND out while on the
    # $300 trial credit (project free-ai-499902). Move to published Vertex rates when credit runs out.
    "gemini-2.5-flash":      (0.0, 0.0),
    "gemini-2.5-pro":        (0.0, 0.0),
    "gemini-2.5-flash-lite": (0.0, 0.0),
    "gemini-2.0-flash":      (0.0, 0.0),
    "gemini-1.5-flash":      (0.0, 0.0),
    # DeepSeek — the cheap-strong loop driver. (input cache-MISS, output) per 1M.
    # PUBLISHED RATES 2026-08-17 (vendor Model Details page). These are the PEAK numbers;
    # off-peak is exactly HALF and is applied at call time by _deepseek_peak_factor(), so the
    # table stays a single source of truth. Peak = 01:00-04:00 and 06:00-10:00 UTC.
    # deepseek-v4-pro/-flash are the LIVE models; -chat/-reasoner are DEPRECATED ALIASES that
    # the API now resolves to v4-flash server-side, so they are priced AS v4-flash — pricing an
    # alias at its retired rate over-reported the swarm ~2-4x (caught when the swarm meter
    # first printed a number, 2026-08-17).
    "deepseek-v4-pro":   (1.32, 3.96),     # PEAK in-miss $1.32/M, out $3.96/M
    "deepseek-v4-flash": (0.44, 1.32),     # PEAK in-miss $0.44/M, out $1.32/M
    "deepseek-chat":     (0.44, 1.32),     # DEPRECATED -> server-side alias of v4-flash
    "deepseek-reasoner": (0.44, 1.32),     # DEPRECATED -> server-side alias of v4-flash
    # OpenAI-family (API-equivalent $/1M)
    "gpt-5-mini":        (0.25, 2.0),
    "gpt-4o-mini":       (0.15, 0.60),
    "gpt-5.4-mini":      (0.55, 4.4),
    "gpt-5.2":           (1.25, 10.0),
    "gpt-5.4":           (1.25, 10.0),
    "gpt-5.5":           (5.0, 15.0),
    # Anthropic
    "claude-haiku-4.5":  (1.0, 5.0),
    "claude-sonnet-4.6": (3.0, 15.0),
    "claude-sonnet-4.5": (3.0, 15.0),
    "claude-opus-4.7":   (15.0, 75.0),
    "claude-opus-4.8":   (15.0, 75.0),
    # Google
    "gemini-3.1-pro-preview": (1.25, 5.0),
    "gemini-2.5-pro":    (1.25, 5.0),
    "gemini-3-flash":    (0.30, 2.5),
    "gemini-3.5-flash":  (0.30, 2.5),
}
_DEFAULT_USD = (5.0, 15.0)   # conservative: an unknown model assumed mid-frontier, not free.

# CACHE-HIT input rate (owner 2026-06-07): a provider's automatic context cache bills repeated input
# (a stable transcript prefix) FAR cheaper than a fresh read. The meter previously billed ALL input at
# the MISS rate -> it over-reported ~5x and halted runs that had real headroom (a society run "blew" $5
# but truly spent ~$1.2 — the cache averted the disaster). USD/1M for a cache HIT, keyed by model. Only
# providers that REPORT a hit count (DeepSeek) use it; others see tokens_cached=0 -> all-miss (unchanged).
USD_PER_M_CACHED: dict[str, float] = {
    # DeepSeek context-cache HIT rates — PUBLISHED 2026-08-17, PEAK (off-peak is half, applied
    # by _deepseek_peak_factor). The hit/miss gap is ~30x, which is why a 6-lens swarm whose
    # input is ~96% cache hits costs a fraction of a cent.
    "deepseek-v4-pro":   0.044,     # vs $1.32 miss
    "deepseek-v4-flash": 0.014,     # vs $0.44 miss
    "deepseek-chat":     0.014,     # DEPRECATED -> alias of v4-flash
    "deepseek-reasoner": 0.014,     # DEPRECATED -> alias of v4-flash
}


def _deepseek_peak_factor(when=None) -> float:
    """1.0 during PEAK, 0.5 OFF-PEAK. DeepSeek halves every rate outside peak hours, so a
    meter that ignores the clock over-reports by up to 2x for most of the day.

    Vendor: peak = 01:00-04:00 and 06:00-10:00 UTC; all other hours are off-peak.
    """
    from datetime import datetime, timezone
    h = (when or datetime.now(timezone.utc)).hour
    return 1.0 if (1 <= h < 4 or 6 <= h < 10) else 0.5


def usd_cold(model_id: str, input_tokens: int, output_tokens: int = 0) -> float:
    """NORMALIZED COLD COST — every input token priced as a cache MISS, at PEAK list rates,
    ignoring the wall clock.

    Why this exists (bakeoff methodology): `usd_of` is deliberately reality-shaped — it honors
    cache hits and the peak/off-peak window, which is what you want for a budget brake. That
    makes it USELESS for comparing two configurations, because a cell that happened to run
    after the cache warmed, or at 11:00 UTC instead of 09:00, looks cheaper without being
    better. Repricing every response cold under one frozen schedule removes run-order and
    time-of-day from the comparison, so a cost delta means a real token delta.

    Report `usd_of` alongside it as the observed/billed figure — never instead of it.
    """
    pi, po = USD_PER_M.get(model_id, _DEFAULT_USD)
    return pi * (input_tokens / 1_000_000.0) + po * (output_tokens / 1_000_000.0)


def usd_of(model_id: str, input_tokens: int, output_tokens: int = 0, cached_tokens: int = 0) -> float:
    """Real USD a call costs, CACHE-AWARE: cached input bills at the hit rate, the rest at miss, plus
    output. cached_tokens (<= input_tokens) is the cache-HIT portion the provider reported; 0 = bill all
    input at miss (old behavior, correct when no cache info). The dashboard-true draw."""
    pi, po = USD_PER_M.get(model_id, _DEFAULT_USD)
    cached = max(0, min(cached_tokens, input_tokens))
    fresh = input_tokens - cached
    p_hit = USD_PER_M_CACHED.get(model_id, pi)   # if a model has no separate hit rate, hits cost the same
    # DeepSeek rates in the table are PEAK; off-peak is exactly half. Applying the clock here
    # keeps one rate table instead of two, and makes every caller's total time-correct.
    factor = _deepseek_peak_factor() if str(model_id).startswith("deepseek") else 1.0
    return factor * (p_hit * (cached / 1_000_000.0)
                     + pi * (fresh / 1_000_000.0)
                     + po * (output_tokens / 1_000_000.0))


# --- IMAGE generation: priced PER-IMAGE, not per-token (xAI dashboard, verified 2026-06-06).
# A token meter is blind to this tier; without it the gauge reads $0 on the priciest-shaped call
# (the "Budget Meter Must Track Real API" trap). Per-image USD, keyed by exact model id.
IMAGE_USD: dict[str, float] = {
    "grok-imagine-image-quality": 0.05,
    "grok-imagine-image":         0.02,
}
_DEFAULT_IMAGE_USD = 0.10   # conservative: an unknown image model assumed dearer, not free.


def usd_of_image(model_id: str, n: int = 1) -> float:
    """Real USD for an image-generation call: per-image rate * n."""
    return IMAGE_USD.get(model_id, _DEFAULT_IMAGE_USD) * max(1, n)

DEFAULT_BUDGET = 5.0       # USD. The finite truth (owner: "capped at that"), now in REAL dollars.


def cost_of(model_id: str, input_tokens: int) -> float:
    """Legacy bridge-UNIT draw (input only). Retained for the bridge tier's own accounting;
    the meter no longer uses this — it uses real USD (usd_of). See FIX 1."""
    rate = IN_RATE.get(model_id, _DEFAULT_IN_RATE)
    return rate * (input_tokens / 1_000_000.0)


def budget_key(goal: str) -> str:
    """Derive a stable budget key from a goal string (SHA256, first 16 hex chars)."""
    return hashlib.sha256(goal.encode("utf-8")).hexdigest()[:16]


def _budget_journal_dir() -> Path:
    """Resolve the budget journal directory.
    ECHELON_BUDGET_DIR > ECHELON_HOME/budget > ~/.echelon/budget."""
    if "ECHELON_BUDGET_DIR" in os.environ:
        return Path(os.environ["ECHELON_BUDGET_DIR"]).expanduser()
    if "ECHELON_HOME" in os.environ:
        return Path(os.environ["ECHELON_HOME"]).expanduser() / "budget"
    return Path.home() / ".echelon" / "budget"


def _sanitize_key(key: str) -> str:
    """Make a budget key filename-safe (alphanum, dots, dashes, underscores only)."""
    return "".join(c if c.isalnum() or c in "._-" else "_" for c in key)


@dataclass
class Budget:
    """The resource meter — now in REAL USD (FIX 1). States the finite truth; it does not
    moralize about it. The work ends when the resource is genuinely gone — not as punishment,
    as fact (like a full disk). Meters BOTH the reason() tier-hand AND the loop DRIVER (the
    dominant, previously-invisible spend), so the gauge equals the live vendor dashboard."""
    total: float = DEFAULT_BUDGET                     # USD
    spent: float = 0.0                                # USD
    calls: list[dict] = field(default_factory=list)   # per-call ledger, for the receipt
    driver_spent: float = 0.0                         # USD spent by the loop driver (split out)
    reason_spent: float = 0.0                         # USD spent by reason() tier-hands
    partner_consults: int = 0                         # consults answered by the Opus partner — $0 model cost, but REAL reasoning. Counted so the receipt never reads "no reasoning happened" when the partner did the thinking for free.
    key: str | None = field(default=None, compare=False, repr=False)  # persistence key; None = pure in-memory

    def __post_init__(self):
        self._journal_path: Path | None = None
        if self.key is not None:
            self._journal_path = _budget_journal_dir() / f"{_sanitize_key(self.key)}.jsonl"
            self._reload_from_journal()

    def _reload_from_journal(self) -> None:
        """Re-derive spent/driver_spent/reason_spent/calls from the journal (single source of truth)."""
        spent = 0.0
        driver = 0.0
        reason = 0.0
        calls: list[dict] = []
        if self._journal_path and self._journal_path.exists():
            try:
                with open(self._journal_path, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            rec = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        usd = rec.get("usd", 0.0)
                        spent += usd
                        if rec.get("kind") == "driver":
                            driver += usd
                        else:
                            reason += usd
                        # Normalize: ensure both 'usd' and 'cost' fields exist
                        # so by_tier() works regardless of record origin.
                        if "cost" not in rec:
                            rec["cost"] = usd
                        calls.append(rec)
            except FileNotFoundError:
                pass
        self.spent = spent
        self.driver_spent = driver
        self.reason_spent = reason
        self.calls = calls

    def _append_journal(self, rec: dict) -> None:
        """Append one charge line to the journal. O_APPEND via 'a' mode, flush to OS."""
        if not self._journal_path:
            return
        self._journal_path.parent.mkdir(parents=True, exist_ok=True)
        if "ts" not in rec:
            rec["ts"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        if "key" not in rec:
            rec["key"] = self.key
        line = json.dumps(rec, ensure_ascii=False) + "\n"
        with open(self._journal_path, "a", encoding="utf-8") as f:
            f.write(line)
            f.flush()
            os.fsync(f.fileno())

    def _write_and_reload(self, rec: dict) -> None:
        """Append one record to journal, then re-derive all state from the journal."""
        self._append_journal(rec)
        self._reload_from_journal()

    def note_partner_consult(self) -> None:
        """Record a partner-answered consult: zero USD, but a real reasoning event. Keeps the
        meter HONEST — 'reason $0.0000' alone would imply no judgment occurred when in fact the
        durable Opus peer answered it for free (the tokenomics inversion working, not absent)."""
        self.partner_consults += 1

    @property
    def remaining(self) -> float:
        """Finite USD remaining. When keyed, re-derives from journal for cross-process correctness.
        Staleness window: charges from other processes that land between our last charge() and this
        read are visible only on the NEXT remaining() call (or after the next charge())."""
        if self._journal_path is not None:
            self._reload_from_journal()
        return max(0.0, self.total - self.spent)

    @property
    def exhausted(self) -> bool:
        """True when the finite resource is genuinely gone. Journal-aware when keyed."""
        return self.remaining <= 0.0

    def would_exceed(self, model_id: str, input_tokens: int, output_tokens: int = 0) -> bool:
        """Truth-check BEFORE a call: would this draw take us past the finite edge? Real USD.
        Journal-aware when keyed: syncs from journal first for cross-process correctness."""
        if self._journal_path is not None:
            self._reload_from_journal()
        return (self.spent + usd_of(model_id, input_tokens, output_tokens)) > self.total

    def would_exceed_messages(self, model_id: str, messages: list[dict],
                              expected_output: int = 800) -> bool:
        """Pre-flight gate using the shared tokenizer: count the prompt tokens UP FRONT (no API
        round-trip) and check the projected draw against the finite edge. expected_output is a
        conservative reserve for the reply (the real output bills post-call via charge()). This is
        the foresight the meter lacked — before, would_exceed had no source for input_tokens; the
        tokenizer supplies it. Estimate, not billing truth (usage on the response stays the truth)."""
        from echelon_sdk.tokenizer import count_messages
        return self.would_exceed(model_id, count_messages(messages), expected_output)

    def charge(self, model_id: str, input_tokens: int, output_tokens: int = 0,
               *, kind: str = "reason", cached_tokens: int = 0, tier: str | None = None) -> float:
        """Record a call's REAL-USD draw, CACHE-AWARE. kind='reason' (tier-hand) or 'driver' (loop).
        cached_tokens = the cache-HIT portion of input (billed cheap); 0 = all-miss. Returns $ charged.

        tier (OS|T1|T2|T3) is the ECHELON tier the call ran AT — recorded so a per-tier accounting
        (by_tier) is real, not reconstructed. None = caller didn't tag it (legacy / a step that didn't
        flow through the tiered runner); such calls aggregate under 'untiered' so the books still balance.

        PERSISTENCE: when key is set, appends to the journal and re-derives spent from the journal
        sum (single source of truth). Cross-process: other processes' lines are picked up on reload."""
        c = usd_of(model_id, input_tokens, output_tokens, cached_tokens)
        rec: dict = {"model": model_id, "in": input_tokens, "out": output_tokens,
                     "kind": kind, "usd": round(c, 6)}
        if cached_tokens:
            rec["cached"] = cached_tokens
        if tier:
            rec["tier"] = tier

        if self._journal_path is not None:
            self._write_and_reload(rec)
        else:
            self.spent += c
            if kind == "driver":
                self.driver_spent += c
            else:
                self.reason_spent += c
            rec["cost"] = round(c, 6)
            rec["spent_after"] = round(self.spent, 6)
            self.calls.append(rec)
        return c

    def by_tier(self) -> dict:
        """Per-ECHELON-tier accounting from the call ledger: for each tier (OS/T1/T2/T3, + 'untiered'
        for untagged calls and an implicit 'T3-code' line for the $0 mechanical floor), the call count,
        tokens in/out, providers/models seen, and est USD. The honest answer to 'how much did each tier
        do + spend' — built ONLY from recorded facts (a call with no tier tag is 'untiered', never guessed)."""
        out: dict = {}
        for c in self.calls:
            t = c.get("tier") or "untiered"
            b = out.setdefault(t, {"calls": 0, "tokens_in": 0, "tokens_out": 0,
                                   "models": {}, "usd": 0.0})
            b["calls"] += 1
            b["tokens_in"] += c.get("in", 0)
            b["tokens_out"] += c.get("out", 0)
            b["usd"] += c.get("cost", 0.0)
            m = c.get("model", "?")
            b["models"][m] = b["models"].get(m, 0) + 1
        for b in out.values():
            b["usd"] = round(b["usd"], 6)
        return out

    def charge_driver(self, model_id: str, input_tokens: int, output_tokens: int = 0,
                      cached_tokens: int = 0, *, tier: str | None = None) -> float:
        """Charge a loop-driver step (the dominant spend FIX 1 makes visible), cache-aware.
        tier (OS|T1|T2|T3) is the ECHELON tier the call ran AT — threaded through so the
        full-agent-loop step's budget charge carries the tier tag for by_tier accounting."""
        return self.charge(model_id, input_tokens, output_tokens, kind="driver",
                           cached_tokens=cached_tokens, tier=tier)

    def charge_image(self, model_id: str, n: int = 1) -> float:
        """Record an image-generation draw (per-image, not per-token). Returns $ charged."""
        c = usd_of_image(model_id, n)
        rec: dict = {"model": model_id, "images": n, "kind": "image", "usd": round(c, 6)}
        if self._journal_path is not None:
            self._write_and_reload(rec)
        else:
            self.spent += c
            self.reason_spent += c   # image gen is a tier-hand draw, like reason()
            rec["cost"] = round(c, 6)
            rec["spent_after"] = round(self.spent, 6)
            self.calls.append(rec)
        return c

    def state(self) -> str:
        """The honest one-line statement of the finite world (real USD), for the agent + receipt."""
        partner = (f", partner {self.partner_consults} consult(s) @ $0"
                   if self.partner_consults else "")
        return (f"resources: ${self.remaining:.4f}/${self.total:.2f} remaining "
                f"(${self.spent:.4f} spent across {len(self.calls)} calls "
                f"— driver ${self.driver_spent:.4f}, reason ${self.reason_spent:.4f}{partner})")
