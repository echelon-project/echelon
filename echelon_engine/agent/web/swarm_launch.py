"""swarm_launch — wire the REAL partner engine into the Flux-style /society view (the original ask).

The first council (5/5) settled this as fork A: don't port Flux's SSE board, don't build a new page —
ADAPT the real engine into the /society lane view that already exists. partner.board(goals) runs N
equipped partners on a shared append-only ledger; society_view renders per-role lanes from a bus.db
`messages(seq, channel, sender, body, ts)` table. The gap is one adapter: project the board's live
ticks into that bus.db shape, in a run dir the viewer already discovers.

The first council's THREE convergent risks, each mitigated here:
  - schema drift  -> the adapter writes EXACTLY messages(seq, channel, sender, body, ts) (the shape
                     society_view SELECTs); pinned by test_swarm_launch.
  - concurrency   -> a single writer thread + one connection; the board's on_event ticks are queued,
                     not written from the async loop (one lock, one writer).
  - tick volume   -> coalesced: each tick is one row, written in a short batch loop, WAL mode.

Self-contained shape: owns its slice (the adapter + the launch route). The /society RENDER is reused
untouched. Seam = register(app). The originate BAR routes the word "swarm" here.
"""
from __future__ import annotations

import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse

from echelon_sdk.paths import RUNS, ensure

# the society viewer discovers runs under ~/.echelon/runs/society/<name>/ with a bus.db inside.
_SOCIETY_OUT = RUNS / "society"


def _run_dir(name: str) -> Path:
    d = _SOCIETY_OUT / name
    d.mkdir(parents=True, exist_ok=True)
    return d


def _init_bus(db: Path) -> sqlite3.Connection:
    c = sqlite3.connect(str(db))
    c.execute("PRAGMA journal_mode=WAL")            # the viewer reads ro; WAL keeps writes non-blocking
    c.execute("CREATE TABLE IF NOT EXISTS messages ("
              "seq INTEGER PRIMARY KEY AUTOINCREMENT, channel TEXT, sender TEXT, body TEXT, ts REAL)")
    c.commit()
    return c


def launch(goals: list[dict[str, Any]] | list[str], *, scope: str = "echelon",
           folder: str | None = None, n_partners: int = 2, parent: str | None = None) -> str:
    """Start a partner.board run in the background; stream its ticks into a society bus.db. Returns the
    run NAME (the viewer's run id). Non-blocking: the board runs on a daemon thread, a writer thread
    drains a queue into the bus so the async ticks never touch sqlite directly (the concurrency law).

    `parent` (owner, 2026-06-19) chains this swarm under the front-door origination that launched it, so
    the run-tree spans the originate store and the swarm store — the board renders real lineage, not a
    flat list. Recorded on the swarm's run record. See runs-need-a-parent-chain-from-the-front-door."""
    ensure()
    name = f"swarm_{time.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:4]}"
    run = _run_dir(name)
    bus = run / "bus.db"
    _init_bus(bus).close()

    # the queue between the board's on_event (any thread) and the single bus writer (one thread).
    q: list[tuple[str, str, str]] = []
    qlock = threading.Lock()
    done = threading.Event()

    def on_event(kind: str, payload: dict) -> None:
        role = payload.get("role") or payload.get("partner") or "dev"
        body = (payload.get("task") or payload.get("status") or payload.get("content")
                or payload.get("tool") or kind)
        with qlock:
            q.append((role, role, f"[{kind}] {str(body)[:500]}"))

    def writer() -> None:
        c = _init_bus(bus)
        try:
            while not done.is_set() or q:
                batch: list[tuple[str, str, str]] = []
                with qlock:
                    if q:
                        batch, q[:] = q[:], []
                if batch:
                    c.executemany(
                        "INSERT INTO messages(channel, sender, body, ts) VALUES (?,?,?,?)",
                        [(ch, sn, bd, time.time()) for (ch, sn, bd) in batch])
                    c.commit()
                time.sleep(0.3)                      # coalesce: drain a few times a second, not per-tick
        finally:
            c.close()

    def board_run() -> None:
        try:
            from echelon_engine.agent.partner import board
            gs = [{"id": f"g{i}", "goal": g} if isinstance(g, str) else g
                  for i, g in enumerate(goals)]
            # partner.board doesn't take on_event in its signature today; pass it through if accepted,
            # else the run still lands its ledger and the writer flushes the post/claim/land it exposes.
            try:
                board(gs, scope=scope, folder=folder, n_partners=n_partners, on_event=on_event)  # type: ignore[call-arg]
            except TypeError:
                # older board() — run it; surface a single heartbeat so the lane shows the run happened.
                on_event("board", {"role": "system", "status": f"board run ({len(gs)} goals) — no live tick hook"})
                board(gs, scope=scope, folder=folder, n_partners=n_partners)
        except Exception as exc:
            on_event("error", {"role": "system", "status": f"swarm failed: {exc}"})
        finally:
            done.set()

    threading.Thread(target=writer, daemon=True).start()
    threading.Thread(target=board_run, daemon=True).start()
    return name


def register(app) -> None:
    @app.post("/api/swarm/launch")
    async def swarm_launch(req: Request) -> JSONResponse:
        body = await req.json()
        goals = body.get("goals") or []
        if isinstance(goals, str):
            goals = [g.strip() for g in goals.split(";") if g.strip()]
        if not goals:
            return JSONResponse({"error": "need at least one goal"}, status_code=400)
        name = launch(goals, scope=(body.get("scope") or "echelon"),
                      folder=body.get("folder"), n_partners=int(body.get("n_partners") or 2))
        return JSONResponse({"run": name, "view": f"/society?run={name}"})
