"""originate — the COMMAND BAR feature, as a self-contained module (a new shape, 2026-06-19).

The council (5/5) said the web app is a console, not a command center, until the owner can ORIGINATE
work from inside the UI — type a sentence, run a substrate verb or dispatch a partner, without
dropping to a CLI that is "underdeveloped for me to use". This is that bar. The owner's UI-side
mirror of finishing the CLI front door.

NEW SHAPE (owner: "no need to follow old structure, we explore something new"): unlike status_card /
society_view (data-only modules the server wires by hand), this feature OWNS ITS WHOLE SLICE — its
run-state, its background runner, its routes, AND its HTML fragment + CSS (the Flux triplet idea: a
feature carries its own server + render + style). The only seam to the app is ONE call:
`originate.register(app)`, and ONE include of FRAGMENT_HTML in index.html. If the experiment proves
out, it's the template for every future panel; if not, it deletes in one line.

ROUTING (the bar's one rule): first token is a known substrate verb -> run `python -X utf8 -m
echelon_engine <verb> ...` (subprocess, allow-listed, cwd=repo); else -> the whole sentence is a
partner DISPATCH goal (scope+folder from the bar, defaults echelon / repo-root). Slow work is
BACKGROUNDED behind a run_id the UI polls (the app's existing poll model — no SSE, per the council).
"""
from __future__ import annotations

import json
import shlex
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from fastapi import Request
from fastapi.responses import HTMLResponse, JSONResponse

from .run_registry import run_tree

_REPO_ROOT = Path(__file__).resolve().parents[3]   # .../ECHELON-AGENT

# The substrate CLI verbs the bar may run. An ALLOWLIST — anything else is treated as a partner
# goal, never shelled. (No arbitrary command execution from the box.)
_VERBS = {"recall", "relive", "wrap", "scan", "heal", "dispute",
          "disclaim", "redeem", "inspect", "dream", "graph"}

_TIMEOUT_S = 300
_MAX_RUNS = 60                       # cap the in-memory store (oldest evicted)

# ── run-state store (thread-safe; the app's own pattern: in-memory dict + lock) ────────────────
_RUNS: dict[str, dict[str, Any]] = {}
_LOCK = threading.Lock()


def _new_run(text: str, parent: str | None = None) -> str:
    rid = uuid.uuid4().hex[:12]
    with _LOCK:
        if len(_RUNS) >= _MAX_RUNS:                       # evict oldest by start ts
            oldest = min(_RUNS, key=lambda k: _RUNS[k]["ts"])
            _RUNS.pop(oldest, None)
        # `parent` is the run-lineage edge (owner, 2026-06-19): the FRONT DOOR is a ROOT (parent=None);
        # anything an origination spawns passes the origin rid so the board can render the run-TREE, not a
        # flat list. See runs-need-a-parent-chain-from-the-front-door.
        _RUNS[rid] = {"status": "running", "output": "", "done": False,
                      "kind": "", "cmd": text, "ts": time.time(), "parent": parent}
    return rid


def _append(rid: str, chunk: str) -> None:
    if not chunk:
        return
    with _LOCK:
        r = _RUNS.get(rid)
        if r is not None:
            r["output"] += chunk


def _finish(rid: str, status: str) -> None:
    with _LOCK:
        r = _RUNS.get(rid)
        if r is not None:
            r["status"], r["done"] = status, True


def _snapshot(rid: str) -> dict | None:
    with _LOCK:
        r = _RUNS.get(rid)
        return dict(r) if r is not None else None


# ── the background runner — verb subprocess OR partner dispatch ────────────────────────────────
def _run_verb(rid: str, verb: str, args: list[str]) -> None:
    """Run a substrate CLI verb, streaming stdout+stderr into the run state incrementally."""
    with _LOCK:
        _RUNS[rid]["kind"] = "verb"
    cmd = [sys.executable, "-X", "utf8", "-m", "echelon_engine", verb, *args]
    _append(rid, f"$ echelon {verb} {' '.join(args)}\n\n")
    try:
        proc = subprocess.Popen(cmd, cwd=str(_REPO_ROOT), stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                                errors="replace", bufsize=1)
        start = time.time()
        for line in proc.stdout:                          # incremental: line-by-line into the pane
            _append(rid, line)
            if time.time() - start > _TIMEOUT_S:
                proc.kill()
                _append(rid, f"\n[timeout after {_TIMEOUT_S}s]\n")
                _finish(rid, "error")
                return
        proc.wait()
        _finish(rid, "done" if proc.returncode == 0 else "error")
    except Exception as exc:                              # never let the thread die silently
        _append(rid, f"\n[error: {exc}]\n")
        _finish(rid, "error")


