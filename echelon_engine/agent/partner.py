"""partner.py -- the ON-DEMAND TRUSTED PARTNER, the culmination skill engine.

This is what ECHELON is FOR, made into one entrypoint. The owner's cut (2026-06-17,
AlphaApp): "imagine Grok equipped with ECHELON, simple goal migrate a,b,c,d with
rules 1,2,3 -- why complicated like this? what is ECHELON for?" (the loop driver here
defaults to grok-4.3 -- the standing tool-loop driver) The answer this module
makes operational: ECHELON makes a capable model wake up as an OPERATOR WHO ALREADY
LIVES HERE -- it holds the scope's topology, settled decisions, and traps as warm atoms,
so the human hands ONE SENTENCE and the partner ACTS.

This SUBSUMES and replaces the removed ceremony skills (/echelon-swarm, /pipeline,
/dispatch, /embed). Those kept the orchestrator in the loop as conductor + integrator --
turning a one-sentence goal into a 150-message DAG project. The lesson
([[equipping-simpler-not-harder]]): if equipping makes it HARDER, you inverted the
substrate. So the DEFAULT path here is dead simple: one equipped agent, one sentence,
it acts, you verify the OUTCOME (not every diff) -- trust earned because it shares your
bank. Swarm (N partners) and board (shared ledger) are CAPABILITIES the partner reaches
for when the goal genuinely fans out -- never the entry ritual.

The trust model: you do NOT re-review every line. You verify the outcome works (the app
runs, the scanner is green). The shared cartridge is what makes that trust earned rather
than blind -- the partner already knows the traps you'd otherwise gate against.

Three shapes, escalating only as the GOAL demands:
  1. dispatch(goal, scope, folder)          -- ONE partner. The default. 90% of asks.
  2. swarm(goals, scope, folder)            -- N partners, parallel, warmth-scheduled.
  3. board(...)                             -- partners coordinating through a shared
                                              append-only ledger (DOME-like). Phase 2.

Run python with `python -X utf8` (cp1252 arrow-crash trap). Never raw-SQL the soul.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

# -- the equipped-agent primitive (already proven; we just hand it a sentence) ---------
# make_agent_runner builds a FULL agent loop booted into a role, on its lived device
# (the cartridge), sandboxed to `folder`, with its own ToolRegistry (read/write/edit/
# bash + read-before-edit guard). That IS the partner. We add: cartridge warm-up, the
# one-sentence contract, outcome verification, and the swarm/board escalation.


def _warm_one(scope: str, goal: str, store, graph, *, n: int = 5) -> dict[str, Any]:
    """Foveate ONE cartridge (scope) on the goal. Returns its verdict + warmest move bodies."""
    from echelon_engine.atoms.warmth import warmth
    r = warmth(goal, store, scope=scope, scope_graph=graph)
    warmest = []
    for sw in (getattr(r, "warmest", None) or [])[:n]:
        seed = sw.seed if hasattr(sw, "seed") else sw
        warmest.append((getattr(seed, "content", "") or "")[:140])
    return {"scope": scope, "verdict": getattr(r, "verdict", "unknown"),
            "score": round(float(getattr(r, "score", 0.0)), 3),
            "guidance": getattr(r, "guidance", None), "warmest": warmest}


def _warm_the_partner(scope: str, goal: str, db_path: str | None = None,
                      cartridges: list[str] | None = None) -> dict[str, Any]:
    """Foveate the partner's cartridge(s) on THIS goal before it acts -- so it wakes holding
    the relevant traps/decisions, not cold. The PROJECT scope is the primary cartridge (its
    topology + settled decisions); `cartridges` are EXTRA task-type cartridges plugged in
    ALONGSIDE it (e.g. `craft` = the disciplined-dev methodology), warmth-merged. This is
    multi-cartridge compose: a partner = project-cartridge + task-cartridge(s), so discipline
    travels as a plug-in instead of being re-pasted per task. Returns the primary verdict (a
    signal, not a gate) plus each plugged cartridge's warmest moves. Uses the real warmth()
    with the atlas bridge so a neighbour scope's lesson can travel in."""
    from echelon_engine.atoms.store import SeedStore
    try:
        from echelon_sdk.scopegraph import ScopeGraph  # type: ignore
        graph = ScopeGraph()
    except Exception:
        graph = None
    try:
        store = SeedStore(db_path) if db_path else SeedStore()
        primary = _warm_one(scope, goal, store, graph)
        plugged = []
        for c in (cartridges or []):
            if c and c != scope:
                try:
                    plugged.append(_warm_one(c, goal, store, graph))
                except Exception:
                    pass
        out = dict(primary)
        if plugged:
            out["cartridges"] = plugged
        return out
    except Exception as e:
        return {"verdict": "unknown", "score": 0.0, "warmest": [], "error": str(e)[:120]}


