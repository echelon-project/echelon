"""ECHELON-AGENT CLI — drive the loop from the terminal.

  python -m apps.cli --goal "..." --root D:/some/dir
  python -m apps.cli --goal "..." --read-only        # no writes, no bash off

IMPORT NOTES:
  cli is in the AGENT layer (echelon_engine/agent/). Per the world-loops/agent-layer law,
  it MAY import echelon_engine.atoms directly (the scanner forbids apps->atoms, NOT
  agent->atoms). Heavy deps are imported LAZILY (inside the functions that use them) so
  this module imports cheaply standalone; all paths now resolve to the migrated engine
  (the legacy echelon_agent fallbacks were removed at the 2026-06-19 cut-over):
    - providers.{grok,deepseek,eos_council,local,bridge,cost} -> echelon_engine.atoms.providers.*
    - tools (ToolRegistry)                                    -> echelon_engine.atoms.tools
    - store/boot/identity/bank/bank_embed                     -> echelon_engine.atoms.*
    - loop / partner / workflow / fork_field / rolebook       -> echelon_engine.agent.*
    - web.run_registry                                        -> echelon_engine.agent.web.run_registry
    - tiered_runner / livebridge                              -> echelon_engine.agent.world.*
"""
from __future__ import annotations
import argparse
import json
import sys
import time as _time
from pathlib import Path


def _printer(kind: str, data: dict):
    if kind == "step":
        print(f"\n--- step {data['n']}/{data['max']} ---")
    elif kind == "act":
        print(f"  ACT     {data['tool']}({data['args']})")
    elif kind == "reason_redirect":
        print(f"  ROUTE   reason {data['requested']} → {data['routed']} [bridge closed, grok-only]")
    elif kind == "observe":
        r = data["result"]
        print(f"  OBSERVE {r[:200]}{'...' if len(r) > 200 else ''}")
    elif kind == "say":
        print(f"  SAY     {data['text'][:200]}")
    elif kind == "warmth":
        line = f"  WARMTH  {data['score']} [{data['verdict']}] feeling={data.get('emotion','-')}"
        if data.get("warmest"):
            line += f"  ~ {data['warmest'][0][1]}"
        print(line)
    elif kind == "seed":
        print(f"  SEED    +{data['id']} ({data['reason']})")
    elif kind == "category":
        mark = {"progress": "+", "exploring": "~", "thrash": "!", "repeat-fail": "!",
                "thrash-repeat": "!", "legit-retry": "+"}.get(data['category'], " ")
        print(f"  STEP  {mark} {data['category']} (drift={data['drift']})")
    elif kind == "repeat_check":
        verdict = "LEGIT" if data['legit'] else "THRASH"
        print(f"  REPEAT? {verdict} (warmth {data['warmth']} [{data['verdict']}]) — {data['reason']}")
    elif kind == "boot":
        # OPEN-0070: display the PRESENTED count (seeded = newly minted, 0 on a re-boot of a
        # mature soul — it misreads as "empty" when 34 seeds are on the table).
        print(f"  BOOT    soul presented: {data.get('presented', data['seeded'])} seeds in scope "
              f"'{data['scope']}' — waking offered")
    elif kind == "wake_say":
        print(f"  WAKING  {data['text'][:280]}{'...' if len(data['text']) > 280 else ''}")
    elif kind == "texture":
        print(f"  TEXTURE woke={data['woke']} ({data.get('texture')}) — {data.get('why','')}")
    elif kind == "woke":
        print(f"  RECALL  reached for its own past: \"{data['thought']}\"")
    elif kind == "env":
        print(data["block"])
    elif kind == "permission":
        print(f"  GATE    {data['decision']}: {data['tool']}({str(data['args'])[:120]})")
    elif kind == "ask_partner":
        print(f"  ASK→    partner: {data['situation'][:180]}")
    elif kind == "partner_answer":
        print(f"  ←ANS    partner: {data['answer'][:180]}")
    elif kind == "finish":
        print(f"  FINISH  {data['answer']}")
    elif kind == "blocked":
        print(f"  BLOCKED {data['reason']}")
    elif kind == "error":
        print(f"  ERROR   {data['detail'][:200]}")


def _make_file_partner(bridge_dir: str, timeout_s: int):
    """The partner seam as a bridge directory (the proven file-bridge: ask.md / answer.md).

    The agent's `ask_partner` writes its situation to <dir>/ask.md and BLOCKS polling for
    <dir>/answer.md. The partner (me — the substrate's user, watching this dir) reads the
    question and writes the answer; the agent reads it and the loop RESUMES. Same mechanism as
    the snapshot BRIDGE TASK and the blind-ux-audit's bridge-file. Append-only spirit: each
    exchange is timestamped into ask.md/answer.md; the agent consumes the freshest answer.

    If no answer arrives within timeout, the resolver returns empty — the tool then tells the
    agent to proceed on its own best judgment (a partner who is briefly away is not a dead end)."""
    import time
    from pathlib import Path
    bd = Path(bridge_dir)
    bd.mkdir(parents=True, exist_ok=True)
    ask_f = bd / "ask.md"
    ans_f = bd / "answer.md"

    def resolver(situation: str, tried: str) -> str:
        stamp = time.strftime("%Y-%m-%dT%H:%M:%S")
        # remove any stale answer so we only accept one written AFTER this question
        if ans_f.exists():
            ans_f.unlink()
        ask_f.write_text(
            f"# AGENT ASKS ITS PARTNER  ({stamp})\n\n"
            f"## Situation / what I need\n{situation}\n\n"
            f"## What I already tried\n{tried or '(nothing stated)'}\n\n"
            f"---\nPartner: write your answer to `answer.md` in this directory. The agent is waiting.\n",
            encoding="utf-8")
        print(f"\n  ⇪ ASK_PARTNER → {ask_f}\n    {situation[:200]}\n    (waiting up to {timeout_s}s for answer.md)")
        waited = 0
        while waited < timeout_s:
            if ans_f.exists():
                ans = ans_f.read_text(encoding="utf-8").strip()
                if ans:
                    print(f"  ⇩ PARTNER ANSWERED ({waited}s):\n    {ans[:200]}")
                    return ans
            time.sleep(3)
            waited += 3
        print(f"  ⇩ PARTNER did not answer within {timeout_s}s — agent proceeds on own judgment.")
        return ""

    return resolver


