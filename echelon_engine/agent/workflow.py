"""workflow.py -- T1 plans a DAG of role-assigned steps; the engine runs it in parallel by Gantt waves.

Owner 2026-06-07: "T1 agent will create a workflow based on the goal. it will pick the agent, for each
step. like a real world task assignment. difference, it will run in parallel. using a gantt method."

THE SHAPE:
  GOAL + FOLDER --(T1 plans)--> WORKFLOW = steps[{id, agent, task, depends_on[]}]
  --(topological)--> WAVES = [[s1], [s2, s3], [s4]]   (each wave = steps with deps satisfied)
  --(execute)--> each wave runs its steps IN PARALLEL (spawn_subagents onto the lived role devices),
                 the next wave starts when the prior completes. A real-world task board, run concurrently.

TWO WAYS TO GET THE PLAN (owner):
  - the T1 MODEL plans it (auto-routed / configurable tier) -- for the human web UI later.
  - an LLM (me) supplies the plan directly (the "T11" path) -- I AM T1, no model call.
Both produce the same WORKFLOW dict; the engine doesn't care who planned it.

PARALLELISM SAFETY: steps in a wave run as concurrent swarm workers sharing the substrate -- safe by
the same property that makes the society safe (append-only + content-addressed + lock-serialized;
uame-makes-parallel-swarm-safe). Each step boots on its role's LIVED device (role_devices/<role>/).

make_agent_runner lazy-imports loop (run, MemoryContext) and tools (ToolRegistry) at call time
(not at module import) so the module loads standalone. The engine modules are fully ported;
a missing import raises ImportError.

PERSISTENCE (tail/wfpersist, 2026-07-30): WorkflowRunJournal is an append-only JSONL journal
for workflow runs. Pass journal=<WorkflowRunJournal> to run_workflow / run_fork_field to enable
durable persistence (crash-safe: every line is O_APPEND+flush; a kill -9 leaves all events up to
the last flush readable). journal_dir=None (the default) = pure in-memory -- unchanged default,
same opt-in contract as Budget(key=None). The journal directory is resolved by _workflow_runs_dir():
ECHELON_WORKFLOW_RUNS_DIR > ECHELON_HOME/workflow_runs > ~/.echelon/workflow_runs.
"""
from __future__ import annotations

import json
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Callable

from echelon_sdk import roles as _roles


# -- persistence: append-only JSONL journal for workflow runs ------------------

def _persist_dispatch_session(path, session, continuity, on_event=None):
    from echelon_sdk.session import Session
    try:
        if session is not None:
            session.save(path, expected_digest=continuity.get("observed_digest"))
            result = {"status": "saved", "session_id": session.id,
                      "sha256": session._saved_digest, "boot_sha256": session.boot_sha256}
        else:
            result = Session.retire(path, expected_digest=continuity.get("observed_digest"))
    except Exception as exc:
        if on_event is not None:
            metadata = {"error_type": type(exc).__name__,
                        "task_effects": "not_rolled_back", "retry_safe": False,
                        "predecessor_id": continuity.get("predecessor_id"),
                        "expected_digest": continuity.get("observed_digest")}
            from echelon_sdk.session import SessionCommitUnknown
            if isinstance(exc, SessionCommitUnknown):
                metadata.update(operation=exc.operation, candidate_digest=exc.candidate_digest,
                                archive=exc.archive)
            on_event("session_persistence_failed", metadata)
        raise
    result.update(predecessor_id=continuity.get("predecessor_id"),
                  expected_digest=continuity.get("observed_digest"))
    if on_event is not None:
        on_event("session_persistence", result)
    return result


def _execution_manifest(model, tools, *, role, doctrine):
    import hashlib
    def digest(value):
        return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                        separators=(",", ":")).encode("utf-8")).hexdigest()
    return {"schema_version": 1, "model": model,
            "model_provenance": "requested_not_provider_attested",
            "tools_sha256": digest(tools.schemas()),
            "permissions": {"root": str(tools.root),
                            "read_roots": sorted(str(p) for p in tools.read_roots),
                            "allow_write": tools.allow_write, "allow_bash": tools.allow_bash,
                            "shell_exec": tools.shell_exec, "shell_kind": tools.shell_kind},
            "role": role, "doctrine_sha256": digest(doctrine)}