def _make_partner_runner(provider, model: str, folder: str, *, role: str = "dev",
                         guidance: str | None = None,
                         files: list[str] | None = None,
                         budget=None, max_steps: int = 60,
                         session_path: str | None = None,
                         on_event: Callable[[str, dict], None] | None = None,
                         control: Callable[[], dict] | None = None) -> Callable[[dict, dict], dict]:
    """One equipped partner = one make_agent_runner step. Booted into `role` on its lived
    device (the cartridge), sandboxed to `folder`, own tools. `guidance` rides as a system
    reminder (the rules the partner already half-knows from the bank, made explicit);
    `files` ground it (the canonical pattern, e.g. pnl.html). on_event streams every worker
    step up for observability (owner: we MUST observe everything, not just the outcome)."""
    from .workflow import make_agent_runner
    return make_agent_runner(provider, model, folder, budget=budget, max_steps=max_steps,
                             guidance=guidance, files=files, session_path=session_path,
                             on_event=on_event, control=control)


def _default_provider_model() -> tuple[Any, str]:
    """The loop-driver: a capable model that natively drives the ToolRegistry. Grok is
    the standing loop driver (the bridge is text-only and cannot drive a tool loop)."""
    from echelon_engine.atoms.providers.grok import GrokProvider
    return GrokProvider(), "grok-4.3"


def earn_craft_from_trace(goal: str, *, rules: str | None = None,
                          outcome_ok: bool, db_path: str | None = None) -> dict[str, Any] | None:
    """Credit the matching craft cartridge card from a REAL run trace -- the one earn-hook
    shared by dispatch() AND a command-center session (cli.py). The craft cartridge USES
    itself: born neutral, it warms ONLY because a card that loaded it succeeded. So a coding
    run that plugged `craft` in and VERIFIED green credits craft:feature-loop / craft:bugfix-loop;
    an unverified/red run credits a stall Q (marks the attempt without claiming a win).

    The honesty law (why this is not say-so): `outcome_ok` must come from a verified outcome
    (files changed on disk / scanner green / status==completed-with-evidence), NEVER from the
    model's own "I'm done." A red trace still earns a low Q (the attempt is real); only a
    verified-green trace earns the warm Q. Best-effort, returns None on any failure -- earning
    must never break the run that earned it. source='trace' always."""
    try:
        from echelon_engine.atoms.cards import CardStore
        cs = CardStore(db_path) if db_path else CardStore()
        label = "craft:bugfix-loop" if any(
            w in (goal + " " + (rules or "")).lower()
            for w in ("fix", "bug", "crash", "error", "broken")
        ) else "craft:feature-loop"
        cid = cs.card_id_for_label(label) if hasattr(cs, "card_id_for_label") else None
        if cid is None:
            row = cs.conn.execute("SELECT id FROM cards WHERE label=?", (label,)).fetchone()
            cid = row["id"] if row else None
        if not cid:
            return {"card": label, "ok": False, "detail": "no such craft card in bank"}
        q = 85.0 if outcome_ok else 35.0  # green trace earns; red trace marks a stall
        rep = cs.reinforce_card(cid, q, source="trace")
        return {"card": label, "q": q, "ok": bool(rep.get("ok", True))}
    except Exception as e:
        return {"error": str(e)[:120]}


_CLAIMED_DONE = ("completed", "done", "success")

def verify_gate(claimed_status: str | None, outcome: dict | None) -> str:
    """THE VERIFY REFLEX, enforced by the transport (NOT by a cartridge atom). A partner's
    self-reported status is a CLAIM; the outcome (an on-disk / verify-fn check) is the witnessed
    truth. When a partner CLAIMS done but a verifiable outcome is RED, this returns 'outcome-failed'
    so no caller can read a claimed-done as truth.

    Why a code gate and not an atom: the op-04 verify-reflex could NOT be earned from cartridge prose
    (battle-test 2026-06-18: the verify question lost 0/3 even after the atom was strengthened twice).
    The chess +4 precedent is that an earned reflex must live in the TRANSPORT, keyed on the witnessed
    outcome -- same shape here. Pure + tiny so it is unit-testable in isolation (no loop spin-up):
      - claimed-done + RED outcome      -> 'outcome-failed'  (the gate fires)
      - claimed-done + GREEN outcome    -> unchanged          (verified true)
      - claimed-done + NO outcome check -> unchanged          (nothing to verify against)
      - not a done-claim                -> unchanged          (gate only reconciles done-claims)
    """
    if outcome is not None and not outcome.get("ok") and claimed_status in _CLAIMED_DONE:
        return "outcome-failed"
    return claimed_status


