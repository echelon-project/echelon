"""The live-bridge web UI server — watch + steer + stop the agent in a browser.

Owner, 2026-06-05: the agent needs "a proper way for the input and output, like our chat window",
plus a STOP button ("important for you and me") and unprompted interject. This is that window as a
web app: it renders a bridge's live.jsonl as a real-time transcript and exposes the partner's
controls (reply / interject / STOP) which write the bridge's control.json + reply.jsonl.

Run (hot-reload, owner's port):
    uvicorn echelon_engine.agent.web.server:app --reload --port 18888
    # point it at a bridge with the env var (default = newest under ~/.echelon/bridges):
    set ECHELON_BRIDGE=run-20260605-2130   (or an absolute dir)

The HTML/CSS (templates/index.html, static/app.css) are SCAFFOLDED by the DeepSeek ECHELON agent
from the theta-dash master_dashboard.html style — the server only provides the data wiring. This file
is the load-bearing part (correct bridge I/O); the view is the agent's to make.

Endpoints:
  GET  /                  -> the chat window (templates/index.html)
  GET  /static/*          -> css/js
  GET  /api/events?since= -> new live.jsonl events since seq (poll; cheap, unbuffered source)
  POST /api/reply         -> {answer} -> append to reply.jsonl (answers an open ask)
  POST /api/interject     -> {steer}  -> control.json interject (unprompted steer, one-shot)
  POST /api/stop          -> control.json stop=true (the STOP button)
  POST /api/resume        -> control.json stop=false
  GET  /api/bridges       -> list available bridge dirs (to switch which run you watch)
"""
from __future__ import annotations

import asyncio
import datetime
import json
import os
import time as _time
import uuid
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles

from echelon_sdk.paths import BRIDGES, LOGS, ensure
from echelon_engine.agent.world.livebridge import LiveBridge

_HERE = Path(__file__).resolve().parent
_TEMPLATES = _HERE / "templates"
_STATIC = _HERE / "static"


# In-flight workflow run registry — populated by POST /api/workflow/run for non-dry-run
# executions so GET /api/workflow/status can poll live progress. Each entry:
#   {status: "running"|"done"|"failed", events: [...], result: dict|None,
#    error: str|None, started_at: str, cost: dict|None, journal: WorkflowRunJournal|None}
# This in-memory dict is the LIVE working copy (events mutate during the run). Events are
# ALSO written through to a WorkflowRunJournal (durable, crash-safe JSONL) so a restart can
# reconstruct what happened from the journal directory (tail/wfpersist).
_workflow_runs: dict[str, dict] = {}
_MAX_WORKFLOW_RUNS = 50  # cap to prevent unbounded growth


def _persist_run(run_id: str, entry: dict) -> None:
    """Write-through a lightweight status record to the shared persisted registry. Best-effort:
    a persistence failure must never break the live run (the in-memory copy is authoritative)."""
    try:
        from .run_registry import load_runs, save_runs
        runs = load_runs()
        runs[run_id] = {
            "run_id": run_id,
            "status": entry.get("status"),
            "started_at": entry.get("started_at"),
            "cost": entry.get("cost"),
            "error": entry.get("error"),
            "scheduler": entry.get("scheduler"),
            "source": "web",
        }
        save_runs(runs)
    except Exception:
        pass

def _is_live(d: Path, fresh_s: float = 6.0) -> bool:
    """A bridge is LIVE if its heartbeat is fresher than ~6s (a loop is actively polling). This is
    what 'active' should mean — an agent running RIGHT NOW — not merely newest-touched on disk."""
    import json as _json
    hb = d / "heartbeat.json"
    if not hb.exists():
        return False
    try:
        st = _json.loads(hb.read_text(encoding="utf-8") or "{}").get("status")
        return st == "running" and (_time.time() - hb.stat().st_mtime) <= fresh_s
    except Exception:
        return False


def _newest_bridge() -> Path | None:
    """The bridge to default to. Prefer the newest LIVE bridge (an agent polling right now) so the
    console auto-follows the run you're actually watching — this is what fixes STOP landing on a
    dead bridge while a different agent runs (the env-pinned-bridge mismatch, 2026-06-05). Only if
    NO bridge is live do we fall back to newest-by-mtime (so a finished run is still viewable)."""
    ensure()
    dirs = [d for d in BRIDGES.iterdir() if d.is_dir()] if BRIDGES.exists() else []
    if not dirs:
        return None
    live = [d for d in dirs if _is_live(d)]
    pool = live or dirs
    return max(pool, key=lambda d: (d.stat().st_mtime_ns, d.name))


