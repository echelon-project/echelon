"""rolebook.py — the per-ROLE BOOK, the disk half of the brain/book split.

THE METAPHOR (owner, 2026-06-18): the BANK (`core_v2.db`) is the BRAIN — it knows
*which book has what work and how much it earned* (a warm pointer + weight). The
BOOKS are append-only jsonl on disk — all the notes/progress of the work, observable,
kept forever even for failed runs. On verified DONE a card composes ACROSS role-books
and credit flows into the brain (card-primary); a failed run keeps its notes on the
shelf but the brain weights nothing (the honesty law: significance is earned by trace).

THE BOUNDARY IS THE ROLE, NOT THE RUN (owner: "you don't want a math book to have
biology"). Each ROLE in the society — builder/dev, checker/qc, doubter/system,
supporter/architect — owns ONE book that accretes ACROSS runs:
    ~/.echelon/books/<scope>/<role>.jsonl
A run does not get *a* book — it APPENDS into the role-books it touches. This keeps
per-role foveation clean: `cartridge_boot()` can warm the builder on prior builds and
the doubter on prior breaks without the two smearing together.

This module is THIN: a RoleBook is a `WorldJournal` (worldjournal.py — already an
append-only, observable, temporally-validity-stamped jsonl) homed under ~/.echelon/books
and addressed by (scope, role). The book class is reused, not re-implemented.

Run python with `python -X utf8` (cp1252 arrow-crash trap).

WorldJournal is imported from the ported engine module (echelon_engine.agent.world.worldjournal).
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from echelon_engine.agent.world.worldjournal import WorldJournal


def books_home() -> Path:
    """The one shelf, beside the brain. Override with ECHELON_BOOKS for tests."""
    env = os.environ.get("ECHELON_BOOKS")
    if env:
        return Path(env)
    return Path.home() / ".echelon" / "books"


def book_path(scope: str, role: str, *, home: str | os.PathLike | None = None) -> Path:
    base = Path(home) if home is not None else books_home()
    return base / scope / f"{role}.jsonl"


class RoleBook:
    """One role's append-only book for a scope. Reuses WorldJournal for the on-disk
    append + snapshot + temporal valid/invalid ledger; adds the role/scope addressing
    and a `record_action` that an agent's on_event stream folds into.

    A book is observable live (tail the jsonl) and survives a crash (no runtime state).
    Subject-coherent by construction: only this role's moves land here."""

    def __init__(self, scope: str, role: str, *, home: str | os.PathLike | None = None):
        self.scope = scope
        self.role = role
        self.path = book_path(scope, role, home=home)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._journal = WorldJournal(self.path)
        # tag this book's open so the shelf is self-describing
        self._journal.note("book_open", scope=scope, role=role)

    def record_action(self, goal_id: str, partner: str, kind: str,
                      payload: dict[str, Any] | None = None,
                      *, artifact_ref: str | None = None) -> None:
        """Fold one worker ACTION (an on_event tick: step_start, step_done, write_back,
        a file write, a check verdict) into this role's book. The artifact (a diff, a
        file) is referenced by `artifact_ref` (a path or content-hash) — the book holds
        the note, the artifact lives where it was written; the book points at it."""
        rec = {"goal": goal_id, "partner": partner, "kind": kind,
               "artifact_ref": artifact_ref, **(payload or {})}
        self._journal.consume("action", rec)

    def record_outcome(self, goal_id: str, partner: str, *, ok: bool | None,
                       summary: str = "") -> None:
        """Record this role's outcome for a goal — the disk truth a DONE-by-disk check
        and the earn-on-DONE card read. `ok` is a verified outcome, never say-so."""
        self._journal.consume("outcome", {"goal": goal_id, "partner": partner,
                                          "ok": ok, "summary": summary[:600]})

    def events(self) -> list[dict[str, Any]]:
        """Replay this book in order (for card composition / study)."""
        import json
        if not self.path.exists():
            return []
        out: list[dict[str, Any]] = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except Exception:
                    pass
        return out

    @property
    def snapshot(self) -> dict[str, Any]:
        return self._journal.snapshot


# ── the role-society ↔ DOME-role map (builder/checker/doubter/supporter) ───────────────
# The society's honesty roles, named to DOME's existing role files so the marriage is
# literal: dev=builder, qc=checker, system=doubter, architect=supporter.
SOCIETY = {
    "builder": "dev",
    "checker": "qc",
    "doubter": "system",
    "supporter": "architect",
}
# canonical ORDER a card composes in (the honest sequence: build → check → doubt → support)
CARD_ORDER = ["dev", "qc", "system", "architect"]


def role_of(society_name_or_role: str) -> str:
    """Normalize a society name (builder) or a DOME role (dev) to the DOME role."""
    return SOCIETY.get(society_name_or_role, society_name_or_role)


# ── TIER CARTRIDGE: equipped by default based on the goal's work-kind ──────────────────
# A partner boots with ITS TIER's cartridge (sufficiency by tier — carry the least that lets
# you act correctly at your level). OS_TIER = the earned echelon-op operating cartridge; the
# lower tiers add a small tier-specific seed (memory/operating/t1|t2|t3, scope echelon-t1|t2|t3).
# The common ECHELON seed travels via the cross-scope atlas bridge; the tier scope is what's ADDED.
#
# Tiering rides the SAME classify_tier the EXECUTOR tiering uses (gantt_pillars) — equipment-tier
# and execution-tier agree by construction, no second classifier.
TIER_SCOPES = {
    "OS_TIER": "echelon-op",   # you & me — the whole substrate (op-00..08), already earned ground
    "T1": "echelon-t1",        # orchestrator — decompose / tier-tag / delegate down / verify outcome
    "T2": "echelon-t2",        # builder — act directly, craft discipline
    "T3": "echelon-t3",        # cheap floor — do the atom and stop
}
# work-kind (classify_tier output) -> equipment tier
_KIND_TO_TIER = {
    "planning": "T1",
    "code_generation": "T2",
    "validation": "T2",
    "fast": "T3",
}


