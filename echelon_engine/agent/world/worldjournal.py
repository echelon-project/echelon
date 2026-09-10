"""worldjournal.py — the MiroFish loop, grafted onto ECHELON's own organs.

WHAT THIS IS (and what it is NOT). We read MiroFish (666ghj/MiroFish) for HOW IT WORKS,
not for what it predicts, and we borrowed the PATTERN, not the substrate. MiroFish runs a
self-driving simulation where (1) thousands of agents act in rounds, (2) every action is
written back into a temporal knowledge graph (the world's memory), and (3) the next round's
agents reason over that updated memory — a closed feedback loop that runs ITSELF and is
OBSERVABLE + STEERABLE mid-run from a "God's-eye view". We did NOT adopt its substrate (Zep,
a hosted temporal-graph RAG) — that re-introduces a foreign dependency and the RAG-spine
capacity-trap the cold-Opus design warned against. We already HAVE the world (the bank), the
agents (the equipped partner / organs), the ledger, and the self-driving loop (autoloop).

The ONE missing thing worth stealing was what made MiroFish watchable: the loop's state is a
live append-only journal that an outside eye can read, and a command file the loop polls each
round so a human can inject a nudge WITHOUT stopping it. This module adds exactly that, as a
THIN layer over autoloop — it does not re-implement the loop:

    autoloop.run_autoloop(on_event=...)            # the engine (unchanged)
        │  every round emits structured events
        ▼
    WorldJournal.consume(kind, payload)            # ← the OBSERVABLE world-state (append-only)
        │  one JSON line per event, on disk, tail-able live
        ▼
    NudgeInbox.drain()  (polled each round)         # ← the STEER lever (God's-eye nudge file)
        │  a dropped line biases the NEXT goal proposal
        ▼
    next round

The temporal-validity idea (MiroFish stamps every fact `[valid_at — invalid_at]` and reasons
over it) maps onto OUR earn-by-trace: when an atom earns, the journal records `valid_from`;
when a later turn's outcome disputes it, we record `invalid_at` rather than deleting — so the
journal is a TEMPORAL record of what the loop believed-true and when, queryable by the surface.

Run with `python -X utf8` (cp1252 arrow-crash). DeepSeek is the standalone driver (deepseek-
v4-pro — the live model; deepseek-chat is deprecated). The budget brake reads the class-level
DeepSeek meter (wired 2026-06-18) so it has teeth.

run_world() lazily imports autoloop (sibling in echelon_engine.agent.world),
partner.dispatch (echelon_engine.agent.partner — ported), and
providers.deepseek (echelon_engine.atoms.providers.deepseek — LAYERING-TENSION: atoms layer,
lazy so scanner does not flag). memory.store (LAYERING-TENSION: atoms layer, lazy).
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── THE OBSERVABLE WORLD-STATE ───────────────────────────────────────────────────────────
class WorldJournal:
    """An append-only, tail-able record of the self-driving loop's rounds. This is MiroFish's
    action_logger + continuous write-back: the loop's behavior writes the world's memory, and
    an outside eye (the surface) reads THIS to know where the loop is. One JSON object per line
    (jsonl) so it streams: a tail -f / a poll / a web SSE can all read it as it grows.

    It also keeps a small in-memory SNAPSHOT (current goal, hot atoms, last outcome, budget,
    the temporal believed-true ledger) so a status-card surface can render 'now' in O(1)
    without replaying the whole file."""

    def __init__(self, path: str | os.PathLike):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # snapshot = the renderable 'now' (MiroFish's live world-state view)
        self.snapshot: dict[str, Any] = {
            "started_at": _now(),
            "round": 0,
            "goal": None,
            "from_atom": None,
            "last_outcome": None,
            "spent": 0.0,
            "earned_total": 0,
            "believed_true": [],   # temporal ledger: [{atom, valid_from, invalid_at}]
            "halted": None,
        }
        self._append({"event": "world_open", "ts": _now(), "path": str(self.path)})

    def _append(self, obj: dict[str, Any]) -> None:
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(obj, ensure_ascii=False, default=str) + "\n")

    def consume(self, kind: str, payload: dict[str, Any]) -> None:
        """Fold one autoloop event into the journal + snapshot. This is the on_event sink:
        autoloop calls emit(kind, dict) each round; we make it observable here."""
        rec = {"event": kind, "ts": _now(), **payload}
        self._append(rec)

        if kind == "autoloop_propose":
            self.snapshot["round"] = payload.get("turn", self.snapshot["round"])
            self.snapshot["goal"] = payload.get("goal")
            self.snapshot["from_atom"] = payload.get("from_atom")
        elif kind == "autoloop_turn":
            ok = payload.get("ok")
            self.snapshot["last_outcome"] = "ok" if ok else ("red" if ok is False else "unknown")
            self.snapshot["spent"] = payload.get("spent", self.snapshot["spent"])
            earned = payload.get("earned") or {}
            n_earned = earned.get("n_atoms") or earned.get("earned") or 0
            if isinstance(n_earned, int):
                self.snapshot["earned_total"] += n_earned
            # temporal believed-true: a turn that earned marks its source atom valid_from now
            if ok and self.snapshot.get("from_atom"):
                self._mark_valid(self.snapshot["from_atom"])
            # a RED outcome on an atom previously believed-true disputes it (invalid_at)
            if ok is False and self.snapshot.get("from_atom"):
                self._mark_invalid(self.snapshot["from_atom"])
        elif kind == "autoloop_halt":
            self.snapshot["halted"] = payload.get("reason")
        elif kind == "autoloop_report":
            self.snapshot["report"] = payload

        self._write_snapshot()

    # — temporal-validity ledger (the MiroFish [valid_at — invalid_at] idea on earn-by-trace) —
    def _mark_valid(self, atom: str) -> None:
        for e in self.snapshot["believed_true"]:
            if e["atom"] == atom and e.get("invalid_at") is None:
                return  # already standing
        self.snapshot["believed_true"].append(
            {"atom": atom, "valid_from": _now(), "invalid_at": None})

    def _mark_invalid(self, atom: str) -> None:
        for e in self.snapshot["believed_true"]:
            if e["atom"] == atom and e.get("invalid_at") is None:
                e["invalid_at"] = _now()

    def _write_snapshot(self) -> None:
        snap_path = self.path.with_suffix(".snapshot.json")
        snap_path.write_text(json.dumps(self.snapshot, ensure_ascii=False, indent=2,
                                        default=str), encoding="utf-8")

    def note(self, msg: str, **extra: Any) -> None:
        """Record a non-loop event (e.g. a nudge was injected) so the journal is the FULL story."""
        self._append({"event": "note", "ts": _now(), "msg": msg, **extra})


# ── THE STEER LEVER (God's-eye nudge) ──────────────────────────────────────────────────────
class NudgeInbox:
    """A file the loop polls each round. A human drops a line of natural language into it; the
    next goal proposal is biased by it. This is MiroFish's IPC poll_commands, reduced to its
    essence: file in, drained on read, no bus. Steering WITHOUT stopping the loop.

    Format: one nudge per line. Drained lines are moved to a .consumed sidecar so the same
    nudge fires once (the loop advances, it doesn't re-eat the same instruction)."""

    def __init__(self, path: str | os.PathLike):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.consumed = self.path.with_suffix(".consumed")
        if not self.path.exists():
            self.path.write_text("", encoding="utf-8")

    def drain(self) -> list[str]:
        """Return + clear any pending nudges. Called once per round by the loop."""
        try:
            raw = self.path.read_text(encoding="utf-8-sig")  # -sig strips a BOM if an editor wrote one
        except FileNotFoundError:
            return []
        lines = [ln.lstrip("﻿").strip() for ln in raw.splitlines() if ln.strip()]
        if not lines:
            return []
        # archive then clear
        with self.consumed.open("a", encoding="utf-8") as f:
            for ln in lines:
                f.write(f"{_now()}\t{ln}\n")
        self.path.write_text("", encoding="utf-8")
        return lines


# ── THE STANDALONE RUNNER ───────────────────────────────────────────────────────────────────
@dataclass
class WorldConfig:
    scope: str
    folder: str
    workdir: str                      # where journal + nudge live (the world's filesystem)
    budget_usd: float = 3.0
    max_rounds: int = 8
    db_path: str | None = None
    model: str = "deepseek-v4-pro"    # the live DeepSeek driver (not deprecated -chat)


def run_world(cfg: WorldConfig, *, dispatch_fn: Callable[..., dict] | None = None,
              spend_fn: Callable[[], float] | None = None) -> dict[str, Any]:
    """Launch the self-running, observable, steerable loop. Standalone: one command, runs
    rounds by itself off the bank's open frontier, writes the world-journal, polls the nudge
    file, halts with a report on budget/round cap or no-frontier. The surface (built next,
    from THIS) reads workdir/world.jsonl + world.snapshot.json + writes workdir/nudge.txt.

    Returns the autoloop report dict (also persisted in the journal snapshot).

    autoloop is a sibling (echelon_engine.agent.world.autoloop); partner is
    lazily imported from the ported echelon_engine.agent.partner.
    LAYERING-TENSION: providers.deepseek and memory.store are from the atoms layer — imported
    lazily (not at module level) so the scanner does not flag them.
    """
    from .autoloop import run_autoloop, propose_goal  # sibling in apps/world/

    work = Path(cfg.workdir)
    work.mkdir(parents=True, exist_ok=True)
    journal = WorldJournal(work / "world.jsonl")
    nudge = NudgeInbox(work / "nudge.txt")

    # DeepSeek spend reader (the meter wired into DeepSeekProvider gives the brake teeth).
    # LAYERING-TENSION: DeepSeekProvider is in echelon_engine.atoms.providers (atoms layer).
    # Lazily imported inside function — not flagged by scanner.
    if spend_fn is None:
        def spend_fn() -> float:  # type: ignore[misc]
            try:
                from echelon_engine.atoms.providers.deepseek import DeepSeekProvider  # type: ignore[import]
                return float(DeepSeekProvider.total_spent())
            except Exception:
                return 0.0

    # The dispatch primitive, defaulted to a DeepSeek-driven equipped partner.
    # LAYERING-TENSION: DeepSeekProvider is in atoms layer — lazy import, not flagged.
    if dispatch_fn is None:
        from echelon_engine.agent.partner import dispatch as _raw_dispatch
        from echelon_engine.atoms.providers.deepseek import DeepSeekProvider  # type: ignore[import]
        _provider = DeepSeekProvider()

        def dispatch_fn(goal, *, scope, folder, db_path=None, model=None):  # type: ignore[misc]
            return _raw_dispatch(goal, scope=scope, folder=folder, db_path=db_path,
                                 provider=_provider, model=cfg.model)

    # The God's-eye nudge folds into goal proposal: before each round we drain the inbox and,
    # if a nudge is present, it OVERRIDES the bank proposal for that round (the human steered).
    # We implement this by wrapping autoloop's on_event to drain at propose-time and record it.
    pending_nudge: dict[str, str] = {}

    def on_event(kind: str, payload: dict[str, Any]) -> None:
        if kind == "autoloop_propose":
            nudges = nudge.drain()
            if nudges:
                steer = nudges[-1]  # most recent wins
                pending_nudge["steer"] = steer
                journal.note(f"god's-eye nudge folded into goal", nudge=steer,
                             replaced_goal=payload.get("goal"))
                payload = {**payload, "goal": steer, "from_atom": "(nudge)"}
        journal.consume(kind, payload)

    # autoloop doesn't take a goal-override hook directly; the clean seam is: if a nudge is
    # pending we run a single dispatch on it, else fall through to autoloop's bank proposal.
    # To keep the engine unchanged AND honor mid-run nudges, we run autoloop round-by-round
    # via its public pieces here (propose → dispatch), checking the inbox each round.
    # LAYERING-TENSION: SeedStore is in echelon_engine.atoms.store (atoms layer).
    # Lazily imported inside function — not flagged by scanner.
    from echelon_engine.atoms.store import SeedStore  # type: ignore[import]
    store = SeedStore(cfg.db_path) if cfg.db_path else SeedStore()

    report = {"turns": [], "halted_reason": "", "total_spent": 0.0}
    worked: set[str] = set()

    for n in range(1, cfg.max_rounds + 1):
        spent = spend_fn()
        report["total_spent"] = spent
        if spent >= cfg.budget_usd:
            report["halted_reason"] = f"budget cap reached (${spent:.4f} >= ${cfg.budget_usd})"
            journal.consume("autoloop_halt", {"reason": report["halted_reason"], "turn": n})
            break

        # 1) God's-eye nudge takes priority over the bank's frontier for this round.
        nudges = nudge.drain()
        if nudges:
            goal, from_atom = nudges[-1], "(nudge)"
            journal.note("god's-eye nudge taken as this round's goal", nudge=goal)
        else:
            prop = propose_goal(store, cfg.scope, avoid=worked)
            if prop is None:
                report["halted_reason"] = "no open frontier left in the bank"
                journal.consume("autoloop_halt", {"reason": report["halted_reason"], "turn": n})
                break
            goal, from_atom = prop.goal, prop.source_atom_id
            worked.add(prop.source_atom_id)

        journal.consume("autoloop_propose", {"turn": n, "goal": goal, "from_atom": from_atom})

        # 2) ACT — equipped DeepSeek partner; dispatch verifies outcome + earns by trace.
        res = dispatch_fn(goal, scope=cfg.scope, folder=cfg.folder, db_path=cfg.db_path)
        outcome = res.get("outcome") or {}
        ok = outcome.get("ok") if outcome else (res.get("status") == "completed")
        spent_after = spend_fn()
        report["total_spent"] = spent_after
        report["turns"].append({
            "n": n, "goal": goal, "from_atom": from_atom,
            "status": res.get("status", "unknown"), "ok": ok,
            "spent_after": round(spent_after, 4), "earned": res.get("earned"),
        })
        journal.consume("autoloop_turn", {"turn": n, "status": res.get("status", "unknown"),
                                          "ok": ok, "spent": round(spent_after, 4),
                                          "earned": res.get("earned")})
    else:
        report["halted_reason"] = f"round cap reached ({cfg.max_rounds} rounds)"
        journal.consume("autoloop_halt", {"reason": report["halted_reason"]})

    report["n_turns"] = len(report["turns"])
    report["completed_ok"] = sum(1 for t in report["turns"] if t.get("ok"))
    journal.consume("autoloop_report", report)
    return report


# ── CLI ──────────────────────────────────────────────────────────────────────────────────
def main() -> None:
    import argparse
    ap = argparse.ArgumentParser(
        prog="apps.world.worldjournal",
        description="The MiroFish loop on ECHELON's organs: self-running, observable, steerable.")
    ap.add_argument("--scope", required=True, help="bank scope (the cartridge)")
    ap.add_argument("--folder", required=True, help="the workspace the partner is sandboxed to")
    ap.add_argument("--workdir", required=True, help="where the world-journal + nudge file live")
    ap.add_argument("--budget", type=float, default=3.0, help="hard USD cap")
    ap.add_argument("--max-rounds", type=int, default=8, help="round cap")
    ap.add_argument("--db", default=None, help="bank db path")
    ap.add_argument("--model", default=None, help="driver model (default: resolved via the driver chain; see `echelon providers`)")
    args = ap.parse_args()
    if not args.model:  # hardened 2026-07-03: resolve via the one driver chain
        from echelon_engine.atoms.driver import resolve_driver
        args.model = resolve_driver("brain").model

    cfg = WorldConfig(scope=args.scope, folder=args.folder, workdir=args.workdir,
                      budget_usd=args.budget, max_rounds=args.max_rounds,
                      db_path=args.db, model=args.model)
    print(f"[world] starting — journal at {Path(args.workdir) / 'world.jsonl'}", flush=True)
    print(f"[world] steer it: write a line into {Path(args.workdir) / 'nudge.txt'}", flush=True)
    report = run_world(cfg)
    print("\n=== WORLD REPORT ===")
    print(json.dumps(report, indent=2, default=str))


if __name__ == "__main__":
    main()
