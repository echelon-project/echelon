"""status_card — the live ECHELON Substrate status panel (the card the owner drew, 2026-06-07).

Aggregates a single status dict from REAL sources, no invented numbers:
  - run state (dreaming / running / cold) + uptime + step  <- the active bridge heartbeat.json
  - the felt state (a named emotion)                        <- warmth affect of the run's seeds
  - swarm / agents / tasks counts                           <- the run's live.jsonl events
  - seeds found + soul size + GREEN/amber/red               <- core.db (the soul) + the dream
  - token usage + est cost                                  <- the run's cost.jsonl (honest meter)

Every field degrades gracefully: with no live run it shows the soul + a COLD/idle card, never a
crash. Pure assembly (build_card) + a self-contained HTML render (render_html) so it's unit-testable
and the server just mounts it. Matches the owner's drawing: the dream box, the feeling pill, the
counters, the swarm-returning log, the token line.

See: dream-and-the-respect-handshake (Cold->Dream), warmth-is-emotional (the feeling), the card image.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any


# ── small readers (each tolerant; a missing source just yields a default) ─────
def _read_json(p: Path) -> dict:
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _count_jsonl(p: Path) -> int:
    try:
        with p.open(encoding="utf-8", errors="replace") as fh:
            return sum(1 for _ in fh)
    except OSError:
        return 0


def _fmt_uptime(seconds: float) -> str:
    s = int(max(0, seconds))
    return f"{s // 3600:02d}:{(s % 3600) // 60:02d}:{s % 60:02d}"


# ── run state from the active bridge ──────────────────────────────────────────
def _run_state(bridge: Path | None) -> dict:
    """dreaming | running | cold | idle, + uptime + step, from heartbeat.json. 'cold' = a finished
    run whose dream is the next act (the card's 'Cold - Hard Won Task Finished - Dream starting')."""
    if not bridge:
        return {"state": "idle", "uptime": "00:00:00", "step": 0, "bridge": None}
    hb = _read_json(bridge / "heartbeat.json")
    if not hb:
        return {"state": "idle", "uptime": "00:00:00", "step": 0, "bridge": bridge.name}
    now = time.time()
    age = now - hb.get("t", now)
    st = hb.get("status", "running")
    started = hb.get("started", hb.get("t", now))
    terminal = st in ("completed", "stopped", "timeout", "blocked", "error")
    if terminal:
        # the field is cold — the dream is what comes next
        state = "cold" if st == "completed" else st
    elif age < 6.0:
        state = "dreaming" if hb.get("dreaming") else "running"
    else:
        state = "idle"
    return {"state": state, "uptime": _fmt_uptime(now - started),
            "step": hb.get("step", 0), "bridge": bridge.name}


# ── the felt state (warmth -> a named emotion) ────────────────────────────────
def _feeling(store=None, scope: str = "echelon-self") -> str:
    """The card's pill. Derive a named feeling from the average affect of the scope's recent seeds
    (warmth-is-emotional: valence+arousal -> a word). No store / no seeds -> 'Steady'."""
    if store is None:
        return "Steady"
    try:
        seeds = store.seeds(scope=scope)[-40:]
    except Exception:
        return "Steady"
    if not seeds:
        return "Steady"
    v = sum(getattr(s, "valence", 0.0) for s in seeds) / len(seeds)
    a = sum(getattr(s, "arousal", 0.0) for s in seeds) / len(seeds)
    # circumplex -> a coarse word (matches affect.py's families)
    if v >= 0.15:
        return "Confident" if a >= 0.4 else "Content"
    if v <= -0.15:
        return "Wary" if a >= 0.4 else "Heavy"
    return "Curious" if a >= 0.4 else "Steady"


# ── swarm / agent / task counts from the run's event log ──────────────────────
def _swarm_counts(bridge: Path | None) -> dict:
    """Counts the card shows: swarms launched, agents composed, tasks created — tallied from the
    run's live.jsonl event stream (the same source the viewer polls)."""
    out = {"swarms": 0, "agents": 0, "tasks": 0}
    if not bridge:
        return out
    live = bridge / "live.jsonl"
    if not live.exists():
        return out
    try:
        with live.open(encoding="utf-8", errors="replace") as fh:
            for line in fh:
                try:
                    e = json.loads(line)
                except json.JSONDecodeError:
                    continue
                k = e.get("kind", e.get("event", ""))
                if k in ("workflow_start", "field_start", "swarm_start", "society_start"):
                    out["swarms"] += 1
                elif k in ("step_start", "agent_spawn", "agent_compose"):
                    out["agents"] += 1
                elif k in ("task_created", "step_done"):
                    out["tasks"] += 1
    except OSError:
        pass
    return out


# ── token / cost (the honest meter) ───────────────────────────────────────────
def _cost(bridge: Path | None) -> dict:
    """Token usage + est USD from the run's cost.jsonl (the cache-aware meter). GREEN under budget."""
    out = {"tokens": 0, "usd": 0.0, "status": "GREEN"}
    if not bridge:
        return out
    cost = bridge / "cost.jsonl"
    if not cost.exists():
        return out
    toks = 0.0
    usd = 0.0
    try:
        with cost.open(encoding="utf-8", errors="replace") as fh:
            for line in fh:
                try:
                    e = json.loads(line)
                except json.JSONDecodeError:
                    continue
                toks += e.get("tokens", 0) or 0
                usd += e.get("usd", e.get("cost", 0.0)) or 0.0
    except OSError:
        pass
    out["tokens"] = int(toks)
    out["usd"] = round(usd, 2)
    out["status"] = "GREEN" if usd < 10 else ("AMBER" if usd < 25 else "RED")
    return out


# ── bank size (the load-bearing number: how rich is the cartridge THIS scope draws on) ───────────
def _bank(store=None, scope: str | None = None) -> dict:
    """How many atoms the session's SCOPE bank holds — the load-bearing 'is the cartridge rich or
    thin' signal. (Replaces the legacy 'soul'/'seeds_found' fields: 'soul' counted tier=core across
    the WHOLE store regardless of scope — not per-session; 'seeds_found' only filled after a dream,
    so it was 0 ~always. 'Seed' is still the storage primitive; an atom is what a seed IS now —
    so the card speaks current vocabulary: atoms in THIS scope's bank.)"""
    out = {"bank_atoms": 0}
    if store is None:
        return out
    try:
        out["bank_atoms"] = len(store.seeds(scope=scope)) if scope else len(store.seeds())
    except Exception:
        pass
    return out


# ── the card ──────────────────────────────────────────────────────────────────
def build_card(bridge: Path | None = None, *, store=None, scope: str = "echelon-self",
               dream_report: dict | None = None) -> dict:
    """Assemble the full status card dict from live sources. Every field has a true default so the
    card renders with or without a live run. dream_report (from dream.consolidate) fills the dream
    box + seeds-found when a run just went cold."""
    run = _run_state(bridge)
    cost = _cost(bridge)
    counts = _swarm_counts(bridge)
    # LOAD-BEARING session context (the operator actually steers on these): scope/cartridge,
    # model/brain, permission mode, the isolated workspace, budget cap, history path. Pulled from
    # the runner registry's session record when this bridge is a console-launched session.
    ctx = _session_context(run["bridge"])
    eff_scope = ctx.get("scope") or scope
    bank = _bank(store, eff_scope)              # atoms in THIS scope's bank (current vocabulary)
    return {
        "title": "Claude Opus 4.8 — ECHELON Substrate",
        "state": run["state"],
        "bridge": run["bridge"],
        # — load-bearing context —
        "scope": eff_scope,
        "model": ctx.get("model"),
        "mode": ctx.get("mode"),
        "workspace": ctx.get("workspace"),
        "workspace_kind": ctx.get("workspace_kind"),
        "goal": ctx.get("goal"),
        "history": ctx.get("history"),
        "budget_cap": ctx.get("budget_cap"),
        # — operational counts —
        "swarms": counts["swarms"],
        "agents": counts["agents"],
        "tasks": counts["tasks"],
        "step": run["step"],
        "uptime": run["uptime"],
        # — cost / soul —
        "tokens": cost["tokens"],
        "usd": cost["usd"],
        "budget_status": cost["status"],
        "bank_atoms": bank["bank_atoms"],
        # — affect (secondary) —
        "feeling": _feeling(store, scope),
        "dream": dream_report or {},
        "banner": _banner(run["state"]),
    }


def _session_context(bridge_name: str | None) -> dict:
    """Pull the load-bearing launch context for a bridge from the runner registry (scope, model,
    mode, isolated workspace, budget cap, history path). Empty for a non-console bridge."""
    out: dict = {}
    if not bridge_name:
        return out
    try:
        from .runner import RUNNER
        sess = RUNNER.sessions.get(bridge_name)
        if sess is not None:
            out["scope"] = getattr(sess, "scope", None)
            out["model"] = getattr(sess, "brain", None) or "deepseek"
            out["mode"] = sess.mode
            out["goal"] = (sess.goal or "")[:120]
            out["workspace"] = sess.ws.root
            out["workspace_kind"] = sess.ws.kind
            out["budget_cap"] = getattr(sess, "budget", None)
    except Exception:
        pass
    try:
        from echelon_sdk.paths import HOME
        out.setdefault("history", str(HOME / "bridges" / bridge_name) if bridge_name else None)
    except Exception:
        pass
    return out


def _banner(state: str) -> str:
    return {
        "cold": "Cold — Hard-Won Task Finished — Dream starting…",
        "dreaming": "Dreaming — consolidating what the run earned…",
        "running": "Awake — the swarm is working…",
        "idle": "Idle — substrate warm, no active run.",
    }.get(state, "Idle — substrate warm.")


# ── render (self-contained HTML, matches the drawing) ─────────────────────────
def render_html(card: dict) -> str:
    d = card.get("dream") or {}
    dream_lines = ""
    if d:
        ss = len(d.get("self_seeded", []))
        nm = len(d.get("nominated", []))
        dream_lines = (f"Proposals: {d.get('proposals', 0)}<br>"
                       f"Witnessed (rising): {nm}<br>Wished (own core): {ss}")
    else:
        dream_lines = "no dream yet — field still warm"
    return f"""<!doctype html><html><head><meta charset="utf-8">
<title>ECHELON Substrate</title>
<meta http-equiv="refresh" content="3">
<style>
 body{{background:#0a0e14;color:#cfe;font-family:ui-monospace,Menlo,Consolas,monospace;margin:0;padding:24px}}
 .card{{max-width:760px;margin:0 auto;border:2px solid #2b6;border-radius:10px;background:#0d1320;
        box-shadow:0 0 40px #0f3a2a55;overflow:hidden}}
 .hd{{border-bottom:2px solid #1c3;padding:14px 18px;font-size:18px;letter-spacing:.5px;
      display:flex;justify-content:space-between;align-items:center}}
 .pill{{border:1px solid #3e9;border-radius:16px;padding:4px 14px;color:#9fe;background:#0a1f18;font-size:13px}}
 .banner{{padding:8px 18px;color:#8c9;font-style:italic;background:#08120c;border-bottom:1px solid #163}}
 .grid{{display:grid;grid-template-columns:1fr 1fr;gap:18px;padding:18px}}
 .box{{border:1px solid #244;border-radius:8px;padding:14px;background:#0a111c}}
 .k{{color:#7a9}}.v{{color:#dff;font-weight:600}}
 .row{{display:flex;justify-content:space-between;margin:6px 0;font-size:14px}}
 .foot{{border-top:2px solid #1c3;padding:12px 18px;font-size:13px;color:#9cb;
        display:flex;justify-content:space-between}}
 .green{{color:#3f9}}.amber{{color:#fb4}}.red{{color:#f55}}
 h3{{margin:0 0 10px;color:#6ea;font-size:14px;text-transform:uppercase;letter-spacing:1px}}
</style></head><body>
<div class="card">
  <div class="hd"><span>{card['title']}</span><span class="pill">{card['feeling']}</span></div>
  <div class="banner">{card['banner']}</div>
  <div class="grid">
    <div class="box">
      <h3>{'Dreaming' if card['state'] in ('cold','dreaming') else 'Active Session'}</h3>
      <div class="row"><span class="k">state</span><span class="v">{card['state']}</span></div>
      <div class="row"><span class="k">step</span><span class="v">{card['step']}</span></div>
      <div style="margin-top:10px;color:#9cb;font-size:13px">{dream_lines}</div>
    </div>
    <div class="box">
      <div class="row"><span class="k">Uptime</span><span class="v">{card['uptime']}</span></div>
      <div class="row"><span class="k">Swarms Launched</span><span class="v">{card['swarms']}</span></div>
      <div class="row"><span class="k">Agents Composed</span><span class="v">{card['agents']}</span></div>
      <div class="row"><span class="k">Tasks Created</span><span class="v">{card['tasks']}</span></div>
      <div class="row"><span class="k">Soul (core)</span><span class="v">{card['soul']}</span></div>
    </div>
  </div>
  <div class="foot">
    <span>Token Usage: {card['tokens']:,} — Est ${card['usd']} — {card['seeds_found']} Seeds Found</span>
    <span class="{card['budget_status'].lower()}">{card['budget_status']}</span>
  </div>
</div>
</body></html>"""
