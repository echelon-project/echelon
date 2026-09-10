#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
warmth_update (EXPERIMENTAL) — let the local LLM (gemma/T1) re-score a scope's
memory atoms on the ACTIVE-NOW axis, so the SessionStart hook's "top N by warmth"
becomes a meaningful active-vs-dormant index instead of a flat-100 / alphabetical
list.

THE AXIS (active-now, load-bearing for live prod):
  100 = a fact still IN FORCE / needed for current work (settled laws, live traps,
        the newest session ledger, how-to-work playbooks).
    0 = finished / superseded / purely historical (old closed session ledgers,
        reverted math, resolved audits, architecture backstory).
Session ledgers DECAY as they age; evergreen laws + live traps stay hot. This is
exactly the active/dormant split the bank-as-index design needs.

HOW: load all atoms for --scope, ask gemma for a 0-100 score per atom (batched),
ITERATE --iters times (re-score; converge by averaging across passes to damp the
local model's noise), write the converged score back to core_<scope>.score.

The SessionStart hook (echelon_warmup._ground_index) already sorts by score desc,
so after this runs the active index reflects gemma's judgment. Dormant atoms (low
score) drop off the printed list but stay fully queryable via recall.py.

Run:  python -X utf8 -m echelon_engine warmth --scope mol --iters 3
      python -X utf8 -m echelon_engine warmth --scope mol --dry-run