def _check_session_execution(session, manifest, continuity, on_event=None):
    decision = ({"status": "cold_start", "compatible": True} if session is None
                else session.check_boot_manifest(manifest))
    decision.update(predecessor_id=continuity.get("predecessor_id"),
                    expected_digest=continuity.get("observed_digest"),
                    policy="exact_manifest_v1")
    if on_event is not None:
        on_event("session_compatibility", decision)
    if not decision["compatible"]:
        raise RuntimeError("session execution context " + decision["status"] +
                           "; preserve predecessor and explicitly migrate before resume")
    return decision


def _load_session_for_dispatch(path, on_event=None):
    from echelon_sdk.session import Session
    result = Session.load_result(path)
    metadata = {key: value for key, value in result.items() if key != "session"}
    if on_event is not None:
        on_event("session_load", metadata)
    if result["status"] in {"corrupt", "unavailable"}:
        raise RuntimeError("saved session " + result["status"] + "; preserve and diagnose before replacement")
    if result["session"] is not None and result.get("boot_integrity") != "verified":
        raise RuntimeError("saved session boot integrity unverified; preserve predecessor before an explicit cold restart")
    return result["session"], metadata


def _workflow_runs_dir() -> Path:
    """Resolve the workflow-runs journal directory.
    ECHELON_WORKFLOW_RUNS_DIR > ECHELON_HOME/workflow_runs > ~/.echelon/workflow_runs."""
    if "ECHELON_WORKFLOW_RUNS_DIR" in os.environ:
        return Path(os.environ["ECHELON_WORKFLOW_RUNS_DIR"]).expanduser()
    if "ECHELON_HOME" in os.environ:
        return Path(os.environ["ECHELON_HOME"]).expanduser() / "workflow_runs"
    return Path.home() / ".echelon" / "workflow_runs"