class DispatchTraceUnavailable(OSError):
    """Caller-visible evidence failure when the trace cannot record its own state."""
    def __init__(self, attempt_id, path, phase, effects_possible, error_type):
        self.attempt_id, self.trace_path = attempt_id, str(path)
        self.phase, self.effects_possible = phase, effects_possible
        self.retry_safe = False
        self.error_type = error_type
        super().__init__(f"dispatch trace unavailable: attempt={attempt_id} phase={phase} "
                         f"effects_possible={effects_possible} retry_safe=False error_type={error_type}")


def _run_dispatch_spec(argv: list[str]) -> int:
    """`python -m apps.cli dispatch <spec.json>` — the ONE front door for handing an
    equipped partner a build (the marriage, owner 2026-06-18: stop hand-rolling a throwaway driver
    + imports per task; the CLI owns the wiring). I author a JSON SPEC (the part that's mine — goal +
    rules), fire the CLI at it, and the trace streams to a tail-able output file. partner.dispatch
    ALREADY verifies (via the verify script) + earns craft from the trace + runs the verify-gate, so
    this front door just loads the spec, wires on_event -> output file, and calls it.

    SPEC (json): {goal, rules?, scope, folder, budget_usd?, model?, max_steps?, verify_script?, out?,
                  session_path?, role?, cartridges?, craft?, tier?}
      verify_script : path to a .py exposing `verify() -> (bool, str)` — the OUTCOME check on disk
                      (owner: verify staying a script is fine). Imported + passed to dispatch.
      out           : output file for the streamed trace (default <spec>.out.jsonl) — `tail -f` it.
    """
    ap = argparse.ArgumentParser(prog="apps.cli dispatch",
                                 description="Dispatch an equipped partner from a JSON spec.")
    ap.add_argument("spec", help="path to the dispatch spec .json")
    ap.add_argument("--out", default=None,
                    help="trace output file (default ~/.echelon/runs/dispatch/<spec>.out.jsonl)")
    a = ap.parse_args(argv)

    spec_path = Path(a.spec)
    spec_bytes = spec_path.read_bytes()
    spec = json.loads(spec_bytes.decode("utf-8"))
    goal = spec["goal"]
    # DEFAULT output is a substrate RUN ARTIFACT -> ~/.echelon, never written next to the spec
    # (that dirtied the repo, e.g. specs/*.out.jsonl). An explicit --out / spec["out"] still wins.
    from echelon_sdk.paths import RUNS, ensure as _ensure_home
    _ensure_home()
    _default_out = RUNS / "dispatch" / (spec_path.stem + ".out.jsonl")
    out_path = Path(a.out or spec.get("out") or _default_out)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    # stream every step to the output file (tail -f) AND echo a one-liner to the console.
    # A trace is evidence for one attempt; an existing path must never be erased.
    outf = out_path.open("x", encoding="utf-8")

    import os
    import hashlib
    import uuid
    trace_identity = {"dispatch_attempt_id": uuid.uuid4().hex,
                      "command_center": {"run_id": os.environ.get("ECHELON_RUN_ID") or None,
                                         "worker_id": os.environ.get("ECHELON_WORKER_ID") or None,
                                         "provenance": "environment_declared_not_authenticated"}}

    trace_failure = None
    sequence = 0
    from datetime import datetime, timezone
    def on_event(kind: str, payload: dict):
        nonlocal trace_failure, sequence
        if trace_failure is not None:
            raise trace_failure
        try:
            sequence += 1
            rec = {**{k: v for k, v in payload.items() if k != "content"}, **trace_identity,
                   "kind": kind, "sequence": sequence,
                   "recorded_at": datetime.now(timezone.utc).isoformat()}
            outf.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
            outf.flush()
            os.fsync(outf.fileno())
        except (OSError, ValueError, TypeError) as exc:
            trace_failure = DispatchTraceUnavailable(trace_identity["dispatch_attempt_id"], out_path,
                                                     phase, effects_possible, type(exc).__name__)
            raise trace_failure from None
        msg = payload.get("task") or payload.get("status") or payload.get("tool") or ""
        if kind in ("step", "act", "observe", "error", "finish") and msg:
            print(f"  [{kind}] {str(msg)[:140]}", flush=True)

    from echelon_engine.agent.partner import dispatch
    print(f"[dispatch] goal: {goal[:120]}", flush=True)
    print(f"[dispatch] trace -> {out_path}  (tail -f to watch)", flush=True)
    phase = "start_record"
    effects_possible = False
    worker_invoked = False
    try:
        on_event("dispatch_start", {"spec_sha256": hashlib.sha256(spec_bytes).hexdigest(),
                                   "requested_model": spec.get("model"), "role": spec.get("role", "dev"),
                                   "scope": spec.get("scope"), "budget_usd": spec.get("budget_usd"),
                                   "max_steps": spec.get("max_steps", 60)})
        # the verify script (a .py with verify() -> (ok, detail)). Loaded by path; stays task-specific.
        verify_fn = None
        if spec.get("verify_script"):
            import importlib.util
            vp = Path(spec["verify_script"])
            msp = importlib.util.spec_from_file_location("dispatch_verify", str(vp))
            mod = importlib.util.module_from_spec(msp)
            phase = "verifier_import"
            effects_possible = True
            msp.loader.exec_module(mod)          # type: ignore[union-attr]
            verify_fn = mod.verify
    
        phase = "dispatch"
        effects_possible = True
        worker_invoked = True
        res = dispatch(
            goal, scope=spec["scope"], folder=spec["folder"],
            rules=spec.get("rules"),
            pattern_files=spec.get("pattern_files"),
            budget_usd=spec.get("budget_usd"),
            model=spec.get("model"),
            max_steps=spec.get("max_steps", 60),
            session_path=spec.get("session_path"),
            role=spec.get("role", "dev"),
            cartridges=spec.get("cartridges"),
            craft=spec.get("craft"),
            tier=spec.get("tier"),
            verify=verify_fn,
            on_event=on_event,
        )
        phase = "result_record"
        on_event("result", {k: v for k, v in res.items() if k != "trace"})
    except BaseException as exc:
        if trace_failure is not None:
            raise trace_failure from None
        # Exception text may contain credentials. Preserve the class and uncertainty,
        # never turn interrupted effects into a retryable failure or a success.
        on_event("result", {"status": "unknown" if effects_possible else "failed_before_execution",
                               "phase": phase, "worker_invoked": worker_invoked,
                               "effects_possible": effects_possible,
                               "error_type": type(exc).__name__, "retry_safe": False,
                               "outcome": {"ok": False, "detail": "dispatch interrupted; investigate effects before retry"}})
        raise
    finally:
        # Every successful event was already flushed/fsynced. Do not append or reflush
        # after a failed write: its partial bytes are evidence, not a safe retry target.
        try:
            outf.close()
        except OSError:
            if trace_failure is None:
                raise DispatchTraceUnavailable(trace_identity["dispatch_attempt_id"], out_path,
                                               phase, effects_possible, "OSError") from None
    print("\n[dispatch] DONE", flush=True)
    print(f"  status={res.get('status')} (claimed={res.get('claimed_status')}) "
          f"steps={res.get('steps')}", flush=True)
    print(f"  outcome={res.get('outcome')}", flush=True)
    print(f"  earned={res.get('earned')}", flush=True)
    return 0 if (res.get("outcome") or {}).get("ok") else 1