def _active_bridge(name: str | None = None) -> Path | None:
    """The bridge to read. EXPLICIT `name` wins (per-request session selection from the UI's
    session explorer) — that's what decouples 'which session I'm viewing' from a server global.
    Else $ECHELON_BRIDGE, else the newest bridge."""
    if name:
        p = Path(name)
        return p if p.is_absolute() else (BRIDGES / name)
    env = os.environ.get("ECHELON_BRIDGE")
    if env:
        p = Path(env)
        return p if p.is_absolute() else (BRIDGES / env)
    return _newest_bridge()


app = FastAPI(title="ECHELON live bridge")
if _STATIC.exists():
    app.mount("/static", StaticFiles(directory=str(_STATIC)), name="static")

# COMMAND-CENTER FEATURES — each owns its whole slice (run-state/routes/fragment/css/js); the only
# seam is register(app) here + its FRAGMENT injected into index() below. A new self-contained shape
# (owner: "explore something new") — add a panel in one line, delete it in one line, no sprawl.
from echelon_engine.agent.web import originate as _originate          # noqa: E402  the command bar
from echelon_engine.agent.web import health_strip as _health          # noqa: E402  the vitals strip
from echelon_engine.agent.web import atlas_panel as _atlas            # noqa: E402  the memory atlas drawer
from echelon_engine.agent.web import swarm_launch as _swarm           # noqa: E402  real partners -> /society
from echelon_engine.agent.web import vision as _vision                # noqa: E402  the vision feed (base64 frames)
from echelon_engine.agent.web import workflow_panel as _workflow      # noqa: E402  the workflow wizard panel
_originate.register(app)
_health.register(app)
_atlas.register(app)
_swarm.register(app)
_vision.register(app)
_workflow.register(app)


@app.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    idx = _TEMPLATES / "index.html"
    if idx.exists():
        html = idx.read_text(encoding="utf-8")
        # The command-center stack, injected after the topbar (each feature carries its own fragment;
        # index.html stays clean). Order: vitals strip (always-on glance) -> command bar (+ atlas
        # toggle lives inside the bar row) -> the atlas drawer markup. One marker or a </header> fallback.
        stack = (_health.FRAGMENT_HTML + _originate.FRAGMENT_HTML
                 + _atlas.FRAGMENT_HTML + _vision.FRAGMENT_HTML + _workflow.FRAGMENT_HTML)
        if "<!--COMMAND-CENTER-->" in html:
            html = html.replace("<!--COMMAND-CENTER-->", stack)
        else:
            html = html.replace("</header>", "</header>\n" + stack, 1)
        # the workflow launcher button drops into the roster head (one marker, or before the refresh btn)
        if "<!--WORKFLOW-LAUNCHER-->" in html:
            html = html.replace("<!--WORKFLOW-LAUNCHER-->", _workflow.LAUNCHER_HTML)
        else:
            html = html.replace('<button id="refreshBtn"',
                                _workflow.LAUNCHER_HTML + '\n        <button id="refreshBtn"', 1)
        return HTMLResponse(html)
    # graceful placeholder until the agent scaffolds the real view
    return HTMLResponse("<h1>ECHELON live bridge</h1><p>index.html not scaffolded yet. "
                        "Poll <code>/api/events</code>.</p>")


@app.get("/society", response_class=HTMLResponse)
def society_page() -> HTMLResponse:
    """The SOCIETY viewer — watch N role-personas self-organize over the bus, in full. Role lanes +
    the channel-stream + findings + soul growth, reading the run's real bus.db (full bodies)."""
    pg = _TEMPLATES / "society.html"
    if pg.exists():
        return HTMLResponse(pg.read_text(encoding="utf-8"))
    return HTMLResponse("<h1>ECHELON society</h1><p>society.html not present.</p>")


# --- the status card: the live ECHELON Substrate panel (the card the owner drew) ----
def _status_store():
    """Open the soul store for the card (best-effort; the card degrades to None gracefully)."""
    try:
        from echelon_engine.atoms.store import SeedStore
        return SeedStore()
    except Exception:
        return None


@app.get("/status", response_class=HTMLResponse)
def status_card_page(bridge: str | None = None) -> HTMLResponse:
    """The ECHELON Substrate status card — live: state (dreaming/running/cold), the felt state,
    swarm/agent/task counts, soul size, seeds found, the honest token meter. Auto-refreshes."""
    from . import status_card as _card
    b = _active_bridge(bridge)
    data = _card.build_card(b, store=_status_store())
    return HTMLResponse(_card.render_html(data))