class WorkflowRunJournal:
    """Append-only JSONL journal for ONE workflow run. Crash-safe: every write is O_APPEND + flush
    so a kill -9 mid-run leaves a readable file with all events up to the last flushed line.
    A partial last line is silently skipped on reload.

    journal_dir=None -> pure in-memory (no file I/O). The unchanged default.

    Each line is a self-contained JSON record: {kind, ts, run_id, ...data}.
    """

    def __init__(self, run_id: str, journal_dir: Path | None = None):
        self.run_id = run_id
        self._path: Path | None = None
        self._started = False
        if journal_dir is not None:
            journal_dir.mkdir(parents=True, exist_ok=True)
            self._path = journal_dir / f"{run_id}.jsonl"

    # -- write helpers ----------------------------------------------------------

    def _append(self, rec: dict) -> None:
        """Append one line to the journal. No-op when in-memory (no dir)."""
        if self._path is None:
            return
        rec.setdefault("run_id", self.run_id)
        rec.setdefault("ts", time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
        line = json.dumps(rec, ensure_ascii=False) + "\n"
        with open(self._path, "a", encoding="utf-8") as f:
            f.write(line)
            f.flush()

    def start(self, spec: dict) -> None:
        """Write the run-start record: plan, waves, metadata. Call ONCE at the start of a run."""
        if self._started:
            return
        self._started = True
        self._append({"kind": "run_start", **spec})

    def record(self, kind: str, data: dict) -> None:
        """Append one event line (step_start, step_done, wave_start, workflow_done, etc.)."""
        self._append({"kind": kind, **data})

    def finish(self, result: dict) -> None:
        """Write the terminal record. Call ONCE when the run completes (success or failure)."""
        self._append({"kind": "run_finish", **result})

    # -- read helpers (static — scan / load from a directory) -------------------

    @staticmethod
    def _read_journal(path: Path) -> list[dict] | None:
        """Read all lines from a journal file. Returns None if the file is missing/unreadable.
        A partial/trailing line (truncated by kill -9) is silently skipped."""
        if not path.exists():
            return None
        lines: list[dict] = []
        try:
            for ln in path.read_text(encoding="utf-8").splitlines():
                ln = ln.strip()
                if not ln:
                    continue
                try:
                    lines.append(json.loads(ln))
                except json.JSONDecodeError:
                    # partial last line from crash — skip, keep what we have
                    pass
        except OSError:
            return None
        return lines

    @staticmethod
    def list_runs(journal_dir: Path | None = None) -> list[dict]:
        """Scan the journal directory and return a summary for every run (newest first).
        Each summary: {run_id, status, started_at, finished_at, steps_total, steps_done, ok, cost}.

        journal_dir=None -> resolve via _workflow_runs_dir().
        """
        jd = journal_dir or _workflow_runs_dir()
        if not jd.exists():
            return []
        out: list[dict] = []
        # st_mtime_ns (not the float st_mtime) so two runs written moments apart don't
        # collapse into one timestamp; run_id breaks a genuine same-tick tie so the
        # order is STABLE across calls (within one tick, creation order is unknowable).
        for f in sorted(jd.iterdir(), key=lambda x: (x.stat().st_mtime_ns, x.stem), reverse=True):
            if not f.suffix == ".jsonl":
                continue
            run_id = f.stem
            lines = WorkflowRunJournal._read_journal(f)
            if not lines:
                continue
            # first line = run_start; last line = run_finish or whatever was last written
            first = lines[0]
            last = lines[-1]
            summary: dict = {
                "run_id": run_id,
                "status": "running",
                "started_at": first.get("ts", ""),
                "finished_at": None,
                "steps_total": first.get("steps", 0),
                "steps_done": sum(1 for ln in lines if ln.get("kind") == "step_done"),
                "ok": None,
                "cost": None,
                "file_size": f.stat().st_size,
            }
            if last.get("kind") == "run_finish":
                summary["status"] = "done" if last.get("ok") else "failed"
                summary["finished_at"] = last.get("ts", "")
                summary["ok"] = last.get("ok")
                summary["cost"] = last.get("cost")
            # enrich with metadata from run_start
            summary["goal"] = first.get("goal", "")
            summary["folder"] = first.get("folder", "")
            summary["scheduler"] = first.get("scheduler", "")
            out.append(summary)
        return out

    @staticmethod
    def load_run(run_id: str, journal_dir: Path | None = None) -> dict | None:
        """Load a single run's full journal. Returns {run_id, status, events:[...], result?}
        or None if the run is not found.

        journal_dir=None -> resolve via _workflow_runs_dir().
        """
        jd = journal_dir or _workflow_runs_dir()
        f = jd / f"{run_id}.jsonl"
        lines = WorkflowRunJournal._read_journal(f)
        if lines is None:
            return None
        if not lines:
            # empty file (created but never written to) — treat as a running run with no events
            return {
                "run_id": run_id,
                "status": "running",
                "started_at": "",
                "finished_at": None,
                "events": [],
                "result": None,
            }
        first = lines[0]
        last = lines[-1]
        status = "running"
        result = None
        if last.get("kind") == "run_finish":
            status = "done" if last.get("ok") else "failed"
            result = {
                "ok": last.get("ok"),
                "elapsed": last.get("elapsed"),
                "steps_done": last.get("steps_done"),
                "cost": last.get("cost"),
            }
        return {
            "run_id": run_id,
            "status": status,
            "started_at": first.get("ts", ""),
            "finished_at": last.get("ts") if last.get("kind") == "run_finish" else None,
            "events": lines,
            "result": result,
        }


# -- the plan shape ------------------------------------------------------------
def available_agents() -> list[dict]:
    """The cast T1 may assign -- each {agent, mission, lived} so the planner picks the right specialist."""
    out = []
    for rid, spec in _roles.ROLES.items():
        out.append({"agent": rid, "mission": spec.get("mission", ""),
                    "lived": _roles.role_device_dir(rid) is not None})
    return out


def validate_workflow(wf: dict) -> list[str]:
    """Assert a workflow is runnable: steps have unique ids, known agents, and depends_on that resolve
    + are acyclic. Returns a list of problems ([] = OK). The mechanical backstop before we execute."""
    problems: list[str] = []
    steps = wf.get("steps")
    if not isinstance(steps, list) or not steps:
        return ["workflow has no steps"]
    ids = [s.get("id") for s in steps]
    if len(set(ids)) != len(ids):
        problems.append(f"duplicate step ids: {[i for i in ids if ids.count(i) > 1]}")
    known_agents = set(_roles.ROLES.keys())
    idset = set(ids)
    for s in steps:
        sid = s.get("id")
        if not sid:
            problems.append("a step has no id")
        if not s.get("task"):
            problems.append(f"step {sid}: no task text")
        ag = s.get("agent")
        if ag not in known_agents:
            problems.append(f"step {sid}: unknown agent {ag!r} (known: {sorted(known_agents)})")
        for d in s.get("depends_on", []) or []:
            if d not in idset:
                problems.append(f"step {sid}: depends_on {d!r} is not a step id")
    # cycle check via topological sort (if waves can't cover all steps, there's a cycle)
    try:
        waves = compute_waves(wf)
        covered = sum(len(w) for w in waves)
        if covered != len(steps):
            problems.append("dependency cycle detected (not all steps schedulable)")
    except ValueError as e:
        problems.append(str(e))
    return problems


def compute_waves(wf: dict) -> list[list[str]]:
    """Topological scheduling: WAVES of step-ids that can run in PARALLEL. Wave k = every step whose
    deps are all satisfied by waves < k. This IS the Gantt -- each wave is a vertical slice of bars that
    run concurrently; the chart's depth = the critical path. Raises ValueError on a cycle."""
    steps = {s["id"]: set(s.get("depends_on", []) or []) for s in wf["steps"]}
    done: set[str] = set()
    waves: list[list[str]] = []
    while len(done) < len(steps):
        ready = [sid for sid, deps in steps.items() if sid not in done and deps <= done]
        if not ready:
            raise ValueError("dependency cycle -- no step is ready but steps remain")
        ready.sort()                      # stable order within a wave
        waves.append(ready)
        done |= set(ready)
    return waves


# -- the T1 planner (the MODEL path) -------------------------------------------
_PLANNER_SYS = (
    "You are T1, the ECHELON orchestrator -- the framing/architect tier (CV-012). You DECOMPOSE a goal "
    "into a WORKFLOW of steps and ASSIGN the right specialist agent to each, like a real-world project "
    "lead writing a task board. Steps that don't depend on each other MUST be independent so they run "
    "in PARALLEL. You do not do the work; you plan WHO does WHAT and in what ORDER.\n\n"
    "Return STRICT JSON only: {\"steps\":[{\"id\":\"s1\",\"agent\":\"<one of the agents>\",\"task\":"
    "\"<concrete instruction for that agent>\",\"depends_on\":[\"<prior step ids>\"]}]}. Rules: ids are "
    "short + unique; depends_on lists ONLY ids that must finish first (empty = can start immediately); "
    "maximise parallelism (don't serialize steps that are independent); assign each step to the agent "
    "whose mission fits best."
)


def plan_with_model(goal: str, folder: str, *, provider, model: str,
                    context: str = "") -> dict:
    """T1-the-MODEL plans the workflow. Returns the workflow dict (may be invalid -- caller validates).
    provider/model come from routing (auto-route) or the wizard's configured tier."""
    agents = available_agents()
    agent_list = "\n".join(f"  - {a['agent']}: {a['mission']}" for a in agents)
    user = (f"GOAL: {goal}\nTARGET FOLDER: {folder}\n"
            + (f"FOLDER CONTEXT:\n{context}\n" if context else "")
            + f"\nAVAILABLE AGENTS (assign each step to one):\n{agent_list}\n\n"
            "Produce the workflow JSON now.")
    resp = provider.send([{"role": "system", "content": _PLANNER_SYS},
                          {"role": "user", "content": user}], model_id=model)
    return _parse_plan(resp.content or ""), resp


def _parse_plan(text: str) -> dict:
    """Extract the workflow JSON from a model reply (tolerant of fences / prose around it)."""
    t = text.strip()
    if "```" in t:
        # take the first fenced block's body
        parts = t.split("```")
        for p in parts:
            p = p.strip()
            if p.startswith("json"):
                p = p[4:].strip()
            if p.startswith("{"):
                t = p
                break
    # else find the outermost { ... }
    if not t.startswith("{"):
        i, j = t.find("{"), t.rfind("}")
        if i >= 0 and j > i:
            t = t[i:j + 1]
    try:
        return json.loads(t)
    except json.JSONDecodeError:
        return {"steps": [], "_parse_error": text[:400]}


# -- the engine (run the DAG in parallel waves) --------------------------------
def run_workflow(wf: dict, *, run_step: Callable[[dict, dict], dict],
                 on_event: Callable[[str, dict], None] | None = None,
                 max_parallel: int = 6,
                 journal: WorkflowRunJournal | None = None) -> dict:
    """Execute a validated workflow wave-by-wave. Each wave's steps run IN PARALLEL via run_step (the
    injected executor -- the swarm in production, a stub in tests). run_step(step, prior_results) -> a
    result dict; prior_results maps step-id -> result so a step sees its dependencies' output.

    Returns {steps:[...with status/answer/timing...], waves:[...], started, finished, ok}. Never raises
    on a step failure -- a failed step is recorded; downstream steps still run (they may handle it), the
    way a real board keeps moving. This is the Gantt executed.

    journal=None (default): pure in-memory. Pass a WorkflowRunJournal to enable durable persistence
    (crash-safe append-only JSONL). The caller must call journal.start() BEFORE and journal.finish()
    AFTER — run_workflow records step/wave events through journal.record()."""
    def emit(k, d):
        if on_event:
            on_event(k, d)
        if journal is not None:
            journal.record(k, d)

    waves = compute_waves(wf)
    by_id = {s["id"]: s for s in wf["steps"]}
    results: dict[str, dict] = {}
    started = time.time()
    emit("workflow_start", {"steps": len(by_id), "waves": [list(w) for w in waves]})

    for wi, wave in enumerate(waves):
        emit("wave_start", {"wave": wi, "steps": wave})
        # the prior-results view a step in this wave may read (its deps are all in earlier waves)
        snapshot = dict(results)

        def _do(sid: str) -> tuple[str, dict]:
            step = by_id[sid]
            deps = {d: snapshot.get(d, {}) for d in (step.get("depends_on") or [])}
            t0 = time.time()
            emit("step_start", {"id": sid, "agent": step.get("agent"), "task": step.get("task", "")[:80]})
            try:
                r = run_step(step, deps)
            except Exception as e:  # a step dying must not kill the wave
                r = {"status": "error", "answer": repr(e)}
            r = {**r, "id": sid, "agent": step.get("agent"),
                 "elapsed": round(time.time() - t0, 2), "wave": wi}
            emit("step_done", {"id": sid, "agent": step.get("agent"),
                               "status": r.get("status"), "elapsed": r["elapsed"]})
            return sid, r

        with ThreadPoolExecutor(max_workers=min(max_parallel, len(wave))) as ex:
            for fut in as_completed([ex.submit(_do, sid) for sid in wave]):
                sid, r = fut.result()
                results[sid] = r
        emit("wave_done", {"wave": wi})

    finished = time.time()
    ordered = [results[s["id"]] for s in wf["steps"] if s["id"] in results]
    ok = all(r.get("status") not in ("error",) for r in ordered)
    out = {"steps": ordered, "waves": [list(w) for w in waves],
           "started": started, "finished": finished,
           "elapsed": round(finished - started, 2), "ok": ok}
    emit("workflow_done", {"ok": ok, "elapsed": out["elapsed"]})
    return out


# -- the real executor: run a step as a role-worker on its LIVED device ---------
def make_agent_runner(provider, model: str, folder: str, *, budget=None,
                      max_steps: int = 60,
                      guidance: str | None = None,
                      files: list[str] | None = None,
                      image: list[str] | None = None,
                      session_path: str | None = None,
                      on_event: Callable[[str, dict], None] | None = None,
                      control: Callable[[], dict] | None = None) -> Callable[[dict, dict], dict]:
    """Build the run_step the engine calls: each step is a full agent loop, booted INTO the step's role
    on its LIVED device (role_devices/<role>/core.db, scope persona:<role>), sandboxed to `folder`,
    bounded by the role's forbidden tools. A step sees its dependencies' answers in the goal preamble
    (real-world task hand-off). This is where the workflow becomes 14 specialists doing real work.
    The returned run_step accepts an optional tier= kwarg threaded into budget.charge calls so the
    full-agent-loop step's spend is tagged for by_tier accounting.

    OS-tier steering context: guidance (string prepended as system-reminder above the task),
    files (paths read and attached as grounding context in the preamble), image (paths for
    visual context passed to the agent). The plan dict is never modified.

    loop (run, MemoryContext), ToolRegistry, SeedStore, boot are lazy-imported
    at call time so the module loads standalone. All targets are in the ported engine;
    a missing import raises ImportError immediately."""

    def _import_loop():
        """Lazy import of loop.run + MemoryContext from the ported engine module."""
        try:
            from echelon_engine.agent.loop import run as _run, MemoryContext as _MC  # type: ignore
            return _run, _MC
        except ImportError as e:
            raise ImportError(
                f"loop.run / MemoryContext not available from echelon_engine.agent.loop: {e}"
            ) from e

    def _import_tools():
        """Lazy import of ToolRegistry from the ported engine module."""
        try:
            from echelon_engine.atoms.tools import ToolRegistry  # type: ignore
            return ToolRegistry
        except ImportError as e:
            raise ImportError(
                f"ToolRegistry not available from echelon_engine.atoms.tools: {e}"
            ) from e

    # Pre-read file contents for grounding context (done once, not per step)
    _grounding_parts: list[str] = []
    if files:
        import hashlib
        for index, fp in enumerate(files):
            try:
                p = Path(fp)
                content = p.read_bytes()
                decoded = content.decode("utf-8")
            except Exception as exc:
                if on_event is not None:
                    on_event("grounding_failed", {"input_index": index,
                             "error_type": type(exc).__name__, "worker_started": False})
                raise RuntimeError(f"required grounding input {index} unavailable; worker not started") from None
            _grounding_parts.append(
                f"\n--- grounding file: {fp} ---\n{decoded}\n--- end {fp} ---")
            if on_event is not None:
                on_event("grounding_loaded", {"input_index": index,
                         "sha256": hashlib.sha256(content).hexdigest(), "bytes": len(content)})
    _grounding = "".join(_grounding_parts) if _grounding_parts else ""

    def run_step(step: dict, deps: dict, *, tier: str | None = None) -> dict:
        _run, _MC = _import_loop()
        ToolRegistry = _import_tools()
        from echelon_engine.atoms.store import SeedStore
        from echelon_engine.atoms.boot import boot as _boot

        role = step.get("agent")
        task = step.get("task", "")
        # the hand-off: a step reads what its dependencies produced (like a worker reading the prior desk)
        handoff = ""
        for did, dr in (deps or {}).items():
            ans = (dr.get("answer") or "")[:600]
            if ans:
                handoff += f"\n[from {dr.get('agent', did)} ({did})]: {ans}\n"
        preamble = _roles.role_preamble(role)
        # Build goal_text: guidance as system-reminder, then preamble + task, then grounding context
        goal_text = ""
        if guidance:
            goal_text += f"[SYSTEM REMINDER]: {guidance}\n\n"
        goal_text += preamble + task
        if _grounding:
            goal_text += f"\n\nGROUNDING CONTEXT:\n{_grounding}"
        goal_text += (f"\n\nYOU ARE HANDED THIS FROM EARLIER STEPS:\n{handoff}" if handoff else "")
        goal_text += "\n\nWork within the target folder. State your result clearly, then finish."
        # bound the worker by the role's forbidden tools
        forbid = set(_roles.forbidden_tools(role))
        tools = ToolRegistry(folder, allow_write=(not forbid & {"write_file", "edit_file"}),
                             allow_bash=("run_bash" not in forbid), read_roots=[folder])
        for fb in forbid:
            tools._tools.pop(fb, None)
        from echelon_sdk.environment import probe as probe_env
        environment = probe_env(folder)
        environment.shell_label = tools.shell_kind
        environment.shell_path = tools.shell_exec or environment.shell_path
        env_block = environment.render()
        # boot on the role's LIVED device if it has one; else a fresh per-role scope on a temp store
        dev = _roles.role_device_dir(role)
        if dev is not None:
            store = SeedStore(str(dev / "core.db"))
            scope = _roles.role_device_scope(role)
        else:
            _roles.build_role(role, None)   # no store -> 0; worker runs without warmth (still bounded)
            store, scope = None, f"role-{role}"
        mem = _MC(store, scope, judge_provider=provider) if store is not None else None
        rb = None
        boot_health = {"status": "not_configured" if store is None else "available"}
        if store is not None:
            try:
                rb = _boot(store, scope)
            except Exception as exc:
                rb = None
                boot_health = {"status": "degraded", "role": role, "scope": scope,
                               "error_type": type(exc).__name__, "fallback": "without_memory_boot",
                               "substrate_consulted": False}
                if on_event is not None:
                    on_event("memory_boot_degraded", boot_health)
        # OBSERVABILITY: thread on_event down to the worker loop so EVERY step (act->observe,
        # tool calls, warmth, drift, driver) streams up -- not just the step's final status. The
        # loop already emits all of it (loop.run on_event); the chain used to DROP it here, which
        # is why a hung worker looked frozen (owner 2026-06-18: "we MUST be able to observe
        # everything"). We tag each event with the role so a multi-worker view stays legible.
        _oe = None
        if on_event is not None:
            def _oe(kind: str, data: dict) -> None:  # noqa: ANN001
                on_event(kind, {"role": role, **(data or {})})
        # THE SESSION DOOR (owner 2026-08-18): with a session_path, the boot
        # ritual is paid ONCE and persisted; every later run_step warm-resumes
        # (loop.py WARM RESUME — "we do NOT pay for it again"). A bloated
        # session deletes its file so the next run re-boots naturally.
        if session_path:
            from pathlib import Path as _P
            from echelon_sdk.session import Session as _Session
            from echelon_engine.agent.loop import run_task as _run_task
            _sess, _continuity = _load_session_for_dispatch(session_path, _oe)
            _manifest = _execution_manifest(model, tools, role=role,
                                            doctrine={"role_preamble": preamble, "guidance": guidance,
                                                      "environment": env_block})
            _compatibility = _check_session_execution(_sess, _manifest, _continuity, _oe)
            res, _sess2 = _run_task(goal_text, provider, tools, model,
                                    session=_sess, on_event=_oe,
                                    max_steps=max_steps, ttl_seconds=None,
                                    memory=mem, boot=rb, budget=budget,
                                    mode="auto", tier=tier, control=control, env_block=env_block)
            if _sess2 is not None and _sess is None:
                _sess2.bind_boot_manifest(_manifest)
            _persistence = _persist_dispatch_session(session_path, _sess2, _continuity, _oe)
            return {"status": res.status, "answer": res.answer, "steps": res.steps,
                    "session_continuity": _continuity, "session_persistence": _persistence,
                    "session_compatibility": _compatibility, "memory_boot": boot_health}
        res = _run(goal_text, provider, tools, model_id=model, max_steps=max_steps,
                   ttl_seconds=None, memory=mem, boot=rb, budget=budget, mode="auto", tier=tier,
                   on_event=_oe, control=control, env_block=env_block)
        return {"status": res.status, "answer": res.answer, "steps": res.steps,
                "memory_boot": boot_health}

    return run_step
