"""worldview.py — the steer-and-observe SURFACE for the self-running world loop.

Built AFTER and FROM the engine (worldjournal.py), against a REAL world.jsonl + snapshot on
disk — not a mockup. The proper dogfood: this renders the loop's actual emitted state, or it
shows nothing. Three observe surfaces + one steer verb, the three the owner asked for:

    tail   — follow world.jsonl live; print each round as it lands (the foreground watch,
             honoring 'watch the dispatch, don't fire-and-forget').
    card   — render world.snapshot.json as a glanceable markdown status card (lowest
             attention; open it whenever). Includes the temporal believed-true ledger.
    html   — emit a self-refreshing HTML status card (the live-console seed); snapshot-driven.
    nudge  — the STEER lever: append a God's-eye instruction to nudge.txt (BOM-free), which
             the running loop drains next round. Scriptable; steers WITHOUT stopping the loop.

All read the same workdir the loop writes. Run with `python -X utf8`.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any


def _load_snapshot(workdir: Path) -> dict[str, Any] | None:
    snap = workdir / "world.snapshot.json"
    if not snap.exists():
        return None
    try:
        return json.loads(snap.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


# ── observe: tail the live journal ─────────────────────────────────────────────────────────
def _fmt_event(rec: dict[str, Any]) -> str | None:
    """One line per meaningful event — the round narrative, MiroFish's log_round style."""
    ev = rec.get("event")
    if ev == "autoloop_propose":
        return f"round {rec.get('turn')}  goal: {rec.get('goal')}  ←{rec.get('from_atom')}"
    if ev == "autoloop_turn":
        mark = "✓" if rec.get("ok") else ("✗" if rec.get("ok") is False else "?")
        earned = rec.get("earned") or {}
        e = f"  earned {earned.get('card', '')} q={earned.get('q')}" if earned else ""
        return f"  {mark} {rec.get('status')}   spent ${rec.get('spent')}{e}"
    if ev == "note":
        return f"  · {rec.get('msg')}" + (f": {rec.get('nudge')}" if rec.get("nudge") else "")
    if ev == "autoloop_halt":
        return f"— HALT: {rec.get('reason')}"
    if ev == "autoloop_report":
        return f"=== report: {rec.get('completed_ok')}/{rec.get('n_turns')} ok, ${rec.get('total_spent')} ==="
    return None


def tail(workdir: Path, *, follow: bool = True, poll: float = 1.0) -> None:
    jpath = workdir / "world.jsonl"
    print(f"[worldview] tailing {jpath}", flush=True)
    pos = 0
    while True:
        if jpath.exists():
            with jpath.open("r", encoding="utf-8") as f:
                f.seek(pos)
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    out = _fmt_event(rec)
                    if out:
                        print(out, flush=True)
                pos = f.tell()
        if not follow:
            return
        # stop following once the loop has reported (halted)
        snap = _load_snapshot(workdir)
        if snap and snap.get("halted"):
            return
        time.sleep(poll)


# ── observe: the markdown status card ──────────────────────────────────────────────────────
def render_card(workdir: Path) -> str:
    snap = _load_snapshot(workdir)
    if not snap:
        return "no world yet — start the loop (echelon_engine.agent.world.worldjournal) to create a journal."
    bt = snap.get("believed_true", [])
    standing = [e for e in bt if not e.get("invalid_at")]
    disputed = [e for e in bt if e.get("invalid_at")]
    lines = [
        "# ECHELON WORLD — status",
        "",
        f"- **round**: {snap.get('round')}   **last outcome**: {snap.get('last_outcome')}",
        f"- **goal**: {snap.get('goal')}",
        f"- **from atom**: `{snap.get('from_atom')}`",
        f"- **spent**: ${snap.get('spent')}   **atoms earned**: {snap.get('earned_total')}",
        f"- **halted**: {snap.get('halted') or '— running —'}",
        "",
        f"## believed-true (temporal ledger) — {len(standing)} standing, {len(disputed)} disputed",
    ]
    for e in standing:
        lines.append(f"- ✓ `{e['atom']}`  valid since {e['valid_from']}")
    for e in disputed:
        lines.append(f"- ✗ `{e['atom']}`  disputed at {e['invalid_at']} (kept, not deleted)")
    return "\n".join(lines)