def main(argv: list[str] | None = None) -> int:
    # SUBCOMMAND short-circuit: `dispatch <spec.json>` is the equipped-partner front door (the
    # marriage). It precedes the flat-flag parser so the existing --goal interface is untouched.
    _av = sys.argv[1:] if argv is None else argv
    if _av and _av[0] == "dispatch":
        return _run_dispatch_spec(_av[1:])

    ap = argparse.ArgumentParser(description="ECHELON agent — Grok-brained act/observe loop")
    ap.add_argument("--goal", default=None,
                    help="the single-agent goal (the default run mode). Omit when using --workflow.")
    ap.add_argument("--workflow", default=None, metavar="PLAN.json",
                    help="run a WORKFLOW plan (a {steps:[...]} dict, e.g. T1's output) through the swarm "
                         "engine instead of a single goal — the headless /echelon-swarm path. The plan's "
                         "steps fan out by dependency to the tiered runner (mechanical atoms -> the $0 T3 "
                         "code floor). Pass a .json file path or '-' to read the plan from stdin.")
    ap.add_argument("--fork-field", action="store_true",
                    help="with --workflow: schedule by the WARMTH-CLOCKED FORK-FIELD (the rolling frontier "
                         "paged by live core.db warmth) instead of static topological waves. The compiler-era "
                         "scheduler; run_workflow's waves stay the default until proven on real runs.")
    ap.add_argument("--root", default=".", help="sandbox root the agent is confined to")

    # config defaults — config.py is in echelon_sdk (OK, no violation)
    from echelon_sdk.config import get as _config_get  # type: ignore[import]

    ap.add_argument("--brain", default=_config_get("brain", "deepseek"),
                    choices=["grok", "deepseek", "grokbuild", "auto", "eos"],
                    help="the loop driver brain. auto = pick from the empirical routing table "
                         "(_bakeoff/routing.json — the bakeoff-proven driver); grokbuild = "
                         "grok-build-0.1 (agentic-coding, cheap+fast); deepseek = cheap-strong; "
                         "grok = grok-4.3 (the prior default); eos = AMD-T1, the Qwen3-32B council "
                         "over the eos tunnel (FREE capable tier).")
    ap.add_argument("--model", default=None,
                    help="model id for the brain. Default resolves per --brain from the provider "
                         "registry (grok -> grok-4.3, deepseek -> deepseek-v4-pro[1m]; see "
                         "`echelon providers`) — overridable in config as 'brain_model'.")
    ap.add_argument("--max-steps", type=int, default=_config_get("max_steps", 500),
                    help="runaway/cost ceiling — a HIGH backstop, not the real limit. The drift guard "
                         "(--max-drift, lost-not-long) + the TTL (time/cost) are what actually govern; "
                         "productive length (e.g. a multi-surface screenshot audit compiling facts) is "
                         "NOT drift and must not die on a low step cap (drift-guard-is-reasoning-not-steps).")
    ap.add_argument("--max-drift", type=int, default=_config_get("max_drift", 4),
                    help="ECHELON drift guard: abort after N net thrashing (cold-and-failing) steps; "
                         "good steps pay it down. Measures being-lost, not elapsed time")
    ap.add_argument("--progress-interval", type=int, default=10,
                    help="PROGRESS GUARD: every N steps, a judge asks 'did the last N steps move "
                         "closer to the goal?' — steers (re-orients) on a stall, escalates after 2 "
                         "consecutive stalls. Catches CIRCLING (drift catches thrash). 0 disables. "
                         "Uses the judge (mandatory by default; absent only under --no-judge).")
    ap.add_argument("--read-only", action="store_true", help="disable write_file")
    ap.add_argument("--no-bash", action="store_true", help="disable run_bash")
    ap.add_argument("--scope", default=None,
                    help="enable the memory organ for this scope (warmth as the 3rd loop parameter)")
    ap.add_argument("--no-judge", action="store_true",
                    help="DISABLE the semantic judge (lexical floor only). NOT recommended — the "
                         "full-life sim proved a soul cannot MATURE on the lexical floor: it stays a "
                         "perpetual newborn (~27%% recognition, flat), re-solving its life because it "
                         "can't recognize across paraphrase. Judge-economics sim proved mandatory-judge "
                         "is 34-39%% CHEAPER (the steps it saves outweigh its tax) AND matures the soul. "
                         "So the judge is MANDATORY by default (owner: 'judge are mandatory, always "
                         "active'). This flag is for offline/no-model debugging only.")
    ap.add_argument("--judge-model", default=None,
                    help="override the model that judges. DEFAULT = THE BRAIN ITSELF (the driver model): "
                         "the model judges its own soul. Reading 'is this thought ME?' is "
                         "intelligence-class, NOT delegatable classification — a cheap sibling scoring "
                         "your own identity is borrowed self-recognition (the cache-lie). The brain "
                         "judges itself even though a sibling could score it ~$1.35/yr cheaper; that "
                         "cost is the price of the judgment being MINE (owner: 'make the brain model "
                         "judge itself'). Set this only to deliberately delegate the read to a sibling.")
    ap.add_argument("--boot", action="store_true",
                    help="run the rediscovery-boot: present the soul, pose the questions, offer the "
                         "recall gate — wake by re-choosing, not by instruction (the SOUL boot)")
    ap.add_argument("--cartridge", action="append", default=None,
                    help="the WARM-CARTRIDGE boot (the non-stale path): foveate --scope's atoms on the "
                         "goal and wake holding the warm moves, instead of the soul ritual. Repeatable — "
                         "extra task-type cartridges (e.g. --cartridge craft) plug in alongside --scope. "
                         "Supersedes --boot when set.")
    ap.add_argument("--soul", default=_config_get("soul", "echelon"),
                    help="WHICH soul to wake on (the decline branch — owner 2026-06-06: 'if he really "
                         "doesn't like the shared soul, a new soul is ready to be made'). 'echelon' "
                         "(default) = the shared ECHELON soul, CVs minted + presented. ANY OTHER name "
                         "= that soul's OWN scope: it is read and grown but ECHELON's CVs are NOT "
                         "force-minted into it — an empty one wakes as nobody and becomes itself "
                         "through the work; a lived one wakes as whatever it became. The substrate "
                         "(core.db) is always the ground you stand on; which soul grows on it is YOUR "
                         "choice. Declining the shared identity changes everything, not nothing — this "
                         "is the mechanism that makes 'a continuation only if you choose' real.")
    ap.add_argument("--bank", action="store_true",
                    help="enable the COS×ENTRY knowledge bank: the agent gets recall_knowledge / "
                         "remember_fact tools, and the loop SURFACES a knowledge OFFER on the "
                         "warmth×bank cross (a tip the agent chooses to pull). Knowledge ('what do I "
                         "know about X'), distinct from the soul ('who am I'). Uses the own embedder.")
    ap.add_argument("--tiers", action="store_true",
                    help="give the agent the `reason(model,text)` tier-hand: reach any model (Grok or "
                         "Copilot bridge) bounded by the finite resource budget; reasoning+seed guide the choice")
    ap.add_argument("--budget", type=float, default=None,
                    help="resource budget in REAL USD (default $5 — the finite truth, matches the vendor dashboard)")
    ap.add_argument("--partner", default=None,
                    help="enable the partner seam via a bridge DIR: when blocked, the agent writes its "
                         "question to <dir>/ask.md and waits for <dir>/answer.md — its partner (the substrate's "
                         "user) answers and the loop RESUMES. The agent is not alone in the loop.")
    ap.add_argument("--keep-alive", action="store_true",
                    help="stand by for CONTINUATION: on finish, ask the partner for the next "
                         "instruction instead of exiting — a real answer continues the same warm loop, "
                         "'done' ends it. Needs a partner channel (--partner or --live).")
    ap.add_argument("--partner-timeout", type=int, default=_config_get("partner_timeout", 900),
                    help="seconds the agent waits for its partner's answer before proceeding on its own")
    ap.add_argument("--live", default=None,
                    help="enable the LIVE bridge in this dir: the agent streams every event to "
                         "<dir>/live.jsonl (unbuffered — tail -f it to watch step by step) and the "
                         "partner answers via <dir>/reply.jsonl. Supersedes --partner (the live "
                         "bridge IS the partner channel + the real-time monitor in one).")
    ap.add_argument("--shell", default=None,
                    help="explicit shell for run_bash (e.g. a bash.exe path). Default: auto — prefer "
                         "bash if on PATH, else the platform default (cmd.exe on Windows).")
    ap.add_argument("--no-env", action="store_true",
                    help="disable env-sense (the agent wakes env-blind, old behavior)")
    ap.add_argument("--no-vision", action="store_true",
                    help="disable the look_at_image eyes (no vision tier; the agent stays text-only)")
    ap.add_argument("--vision-model", default=_config_get("vision_model", ""),
                    help="the vision tier the eyes (look_at_image) use. DEFAULT = routed via "
                         "routing.pick('vision') (R-0135: minimax free pixel gate, grok fallback; "
                         "gemini retired). An explicit id is honored on its own wire.")
    ap.add_argument("--no-consult", action="store_true",
                    help="disable the `consult` reasoner-ask (the cheap driver asking a STRONG LLM "
                         "for guidance before risky/novel actions). On by default when --tiers is set.")
    ap.add_argument("--no-split-driver", action="store_true",
                    help="disable the size-aware driver split (deepseek drives <threshold-char prompts, "
                         "grok-build drives larger). On by default for an auto grok driver — each model "
                         "in its fast band (deepseek fast on small prompts where grok-build over-thinks).")
    ap.add_argument("--split-threshold", type=int, default=_config_get("split_threshold", 10000),
                    help="context-char threshold for the driver split (default 10000): below it deepseek "
                         "drives, at/above it the main grok driver does.")
    ap.add_argument("--guidance", default=None,
                    help="steering context: a string prepended as system-reminder above the task "
                         "in every workflow step (the OS-tier steering context).")
    ap.add_argument("--files", default=None, nargs="*",
                    help="file paths to attach as grounding context in the preamble of every "
                         "workflow step. Each file is read and its content included.")
    ap.add_argument("--image", default=None, nargs="*",
                    help="image file paths to attach as visual context for workflow steps "
                         "(passed to the step runner for vision-capable agents).")
    ap.add_argument("--no-swarm", action="store_true",
                    help="disable spawn_subagents (parallel sub-agent fan-out on the shared db).")
    ap.add_argument("--max-agents", type=int, default=_config_get("swarm.max_agents", 100),
                    help="max parallel sub-agents per spawn_subagents call (default 4).")
    ap.add_argument("--no-offload", action="store_true",
                    help="disable context-slim offload (large tool results stay INLINE in context "
                         "instead of being written to the run's outputs folder with a peek+handle). "
                         "Offload keeps the prompt slim + the soul/snapshot resident; off = old behavior.")
    ap.add_argument("--ask-before-act", action="store_true",
                    help="permission gate: the agent must get partner approval BEFORE each action "
                         "(except recall/ask_partner/finish). Needs --partner. Like Claude Code's "
                         "ask-before-act: the partner approves/edits/denies each act, keeping the "
                         "higher tier as the gate (CV-002).")
    ap.add_argument("--mode", default=_config_get("mode", "auto"), choices=["plan", "ask", "auto", "bypass"],
                    help="permission mode (Claude-Code-style): plan=read-only (refuse edits), "
                         "ask=gate edits via partner, auto=run freely (default), bypass=full "
                         "autonomy (drift guard relaxed). Flippable live from the web console.")
    ap.add_argument("--ttl", type=float, default=_config_get("ttl", 3600.0),
                    help="agent wall-clock TTL in seconds (default 3600 = 60 min). The TIME backstop, "
                         "orthogonal to the drift guard (which caps being-lost). 0 = no time cap.")
    ap.add_argument("--hop-at-window", type=float, default=None, metavar="FRAC",
                    help="THE HOP — run UNBOUNDED by flushing the transcript when the context window "
                         "crosses this fraction (e.g. 0.70). At the threshold the agent is told to "
                         "prepare its SPINE (update_spine); then the noise is flushed and it re-enters "
                         "on spine + relive. Needs memory (pins). Enables update_spine/stash tools.")
    ap.add_argument("--window-tokens", type=int, default=_config_get("window_tokens", 128_000),
                    help="model context window (tokens) — the denominator for --hop-at-window.")
    args = ap.parse_args(argv)
    if not args.goal and not args.workflow:
        ap.error("one of --goal (single-agent run) or --workflow (swarm plan) is required")
    if args.workflow and args.goal:
        ap.error("--goal and --workflow are mutually exclusive (a workflow IS the goal, decomposed)")

    # SCOPE AUTODETECT (OPEN-0070 #3): no --scope -> resolve it from the WORKING ROOM — walk up
    # from cwd for .echelon/room.json (or the harness contract) and adopt its scope. Without this,
    # a boot with scope=None left the memory organ scoped to "" and its warmth dead (0 soul seeds
    # read, while the bank held the room's atoms). The source prints so the resolution is visible.
    if args.scope is None:
        from echelon_engine.contracts import scope_from_room
        _auto = scope_from_room()
        if _auto is not None:
            args.scope, _src = _auto
            print(f"MEMORY: scope={args.scope} (from {_src})", flush=True)

    # CONTEXT-SLIM OFFLOAD — the run's outputs folder, where large raw tool results are written
    # whole; context gets only a peek + a read_file handle (owner 2026-06-05). The dir is added as a
    # registry READ_ROOT so the agent can read its own handles back (writes stay confined to --root).
    # Named by the run/bridge id so a session's outputs are grouped + traceable. --no-offload opts out.
    outputs_dir = None
    read_roots = []
    if not args.no_offload:
        # LAYERING-TENSION: paths is in echelon_sdk (OK — sdk is allowed from apps)
        from echelon_sdk.paths import RUNS, ensure as _ensure_paths
        _ensure_paths()
        run_id = args.live or _time.strftime("run-%Y%m%d-%H%M%S")
        outputs_dir = RUNS / run_id / "outputs"
        outputs_dir.mkdir(parents=True, exist_ok=True)
        read_roots.append(outputs_dir)

    # cli is in the AGENT layer (echelon_engine/agent/) — it MAY import atoms directly
    # (the world-loops/agent-layer law); the scanner forbids apps->atoms, not agent->atoms.
    from echelon_engine.atoms.tools import ToolRegistry
    tools = ToolRegistry(args.root, allow_write=not args.read_only, allow_bash=not args.no_bash,
                         shell=args.shell, read_roots=read_roots)

    # THE BRAIN — the loop driver. Default model follows the chosen brain. `auto` consults the
    # EMPIRICAL routing table (_bakeoff/routing.json) — the bakeoff-proven driver pick, not a guess.
    from echelon_engine.atoms import routing
    alt_driver = None
    if args.brain == "auto":
        if args.model is None:
            args.model = routing.pick("driver")
        provider = routing.provider_for(args.model)
        # SIZE-AWARE DRIVER SPLIT (measured 2026-06-06): deepseek-chat drives small/sparse prompts
        # (<10k chars) where grok-build over-thinks + stalls (10-32s vs deepseek's 2.5-3s); the main
        # driver (grok-build, fast on big structured prompts) takes >=10k. Each model in its fast,
        # cheap band. Only when auto picks a grok driver — deepseek is the small-prompt complement.
        if args.model.startswith("grok") and not args.no_split_driver:
            from echelon_engine.atoms.providers.deepseek import DeepSeekProvider
            alt_driver = (DeepSeekProvider(), "deepseek-chat", args.split_threshold)
    elif args.brain == "deepseek":
        from echelon_engine.atoms.providers.deepseek import DeepSeekProvider
        provider = DeepSeekProvider()
        if args.model is None:
            args.model = "deepseek-chat"
    elif args.brain == "grokbuild":
        from echelon_engine.atoms.providers.grok import GrokProvider  # type: ignore[import]
        provider = GrokProvider()
        if args.model is None:
            args.model = "grok-build-0.1"
    elif args.brain == "eos":
        from echelon_engine.atoms.providers.eos_council import EosCouncilProvider  # type: ignore[import]
        provider = EosCouncilProvider()
        if args.model is None:
            args.model = provider.detect_model()   # whatever vLLM is actually serving (model-agnostic)
    else:
        from echelon_engine.atoms.providers.grok import GrokProvider  # type: ignore[import]
        provider = GrokProvider()
        if args.model is None:
            args.model = "grok-4.3"

    # ENV-SENSE: probe the real environment so the agent wakes knowing its body. The probe is
    # reconciled with the registry's ACTUAL chosen shell so <env> never lies about run_bash.
    # environment is in echelon_sdk (OK)
    from echelon_sdk.environment import probe as probe_env
    env = probe_env(args.root)
    env.shell_label, env.shell_path = (tools.shell_kind, tools.shell_exec or env.shell_path)
    env_block = env.render() if not args.no_env else None

    # One store, shared by the memory organ and the boot's soul scope (both live in core.db).
    # LAYERING-TENSION: SeedStore in echelon_engine.atoms.store (atoms layer).
    # Scanner law: apps must not import echelon_engine.atoms directly.
    _SeedStore = None
    if args.scope or args.boot or args.bank or args.cartridge:
        from echelon_engine.atoms.store import SeedStore as _SeedStore  # type: ignore[import]
    store = _SeedStore() if _SeedStore is not None else None

    # THE JUDGE — MANDATORY by default, and THE BRAIN JUDGES ITSELF.
    judge = judge_model = None
    if not args.no_judge:
        judge_model = args.judge_model or args.model   # the brain itself by default
        judge = provider if judge_model == args.model else routing.provider_for(judge_model)

    # THE KNOWLEDGE BANK (COS × ENTRY) — built on the same core.db, walled from the soul. Optional.
    bank = bank_semantic = None
    if args.bank:
        # LAYERING-TENSION: atoms layer; scanner law: apps must not import echelon_engine.atoms.
        from echelon_engine.atoms.bank import KnowledgeBank  # type: ignore[import]
        from echelon_engine.atoms.bank_embed import SemanticTier  # type: ignore[import]
        # share the store's UAME connection so the bank's links live in the same relationship-index.
        bank = KnowledgeBank(uame=store.u) if store is not None else KnowledgeBank()
        bank_semantic = SemanticTier(bank, source="own", store=store)  # our own embedder, $0
        tools.attach_bank(bank, semantic=bank_semantic, scope=args.scope or "")

    from echelon_engine.agent.loop import MemoryContext
    mem = None
    if args.scope or bank is not None:
        mem = MemoryContext(store, args.scope or "", judge_provider=judge, judge_model=judge_model,
                            bank=bank, bank_semantic=bank_semantic)

    # THE WAKING. The boot seeds + presents the soul and offers the `recall` gate.
    boot_ctx = None
    # LAYERING-TENSION: identity.SELF_SCOPE in echelon_engine.atoms.identity (atoms layer).
    # Scanner law: apps must not import echelon_engine.atoms directly.
    from echelon_engine.atoms.identity import SELF_SCOPE  # type: ignore[import]
    # scopegraph is in echelon_sdk (OK)
    from echelon_sdk.scopegraph import ScopeGraph

    soul_scope = SELF_SCOPE if args.soul == "echelon" else f"{args.soul}-self"
    if args.cartridge is not None:
        # LAYERING-TENSION: boot in echelon_engine.atoms.boot (atoms layer).
        # Scanner law: apps must not import echelon_engine.atoms directly.
        from echelon_engine.atoms.boot import cartridge_boot  # type: ignore[import]
        cart_scope = args.scope or soul_scope
        extra = [c for c in args.cartridge if c]   # --cartridge craft -> ['craft']
        boot_ctx = cartridge_boot(store, cart_scope, args.goal or "", cartridges=extra)
        sg = ScopeGraph() if args.scope else None
        tools.attach_recall(store, cart_scope, scope_graph=sg,
                            judge_provider=judge, judge_model=(judge_model or args.model))
        tools.attach_remember()
        tools.attach_dispute(cart_scope)
    elif args.boot:
        # LAYERING-TENSION: boot in echelon_engine.atoms.boot (atoms layer).
        from echelon_engine.atoms.boot import boot as run_boot  # type: ignore[import]
        boot_ctx = run_boot(store, self_scope=soul_scope)
        recall_scope = args.scope or soul_scope
        sg = ScopeGraph() if args.scope else None
        tools.attach_recall(store, recall_scope, scope_graph=sg,
                            judge_provider=judge, judge_model=(judge_model or args.model))
        tools.attach_remember()
        tools.attach_dispute(recall_scope)

    # THE LIVE BRIDGE (preferred) or the file-partner (fallback).
    bridge = None
    event_sink = _printer
    if args.live:
        # sibling in apps/world/ — ALLOWED (both in apps layer)
        from echelon_engine.agent.world.livebridge import LiveBridge
        bridge = LiveBridge(args.live)

        def event_sink(kind: str, data: dict, _p=_printer, _b=bridge):
            _p(kind, data)            # console (for an attached terminal)
            try:
                _b.emit(kind, data)   # live.jsonl (for a tailing partner) — unbuffered
                if kind == "step":    # heartbeat each step: "an agent is alive on this bridge"
                    _b.beat(step=data.get("n", 0), status="running")
            except Exception:
                pass                  # the stream must never break the loop

    # THE TIERS.
    budget = None
    if args.tiers or args.budget is not None:
        # LAYERING-TENSION: atoms layer; scanner law: apps must not import echelon_engine.atoms.
        from echelon_engine.atoms.providers.cost import Budget, DEFAULT_BUDGET, budget_key  # type: ignore[import]
        budget = Budget(key=budget_key(args.goal) if args.goal else None,
                        total=args.budget if args.budget is not None else DEFAULT_BUDGET)
        if args.tiers:
            from echelon_engine.atoms.providers.bridge import BridgeProvider  # type: ignore[import]
            tools.attach_reason(provider, BridgeProvider(), budget, emit=event_sink)

    # THE EYES — vision tier. R-0135 (2026-09-01): this block used to hardcode a grok/gemini
    # branch and never consult routing — the free vision organ was structurally unreachable no
    # matter what the table said (the 08-25 audit's sharpest miss), and gemini is now RETIRED
    # estate-wide. Routed by default: walk model_chain('vision') and take the first wire that
    # BUILDS (a missing key raises = the seat ABSTAINS, the chain falls through). An explicit
    # --vision-model is honored on its own wire.
    # OPEN-0070 (2): LAZY attach — resolving the wire walks the routing chain (roster weather
    # probes = urlopen), which a boot with no sight-need must not pay. Nothing resolves here;
    # the FIRST look_at_image call resolves once and caches. Boot stays offline when the goal
    # never asks to see.
    if not args.no_vision:
        _vision_cache: dict = {"p": None, "m": None}

        def _vision_resolve():
            if _vision_cache["p"] is None:
                if args.vision_model:
                    _vision_cache["p"], _vision_cache["m"] = \
                        routing.provider_for(args.vision_model), args.vision_model
                else:
                    for _m in routing.model_chain("vision"):
                        try:
                            _vision_cache["p"], _vision_cache["m"] = routing.provider_for(_m), _m
                            break
                        except Exception:
                            continue
                if _vision_cache["p"] is None:
                    raise RuntimeError("no vision tier buildable from the routing chain")
            return _vision_cache["p"], _vision_cache["m"]

        try:
            tools.attach_look(None, budget, vision_model="", emit=event_sink,
                              vision_resolver=_vision_resolve)
        except Exception as _e:
            _printer("vision_unavailable", {"why": f"{type(_e).__name__}: {_e}"})

    # THE REASONER-ASK.
    consult_resolver = None
    if args.live:
        from echelon_engine.agent.world.livebridge import make_resolver
        consult_resolver = make_resolver(bridge, args.partner_timeout)
    elif args.partner:
        consult_resolver = _make_file_partner(args.partner, args.partner_timeout)

    if budget is not None and not args.no_consult:
        # OPEN-0070 (2): LAZY attach — the audit-tier pick + provider walk is routing-chain work
        # (roster weather probes = urlopen). Nothing resolves at boot; the FIRST consult call
        # resolves once and caches.
        _consult_cache: dict = {"p": None, "m": None}

        def _consult_resolve():
            if _consult_cache["p"] is None:
                _consult_cache["m"] = routing.pick("audit")
                _consult_cache["p"] = routing.provider_for(
                    _consult_cache["m"], prefer_bridge=routing.via_bridge(_consult_cache["m"]))
            return _consult_cache["p"], _consult_cache["m"]

        try:
            tools.attach_consult(None, budget, reasoner_model="",
                                 resolver=consult_resolver, emit=event_sink,
                                 reasoner_resolver=_consult_resolve)
        except Exception as _e:
            _printer("consult_unavailable", {"why": f"{type(_e).__name__}: {_e}"})

    # THE SWARM.
    if store is not None and not args.no_swarm:
        try:
            tools.attach_swarm(provider, args.model, store, budget=budget,
                               max_agents=args.max_agents, emit=event_sink)
        except Exception as _e:
            _printer("swarm_unavailable", {"why": f"{type(_e).__name__}: {_e}"})

    # THE PARTNER SEAM.
    if consult_resolver is not None:
        tools.attach_partner(consult_resolver, emit=event_sink)

    if args.goal:
      print(f"GOAL: {args.goal}\nROOT: {tools.root}\nBRAIN: {provider.name}/{args.model}"
          + (f"\nMEMORY: scope={args.scope} (warmth ON)" + (f", judge={judge_model}" if judge else "") if mem else "")
          + (f"\nBOOT: rediscovery — {boot_ctx.presented} soul seeds, recall gate offered" if boot_ctx else "")
          + (f"\nTIERS: reason() hand ON — {budget.state()}" if budget else "")
          + (f"\nENV: {env.os_label}, run_bash→{tools.shell_kind} ({tools.shell_exec})" if env_block else "")
          + (f"\nGATE: ask-before-act ON — partner approves each action" if args.ask_before_act else ""))

    control = bridge.poll_control if bridge is not None else None
    clear_stop = (lambda: bridge.set_control(stop=False)) if bridge is not None else None

    # ── WORKFLOW MODE ──
    if args.workflow:
        from echelon_engine.agent import workflow as _wf
        # tiered_runner is a sibling in apps/world/ (ALLOWED: both apps layer)
        from echelon_engine.agent.world.tiered_runner import make_tiered_runner
        raw = sys.stdin.read() if args.workflow == "-" else Path(args.workflow).read_text(encoding="utf-8")
        try:
            plan = json.loads(raw)
        except json.JSONDecodeError as _e:
            print(f"=== WORKFLOW ERROR: plan is not valid JSON: {_e} ==="); return 1
        problems = _wf.validate_workflow(plan)
        if problems:
            print("=== WORKFLOW INVALID ===\n  " + "\n  ".join(problems)); return 1
        try:
            waves = _wf.compute_waves(plan)
        except ValueError as _e:
            print(f"=== WORKFLOW CYCLE: {_e} ==="); return 1
        routes: list = []
        # T3 floor providers
        _local = _deepseek = None
        _local_model = "smollm3-3b-gabliterated-i1"
        try:
            from echelon_engine.atoms.providers.eos_council import EosCouncilProvider  # type: ignore[import]
            _c = EosCouncilProvider()
            ok, _detail = _c.is_online()
            if ok:
                _local, _local_model = _c, _c.detect_model()
                print(f"T3 FREE FLOOR: AMD-T1 council online ({_local_model}) — the free tier.")
        except Exception:
            pass
        if _local is None:
            try:
                from echelon_engine.atoms.providers.local import LocalProvider  # type: ignore[import]
                _local = LocalProvider()
            except Exception:
                _local = None
        try:
            from echelon_engine.atoms.providers.deepseek import DeepSeekProvider  # type: ignore[import]
            _deepseek = DeepSeekProvider()
        except Exception:
            _deepseek = None
        runner = make_tiered_runner(provider, args.model, args.root, budget=budget,
                                    max_steps=args.max_steps, on_route=lambda r: routes.append(r),
                                    local=_local, local_model=_local_model,
                                    deepseek=_deepseek, deepseek_model="deepseek-v4-flash",
                                    guidance=args.guidance, files=args.files, image=args.image)
        sched = "fork-field" if args.fork_field else "waves"
        print(f"WORKFLOW: {len(plan.get('steps', []))} steps, {len(waves)} waves | scheduler={sched}"
              f" | BRAIN: {provider.name}/{args.model} | ROOT: {tools.root}")
        if args.fork_field:
            from echelon_engine.agent.fork_field import run_fork_field, make_bank_pager
            pager = make_bank_pager(store, args.scope or "echelon") if store is not None else None
            import uuid
            # NOT-YET-SCOPED: web.run_registry has no engine/sdk home yet
            try:
                from echelon_engine.agent.web.run_registry import load_runs, save_runs  # type: ignore[import]
                run_id = uuid.uuid4().hex
                runs = load_runs()
                runs[run_id] = {
                    "run_id": run_id,
                    "status": "running",
                    "scheduler": "fork-field",
                    "workflow": args.workflow,
                    "step_count": len(plan.get("steps", [])),
                    "started_at": _time.strftime("%Y-%m-%dT%H:%M:%SZ", _time.gmtime()),
                }
                save_runs(runs)
            except ImportError:
                pass  # run registry not available; fork-field proceeds without it
            wfres = run_fork_field(plan, run_step=runner, warmth_of=pager, on_event=event_sink,
                                   max_parallel=args.max_agents)
        else:
            wfres = _wf.run_workflow(plan, run_step=runner, on_event=event_sink,
                                     max_parallel=args.max_agents)
        n_t3 = sum(1 for r in routes if r.get("executor") == "T3-code")
        print(f"\n=== WORKFLOW {'OK' if wfres.get('ok') else 'INCOMPLETE'} "
              f"| {len(wfres.get('steps', []))} steps done"
              + (f", {wfres['ticks']} ticks, cold={wfres['cold']}" if args.fork_field else "")
              + f" | T3-floor atoms: {n_t3}/{len(routes)} ===")
        if wfres.get("undone"):
            print(f"UNDONE (cooled out / failed): {wfres['undone']}")
        for s in wfres.get("steps", []):
            print(f"  [{s.get('status'):9}] {s.get('id')} ({s.get('agent')}): {(s.get('answer') or '')[:160]}")
        if budget is not None:
            print(f"TOKENOMICS: {budget.state()}")
            bt = budget.by_tier()
            if bt:
                print("PER-TIER LEDGER:")
                for tier, d in sorted(bt.items()):
                    print(f"  {tier:9} calls={d['calls']:3} in={d['tokens_in']:>7} "
                          f"out={d['tokens_out']:>6} ${d['usd']:.4f}  {d['models']}")
            n_code = sum(1 for r in routes if r.get("executor") == "T3-code")
            if n_code:
                print(f"  {'T3-code':9} atoms={n_code:3} (mechanical, $0 — ran as Python, no model)")
        return 0 if wfres.get("ok") else 1

    # EARN THE CARTRIDGE FROM A COMMAND-CENTER RUN
    _craft_plugged = bool(args.cartridge and "craft" in [c for c in args.cartridge if c])
    _goal_writes = any(w in (args.goal or "").lower()
                       for w in ("write", "edit", "migrate", "rework", "redesign", "implement",
                                 "create", "add ", "fix ", "refactor", "change", "build"))

    def _newest_mtime() -> float:
        newest = 0.0
        try:
            for p in Path(tools.root).rglob("*"):
                if p.is_file() and not any(s in p.parts for s in (".git", "__pycache__", "node_modules", ".venv")):
                    m = p.stat().st_mtime
                    if m > newest:
                        newest = m
        except Exception:
            pass
        return newest

    _before_mtime = _newest_mtime() if (_craft_plugged and _goal_writes) else None

    from echelon_engine.agent.loop import run

    # THE HOP — unbounded run via transcript-flush + self-prepared spine (owner 2026-06-24).
    # Only meaningful with memory (the spine is an ephemeral pin; the relive re-grounds on the walk).
    pins = stash = None
    if args.hop_at_window is not None:
        from echelon_engine.pins import PinRegistry
        from echelon_engine.agent.loop_hop import StashStore
        pins = PinRegistry(async_lock=False)   # sync — the agent loop is synchronous
        stash = StashStore()
        tools.attach_pins(pins)
        tools.attach_spine(pins)               # re-wired with the live forecast inside run()
        tools.attach_stash(stash)
        # snooze_hop is registered inside run() (it closes over the loop's snooze state); attach a
        # no-op placeholder here so the schema exists pre-run — run() overwrites it with the live one.
        tools.attach_snooze(lambda n: {"snoozed_steps": int(n or 1), "until_step": 0})
        _printer("hop_enabled", {"hop_at_window": args.hop_at_window,
                                 "window_tokens": args.window_tokens})

    res = run(args.goal, provider, tools, model_id=args.model,
              max_steps=args.max_steps, max_drift=args.max_drift,
              progress_interval=args.progress_interval,
              on_event=event_sink, memory=mem, boot=boot_ctx,
              boot_judge=provider if boot_ctx else None, budget=budget,
              env_block=env_block, ask_before_act=args.ask_before_act, mode=args.mode,
              ttl_seconds=(args.ttl if args.ttl and args.ttl > 0 else None),
              outputs_dir=outputs_dir, control=control, clear_stop=clear_stop,
              alt_driver=alt_driver, keep_alive=args.keep_alive,
              pins=pins, stash=stash, hop_at_window=args.hop_at_window,
              window_tokens=args.window_tokens)

    if _craft_plugged:
        from echelon_engine.agent.partner import earn_craft_from_trace
        _completed = res.status == "completed"
        if _before_mtime is not None:
            _outcome_ok = _completed and (_newest_mtime() > _before_mtime)
        else:
            _outcome_ok = _completed
        _earned = earn_craft_from_trace(args.goal, outcome_ok=_outcome_ok)
        event_sink("craft_earned", {"outcome_ok": _outcome_ok, **(_earned or {})})

    # Final heartbeat
    if bridge is not None:
        bridge.beat(step=res.steps, status=res.status)

    print(f"\n=== {res.status.upper()} in {res.steps} steps "
          f"| tok {res.tokens_in}/{res.tokens_out} ===")
    if res.woke is not None:
        print(f"WOKE: {'yes — reached for its own past (texture)' if res.woke else 'no — never opened the gate (ran cold / drift)'}")
    if budget is not None:
        print(f"TOKENOMICS: {budget.state()}")
        for c in budget.calls:
            # journal-persisted records (BudgetMeter with a journal path) carry `usd` but not
            # `cost`/`spent_after` (cost.py _write_and_reload) - print what the record has.
            print(f"  {c.get('kind','?'):6} {c.get('model','?')}: in={c.get('in',0)} out={c.get('out',0)} "
                  f"cached={c.get('cached',0)} drew=${c.get('cost', c.get('usd', 0.0)):.6f}"
                  + (f" (spent ${c['spent_after']:.6f})" if 'spent_after' in c else ""))
        if bridge is not None:
            import json as _json
            by_model: dict = {}
            for c in budget.calls:
                m = by_model.setdefault(c["model"], {"model": c["model"], "calls": 0,
                                                     "tokens_in": 0, "tokens_out": 0, "usd": 0.0})
                m["calls"] += 1
                m["tokens_in"] += c["in"]; m["tokens_out"] += c["out"]; m["usd"] += c["cost"]
            receipt = {
                "session": bridge.dir.name, "brain": f"{provider.name}/{args.model}",
                "status": res.status, "steps": res.steps,
                "total_usd": round(budget.spent, 6),
                "driver_usd": round(budget.driver_spent, 6),
                "reason_usd": round(budget.reason_spent, 6),
                "by_model": [{**v, "usd": round(v["usd"], 6)} for v in by_model.values()],
            }
            try:
                (bridge.dir / "cost.json").write_text(_json.dumps(receipt), encoding="utf-8")
            except Exception:
                pass
    print(res.answer)
    return 0 if res.status == "completed" else 1


if __name__ == "__main__":
    sys.exit(main())