# -- SHAPE 1: dispatch -- ONE partner, one sentence. The default. ----------------------
def dispatch(goal: str, *, scope: str, folder: str,
             rules: str | None = None,
             pattern_files: list[str] | None = None,
             role: str = "dev",
             provider=None, model: str | None = None,
             db_path: str | None = None,
             max_steps: int = 60,
             budget_usd: float | None = None,
             cartridges: list[str] | None = None,
             craft: bool | None = None,
             tier: str | None = None,
             session_path: str | None = None,
             verify: Callable[[], tuple[bool, str]] | None = None,
             on_event: Callable[[str, dict], None] | None = None,
             control: Callable[[], dict] | None = None) -> dict[str, Any]:
    """Hand ONE equipped partner a one-sentence goal (+ optional rules it already half-holds
    from the bank, + the canonical pattern files) and let it ACT in `folder`. Returns its
    result plus the warm verdict and the outcome-verification -- you check the OUTCOME, not
    the diffs.

        goal          : one sentence -- "migrate orders/products/ledger/supplier to Tabulator"
        scope         : the project's bank scope -- the cartridge that makes it a local operator
        folder        : the repo it is sandboxed to (its hands resolve here)
        rules         : the constraints, made explicit (it already knows most from the bank):
                        "copy pnl.html; lockstep cache-buster; don't touch the autolink"
        pattern_files : canonical patterns to ground it (read once, attached)
        verify        : an OUTCOME check (scanner green? page loads?) -- trust is in the
                        outcome, not line-by-line review.
    """
    if provider is None:
        # If a model was named, route it to ITS provider (the empirical routing table
        # maps e.g. gemini-* -> GeminiProvider). Without this a named non-Grok model was
        # sent to the Grok default and 404'd ("Model not found"). Fall back to the standing
        # Grok loop-driver only when no model was named.
        if model:
            try:
                from echelon_engine.atoms import routing
                provider = routing.provider_for(model)
            except Exception as exc:
                # An explicit model is part of the dispatch contract. Routing failure
                # must not change provider, authority, cost or data destination silently.
                raise RuntimeError(
                    "named model provider resolution failed (" + type(exc).__name__
                    + "); no fallback provider was selected"
                ) from None
        else:
            provider, _dm = _default_provider_model()
            model = _dm
    model = model or "grok-4.3"

    # PLUG IN THE CRAFT CARTRIDGE (the task-type cartridge). A coding goal gets the
    # disciplined-dev cartridge ALONGSIDE the project cartridge by default -- the partner wakes
    # holding TDD / verify-before-claim / root-cause-debug / plan-anchoring as moves, not just
    # the project's traps. `craft=False` opts out; `cartridges=[...]` adds others.
    from echelon_sdk import roles as _roles
    _forbid = set(_roles.forbidden_tools(role))
    _goal_needs_write = any(w in (goal + " " + (rules or "")).lower()
                            for w in ("write", "edit", "migrate", "rework", "redesign",
                                      "implement", "create", "add ", "fix ", "refactor", "change"))
    _plug = list(cartridges or [])
    _use_craft = craft if craft is not None else _goal_needs_write
    if _use_craft and "craft" not in _plug and scope != "craft":
        _plug.append("craft")

    # TIER CARTRIDGE (equipped by default based on the goal's work-kind). The partner wakes with
    # ITS tier's seed (T1 orchestrate / T2 build / T3 cheap-floor) on top of the project+craft
    # cartridges -- sufficiency by tier. Rides the SAME classify_tier the executor tiering uses, so
    # equipment-tier and execution-tier agree. Skipped if the goal's scope IS a tier scope already.
    from .rolebook import tier_cartridge
    _tier, _tier_scope = tier_cartridge(goal, explicit=tier)
    if _tier_scope and _tier_scope != scope and _tier_scope not in _plug:
        _plug.append(_tier_scope)

    warm = _warm_the_partner(scope, goal, db_path, cartridges=_plug)

    # HANDS-CHECK (gap caught 2026-06-17): a build goal dispatched to a role whose write tools
    # are forbidden produces a confident-sounding answer and ZERO disk changes (the 'designer
    # described a redesign but wrote nothing' failure). If the goal needs writing but the role
    # can't write, refuse loudly rather than 'succeed' empty-handed.
    if _goal_needs_write and ({"write_file", "edit_file"} & _forbid):
        return {"goal": goal, "scope": scope, "warm": warm, "status": "refused",
                "answer": f"role '{role}' forbids write tools ({sorted(_forbid)}) but the goal "
                          f"needs to write to disk. Dispatch with a writing role (e.g. role='dev'). "
                          f"A no-hands partner cannot build.",
                "steps": 0, "outcome": {"ok": False, "detail": "no write hands for a build goal"}}

    # Fold each plugged task-cartridge's warmest moves into the guidance -- this is what makes
    # the partner WAKE holding the discipline as moves (not be told it from outside). The moves
    # are surfaced by warmth on THIS goal, so only the relevant ones ride along.
    craft_block = ""
    for c in (warm.get("cartridges") or []):
        moves = c.get("warmest") or []
        if moves:
            craft_block += (
                f" You also have the `{c['scope']}` cartridge plugged in -- these earned moves "
                f"apply to this goal, follow them: " + " | ".join(m for m in moves) + "."
            )

    guidance = (
        f"You are an ECHELON-equipped partner operating IN-SCOPE (`{scope}`). You already "
        f"hold this estate's traps and settled decisions as warm memory -- act like an operator "
        f"who lives here, not a stateless model. RULES (you know most of these already): "
        f"{rules or '(none beyond the bank)'}.{craft_block} Act directly with your own tools; "
        f"do not ask for permission to do the obvious. If the goal is new ground (the warm "
        f"verdict was '{warm.get('verdict')}'), prove on the smallest slice first, then continue."
    )

    # BUDGET CAP (gap caught 2026-06-18 dogfood): without a Budget the loop's $-cap never runs --
    # a partner is bounded by STEPS only, never by SPEND. Thread a real Budget when a cap is given
    # so the loop charges + halts at the dollar edge (the meter counts; THIS makes it stop).
    _budget = None
    if budget_usd is not None:
        from echelon_engine.atoms.providers.cost import Budget, budget_key
        _budget = Budget(key=budget_key(goal), total=float(budget_usd))
    run_step = _make_partner_runner(provider, model, folder, role=role,
                                    guidance=guidance, files=pattern_files,
                                    budget=_budget, max_steps=max_steps,
                                    session_path=session_path, on_event=on_event,
                                    control=control)

    # DEFAULT OUTCOME-VERIFY (gap caught 2026-06-17): without this, a claims-done-blind partner
    # returns status=completed + outcome=null and looks like it worked. So when no custom verify
    # is given, snapshot the folder's newest-mtime before/after -- a build that changed nothing on
    # disk is RED, surfaced honestly (you still verify the *real* outcome yourself, but a write
    # goal that touched zero files never silently passes).
    def _newest_mtime() -> float:
        newest = 0.0
        try:
            for p in Path(folder).rglob("*"):
                if p.is_file() and not any(s in p.parts for s in (".git", "__pycache__", "node_modules", ".venv")):
                    m = p.stat().st_mtime
                    if m > newest:
                        newest = m
        except Exception:
            pass
        return newest

    _before = _newest_mtime() if verify is None and _goal_needs_write else None
    result = run_step({"agent": role, "task": goal}, {})

    outcome = None
    if verify is not None:
        try:
            ok, detail = verify()
            outcome = {"ok": ok, "detail": detail}
        except Exception as e:  # a verify that throws is a RED outcome, surfaced honestly
            outcome = {"ok": False, "detail": f"verify raised: {e}"}
    elif _before is not None:
        changed = _newest_mtime() > _before
        outcome = {"ok": changed,
                   "detail": "files changed on disk" if changed else
                             "NO files changed on disk -- partner claimed done but wrote nothing (RED)"}

    # EARN-BY-TRACE (the cartridge USES itself): if the craft cartridge was plugged in AND the
    # OUTCOME verified, credit the matching craft card from this REAL trace -- the share flows to
    # the atoms it loaded, so the discipline moves earn warmth honestly (born neutral -> warm only
    # because a card that used them succeeded). A failed/unverified run credits NOTHING (the honesty
    # law: source='trace', Q from the actual outcome, never say-so). Best-effort, never blocks.
    earned = None
    if "craft" in _plug and outcome is not None:
        earned = earn_craft_from_trace(goal, rules=rules,
                                       outcome_ok=bool(outcome.get("ok")), db_path=db_path)

    # -- THE VERIFY GATE, ENFORCED BY THE TRANSPORT (not by an atom). See verify_gate(). --
    claimed = result.get("status")
    verified_status = verify_gate(claimed, outcome)

    return {"goal": goal, "scope": scope, "warm": warm,
            "status": verified_status, "claimed_status": claimed,
            "answer": result.get("answer"), "steps": result.get("steps"),
            "outcome": outcome, "earned": earned}


