"""atom_kinds — the SECOND AXIS of memory: topical KIND, orthogonal to scope/estate.

THE DECISION (owner, 2026-06-09, settled on data — see the fork experiment): a scope
is WHERE an atom was learned (its estate); a KIND is WHAT it is ABOUT (compute, pricing,
sync, deploy, memory, ui, identity). The council-classified corpus is 67% MULTI-kind
(regex over-claimed 82%; the council audit corrected it), so kind MUST be multi-label —
forcing a single home (kind-only) loses info on two-thirds of atoms. Estate stays the
sharp partition; kind is the cross-cutting axis so a NEW project session can draw
relevant knowledge by KIND even when it was learned in a different estate.

WHY A SIDE TABLE (not content/coordinate/kind): the atom id is content-addressed from
(content, domain, kind), and `coordinate` DETERMINES the domain table. Writing kinds into
any of those changes the id -> re-ingest would DUPLICATE the bank (dedup is INSERT OR
IGNORE on the id). So kinds live in their OWN table keyed by atom id: no id change, no
duplicate, no core-table migration, idempotent. recall joins on it.

The classifier is the existing COUNCIL (warmth_update.COUNCIL_POOL — qwen3.5-0.8b,
qwen2.5-1.5b, smollm3-3b, gemma-4): co-load once, majority-vote each (atom, kind). Tiny
models, $0 floor, the right tier for a 7-bucket multi-label call. Ingest is rare, so the
~0.2 atoms/s council cost is fine.
"""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path

from .echelon_home import home_db as _home_db
DEFAULT_DB = _home_db("core.db")

# The kind vocabulary. SEED list (not frozen): the classifier picks from these; new kinds
# can be added here as the estate grows. Kept few-and-broad on purpose — the data showed
# atoms cluster at 2-3 kinds, so broad buckets + multi-label beats many narrow single-tags.
KINDS = ["compute", "pricing", "sync", "deploy", "memory", "ui", "identity"]

# One yes/no question per kind, mirroring warmth_update.COUNCIL_QUESTIONS format exactly
# (a tiny model answers each reliably; majority across the council fuses out single-model bias).
KIND_QUESTIONS = {
    "compute":  "Is this note ABOUT local LLMs, GPUs, VRAM, compute tiering, or model infrastructure? Answer ONLY yes or no.",
    "pricing":  "Is this note ABOUT loan pricing — LTV, OTR, fees, insurance, or principal? Answer ONLY yes or no.",
    "sync":     "Is this note ABOUT Google-Sheet sync, column maps, or the sheet writer? Answer ONLY yes or no.",
    "deploy":   "Is this note ABOUT deploying, hot-patching, staging, or releases? Answer ONLY yes or no.",
    "memory":   "Is this note ABOUT the memory bank, warmth, scopes, recall, or the atlas graph? Answer ONLY yes or no.",
    "ui":       "Is this note ABOUT a user interface — dashboard, calculator, Flux, or rendering? Answer ONLY yes or no.",
    "identity": "Is this note ABOUT agent identity, soul, or continuity? Answer ONLY yes or no.",
}


def _conn(db_path: Path | str | None = None) -> sqlite3.Connection:
    c = sqlite3.connect(str(db_path or DEFAULT_DB))
    c.execute(
        "CREATE TABLE IF NOT EXISTS atom_kinds ("
        " atom_id TEXT NOT NULL, kind TEXT NOT NULL, ts TEXT DEFAULT (datetime('now')),"
        " PRIMARY KEY (atom_id, kind))"
    )
    c.execute("CREATE INDEX IF NOT EXISTS ix_atom_kinds_kind ON atom_kinds(kind)")
    return c


def set_kinds(atom_id: str, kinds: list[str], db_path=None) -> int:
    """Idempotent: write (atom_id, kind) rows. INSERT OR IGNORE so re-classifying never dups."""
    c = _conn(db_path)
    try:
        rows = [(atom_id, k) for k in kinds if k in KINDS]
        c.executemany("INSERT OR IGNORE INTO atom_kinds(atom_id, kind) VALUES (?, ?)", rows)
        c.commit()
        return len(rows)
    finally:
        c.close()


def kinds_of(atom_id: str, db_path=None) -> list[str]:
    c = _conn(db_path)
    try:
        return [r[0] for r in c.execute("SELECT kind FROM atom_kinds WHERE atom_id=?", (atom_id,))]
    finally:
        c.close()