def _run_partner(rid: str, goal: str, scope: str, folder: str) -> None:
    """Dispatch one equipped partner; its on_event ticks stream live into the run state."""
    with _LOCK:
        _RUNS[rid]["kind"] = "partner"
    _append(rid, f"⊳ dispatching partner — scope={scope} folder={folder}\n  goal: {goal}\n\n")
    try:
        from echelon_engine.agent.partner import dispatch

        def on_event(kind: str, payload: dict) -> None:
            msg = payload.get("task") or payload.get("status") or payload.get("tool") or ""
            _append(rid, f"  [{kind}] {str(msg)[:160]}\n")

        res = dispatch(goal, scope=scope, folder=folder, max_steps=60, on_event=on_event)
        _append(rid, "\n" + json.dumps(
            {k: v for k, v in res.items() if k != "trace"}, indent=2, default=str) + "\n")
        _finish(rid, "done" if (res.get("outcome") or {}).get("ok") else "error")
    except Exception as exc:
        _append(rid, f"\n[error: {exc}]\n")
        _finish(rid, "error")


def _run_swarm(rid: str, goals_text: str, scope: str, folder: str) -> None:
    """Launch N real partners on the shared ledger -> a society bus.db the /society view renders live."""
    with _LOCK:
        _RUNS[rid]["kind"] = "swarm"
    goals = [g.strip() for g in goals_text.split(";") if g.strip()] or [goals_text]
    _append(rid, f"⊰ launching swarm — {len(goals)} goal(s), scope={scope}\n")
    try:
        from echelon_engine.agent.web.swarm_launch import launch
        name = launch(goals, scope=scope, folder=folder, parent=rid)  # chain the swarm under this origin
        _append(rid, f"\n  run: {name}\n  watch the lanes live -> /society?run={name}\n")
        _finish(rid, "done")
    except Exception as exc:
        _append(rid, f"\n[error: {exc}]\n")
        _finish(rid, "error")


def _launch(text: str, scope: str, folder: str, parent: str | None = None) -> str:
    """Parse the routing rule, start the work in a daemon thread, return the run_id immediately.
    `parent` chains this run under an origination (None = a root front-door run)."""
    rid = _new_run(text, parent=parent)
    tokens = shlex.split(text)
    if tokens and tokens[0] == "swarm":
        # `swarm <goal>; <goal>; ...` -> real partners on the shared ledger, watch on /society
        t = threading.Thread(target=_run_swarm, args=(rid, text[5:].strip(), scope, folder), daemon=True)
    elif tokens and tokens[0] in _VERBS:
        t = threading.Thread(target=_run_verb, args=(rid, tokens[0], tokens[1:]), daemon=True)
    else:
        t = threading.Thread(target=_run_partner, args=(rid, text, scope, folder), daemon=True)
    t.start()
    return rid