# -- SHAPE 2: swarm -- N partners, parallel, warmth-scheduled. Only when the goal fans out.
def swarm(goals: list[dict[str, Any]], *, scope: str, folder: str,
          provider=None, model: str | None = None,
          db_path: str | None = None, max_concurrency: int = 4) -> dict[str, Any]:
    """N equipped partners on independent goals, scheduled by bank warmth via the proven
    fork-field. Each `goals` item: {id, task, role?, rules?, files?}. This is the OLD swarm's
    one good part (parallel equipped agents) -- kept as a CAPABILITY, stripped of the cast-
    composition / DAG-validation ceremony. Use it only when the work genuinely fans out into
    independent partners (e.g. four independent pages). For dependent work, prefer board()."""
    from .fork_field import run_fork_field, make_bank_pager
    from echelon_engine.atoms.store import SeedStore

    if provider is None:
        # If a model was named, route it to ITS provider (the empirical routing table
        # maps e.g. gemini-* -> GeminiProvider). Without this a named non-Grok model was
        # sent to the Grok default and 404'd ("Model not found"). Fall back to the standing
        # Grok loop-driver only when no model was named.
        if model:
            try:
                from echelon_engine.atoms import routing
                provider = routing.provider_for(model)
            except Exception:
                provider, _dm = _default_provider_model()
        else:
            provider, _dm = _default_provider_model()
            model = _dm
    model = model or "grok-4.3"

    store = SeedStore(db_path) if db_path else SeedStore()
    pager = make_bank_pager(store, scope)  # lexical floor ($0) warmth scheduling

    # Build one equipped run_step; the field calls it per goal, scheduled by warmth.
    base_guidance = (f"ECHELON-equipped partner in-scope (`{scope}`). Act directly with your "
                     f"own tools as an operator who lives here. If new ground, prove the "
                     f"smallest slice first.")
    run_step = _make_partner_runner(provider, model, folder, guidance=base_guidance)

    steps = [{"id": g["id"], "agent": g.get("role", "dev"),
              "task": g["task"] + (f"\nRULES: {g['rules']}" if g.get("rules") else ""),
              "deps": []} for g in goals]
    plan = {"steps": steps}

    trace = run_fork_field(plan, run_step=run_step, warmth_of=pager)
    return {"scope": scope, "n": len(goals), "trace": trace}


