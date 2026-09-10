"""workflow_panel — surface the WORKFLOW WIZARD in the UI (command-center addition, 2026-06-19).

The backend already exists and is powerful: POST /api/workflow/run plans a DAG (T1 model), computes
Gantt waves, and runs N specialists in parallel; GET /api/workflow/status polls live progress. But it
had ZERO UI — a built-but-hidden capability the council (5/5) flagged as the biggest remaining ORIGINATE
gap. This module is its face: goal -> PLAN (dry-run, render the Gantt waves) -> EXECUTE -> watch status
stream, all in a center-board panel (the same act->watch surface the originate runs land in).

This adds NO new backend: it is a thin client over the two endpoints server.py already mounts. Same
self-contained shape as originate/vision: owns its slice (fragment + css + js); seam = register(app)
(serves its asset endpoints) + ONE launcher button injected into the roster head, + its fragment in the
shell. Delete in one line.

Identity: consumes the shell token contract (command.css :root) — blue = structure, --action (orange)
= the live/hot thing only (the PLAN/EXECUTE buttons, the running wave). One focal accent.
"""
from __future__ import annotations

from fastapi.responses import HTMLResponse


def register(app) -> None:
    """Mount the workflow-panel asset endpoints. ONE call is the whole seam (no new API — the
    workflow run/status routes already live in server.py)."""

    @app.get("/api/workflow-panel.css")
    def workflow_css() -> HTMLResponse:
        return HTMLResponse(STYLE_CSS, media_type="text/css")

    @app.get("/api/workflow-panel.js")
    def workflow_js() -> HTMLResponse:
        return HTMLResponse(SCRIPT_JS, media_type="application/javascript")


# ── the fragment: a launcher button (mounted into the roster head) + the panel template + assets ──
# The launcher lives in the roster head next to "+ New"; clicking it opens a workflow panel in the
# board. The panel itself is cloned from a <template> the script owns, so the board stays generic.
FRAGMENT_HTML = """
<link rel="stylesheet" href="/api/workflow-panel.css">
<template id="workflowPanelTpl">
  <section class="panel panel-workflow" data-workflow="">
    <div class="panel-head">
      <div class="panel-id">
        <span class="wf-glyph">◈</span>
        <span class="panel-name">workflow</span>
        <span class="panel-state wf-state">draft</span>
      </div>
      <div class="panel-meta">
        <span class="wf-stat"></span>
        <button class="panel-close" title="close panel">✕</button>
      </div>
    </div>
    <div class="wf-body">
      <label class="wf-fld">
        <span class="wf-l">Goal</span>
        <textarea class="wf-goal" rows="2" placeholder="one sentence — what should the society build?"></textarea>
      </label>
      <label class="wf-fld">
        <span class="wf-l">Folder <span class="wf-hint">(where the work lands — required)</span></span>
        <input class="wf-folder" type="text" placeholder="absolute path…" autocomplete="off" spellcheck="false">
      </label>
      <div class="wf-actions">
        <button class="wf-btn wf-plan">Plan</button>
        <button class="wf-btn wf-go wf-exec" disabled>▶ Execute</button>
        <span class="wf-msg"></span>
      </div>
      <div class="wf-gantt"></div>
      <pre class="wf-log" hidden></pre>
    </div>
  </section>
</template>
<script src="/api/workflow-panel.js"></script>
"""

# the launcher button — injected into the roster head by the shell (kept separate so the shell
# decides placement). The shell looks for this marker and drops the button in.
LAUNCHER_HTML = (
    '<button id="workflowBtn" class="ghost wf-launch" '
    'title="plan & run a parallel workflow (goal → DAG → Gantt → execute)">◈ Flow</button>'
)