def atoms_with_kind(kind: str, db_path=None) -> list[str]:
    """All atom ids tagged with this kind — across EVERY estate (the cross-cutting axis)."""
    c = _conn(db_path)
    try:
        return [r[0] for r in c.execute("SELECT atom_id FROM atom_kinds WHERE kind=?", (kind,))]
    finally:
        c.close()


def kind_counts(db_path=None) -> dict[str, int]:
    c = _conn(db_path)
    try:
        return {k: n for k, n in c.execute("SELECT kind, COUNT(*) FROM atom_kinds GROUP BY kind")}
    finally:
        c.close()


import json as _json
import re as _re

# ONE prompt that asks a model for ALL kinds at once. This is the perf shape: NOT async
# across models (the single-GPU box 500s on concurrent multi-model requests — tested), and
# NOT 7 serial yes/no calls per model (28 calls/atom). Instead ONE batched call per model
# (4 calls/atom), each returning a {kind: bool} map. Same council majority-vote fusion.
_BATCH_PROMPT = (
    "For the NOTE below, decide which KINDS it is ABOUT (its topic categories, NOT which "
    "project it came from). Reply ONLY a JSON object mapping EACH of these keys to true or "
    "false: " + _json.dumps(KINDS) + ".\nNOTE:\n{atom}"
)


def _ask_all_kinds(model, content: str, ctx: str = "") -> dict:
    """One batched call: ask `model` for every kind at once. Returns {kind: bool} (missing/
    unparseable -> treated as no-vote by the caller). Lazy-imports the chat primitive."""
    from .warmth_update import _chat, _THINK_RE   # sibling atom (post WELD #1, _chat is from floor_chat)
    sysmsg = (ctx + "\n\n" if ctx else "") + _BATCH_PROMPT.format(atom=content[:700])
    try:
        raw = _chat(model, sysmsg, "", max_tokens=90, temperature=0.0)
    except Exception:
        return {}
    raw = _THINK_RE.sub("", raw)
    m = _re.search(r"\{.*?\}", raw, _re.S)
    if not m:
        return {}
    try:
        obj = _json.loads(m.group(0))
    except Exception:
        return {}
    return {k: bool(v) for k, v in obj.items() if k in KINDS}


def classify(content: str, pool=None, ctx: str = "") -> list[str]:
    """COUNCIL multi-label classify: each model votes ALL kinds in one batched call, then
    majority-fuse per kind. Returns the kinds the council agrees this atom is ABOUT (0..N;
    empty = no clean kind = the 'misc' tail). 4 calls/atom (one per co-resident model),
    NOT 28 — see _BATCH_PROMPT. Lazy-imports the council (no LLM dep to read kinds)."""
    from .warmth_update import COUNCIL_POOL   # sibling atom
    pool = pool or COUNCIL_POOL
    tally = {k: [0, 0] for k in KINDS}  # kind -> [yes, total]
    for m in pool:
        votes = _ask_all_kinds(m, content, ctx)
        for k in KINDS:
            if k in votes:
                tally[k][1] += 1
                tally[k][0] += 1 if votes[k] else 0
    return [k for k, (yes, total) in tally.items() if total and yes / total > 0.5]


def classify_and_store(atoms: list, db_path=None, verbose: bool = True, co_load: bool = True) -> dict:
    """Classify a list of Seed-like objects (need .id and .content) and store their kinds.
    Co-loads the council ONCE (no thrash), votes all atoms, unloads. Returns {atom_id: kinds}.

    This is the ingest hook: call it after remember_many returns the ids."""
    from .warmth_update import COUNCIL_POOL   # sibling atom
    from echelon_sdk import lms_balancer as bal   # pure leaf — migrated to sdk

    if co_load:
        bal.unload_all()
        for m in COUNCIL_POOL:
            bal.load(m)
            bal.wait_until_loaded(m)
        if verbose:
            print(f"  council co-resident: {[k.split('-')[0] for k in bal.loaded_keys()]}")

    result = {}
    try:
        for i, a in enumerate(atoms):
            ks = classify(a.content)
            set_kinds(a.id, ks, db_path=db_path)
            result[a.id] = ks
            if verbose and (i + 1) % 10 == 0:
                print(f"  ...classified {i + 1}/{len(atoms)}")
    finally:
        if co_load:
            bal.unload_all()
    if verbose:
        from collections import Counter
        dist = Counter(len(v) for v in result.values())
        print(f"  kinds assigned — multi(>=2): {sum(n for k,n in dist.items() if k>=2)}/{len(result)}, "
              f"single: {dist.get(1,0)}, none: {dist.get(0,0)}")
    return result
