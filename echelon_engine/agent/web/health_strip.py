"""health_strip — the always-on substrate vitals (command-center addition #2).

The council (5/5) named this #2 after the originate bar: "five numbers, always on. Turns 'is the estate
OK and am I bleeding money' from a 4-click investigation into a glance." ECHELON is a MEMORY substrate;
hiding the bank's health makes the app feel like agent-UI, not system-command.

Same self-contained shape as originate.py (owner: explore the new shape): the feature owns its slice —
its data assembly, its route, its fragment + css. Seam = one register(app) + the fragment in index().

THE NUMBERS (all from REAL sources, no invented figures — the status_card honesty law):
  bank      — atoms in scope
  earned    — atoms that earned weight (use_count>0): proof-of-work, the orange in the atlas
  immune    — scan verdict: fail/warn (0/0 = clean); the structure antibody at a glance
  disclaimed— live disclaimed atoms (held-false): the bank's open debt
  edges     — typed edges (the soul-graph), separate from the refs mesh
Cost/budget is intentionally LEFT to the existing status card (one number, one owner — no duplication).
"""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path

from fastapi.responses import HTMLResponse, JSONResponse

_DB = str(Path.home() / ".echelon" / "echelon.db")
_JUDGED = "judged:"
_CACHE: dict = {"ts": 0.0, "data": None}
_TTL_S = 8.0          # the scan is cheap but not free; cache a few seconds so a poll storm can't thrash it


def vitals(scope: str = "echelon") -> dict:
    """Assemble the five honest numbers. Cached briefly. Degrades gracefully (never crashes the strip)."""
    now = time.time()
    if _CACHE["data"] is not None and now - _CACHE["ts"] < _TTL_S:
        return _CACHE["data"]
    out = {"scope": scope, "bank": 0, "earned": 0, "disclaimed": 0,
           "edges": 0, "fail": 0, "warn": 0, "ok": True}
    try:
        c = sqlite3.connect(f"file:{Path(_DB).as_posix()}?mode=ro", uri=True)
        c.row_factory = sqlite3.Row
        out["bank"] = c.execute("SELECT COUNT(*) FROM atoms WHERE scope=?", (scope,)).fetchone()[0]
        out["earned"] = c.execute(
            "SELECT COUNT(*) FROM atom_earned e JOIN atoms a ON a.id=e.atom_id "
            "WHERE a.scope=? AND e.use_count>0", (scope,)).fetchone()[0]
        out["disclaimed"] = c.execute(
            "SELECT COUNT(*) FROM atoms WHERE scope=? AND born_from LIKE ? "
            "AND born_from NOT LIKE 'redeemed:%'", (scope, f"%{_JUDGED}%")).fetchone()[0]
        out["edges"] = c.execute(
            "SELECT COUNT(*) FROM atom_links WHERE superseded_on=0 AND relation<>'refs'").fetchone()[0]
        c.close()
    except Exception:
        out["ok"] = False
    # the immune verdict — through the engine's own scanner (the structure antibody)
    try:
        from echelon_engine.atoms.cards import CardStore
        rep = CardStore().scan(scope=scope)
        out["fail"], out["warn"] = rep["stats"]["fail"], rep["stats"]["warn"]
    except Exception:
        out["ok"] = False
    _CACHE.update(ts=now, data=out)
    return out


def register(app) -> None:
    @app.get("/api/health")
    def health(scope: str = "echelon") -> JSONResponse:
        return JSONResponse(vitals(scope))

    @app.get("/api/health-strip.css")
    def health_css() -> HTMLResponse:
        return HTMLResponse(STYLE_CSS, media_type="text/css")

    @app.get("/api/health-strip.js")
    def health_js() -> HTMLResponse:
        return HTMLResponse(SCRIPT_JS, media_type="application/javascript")


FRAGMENT_HTML = """
<link rel="stylesheet" href="/api/health-strip.css">
<div id="health-strip" class="health-strip" aria-label="Substrate vitals">
  <span class="hs-cell"><b id="hs-bank">·</b><i>bank</i></span>
  <span class="hs-cell"><b id="hs-earned">·</b><i>earned</i></span>
  <span class="hs-cell"><b id="hs-edges">·</b><i>edges</i></span>
  <span class="hs-cell" id="hs-immune-cell"><b id="hs-immune">·</b><i>immune</i></span>
  <span class="hs-cell" id="hs-disc-cell"><b id="hs-disc">·</b><i>disclaimed</i></span>
  <span class="hs-scope" id="hs-scope">echelon</span>
</div>
<script src="/api/health-strip.js"></script>
"""

SCRIPT_JS = r"""
(() => {
  const $ = (id) => document.getElementById(id);
  async function refresh(){
    try{
      const d = await (await fetch("/api/health?scope=echelon")).json();
      $("hs-bank").textContent = d.bank;
      $("hs-earned").textContent = d.earned;
      $("hs-edges").textContent = d.edges;
      $("hs-scope").textContent = d.scope;
      const imm = (d.fail>0) ? ("FAIL "+d.fail) : (d.warn>0 ? ("warn "+d.warn) : "clean");
      $("hs-immune").textContent = imm;
      $("hs-immune-cell").dataset.level = d.fail>0 ? "fail" : (d.warn>0 ? "warn" : "ok");
      $("hs-disc").textContent = d.disclaimed;
      $("hs-disc-cell").dataset.level = d.disclaimed>0 ? "warn" : "ok";
    }catch(e){ /* leave dots; the strip never breaks the page */ }
  }
  refresh(); setInterval(refresh, 15000);   // a glance value — slow vitals, gentle poll
})();
"""

STYLE_CSS = r"""
.health-strip{
  --hs-bg:#0a0a0c; --hs-ink:#e6e6e8; --hs-faint:#6a6a72; --hs-border:#26262c;
  --hs-ok:#7af3b0; --hs-warn:#ffd23f; --hs-fail:#ff5a5a; --hs-accent:#ff7a3d;
  --hs-mono:ui-monospace,"Cascadia Code",Consolas,monospace;
  display:flex; align-items:center; gap:22px; padding:7px 16px;
  background:var(--hs-bg); border-bottom:1px solid var(--hs-border);
  font-family:var(--hs-mono); font-size:11px; color:var(--hs-faint);
}
.hs-cell{ display:flex; align-items:baseline; gap:6px; }
.hs-cell b{ font-size:14px; font-weight:700; color:var(--hs-ink); letter-spacing:.02em; }
.hs-cell i{ font-style:normal; text-transform:uppercase; letter-spacing:.07em; font-size:9px; }
#hs-earned{ color:var(--hs-accent); }
.hs-cell[data-level=ok] b{ color:var(--hs-ok); }
.hs-cell[data-level=warn] b{ color:var(--hs-warn); }
.hs-cell[data-level=fail] b{ color:var(--hs-fail); }
.hs-scope{ margin-left:auto; padding:2px 9px; border:1px solid var(--hs-border); border-radius:99px;
  color:var(--hs-faint); font-size:10px; letter-spacing:.05em; }
"""