SCRIPT_JS = r"""
(() => {
  let count = 0;
  // Resolve the shell anchors lazily (this script is injected ABOVE the roster/board in the
  // document, so they don't exist when it first runs — bind on DOM-ready / at click time).
  const tpl = () => document.getElementById("workflowPanelTpl");
  const boardEl = () => document.getElementById("board");
  const boardEmptyEl = () => document.getElementById("boardEmpty");

  const esc = (s) => String(s).replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));

  function openWorkflowPanel() {
    const board = boardEl(), boardEmpty = boardEmptyEl(), t = tpl();
    if (!board || !t) return;
    if (boardEmpty) boardEmpty.style.display = "none";
    const node = t.content.firstElementChild.cloneNode(true);
    const id = "wf" + (++count);
    node.dataset.workflow = id;
    board.appendChild(node);

    const $ = (s) => node.querySelector(s);
    const goal = $(".wf-goal"), folder = $(".wf-folder"),
          planBtn = $(".wf-plan"), execBtn = $(".wf-exec"),
          msg = $(".wf-msg"), gantt = $(".wf-gantt"), log = $(".wf-log"),
          stateEl = $(".wf-state"), statEl = $(".wf-stat");
    let plan = null, runId = null, pollTimer = null;

    $(".panel-close").onclick = () => {
      clearInterval(pollTimer);
      node.remove();
      if (!board.querySelector(".panel") && boardEmpty) boardEmpty.style.display = "";
    };
    // close X also clears boardEmpty correctly even if originate panels exist — board.querySelector covers all .panel

    const setState = (s, cls) => { stateEl.textContent = s; stateEl.className = "panel-state wf-state " + (cls||""); };

    // ── PLAN: dry-run the wizard, render the Gantt waves ──
    planBtn.onclick = async () => {
      const g = goal.value.trim(), f = folder.value.trim();
      if (!g || !f) { msg.textContent = "goal AND folder required"; msg.className = "wf-msg err"; return; }
      msg.textContent = "planning…"; msg.className = "wf-msg"; planBtn.disabled = true; setState("planning", "");
      gantt.innerHTML = ""; execBtn.disabled = true; plan = null;
      try {
        const r = await fetch("/api/workflow/run", {
          method: "POST", headers: {"Content-Type":"application/json"},
          body: JSON.stringify({ goal: g, folder: f, dry_run: true }),
        });
        const d = await r.json();
        if (d.validation && d.validation !== "OK") {
          msg.textContent = "invalid plan: " + JSON.stringify(d.validation); msg.className = "wf-msg err";
          setState("invalid", "s-stopped"); return;
        }
        plan = d.plan || null;
        renderGantt(d.waves || [], d.plan || {});
        const nSteps = (d.plan && d.plan.steps ? d.plan.steps.length : 0), nWaves = (d.waves||[]).length;
        statEl.textContent = nSteps + " steps · " + nWaves + " waves";
        msg.textContent = "planned via " + esc(d.plan_source || "?") + " — review, then Execute";
        msg.className = "wf-msg ok";
        setState("planned", "s-done"); execBtn.disabled = false;
      } catch (e) {
        msg.textContent = "plan failed: " + (e.message||e); msg.className = "wf-msg err"; setState("error", "s-stopped");
      } finally { planBtn.disabled = false; }
    };

    // ── render the Gantt: one row per wave, step chips inside, bar width ∝ wave size ──
    function renderGantt(waves, planObj) {
      if (!waves.length) { gantt.innerHTML = '<div class="wf-empty">no waves produced</div>'; return; }
      const maxW = Math.max(...waves.map(w => w.length), 1);
      gantt.innerHTML = waves.map((w, i) => {
        const chips = w.map(s => {
          const id = typeof s === "string" ? s : (s.id || s.agent || "?");
          return `<span class="wf-chip">${esc(id)}</span>`;
        }).join("");
        const pct = Math.round((w.length / maxW) * 100);
        return `<div class="wf-wave">
          <span class="wf-wave-n">wave ${i}</span>
          <div class="wf-wave-bar" style="width:${pct}%"></div>
          <div class="wf-wave-chips">${chips}</div>
        </div>`;
      }).join("");
    }

    // ── EXECUTE: launch the real run, poll status into the log ──
    execBtn.onclick = async () => {
      const g = goal.value.trim(), f = folder.value.trim();
      if (!g || !f) return;
      execBtn.disabled = true; planBtn.disabled = true;
      msg.textContent = "launching…"; msg.className = "wf-msg"; setState("running", "s-running");
      log.hidden = false; log.textContent = "";
      try {
        const body = { goal: g, folder: f };
        if (plan) body.plan = plan;                       // reuse the previewed plan
        const r = await fetch("/api/workflow/run", {
          method: "POST", headers: {"Content-Type":"application/json"}, body: JSON.stringify(body),
        });
        const d = await r.json();
        runId = d.run_id || null;
        if (!runId) {
          msg.textContent = "launch failed: " + JSON.stringify(d.validation || d.error || d);
          msg.className = "wf-msg err"; setState("error", "s-stopped"); planBtn.disabled = false; return;
        }
        msg.textContent = "run " + runId.slice(0, 8) + " — streaming…"; msg.className = "wf-msg";
        pollTimer = setInterval(pollStatus, 1500); pollStatus();
      } catch (e) {
        msg.textContent = "error: " + (e.message||e); msg.className = "wf-msg err"; setState("error", "s-stopped");
        planBtn.disabled = false;
      }
    };

    let shownEvents = 0;
    async function pollStatus() {
      if (!runId) return;
      try {
        const d = await (await fetch("/api/workflow/status?run_id=" + encodeURIComponent(runId))).json();
        const evs = d.events || [];
        if (evs.length > shownEvents) {
          for (let i = shownEvents; i < evs.length; i++) {
            const e = evs[i];
            log.textContent += "[" + (e.k || "·") + "] " + JSON.stringify(
              Object.fromEntries(Object.entries(e).filter(([k]) => k !== "k"))).slice(0, 200) + "\n";
          }
          shownEvents = evs.length; log.scrollTop = log.scrollHeight;
        }
        if (d.status === "done" || d.status === "failed") {
          clearInterval(pollTimer);
          setState(d.status, d.status === "done" ? "s-done" : "s-stopped");
          const cost = d.cost ? " · $" + (d.cost.spent_usd ?? 0) : "";
          msg.textContent = d.status + cost; msg.className = "wf-msg " + (d.status === "done" ? "ok" : "err");
          planBtn.disabled = false;
        }
      } catch (e) { /* transient poll error — keep last */ }
    }

    goal.focus();
  }

  // mount the launcher (the shell injects #workflowBtn into the roster head, which is parsed
  // AFTER this script — so wire on DOM-ready, not inline).
  function bindLauncher() {
    const btn = document.getElementById("workflowBtn");
    if (btn) btn.onclick = openWorkflowPanel;
  }
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", bindLauncher);
  } else {
    bindLauncher();
  }
})();
"""


