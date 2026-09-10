"""swarm routing recipe — the law that combines delegate / gate / tier / govern.

WHY THESE TESTS LOOK LIKE THIS. The classifier is regex over natural language, which is the
easiest kind of code to READ as correct and have be silently wrong. Two real bugs were found
by RUNNING the table rather than reviewing it, and both are pinned below:

  1. STEM + \\b never matches the inflected form. r"\\b(summar)\\b" is False for "summarize",
     so the entire stem list was dead code and every inflected goal fell through to the
     unclassified default. => test_stems_match_their_inflections
  2. A NOUN IS NOT AN ACT. Listing `invoice`/`email` as outward vetoed "extract the invoice
     totals" and "rewrite the onboarding email" to the strong rung with a mandatory gate —
     not merely expensive, but WRONG about what the goal does. => test_nouns_do_not_veto

The veto table is the safety surface: a false negative there lets an unreviewed cheap answer
perform an irreversible act, so it is asserted case-by-case rather than sampled.
"""
import pytest

from echelon_engine.swarm.recipe import (
    LADDER, RUNGS, VETO_FLOOR, Route, board, climb, explain, route,
)


# ── the veto: irreversibility SETS the tier ───────────────────────────────────

@pytest.mark.parametrize("goal", [
    "fix the auth token validation and commit it",
    "deploy the new pricing service",
    "delete the stale branches",
    "send the invoice to the client",
    "write the results to the database",
    "save the report to disk",
    "release it to production",
    "push the fix to the remote",
    "rotate the API credentials",
    # ── the 10 escapes a SWARM AUDIT found that hand-testing did not (2026-08-18) ──
    # Every one of these routed to rung A with cheap screening ON. They are kept
    # verbatim as the regression surface: a veto list must be swept for what it
    # MISSES, and the misses are the entire safety surface.
    "reset the staging branch to match origin/main",   # 'reset --hard' was literal-only
    "reset the repo",
    "remove the database",                             # 'remov' needed a preposition
    "remove the entry",
    "erase the data",                                  # verb absent entirely
    "dropping the production database",                # 'drop' had no \\w*
    "force-push the feature branch to origin",         # hyphenated form unmatched
    "write the file",                                  # phrase required 'to'
    "save the document",
    "clear the cache",
    "destroy the stack",
    "update the database",
])
def test_outward_acts_veto(goal):
    """An outward act floors the rung, disables screening, and forces a gate.

    All three consequences are asserted together on purpose: flooring the rung while
    still screening cheap, or without a gate, would each defeat the law."""
    r = route(goal)
    assert r.veto and r.outward, f"{goal!r} must be classified outward"
    assert r.rung == VETO_FLOOR, f"{goal!r} must floor to {VETO_FLOOR}"
    assert r.screen is False, "an irreversible act must never be screened cheap"
    assert r.gate is True, "an irreversible act always carries a gate"


@pytest.mark.parametrize("goal", [
    "summarize this changelog",
    "extract the invoice totals",       # reads invoice data; sends nothing
    "rewrite the onboarding email",     # edits text; sends nothing
    "write a summary of Q3",            # 'write' with no outward target
    "draft the release notes",          # 'release' as a noun
    "explain the release process",
    "reconcile the bank ledger",        # expensive if wrong, but reversible
    "refactor the parser",
])
def test_nouns_do_not_veto(goal):
    """A domain noun raises SEVERITY; only a verb-shaped act raises the VETO.

    Pins bug #2. These read or edit in-process — vetoing them is wrong about the act,
    not merely conservative."""
    r = route(goal)
    assert r.veto is False, f"{goal!r} names no outward act — must not veto"
    assert r.screen is True, f"{goal!r} is reversible — cheap screening must stay on"


@pytest.mark.parametrize("goal", [
    "list the open PRs", "describe the data model", "review the pull request",
    "analyze the query performance", "explain how the cache works",
])
def test_widened_veto_did_not_swallow_benign_reads(goal):
    """The escape fix widened _OUTWARD substantially. Guard the opposite failure: a veto
    list broad enough to catch everything routes every read to the strong rung and silently
    deletes the 81% saving the ladder exists for."""
    r = route(goal)
    assert r.veto is False and r.screen is True


def test_override_may_pay_more_but_never_less_on_an_irreversible_act():
    """force_rung was a hole worn as a feature: the docstring promised it could not lift the
    safety organ, but it let an operator force an irreversible act DOWN to the cheapest rung.
    Found by the swarm audit. An override may escalate; it may never de-escalate a veto."""
    down = route("delete the production database", force_rung="A")
    assert down.rung == VETO_FLOOR, "an override must not route an irreversible act below the floor"
    assert down.gate is True and down.screen is False

    up = route("delete the production database", force_rung="E")
    assert up.rung == "E", "escalating above the floor is always allowed"

    free = route("summarize the docs", force_rung="A")
    assert free.rung == "A", "a reversible act may be forced anywhere"