# ── the routes (the feature mounts itself) ─────────────────────────────────────────────────────
def register(app) -> None:
    """Mount the originate-bar routes + its asset endpoints on the app. ONE call is the whole seam."""

    @app.post("/api/originate")
    async def originate(req: Request) -> JSONResponse:
        body = await req.json()
        text = str(body.get("text", "")).strip()
        if not text:
            return JSONResponse({"error": "say something"}, status_code=400)
        scope = (body.get("scope") or "echelon").strip()
        folder = (body.get("folder") or str(_REPO_ROOT)).strip()
        parent = (body.get("parent") or None)  # chain a sub-run under its origin; None = a root run
        return JSONResponse({"run_id": _launch(text, scope, folder, parent=parent)})

    @app.get("/api/originate/history")
    def originate_history() -> JSONResponse:
        """Surface the in-memory run store as a list (newest first) so the roster can show
        originate runs as synthetic entries and the bar can offer ↑-recall + re-run. This is the
        store that already exists (_RUNS) — no new infra, just a window onto it. The full output
        is NOT included here (it can be large); fetch per-run via /api/originate/{rid}."""
        with _LOCK:
            runs = [
                {"rid": rid, "cmd": r["cmd"], "kind": r["kind"], "status": r["status"],
                 "done": r["done"], "ts": r["ts"], "parent": r.get("parent")}
                for rid, r in _RUNS.items()
            ]
        runs.sort(key=lambda r: r["ts"], reverse=True)
        return JSONResponse({"runs": runs})

    @app.get("/api/originate/tree")
    def originate_tree() -> JSONResponse:
        """The run-TREE (owner, 2026-06-19): fold the flat run store into a forest by the `parent`
        edge so the board can render the RUNNING ECHELON as it actually is — an origination (root) with
        its spawned sub-runs as children — not a flat sibling list. The fix for 'the UI cannot reflect
        the running echelon'. See runs-need-a-parent-chain-from-the-front-door."""
        with _LOCK:
            flat = {rid: {"cmd": r["cmd"], "kind": r["kind"], "status": r["status"],
                          "done": r["done"], "started_at": r["ts"], "parent": r.get("parent")}
                    for rid, r in _RUNS.items()}
        return JSONResponse({"tree": run_tree(flat)})

    @app.get("/api/originate/{rid}")
    def originate_status(rid: str) -> JSONResponse:
        snap = _snapshot(rid)
        if snap is None:
            return JSONResponse({"error": "no such run"}, status_code=404)
        return JSONResponse({"status": snap["status"], "output": snap["output"],
                             "done": snap["done"], "kind": snap["kind"],
                             "cmd": snap["cmd"], "ts": snap["ts"]})

    @app.get("/api/originate-bar.css")
    def originate_css() -> HTMLResponse:
        return HTMLResponse(STYLE_CSS, media_type="text/css")

    @app.get("/api/originate-bar.js")
    def originate_js() -> HTMLResponse:
        return HTMLResponse(SCRIPT_JS, media_type="application/javascript")


# ── the FRAGMENT (include once in index.html), + the asset constants the feature serves ─────────
FRAGMENT_HTML = """
<link rel="stylesheet" href="/api/originate-bar.css">
<section id="originate" class="originate" aria-label="Command bar">
  <form id="originate-form" class="originate-form">
    <span class="originate-prompt">⊳</span>
    <input id="originate-input" class="originate-input" type="text" autocomplete="off"
           list="originate-verbs" spellcheck="false"
           placeholder="run a verb (scan, recall …, graph) — or describe what a partner should do" />
    <datalist id="originate-verbs"></datalist>
    <button type="button" id="originate-ctx" class="originate-ctx" title="dispatch context">echelon</button>
    <button type="submit" id="originate-run" class="originate-run">Run</button>
  </form>
  <div id="originate-ctxpanel" class="originate-ctxpanel" hidden>
    <label>scope<input id="originate-scope" value="echelon"></label>
    <label>folder<input id="originate-folder" placeholder="(repo root)"></label>
  </div>
  <div id="originate-recent" class="originate-recent" hidden></div>
  <pre id="originate-pane" class="originate-pane" hidden></pre>
</section>
<script src="/api/originate-bar.js"></script>
"""