# -- SHAPE 3: board -- partners coordinating through a shared append-only ledger (DOME-like)
import threading
import time as _time


class _LegacyLedger:
    """The shared coordination structure board() partners work through -- DOME's
    TASK_QUEUE/IntentPool, but for EQUIPPED partners. Three ops, all thread-safe:

      claim(goal_id, partner) -> bool   a partner CLAIMS a record so two don't grab the same
                                        work. First claim wins; a re-claim by another returns
                                        False (the no-double-grab invariant).
      land(goal_id, partner, result)    APPEND the partner's result as an immutable entry. NEVER
                                        overwrites a prior landing -- a second land on the same
                                        record APPENDS a new entry (the append-only invariant).
      read(goal_id=None)                read landed entries (one record's, or all) so a partner
                                        can see others' landed work before acting.
      post(goal_id, by, task, ...)      APPEND a NEW sub-goal onto the board (the plan GROWS on the
                                        ledger -- MiroFish-as-planning, 2026-06-18). A partner that
                                        discovers more work POSTS it instead of doing everything
                                        itself; the field then drains the new record like any other.
      pending()                         the open sub-goals (posted, not yet landed) -- what a draining
                                        field still has to claim. Empty = the plan is complete.

    The log is append-only by construction: `_log` only ever grows, entries are frozen dicts,
    and nothing is mutated in place. This is the honest substrate for coordination -- you can
    replay exactly who claimed/posted/landed what, in order. The POSTED sub-goals are the living
    plan: it is not pre-computed top-down, it accretes as partners discover + post work."""

    def __init__(self, *, scope: str | None = None, books_home: str | None = None) -> None:
        self._lock = threading.Lock()
        self._claims: dict[str, str] = {}          # goal_id -> first partner who claimed it
        self._log: list[dict[str, Any]] = []       # append-only: posts + claims + acts + landings, in order
        self._posted: dict[str, dict] = {}         # goal_id -> the sub-goal record (the living plan)
        self._landed: set[str] = set()             # goal_ids that have a landing
        # the role-BOOKS this ledger's actions are routed into (None until bound).
        self._books: dict[str, Any] | None = None
        self._scope = scope
        if scope is not None:
            self.bind_books(scope, home=books_home)

    def bind_books(self, scope: str, *, roles: list[str] | None = None,
                   home: str | None = None) -> None:
        """Open the per-role BOOKS under ~/.echelon/books/<scope>/ so Ledger.act routes a
        worker's moves into its role's book. Idempotent; called once when board() starts."""
        from .rolebook import RoleBook, CARD_ORDER
        self._scope = scope
        self._books = {r: RoleBook(scope, r, home=home) for r in (roles or CARD_ORDER)}

    def claim(self, goal_id: str, partner: str) -> bool:
        with self._lock:
            if goal_id in self._claims:
                return self._claims[goal_id] == partner  # idempotent for the owner; False for a rival
            self._claims[goal_id] = partner
            self._log.append({"op": "claim", "goal": goal_id, "partner": partner, "t": _time.time()})
            return True

    def land(self, goal_id: str, partner: str, result: Any) -> dict[str, Any]:
        # APPEND-ONLY: never overwrite a prior landing; a re-land appends a new immutable entry.
        with self._lock:
            entry = {"op": "land", "goal": goal_id, "partner": partner,
                     "result": result, "seq": len(self._log), "t": _time.time()}
            self._log.append(entry)
            self._landed.add(goal_id)
            return dict(entry)

    def act(self, goal_id: str, partner: str, role: str, kind: str,
            payload: dict[str, Any] | None = None, *, artifact_ref: str | None = None) -> None:
        """APPEND a worker ACTION (an on_event tick) to the shared coordination log AND
        route it to the role's BOOK (~/.echelon/books/<scope>/<role>.jsonl). This is the
        marriage of DOME's roles to ECHELON's earn-physics: the coordination spine records
        WHO acted on WHAT record (claim/land), the role-book records HOW (the moves), so a
        run leaves a studyable, observable, per-role history instead of scattered temp litter.
        Best-effort on the book (an action is never blocked by a book write failing)."""
        with self._lock:
            self._log.append({"op": "act", "goal": goal_id, "partner": partner, "role": role,
                              "kind": kind, "artifact_ref": artifact_ref, "seq": len(self._log),
                              "t": _time.time()})
        book = self._books.get(role) if self._books is not None else None
        if book is not None:
            try:
                book.record_action(goal_id, partner, kind, payload, artifact_ref=artifact_ref)
            except Exception:
                pass

    def post(self, goal_id: str, by: str, task: str, *, role: str = "dev",
             rules: str | None = None, parent: str | None = None) -> dict[str, Any]:
        """APPEND a NEW sub-goal onto the board -- the plan GROWS on the ledger. A partner that
        discovers more work POSTS it (instead of doing everything in one record); the draining
        field then claims it like any other. Idempotent on goal_id (a re-post of the same id is a
        no-op append-skip) so two partners posting the same discovered sub-goal don't duplicate it.
        `parent` records which record spawned it (the plan is a tree, auditable)."""
        with self._lock:
            if goal_id in self._posted:
                return dict(self._posted[goal_id])   # already on the board -- no duplicate
            rec = {"id": goal_id, "task": task, "role": role, "rules": rules,
                   "by": by, "parent": parent, "seq": len(self._log), "t": _time.time()}
            self._posted[goal_id] = rec
            self._log.append({"op": "post", "goal": goal_id, "partner": by,
                              "task": task, "parent": parent, "seq": len(self._log), "t": _time.time()})
            return dict(rec)

    def pending(self) -> list[dict[str, Any]]:
        """Open sub-goals: POSTED but not yet LANDED -- what a draining field still has to do.
        Empty = the living plan is complete (nothing left posted that hasn't landed)."""
        with self._lock:
            return [dict(r) for gid, r in self._posted.items() if gid not in self._landed]

    def read(self, goal_id: str | None = None) -> list[dict[str, Any]]:
        with self._lock:
            lands = [dict(e) for e in self._log if e["op"] == "land"]
        return [e for e in lands if goal_id is None or e["goal"] == goal_id]

    @property
    def landings(self) -> list[dict[str, Any]]:
        return self.read()