def tier_for_goal(goal: str, *, explicit: str | None = None) -> str:
    """The equipment tier for a goal. `explicit` (e.g. 'T2') wins; else classify the work-kind
    with the SAME classifier the executor uses, and map it to a tier. Defaults to T2 (build)."""
    if explicit and explicit.upper() in TIER_SCOPES:
        return explicit.upper()
    try:
        from echelon_sdk.gantt_pillars import classify_tier
        kind = classify_tier(goal or "")
    except Exception:
        kind = "code_generation"
    return _KIND_TO_TIER.get(kind, "T2")


def tier_cartridge(goal: str, *, explicit: str | None = None) -> tuple[str, str]:
    """Return (tier, scope) of the tier-cartridge to plug in for this goal. The scope is added to
    the partner's cartridge compose ALONGSIDE the project scope + craft, so the partner wakes
    equipped for its tier. OS_TIER is reachable by explicit='OS_TIER' (you & me, not auto-assigned
    to a worker goal)."""
    tier = tier_for_goal(goal, explicit=explicit)
    return tier, TIER_SCOPES[tier]


# ── BRAIN ← BOOK: compose a cross-role card on verified DONE, credit the bank ───────────
def compose_card_on_done(scope: str, goal: str, *, outcome_ok: bool,
                         books: dict[str, Any] | None = None,
                         home: str | os.PathLike | None = None,
                         tier_scope: str | None = None,
                         db_path: str | None = None) -> dict[str, Any] | None:
    """The earn-on-DONE join: on a VERIFIED outcome, compose the run's role-books into one
    ordered card (build -> check -> doubt -> support) and credit it in the BANK (card-primary).
    This is the ".md is source-of-truth, bank is a one-way plant" law applied to RUNS: the
    books are source-of-truth (they keep their notes regardless), the brain learns the index
    entry — which books held this work and how much it earned — ONLY on a verified outcome.

    The honesty law (same as earn_craft_from_trace): `outcome_ok` must be a witnessed outcome
    (disk truth / verify-fn), NEVER a worker's say-so. A green run earns a warm Q; a red/
    unverified run earns a low stall Q (the attempt is real, the win is not claimed). The book
    keeps its notes either way — the brain just doesn't WEIGHT a non-earned run. Best-effort:
    returns None / an error dict on any failure; earning never breaks the run that earned it.
    source='trace' always.

    EARNING THE CARTRIDGE, NOT JUST THE CARD (2026-06-18, the open hop the marriage stopped on):
    the run was EQUIPPED with a tier-cartridge ([[tier-cartridge-equipped-by-default-on-the-boot]]),
    so a verified-green run must warm the SEEDS it used, not just the run-card. The card's refs
    therefore carry the equipped tier scope's atom COORDINATES (tight attribution — only the tier
    actually equipped, per the owner's fork) alongside the role-book pointers; §7-P2 partial credit
    then flows into echelon-t1/t2/t3. The book pointers stay in refs as the index entry but match no
    atom (inert for credit by design); the tier coordinates are the live credit path. `tier_scope`
    is resolved from the goal via tier_cartridge() when not passed."""
    try:
        # which role-books actually have moves for this run — the card spans only those.
        bks = books or {r: RoleBook(scope, r, home=home) for r in CARD_ORDER}
        touched = []
        for r in CARD_ORDER:
            b = bks.get(r)
            if b is None:
                continue
            acts = [e for e in b.events() if e.get("event") == "action"]
            if acts:
                touched.append((r, len(acts)))
        if not touched:
            return {"card": None, "ok": False, "detail": "no role-book moves to compose"}

        from echelon_engine.atoms.cards import CardStore
        cs = CardStore(db_path) if db_path else CardStore()
        label = f"dome:{scope}:run"
        # find-or-create the run-card (add_card is content-addressed + INSERT-OR-IGNORE, so this
        # is idempotent: the same scope's run is the same card, accreting score across runs). Its
        # refs are the touched role-books in canonical order (the index entry the brain learns) PLUS
        # the equipped tier-cartridge's atom coordinates — the live credit path so a green run warms
        # the SEEDS it used, not just the card (closes "earning the card isn't earning the cartridge").
        book_refs = [f"book:{scope}:{r}" for r, _ in touched]
        ts = tier_scope or tier_cartridge(goal)[1]
        tier_refs: list[str] = []
        try:
            tier_refs = [a["coordinate"] for a in cs.atom_ids_in_scope(ts) if a.get("coordinate")]
        except Exception:
            tier_refs = []
        # content-address the card on its book pointers (stable id across runs); the tier coordinates
        # are appended after so the SAME tier scope's run stays one accreting card.
        refs = book_refs + tier_refs
        cid = cs.add_card(label, refs, born_from="dome-run")

        q = 85.0 if outcome_ok else 35.0   # verified-green earns warm; red/unverified marks a stall
        rep = cs.reinforce_card(cid, q, source="trace")
        # record the outcome into each touched book so the book is self-describing about the earn.
        for r, _n in touched:
            try:
                bks[r].record_outcome(goal, "card", ok=outcome_ok,
                                      summary=f"composed into {label} q={q}")
            except Exception:
                pass
        return {"card": label, "q": q, "ok": bool(rep.get("ok", True)),
                "roles": [r for r, _ in touched], "order": [r for r in CARD_ORDER if r in dict(touched)]}
    except Exception as e:
        return {"error": str(e)[:160]}