# ── observe: the self-refreshing HTML console (the live-console seed) ──────────────────────
def render_html(workdir: Path) -> str:
    """A GENUINELY live page: it does NOT bake the snapshot in (the frozen-HTML bug caught
    2026-06-18 — a baked page + <meta refresh> just reloads stale numbers). Instead it FETCHES
    world.snapshot.json every 2s with JS and re-renders the DOM, so opening the file once shows
    the loop progress live. Open it from the workdir (so the relative fetch of the snapshot
    resolves): the page and world.snapshot.json sit side by side."""
    return """<!doctype html><html><head><meta charset="utf-8">
<title>ECHELON WORLD</title>
<style>body{font:14px ui-monospace,monospace;background:#0b0e14;color:#cdd6f4;padding:24px}
h1{color:#89b4fa} .k{color:#a6adc8} .v{color:#f9e2af}
table{border-collapse:collapse;margin-top:12px;width:100%}
td,th{border:1px solid #313244;padding:4px 8px;text-align:left}
.bar{margin:12px 0;padding:10px;background:#11151c;border-radius:8px}
#err{color:#f38ba8}</style></head><body>
<h1>ECHELON WORLD &mdash; live</h1>
<div class="bar">
<span class="k">round</span> <span class="v" id="round">-</span> &nbsp;
<span class="k">last</span> <span class="v" id="last">-</span> &nbsp;
<span class="k">spent</span> <span class="v" id="spent">$0</span> &nbsp;
<span class="k">earned</span> <span class="v" id="earned">0</span> &nbsp;
<span class="k">status</span> <span class="v" id="status">connecting&hellip;</span>
</div>
<div class="bar"><span class="k">goal</span> &rarr; <span class="v" id="goal">&mdash;</span><br>
<span class="k">from</span> <code id="from">&mdash;</code></div>
<div class="bar"><span class="k">live step</span> &rarr; <span class="v" id="livestep">&mdash;</span> &nbsp;
<span class="k">warmth</span> <span class="v" id="livewarmth">&mdash;</span> &nbsp;
<span class="k">last event</span> <span class="v" id="lastevt">&mdash;</span> <span id="stale"></span></div>
<h3>believed-true (temporal ledger)</h3>
<table id="bt"><tr><th></th><th>atom</th><th>valid_from</th><th>invalid_at</th></tr></table>
<div id="err"></div>
<script>
async function tick(){
  try{
    // cache-bust so the browser always re-reads the file the loop is rewriting
    const r = await fetch('world.snapshot.json?_=' + Date.now());
    if(!r.ok){ document.getElementById('err').textContent = 'snapshot not found ('+r.status+')'; return; }
    const s = await r.json();
    document.getElementById('err').textContent = '';
    document.getElementById('round').textContent  = s.round ?? '-';
    document.getElementById('last').textContent   = s.last_outcome ?? '-';
    document.getElementById('spent').textContent  = '$' + (s.spent ?? 0);
    document.getElementById('earned').textContent = s.earned_total ?? 0;
    document.getElementById('status').textContent = s.halted ? s.halted : '— running —';
    document.getElementById('goal').textContent   = s.goal ?? '—';
    document.getElementById('from').textContent   = s.from_atom ?? '—';
    document.getElementById('livestep').textContent   = s.live_step ?? '—';
    document.getElementById('livewarmth').textContent = s.live_warmth ?? '—';
    // last event timestamp + a STALE warning if a worker hasn't emitted in >60s (a hang)
    const t = s.last_event_ts ? new Date(s.last_event_ts) : null;
    document.getElementById('lastevt').textContent = t ? t.toLocaleTimeString() : '—';
    const stale = t ? (Date.now() - t.getTime())/1000 : 0;
    document.getElementById('stale').innerHTML = (t && stale > 60 && !s.halted)
      ? ' <span style="color:#f38ba8">⚠ STALLED ' + Math.round(stale) + 's (worker may be hung)</span>' : '';
    const bt = s.believed_true || [];
    const head = '<tr><th></th><th>atom</th><th>valid_from</th><th>invalid_at</th></tr>';
    const rows = bt.map(e =>
      '<tr><td>'+(e.invalid_at?'✗':'✓')+'</td><td><code>'+e.atom+'</code></td>'+
      '<td>'+(e.valid_from||'')+'</td><td>'+(e.invalid_at||'—')+'</td></tr>').join('');
    document.getElementById('bt').innerHTML = head + rows;
  }catch(e){ document.getElementById('err').textContent = 'read error: ' + e; }
}
tick(); setInterval(tick, 2000);
</script>
</body></html>"""