def _legacy_board(goals: list, *, scope: str = "echelon", folder: str | None = None,
          n_partners: int = 2, provider=None, model: str | None = None,
          db_path: str | None = None, cartridges: list[str] | None = None,
          tier_scope: str | None = None,
          simulate_double_land: bool = False, simulate_race: bool = False) -> dict[str, Any]:
    """N craft-equipped partners coordinating through a SHARED append-only Ledger (claim -> act ->
    land -> read others). This is DOME's coordination, but the workers are cartridge-loaded
    partners, not dumb search/replace. Each `goals` item is an id (str) or {id, task, role?, rules?}.

    Real path (folder given, simulate_* off): build one equipped run_step (craft plugged in via
    the cartridge boot the partner runner uses), fan the goals across the field, and have each
    partner claim its record on the ledger before acting + land its result after. Partners read
    the ledger to see landed work -- so dependent work coordinates instead of colliding.

    Test/simulation path (simulate_* flags, or no folder): exercise the LEDGER INVARIANTS without
    spending on live partners -- claim prevents double-grab, land is append-only. This keeps the
    coordination core unit-testable (the dogfood's tests/test_board.py drive exactly this)."""
    items = [{"id": g} if isinstance(g, str) else dict(g) for g in goals]
    # the ledger binds the per-role BOOKS for this scope (brain=bank, books=~/.echelon/books).
    ledger = _LegacyLedger(scope=scope)

    # -- the no-double-grab invariant: two partners race to claim the SAME record; first wins.
    if simulate_race:
        gid = items[0]["id"] if items else "g0"
        first = ledger.claim(gid, "partner-A")
        second = ledger.claim(gid, "partner-B")   # rival re-claim must be refused
        claims_unique = first is True and second is False
        return {"ledger": ledger.read(), "landed": len(ledger.landings),
                "claims_unique": claims_unique, "n_partners": n_partners}

    # -- the append-only invariant: a second land on a record APPENDS, never overwrites.
    if simulate_double_land:
        gid = items[0]["id"] if items else "g0"
        ledger.claim(gid, "partner-A")
        ledger.land(gid, "partner-A", "v1")
        ledger.land(gid, "partner-A", "v2")       # must append, not clobber
        lands = ledger.read(gid)
        append_only_ok = (len(lands) == 2 and lands[0]["result"] == "v1"
                          and lands[1]["result"] == "v2" and lands[1]["seq"] > lands[0]["seq"])
        return {"ledger": ledger.read(), "landed": len(ledger.landings),
                "append_only_ok": append_only_ok, "n_partners": n_partners}

    # -- DRY-RUN (no folder): drive the ledger lifecycle for each goal so the structure is
    # exercised + the shape is returned, without dispatching live partners.
    if not folder:
        for i, it in enumerate(items):
            p = f"partner-{i % max(1, n_partners)}"
            if ledger.claim(it["id"], p):
                ledger.land(it["id"], p, {"status": "dry-run", "task": it.get("task", it["id"])})
        return {"ledger": ledger.read(), "landed": len(ledger.landings),
                "claims_unique": True, "append_only_ok": True, "n_partners": n_partners}

    # -- REAL PATH: equipped partners coordinate through the ledger via the proven fork-field.
    from .fork_field import run_fork_field, make_bank_pager
    from echelon_engine.atoms.store import SeedStore

    if provider is None:
        # If a model was named, route it to ITS provider (the empirical routing table
        # maps e.g. gemini-* -> GeminiProvider). Without this a named non-Grok model was
        # sent to the Grok default and 404'd ("Model not found"). Fall back to the standing
        # Grok loop-driver only when no model was named.
        if model:
            try:
                from echelon_engine.atoms import routing
                provider = routing.provider_for(model)
            except Exception:
                provider, _dm = _default_provider_model()
        else:
            provider, _dm = _default_provider_model()
            model = _dm
    model = model or "grok-4.3"
    _plug = list(cartridges or ["craft"])

    store = SeedStore(db_path) if db_path else SeedStore()
    pager = make_bank_pager(store, scope)
    base_guidance = (
        f"ECHELON-equipped partner in-scope (`{scope}`), craft cartridge plugged in. You share a "
        f"BOARD with the other partners: CLAIM your record before acting, LAND your result when "
        f"done, and READ the board to see what others landed before you build on it. Act directly.")
    run_step = _make_partner_runner(provider, model, folder, guidance=base_guidance)

    from .rolebook import role_of

    # wrap run_step so each goal claims->acts->lands on the shared ledger (the coordination),
    # AND each worker move is recorded into the acting role's BOOK (the earn-physics).
    def _coordinated(step: dict, deps: dict, **kw) -> dict:
        gid = step["id"]
        partner = step.get("agent", "dev")
        role = role_of(step.get("agent", "dev"))   # society-name or DOME-role -> DOME-role
        if not ledger.claim(gid, partner):
            return {"status": "skipped", "answer": "record already claimed by another partner", "steps": 0}
        ledger.act(gid, partner, role, "step_start", {"task": (step.get("task") or "")[:120]})
        res = run_step(step, deps, **kw)
        # the run's own step count + answer become the role-book's record of HOW it acted.
        ledger.act(gid, partner, role, "step_done",
                   {"status": res.get("status"), "steps": res.get("steps"),
                    "answer": (res.get("answer") or "")[:300]},
                   artifact_ref=res.get("folder") or folder)
        ledger.land(gid, partner, {"status": res.get("status"), "answer": (res.get("answer") or "")[:600]})
        return res

    steps = [{"id": it["id"], "agent": it.get("role", "dev"),
              "task": it.get("task", it["id"]) + (f"\nRULES: {it['rules']}" if it.get("rules") else ""),
              "deps": []} for it in items]
    trace = run_fork_field({"steps": steps}, run_step=_coordinated, warmth_of=pager)

    # BRAIN <- BOOK: on a verified outcome, compose the cross-role card and credit the bank
    # (card-primary). DONE-by-disk, not say-so: a board run is OK only if every landed record
    # reports a completed status (a red/skipped landing earns the low stall Q, never a win).
    lands = ledger.read()
    outcome_ok = bool(lands) and all(
        (e.get("result") or {}).get("status") in ("completed", "done", "success")
        for e in lands)
    from .rolebook import compose_card_on_done
    earned = compose_card_on_done(scope, "; ".join(it.get("task", it["id"]) for it in items),
                                  outcome_ok=outcome_ok, books=ledger._books,
                                  tier_scope=tier_scope, db_path=db_path)
    return {"scope": scope, "n": len(items), "ledger": lands,
            "landed": len(ledger.landings), "trace": trace, "_ledger": ledger,
            "outcome_ok": outcome_ok, "earned": earned}