STYLE_CSS = r"""
/* Workflow panel — consumes the shell token contract. blue = structure, --action = the live action. */
.wf-launch{ width:auto !important; padding:0 9px; font:600 11px var(--sans); letter-spacing:.04em;
  color:var(--text-2); }
.wf-launch:hover{ color:var(--action); border-color:var(--action); }

.panel-workflow .wf-glyph{ color:var(--action); font-size:14px; }
.panel-workflow .panel-name{ text-transform:none; }
.wf-state{ /* inherits .panel-state pill */ }

.wf-body{ flex:1 1 auto; overflow-y:auto; padding:14px; display:flex; flex-direction:column; gap:12px; }
.wf-fld{ display:flex; flex-direction:column; gap:5px; }
.wf-l{ font-size:10px; text-transform:uppercase; letter-spacing:.08em; color:var(--text-3); }
.wf-hint{ text-transform:none; letter-spacing:0; color:var(--text-3); opacity:.8; }
.wf-goal, .wf-folder{
  background:var(--bg-1); border:1px solid var(--line-2); border-radius:7px; padding:9px 11px;
  color:var(--text); font:12px var(--mono); outline:none; resize:vertical; transition:border-color .15s, box-shadow .15s;
}
.wf-goal:focus, .wf-folder:focus{ border-color:var(--action); box-shadow:0 0 0 3px rgba(255,122,61,.13); }

.wf-actions{ display:flex; align-items:center; gap:9px; }
.wf-btn{
  background:var(--bg-2); color:var(--text); border:1px solid var(--line-2); border-radius:7px;
  padding:8px 16px; font:700 11px var(--sans); letter-spacing:.06em; text-transform:uppercase;
  cursor:pointer; transition:border-color .15s, background .15s, box-shadow .15s;
}
.wf-btn:hover:not(:disabled){ border-color:var(--accent-dim); }
.wf-btn.wf-go{ background:var(--action); color:var(--action-ink); border-color:transparent; }
.wf-btn.wf-go:hover:not(:disabled){ background:var(--action-hi); box-shadow:0 2px 14px rgba(255,122,61,.28); }
.wf-btn:disabled{ opacity:.4; cursor:not-allowed; }
.wf-msg{ font-size:11px; color:var(--text-3); margin-left:auto; }
.wf-msg.ok{ color:var(--green); }
.wf-msg.err{ color:var(--red); }
.wf-stat{ color:var(--text-3); font-size:11px; font-variant-numeric:tabular-nums; }

/* the Gantt: stacked wave rows, each a labelled bar + step chips */
.wf-gantt{ display:flex; flex-direction:column; gap:7px; }
.wf-empty{ color:var(--text-3); font-size:12px; padding:8px 0; }
.wf-wave{ display:grid; grid-template-columns:54px 1fr; grid-template-rows:auto auto; column-gap:10px; align-items:center; }
.wf-wave-n{ grid-row:span 2; font:10px var(--mono); color:var(--text-3); text-transform:uppercase; letter-spacing:.05em; }
.wf-wave-bar{ height:6px; border-radius:3px; background:linear-gradient(90deg, var(--accent), var(--accent-dim));
  min-width:14px; transition:width .3s ease; }
.wf-wave-chips{ display:flex; flex-wrap:wrap; gap:5px; margin-top:5px; }
.wf-chip{ font:10px var(--mono); color:var(--text-2); background:var(--bg-2);
  border:1px solid var(--line); border-radius:5px; padding:2px 7px; }

.wf-log{
  margin:0; padding:10px 12px; max-height:220px; overflow-y:auto; background:var(--bg);
  border:1px solid var(--line); border-radius:7px; color:var(--text-2);
  font:11px/1.5 var(--mono); white-space:pre-wrap; word-break:break-word;
}
.wf-log::-webkit-scrollbar{ width:8px; }
.wf-log::-webkit-scrollbar-thumb{ background:var(--line-2); border-radius:4px; }
"""