"""
import argparse
import json
import re
import sys
import time

from .store import SeedStore
# WELD #1 CUT: _chat + T1_ARCHITECT used to come from the 1436-line add_steering god-module
# (dragging the whole T2/T3 ADD stack into the memory layer). They now live in the clean provider
# primitive echelon_engine.atoms.providers.floor_chat — stdlib + sdk only. See floor_chat.py.
from .providers.floor_chat import _chat, T1_ARCHITECT
from echelon_sdk import lms_balancer as bal   # lms_balancer is a pure leaf — migrated to sdk
# Rod-digest leaf: extracted to rod_digest.py (pure, zero-model, stdlib-only atom).
# Re-imported here so callers of warmth_update can still reach rod_digest directly.
from .rod_digest import (  # noqa: F401  (public re-export)
    rod_digest,
    _RE_DATE,
    _RE_SUPERSEDE,
    _RE_ARCHIVE,
    _RE_LIVE,
    _RE_LEDGER,
)

# Default scoring model. Qwen3.5-9B (Claude-4.6-OS-Auto-Var) — a stronger judge than
# the gemma-4B floor for the active-now axis. It's a THINKING model, so its replies
# carry <think>...</think> blocks; _score_batch strips them before JSON parse. The
# standard LAN _chat path does NOT auto-strip think (only the eos/burst path does).
WARMTH_MODEL = "qwen3.5-9b-claude-4.6-os-auto-variable-heretic-uncensored-thinking-max-neocode-imatrix"
_THINK_RE = re.compile(r"<think>.*?</think>\s*", re.DOTALL)

# The ENSEMBLE panel — a diverse jury of judges. A single small model is a noisy
# rater; averaging across DIFFERENT architectures cancels each one's bias (gemma
# anchors high, the qwen3.5 spreads wide, the polaris-distill lands differently).
# llama was on the jury but proved a consensus-wrecking outlier (dropped 2026-06-09).
# Only capable instruct/reasoning models — NOT coders or the 1.5b
# (weak judges) or the embedding model (can't chat). Models JIT-load one at a time
# (single-GPU), so the panel runs serially. Override with --models a,b,c.
WARMTH_PANEL = [
    "qwen3.5-9b-claude-4.6-os-auto-variable-heretic-uncensored-thinking-max-neocode-imatrix",
    "gemma-4-e4b-uncensored-hauhaucs-aggressive",
    "qwen2.5-14b-instruct",
    "qwen3-4b-instruct-2507-polaris-alpha-distill-heretic-abliterated-i1",
    # llama-3.2-3b DROPPED 2026-06-09 (owner): a confirmed OUTLIER that skews the
    # consensus (same reason it was pulled from COUNCIL_POOL). It also fails to load
    # here (V Cache Quantization requires flash attention) — so the prior "harmless,
    # balancer skips it" note understated the risk: if the flash-attn config is ever
    # fixed it would re-activate and skew scores. Removed outright rather than left as
    # a latent re-activating outlier. Re-add only if it's proven NOT an outlier.
]

import datetime as _dt
_TODAY = _dt.date.today().isoformat()

# Estate context fed to gemma BEFORE it scores — so "active-now" is judged against
# the ACTUAL live stack and what has shipped/superseded, not cold from the slug
# alone. Keep this current when the estate's live state changes (it's the ground
# truth the warmth axis is measured against). Passed per --scope; 'mol' below.
_ESTATE_CONTEXT = {
    "mol": (
        "ESTATE = the Mol vehicle-lending stack, 3 repos: Mol-Data-Engine (DE, "
        "pricing + vehicle SoT), Mol-Preliminary (submissions + sheet sync + "
        "dashboard), Mol-MRP (apply funnel/frontend). LIVE STATE (as of today): the "
        "CUTOVER IS DONE — PROD is the NEW stack (DE :9300 / MRP :9302 / Prelim "
        ":9304) on DB 'molai'; legacy MRP-dih :9003 is ARCHIVED; staging is "
        "molai_stg (:9301/:9303/:9401). SETTLED LAWS STILL IN FORCE: principal = "
        "effective LTV (mfa_range_low), NOT OTR×base-LTV (this REVERSED an older "
        "decision — any atom still saying OTR×base-LTV is superseded); the "
        "depreciating-insurance + disbursed=principal−Total Biaya formulas; the "
        "scanner ruleset (validate Prelim/DE with the BOOT scanner before deploy); "
        "MAiSheet 2.4 (get_headers ignores header_row → use get_range; TWO "
        "endpoints = TWO workbooks). WHAT'S NOW HISTORICAL: anything describing the "
        "PRE-cutover world (a separate -prod artifact, legacy :9003 as prod, the "
        "destroy-and-rebuild, pre-cutover deploy debt), one-off audits that have "
        "since been fixed, and superseded pricing math. The NEWEST session ledger "
        "describes today's live work and ranks high; every OLDER dated ledger "
        "decays by age. Judge each atom against THIS reality."
    ),
}

SYSTEM = (
    f"Today is {_TODAY}. You score software-project MEMORY atoms on ONE axis: "
    "ACTIVE-NOW = how load-bearing this fact is for ongoing work on the LIVE "
    "production stack right now. You MUST USE THE FULL 0-100 RANGE and SPREAD the "
    "scores — do NOT cluster everything near 100.\n"
    "ANCHORS:\n"
    "  90-100: evergreen + still in force — settled formulas/laws still applied, "
    "live traps/gotchas, how-to-work playbooks, the SINGLE newest session ledger "
    "describing the CURRENT live state.\n"
    "  60-85: useful background still true (architecture, ownership, contracts) "
    "but not something you act on every task.\n"
    "  25-55: a dated session ledger from a PAST session whose work has shipped "
    "and been superseded by a newer ledger — keep but demote by age.\n"
    "  0-20: finished/obsolete — reverted math, a resolved one-off audit, a "
    "pre-cutover history note, anything explicitly marked SUPERSEDED.\n"
    "RULE: an atom whose slug contains an OLDER date than another session ledger "
    "scores LOWER than the newer one. The newest session ledger is the only one "
    "near the top; every older dated ledger decays with age.\n"
    "Some atoms carry <<rod-signals: ...>> — cheap auto-extracted markers to GUIDE "
    "you (the atom text is still the truth; signals are hints):\n"
    "  • 'dated-session-ledger' + an OLD date + NOT the newest -> a past shipped "
    "session -> score LOW (25-55), lower the older it is.\n"
    "  • 'mentions-archive/destroy' -> this describes ARCHIVED/DESTROYED/pre-cutover "
    "things -> usually DORMANT history -> score LOW (0-30) UNLESS it's also a "
    "still-applied rule.\n"
    "  • 'mentions-supersede/revert' -> if the atom IS the superseded thing, score "
    "LOW; if it is the NEW rule that reverses an old one, score HIGH.\n"
    "  • 'mentions-still-in-force' / no date (evergreen law/playbook/trap) -> HIGH.\n"
    "Output ONLY a JSON object mapping each atom index to an integer 0-100. No prose."
)


_DIGEST_ON = False   # toggled by --digest: append rod-extracted features to each atom


def _score_batch(atoms_slice, start_idx, system, model):
    """Ask the model for {local_index: score}. Returns {global_index: int}. When
    _DIGEST_ON, each atom is ENRICHED with the rod_digest() features (the corrected
    council: cheap rods extract, the cone reads the pre-digested atom)."""
    lines = []
    for j, s in enumerate(atoms_slice):
        c = s.content.strip().replace("\n", " ")
        entry = f"[{j}] {c[:280]}"
        if _DIGEST_ON:
            entry += f"   <<rod-signals: {rod_digest(s.content)}>>"
        lines.append(entry)
    user = (
        "Score each atom 0-100 on ACTIVE-NOW. Reply with ONLY a JSON object like "
        '{"0": 90, "1": 10, ...}.\n\n' + "\n".join(lines)
    )
    raw = _chat(model, system, user, max_tokens=2000, temperature=0.0)
    raw = _THINK_RE.sub("", raw)  # thinking models emit <think>…</think>; drop it before JSON parse
    m = re.search(r"\{[^{}]*\}", raw, re.DOTALL) or re.search(r"\{.*\}", raw, re.DOTALL)
    if not m:
        return {}
    try:
        obj = json.loads(m.group(0))
    except Exception:
        return {}
    out = {}
    for k, v in obj.items():
        try:
            li = int(str(k).strip())
            sc = max(0, min(100, int(float(v))))
            out[start_idx + li] = sc
        except Exception:
            continue
    return out


# ---- COUNCIL: a society of co-loaded SMALL models reasons COLLECTIVELY ----
# The insight (owner, 2026-06-09): don't make ONE big model reason — co-load several
# SMALL models (the balancer proved >1 fits) and have OUR SYSTEM route context so they
# reason TOGETHER. Empirically grounded: a single 0.8B can't hold the fuzzy whole
# ("active-now 0-100") — it scored a superseded atom 100 — but on NARROW yes/no
# sub-questions it's mostly right, and its errors are RANDOM per-question, not
# systematic. So: (1) DECOMPOSE the judgment into narrow classifications a tiny model
# CAN do; (2) co-load a POOL of tiny models; (3) MAJORITY-VOTE each sub-question across
# the pool (redundancy cancels the random error); (4) deterministic FUSION of votes ->
# score. The reasoning lives in the decomposition + voting + fusion (our system), not
# in any one small model. Capability from orchestration, not size.
COUNCIL_POOL = [
    "qwen3.5-0.8b",
    "qwen2.5-1.5b-instruct",
    "smollm3-3b-gabliterated-i1",
    # llama-3.2-3b DROPPED 2026-06-09: confirmed outlier across runs (it "wrecks the
    # consensus" — see notes ~L508/L543) AND fails to load on this box (V Cache
    # Quantization requires flash attention). Replaced with gemma-4-e4b to KEEP family
    # diversity (Qwen + SmolLM + Gemma = 3 architectures, so bias still cancels);
    # gemma is already the trusted CASCADE_FAST triage model.
    "gemma-4-e4b-uncensored-hauhaucs-aggressive",
]
# Narrow sub-questions (each a yes/no a tiny model handles). The fusion weights below
# turn the vote-fractions into a 0-100 active-now score.
COUNCIL_QUESTIONS = {
    "superseded": "Does this note say it is superseded, reverted, obsolete, replaced, "
                  "archived, or pre-cutover history? Answer ONLY yes or no.",
    "live_now":   "Does this describe the CURRENT live production stack, or a rule / "
                  "formula / contract that is STILL applied today? Answer ONLY yes or no.",
    "actionable": "Would you need this fact to do ongoing work right now (a how-to, a "
                  "live trap, an open debt), as opposed to finished background? "
                  "Answer ONLY yes or no.",
}


def _ask_yesno(model, question, atom_text, ctx):
    # A failed vote returns None (doesn't count) — that IS the redundancy design: one
    # rod failing never stops the council; the other models still vote. Never fatal.
    sysmsg = (ctx + "\n\n" if ctx else "") + question
    try:
        r = _chat(model, sysmsg, atom_text[:280], max_tokens=8, temperature=0.0)
    except Exception:
        return None
    r = _THINK_RE.sub("", r).strip().lower()
    if "yes" in r:
        return 1
    if "no" in r:
        return 0
    return None


def _run_council(store, atoms, args):
    n = len(atoms)
    ctx = _ESTATE_CONTEXT.get(args.scope, "")
    pool = COUNCIL_POOL
    print(f"COUNCIL: {n} atoms — {len(pool)} co-loaded small models vote on "
          f"{len(COUNCIL_QUESTIONS)} sub-questions, majority-fused.\n  pool: "
          f"{', '.join(p.split('-')[0] for p in pool)}\n")

    def _slug(s):
        return s.content[1:s.content.index("]")] if s.content.startswith("[") and "]" in s.content else s.id

    # CO-LOAD the whole pool at once (they fit under the cap — that's the point).
    if not args.no_balance:
        bal.unload_all()
        for m in pool:
            bal.load(m)
            bal.wait_until_loaded(m)
        resident = bal.loaded_keys()
        print(f"  co-resident: {len(resident)} models — {', '.join(k.split('-')[0] for k in resident)}\n")

    t0 = time.time()
    final = []
    for idx, s in enumerate(atoms):
        text = s.content
        # each question: collect a vote from every pool model, take the MAJORITY.
        qfrac = {}
        for qkey, qtext in COUNCIL_QUESTIONS.items():
            votes = [_ask_yesno(m, qtext, text, ctx) for m in pool]
            votes = [v for v in votes if v is not None]
            qfrac[qkey] = (sum(votes) / len(votes)) if votes else 0.5
        # FUSION: active-now score from the fused sub-answers. live_now & actionable
        # push UP; superseded pushes DOWN. Tuned to span 0-100.
        score = 50.0
        score += 35 * (qfrac["live_now"] - 0.5) * 2
        score += 25 * (qfrac["actionable"] - 0.5) * 2
        score -= 45 * (qfrac["superseded"] - 0.5) * 2
        score = max(0, min(100, round(score, 1)))
        final.append((s, score, qfrac))
        if (idx + 1) % 10 == 0:
            print(f"  ...{idx+1}/{n} ({(idx+1)/(time.time()-t0):.1f} atoms/s)")
    if not args.no_balance:
        bal.unload_all()
    print(f"\n  council scored {n} atoms in {time.time()-t0:.1f}s")

    final.sort(key=lambda t: t[1], reverse=True)
    print("\n-- COUNCIL scores (active -> dormant)   [super/live/act vote-fractions] --")
    for s, sc, qf in final:
        print(f"  {sc:5.1f}  [sup={qf['superseded']:.2f} live={qf['live_now']:.2f} "
              f"act={qf['actionable']:.2f}]  {_slug(s)}")

    if args.dry_run:
        print("\nDRY-RUN: bank not written.")
        return 0
    written = 0
    for s, sc, _qf in final:
        try:
            if store.reinforce(s.id, args.scope, q=float(sc), allow_promote=False).get("ok"):
                written += 1
        except Exception as e:
            print(f"  WRITE FAIL {s.id}: {e}")
    print(f"\nreinforced {written}/{n} atoms (council).")
    return 0


# ---- CASCADE: fast judge triages, reasoner handles the ambiguous middle ----
# EMPIRICAL CHOICE (2026-06-09): the fast stage is GEMMA, not a sub-1B. I downloaded
# qwen3.5-0.8b and tested it as the triage — it was fast (0.3s) but WRONG (scored a
# superseded-history atom 100 and a still-applied law 0: anti-correlated on the
# nuanced cases a 0.8B can't reason about). The scorecard showed gemma is 0.23s/atom
# AND the best judge (MAD 7.6), so gemma IS the right fast stage. The reasoner
# (qwen3.5-9B) then only re-scores atoms gemma left in the ambiguous band — saving
# the slow model's budget for where judgment actually matters.
CASCADE_FAST = "gemma-4-e4b-uncensored-hauhaucs-aggressive"
CASCADE_REASONER = "qwen3.5-9b-claude-4.6-os-auto-variable-heretic-uncensored-thinking-max-neocode-imatrix"
CASCADE_HI = 90          # fast score >= HI -> accept as active (no reasoner needed)
CASCADE_LO = 25          # fast score <= LO -> accept as dormant
# (the band (LO, HI) is the ambiguous middle the reasoner re-judges. HI was 80 but
# gemma over-rated a pre-cutover-history atom at 85 and it slipped through un-reviewed;
# widened to 90 so the reasoner verifies gemma's high-side calls too. Still cheap —
# the truly-obvious-active atoms (95+) skip the reasoner, only the borderline don't.)


def _run_cascade(store, atoms, args):
    n = len(atoms)
    ctx = _ESTATE_CONTEXT.get(args.scope, "")
    system = (ctx + "\n\n" + SYSTEM) if ctx else SYSTEM
    print(f"CASCADE: {n} atoms — stage1 {CASCADE_FAST.split('-')[0]} (triage) -> "
          f"stage2 {CASCADE_REASONER.split('-')[0]} (ambiguous {CASCADE_LO}-{CASCADE_HI} only)\n")

    def _slug(s):
        return s.content[1:s.content.index("]")] if s.content.startswith("[") and "]" in s.content else s.id

    def _score_all(model):
        acc = {}
        for b in range(0, n, args.batch):
            sl = atoms[b:b + args.batch]
            for gi, sc in _score_batch(sl, b, system, model).items():
                acc[gi] = sc
        return acc

    # stage 1 — fast judge over everything.
    if not args.no_balance:
        bal.unload_all(); bal.load(CASCADE_FAST); bal.wait_until_loaded(CASCADE_FAST)
    t1 = time.time()
    fast = _score_all(CASCADE_FAST)
    print(f"  stage1 ({CASCADE_FAST.split('-')[0]}): scored {len(fast)}/{n} in {time.time()-t1:.1f}s")

    # the ambiguous middle (and anything the fast judge missed) -> reasoner.
    ambiguous = [i for i in range(n)
                 if fast.get(i) is None or CASCADE_LO < fast[i] < CASCADE_HI]
    print(f"  ambiguous middle: {len(ambiguous)}/{n} -> reasoner; {n-len(ambiguous)} accepted from triage")

    deep = {}
    if ambiguous:
        if not args.no_balance:
            bal.unload_all(); bal.load(CASCADE_REASONER); bal.wait_until_loaded(CASCADE_REASONER)
        t2 = time.time()
        # score only the ambiguous atoms (re-index a sub-list).
        sub = [atoms[i] for i in ambiguous]
        for b in range(0, len(sub), args.batch):
            sl = sub[b:b + args.batch]
            for gi, sc in _score_batch(sl, b, system, CASCADE_REASONER).items():
                deep[ambiguous[gi]] = sc
        print(f"  stage2 ({CASCADE_REASONER.split('-')[0]}): re-scored {len(deep)}/{len(ambiguous)} in {time.time()-t2:.1f}s")
    if not args.no_balance:
        bal.unload_all()

    # blend: reasoner wins where it ran, else the fast triage score.
    final = []
    for i, s in enumerate(atoms):
        sc = deep.get(i, fast.get(i))
        src = "deep" if i in deep else "fast"
        final.append((s, sc if sc is not None else (getattr(s, "score", 50.0) or 50.0), src))
    final.sort(key=lambda t: t[1], reverse=True)
    print("\n-- CASCADE scores (active -> dormant)   [src] --")
    for s, sc, src in final:
        print(f"  {sc:5.1f}  [{src}]  {_slug(s)}")

    if args.dry_run:
        print("\nDRY-RUN: bank not written.")
        return 0
    written = 0
    for s, sc, _src in final:
        try:
            if store.reinforce(s.id, args.scope, q=float(sc), allow_promote=False).get("ok"):
                written += 1
        except Exception as e:
            print(f"  WRITE FAIL {s.id}: {e}")
    print(f"\nreinforced {written}/{n} atoms (cascade).")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--scope", required=True)
    ap.add_argument("--iters", type=int, default=3, help="re-score passes; averaged to damp noise")
    ap.add_argument("--batch", type=int, default=10)
    ap.add_argument("--model", default=None, help="single model id (overrides the panel)")
    ap.add_argument("--models", default=None, help="comma-separated ensemble panel (default = WARMTH_PANEL)")
    ap.add_argument("--no-balance", action="store_true", help="skip VRAM load-balancing (rely on JIT)")
    ap.add_argument("--cascade", action="store_true",
                    help="2-stage: FAST_JUDGE scores all, REASONER re-scores only the ambiguous middle")
    ap.add_argument("--council", action="store_true",
                    help="co-loaded SMALL models vote on decomposed sub-questions, majority-fused")
    ap.add_argument("--digest", action="store_true",
                    help="rod pre-pass: enrich each atom with cheap extracted features (date/keywords) before the cone reads")
    ap.add_argument("--dry-run", action="store_true", help="print scores, do NOT write the bank")
    args = ap.parse_args(argv)

    global _DIGEST_ON
    _DIGEST_ON = args.digest

    if args.council:
        store = SeedStore()
        atoms = list(store.seeds(scope=args.scope))
        if not atoms:
            print(f"no atoms in scope '{args.scope}'")
            return 1
        return _run_council(store, atoms, args)

    if args.cascade:
        store = SeedStore()
        atoms = list(store.seeds(scope=args.scope))
        if not atoms:
            print(f"no atoms in scope '{args.scope}'")
            return 1
        return _run_cascade(store, atoms, args)

    if args.model:
        panel = [args.model]
    elif args.models:
        panel = [m.strip() for m in args.models.split(",") if m.strip()]
    else:
        panel = list(WARMTH_PANEL)

    store = SeedStore()
    atoms = list(store.seeds(scope=args.scope))
    if not atoms:
        print(f"no atoms in scope '{args.scope}'")
        return 1
    n = len(atoms)
    print(f"ENSEMBLE: {n} atoms in scope '{args.scope}', {len(panel)} judge(s) x {args.iters} iter(s)")
    for m in panel:
        print(f"  - {m}")
    print()

    ctx = _ESTATE_CONTEXT.get(args.scope, "")
    system = (ctx + "\n\n" + SYSTEM) if ctx else SYSTEM
    if ctx:
        print(f"(estate context: {len(ctx)} chars prepended)\n")

    def _slug(s):
        return s.content[1:s.content.index("]")] if s.content.startswith("[") and "]" in s.content else s.id

    # LOAD-BALANCE: pack the panel into VRAM-safe batches (owner's 80% cap +
    # evict-before-load). Each batch's models are co-resident, so we score them
    # back-to-back without an evict between; we evict the PRIOR batch first.
    if args.no_balance or len(panel) == 1:
        batches = [[m] for m in panel]
        print(f"(load balancing OFF — {len(panel)} solo batch(es))\n")
    else:
        batches, foot, cap = bal.plan_batches(panel, vram_gib=bal.vram_total_gib())
        print(f"VRAM cap {cap} GiB — packed {len(panel)} judges into {len(batches)} batch(es):")
        for bi, b in enumerate(batches):
            print(f"  batch {bi+1} ({sum(foot[x] for x in b):.1f} GiB): {', '.join(x.split('-')[0] for x in b)}")
        print()

    # per-model mean: model_means[model][atom_index] = avg over iters. A model that
    # fails to load/score is SKIPPED (recorded as all-None) — never fatal to the panel.
    model_means = {}
    timing = {}          # model -> {elapsed, covered, per_atom} — speed dimension
    for bi, batch in enumerate(batches):
        if not args.no_balance and len(panel) > 1:
            bal.unload_all()                       # evict prior batch + WAIT for VRAM clear
            for m in batch:
                out = bal.load(m)                  # co-load this batch
                if "__ERR__" in out:
                    print(f"  [batch {bi+1}] LOAD FAIL {m.split('-')[0]}: {out[:80]}")
                    continue
                if not bal.wait_until_loaded(m):   # confirm resident before scoring (no race)
                    print(f"  [batch {bi+1}] {m.split('-')[0]} did not become resident — skipping")
        for model in batch:
            short = model.split("-")[0][:10]
            acc = {i: [] for i in range(n)}
            t_model = time.time()
            covered = 0
            try:
                for it in range(args.iters):
                    got = 0
                    for b in range(0, n, args.batch):
                        sl = atoms[b:b + args.batch]
                        scores = _score_batch(sl, b, system, model)
                        for gi, sc in scores.items():
                            acc[gi].append(sc)
                            got += 1
                    print(f"  [b{bi+1} {short}] iter {it+1}/{args.iters}: scored {got}/{n}")
                covered = sum(1 for v in acc.values() if v)
            except Exception as e:
                print(f"  [b{bi+1} {short}] SKIPPED (judge error): {str(e)[:90]}")
            elapsed = round(time.time() - t_model, 1)
            per_atom = round(elapsed / max(covered, 1), 2)
            timing[model] = {"elapsed": elapsed, "covered": covered, "per_atom": per_atom}
            print(f"  [b{bi+1} {short}] TIME {elapsed}s ({per_atom}s/atom, {covered}/{n} covered)")
            model_means[model] = {i: (round(sum(v)/len(v), 1) if v else None) for i, v in acc.items()}
    if not args.no_balance and len(panel) > 1:
        bal.unload_all()                           # leave the GPU clean

    # ENSEMBLE: MEDIAN across the judges that scored each atom — NOT the mean. A mean
    # lets ONE bad judge wreck the consensus: in the first run llama scored the
    # genuinely-active atoms (cutover ledger, principal-flip) at 10 while the other 4
    # said 90+, dragging the mean down ~25pts. That's not noise that cancels — it's a
    # consistent wrong bias. The median ignores a lone outlier (the 3-4 agreeing judges
    # decide). Spread (max-min) still flags where judges genuinely disagree.
    def _median(xs):
        xs = sorted(xs)
        k = len(xs)
        return xs[k // 2] if k % 2 else (xs[k // 2 - 1] + xs[k // 2]) / 2.0

    tbl = f"core_{args.scope}"
    final = []
    for i, s in enumerate(atoms):
        votes = [model_means[m][i] for m in panel if model_means[m][i] is not None]
        if votes:
            ens = round(_median(votes), 1)
            spread = round(max(votes) - min(votes), 0)
        else:
            ens = getattr(s, "score", 50.0) or 50.0
            spread = 0
        final.append((s, ens, votes, spread))

    final.sort(key=lambda t: t[1], reverse=True)
    cols = "  ".join(m.split("-")[0][:6] for m in panel)
    print(f"\n-- ENSEMBLE scores (active -> dormant)   [per-judge: {cols}]   ±spread --")
    for s, ens, votes, spread in final:
        vs = " ".join(f"{v:3.0f}" if v is not None else "  -" for v in
                      [model_means[m][atoms.index(s)] for m in panel])
        flag = " <-- high disagreement" if spread >= 40 else ""
        print(f"  {ens:5.1f}  [{vs}] ±{spread:2.0f}  {_slug(s)}{flag}")

    # ---- JUDGE SCORECARD: speed vs agreement (the demote signal) ----
    # agreement = mean |judge - ensemble_median| over the atoms it scored (LOWER is
    # better — closer to consensus). time = s/atom. A judge that is SLOW and has
    # WORSE agreement than a faster one is demotable; a fast judge that tracks
    # consensus is a keeper. (llama showed up here as the outlier in earlier runs.)
    ens_by_atom = {atoms.index(s): ens for s, ens, _v, _sp in final}
    print("\n-- JUDGE SCORECARD (speed vs consensus-agreement) --")
    print(f"  {'judge':<26} {'s/atom':>7} {'total_s':>8} {'covered':>8} {'disagree(MAD)':>14}")
    cards = []
    for m in panel:
        tinfo = timing.get(m, {})
        devs = [abs(model_means[m][i] - ens_by_atom[i])
                for i in range(n) if model_means[m].get(i) is not None]
        mad = round(sum(devs) / len(devs), 1) if devs else None
        cards.append((m, tinfo.get("per_atom"), tinfo.get("elapsed"), tinfo.get("covered", 0), mad))
    # sort by agreement (best judges first); None sinks
    cards.sort(key=lambda c: (c[4] is None, c[4] if c[4] is not None else 999))
    for m, pa, el, cov, mad in cards:
        short = m.split("-")[0][:24]
        pa_s = f"{pa}" if pa is not None else "  -"
        el_s = f"{el}" if el is not None else "  -"
        mad_s = f"{mad}" if mad is not None else "SKIPPED"
        note = ""
        if mad is not None and mad >= 25:
            note = "  <-- poor judge (far from consensus)"
        print(f"  {short:<26} {pa_s:>7} {el_s:>8} {cov:>8} {mad_s:>14}{note}")
    print("  (demote: a judge that is slower AND has higher MAD than a cheaper one adds no signal.)")

    if args.dry_run:
        print("\nDRY-RUN: bank not written.")
        return 0

    # Write via reinforce() — NOT a raw UPDATE of score. consolidate() (the dream,
    # run every SessionStart) RE-DERIVES score from score_history, so a raw score
    # write is wiped on the next boot. reinforce(q) appends the rating as a decaying
    # history delta (q on the same 0-100 scale), which compute_score honours and
    # consolidate preserves. allow_promote=False: these are already core atoms.
    written = 0
    for s, score, _votes, _spread in final:
        try:
            r = store.reinforce(s.id, args.scope, q=float(score), allow_promote=False)
            if r.get("ok"):
                written += 1
            else:
                print(f"  WRITE SKIP {s.id}: {r.get('reason')}")
        except Exception as e:
            print(f"  WRITE FAIL {s.id}: {e}")
    print(f"\nreinforced {written}/{n} atoms in scope '{args.scope}' (survives consolidate).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