SCRIPT_JS = r"""
(() => {
  const $ = (id) => document.getElementById(id);
  const form=$("originate-form"), input=$("originate-input"), run=$("originate-run"),
        pane=$("originate-pane"), ctx=$("originate-ctx"), panel=$("originate-ctxpanel"),
        scope=$("originate-scope"), folder=$("originate-folder"),
        verbs=$("originate-verbs"), recent=$("originate-recent");
  let busy=false, timer=null, shown=0;

  // ── verb palette: the allowlisted substrate verbs, made visible (killed blank-input paralysis) ──
  const VERBS = [
    ["recall","surface warm atoms for a reasoning"], ["relive","re-walk a session's chain of moves"],
    ["wrap","close out a session into memory"], ["scan","run the graph immune scan"],
    ["heal","repair flagged edges"], ["dispute","challenge an atom's verdict"],
    ["disclaim","mark an atom counterfeit"], ["redeem","restore a falsely-disclaimed atom"],
    ["inspect","examine an atom + its history"], ["dream","run the dream-test pass"],
    ["graph","render the atlas/chainboard"], ["swarm","launch N partners -> /society"],
  ];
  verbs.innerHTML = VERBS.map(([v,h])=>`<option value="${v} ">${v} — ${h}</option>`).join("");

  ctx.onclick=()=>{ panel.hidden=!panel.hidden; };
  const setBusy=(b)=>{ busy=b; input.disabled=b; run.disabled=b;
    run.textContent=b?"…":"Run"; run.classList.toggle("is-busy",b); };
  const line=(t)=>{ const s=document.createElement("span"); s.className="originate-line";
    s.textContent=t; pane.appendChild(s); pane.scrollTop=pane.scrollHeight; };

  // ── ↑/↓ command recall — client-side ring over what this tab has launched ──
  let hist=[], hpos=-1, draft="";
  input.addEventListener("keydown",(e)=>{
    if(e.key==="ArrowUp"){
      if(!hist.length) return;
      if(hpos===-1){ draft=input.value; hpos=0; } else if(hpos<hist.length-1){ hpos++; }
      input.value=hist[hpos]; e.preventDefault();
    } else if(e.key==="ArrowDown"){
      if(hpos===-1) return;
      if(hpos>0){ hpos--; input.value=hist[hpos]; } else { hpos=-1; input.value=draft; }
      e.preventDefault();
    }
  });

  // ── recent-runs strip: re-run pills sourced from the server's run store ──
  async function refreshRecent(){
    let runs=[];
    try{ runs=(await (await fetch("/api/originate/history")).json()).runs||[]; }catch{ return; }
    hist = runs.map(r=>r.cmd);                          // newest-first feeds ↑-recall too
    if(!runs.length){ recent.hidden=true; recent.innerHTML=""; return; }
    recent.hidden=false;
    recent.innerHTML = runs.slice(0,8).map(r=>{
      const cls = r.status==="error" ? "err" : r.done ? "done" : "live";
      return `<button class="originate-pill ${cls}" data-rid="${r.rid}" data-cmd="${
        r.cmd.replace(/"/g,"&quot;")}" title="re-run">${escapeHtml(clip(r.cmd,42))}</button>`;
    }).join("");
  }
  const clip=(s,n)=> s.length>n ? s.slice(0,n)+"…" : s;
  const escapeHtml=(s)=>String(s).replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
  recent.addEventListener("click",(e)=>{
    const b=e.target.closest(".originate-pill"); if(!b) return;
    input.value=b.dataset.cmd; input.focus();           // re-run = recall into the box, one Enter to fire
  });

  async function poll(rid){
    try{
      const r=await fetch("/api/originate/"+rid); if(!r.ok) throw new Error("HTTP "+r.status);
      const d=await r.json(); const out=String(d.output||"");
      if(out.length>shown){ line(out.slice(shown)); shown=out.length; }
      // mirror the live output into the board panel watching this run (act -> watch, same gaze)
      document.dispatchEvent(new CustomEvent("echelon:originate-tick",
        {detail:{rid, output:out, status:d.status, done:d.done, kind:d.kind, cmd:d.cmd}}));
      if(d.done){ pane.dataset.status=d.status; setBusy(false); refreshRecent(); return; }
      timer=setTimeout(()=>poll(rid),1000);
    }catch(e){ line("\n[ui error: "+(e.message||e)+"]\n"); setBusy(false); }
  }
  async function submit(ev){
    ev.preventDefault(); if(busy) return;
    const text=input.value.trim(); if(!text) return;
    hpos=-1;
    pane.hidden=false; pane.textContent=""; shown=0; delete pane.dataset.status; setBusy(true);
    try{
      const r=await fetch("/api/originate",{method:"POST",headers:{"Content-Type":"application/json"},
        body:JSON.stringify({text, scope:scope.value, folder:folder.value})});
      const d=await r.json(); if(!r.ok||!d.run_id) throw new Error(d.error||"launch failed");
      // tell the board a run was originated -> it opens a panel that streams this run (the landing place)
      document.dispatchEvent(new CustomEvent("echelon:originate-run",{detail:{rid:d.run_id, cmd:text}}));
      refreshRecent();
      poll(d.run_id);
    }catch(e){ line("[error: "+(e.message||e)+"]\n"); setBusy(false); }
  }
  form.addEventListener("submit",submit);
  refreshRecent();
})();
"""