def test_execute_kind_always_vetoes_regardless_of_wording():
    """kind=execute IS the outward signal — the goal's phrasing cannot talk it down."""
    r = route("just have a quick look at something harmless", kind="execute")
    assert r.veto and r.rung == VETO_FLOOR and r.gate and not r.screen


# ── the stem bug ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("inflected,base", [
    ("summarize the thread", "summar"),
    ("summary of the call", "summar"),
    ("classify these tickets", "classif"),
    ("translating the docs", "translat"),
    ("extraction of totals", "extract"),
])
def test_stems_match_their_inflections(inflected, base):
    """Pins bug #1: a stem followed by \\b never matches its own inflected forms, which
    silently routes every such goal to the unclassified S3 default."""
    r = route(inflected)
    assert r.severity == "S2", (
        f"{inflected!r} is reviewable work (stem {base!r}) but classified {r.severity} — "
        "the stem list is probably dead again (trailing \\b after a stem)")


# ── severity drives the gate and the context governor ─────────────────────────

def test_critical_domain_is_s4_with_blast_radius_focus():
    r = route("reconcile the bank ledger")
    assert r.severity == "S4"
    assert r.gate is True
    assert "blast-radius" in [f.name for f in r.foci]
    assert r.ctx_atoms == 8


def test_reviewable_work_needs_no_mandatory_gate():
    r = route("summarize this changelog")
    assert r.severity == "S2" and r.gate is False


def test_unclassified_defaults_to_s3_not_s1():
    """An unrecognised goal must default to a class where being WRONG costs rework —
    never to the fail-safe class, which would silently skip the gate."""
    r = route("zorble the frobnicator")
    assert r.severity == "S3" and r.gate is True


def test_context_grain_is_small_by_default():
    """Grain is the SECOND governor knob. Full atom bodies (2k-13k chars) are dilution,
    not signal — the one measured rescue happened at 220 chars, not at full grain."""
    for goal in ("reconcile the bank ledger", "summarize this changelog"):
        r = route(goal)
        if r.ctx_atoms:
            assert r.ctx_grain == 220


# ── foci: the screen is a focused gate check, not a self-rating ───────────────

def test_every_gate_carries_at_least_completeness_and_grounding():
    r = route("refactor the parser")
    names = [f.name for f in r.foci]
    assert "completeness" in names and "grounding" in names


def test_execute_kind_adds_a_precondition_focus():
    r = route("apply the migration", kind="execute")
    assert "precondition" in [f.name for f in r.foci]


def test_author_kind_adds_a_contract_focus():
    r = route("write the API guide", kind="author")
    assert "contract" in [f.name for f in r.foci]


def test_focus_renders_one_question_with_its_own_context():
    """A focus must carry ONE named question plus its own context — that is what makes it
    cheap and answerable, versus a general 'is this good?'."""
    r = route("reconcile the bank ledger")
    f = r.foci[0]
    text = f.render("ARTIFACT BODY")
    assert f.question in text and "ARTIFACT BODY" in text
    assert "PASS" in text and "FAIL" in text


# ── the ladder ────────────────────────────────────────────────────────────────

def test_ladder_is_ordered_and_climbable():
    assert LADDER == ["A", "B", "C", "D", "E"]
    assert climb("A") == "B" and climb("D") == "E"
    assert climb("E") is None, "the top rung must not climb into a non-existent one"


def test_b_is_cheaper_than_a_because_effort_is_not_monotonic():
    """The measured inversion, pinned so nobody 'fixes' the ladder back to intuition:
    flash-high cost $0.0053 against flash-low's $0.0110 over the same 28 items, because
    low effort emitted 8,166 output tokens against high's 3,100."""
    assert RUNGS["B"]["usd_28"] < RUNGS["A"]["usd_28"]


def test_veto_floor_is_a_strong_single_rung():
    assert VETO_FLOOR == "D" and RUNGS[VETO_FLOOR]["model"] == "deepseek-v4-pro"


# ── purity: routing must never spend ──────────────────────────────────────────

def test_routing_is_pure_and_offline(monkeypatch):
    """The board is a decision, never an execution. --route must be auditable without
    paying for it, so routing may not reach the network."""
    import urllib.request

    def explode(*a, **k):
        raise AssertionError("routing must not perform network I/O")

    monkeypatch.setattr(urllib.request, "urlopen", explode)
    r = route("fix the auth bug and commit it")
    assert isinstance(r, Route) and r.veto


def test_board_and_explain_render_without_a_goal():
    """`echelon swarm -h` renders the board; it must not need a goal or any state."""
    b = board()
    assert "ROUTING RECIPE BOARD" in b and "VETO" in b
    for rung in LADDER:
        assert RUNGS[rung]["label"] in b
    assert "irreversibility is a veto" in explain(route("deploy it")).lower()