# ── observe: serve the live console over HTTP (fetch() needs http, not file://) ────────────
def serve(workdir: Path, *, port: int = 8787, open_browser: bool = True) -> None:
    """Serve the workdir over HTTP so the live HTML can fetch world.snapshot.json (browsers
    block fetch of file:// URLs). Writes world.html if missing, then serves until Ctrl-C."""
    import http.server, socketserver, functools, threading, webbrowser
    workdir.mkdir(parents=True, exist_ok=True)
    (workdir / "world.html").write_text(render_html(workdir), encoding="utf-8")
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(workdir))
    url = f"http://localhost:{port}/world.html"
    with socketserver.TCPServer(("", port), handler) as httpd:
        print(f"[worldview] live console at {url}  (Ctrl-C to stop)", flush=True)
        if open_browser:
            threading.Timer(0.6, lambda: webbrowser.open(url)).start()
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\n[worldview] server stopped", flush=True)


# ── steer: the God's-eye nudge ──────────────────────────────────────────────────────────────
def push_nudge(workdir: Path, text: str) -> None:
    npath = workdir / "nudge.txt"
    npath.parent.mkdir(parents=True, exist_ok=True)
    # BOM-free append (the live-caught defect 2026-06-18): the loop reads utf-8-sig, but we
    # write clean UTF-8 with no BOM so even a raw reader gets a clean goal.
    with npath.open("a", encoding="utf-8") as f:
        f.write(text.strip() + "\n")
    print(f"[worldview] nudged → {npath}: {text.strip()}", flush=True)


# ── CLI ──────────────────────────────────────────────────────────────────────────────────
def main() -> None:
    import argparse
    ap = argparse.ArgumentParser(prog="apps.world.worldview",
                                 description="Steer & observe the self-running world loop.")
    ap.add_argument("verb", choices=["tail", "card", "html", "serve", "nudge"])
    ap.add_argument("--workdir", required=True, help="the loop's workdir (has world.jsonl)")
    ap.add_argument("--text", default="", help="(nudge) the God's-eye instruction")
    ap.add_argument("--out", default="", help="(html) write to this path instead of stdout")
    ap.add_argument("--port", type=int, default=8787, help="(serve) http port")
    ap.add_argument("--no-follow", action="store_true", help="(tail) print once and exit")
    args = ap.parse_args()
    wd = Path(args.workdir)

    if args.verb == "tail":
        tail(wd, follow=not args.no_follow)
    elif args.verb == "card":
        print(render_card(wd))
    elif args.verb == "html":
        html = render_html(wd)
        if args.out:
            Path(args.out).write_text(html, encoding="utf-8")
            print(f"[worldview] wrote {args.out}")
        else:
            print(html)
    elif args.verb == "serve":
        serve(wd, port=args.port)
    elif args.verb == "nudge":
        if not args.text:
            ap.error("nudge needs --text")
        push_nudge(wd, args.text)


if __name__ == "__main__":
    main()