STYLE_CSS = r"""
/* The originate spine consumes the SHELL's token contract (command.css :root) — no private
   palette. Identity rule (2026-06-19): blue = structure, --action (orange) = the live/hot thing
   ONLY (the Run button, the focused input ring, the live pill). One focal accent. */
.originate{
  position:sticky; top:0; z-index:50; background:var(--bg);
  border-bottom:1px solid var(--line); font-family:var(--sans);
}
.originate-form{ display:flex; align-items:center; gap:var(--space-2); padding:10px 16px; }
.originate-prompt{ color:var(--action); font-weight:700; user-select:none; font-size:15px; }
.originate-input{
  flex:1; background:var(--bg-1); border:1px solid var(--line-2); border-radius:7px;
  padding:10px 13px; color:var(--text); font:13px/1.4 var(--mono); outline:none;
  transition:border-color .15s, box-shadow .15s;
}
.originate-input:focus{ border-color:var(--action); box-shadow:0 0 0 3px rgba(255,122,61,.14); }
.originate-input::placeholder{ color:var(--text-3); }
.originate-ctx{
  background:var(--bg-2); color:var(--text-2); border:1px solid var(--line-2); border-radius:7px;
  padding:9px 13px; font:11px var(--mono); cursor:pointer; transition:border-color .15s,color .15s;
}
.originate-ctx:hover{ border-color:var(--accent-dim); color:var(--text); }
.originate-run{
  background:var(--action); color:var(--action-ink); border:0; border-radius:7px; padding:10px 22px;
  font:700 12px var(--sans); letter-spacing:.09em; text-transform:uppercase; cursor:pointer;
  transition:background .15s, opacity .15s, box-shadow .15s;
}
.originate-run:hover:not(:disabled){ background:var(--action-hi); box-shadow:0 2px 14px rgba(255,122,61,.28); }
.originate-run:disabled{ opacity:.45; cursor:wait; }
.originate-ctxpanel{ display:flex; gap:var(--space-4); padding:0 16px 12px 38px; }
.originate-ctxpanel label{ display:flex; flex-direction:column; gap:4px; color:var(--text-3);
  font-size:10px; text-transform:uppercase; letter-spacing:.07em; }
.originate-ctxpanel input{ background:var(--bg-1); border:1px solid var(--line-2);
  border-radius:6px; padding:7px 10px; color:var(--text); font:12px var(--mono); outline:none; }
.originate-ctxpanel input:focus{ border-color:var(--action); }
.originate-pane{
  margin:0; padding:12px 16px; max-height:380px; overflow-y:auto; background:var(--bg);
  border-top:1px solid var(--line); color:var(--text-2); font:12px/1.55 var(--mono);
  white-space:pre-wrap; word-break:break-word;
}
.originate-pane[data-status=error]{ border-left:2px solid var(--red); }
.originate-pane[data-status=done]{ border-left:2px solid var(--accent); }
.originate-line{ display:inline; animation:o-fade .2s ease-out; }
@keyframes o-fade{ from{opacity:0} to{opacity:1} }
.originate-pane::-webkit-scrollbar{ width:8px; }
.originate-pane::-webkit-scrollbar-thumb{ background:var(--line-2); border-radius:4px; }
.originate-recent{ display:flex; flex-wrap:wrap; gap:6px; padding:0 16px 10px 38px; }
.originate-pill{
  max-width:280px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap;
  background:var(--bg-2); color:var(--text-2); border:1px solid var(--line-2); border-radius:6px;
  padding:5px 10px; font:11px var(--mono); cursor:pointer; transition:border-color .15s,color .15s;
}
.originate-pill:hover{ border-color:var(--accent-dim); color:var(--text); }
.originate-pill::before{ content:"⟲ "; color:var(--text-3); }
.originate-pill.done{ border-left:2px solid var(--accent); }
.originate-pill.err{ border-left:2px solid var(--red); }
.originate-pill.live{ border-left:2px solid var(--action); }
"""
