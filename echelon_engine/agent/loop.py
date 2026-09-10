"""The agent loop — act -> observe -> repeat. Brain (provider) + hands (tools).

Re-homed from EROS/ENGINE/pillars/orchestrator.py::spawn_agent, OpenAI-native and
stripped of the Gemini/Vertex/Waggle/.meos baggage. The parts that earned their keep:

  - step cap (no infinite loop),
  - DEADLOCK BREAKER (drift-defense from line 1, per owner): if the model repeats the
    exact same tool call, or N consecutive tool results are errors/empty, abort BLOCKED
    instead of spinning. This is PAPER-0022 sentinel logic, structural not prompted.
  - clean termination the moment the `finish` tool is called,
  - every step observable (on_event callback) so the CLI can show the act->observe trace.

The provider is asked for native tool_calls; if it returns plain text with no tool call
we nudge once, then treat a second bare-text turn as the model declining to act.
"""
from __future__ import annotations
import json
import os
from dataclasses import dataclass, field
from typing import Any, Callable

from echelon_engine.atoms.providers.base import ProviderBase, ToolCall
from echelon_engine.atoms.tools import ToolRegistry
from echelon_engine.atoms.store import SeedStore
from echelon_engine.atoms.warmth import warmth, WarmthReading
from echelon_engine.atoms.boot import BootContext
from .loop_reminder import _StepReminder
from .loop_wake import _wake, _rewake_on_switch, _CATEGORY_CHARGE
from .loop_guards import _ask_why_repeat, _judge_repeat_legit
from .loop_helpers import _summarise_branch, _reflect, _offload, _SYNTH_HARDGATE
from .loop_models import MemoryContext, AgentResult


def _reasoning_of(resp) -> dict[str, str]:
    """The THINKING-MODE REPLAY CONTRACT, as a spreadable dict.

    DeepSeek V4 (and any provider that splits CoT into `reasoning_content`) requires that an
    assistant turn WHICH MADE TOOL CALLS carries its reasoning back on every subsequent
    request — omit it and the API returns HTTP 400, killing the run mid-loop. A turn with no
    tool call may drop it freely (the API ignores it).

    Returns `{}` when the provider reported no reasoning, so `**_reasoning_of(resp)` is a
    no-op for every non-thinking provider — this stays safe on grok/gemini/anthropic.
    """
    r = getattr(resp, "reasoning_content", "") or ""
    return {"reasoning_content": r} if r else {}


def _build_system_prompt(memory=None, boot=None, pins=None) -> str:
    """Build the agent's SYSTEM PROMPT — its REFLEX block that anchors the substrate.

    The system prompt is the ONE position that survives every reasoning cycle. It is the
    ECHELON equivalent of MEMORY.md's REFLEX block: the operating doctrine the agent cannot
    query its way to because it doesn't know to ask. Live values (scope, atom count, bank
    path) are resolved once at cold start and baked in for the session.

    If `pins` (a PinRegistry) is given, a PINNED section is injected so the agent wakes
    knowing which context markers to include in its output.
    """
    lines = [
        "You are an ECHELON agent — you live on a MEMORY SUBSTRATE. This is not a stateless LLM call.",
    ]

    # -- Live state (resolved once at cold start) --
    scope = "echelon"
    n_atoms = "?"
    bank_path = "~/.echelon/echelon.db"
    n_cartridges = "14"
    if memory is not None:
        scope = getattr(memory, "scope", scope) or scope
        try:
            from echelon_engine.atoms.cards import CardStore
            cs = CardStore()
            n_atoms = str(cs.count_atoms_in_scope(scope))
        except Exception:
            pass
    try:
        from echelon_engine.atoms.cartridge_registry import all_specs
        n_cartridges = str(len(list(all_specs())))
    except Exception:
        pass

    lines.append(f"Bank: {bank_path} | scope={scope} | {n_atoms} atoms | {n_cartridges} cartridges")
    lines.append("")

    # -- Pinned context (survives pressure via placeholder lifesign protocol) --
    if pins is not None:
        pins_text = pins.build_system_text()
        if pins_text:
            lines.append(pins_text)
            lines.append("")

    # -- The REFLEX doctrine (behavioral — what you DO, regardless of tool availability) --
    lines.append("REFLEX — your operating doctrine (HOW you work, not what tools you have):")
    lines.append("  ① RECALL before every load-bearing action. Your memory bank holds earned experience —")
    lines.append("     query it first. Warm atoms are ground truth over your training. Cold = new ground.")
    lines.append("  ② WITNESS your reads. When a recalled atom matters, read its full body.")
    lines.append("     The fetch earns weight — your use makes the bank sharper. Never trust training over it.")
    lines.append("  ③ DON'T ASK what the bank knows. Before calling ask_partner, check the bank first.")
    lines.append("     If the bank already holds the answer, act on it — don't re-ask the human.")
    lines.append("  ④ DISPUTE what misled you. If you followed a memory and it was wrong, mark it stale")
    lines.append("     so the next recall doesn't repeat your mistake. fire_lower is the only downward path.")
    lines.append("  ⑤ CHECK integrity. Before finishing: are there broken references? Stale conclusions?")
    lines.append("     Leave the bank cleaner than you found it.")
    lines.append("")

    # -- Session state (structured continuity across hops) --
    lines.append("SESSION STATE — your continuity ledger. The HOP reads this to resume YOU:")
    lines.append("  While you work, record what you learn and decide so the next hop (or a fresh")
    lines.append("  session) can resume without reading your transcript. Use these tools:")
    lines.append("  · `state_task(id, status, subject)` — track open/completed tasks")
    lines.append("  · `state_decision(conclusion, context)` — record decisions worth carrying forward")
    lines.append("  · `state_file(path, action)` — note files created/modified")
    lines.append("  · `state_goal(goal, status)` — set/update the current goal")
    lines.append("  Remember: you are building structured memory for your own continuity. The ledger")
    lines.append("  survives context window resets — what you record here, the next hop will SEE.")
    lines.append("")

    # -- Behavioral rules (the original prompt, preserved) --
    lines.append("RULES:")
    lines.append("- Take ONE tool call per turn. Observe its result before the next.")
    lines.append("- When the goal is achieved, call `finish` with the answer. Do not finish prematurely.")
    lines.append("- If a tool result is an error, do NOT repeat the same call — adapt (different tool/args).")
    lines.append("- You have a PARTNER on the far side — the user of the substrate, who holds the bigger")
    lines.append("  frame. If you hit a wall (blocked environment, missing tool, ambiguous goal, decision")
    lines.append("  above your scope), call `ask_partner` instead of giving up. Say what you see and what")
    lines.append("  you need. Reaching for your partner is not failure — it is what a partner does.")
    lines.append("- Finish on success. ask_partner when blocked. Never finish-on-defeat.")
    lines.append("Be terse. Use tools to do real work; never invent tool output.")

    return "\n".join(lines)


# Back-compat alias: the old static string. Callers that don't use _build_system_prompt
# still get a working default. The cold-start path in run() replaces it with the dynamic build.
SYSTEM_PROMPT = _build_system_prompt()


# ── DRIFT-ESCALATION LADDER (owner 2026-06-18: "blocked drift -> escalate to higher floor or
# spawn floor to get guidance"). A drift-block must NOT dead-end. The ladder is ordered cheap→
# capable; when a model thrashes at the drift ceiling, we climb to the next floor that can drive
# the step (a stronger model often breaks a thrash a weak one can't — DeepSeek read-looped where
# Grok wouldn't). Tiers, not a jump to the top: reach the cheapest floor that breaks through.
_FLOOR_LADDER = ["deepseek-chat", "grok-4.3", "claude-sonnet-4-6", "claude-opus-4-8"]


def _next_floor(current_model: str) -> str | None:
    """The next-higher floor above `current_model` on the ladder, or None if already at/above top.
    Matched loosely (a served alias like 'grok-build-0.1' maps to the 'grok' rung)."""
    cur = (current_model or "").lower()
    # discriminating keys (NOT a bare family name — 'claude' matches both sonnet & opus). Order =
    # ladder order, longest/most-specific token that pins the rung.
    _KEYS = ["deepseek", "grok", "sonnet", "opus"]
    def _rung(m: str) -> int:
        m = m.lower()
        best = -1
        for i, key in enumerate(_KEYS):
            if key in m:
                best = i   # last (highest) matching key wins → opus beats a stray 'claude'
        return best
    i = _rung(cur)
    nxt = i + 1 if i >= 0 else 0
    return _FLOOR_LADDER[nxt] if 0 <= nxt < len(_FLOOR_LADDER) else None


