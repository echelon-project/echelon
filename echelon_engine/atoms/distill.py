"""Distill — turn raw captures into COS ATOMS (the seeding-quality fix).

The flaw the owner caught (2026-06-05): the per-step hook was growing TRANSCRIPT LITTER —
"[ECHELON] Bash: zcat: command not found", "[AlphaApp] Edit: edited ok", and worse, empties.
The scoring/decay/synthesis engine was pristine but grading garbage. COS already solved this
(COSys_DESIGN Layers 1-2 + the project:init ingestion, line 569):

    "AI reads file, extracts atomic units (single rules, single lines of guidance);
     AI generates a coordinate for each atom."

So a GOOD seed is not an event echo — it is an ATOM: a single reusable rule/lesson, placed in
the taxonomy by a coordinate. This module is that ingestion pass, two-tier (owner's call,
"expensive now is tolerable, this is the big win"):

  TIER 1 — capture (the hook, cheap/async): raw steps land in a `*-raw` staging scope. Material.
  TIER 2 — distill (THIS, a boundary pass where a model call is fine): read the raw captures,
     extract the atomic LESSON + assign a COS coordinate, write the real atom (coordinate set).
     Mark the raw consumed. Litter never reaches the scored spine; only atoms do.

Distillation needs JUDGMENT (a model call) — which is why it runs at a boundary (SessionStart /
sleep), NOT in the hot per-tool hook. A capture that carries no lesson is DROPPED, not atomized.
"""
from __future__ import annotations
import json

from .store import SeedStore

# A capture is worth distilling only if it plausibly carries a LESSON. Empties, pure-success
# acks ("edited ok"), and contentless tool echoes are dropped before the model is even asked.
_WORTHLESS_MARKERS = ("edited ok", "(no output)", "no output")


def _looks_worthless(content: str) -> bool:
    body = content.split("::", 1)[-1].strip() if "::" in content else content.strip()
    # strip a leading "[proj] Tool:" prefix to see if anything substantive remains
    if ":" in body:
        body = body.split(":", 1)[-1].strip()
    if len(body) < 8:
        return True
    low = body.lower()
    return any(m in low for m in _WORTHLESS_MARKERS)


_DISTILL_PROMPT = """You convert a raw agent-step record into a single reusable KNOWLEDGE ATOM, \
or reject it.

A good atom is ONE reusable lesson/rule — generalizable beyond this exact moment, the kind of \
thing worth remembering for next time. NOT a transcript echo, NOT "I ran X", NOT a one-off fact.

Also assign a COS COORDINATE: a colon-separated taxonomy path from broad to specific, e.g.
  tooling:shell:windows:gzip   or   workflow:git:commit:hygiene   or   domain:alpha-app:dashboard
3-5 levels, lowercase, the path should read like a sentence narrowing down.

Raw step:
---
{raw}
---

Respond with ONE json object, nothing else:
  {{"keep": true, "atom": "the single reusable lesson, one sentence", "coordinate": "a:b:c:d"}}
or, if there is no reusable lesson here (a pure success ack, an empty echo, a one-off):
  {{"keep": false}}"""


def distill_raw(store: SeedStore, raw_scope: str, atom_scope: str, provider, model_id: str,
                limit: int = 20) -> dict:
    """COS ingestion: read raw captures, extract atoms + coordinates via the model, write the
    real atoms (coordinate set), mark raw consumed (superseded). Returns a small report.

    `provider` is any ProviderBase; `model_id` the tier to distill on (a cheap tier is plenty —
    this is reading+summarizing, not reasoning). Runs at a boundary, NOT in the hot hook."""
    raws = [s for s in store.seeds(scope=raw_scope) if s.kind == "raw"][:limit]
    kept = dropped = failed = 0
    atoms = []
    for r in raws:
        if _looks_worthless(r.content):
            _mark_consumed(store, r, raw_scope)
            dropped += 1
            continue
        try:
            resp = provider.send(
                [{"role": "user", "content": _DISTILL_PROMPT.format(raw=r.content[:800])}],
                model_id=model_id, tools=None)
            verdict = _parse(resp.content)
        except Exception:
            failed += 1
            continue
        if not verdict or not verdict.get("keep"):
            _mark_consumed(store, r, raw_scope)
            dropped += 1
            continue
        atom = (verdict.get("atom") or "").strip()
        coord = (verdict.get("coordinate") or "").strip()
        if not atom:
            _mark_consumed(store, r, raw_scope); dropped += 1; continue
        # Carry the raw step's felt charge onto the atom (a trap stays dread-charged).
        aid = store.remember(atom_scope, atom, kind="atom",
                             valence=r.valence, arousal=r.arousal, coordinate=coord)
        _mark_consumed(store, r, raw_scope)
        atoms.append({"id": aid, "atom": atom, "coordinate": coord})
        kept += 1
    return {"scanned": len(raws), "atoms": kept, "dropped": dropped, "failed": failed,
            "made": atoms}


def _mark_consumed(store: SeedStore, raw_seed, raw_scope: str) -> None:
    """Mark a raw capture consumed (kind 'raw' -> 'raw-consumed'). Append-only spirit: we don't
    delete it (the record survives), we flip its kind so the next distill pass skips it."""
    store.mark_kind(raw_seed.id, raw_scope, "raw-consumed")


def _parse(text: str) -> dict | None:
    """Pull the JSON object out of the model's reply (tolerant of stray prose / fences)."""
    if not text:
        return None
    a, b = text.find("{"), text.rfind("}")
    if a < 0 or b <= a:
        return None
    try:
        return json.loads(text[a:b + 1])
    except Exception:
        return None