# -- CLI: so the substrate (and any skill) can invoke a partner directly ---------------
# board extraction shim: keep partner imports stable while implementation lives in board.py.
from .board import Ledger as _BoardLedger, run_board as _run_board

Ledger = _BoardLedger


def board(goals: list, *, scope: str = "echelon", folder: str | None = None,
          n_partners: int = 2, provider=None, model: str | None = None,
          db_path: str | None = None, cartridges: list[str] | None = None,
          tier_scope: str | None = None,
          simulate_double_land: bool = False, simulate_race: bool = False) -> dict[str, Any]:
    return _run_board(
        goals,
        scope=scope,
        folder=folder,
        n_partners=n_partners,
        provider=provider,
        model=model,
        db_path=db_path,
        cartridges=cartridges,
        tier_scope=tier_scope,
        simulate_double_land=simulate_double_land,
        simulate_race=simulate_race,
        default_provider_model=_default_provider_model,
        make_partner_runner=_make_partner_runner,
    )


def main() -> None:
    import argparse, json
    ap = argparse.ArgumentParser(
        prog="echelon_engine.agent.partner",
        description="On-demand trusted partner: one equipped agent, one sentence, it acts.")
    ap.add_argument("goal", help="the one-sentence goal")
    ap.add_argument("--scope", required=True, help="project bank scope (the cartridge)")
    ap.add_argument("--folder", required=True, help="repo the partner is sandboxed to")
    ap.add_argument("--rules", default=None, help="constraints (it already half-knows from the bank)")
    ap.add_argument("--pattern", action="append", default=None,
                    help="canonical pattern file to ground the partner (repeatable)")
    ap.add_argument("--role", default="dev")
    ap.add_argument("--model", default=None)
    ap.add_argument("--db", default=None, help="bank db path (default: SeedStore default)")
    ap.add_argument("--max-steps", type=int, default=60)
    ap.add_argument("--cartridge", action="append", default=None,
                    help="extra task-type cartridge scope to plug in alongside the project (repeatable)")
    ap.add_argument("--no-craft", action="store_true",
                    help="do NOT auto-plug the craft (disciplined-dev) cartridge on a coding goal")
    ap.add_argument("--warm-only", action="store_true",
                    help="only print the warm verdict for the goal (no dispatch)")
    args = ap.parse_args()

    if args.warm_only:
        _plug = list(args.cartridge or [])
        if not args.no_craft and "craft" not in _plug and args.scope != "craft":
            _plug.append("craft")
        print(json.dumps(_warm_the_partner(args.scope, args.goal, args.db, cartridges=_plug),
                         indent=2, default=str))
        return

    res = dispatch(args.goal, scope=args.scope, folder=args.folder, rules=args.rules,
                   pattern_files=args.pattern, role=args.role, model=args.model,
                   db_path=args.db, max_steps=args.max_steps,
                   cartridges=args.cartridge, craft=(False if args.no_craft else None))
    print(json.dumps(res, indent=2, default=str))


if __name__ == "__main__":
    main()