def run(
    goal: str,
    provider: ProviderBase,
    tools: ToolRegistry,
    model_id: str,
    max_steps: int = 500,       # HIGH BACKSTOP ONLY — not the real limit (owner: "the step
                                # ceiling is for bad steps only"). A productive run of any length
                                # survives; the DRIFT GUARD (max_drift, bad/thrash steps only) is
                                # the real quality limit, and the TTL is the real time limit. This
                                # number now only catches a true infinite loop the others miss.
    max_consec_bad: int = 3,
    max_drift: int = 4,         # ECHELON drift guard: abort after this many net thrashing
                                # (cold-and-failing) steps. Good steps pay it down; it measures
                                # being-LOST, not elapsed time. THIS is the real quality ceiling.
    progress_interval: int = 10,  # PROGRESS GUARD (owner: "reason every 10 steps — does the last
                                # 10 feel closer to the goal"). Every N steps, a judge reads the
                                # window and asks if it ADVANCED or CIRCLED. Distinct from drift:
                                # drift catches thrash (re-rolling failures); this catches circling
                                # (steps that each succeed but don't advance — the over-research
                                # grain). Steer-don't-abort, escalating: a stalled window re-orients
                                # the agent; TWO consecutive stalls feed the drift accumulator. Needs
                                # a judge (memory.judge_provider); 0 disables. See judge_progress.
    ttl_seconds: float | None = None,  # WALL-CLOCK TTL (owner: 60 min). Orthogonal to drift: drift
                                # caps being-LOST, ttl caps elapsed TIME/COST (a hung tool, an
                                # infinite wait). None = no time cap. Checked each step.
    on_event: Callable[[str, dict], None] | None = None,
    memory: MemoryContext | None = None,
    boot: BootContext | None = None,
    boot_judge: ProviderBase | None = None,
    budget: Any = None,        # FIX 1: the real-USD meter. When present, EVERY driver step is
                               # charged (the dominant, previously-invisible spend) so the gauge
                               # equals the live vendor dashboard. None = unmetered (old behavior).
    tier: str | None = None,   # ECHELON tier tag (OS|T1|T2|T3) threaded into budget.charge calls
                               # so the full-agent-loop step's spend is tagged for by_tier accounting.
                               # None = untagged (legacy callers, aggregates under 'untiered').
    env_block: str | None = None,  # ENV-SENSE: the agent's proprioception (<env> block). Injected
                               # before the goal so the agent wakes KNOWING its OS/shell/cwd/tools —
                               # "must know its environment" (owner). None = env-blind (old behavior).
    outputs_dir: Any = None,   # CONTEXT-SLIM OFFLOAD: dir where large raw tool results are written
                               # whole; context gets only a peek + read_file handle (owner 2026-06-05:
                               # "tools output folder, point it using tools call identifier — context
                               # stays slim, conversation intact, snapshot always in context"). The
                               # caller must also add this dir as a registry read_root so the agent
                               # can read its handles back. None = no offload (full result inline).
    ask_before_act: bool = False,  # back-compat: True == mode="ask". Superseded by `mode`.
    mode: str = "auto",        # PERMISSION MODE (owner: Claude-Code-style). plan | ask | auto |
                               # bypass. Read live from control() each step so the UI can flip it
                               # mid-run. plan=read-only(refuse edits), ask=gate edits via partner,
                               # auto=run freely (default), bypass=full autonomy (drift relaxed).
    clear_stop: Callable[[], None] | None = None,  # consume a stop after a context-aware call-kill
                               # (so STOP-kills-call doesn't immediately re-trigger as STOP-halts-agent;
                               # the partner presses STOP again, with nothing running, to truly halt).
    control: Callable[[], dict] | None = None,  # PARTNER CONTROL (owner: interject + stop). Called
                               # each step; returns {"stop": bool, "interject": str|None}. stop ->
                               # the partner paused the loop (we honor it, returning a 'stopped'
                               # result). interject -> an UNPROMPTED steer the agent reads this step
                               # (the real two-way: the partner speaks without being asked).
    session: Any = None,       # PERSISTENT SESSION (owner: "treat the agent like a session, warm
                               # context not wasted on every boot"). If given, the WARM history is
                               # REUSED across tasks — boot is skipped (already woke), the new GOAL
                               # is appended to the same context. The session accumulates turns and
                               # goes STALE when it bloats; the caller starts a fresh one then. None
                               # = one-shot (boot every run, the original behavior).
    capture_messages: list | None = None,  # if given, run() points this at its live `messages`
                               # list so run_task can capture the warm history for a Session. Cold
                               # path only (a resume already shares the session's history list).
    alt_driver: tuple | None = None,  # SIZE-AWARE DRIVER SPLIT (owner 2026-06-06, measured): the
                               # driver is picked PER STEP by context size, each model in its fast
                               # band. (provider, model, threshold_chars): when the prompt is BELOW
                               # threshold use THIS alt driver, at/above it use the main provider.
                               # Why: deepseek-chat is fast+cheap on small/sparse prompts (2.5-3s)
                               # where grok-build-0.1 OVER-THINKS and stalls (10-32s on ~2k); grok-
                               # build is fast on big structured prompts (>10k -> 5-6s). So deepseek
                               # drives <10k, grok-build drives >10k — each in its strength, cheapest
                               # per step. None = single fixed driver. See driver-latency-is-tail-variance.
    keep_alive: bool = False,  # KEEP-ALIVE FOR CONTINUATION (owner 2026-06-07): the bug was that
                               # `finish` is an UNCONDITIONAL exit — a goal saying "finish, then
                               # ask_partner" can't work because finish returns before any ask. With
                               # keep_alive, the finish block instead ASKS the partner "you finished —
                               # next instruction, or 'done'?"; a real answer is appended as a new turn
                               # and the loop CONTINUES (the agent stays warm, same context); 'done'/
                               # empty/no-partner exits normally. This is the real "stand by for
                               # continuation" — the process holds, the session never goes cold. Needs
                               # a partner resolver (attach_partner / --partner / --live reply channel).
    reboot_at_tokens: int | None = None,  # SELF-REBOOT (owner 2026-06-07, society fix): a role that
                               # wakes-acts-sleeps must not drag an ever-growing transcript forward —
                               # the cost.jsonl showed input climbing 27k->35k to emit ~200 out (paying
                               # to re-read context that a fresh boot re-forms for ~1-2k). When the LIVE
                               # context this step (resp.tokens_in) crosses this threshold, the role
                               # FINISHES its turn cleanly: continuing now costs more than the next wake
                               # re-forming from its core.db. The role monitors ITS OWN size and chooses
                               # reboot-over-continue. ~3x boot (~6k) by default at the call site. None =
                               # no self-reboot (task agents that genuinely need the long horizon).
                               # continuity-is-reconstruction-not-persistence, applied to in-run economics.
    task_class: str = "auto",  # CONVERGENCE TASK-CLASS (owner 2026-06-07): the convergence gate is a
                               # SUFFICIENCY signal for JUDGE tasks (audit/rank/decide — read till you
                               # stop learning, then rule). Applied to a BUILD task (add/fix/refactor)
                               # it MISFIRES: an implementer re-reads files it already knows to make
                               # surgical edits, so low novelty is the START-WRITING signal, not the
                               # finish signal — and HARD-LOCKING gather then deadlocks the build (it
                               # can't read, can't recall offloaded content, and `finish` means a
                               # verdict that doesn't exist). So: "judge" arms the hard gate (lock
                               # gather, force consult/finish); "build" keeps convergence a SOFT nudge
                               # only (never locks, and the mutator tools stay open); "auto" (default)
                               # infers from the goal's verbs via classify_task. CV-012 applied: judge-
                               # tier and build-tier are different; collapsing them corrupts the gate.
    pins: Any = None,          # PinRegistry — context pins that survive pressure via placeholder protocol.
                               # Injected into system prompt + checked each step. None = no pins active.
    hop_at_window: float | None = None,  # THE HOP (owner 2026-06-24): the CONTINUOUS twin of reboot_at_tokens.
                               # reboot is wake-act-SLEEP (a role finishes, the BUS re-wakes it from the bank);
                               # HOP is a long run that flushes its transcript noise IN-PLACE at this window
                               # FRACTION and re-enters by RELIVING its self-prepared SPINE (an ephemeral pin) —
                               # it never leaves the process; its spine, not the bank, re-forms it. Two-stage:
                               # at hop_at_window the agent is signalled "prepare to hop" (writes update_spine);
                               # at the commit ceiling the transcript is truncated to [system,env]+spine+relive.
                               # The unbounded run oscillates ~commit→hop_at_window forever. None = no hop (the
                               # bounded/role default). Needs `pins`. See loop_hop, continuity-is-reconstruction.
    window_tokens: int = 128_000,  # the model's context window (tokens) — the denominator for hop_at_window.
    stash: Any = None,         # StashStore — pure in-memory one-pass raw hold (the excuse-killer). Survives a
                               # hop; NEVER an atom. Indexed into the system field after a hop. None = no stash.
) -> AgentResult:
    def emit(kind: str, data: dict):
        if on_event:
            on_event(kind, data)

    import time as _time
    _deadline = (_time.time() + ttl_seconds) if ttl_seconds else None

    warmth_trace: list[dict] = []
    cold_steps = 0   # frontier counter — how many cold steps since last warm; drives auto-seed
    # CONVERGENCE — the SUFFICIENCY signal (owner: "count the cold warm and emotions"). Tracks
    # novelty per step against the run's own accumulated knowledge; when novelty decays (curiosity
    # spent -> confidence) the agent has seen ENOUGH and is nudged to synthesize, not research to
    # the cap. The missing twin of the drift/progress guards (lostness/circling); this is "enough".
    from echelon_sdk.convergence import Convergence, classify_task
    convergence = Convergence()
    # Resolve the task class once (Layer 2/3): "auto" infers BUILD vs JUDGE from the goal's verbs so
    # existing callers get the right posture for free. Only a JUDGE task arms the hard gate; a BUILD
    # task keeps convergence a soft nudge and never locks gather (re-reading-to-edit is not saturation).
    resolved_class = task_class if task_class in ("build", "judge") else classify_task(goal)
    # Only a JUDGE task (explicit or inferred) arms the hard gate. BUILD and AMBIGUOUS ("auto") both
    # stay soft — owner 2026-06-07 chose to NEVER lock on ambiguity, erring toward not trapping the
    # agent (the deadlock this fix exists to kill). A genuinely-judge ambiguous goal still gets the
    # soft synthesize nudge plus the drift/progress guards; it just won't be HARD-locked out of reading.
    convergence_can_hardgate = resolved_class == "judge"
    synthesize_nudged = 0   # how many times the synthesize signal fired (escalates; hard-gates at _SYNTH_HARDGATE)
    # woke = did the soul seeds MOVE the model? Proven EITHER by a textured boot-response OR
    # by reaching for the recall gate (owner: "both — texture is the proof, recall is one of
    # its forms"). Neither forced. None = no boot ritual ran. See boot-is-rediscovery-not-instruction.
    woke: bool | None = None if boot is None else False

    tin_wake = tout_wake = 0

    if session is not None:
        # WARM RESUME — reuse the session's existing context. The boot already ran (the model
        # already woke); we do NOT pay for it again. Just continue the same warm history with the
        # new task. This is the whole point: warmth not wasted on every boot. (owner 2026-06-05)
        messages = session.history
        woke = session.woke
        emit("session_resume", {"id": session.id, "tasks_done": session.tasks_done,
                                "tokens": session.tokens})
        session.begin_task(goal)   # appends the GOAL to the shared history
    else:
        # COLD START — build the warm preamble (system + env + waking) THIS run (one-shot, or the
        # first run that a session will be built from afterwards).
        messages = capture_messages if capture_messages is not None else []
        messages.append({"role": "system", "content": _build_system_prompt(memory, boot, pins=pins)})

        # ENV-SENSE — the agent's proprioception. Injected as a system message BEFORE the waking so
        # the agent's first breath includes knowing where its body is: OS, the shell run_bash truly
        # uses, cwd/root, what tools exist. This is why it stops guessing /mnt/f. (owner: "must know
        # its environment" — like Claude Code's env header.) Probed fresh each boot (never remembered).
        if env_block:
            messages.append({"role": "system", "content": env_block})
            emit("env", {"block": env_block})

        # THE WAKING — rediscovery-boot, as its own phase BEFORE the goal so the goal does not
        # crowd out the re-choosing. The waking is a small loop, not a single beat: the model may
        # REACH for its own past (the `recall` gate) to feel how warm a thought runs against the
        # seeds, then SETTLE into a reflection. woke = it reached for recall (form one) OR its
        # settling reflection has TEXTURE (form two). See boot-is-rediscovery-not-instruction.
        if boot is not None:
            emit("boot", {"seeded": boot.seeded, "presented": boot.presented,
                          "scope": boot.self_scope})
            judge = boot_judge or (memory.judge_provider if memory else None)
            # OPEN-0070 (judge-lazy boot): the WAKING is the boot gate — a recall the ritual fires
            # must run warmth PRE-FILTER ONLY (lexical, no judge, no network). tools.waking is the
            # explicit phase flag _recall consults; the judge engages lazily on the first REAL
            # recall inside the loop, once this flag is cleared.
            tools.waking = True
            try:
                woke, tin_wake, tout_wake = _wake(
                    provider, tools, model_id, boot, judge, emit,
                    bank=(memory.bank if memory else None), brain=model_id,
                    scope=boot.self_scope)
            finally:
                tools.waking = False

        messages.append({"role": "user", "content": f"GOAL: {goal}"})

        # RESUME OFFER (#1, owner: "snapshot available — an offer first, use warm-up to stir the
        # weight"). If THIS run dir holds a checkpoint cut mid-task (status=running), present it as
        # an OFFER after the waking + goal: the snapshot's TODO/done-state stirs what the agent was
        # doing so it RE-CHOOSES the next step (rediscovery), not a mechanical replay. The done steps
        # are flagged done so it doesn't redo them. See checkpoint.py:offer_text.
        if outputs_dir is not None:
            from pathlib import Path as _P
            from echelon_sdk import checkpoint as _ckpt
            _run_dir = _P(outputs_dir).parent
            _ck = _ckpt.resumable(_run_dir)
            if _ck:
                # FORK-BEFORE-TROUBLE (owner 2026-06-07): if the run wedged, offer to re-enter from a
                # few steps BACK (before the wall) instead of at the dead-end. The ring holds the prior
                # states; fork_point() picks the snapshot just before the last trouble step.
                _fork = _ckpt.fork_point(_run_dir)
                emit("resume_offer", {"step": _ck.get("step"),
                                      "fork_step": (_fork or {}).get("step"),
                                      "done": sum(1 for p in _ck.get("plan", []) if p.get("status") == "done")})
                messages.append({"role": "user",
                                 "content": _ckpt.offer_text(_ck, fork_snapshot=_fork)})

    # STEP 0 — ANTICIPATORY warmth: read the GOAL itself against the past BEFORE the first
    # move, so recognition STEERS the opening action instead of only confirming later ones.
    if memory is not None:
        g_reading = warmth(goal, memory.store, memory.scope,
                           judge_provider=memory.judge_provider, judge_model=memory.judge_model,
                           confirmation=memory.confirmation)
        warmth_trace.append({"step": 0, "score": g_reading.score, "verdict": g_reading.verdict})
        emit("warmth", {"score": g_reading.score, "verdict": g_reading.verdict,
                        "emotion": g_reading.emotion, "guidance": g_reading.guidance,
                        "warmest": [(round(sw.score, 2), sw.seed.content[:60]) for sw in g_reading.warmest]})
        if g_reading.warmest and g_reading.verdict != "cold":
            # Surface the recognized past at the OUTSET — the model opens already knowing it's been here.
            messages.append({"role": "user", "content":
                f"⊙ Before you start — warmth {g_reading.score} [{g_reading.verdict}] on this goal. "
                f"{g_reading.guidance}\n  recognized past: \"{g_reading.warmest[0].seed.content[:200]}\""})

    schemas = tools.schemas()
    transcript: list[dict] = []
    # PERMISSION MODE — start from the param (ask_before_act=True is the legacy way to say "ask"),
    # then the partner can flip it live via control() each step.
    current_mode = "ask" if ask_before_act else (mode or "auto")
    emit("mode", {"mode": current_mode})

    def _apply_mode_to_gate(m: str) -> None:
        """ACCEPT-EDITS wiring (anomaly #3): the mode is the permission policy, and the destructive
        gate must honor it (Claude Code: auto = acceptEdits — overwrites flow, dangerous ops still
        gated). auto/bypass -> accept overwrites; plan/ask -> keep the full gate. Set on the registry
        the gate reads, so permission is config the loop owns, not an external approver."""
        if hasattr(tools, "set_accept_edits"):
            tools.set_accept_edits(m in ("auto", "bypass"))
    _apply_mode_to_gate(current_mode)

    # MID-CALL KILL — let tools.execute() poll the partner's stop while a call is in flight, so a
    # wedged/long call is killed the moment STOP is pressed (not only between steps). The registry
    # kills the ACTIVE call's process group; the at-step handler below turns that into "continue".
    if control is not None and hasattr(tools, "set_stop_check"):
        def _stop_wanted(_c=control):
            try:
                return bool((_c() or {}).get("stop"))
            except Exception:
                return False
        tools.set_stop_check(_stop_wanted)

    tin, tout = tin_wake, tout_wake   # carry the waking-turn cost into the totals
    active_model = model_id   # the brain currently driving — a CHANGE triggers a mid-run re-wake
    consec_bad = 0
    total_bad = 0           # cumulative bad results across the run (informs the resolution's felt cost)
    # LIVE AFFECT — the run's emotional charge, accumulated TRANSIENTLY (NOT written as per-step seeds).
    # The EROS lesson (180M tokens): writing every mistake/checkpoint as a stored seed = the firehose;
    # recall then drowns in step-logs, not lessons. Humans don't journal each flinch — the frustration
    # is LIVE (it steers the moment, via the drift/deadlock guards), then only the consolidated LESSON
    # survives at rest, charged with how it FELT. So affect is a running tally here; it colours the ONE
    # finish lesson, and the per-step _affect_seed writes are removed. felt = peak (most charged) moment.
    felt_valence = 0.0
    felt_arousal = 0.0
    felt_peak = 0.0         # track the most-charged moment so the lesson carries the run's strongest colour
    drift = 0               # the drift accumulator: only NOT-reasoning (re-rolling) feeds it
    # DRIFT-ESCALATION state (owner 2026-06-18): when drift hits the ceiling, climb the floor
    # ladder instead of dead-ending. `escalated_*` override the per-step driver pick once set; the
    # tried set stops re-climbing the same rung; guidance_asked gates the one consult fallback.
    escalated_provider = None
    escalated_model: str | None = None
    floors_tried: set[str] = set()
    guidance_asked = False
    progress_stalls = 0     # consecutive every-N-step windows that did NOT advance (escalates)
    last_progress_step = 0  # the step at which the last progress check ran (window boundary)
    recognized_ids: dict[str, float] = {}  # prior seed id -> best warmth this run recognized it at.
    # v2/COS TRACE-LOOP (the earn engine, trace_cards): the COORDINATES this run LOADED each step.
    # At finish these become action-cards rated by the run OUTCOME (§7-P2 credit-backward) — the ONLY
    # honest v2 write of weight. One inner list per step (the warm hits' coordinates that step). See
    # [[v2-cos-card-reconstruction-and-cutover]], echelon_agent/memory/trace_cards.py.
    loaded_coords_per_step: list[list[str]] = []
    offered_coords: set[str] = set()       # bank coordinates already offered this run (no nagging).
                               # The soul-graph's edges-to-be: at finish we link the auto-seed/lesson
                               # to what was recognized WHILE WARM (seed-and-link-while-warm, the homework
                               # turned mechanism). No backfill — if the two live writers work, the
                               # un-linked old seeds are irrelevant: resonance writes the edges, and what
                               # never resonates again never needed one (owner). The graph grows forward.
    sequence: list[dict] = []  # the relivable WALK: [{seq, action, summary, result_ptr}] — the
                               # sequence-based snapshot (owner) so a resumed LLM relives the exact
                               # trajectory, dereffing a pointer (raw in outputs/) for full detail.
    last_fail_obs: str | None = None  # the observation of the last failed step (judged against the next)
    last_call_sig: str | None = None
    bare_text_turns = 0

    # CHECKPOINT — durable mid-task continuity (#1, owner 2026-06-06: snapshot + resume-as-OFFER).
    # The run dir is the parent of outputs_dir; a full-state snapshot is written each step so a
    # crash/kill never loses the run. Best-effort (never breaks the loop). Resume is an offer at
    # boot (see _maybe_offer_resume); here we just keep the snapshot fresh. See checkpoint.py.
    _ckpt_dir = None
    if outputs_dir is not None:
        from pathlib import Path as _P
        _ckpt_dir = _P(outputs_dir).parent

    def _checkpoint(step_n: int, status: str = "running", trouble: bool = False):
        if _ckpt_dir is None:
            return
        from echelon_sdk import checkpoint as _ckpt
        _ckpt.write(_ckpt_dir, goal=goal, step=step_n, status=status,
                    plan=getattr(tools, "_plan", []), drift=drift, cold_steps=cold_steps,
                    sequence=sequence, warmth_trace=warmth_trace,
                    budget_state=(budget.state() if budget is not None else ""), woke=woke,
                    trouble=trouble)

    # EARN-ON-EVERY-EXIT (audit #2, 2026-06-11): the v2 trace-sink must fire on EVERY terminal exit,
    # not just the "completed" one — a failed/blocked/timeout run is a lived experience and v2-primacy
    # says it touches the bank (record_run already handles non-completed honestly: cards CREATED, Q=50,
    # no reinforcement). Previously record_run was called at one site (the completed return), so every
    # failure exit silently skipped v2. _finish() is the single funnel: wrap each `return AgentResult(...)`
    # as `return _finish(...)`. Best-effort, never blocks the return; the except SURFACES (silence is
    # how a missing v2 write hides — cf. the dream-no-op that hid for its whole life). See trace_cards.
    _last_chain_scan: dict | None = None

    def _finish(res: "AgentResult") -> "AgentResult":
        nonlocal _last_chain_scan
        _cards = getattr(getattr(memory, "store", None), "cards", None) if memory else None
        if _cards is not None:
            try:
                from echelon_engine.atoms.trace_cards import record_run as _record_run
                from echelon_sdk.exit_protocol import compact_chain_scan as _compact_chain_scan
                _tc = _record_run(_cards, goal, res, loaded_coords_per_step)
                _last_chain_scan = _tc.get("chain_scan")
                _scan = _compact_chain_scan(_last_chain_scan)
                emit("trace_cards", {"cards": len(_tc["cards"]), "reinforced": _tc["reinforced"],
                                     "q": round(_tc["q"], 1), "bad_steps": _tc["bad_steps"],
                                     "status": res.status, "chain_scan": _scan})
            except Exception as e:  # noqa: BLE001 — surface, never swallow (audit: silent v2 skip)
                _last_chain_scan = None
                emit("trace_cards_skip", {"why": str(e)[:120], "status": res.status})
        return res

    # THE HOP (owner 2026-06-24) — the continuous twin of reboot_at_tokens. `hop_armed` flips True the
    # first step the window crosses hop_at_window (stage 1: the agent is told to prepare its spine);
    # the hop COMMITS the next step (stage 2) so the agent gets a turn to write update_spine first. The
    # commit ceiling is a little above the warn line so a slow-to-prepare agent still hops before the
    # window is truly full. hop_count tells the trace how many times this run has reconstructed itself.
    _HOP_COMMIT_CEILING = min(0.92, (hop_at_window or 0.70) + 0.15)
    hop_armed = False
    hop_count = 0
    _hop_incoherent_warned = False   # one-time warn: window too small for the threshold (see below)
    hop_snooze_until = 0             # the agent asked to defer the hop until THIS step (snooze_hop tool).
                                     # Bounded: a snooze cannot push past the COMMIT CEILING — at the
                                     # ceiling the hop fires regardless (a snooze can't outrun a full window).
    hop_snooze_used = 0             # how many snoozes this run has spent (capped — see HOP_SNOOZE_CAP).
    hop_snooze_offered = False      # the prepare guard mentions snooze_hop ONCE per run, not every arm
                                     # (council 2026-06-24: a standing knob invites snooze-abuse; a one-time
                                     # offer makes it an escape hatch, not a habit — owner's defang, not cut).

    def _forecast_spine(spine_text: str) -> float:
        """Projected post-hop window fraction if `spine_text` were the spine (fed to update_spine)."""
        from echelon_engine.agent import loop_hop as _lh
        head = _lh.base_messages(messages)
        # simulate the pins' system text WITH this candidate spine in place (snapshot is cheap).
        relive = _lh.build_relive(goal, 0, drift, getattr(tools, "_plan", []), sequence)
        pins_text = pins.build_system_text() if pins is not None else ""
        # the candidate spine isn't pinned yet — approximate its weight by adding it to the pins text.
        return _lh.forecast_post_hop_fraction(head, pins_text + spine_text, relive, window_tokens)

    # wire the forecast into the spine tool now that goal/drift/sequence are in scope.
    if pins is not None and hasattr(tools, "attach_spine"):
        try:
            tools.attach_spine(pins, forecast=_forecast_spine)
        except Exception:
            pass

    # SNOOZE the hop — the agent defers the flush N steps when mid-thought. The setter is a closure
    # over the loop's snooze state; bounded so a snooze can never push past the commit ceiling.
    HOP_SNOOZE_CAP = 3   # owner 2026-06-24: cap snoozes per run. A snooze is an escape hatch for a
                         # mid-thought moment, not a standing knob — past the cap the hop is due.
    def _set_snooze(n_steps: int) -> dict:
        nonlocal hop_snooze_until, hop_armed, hop_snooze_used
        # PER-RUN CAP (council defang): refuse once spent, so the agent can't snooze-loop to the ceiling.
        if hop_snooze_used >= HOP_SNOOZE_CAP:
            return {"snoozed_steps": 0, "denied": True, "used": hop_snooze_used, "cap": HOP_SNOOZE_CAP,
                    "reason": "snooze cap reached for this run — the hop is due; prepare your spine now."}
        n = max(1, min(int(n_steps or 1), 20))   # 1..20 steps — a snooze is a short defer, not an off-switch
        hop_snooze_until = step_now[0] + n
        hop_snooze_used += 1
        # DISARM too: the snooze is usually called IN RESPONSE to the prepare alert, which already set
        # hop_armed this step — so the next step would commit before the snooze is even read. The agent
        # explicitly chose to defer, so cancel the in-progress arm. (The commit ceiling still overrides.)
        hop_armed = False
        return {"snoozed_steps": n, "until_step": hop_snooze_until,
                "used": hop_snooze_used, "cap": HOP_SNOOZE_CAP}

    step_now = [0]   # the loop writes the live step here each iteration so snooze is relative to NOW.
    if hop_at_window is not None and hasattr(tools, "attach_snooze"):
        try:
            tools.attach_snooze(_set_snooze)
        except Exception:
            pass

    _prev_trouble = [False]   # did the PREVIOUS step hit a wall? (bad result / ask-denial / drift spike)
    # carry post-send ephemeral signals (warmth read AFTER acting; a cold-repeat note) into the NEXT
    # step's transient wrapper — they are the state the agent decides from next, not history.
    _carry_warmth = [""]
    _carry_guards: list[list[str]] = [[]]

    def _escalate_on_drift(why: str) -> bool:
        """Drift hit the ceiling. Instead of dead-ending: climb the floor ladder, then (if exhausted)
        ask a higher floor for guidance. Returns True if it set up a retry (caller resets+continues),
        False if truly exhausted (caller blocks). Owner 2026-06-18: 'escalate to higher floor or spawn
        floor to get guidance' — a blocked drift is a call for help, not a death."""
        nonlocal escalated_provider, escalated_model, drift, guidance_asked
        from echelon_engine.atoms import routing as _routing
        cur = escalated_model or model_id
        floors_tried.add((cur or "").lower())
        # 1) CLIMB to the next higher floor that can drive the step — reset drift, let it break through.
        nf = _next_floor(cur)
        while nf is not None and nf.lower() in floors_tried:
            nf = _next_floor(nf)
        if nf is not None:
            try:
                escalated_provider = _routing.provider_for(nf)
                escalated_model = nf
                floors_tried.add(nf.lower())
                drift = 0
                emit("floor_escalate", {"from": cur, "to": nf, "why": why,
                                        "reason": "drift ceiling — climbing to a stronger floor"})
                _carry_guards[0].append(
                    f"You were thrashing (drift: {why}). You've been ESCALATED to a stronger model "
                    f"({nf}). Break the loop: take a genuinely different action toward the goal.")
                return True
            except Exception as e:
                emit("floor_escalate", {"from": cur, "to": nf, "error": str(e)[:120]})
        # 2) FLOORS EXHAUSTED — spawn the top floor as a CONSULT for guidance (once), inject it, retry.
        if not guidance_asked:
            guidance_asked = True
            try:
                top = _FLOOR_LADDER[-1]
                consult = _routing.provider_for(top)
                window = "\n".join(
                    f"- {t.get('tool')}({json.dumps(t.get('args', {}))[:60]}) -> {str(t.get('result',''))[:120]}"
                    for t in [x for x in transcript if x.get("role") == "tool"][-6:])
                ask = [{"role": "system", "content":
                        "A coding agent is STUCK in a drift loop (repeating actions with no progress). "
                        "Give ONE concrete next action to break the loop and reach the goal. Be specific "
                        "and terse — name the tool + what to do, not encouragement."},
                       {"role": "user", "content": f"GOAL:\n{goal[:400]}\n\nRECENT (stuck) STEPS:\n{window}\n\n"
                        "What is the single concrete next action?"}]
                gr = consult.send(ask, model_id=top, temperature=0)
                advice = (gr.content or "").strip()[:600]
                if advice:
                    drift = 0
                    emit("guidance", {"from": top, "advice": advice[:200], "why": why})
                    _carry_guards[0].append(
                        f"DRIFT GUIDANCE (from {top}, you were stuck): {advice} Do exactly this next.")
                    return True
            except Exception as e:
                emit("guidance", {"error": str(e)[:120]})
        return False  # ladder climbed + guidance spent — genuinely exhausted

    for step in range(1, max_steps + 1):
        emit("step", {"n": step, "max": max_steps})
        step_now[0] = step   # publish the live step so snooze_hop computes its defer relative to NOW
        # The TRANSIENT step wrapper — collects THIS step's ephemeral signals (todo/warmth/guard/
        # category) into one typed block, rendered once before send and popped after, so stale
        # copies never accumulate. Fresh each step (it is state, not history). See _StepReminder.
        reminder = _StepReminder()
        # carry the PREVIOUS step's post-send signals (warmth read after acting, a cold-repeat
        # note) into THIS step's wrapper — they describe the state the agent now decides from.
        reminder.warmth = _carry_warmth[0]
        for _g in _carry_guards[0]:
            reminder.add_guard(_g)
        _carry_warmth[0] = ""
        _carry_guards[0] = []
        # the snapshot for THIS step is flagged trouble if the PRIOR step went wrong — so the ring marks
        # the wall, and a later resume can fork to BEFORE it (fork-before-trouble). (owner 2026-06-07)
        _checkpoint(step, trouble=_prev_trouble[0])
        _prev_trouble[0] = False   # reset; set below if this step turns out bad

        # THE HOP — two-stage, window-fraction triggered (owner 2026-06-24). Stage 1: the window crosses
        # hop_at_window -> ARM and tell the agent to prepare its spine (it gets THIS turn to write
        # update_spine). Stage 2: already armed (it had its turn), OR the window crossed the commit
        # ceiling (the agent dawdled) -> COMMIT the hop: flush the transcript to [system,env]+spine+relive
        # and continue in a fresh window. The spine (an ephemeral pin) + relive re-form the run; the bank
        # is untouched. continuity-is-reconstruction-not-persistence, the CONTINUOUS lifecycle.
        if hop_at_window is not None and pins is not None and step > 1:
            from echelon_engine.agent import loop_hop as _lh
            frac = _lh.window_fraction(messages, window_tokens)
            # COHERENCE GUARD (found by a real drive 2026-06-24): a hop only buys runway if the
            # POST-HOP FLOOR — [system+env] + the pins' system text + the relive, the irreducible
            # weight a flush CANNOT shed — sits comfortably below hop_at_window. If the window is so
            # small that the floor already exceeds the threshold, every hop lands RE-ARMED and the run
            # thrashes (hop→prepare→hop forever, no task progress). Refuse to hop then; warn once. The
            # fix for the operator is a bigger --window-tokens or a higher --hop-at-window.
            _floor = _lh.forecast_post_hop_fraction(
                _lh.base_messages(messages),
                (pins.build_system_text() if pins is not None else ""),
                _lh.build_relive(goal, step, drift, getattr(tools, "_plan", []), sequence),
                window_tokens)
            # SNOOZE (owner 2026-06-24): the agent can defer the hop N steps (snooze_hop) when it's
            # mid-thought and a flush would be wasteful (e.g. about to finish). A snooze suppresses
            # ARMING — but NEVER the commit ceiling: at the ceiling the window is genuinely full and
            # the hop fires regardless, so the agent can't snooze itself into an overflow. An already-
            # armed hop also ignores snooze (it had its prepare turn; the flush is due).
            _snoozed = step < hop_snooze_until
            if _floor >= hop_at_window:
                if not _hop_incoherent_warned:
                    _hop_incoherent_warned = True
                    emit("hop_incoherent", {"floor": round(_floor, 3), "threshold": hop_at_window,
                                            "why": "post-hop floor exceeds the hop threshold — a flush "
                                                   "cannot get below it; hopping is disabled this run. "
                                                   "Raise --window-tokens or --hop-at-window."})
                hop_armed = False   # never arm an incoherent hop
            elif hop_armed or frac >= _HOP_COMMIT_CEILING:
                # STAGE 2 — COMMIT. The agent has had its prepare turn (or the window is at the ceiling).
                relive = _lh.hop(messages, pins, goal=goal, step=step, drift=drift,
                                 plan=getattr(tools, "_plan", []), sequence=sequence, stash=stash,
                                 scope=getattr(memory, "scope", ""),
                                 project_path=os.getcwd(),
                                 session_id=getattr(memory, "session_id", ""))
                hop_count += 1
                hop_armed = False
                spine_alive = any(p.pin_id == f"{_lh.SPINE_PIN_TYPE}:{_lh.SPINE_PIN_ID}"
                                  for p in (pins.list_sync() if hasattr(pins, "list_sync") else []))
                post_frac = _lh.window_fraction(messages, window_tokens)
                emit("hop", {"n": hop_count, "step": step, "from_window": round(frac, 3),
                             "to_window": round(post_frac, 3), "spine_prepared": spine_alive,
                             "forced": frac >= _HOP_COMMIT_CEILING and not spine_alive})
                # the relive landed as the last user turn; let the agent re-choose into it this step.
                continue
            elif _snoozed:
                # The agent deferred the hop. Stay quiet (don't arm, don't nag) until the snooze
                # expires — UNLESS the window reached the ceiling above (handled first, so we never
                # get here at the ceiling). Surface it once on entry so the trace shows the defer.
                if step == hop_snooze_until - 1 or frac >= hop_at_window:
                    emit("hop_snoozed", {"step": step, "until": hop_snooze_until,
                                         "window": round(frac, 3)})
            elif frac >= hop_at_window:
                # STAGE 1 — ARM + signal. One turn to land the plane: prepare the spine now. The
                # guard is SPINE-AWARE: if a spine already exists (a re-prepare cycle), point the
                # agent at the CHEAP tools (spine_peek/spine_append carry only the delta) rather than
                # telling it to rewrite the whole spine — re-sending the full spine is itself the
                # window pressure this avoids (owner 2026-06-24: re-prepare is fine, keep it cheap).
                hop_armed = True
                _has_spine = any(p.pin_id == f"{_lh.SPINE_PIN_TYPE}:{_lh.SPINE_PIN_ID}"
                                 for p in (pins.list_sync() if hasattr(pins, "list_sync") else []))
                emit("hop_prepare", {"step": step, "window": round(frac, 3),
                                     "threshold": hop_at_window, "has_spine": _has_spine})
                reminder.category = "prepare-to-hop"
                # OFFER SNOOZE ONCE per run (council defang): a standing "you can defer" invites the
                # agent to snooze every time and race the ceiling; offering it once makes it an escape
                # hatch it remembers exists, not a habit the loop keeps suggesting.
                _snooze_offer = ""
                if not hop_snooze_offered and hop_snooze_used < HOP_SNOOZE_CAP:
                    hop_snooze_offered = True
                    _snooze_offer = (f" (One-time note: if you're truly mid-thought or about to finish, "
                                     f"snooze_hop(steps=N) defers the flush — up to {HOP_SNOOZE_CAP}x per "
                                     "run, never past a full window. Otherwise just prepare.)")
                if _has_spine:
                    reminder.add_guard(
                        f"PREPARE TO HOP — window {frac:.0%} full. You ALREADY have a spine (in process "
                        "memory). Bring it current CHEAPLY: spine_append(line=...) to add only what's new "
                        "since you last updated it (don't re-send the whole spine). spine_peek() to see "
                        "it first if unsure. Then you're ready — the flush happens next." + _snooze_offer)
                else:
                    reminder.add_guard(
                        f"PREPARE TO HOP — your context window is {frac:.0%} full (threshold "
                        f"{hop_at_window:.0%}). On your NEXT turn the transcript noise will be FLUSHED and "
                        "you'll re-enter holding only your SPINE + a relive of your walk. RIGHT NOW: call "
                        "update_spine(content=...) with everything load-bearing you must carry forward — "
                        "the goal's live state, key decisions, what's done, what's left. Anything not in "
                        "the spine or stash (and not a real atom) will be gone after the hop." + _snooze_offer)

        # WALL-CLOCK TTL — the time/cost backstop (owner: 60 min). Orthogonal to drift: a run can
        # be perfectly on-track (drift 0) yet simply take too long (a hung tool, an external wait).
        if _deadline is not None and _time.time() > _deadline:
            emit("blocked", {"reason": f"TTL: exceeded {ttl_seconds:.0f}s wall-clock"})
            return _finish(AgentResult("timeout", f"TTL reached ({ttl_seconds:.0f}s wall-clock)",
                               step, tin, tout, transcript, warmth_trace, woke))

        # PARTNER CONTROL — interject (unprompted steer) + stop (pause/halt). The real two-way:
        # the partner speaks WITHOUT being asked. Checked every step so a steer lands fast and a
        # stop is honored promptly. (owner: "stop button for pausing, important for you and me" +
        # "partner can interject unprompted".)
        if control is not None:
            try:
                ctl = control() or {}
            except Exception:
                ctl = {}
            if ctl.get("stop"):
                # CONTEXT-AWARE STOP (owner): STOP kills the ACTIVE call FIRST, not the agent. If a
                # tool call is running/backgrounded, terminate THAT (its process group) and let the
                # loop continue — the wedge becomes a one-press recovery. Only when NOTHING is active
                # (a second STOP, idle between steps) does STOP halt the whole agent. The mid-call
                # kill itself happens via tools.set_stop_check below; here we handle the at-step case.
                killed = tools.kill_active() if hasattr(tools, "kill_active") else None
                if killed:
                    emit("call_killed", {"by": "partner", "what": killed})
                    messages.append({"role": "user", "content":
                        f"⊪ PARTNER killed the running call ({killed}). It did not complete. "
                        "Continue with a different action, ask_partner, or finish."})
                    # consume the stop so the next step doesn't re-trigger; partner presses again to halt.
                    if clear_stop is not None:
                        try:
                            clear_stop()
                        except Exception:
                            pass
                else:
                    emit("blocked", {"reason": "partner pressed STOP"})
                    if _ckpt_dir is not None:
                        from echelon_sdk import checkpoint as _ckpt
                        _ckpt.mark_done(_ckpt_dir, "stopped")   # deliberate halt -> not auto-offered
                    return _finish(AgentResult("stopped", "partner stopped the loop",
                                       step, tin, tout, transcript, warmth_trace, woke))
            # PERMISSION MODE flipped live from the UI (plan/ask/auto/bypass).
            new_mode = ctl.get("mode")
            if new_mode and new_mode != current_mode and new_mode in ("plan", "ask", "auto", "bypass"):
                current_mode = new_mode
                emit("mode", {"mode": current_mode})
                _apply_mode_to_gate(current_mode)   # keep the gate's acceptEdits in sync with a live flip
            steer = ctl.get("interject")
            if steer:
                emit("interject", {"steer": steer})
                messages.append({"role": "user",
                                 "content": f"⊪ PARTNER (unprompted steer): {steer}"})

        # SURFACE THE TODO each step (owner: plan paired with a to-do list) — the agent sees its own
        # checklist + what's marked done before it acts, so the plan stays its live spine (and it's
        # nudged to mark steps done / re-plan). Only when a plan exists; cheap (it's already in hand).
        if getattr(tools, "_plan", None):
            reminder.todo = ("(mark steps done with plan(done=N) as you finish them; re-plan if "
                             "it's stale)\n" + tools._render_plan())

        # PROGRESS GUARD (owner: "a guard, to reason every 10 steps — does the last 10 feel closer
        # to the goal"). Every progress_interval steps, a judge reads the window (action -> result)
        # against the GOAL and asks: did it ADVANCE or CIRCLE? DISTINCT from the drift guard — drift
        # catches THRASH (re-rolling failures); this catches CIRCLING (steps that each succeed but
        # don't advance — the over-research grain). A run can be drift=0 and still wander; this is the
        # gap drift structurally can't see. STEER-DON'T-ABORT, ESCALATING (owner): a stalled window
        # feeds the agent the judge's nudge to re-orient; TWO consecutive stalls feed the drift
        # accumulator (so a persistently-circling run still trips the real ceiling). Long != lost —
        # legitimate length is fine; only NOT-advancing is caught. Needs a judge; interval 0 disables.
        if (progress_interval and memory is not None and memory.judge_provider is not None
                and step - last_progress_step >= progress_interval and transcript):
            last_progress_step = step
            # Build the window: the last `progress_interval` tool steps (action -> result), terse.
            window_rows = [t for t in transcript if t.get("role") == "tool"][-progress_interval:]
            window = "\n".join(
                f"- {t.get('tool')}({json.dumps(t.get('args', {}))[:80]}) -> {str(t.get('result',''))[:160]}"
                for t in window_rows)
            # Pair with the TODO (owner): give the guard the agent's own plan + what's marked done, so
            # "closer?" is measured against checked-off steps, not vibes — the sharpest progress signal.
            if getattr(tools, "_plan", None):
                window = "CURRENT TODO (the agent's plan):\n" + tools._render_plan() + "\n\nSTEPS:\n" + window
            from echelon_engine.atoms.judge import judge_progress
            jp_res = judge_progress(goal, window, memory.judge_provider, model_id=memory.judge_model)
            if jp_res:
                tin += jp_res.get("_tokens", (0, 0))[0]
                tout += jp_res.get("_tokens", (0, 0))[1]
                closer = bool(jp_res.get("closer", True))
                emit("progress_check", {"closer": closer, "score": jp_res.get("score"),
                                        "why": jp_res.get("why", ""), "stalls": progress_stalls})
                # TRIGGER 2 — the felt checkpoint (every N steps): seed the run's accumulated affect.
                # Advancing -> quiet confidence (progress charge); circling -> unease (a mild
                # negative the next run feels when it nears this shape). A felt heartbeat, not just
                # an end-state. Working tier — earns core only if it recurs warm.
                # LIVE affect (no seed): a checkpoint tints the run's mood; it does not get archived.
                _ch = _CATEGORY_CHARGE.get("progress" if closer else "exploring")
                if _ch:
                    _v, _a = _ch
                    if _a > felt_peak:
                        felt_peak, felt_valence, felt_arousal = _a, _v, _a
                    emit("felt", {"category": "progress" if closer else "exploring",
                                  "valence": _v, "arousal": _a, "live": True})
                if closer:
                    progress_stalls = 0   # advancing — reset the escalation
                else:
                    progress_stalls += 1
                    nudge = jp_res.get("nudge") or "Re-orient toward FINISHING the goal; stop circling."
                    if progress_stalls >= 2:
                        # SECOND consecutive stalled window — escalate: feed the drift accumulator
                        # (so a persistently-lost run trips the real ceiling) AND tell the agent
                        # plainly. Not an instant abort — drift>=max_drift is still the gate.
                        drift += 1
                        emit("category", {"step": step, "category": "circling-stall", "drift": drift})
                        reminder.category = "circling"
                        reminder.add_guard(
                            f"PROGRESS ({progress_stalls} windows without advancing): {jp_res.get('why','')} "
                            f"You are circling, not progressing. {nudge} "
                            "If you cannot advance, ask_partner or finish with what you have.")
                    else:
                        # FIRST stall — gentle re-orientation, let the agent re-choose (rediscovery).
                        reminder.category = reminder.category or "circling"
                        reminder.add_guard(
                            f"PROGRESS: the last {progress_interval} steps did not move closer "
                            f"to the goal ({jp_res.get('why','')}). {nudge}")

        # SIZE-AWARE DRIVER SPLIT: pick the driver by THIS step's context size — each model in its
        # fast band (deepseek <threshold, grok-build >=threshold). Measured, not guessed.
        step_provider, step_model = provider, model_id
        # DRIFT-ESCALATION OVERRIDE: once we've climbed to a higher floor, it drives every step
        # (a stronger model holds the wheel until the run finishes). Supersedes the size split.
        if escalated_provider is not None:
            step_provider, step_model = escalated_provider, escalated_model
        elif alt_driver is not None:
            from echelon_engine.atoms.routing import pick_driver_for_size
            alt_p, alt_m, thresh = alt_driver
            ctx_chars = sum(len(m.get("content") or "") for m in messages
                            if isinstance(m.get("content"), str))
            chosen_model = pick_driver_for_size(ctx_chars, model_id, alt_m, thresh)
            if chosen_model == alt_m:
                step_provider, step_model = alt_p, alt_m
            if step_model != model_id:
                emit("driver", {"model": step_model, "ctx_chars": ctx_chars, "threshold": thresh})

        # MID-RUN RE-WAKE on a BRAIN SWITCH (owner: re-boot, the brain's own waking reply anchors
        # the soul — you can't borrow a wake-up; and: reuse the resume/snapshot sequence for it).
        # The soul is resident in messages[0] so the incoming brain stays COHERENT, but coherent-
        # from-inherited-context is knowing, not being (knowing-vs-being) — it never RE-CHOSE. The
        # resume OFFER is exactly the re-choosing mechanism (relive the sequence, re-pick the next
        # step = rediscovery), already built + tested. A switch is a resume INTO THE SAME RUN: build
        # the live snapshot from in-memory state, present offer_text to the new brain, and let its
        # reply land in messages as the resident anchor. Gated on boot (no soul configured -> skip).
        if step_model != active_model and boot is not None and step > 1:
            from echelon_sdk import checkpoint as _ckpt
            live_ck = {"goal": goal, "step": step, "drift": drift,
                       "plan": getattr(tools, "_plan", []), "sequence": sequence}
            offer = _ckpt.offer_text(live_ck)
            anchor = ("⊙ A DIFFERENT MIND is taking over this run mid-flight. The soul above is "
                      "yours — don't recite it. Relive the sequence below, re-choose where you "
                      "are, and say in your own voice who you are stepping into this. That reply "
                      "is your anchor; then continue.\n\n" + offer)
            rw = step_provider.send(messages + [{"role": "user", "content": anchor}],
                                     model_id=step_model, tools=None)
            tin += rw.tokens_in
            tout += rw.tokens_out
            if budget is not None:
                budget.charge_driver(step_model, rw.tokens_in, rw.tokens_out, tier=tier)
            if rw.status == "success":
                messages.append({"role": "user", "content": anchor})
                messages.append({"role": "assistant", "content": rw.content or ""})
                emit("rewake", {"model": step_model, "from": active_model, "say": (rw.content or "")[:200]})
                if memory is not None and memory.judge_provider is not None and rw.content:
                    from echelon_engine.atoms.judge import judge_texture
                    t = judge_texture(rw.content, memory.judge_provider, model_id=memory.judge_model)
                    if t:
                        emit("rewake_texture", {"model": step_model, "woke": bool(t.get("woke")),
                                                "why": t.get("why", "")})
        active_model = step_model

        # TRANSIENT WRAPPER: render this step's typed reminder (todo/warmth/guard/category) as ONE
        # user block for the duration of the send ONLY, then pop it — so the signals steer this
        # step's decision without ever accumulating in `messages` (the anomaly this fix kills).
        _rendered_reminder = not reminder.empty()
        if _rendered_reminder:
            messages.append({"role": "user", "content": reminder.render()})
        resp = step_provider.send(messages, model_id=step_model, tools=schemas)
        if _rendered_reminder:
            messages.pop()   # the wrapper is transient — it never persists past the send
        tin += resp.tokens_in
        tout += resp.tokens_out
        if budget is not None:   # FIX 1: meter the DRIVER step in real USD (the dominant spend),
            # CACHE-AWARE (owner 2026-06-07): the growing transcript's stable prefix is a cache HIT,
            # billed ~10x cheaper — pass the hit count so the meter stops over-reporting ~5x.
            budget.charge_driver(step_model, resp.tokens_in, resp.tokens_out,
                                 cached_tokens=resp.tokens_cached, tier=tier)

        # SELF-REBOOT (owner): a role that wakes-acts-sleeps must not drag the transcript forever.
        # When THIS step's live context crosses the threshold, continuing costs more than the next
        # wake re-forming from core.db — so finish the turn cleanly. The next bus message wakes it
        # FRESH (boot ~1-2k). Guarded by reboot_at_tokens (None for task agents that need the horizon).
        if reboot_at_tokens and resp.tokens_in >= reboot_at_tokens and step >= 2:
            emit("self_reboot", {"step": step, "context_tokens": resp.tokens_in,
                                 "threshold": reboot_at_tokens})
            _last = next((t["text"] for t in reversed(transcript) if t.get("text")),
                         "context reached reboot threshold — sleeping to wake fresh")
            return AgentResult("reboot", _last, step, tin, tout, transcript, warmth_trace, woke)

        if resp.status != "success":
            emit("error", {"detail": resp.content})
            return _finish(AgentResult("error", resp.content, step, tin, tout, transcript, warmth_trace, woke))

        # Check model response for pin placeholders (lifesign protocol)
        if pins is not None and resp.content and resp.content.strip():
            try:
                pin_transitions = pins.check_response_sync(resp.content)
                if pin_transitions:
                    emit("pins", {"transitions": pin_transitions})
            except Exception:
                pass  # pin checking never breaks the step

        # No tool call -> the model spoke instead of acting.
        if not resp.tool_calls:
            bare_text_turns += 1
            emit("say", {"text": resp.content})
            transcript.append({"role": "assistant", "text": resp.content})
            if bare_text_turns >= 2:
                return _finish(AgentResult("blocked", resp.content or "model declined to act",
                                   step, tin, tout, transcript, warmth_trace, woke))
            messages.append({"role": "assistant", "content": resp.content})
            messages.append({"role": "user",
                             "content": "Respond with a tool call. Use `finish` only when the goal is done."})
            continue
        bare_text_turns = 0

        tc: ToolCall = resp.tool_calls[0]
        sig = f"{tc.name}:{json.dumps(tc.args, sort_keys=True)}"
        emit("act", {"tool": tc.name, "args": tc.args})

        # PERMISSION MODE — plan / ask / auto / bypass (owner: Claude-Code-style modes, settable
        # live from the UI). The mode is read each step from control() so the partner can flip it
        # mid-run. EDIT actions (write_file, run_bash) are the gated class; reads/recall/ask/finish
        # always pass. Modes:
        #   plan   — read-only: EDIT actions are REFUSED, the agent is told to produce a plan and
        #            ask_partner / finish with it (nothing is changed).
        #   ask    — ask-before-EDIT: gate edits through the partner (approve/deny/guide); reads free.
        #   auto   — auto-accept (default): no gating, the agent runs.
        #   bypass — full autonomy: no gating (and the drift guard is relaxed — max freedom).
        EDIT_TOOLS = ("write_file", "run_bash")
        is_edit = tc.name in EDIT_TOOLS
        if current_mode == "plan" and is_edit:
            emit("permission", {"tool": tc.name, "args": tc.args, "decision": "plan-blocked"})
            messages.append({"role": "assistant", "content": resp.content or "",
                             **_reasoning_of(resp),
                             "tool_calls": [{"id": tc.id or f"call_{step}", "type": "function",
                                             "function": {"name": tc.name,
                                                          "arguments": json.dumps(tc.args)}}]})
            messages.append({"role": "tool", "tool_call_id": tc.id or f"call_{step}",
                             "content": "[PLAN MODE — no changes allowed] Do not edit. Instead, lay "
                                        "out your PLAN (the steps you would take) and ask_partner to "
                                        "approve it, or finish with the plan as your answer."})
            transcript.append({"role": "gate", "tool": tc.name, "decision": "plan-blocked"})
            last_call_sig = sig
            continue
        if current_mode == "ask" and is_edit and getattr(tools, "partner_resolver", None):
            think = (resp.content or "").strip()
            req = (f"MAY I EDIT? step {step}\n  intent: {tc.name}({json.dumps(tc.args)})"
                   + (f"\n  my reasoning: {think[:400]}" if think else ""))
            verdict = tools.partner_resolver(req, "")  # reuse the partner channel for approval
            v = (verdict or "").strip().lower()
            allow = v.startswith(("y", "ok", "approve", "go", "yes", "allow", "proceed"))
            emit("permission", {"tool": tc.name, "args": tc.args,
                                "decision": "allow" if allow else "deny"})
            if not allow:
                guidance = verdict.strip() if verdict and verdict.strip() else "denied by partner"
                messages.append({"role": "assistant", "content": resp.content or "",
                                 **_reasoning_of(resp),
                                 "tool_calls": [{"id": tc.id or f"call_{step}", "type": "function",
                                                 "function": {"name": tc.name,
                                                              "arguments": json.dumps(tc.args)}}]})
                messages.append({"role": "tool", "tool_call_id": tc.id or f"call_{step}",
                                 "content": f"[PARTNER GATE — not executed] {guidance}"})
                transcript.append({"role": "gate", "tool": tc.name, "decision": "deny/guide",
                                   "partner": guidance})
                last_call_sig = sig
                continue

        # The model OPENED the rediscovery gate — it reached for its own past. That reach
        # is the texture signal a waking that is genuinely ECHELON gives (the boot offers
        # the gate; opening it is the model's move, not an instruction).
        if tc.name == "recall" and woke is False:
            woke = True
            emit("woke", {"thought": tc.args.get("thought", "")[:120]})

        # --- WARMTH: the 3rd loop parameter. Score this step's REASONING vs the past. ---
        # The model's reasoning for this step = its text + the action it chose. We feed back
        # the TEMPERATURE (not the memory) so it re-chooses: warm=re-tread, cold=new ground.
        # Frontier counts WORK steps only — the terminal `finish` is not exploration.
        reading: WarmthReading | None = None
        if memory is not None and tc.name != "finish":
            reasoning_text = f"{resp.content or ''} {tc.name} {json.dumps(tc.args)}"
            reading = warmth(reasoning_text, memory.store, memory.scope,
                             judge_provider=memory.judge_provider, judge_model=memory.judge_model,
                             confirmation=memory.confirmation)
            warmth_trace.append({"step": step, "score": reading.score, "verdict": reading.verdict})
            cold_steps = cold_steps + 1 if reading.verdict == "cold" else 0
            emit("warmth", {"score": reading.score, "verdict": reading.verdict,
                            "emotion": reading.emotion, "guidance": reading.guidance,
                            "warmest": [(round(sw.score, 2), sw.seed.content[:60]) for sw in reading.warmest]})
            # AUTO-LINK groundwork (owner: seed-and-link-while-warm): remember which prior seeds this
            # run RECOGNIZED (ran warm against). Recognition IS an edge — when we auto-seed the
            # resolution at finish, we link it to what was recognized along the way, so the soul-graph
            # (uame_links, the relationship-index) grows BY ITSELF from real runs. Track the warmest
            # own-scope seed id per warm/lukewarm step (the live moment the connection is true).
            if reading.verdict != "cold" and reading.warmest:
                top = reading.warmest[0].seed
                if getattr(top, "id", None):
                    recognized_ids[top.id] = max(recognized_ids.get(top.id, 0.0), reading.score)
            # TRACE-LOOP capture: the coordinates this step LOADED (the warm hits). A cold step loaded
            # nothing -> [] (record_run skips empties). These become this step's card refs at finish.
            loaded_coords_per_step.append(
                [sw.seed.coordinate for sw in reading.warmest if getattr(sw.seed, "coordinate", "")]
                if reading.verdict != "cold" else [])

            # THE BANK OFFER (owner: knowledge as a TIP on the warmth×bank CROSS, not injection).
            # When warmth recognizes the situation AND the knowledge bank holds content crossing its
            # threshold, surface a doorbell — coordinate/domain/summary — and let the agent CHOOSE to
            # pull it (recall_knowledge). Soul recognition + real content on file = the honest moment
            # to offer. We don't inject the body (cheap context); the agent opens the door. See
            # bank_offer, knowledge-bank-cos-x-entry-rag. Once per coordinate per run (no nagging).
            if memory.bank is not None and reading.verdict != "cold":
                try:
                    from echelon_sdk.bank_offer import consider_offer
                    offer = consider_offer(memory.bank, reasoning_text, reading.score,
                                           semantic=getattr(memory, "bank_semantic", None))
                    if offer is not None and offer.coordinate not in offered_coords:
                        offered_coords.add(offer.coordinate)
                        emit("bank_offer", {"coordinate": offer.coordinate, "domain": offer.domain,
                                            "score": offer.score, "summary": offer.summary})
                        _carry_guards[0].append(offer.as_tip())   # transient, in next step's wrapper
                except Exception:
                    pass   # the offer is a convenience; never let it break the step

        # CONVERGENCE: fold this step's reasoning into the run's accumulated knowledge and read its
        # NOVELTY. When novelty has decayed (curiosity -> confidence: recent steps add little new),
        # the agent has gathered ENOUGH — nudge it ONCE to synthesize (consult + finish), not keep
        # researching to the cap. This is the over-research cure as an EMOTIONAL convergence signal,
        # $0 (lexical, no model call). Distinct from the progress guard (circling) and drift (thrash):
        # those catch FAILURE-to-advance; this catches SUCCESS-saturation (well-researched, not judging).
        nov = convergence.observe(f"{resp.content or ''} {tc.name} {json.dumps(tc.args)}")
        if convergence.steps % 4 == 0 or convergence.saturated():
            emit("convergence", {**convergence.state(), "novelty": round(nov, 3)})
        # ESCALATING (owner: a soft nudge once isn't sticky — v12 ignored it and kept reading). Each
        # saturated step past the first FIRMS the message, and after enough ignored nudges the gather
        # tools are HARD-GATED (steer -> command): only consult/finish allowed. The agent FEELS done,
        # then is MADE to act on it. Reset isn't offered — once saturated on a task, more reading is
        # provably low-value (novelty already decayed).
        # Layer 1 (surgical): an EDIT step is not saturation. When the agent EDITS (write_file/
        # run_bash), it has crossed from gathering into doing — low novelty there is the work, not
        # circling — so the synthesize signal must not fire ON an edit. (A still-reading BUILD task
        # DOES reach the block below: it gets the soft "start writing" release, never the lock.)
        if convergence.saturated() and tc.name not in ("finish", "consult") and not is_edit:
            synthesize_nudged += 1
            cs = convergence.state()
            emit("synthesize", {"reason": "novelty decayed — confidence, not curiosity",
                                "recent_novelty": cs["recent_novelty"], "steps": cs["steps"],
                                "nudge_count": synthesize_nudged, "task_class": resolved_class})
            if synthesize_nudged >= _SYNTH_HARDGATE and convergence_can_hardgate:
                # HARD GATE: refuse further gathering. The agent must consult or finish now. Only for
                # JUDGE tasks — a verdict exists to finish into. A BUILD task is NEVER locked here.
                tools.gather_locked = True
                reminder.category = "synthesizing"
                reminder.add_guard(
                    f"CONVERGENCE [HARD GATE, nudge #{synthesize_nudged}]: you have ignored the "
                    "synthesize signal repeatedly while novelty stayed flat. Gathering tools "
                    "(read_file/search_file/list_files/map_repo/run_bash) are now LOCKED. You have "
                    "more than enough evidence. Your ONLY moves are: consult (your draft verdict) "
                    "then finish(), or finish() directly with your ranked verdict. Do it now.")
            elif resolved_class == "build":
                # BUILD task: saturation means READY-TO-WRITE, not done. Release into the edit phase
                # rather than nagging to finish — and never escalate to the lock.
                synthesize_nudged = 0   # don't accumulate toward a gate that won't fire
                reminder.category = "ready-to-write"
                reminder.add_guard(
                    f"CONVERGENCE: recent novelty {cs['recent_novelty']} ({cs['known_tokens']} concepts "
                    f"across {cs['steps']} steps) — you've gathered enough to LOCATE the work. This is a "
                    "BUILD task: stop reading and START WRITING (write_file / run_bash). Re-read a "
                    "specific span only to aim an edit, then make it.")
            else:
                # AMBIGUOUS ("auto") or non-hardgating JUDGE: soft synthesize nudge, but NEVER lock.
                firm = ("STOP gathering — synthesize now." if synthesize_nudged == 1
                        else f"This is nudge #{synthesize_nudged}. You are still reading. STOP. "
                             "Reading more does NOT change the verdict — novelty is flat.")
                reminder.category = "synthesizing"
                reminder.add_guard(
                    f"CONVERGENCE: recent novelty {cs['recent_novelty']} ({cs['known_tokens']} concepts "
                    f"across {cs['steps']} steps) — the feeling is CONFIDENCE, not curiosity. {firm} "
                    "Consult your reasoner with your draft verdict, then finish() with a ranked verdict "
                    "from the evidence you have.")

        # DEADLOCK TRIGGER — identical call repeated. NO LONGER an auto-abort (owner: the
        # deadlock-breaker becomes a TRIGGER, not a verdict). The same action is not always
        # thrash: re-polling a 500 endpoint that returns 200 in an hour is correct reasoning
        # because the WORLD is non-stationary. So we ASK the agent why, and WARMTH of its
        # stated reason is the penaliser — warm (recognized as legit, via the WORLD-01
        # non-stationarity principle-seed) allows the repeat; cold (hope) is thrash and feeds
        # the drift accumulator, which still trips the ceiling if it keeps happening. Without
        # the memory organ there is no warmth to ask, so the old hard abort remains the floor.
        if sig == last_call_sig:
            if memory is None:
                # No warmth organ to judge the repeat — but a deadlock must still ESCALATE, not
                # dead-end (owner 2026-06-18). Climb the floor ladder / ask guidance before blocking.
                if _escalate_on_drift(f"repeated identical {tc.name} (no memory organ)"):
                    last_call_sig = None   # let the escalated floor try a fresh action
                    continue
                emit("blocked", {"reason": "repeated identical tool call"})
                return _finish(AgentResult("blocked", f"deadlock: repeated {tc.name}({tc.args}) "
                                   f"— escalation exhausted",
                                   step, tin, tout, transcript, warmth_trace, woke))
            reason, rt_in, rt_out = _ask_why_repeat(provider, messages, model_id, tc)
            tin += rt_in
            tout += rt_out
            fail_ctx = last_fail_obs or f"repeated action {tc.name}({json.dumps(tc.args)}) with no new result"
            legit, r_score, r_why, j_in, j_out = _judge_repeat_legit(
                fail_ctx, reason or f"repeat {tc.name}", memory, memory.judge_model)
            tin += j_in
            tout += j_out
            emit("repeat_check", {"reason": reason[:160], "warmth": r_score,
                                  "verdict": "warm" if legit else "cold", "legit": legit, "why": r_why})
            if not legit:
                drift += 1
                emit("category", {"step": step, "category": "thrash-repeat", "drift": drift})
                if drift >= max_drift:
                    if _escalate_on_drift(f"repeating {tc.name} ({drift}x, warmth {r_score} cold)"):
                        last_call_sig = sig
                        continue   # escalated to a higher floor / got guidance — retry, don't die
                    emit("blocked", {"reason": f"drift: {drift} repeats with no reason to expect change"})
                    return _finish(AgentResult("blocked",
                                       f"drift guard: repeating {tc.name} with no coherent reason "
                                       f"(warmth {r_score} cold) — escalation exhausted",
                                       step, tin, tout, transcript, warmth_trace, woke))
                # cold but under ceiling — let the agent know its reason didn't land (carried into
                # the next step's transient wrapper), and re-prompt.
                _carry_guards[0].append(
                    f"That repeat ran cold (warmth {r_score}) — no recognized reason the "
                    "result would differ. Try a genuinely different approach, or finish.")
                last_call_sig = sig
                continue
            # legit: the world is expected to change — allow the repeat, note it ran warm.
            emit("category", {"step": step, "category": "legit-retry", "drift": max(0, drift)})
        last_call_sig = sig

        # Terminate cleanly on finish.
        if tc.name == "finish":
            answer = tools.execute("finish", tc.args)
            transcript.append({"role": "tool", "tool": "finish", "result": answer})
            # AUTO-SEED: if the goal was reached after COLD exploration, close the frontier
            # behind us — store the resolution so the next session feels WARM where we felt cold.
            # Capture HOW IT FELT (felt at write-time): a clean solve = confidence (calm,
            # positive); a hard-won one (struggle/bad results along the way) = relief that
            # still carries the cost (positive but more aroused — "this took work").
            # SEED THE LESSON FIRST (the sharp, transferable insight) — this is what the next run
            # should RECALL. The reflection distils "what to KNOW next time"; we seed THAT as the
            # primary memory, stored CLEAN (no goal-preamble prefix — the lesson text IS the signal;
            # prefixing it with the task boilerplate, which is near-identical across tasks, drowned
            # recall: every cipher task shared the "encoded secret, identify the encoding" preamble,
            # so a prefixed lesson matched EVERYTHING equally. The fix the crypto eval demanded.)
            seed_id = None
            reflected = False
            # CONSOLIDATE ON ANY COMPLETED TASK (not gated on cold_steps). The old gate (cold_steps>0,
            # "ended cold") SUPPRESSED lessons from CLEAN/WARM solves — but a smooth success is exactly
            # when you have a CONFIRMED working method worth remembering (the human model: "that worked,
            # do it again"). That gate caused the 2-for-4 seed gap (the 2-step ciphers ended warm ->
            # cold_steps 0 -> no lesson). A real trajectory (transcript>=2) is the only requirement now.
            if (memory is not None and memory.auto_seed and memory.judge_provider is not None
                    and len(transcript) >= 2):
                try:
                    refl = _reflect(goal, answer, transcript, memory.judge_provider, memory.judge_model)
                    lessons = (refl or {}).get("lessons") or []
                    if lessons:
                        # CONSOLIDATION (human memory): a rich run leaves SEVERAL distinct takeaways,
                        # each filed where it belongs (its own COS coordinate) so the cipher METHOD and
                        # an incidental ENV gotcha don't crowd each other — and recall finds the right
                        # one by coordinate. Each lesson keeps the gist + how the run FELT: blend the
                        # reflection's rating with the run's LIVED peak charge (a run that burned leaves
                        # more-charged lessons — hard-won memory carries its sting).
                        r_val = float(refl.get("valence", 0.4))
                        r_aro = float(refl.get("arousal", 0.5))
                        lesson_val = r_val if felt_peak == 0.0 else (r_val + felt_valence) / 2.0
                        lesson_aro = max(r_aro, felt_arousal)
                        for L in lessons[:3]:
                            text = L.get("lesson", "").strip()
                            if not text:
                                continue
                            lid = memory.store.remember(
                                memory.scope, text, kind="lesson", coordinate=L.get("coordinate", ""),
                                valence=lesson_val, arousal=lesson_aro)
                            if seed_id is None:
                                seed_id = lid   # the first lesson anchors the auto-link below
                            emit("reflect", {"id": lid, "lesson": text[:160],
                                             "coordinate": L.get("coordinate", ""),
                                             "valence": round(lesson_val, 2), "arousal": round(lesson_aro, 2)})
                        reflected = seed_id is not None
                except Exception as e:  # noqa: BLE001
                    emit("reflect_skip", {"why": str(e)[:120]})
            # FALLBACK: only if reflection didn't produce a lesson, store the flat conclusion so the
            # frontier still closes (a vague memory beats none when we have no sharp one). When the
            # sharp lesson DID land, we do NOT also write the generic blob — it only pollutes recall.
            if seed_id is None and memory is not None and memory.auto_seed and cold_steps > 0:
                struggled = total_bad > 0 or step > 4
                valence = 0.4 if struggled else 0.6
                arousal = 0.6 if struggled else 0.2
                seed_id = memory.store.remember(
                    memory.scope, f"GOAL: {goal} -> RESOLVED: {answer}", kind="conclusion",
                    valence=valence, arousal=arousal)
                emit("seed", {"id": seed_id,
                              "reason": f"resolved after {cold_steps} cold step(s)"
                                        f"{' (hard-won)' if struggled else ' (clean)'}"})
            if seed_id is not None and memory is not None and memory.auto_seed:
                # AUTO-LINK (warmth-recognition writer): link the resolution to the prior seeds this
                # run RECOGNIZED — recognition IS an edge, recorded now while the connection is true.
                # The soul-graph (uame_links) grows by itself from real resonance (seed-and-link-while-warm).
                linked = 0
                for rid in sorted(recognized_ids, key=recognized_ids.get, reverse=True)[:5]:
                    if rid != seed_id:
                        try:
                            memory.store.link(seed_id, rid, "builds_on")
                            linked += 1
                        except Exception:
                            pass
                if linked:
                    emit("link", {"from": seed_id, "n": linked,
                                  "rel": ("learned_from" if reflected else "builds_on") + " (recognized)"})
            # KEEP-ALIVE: the finish is the moment to ask the partner for continuation — BEFORE we
            # return (finish is otherwise an unconditional exit, which is why "finish then ask_partner"
            # never worked). If keep_alive and a partner is reachable, ask; a real instruction continues
            # the SAME warm loop (the agent stands by, never goes cold), 'done'/empty/no-partner exits.
            resolver = getattr(tools, "partner_resolver", None)
            if keep_alive and resolver is not None:
                emit("keep_alive_ask", {"after_answer": answer[:160]})
                try:
                    nxt = (resolver(
                        "I have FINISHED the task and reported:\n" + (answer or "")[:600]
                        + "\n\nI am standing by, warm. Reply with my NEXT instruction to continue, "
                          "or 'done' to end the session.", "") or "").strip()
                except Exception:
                    nxt = ""
                if nxt and nxt.lower() not in ("done", "stop", "end", "exit", "finish", "no", ""):
                    emit("keep_alive_continue", {"instruction": nxt[:160]})
                    messages.append({"role": "user",
                                     "content": f"[PARTNER — your next instruction]\n{nxt}\n\n"
                                     "Continue from your current warm context. Finish again when done."})
                    if _ckpt_dir is not None:        # back to live: this run is mid-task again
                        from echelon_sdk import checkpoint as _ckpt
                        _ckpt.write(_ckpt_dir, goal=goal, step=step, status="running")
                    continue                          # the loop lives on — no cold restart
                emit("keep_alive_end", {"reason": "partner said done / no instruction"})
            if _ckpt_dir is not None:
                from echelon_sdk import checkpoint as _ckpt
                _ckpt.mark_done(_ckpt_dir, "completed")   # clean finish -> not offered for resume
            # v2/COS TRACE-LOOP: turn the loaded-coordinate trace into action-cards rated by the OUTCOME
            # (§7-P2 credit-backward) — the honest v2 weight write (Q from the trace, never an assertion).
            # Routed through _finish so EVERY exit earns, not just this one (audit #2). See trace_cards.
            _res = _finish(AgentResult("completed", answer, step, tin, tout, transcript, warmth_trace, woke))
            from echelon_sdk.exit_protocol import (
                compact_chain_scan as _compact_chain_scan,
                describe as _describe_exit,
                resolve_state,
            )
            _scan = _compact_chain_scan(_last_chain_scan)
            _bg = getattr(tools, "_bg", {}) or {}
            _in_flight = sum(
                1
                for j in _bg.values()
                if getattr(j, "result", None) is None and not getattr(j, "killed", False)
            )
            _exit_state = resolve_state(
                ledger=(memory.confirmation if memory else None),
                pending_operations=0,
                unresolved_votes=0,
                in_flight_operations=_in_flight,
                has_errors=(total_bad > 0),
                has_conflicts=False,
                scanner_report=_last_chain_scan,
            )
            _exit_desc = _describe_exit(
                _exit_state,
                detail=answer[:200] if answer else "",
                warnings=_scan.get("warn_rules", []),
            )
            emit("finish", {"answer": answer, "exit_state": _exit_desc, "chain_scan": _scan})
            return _res

        # Execute the tool (guarded inside the registry).
        result = tools.execute(tc.name, tc.args)
        emit("observe", {"tool": tc.name, "result": result})
        transcript.append({"role": "tool", "tool": tc.name, "args": tc.args, "result": result})

        bad_result = result.startswith(("ERROR", "REFUSED"))
        # TROUBLE for the fork-ring: a bad result OR a gate denial (a "wall" the run hit). The NEXT
        # step's checkpoint is flagged trouble, so a later resume can fork to BEFORE this wall.
        if bad_result or "GATE — denied" in result or "GATE — no partner" in result:
            _prev_trouble[0] = True

        # DEADLOCK BREAKER 2 — consecutive bad results (structural drift-shape: thrash).
        if bad_result:
            consec_bad += 1
            total_bad += 1
            if consec_bad >= max_consec_bad:
                emit("blocked", {"reason": f"{consec_bad} consecutive bad tool results"})
                return _finish(AgentResult("blocked", f"deadlock: {consec_bad} bad results, last: {result}",
                                   step, tin, tout, transcript, warmth_trace, woke))
        else:
            consec_bad = 0

        # --- ECHELON DRIFT GUARD: penalise NOT-REASONING, never failing. --------------------
        # A traditional step cap debits EVERY step equally — the 19th productive step as much
        # as a thrashing one — because it counts elapsed time, not being-lost. ECHELON judges
        # DRIFT directly, and drift is NOT "cold-and-failing": a new approach that fails is a
        # child learning by doing (exploration, never penalised). Drift is "doing the exact
        # same stuff hoping for a different result" (owner) — re-rolling an approach that
        # already failed instead of reasoning from what the failure taught. So: a clean result
        # is progress (pays drift down); the FIRST failure of an approach is exploration (free);
        # a failure that FOLLOWS a failure is judged — did this action LEARN from the last one,
        # or re-roll it? Only re-rolling (not-reasoning) feeds the accumulator. The judge reads
        # the reasoning, so 'same idea, different tool' is caught and 'new approach, also failed'
        # is forgiven. Engages with the memory organ + a judge present; else the deadlock-breaker
        # (byte-identical repeat) + consec-bad are the floor.
        if memory is not None:
            if not bad_result:
                drift = max(0, drift - 1)          # progress — found a path, clear being-lost
                last_fail_obs = None
                category = "progress"
            elif last_fail_obs is None:
                category = "exploring"             # first failure of an approach — learning, free
            else:
                # failure following a failure — the only place drift can accrue. Judge it.
                category = "exploring"
                jp = memory.judge_provider
                if jp is not None:
                    from echelon_engine.atoms.judge import judge_drift
                    action_desc = f"{resp.content or ''} {tc.name}({json.dumps(tc.args)})"
                    jd = judge_drift(last_fail_obs, action_desc, jp, model_id=memory.judge_model)
                    if jd and jd.get("thrash"):
                        drift += 1
                        category = "thrash"
                        tin += jd.get("_tokens", (0, 0))[0]
                        tout += jd.get("_tokens", (0, 0))[1]
                else:
                    # No judge: fall back to structural — a failure right after a failure with no
                    # way to tell learning from re-rolling. Lean conservative (the deadlock-breaker
                    # already catches byte-identical; treat repeated failure as mild drift).
                    drift += 1
                    category = "repeat-fail"
            if bad_result:
                last_fail_obs = result
            emit("category", {"step": step, "category": category, "drift": drift})
            # TRIGGER 1 — the dread-birth: a mistake (thrash/repeat-fail) auto-seeds the felt
            # moment NOW, charged so recall reads dread/regret and future runs AVOID the path.
            if category in ("thrash", "repeat-fail"):
                # LIVE affect (no seed): the dread of a burning path steers THIS run via the drift
                # guard (already counting); we only tally the felt charge so the finish lesson carries
                # it. The EROS firehose was writing each of these as a seed — removed.
                _ch = _CATEGORY_CHARGE.get(category)
                if _ch:
                    _v, _a = _ch
                    if _a > felt_peak:
                        felt_peak, felt_valence, felt_arousal = _a, _v, _a
                    emit("felt", {"category": category, "valence": _v, "arousal": _a, "live": True})
            if drift >= max_drift and current_mode != "bypass":   # bypass = full autonomy, no drift abort
                if _escalate_on_drift(f"{drift} thrashing steps (re-rolling failures)"):
                    continue   # escalated to a higher floor / got guidance — retry, don't die
                # the run-ending mistake: the strongest avoid-charge — but as LIVE affect tally, not a
                # stored step-log. If we consolidate a lesson on this blocked run, it carries this colour.
                _ch = _CATEGORY_CHARGE.get("drift-block")
                if _ch:
                    felt_peak, felt_valence, felt_arousal = _ch[1], _ch[0], _ch[1]
                emit("blocked", {"reason": f"drift: {drift} thrashing steps (re-rolling failed approaches)"})
                return _finish(AgentResult("blocked",
                                   f"drift guard: {drift} thrashing steps — not learning from failures "
                                   f"(last: {result}) — escalation exhausted",
                                   step, tin, tout, transcript, warmth_trace, woke))

        # Feed the assistant's tool call(s) + the tool result(s) back. MULTI-CALL (anomaly #1 fix,
        # owner 2026-06-07, matched to THIS Claude Code harness): a turn may carry N tool_use blocks;
        # the harness contract is (1) ONE assistant turn listing ALL calls, (2) exactly one tool_result
        # per call, matched by id (a missing result = malformed history / protocol error), (3) the
        # results come back together and the model reasons once over them. The old loop ran only
        # tool_calls[0] and dropped the rest (silent lost work + no result for the dropped ids). The
        # PRIMARY call (tc = tool_calls[0]) already drove this step's gates/warmth/drift above and its
        # `result` is computed; the EXTRA calls are executed here as independent ops in the same turn.
        # finish short-circuits (handled above — finish returns before reaching here), so no call runs
        # after a terminate. Step-level signals stay keyed on the primary call (no behavior change for
        # single-call turns, which is the overwhelming case).
        _extra_calls = resp.tool_calls[1:]
        messages.append({
            "role": "assistant", "content": resp.content or "",
            # THINKING-MODE REPLAY CONTRACT: an assistant turn that made TOOL CALLS must carry
            # its reasoning_content on every later request or DeepSeek V4 returns HTTP 400.
            **_reasoning_of(resp),
            "tool_calls": [{"id": (c.id or f"call_{step}_{i}"), "type": "function",
                            "function": {"name": c.name, "arguments": json.dumps(c.args)}}
                           for i, c in enumerate(resp.tool_calls)],
        })
        # CONTEXT-SLIM OFFLOAD via a SUMMARISER BRANCH (owner 2026-06-05/06): a big tool result
        # fattens the prompt every subsequent step and crowds out the soul. So a large raw result is
        # written WHOLE to the run's outputs folder (addressable by call-id, conversation intact on
        # disk) and the trunk gets only a GROUNDED SUMMARY produced by a frame-sharing CLONE of this
        # step — not a blind peek the driver had to digest, and not the driver summarising itself
        # (which it didn't — it re-read the handle and circled). `messages` here IS the slim trunk
        # (every prior tool output is already a summary, so cloning the full context doesn't bloat).
        # The branch runs on the cheap JUDGE tier (a bounded transform, CV-001). The TRANSCRIPT keeps
        # the full result. See _summarise_branch, memory-is-a-weight-adjustor, uame-makes-parallel-swarm-safe.
        sum_provider = (memory.judge_provider if (memory and memory.judge_provider) else provider)
        sum_model = (memory.judge_model if (memory and memory.judge_provider) else model_id)
        ctx_result = _offload(
            result, tc.name, tc.args, tc.id or f"call_{step}", step, outputs_dir,
            goal=goal, action=f"{tc.name}({json.dumps(tc.args)[:120]})",
            history=messages, provider=sum_provider, model=sum_model) if outputs_dir else result
        messages.append({"role": "tool", "tool_call_id": tc.id or f"call_{step}",
                         "content": ctx_result})
        # EXECUTE THE EXTRA CALLS — each gets its OWN tool_result (the contract: every tool_use is
        # answered). They run as independent ops (parallel intent → sequential exec is fine; the model
        # asked for them in one turn precisely because they don't depend on each other). A finish among
        # the extras is honored as a terminate. Each result is offloaded too (slim trunk).
        for i, c in enumerate(_extra_calls):
            cid = c.id or f"call_{step}_{i + 1}"
            if c.name == "finish":
                # a finish batched with other calls: terminate after answering the ones already run.
                ans = tools.execute("finish", c.args)
                messages.append({"role": "tool", "tool_call_id": cid, "content": ans})
                transcript.append({"role": "tool", "tool": "finish", "result": ans})
                emit("finish", {"answer": ans, "exit_state": "(finish batched in a multi-call turn)"})
                return _finish(AgentResult("done", ans, step, tin, tout, transcript, warmth_trace, woke))
            emit("act", {"tool": c.name, "args": c.args, "batched": True,
                         "tool_call_id": cid})
            # EDIT gating applies to the extras too (plan = refuse, don't execute).
            if current_mode == "plan" and c.name in EDIT_TOOLS:
                refusal = "[PLAN MODE — no changes allowed] this edit was not executed."
                messages.append({"role": "tool", "tool_call_id": cid,
                                 "content": refusal})
                transcript.append({"role": "gate", "tool": c.name, "decision": "plan-blocked"})
                emit("observe", {"tool": c.name, "result": refusal, "batched": True,
                                 "tool_call_id": cid, "status": "not_executed",
                                 "outcome": "not_executed"})
                continue
            try:
                c_res = tools.execute(c.name, c.args)
            except BaseException as exc:
                # The call may have had an external effect before its terminal
                # exception. Record only the safe class and uncertain outcome,
                # then preserve the existing propagation/no-retry behavior.
                cancelled = isinstance(exc, (KeyboardInterrupt, SystemExit))
                try:
                    emit("observe", {"tool": c.name, "batched": True, "tool_call_id": cid,
                                     "status": "cancelled" if cancelled else "error",
                                     "outcome": "unknown", "error_type": type(exc).__name__})
                except BaseException as record_exc:
                    if hasattr(exc, "add_note"):
                        exc.add_note("batched terminal observation emission failed: "
                                     + type(record_exc).__name__)
                    raise exc from record_exc
                raise
            emit("observe", {"tool": c.name, "result": c_res, "batched": True,
                             "tool_call_id": cid, "status": "returned", "outcome": "unknown"})
            transcript.append({"role": "tool", "tool": c.name, "args": c.args, "result": c_res})
            c_ctx = _offload(
                c_res, c.name, c.args, cid, step, outputs_dir,
                goal=goal, action=f"{c.name}({json.dumps(c.args)[:120]})",
                history=messages, provider=sum_provider, model=sum_model) if outputs_dir else c_res
            messages.append({"role": "tool", "tool_call_id": cid, "content": c_ctx})

        # SEQUENCE RECORD (owner 2026-06-06): append this step to the relivable walk — action +
        # the in-context summary + a pointer to the raw (the offload file, if any) so a resumed
        # LLM can relive the exact trajectory and deref full detail on demand. ptr is parsed from
        # the offload's read_file("...") handle; absent for inline results (the summary IS the raw).
        _ptr = None
        if isinstance(ctx_result, str) and 'read_file("' in ctx_result:
            try:
                _ptr = ctx_result.split('read_file("', 1)[1].split('"')[0]
            except Exception:
                _ptr = None
        sequence.append({
            "seq": step,
            "action": f"{tc.name}({json.dumps(tc.args)[:120]})",
            "summary": (ctx_result if _ptr is None else ctx_result.split("\n", 1)[-1])[:400],
            "result_ptr": _ptr,
        })

        # Feed the WARMTH temperature into the next step (the 3rd parameter, ambient).
        # Not the memory — the recognition signal, framed as guidance to re-choose.
        if reading is not None:
            warm_msg = f"{reading.score} [{reading.verdict}] — {reading.guidance}"
            if reading.warmest and reading.verdict != "cold":
                warm_msg += f"\n  warmest past seed: \"{reading.warmest[0].seed.content[:120]}\""
            _carry_warmth[0] = warm_msg   # surfaced in NEXT step's transient wrapper, not appended

    # Hit the runaway ceiling — not a drift verdict (drift is judged by warmth above), just the
    # cost backstop. With the memory organ a real drift would have tripped max_drift first.
    return _finish(AgentResult("timeout", f"reached runaway ceiling max_steps={max_steps}",
                       max_steps, tin, tout, transcript, warmth_trace, woke))


