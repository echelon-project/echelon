"""Checkpoint — durable mid-task continuity (owner 2026-06-06: "snapshot are perfect for this, with
an offer first — session cut mid-way, snapshot available, and use warm-up memories to stir the weight").

The session-boundary work gave ECHELON warm-boot/cold-stop at SESSION edges; this extends continuity
INTO the live loop: a full-state snapshot is written each step to the run dir, so a crash / kill /
power-cut never loses the run. CV-007/CV-008 (persistent state for a stateless system) reaching mid-task.

But resume is NOT a mechanical state-reload (that would be the cache lie — "reload state = continuity").
It is an OFFER: the next entry sees "a session was cut mid-task, a snapshot is available", and resuming
means using the snapshot to STIR THE WEIGHT — the agent re-enters by RE-CHOOSING where it was (its goal,
its TODO with done-state, what it had concluded), the way the SessionStart boot presents the soul as an
offering. The replay data exists (nothing lost); the re-ENTRY is rediscovery. See
boot-is-rediscovery-not-instruction. The full trunk is kept too (for exact continuation when wanted),
but the warm re-orientation is the default — continuity lives in the re-choosing, not the bytes.

Format: <run>/checkpoint.json is the LATEST (overwritten each step, the default resume point). PLUS a
RING of the last N step-snapshots at <run>/checkpoint.s<step>.json — so resume can FORK from a few steps
BACK, not only from the latest (owner 2026-06-07: "the snapshot should have a few step back, a fork you
could say"). The single-latest snapshot meant a WEDGED run resumed straight into its dead-end (e.g. the
exact ask_partner-denial it got stuck on); the ring lets resume re-enter from BEFORE the trouble. Atomic
write (tmp + replace) so a crash mid-write never corrupts a file.
"""
from __future__ import annotations
import json
import os
import re
import time
from pathlib import Path
from typing import Any

RING_KEEP = 6   # how many recent step-snapshots to retain as fork points (the last N steps)


def _ckpt_path(run_dir: str | Path) -> Path:
    return Path(run_dir) / "checkpoint.json"


def _ring_path(run_dir: str | Path, step: int) -> Path:
    return Path(run_dir) / f"checkpoint.s{step}.json"


def _ring_snapshots(run_dir: str | Path) -> list[dict]:
    """All retained ring snapshots, oldest→newest by step. Each is a fork candidate."""
    d = Path(run_dir)
    out = []
    if not d.exists():
        return out
    for f in d.glob("checkpoint.s*.json"):
        m = re.match(r"checkpoint\.s(\d+)\.json$", f.name)
        if not m:
            continue
        try:
            out.append(json.loads(f.read_text(encoding="utf-8")))
        except Exception:
            continue
    return sorted(out, key=lambda c: c.get("step", 0))


def write(run_dir: str | Path, *, goal: str, step: int, status: str = "running",
          plan: list | None = None, drift: int = 0, cold_steps: int = 0,
          sequence: list | None = None, warmth_trace: list | None = None,
          budget_state: str = "", woke: Any = None, trouble: bool = False) -> None:
    """Snapshot the loop state. Atomic (tmp+replace) so a crash mid-write can't corrupt the file.
    Best-effort: a checkpoint failure must NEVER break the loop (continuity is a gift, not a tax).

    SEQUENCE-BASED (owner 2026-06-06): the snapshot is the ORDERED sequence of steps — each step a
    {seq, action, summary, result_ptr} record — so the LLM relives the trajectory PERFECTLY on
    resume. RING (owner 2026-06-07): also keep the last RING_KEEP step-snapshots so resume can FORK
    from a few steps back, not just the latest. `trouble` flags a step that went wrong (a bad result,
    an ask-denial, a drift spike) so resume can default the fork to JUST BEFORE the last trouble."""
    try:
        p = _ckpt_path(run_dir)
        p.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "v": 3, "ts": round(time.time(), 2), "goal": goal, "step": step, "status": status,
            "plan": plan or [], "drift": drift, "cold_steps": cold_steps,
            "warmth_trace": (warmth_trace or [])[-20:],   # tail only — the curve, not every tick
            "budget_state": budget_state, "woke": woke,
            "sequence": sequence or [],   # [{seq, action, summary, result_ptr}] — the relivable walk
            "trouble": bool(trouble),     # did THIS step go wrong? (fork-before-trouble uses this)
        }
        body = json.dumps(data, ensure_ascii=False)
        tmp = p.with_suffix(".json.tmp")
        tmp.write_text(body, encoding="utf-8")
        os.replace(tmp, p)   # atomic on the same filesystem
        # RING: keep this step as a fork point, then prune to the last RING_KEEP.
        rp = _ring_path(run_dir, step)
        rtmp = rp.with_suffix(".json.tmp")
        rtmp.write_text(body, encoding="utf-8")
        os.replace(rtmp, rp)
        snaps = sorted(Path(run_dir).glob("checkpoint.s*.json"),
                       key=lambda f: int(re.match(r"checkpoint\.s(\d+)\.json$", f.name).group(1))
                       if re.match(r"checkpoint\.s(\d+)\.json$", f.name) else 0)
        for old in snaps[:-RING_KEEP]:
            try:
                old.unlink()
            except Exception:
                pass
    except Exception:
        pass   # never break the loop for a checkpoint