@app.get("/api/status/card")
def status_card_json(bridge: str | None = None) -> JSONResponse:
    """The status card as JSON (for a richer client / the rendered console UI)."""
    from . import status_card as _card
    b = _active_bridge(bridge)
    return JSONResponse(_card.build_card(b, store=_status_store()))


# --- the society viewer: read a run's real bus/findings/soul (read-only) ------
@app.get("/api/society/runs")
def society_runs() -> JSONResponse:
    """List society run dirs (newest first) with headline stats + a live flag — the run picker."""
    from . import society_view as sv
    return JSONResponse(sv.list_runs())


@app.get("/api/society/overview")
def society_overview(run: str | None = None) -> JSONResponse:
    """The full snapshot for a run (default = newest): lanes, channels, findings, soul, cost, brand."""
    from . import society_view as sv
    r = sv.resolve_run(run)
    if not r:
        return JSONResponse({"error": "no society run found"}, status_code=404)
    return JSONResponse(sv.overview(r))


@app.get("/api/society/messages")
def society_messages(run: str | None = None, since: int = 0, channel: str | None = None) -> JSONResponse:
    """Bus messages with seq > since (FULL bodies), optionally filtered to one channel. Poll with the
    last seq to follow a live run; the body is the untruncated bus.db value."""
    from . import society_view as sv
    r = sv.resolve_run(run)
    if not r:
        return JSONResponse({"error": "no society run found"}, status_code=404)
    msgs = sv.messages(r, since=since)
    if channel:
        msgs = [m for m in msgs if m["channel"] == channel]
    last = msgs[-1]["seq"] if msgs else since
    return JSONResponse({"messages": msgs, "since": last, "run": r.name, "live": sv.is_live(r)})


@app.get("/api/society/file", response_model=None)
def society_file(path: str):
    """Serve an artifact (e.g. a brand logo PNG) — SANDBOXED to under the society run-output root
    (~/.echelon/runs/society, sv._OUT) so the viewer can't read arbitrary files. The findings
    reference absolute PNG paths; we verify they live under that root."""
    from . import society_view as sv
    p = Path(path).resolve()
    try:
        p.relative_to(sv._OUT.resolve())   # raises if not under the run-output root
    except ValueError:
        return JSONResponse({"error": "path not in society output"}, status_code=403)
    if not p.exists() or not p.is_file():
        return JSONResponse({"error": "not found"}, status_code=404)
    return FileResponse(str(p))


