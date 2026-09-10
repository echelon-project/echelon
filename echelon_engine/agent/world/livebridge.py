"""The live bridge — the agent's real chat window with its partner.

Owner, 2026-06-05: the swarm waits until the agent has "a proper way for the input and
output, like our chat window." What we had was a hack: stdout that BUFFERS when piped (so
the partner saw empty logs), and a poll-a-file input. This is the fix for BOTH, in one
mechanism — the way the owner and I share a live chat, the agent and its partner now share one.

THE SHAPE (a streamed transcript + a reply channel, both tailable live):
  <dir>/live.jsonl   — APPEND-ONLY, one JSON event per line, FLUSHED + fsync'd every write.
                       The agent streams EVERY beat here the moment it happens: act, observe,
                       warmth, say, ask, permission. A partner runs `tail -f live.jsonl` (or the
                       bundled `watch`) and sees the agent think in real time — no buffering.
  <dir>/reply.jsonl  — APPEND-ONLY, the partner's side. Each line is the partner's answer to the
                       most recent agent question/permission request (by seq). The agent blocks
                       only when it ASKS (ask_partner / a gated act); it reads the reply whose
                       `re` matches its open `seq`.

Why JSONL append-only and not a chat buffer: it is the observable-bus shape a SWARM needs (one
bus per agent, many agents, one partner watching N tails) AND it is unbuffered/lossless (the
buffered-stdout bug cannot recur — each line is on disk the instant it's written). It is the
live chat window for ONE agent and the foundation the swarm stands on. See [[partner seam]],
project-memory-is-substrate-readable.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path


class LiveBridge:
    """A live, unbuffered, two-way transcript between the agent and its partner."""

    def __init__(self, bridge_dir: str | Path):
        # A bare NAME (not a path) lands under the substrate home ~/.echelon/bridges/<name> —
        # one home for all state (owner: keep runtime artifacts in ~/.echelon, not OS /tmp).
        # An explicit path (absolute, or containing a separator) is honored as-is.
        bd = Path(bridge_dir)
        if not bd.is_absolute() and len(bd.parts) == 1:
            # LAYERING-TENSION: paths.BRIDGES is in echelon_sdk.paths (sdk layer, OK for apps)
            from echelon_sdk.paths import BRIDGES, ensure
            ensure()
            bd = BRIDGES / bridge_dir
        self.dir = bd
        self.dir.mkdir(parents=True, exist_ok=True)
        self.live = self.dir / "live.jsonl"
        self.reply = self.dir / "reply.jsonl"
        self._seq = 0
        # Fresh session: truncate both streams so a tail starts clean. (Append-only WITHIN a run;
        # a new run is a new conversation — the prior transcript is the run's own artifact to keep
        # elsewhere if wanted.) We DON'T delete history mid-run.
        self.live.write_text("", encoding="utf-8")
        self.reply.write_text("", encoding="utf-8")
        self._emit_raw("session", {"started": time.strftime("%Y-%m-%dT%H:%M:%S"),
                                   "dir": str(self.dir)})

    # --- the stream (agent -> partner), unbuffered ----------------------------
    def _emit_raw(self, kind: str, data: dict) -> int:
        self._seq += 1
        rec = {"seq": self._seq, "t": round(time.time(), 3), "kind": kind, **data}
        line = json.dumps(rec, ensure_ascii=False)
        # O_APPEND + flush + fsync == the line is on disk the instant it's written. This is what
        # kills the buffered-stdout blindness: a `tail -f` sees it immediately, every time.
        with open(self.live, "a", encoding="utf-8") as f:
            f.write(line + "\n")
            f.flush()
            os.fsync(f.fileno())
        return self._seq

    def emit(self, kind: str, data: dict) -> None:
        """Stream one agent event to the live transcript (fire-and-forget, unbuffered)."""
        # keep payloads bounded so the stream stays readable; full data is in the agent's own log
        slim = {k: (v[:1200] if isinstance(v, str) else v) for k, v in data.items()}
        self._emit_raw(kind, slim)

    # --- the ask/answer turn (agent <-> partner) ------------------------------
    def ask(self, situation: str, tried: str = "", timeout_s: int = 900) -> str:
        """The agent asks; block until the partner replies on this seq, or timeout. The question
        is streamed (so the watching partner sees it inline), then we poll reply.jsonl for a line
        whose `re` == this question's seq. Same mechanism as ask_partner + a gated act."""
        qseq = self._emit_raw("ask", {"situation": situation, "tried": tried, "awaiting_reply": True})
        deadline = time.time() + timeout_s
        seen = 0
        while time.time() < deadline:
            try:
                lines = self.reply.read_text(encoding="utf-8").splitlines()
            except FileNotFoundError:
                lines = []
            for ln in lines[seen:]:
                seen = len(lines)
                ln = ln.strip()
                if not ln:
                    continue
                try:
                    r = json.loads(ln)
                except json.JSONDecodeError:
                    # tolerate a partner who just typed raw text as the latest reply line
                    r = {"re": qseq, "answer": ln}
                if r.get("re") in (qseq, "latest", None) and r.get("answer"):
                    self._emit_raw("answered", {"re": qseq, "answer": r["answer"]})
                    return r["answer"]
            time.sleep(1.5)
        self._emit_raw("ask_timeout", {"re": qseq, "after_s": timeout_s})
        return ""

    # --- partner-side helper (so I can answer from a one-liner) ----------------
    def answer(self, text: str, re_seq: int | str = "latest") -> None:
        """Partner writes an answer (used by the watch helper / a partner CLI)."""
        with open(self.reply, "a", encoding="utf-8") as f:
            f.write(json.dumps({"re": re_seq, "answer": text,
                                "t": round(time.time(), 3)}, ensure_ascii=False) + "\n")
            f.flush()
            os.fsync(f.fileno())

    # --- the heartbeat (loop -> partner: "an agent is alive here") ------------
    # Written each step by the running loop. The server reads its mtime/age to tell the UI
    # whether a live agent is attached — so an interject to a DEAD bridge is flagged, not
    # silently queued into a void (the "ok but not registered" confusion, 2026-06-05).
    @property
    def heartbeat_file(self) -> Path:
        return self.dir / "heartbeat.json"

    def beat(self, step: int = 0, status: str = "running") -> None:
        """Loop-side: stamp 'I am alive' each step. Cheap, best-effort."""
        try:
            self.heartbeat_file.write_text(
                json.dumps({"t": round(time.time(), 2), "step": step, "status": status}),
                encoding="utf-8")
        except OSError:
            pass

    # --- the control channel (control -> loop: stop + interject) --------------
    # control.json is the partner's live control surface (written by the web UI's STOP button +
    # interject box). The loop polls poll_control() each step. stop is sticky (a press halts);
    # interject is consumed once (a steer is delivered to the agent a single time, then cleared).
    @property
    def control_file(self) -> Path:
        return self.dir / "control.json"

    def poll_control(self) -> dict:
        """Loop-side: read pending control, CONSUMING a one-shot interject. Returns
        {"stop": bool, "interject": str|None}. Robust to a partial write (returns empty)."""
        cf = self.control_file
        if not cf.exists():
            return {}
        try:
            state = json.loads(cf.read_text(encoding="utf-8") or "{}")
        except (json.JSONDecodeError, OSError):
            return {}
        out = {"stop": bool(state.get("stop")), "interject": state.get("interject")}
        if out["interject"]:
            # consume the one-shot steer so it lands exactly once; keep stop sticky.
            state["interject"] = None
            try:
                cf.write_text(json.dumps(state), encoding="utf-8")
            except OSError:
                pass
        return out

    def signal_stop(self, reason: str = "") -> None:
        """Partner/OS-side: signal the observe-only consumer to halt (stop-on-exception)."""
        self.set_control(stop=True, interject=(reason or None))

    def should_stop(self) -> bool:
        """Loop-side: True if a stop has been signalled (reads the existing control channel)."""
        return bool(self.poll_control().get("stop"))

    def set_control(self, *, stop: bool | None = None, interject: str | None = None) -> None:
        """Partner-side: press stop / send an interject (used by the web UI + a partner CLI)."""
        try:
            state = json.loads(self.control_file.read_text(encoding="utf-8") or "{}")
        except (json.JSONDecodeError, FileNotFoundError, OSError):
            state = {}
        if stop is not None:
            state["stop"] = stop
        if interject is not None:
            state["interject"] = interject
        self.control_file.write_text(json.dumps(state), encoding="utf-8")


def make_resolver(bridge: "LiveBridge", timeout_s: int = 900):
    """Adapt the LiveBridge into the (situation, tried) -> answer resolver the partner seam +
    permission gate already speak. One channel, both uses."""
    def resolver(situation: str, tried: str) -> str:
        return bridge.ask(situation, tried, timeout_s=timeout_s)
    return resolver