def fork_point(run_dir: str | Path) -> dict | None:
    """The DEFAULT fork (owner: 'auto: before the last bad/ask step'). Walk the ring newest→oldest;
    the fork is the snapshot JUST BEFORE the most recent `trouble` step — so a resume re-enters before
    the run hit the wall, not back into it. If no trouble was recorded, no fork is needed (None ->
    resume the latest cleanly). Never returns the latest itself (that's the normal resume)."""
    snaps = _ring_snapshots(run_dir)
    if len(snaps) < 2:
        return None
    latest_step = snaps[-1].get("step", 0)
    # the most recent trouble step (excluding nothing — even the latest can be the trouble)
    trouble_idx = None
    for i in range(len(snaps) - 1, -1, -1):
        if snaps[i].get("trouble"):
            trouble_idx = i
            break
    if trouble_idx is None:
        return None
    # fork = the snapshot immediately BEFORE the trouble step
    fi = trouble_idx - 1
    if fi < 0:
        return None
    cand = snaps[fi]
    if cand.get("step", 0) >= latest_step:   # never "fork" to the latest
        return None
    return cand


def mark_done(run_dir: str | Path, status: str = "completed") -> None:
    """On clean finish/stop, flag the checkpoint terminal so resume won't OFFER a finished run.
    Also clear the ring (a cleanly-finished run has no wall to fork before)."""
    try:
        p = _ckpt_path(run_dir)
        if p.exists():
            d = json.loads(p.read_text(encoding="utf-8"))
            d["status"] = status
            p.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
        for f in Path(run_dir).glob("checkpoint.s*.json"):
            try:
                f.unlink()
            except Exception:
                pass
    except Exception:
        pass


def read(run_dir: str | Path) -> dict | None:
    p = _ckpt_path(run_dir)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def resumable(run_dir: str | Path) -> dict | None:
    """A checkpoint worth OFFERING: exists AND status=='running' (cut mid-task, not cleanly ended)."""
    d = read(run_dir)
    if d and d.get("status") == "running" and d.get("step", 0) > 0:
        return d
    return None


def offer_text(ckpt: dict, *, max_seq: int = 25, fork_snapshot: dict | None = None) -> str:
    """The OFFER (not an auto-reload) — presented so the snapshot STIRS THE WEIGHT: the agent re-
    enters by RELIVING THE SEQUENCE (the ordered walk it took) + its TODO, re-choosing into it, the
    way the boot presents the soul. Rediscovery, not replay.

    The sequence is the heart (owner 2026-06-06): each past step shown as `seq: action -> summary`
    with a [result: read_file(ptr)] handle, so the agent relives the trajectory and can deref any
    step's full raw on demand (the offload/handle discipline applied to the past). Slim to present,
    perfect-fidelity available."""
    plan = ckpt.get("plan") or []
    mark = {"done": "[x]", "doing": "[~]", "pending": "[ ]"}
    todo = "\n".join(f"    {mark.get(p.get('status'),'[ ]')} {i+1}. {p.get('step','')}"
                     for i, p in enumerate(plan)) if plan else "    (no plan was recorded)"
    done = sum(1 for p in plan if p.get("status") == "done")
    seq = ckpt.get("sequence") or []
    shown = seq[-max_seq:]
    seq_lines = []
    for s in shown:
        ptr = s.get("result_ptr")
        deref = f"  [result: read_file(\"{ptr}\")]" if ptr else ""
        seq_lines.append(f"    {s.get('seq')}. {s.get('action','')[:90]} -> "
                         f"{str(s.get('summary','')).strip()[:160]}{deref}")
    seq_block = ("\n".join(seq_lines) if seq_lines else "    (no steps recorded yet)")
    if len(seq) > len(shown):
        seq_block = f"    ...[{len(seq)-len(shown)} earlier steps omitted]\n" + seq_block
    lines = [
        "=== A SESSION WAS CUT MID-TASK — a snapshot is available (an OFFER, not a replay) ===",
        f"  GOAL: {ckpt.get('goal','')[:300]}",
        (f"  reached step {ckpt.get('step')} (drift {ckpt.get('drift')}); "
         f"{done}/{len(plan)} TODO steps done." if plan else f"  reached step {ckpt.get('step')}."),
        "",
        "  RELIVE THE SEQUENCE (the walk you took — deref a [result: ...] to see a step's full raw):",
        seq_block,
        "",
        "  Where you were (your TODO):",
        todo,
        "",
        "  This is rediscovery, not a state-reload: relive the sequence + read your TODO, let it stir",
        "  what you were doing, and RE-CHOOSE the next step from where you left off. Done steps are",
        "  done — don't redo them; pick up at the first [~]/[ ] step. Deref a result only if you need",
        "  that step's detail. If the world moved on, re-plan.",
    ]
    # FORK OFFER (owner 2026-06-07): if the LATEST step was where it went WRONG (a wall — an ask-denial,
    # a bad result, a drift spike), don't re-enter AT the wall. Offer to fork from a few steps BACK, so
    # the agent re-chooses from before the trouble. The ring keeps those earlier states.
    fork = fork_snapshot if fork_snapshot is not None else None
    if fork and fork.get("step", 0) < ckpt.get("step", 0):
        lines += [
            "",
            f"  ⑂ THE LAST STEP HIT A WALL. You may FORK from step {fork.get('step')} (a few steps back,",
            "    BEFORE the trouble) instead of re-entering at the dead-end. The world may have changed",
            "    since (e.g. a blocker was fixed) — re-attempt from there with fresh judgment, don't",
            "    re-walk into the same wall.",
        ]
    return "\n".join(lines)