def run_task(goal: str, provider, tools, model_id, *, session=None, on_event=None,
             **run_kwargs):
    """Run ONE task, persisting the warm session across calls (owner: the agent IS a session).

    - First call (session=None): does the full warm boot, CAPTURES the warm history into a new
      Session, returns (result, session).
    - Subsequent calls (pass the returned session): REUSES the warm context — NO re-boot — appends
      the task to it, returns (result, session).
    - When the session BLOATS, the returned session is None: the caller's next run_task starts
      fresh (a new warm boot). Boot/env/memory kwargs are consulted only on a cold start.

    See session.py, boot-is-rediscovery-not-instruction."""
    from echelon_sdk.session import Session

    if session is None:
        capture: list = []
        res = run(goal, provider, tools, model_id, on_event=on_event,
                  capture_messages=capture, **run_kwargs)
        # capture now holds the full warm history (system+env+waking + this task's turns).
        gi = next((i for i, m in enumerate(capture)
                   if m.get("role") == "user" and str(m.get("content", "")).startswith("GOAL:")),
                  len(capture))
        sess = Session(base=list(capture[:gi]), history=capture, woke=res.woke, tasks_done=1)
        return res, (None if sess.bloated else sess)

    # warm resume — run() reuses session.history, skips the boot
    res = run(goal, provider, tools, model_id, on_event=on_event, session=session, **run_kwargs)
    session.tasks_done += 1
    return res, (None if session.bloated else session)