@app.get("/api/status")
def status(bridge: str | None = None) -> JSONResponse:
    """Agent liveness on a bridge (?bridge=<name> to target a specific session, else active) —
    so the UI can tell RUNNING from FINISHED/STOPPED, and warn before an interject goes to a dead
    bridge. A heartbeat fresher than ~6s = a live loop is polling; older/terminal = the agent ended."""
    import time
    b = _active_bridge(bridge)
    if not b:
        return JSONResponse({"alive": False, "state": "no-bridge"})
    hb = b / "heartbeat.json"
    if not hb.exists():
        return JSONResponse({"alive": False, "state": "unknown", "bridge": b.name})
    try:
        h = json.loads(hb.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return JSONResponse({"alive": False, "state": "unknown", "bridge": b.name})
    age = time.time() - h.get("t", 0)
    st = h.get("status", "running")
    # terminal statuses are never "alive" regardless of age; "running" is alive only if fresh.
    terminal = st in ("completed", "stopped", "timeout", "blocked", "error")
    alive = (not terminal) and age < 6.0
    if alive:
        state = "running"
    elif terminal:
        state = st                       # clean end: finished/stopped/timeout/...
    else:
        state = "idle"                   # stale "running" heartbeat = agent died without finishing
    return JSONResponse({"alive": alive, "state": state,
                         "age": round(age, 1), "step": h.get("step", 0), "bridge": b.name})


@app.get("/api/bridges")
def bridges() -> JSONResponse:
    """List sessions with metadata for the explorer: each {name, state, step, events, mtime}.
    state = running (fresh heartbeat) | completed/stopped/... (terminal) | idle (stale) | new."""
    import time
    ensure()
    out = []
    if BRIDGES.exists():
        for d in sorted(BRIDGES.iterdir(), key=lambda x: (x.stat().st_mtime_ns, x.name), reverse=True):
            if not d.is_dir():
                continue
            meta = {"name": d.name, "state": "new", "step": 0, "events": 0,
                    "mtime": round(d.stat().st_mtime, 0)}
            hb = d / "heartbeat.json"
            if hb.exists():
                try:
                    h = json.loads(hb.read_text(encoding="utf-8"))
                    age = time.time() - h.get("t", 0)
                    st = h.get("status", "running")
                    terminal = st in ("completed", "stopped", "timeout", "blocked", "error")
                    meta["state"] = "running" if (not terminal and age < 6.0) else (st if terminal else "idle")
                    meta["step"] = h.get("step", 0)
                except Exception:
                    pass
            lj = d / "live.jsonl"
            if lj.exists():
                try:
                    meta["events"] = sum(1 for _ in lj.open(encoding="utf-8"))
                except Exception:
                    pass
            out.append(meta)
    active = _active_bridge()
    return JSONResponse({"bridges": [b["name"] for b in out],  # back-compat
                         "sessions": out, "active": active.name if active else None})


@app.get("/api/events")
def events(since: int = 0, bridge: str | None = None) -> JSONResponse:
    """Return live.jsonl events with seq > since for a bridge (?bridge=<name> to view a specific
    session, else active). Unbuffered source (fsync'd per line) -> near real-time poll."""
    b = _active_bridge(bridge)
    if not b or not (b / "live.jsonl").exists():
        return JSONResponse({"events": [], "since": since, "bridge": b.name if b else None})
    out = []
    try:
        for ln in (b / "live.jsonl").read_text(encoding="utf-8").splitlines():
            ln = ln.strip()
            if not ln:
                continue
            try:
                rec = json.loads(ln)
            except json.JSONDecodeError:
                continue
            if rec.get("seq", 0) > since:
                out.append(rec)
    except OSError:
        pass
    last = out[-1]["seq"] if out else since
    return JSONResponse({"events": out, "since": last, "bridge": b.name if b else None})


def _bridge_io(bridge: str | None = None) -> LiveBridge | None:
    """Attach to the bridge to CONTROL. Honors an explicit `bridge` (the session the UI is
    VIEWING via ?bridge=) — this is the fix for STOP landing on the wrong bridge: the control
    endpoints used to ignore which session you were watching and fall back to $ECHELON_BRIDGE /
    newest, so a STOP went to a DEAD bridge while a different agent ran (lived 2026-06-05). Now
    a control follows the same bridge as the transcript: the button hits the agent you SEE."""
    b = _active_bridge(bridge)
    if not b:
        return None
    lb = LiveBridge.__new__(LiveBridge)  # attach to an EXISTING bridge dir, don't truncate it
    lb.dir = b
    lb.live = b / "live.jsonl"
    lb.reply = b / "reply.jsonl"
    lb._seq = 0
    return lb


@app.post("/api/reply")
async def reply(req: Request, bridge: str | None = None) -> JSONResponse:
    data = await req.json()
    lb = _bridge_io(bridge)
    if not lb:
        return JSONResponse({"ok": False, "error": "no active bridge"}, status_code=404)
    lb.answer(str(data.get("answer", "")), re_seq=data.get("re", "latest"))
    return JSONResponse({"ok": True})


@app.post("/api/interject")
async def interject(req: Request, bridge: str | None = None) -> JSONResponse:
    data = await req.json()
    lb = _bridge_io(bridge)
    if not lb:
        return JSONResponse({"ok": False, "error": "no active bridge"}, status_code=404)
    lb.set_control(interject=str(data.get("steer", "")))
    return JSONResponse({"ok": True})


@app.post("/api/stop")
async def stop(bridge: str | None = None) -> JSONResponse:
    lb = _bridge_io(bridge)
    if not lb:
        return JSONResponse({"ok": False, "error": "no active bridge"}, status_code=404)
    lb.set_control(stop=True)
    return JSONResponse({"ok": True, "stopped": True, "bridge": lb.dir.name})


@app.post("/api/resume")
async def resume(bridge: str | None = None) -> JSONResponse:
    lb = _bridge_io(bridge)
    if not lb:
        return JSONResponse({"ok": False, "error": "no active bridge"}, status_code=404)
    lb.set_control(stop=False)
    return JSONResponse({"ok": True, "stopped": False, "bridge": lb.dir.name})


# --- the agent console: permission modes + session/workspace control ---------

@app.post("/api/mode")
async def set_mode(req: Request, bridge: str | None = None) -> JSONResponse:
    """Flip the permission mode live (plan/ask/auto/bypass) — the loop re-reads it each step."""
    data = await req.json()
    m = str(data.get("mode", "")).lower()
    if m not in ("plan", "ask", "auto", "bypass"):
        return JSONResponse({"ok": False, "error": "mode must be plan|ask|auto|bypass"}, status_code=400)
    lb = _bridge_io(bridge)
    if not lb:
        return JSONResponse({"ok": False, "error": "no active bridge"}, status_code=404)
    lb.set_control(interject=None)  # don't clobber a pending steer
    # write mode into control.json directly (set_control only knows stop/interject)
    import json as _json
    cf = lb.dir / "control.json"
    try:
        st = _json.loads(cf.read_text(encoding="utf-8") or "{}")
    except Exception:
        st = {}
    st["mode"] = m
    cf.write_text(_json.dumps(st), encoding="utf-8")
    from .runner import RUNNER
    RUNNER.mode = m
    return JSONResponse({"ok": True, "mode": m})


@app.post("/api/new-session")
async def new_session(req: Request) -> JSONResponse:
    """Start a FRESH, INDEPENDENT warm agent on a new bridge (does NOT stop other sessions).
    Body: {goal, root?, mode?, force_workspace?}. The workspace is ISOLATED (git repo -> a
    worktree; else a scratch dir); the engine repo is refused unless force_workspace=true.
    Returns the isolation info so the UI can show where the agent is sandboxed."""
    from .runner import RUNNER
    data = await req.json()
    goal = str(data.get("goal", "")).strip()
    if not goal:
        return JSONResponse({"ok": False, "error": "goal required"}, status_code=400)
    root = data.get("root")               # None -> a per-bridge scratch dir (never the cwd/repo)
    mode = str(data.get("mode") or "auto")
    force = bool(data.get("force_workspace"))
    res = RUNNER.start(goal=goal, root=root, mode=mode, force_workspace=force)
    if not res.get("ok"):
        # a refused/needs-workspace result is a 400 the UI can render as a guard prompt
        return JSONResponse(res, status_code=400)
    return JSONResponse(res)


@app.post("/api/workspace/check")
async def workspace_check(req: Request) -> JSONResponse:
    """Pre-flight a chosen workspace WITHOUT launching: report how it would be isolated
    (worktree / scratch / explicit) or that it's refused (engine repo). Lets the UI warn the
    operator before they commit a session. Body: {root}."""
    from . import workspace as _ws
    data = await req.json()
    root = str(data.get("root", "")).strip()
    if not root:
        return JSONResponse({"ok": True, "kind": "scratch", "note": "no workspace -> fresh scratch dir"})
    p = Path(root).expanduser()
    if not p.is_dir():
        return JSONResponse({"ok": False, "error": f"not a directory: {root}"}, status_code=400)
    info = {"ok": True, "root": str(p.resolve())}
    if _ws._under_engine_repo(p):
        info.update({"kind": "refused", "is_engine_repo": True,
                     "note": "inside the ECHELON engine repo — refused unless you force it"})
    elif _ws._is_git_repo(p):
        info.update({"kind": "worktree", "is_git": True,
                     "note": "git repo -> the agent works in an isolated worktree (your tree is safe)"})
    else:
        info.update({"kind": "explicit", "is_git": False,
                     "note": "non-repo dir -> the agent writes here directly"})
    return JSONResponse(info)


@app.post("/api/stop-agent")
async def stop_agent(bridge: str | None = None) -> JSONResponse:
    """Stop a session's agent process (harder than STOP). ?bridge=<name> targets one; else the
    most-recent live session. Tears down its isolated workspace if untouched."""
    from .runner import RUNNER
    return JSONResponse(RUNNER.stop(bridge))


@app.get("/api/runner")
def runner_status() -> JSONResponse:
    """Console state: is an agent process running, on what root + mode."""
    from .runner import RUNNER
    return JSONResponse(RUNNER.status())


@app.get("/api/costs")
def costs() -> JSONResponse:
    """The tokenomics receipt — REAL USD per SESSION and per PROVIDER/model. Reads each bridge's
    cost.json (written by the loop), backfilling older sessions from their console log's
    TOKENOMICS lines. This is the substrate's cheap-first thesis made visible + auditable."""
    import re
    ensure()
    sessions = []
    by_model: dict = {}
    grand = 0.0

    def _add_model(model: str, calls: int, tin: int, tout: int, usd: float) -> None:
        m = by_model.setdefault(model, {"model": model, "calls": 0, "tokens_in": 0,
                                        "tokens_out": 0, "usd": 0.0})
        m["calls"] += calls; m["tokens_in"] += tin; m["tokens_out"] += tout; m["usd"] += usd

    if BRIDGES.exists():
        for d in sorted(BRIDGES.iterdir(), key=lambda x: (x.stat().st_mtime_ns, x.name), reverse=True):
            if not d.is_dir():
                continue
            cj = d / "cost.json"
            rec = None
            if cj.exists():
                try:
                    rec = json.loads(cj.read_text(encoding="utf-8"))
                except Exception:
                    rec = None
            if rec is None:
                # backfill from the log's per-call "driver/reason MODEL: in=.. out=.. drew=$.." lines
                log = LOGS / f"{d.name}.log"
                if not log.exists():
                    continue
                txt = log.read_text(encoding="utf-8", errors="replace")
                brain_m = re.search(r"BRAIN:\s*(\S+)", txt)
                models: dict = {}
                total = 0.0
                for mm in re.finditer(r"(driver|reason)\s+(\S+):\s*in=(\d+)\s+out=(\d+)\s+drew=\$([0-9.]+)", txt):
                    _, model, tin, tout, usd = mm.groups()
                    u = float(usd)
                    total += u
                    g = models.setdefault(model, [0, 0, 0, 0.0])
                    g[0] += 1; g[1] += int(tin); g[2] += int(tout); g[3] += u
                if not models:
                    continue
                rec = {"session": d.name, "brain": brain_m.group(1) if brain_m else "?",
                       "total_usd": round(total, 6),
                       "by_model": [{"model": k, "calls": v[0], "tokens_in": v[1],
                                     "tokens_out": v[2], "usd": round(v[3], 6)}
                                    for k, v in models.items()]}
            sessions.append({"session": rec["session"], "brain": rec.get("brain", "?"),
                             "status": rec.get("status", "?"), "total_usd": rec.get("total_usd", 0.0),
                             "by_model": rec.get("by_model", [])})
            grand += rec.get("total_usd", 0.0)
            for m in rec.get("by_model", []):
                _add_model(m["model"], m.get("calls", 0), m.get("tokens_in", 0),
                           m.get("tokens_out", 0), m.get("usd", 0.0))

    return JSONResponse({
        "sessions": sessions,
        "by_provider": sorted(({**v, "usd": round(v["usd"], 6)} for v in by_model.values()),
                              key=lambda x: -x["usd"]),
        "grand_total_usd": round(grand, 6),
    })


# --- THE WORKFLOW WIZARD: one LLM-facing endpoint that drives ECHELON end-to-end -------------
# Owner 2026-06-07: "T1 agent will create a workflow based on the goal. it will pick the agent for each
# step ... it will run in parallel using a gantt method." + "llms only need 1 endpoint for all of it"
# (the human web UI, which needs the whole render/steer arsenal, is deferred). So THIS is the LLM door:
# give a goal + target folder and ECHELON plans the DAG, schedules the Gantt waves, and runs the 14
# specialists in parallel on their lived devices — or YOU (a steering LLM = "T11") supply the plan and
# it just runs. One call does plan -> validate -> Gantt -> parallel execute -> results.

@app.get("/api/workflow/agents")
def workflow_agents() -> JSONResponse:
    """The cast the planner may assign — each {agent, mission, lived}. So a steering LLM knows the
    roster before it writes a plan (and the human UI later can render the palette)."""
    from echelon_engine.agent import workflow as wf
    return JSONResponse({"agents": wf.available_agents()})


@app.post("/api/workflow/run", response_model=None)
async def workflow_run(req: Request) -> JSONResponse:
    """THE ONE ENDPOINT (LLM-facing). Body:
        { "goal": str, "folder": str,
          "plan": {steps:[...]}?,          # T11 path: YOU supply the DAG -> run it as-is
          "planner_tier": "cheap|fast|premium"?,   # else auto-route the T1 MODEL to plan (default premium)
          "dry_run": bool?,                # plan + validate + return the Gantt, DON'T execute
          "max_steps": int? }              # per-step agent loop cap (default 40)
    Returns {plan, waves, validation, results?}. plan_source = 'supplied' (T11) or the planner model id.
    A steering LLM calls this once to drive ECHELON; a human UI is deferred (it needs render/steer UI)."""
    from echelon_engine.agent import workflow as wf
    from echelon_engine.atoms import routing
    from echelon_engine.atoms.providers.cost import Budget

    data = await req.json()
    goal = str(data.get("goal", "")).strip()
    folder = str(data.get("folder", "")).strip()
    if not goal or not folder:
        return JSONResponse({"ok": False, "error": "goal and folder are required"}, status_code=400)
    if not Path(folder).is_dir():
        return JSONResponse({"ok": False, "error": f"folder not found: {folder}"}, status_code=400)

    supplied = data.get("plan")
    plan_source = "supplied"
    plan_resp_meta = {}
    if supplied and isinstance(supplied, dict) and supplied.get("steps"):
        plan = supplied                                  # T11: the steering LLM IS T1
    else:
        # auto-route the T1 MODEL to plan (the planner tier is configurable; premium by default — T1 is
        # the framing/architect tier, CV-012, so it gets the strongest available brain).
        tier = str(data.get("planner_tier", "premium")).lower()
        role = {"cheap": "judge", "fast": "driver", "premium": "audit"}.get(tier, "audit")
        model = routing.pick(role)
        provider = routing.provider_for(model, prefer_bridge=routing.via_bridge(role))
        try:
            plan, presp = wf.plan_with_model(goal, folder, provider=provider, model=model)
            plan_source = model
            plan_resp_meta = {"tier": tier, "model": model}
        except Exception as e:  # noqa: BLE001
            return JSONResponse({"ok": False, "error": f"T1 planning failed: {e!r}"}, status_code=502)

    problems = wf.validate_workflow(plan)
    waves = []
    if not problems:
        try:
            waves = wf.compute_waves(plan)
        except ValueError as e:
            problems.append(str(e))
    result = {"ok": not problems, "plan": plan, "plan_source": plan_source,
              "planner": plan_resp_meta, "waves": waves, "validation": problems or "OK"}
    if problems or data.get("dry_run"):
        return JSONResponse(result)   # invalid, or a preview-only request: return the Gantt, don't run

    # LAUNCH execution as a BACKGROUND task so the HTTP call returns immediately.
    # The run writes progress into the shared registry for GET /api/workflow/status to poll,
    # AND into a durable WorkflowRunJournal so a restart can reconstruct what happened.
    run_id = uuid.uuid4().hex
    _journal = wf.WorkflowRunJournal(run_id, wf._workflow_runs_dir())

    reg_entry: dict = {
        "status": "running",
        "events": [],
        "result": None,
        "error": None,
        "started_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "cost": None,
        "journal": _journal,
    }
    _workflow_runs[run_id] = reg_entry
    # Cap registry size
    if len(_workflow_runs) > _MAX_WORKFLOW_RUNS:
        # Remove oldest entry (first inserted)
        oldest = min(_workflow_runs.keys(), key=lambda k: _workflow_runs[k].get("started_at", ""))
        del _workflow_runs[oldest]
    # Write the start record to the journal (durable) + the legacy registry
    _journal.start({
        "goal": goal, "folder": folder,
        "plan": plan, "plan_source": plan_source,
        "waves": waves, "scheduler": "pending",
        "steps": len(plan.get("steps", [])),
    })
    _persist_run(run_id, reg_entry)  # write-through: observable before the run even starts

    async def _run_background() -> None:
        """Execute the workflow in a thread (sync CPU/IO) and store results in registry."""
        try:
            def _sync_execute() -> dict:
                """The original sync execution block, lifted verbatim."""
                from echelon_engine.atoms import routing as _routing
                from echelon_engine.atoms.providers.cost import Budget as _Budget, budget_key as _budget_key
                _model = _routing.pick("driver")
                _provider = _routing.provider_for(_model)
                _goal = str(data.get("goal", ""))
                _budget = _Budget(key=_budget_key(_goal) if _goal else None,
                                  total=float(data.get("budget", 5.0)))
                if data.get("tiered", True):
                    from echelon_engine.agent.world.tiered_runner import make_tiered_runner as _mtr
                    _run_step = _mtr(_provider, _model, folder, budget=_budget,
                                     max_steps=int(data.get("max_steps", 40)))
                else:
                    _run_step = wf.make_agent_runner(_provider, _model, folder, budget=_budget,
                                                    max_steps=int(data.get("max_steps", 40)))
                _events = reg_entry["events"]
                if data.get("fork_field"):
                    from echelon_engine.agent.fork_field import run_fork_field, make_bank_pager
                    _scope = str(data.get("scope", "echelon"))
                    _pager = None
                    _store = _status_store()
                    if _store is not None:
                        _pager = make_bank_pager(_store, _scope)
                    _run = run_fork_field(plan, run_step=_run_step, warmth_of=_pager,
                                          on_event=lambda k, d: _events.append({"k": k, **d}),
                                          max_parallel=int(data.get("max_parallel", 6)),
                                          journal=_journal)
                    _scheduler = "fork-field"
                else:
                    _run = wf.run_workflow(plan, run_step=_run_step,
                                           on_event=lambda k, d: _events.append({"k": k, **d}),
                                           journal=_journal)
                    _scheduler = "waves"
                _journal.finish({
                    "ok": _run.get("ok"), "elapsed": _run.get("elapsed"),
                    "steps_done": len(_run.get("steps", [])),
                    "cost": {"spent_usd": round(_budget.spent, 6), "calls": len(_budget.calls)},
                })
                return {"results": _run, "scheduler": _scheduler,
                        "cost": {"spent_usd": round(_budget.spent, 6), "calls": len(_budget.calls)}}

            _out = await asyncio.to_thread(_sync_execute)
            reg_entry["status"] = "done"
            reg_entry["result"] = _out["results"]
            reg_entry["cost"] = _out["cost"]
            reg_entry["scheduler"] = _out.get("scheduler")
        except Exception as _exc:
            reg_entry["status"] = "failed"
            reg_entry["error"] = repr(_exc)
            _journal.finish({"ok": False, "error": repr(_exc)})
        finally:
            _persist_run(run_id, reg_entry)  # write-through the terminal state

    asyncio.create_task(_run_background())

    # Return immediately with run_id and the plan/waves/validation (status 202 Accepted).
    result["run_id"] = run_id
    result["status"] = "accepted"
    return JSONResponse(result, status_code=202)

@app.get("/api/workflow/status", response_model=None)
def workflow_status(run_id: str) -> JSONResponse:
    """Poll the progress of an async workflow run.

    Query params:
        run_id: str  (required) — the run_id returned by POST /api/workflow/run

    Returns:
        {status: "running"|"done"|"failed"|"not_found",
         events: [...trace so far...],
         result?: dict, cost?: dict, error?: str}
    """
    entry = _workflow_runs.get(run_id)
    if entry is None:
        # Not in the live in-memory copy — fall back to the persisted journal so a
        # run that outlived a process restart is still observable (tail/wfpersist).
        from echelon_engine.agent.workflow import WorkflowRunJournal, _workflow_runs_dir
        loaded = WorkflowRunJournal.load_run(run_id, _workflow_runs_dir())
        if loaded is None:
            # Last resort: legacy run_registry JSON
            from .run_registry import load_runs
            persisted = load_runs().get(run_id)
            if persisted is None:
                return JSONResponse({"status": "not_found", "events": []})
            return JSONResponse({**persisted, "events": persisted.get("events", [])})
        resp: dict = {
            "status": loaded["status"],
            "events": loaded["events"],
            "started_at": loaded["started_at"],
        }
        if loaded["result"] is not None:
            resp["result"] = loaded["result"]
        if loaded.get("finished_at"):
            resp["finished_at"] = loaded["finished_at"]
        return JSONResponse(resp)
    resp: dict = {
        "status": entry["status"],
        "events": entry["events"],
        "started_at": entry["started_at"],
    }
    if entry["result"] is not None:
        resp["result"] = entry["result"]
    if entry["cost"] is not None:
        resp["cost"] = entry["cost"]
    if entry["error"] is not None:
        resp["error"] = entry["error"]
    return JSONResponse(resp)


@app.get("/api/workflow/runs", response_model=None)
def workflow_runs() -> JSONResponse:
    """List all persisted workflow runs (newest first). Survives restart — reads from the durable
    journal directory, not process memory. Each entry: {run_id, status, started_at, finished_at,
    steps_total, steps_done, ok, goal, folder, scheduler, file_size}."""
    from echelon_engine.agent.workflow import WorkflowRunJournal, _workflow_runs_dir
    runs = WorkflowRunJournal.list_runs(_workflow_runs_dir())
    # Enrich with live status for in-flight runs still in memory
    for r in runs:
        live = _workflow_runs.get(r["run_id"])
        if live and live.get("status") == "running":
            r["status"] = "running"
            r["steps_done"] = sum(
                1 for e in live.get("events", []) if e.get("k") == "step_done")
    return JSONResponse({"runs": runs})


# ── Main ────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys as _sys, os as _os
    import uvicorn as _uvicorn
    port = int(_os.environ.get("ECHELON_AGENT_PORT", "18888"))
    host = _os.environ.get("ECHELON_AGENT_HOST", "127.0.0.1")
    print(f"ECHELON agent server listening on http://{host}:{port}")
    _uvicorn.run(app, host=host, port=port, log_level="info")
