"""bg_job.py — background-job tracker for the tool timeout-detach harness.

A tool call that overruns its timeout DETACHes to a _BgJob so the loop
never wedges on a hung call (the headless-Chrome freeze pattern). The job
holds the worker thread + any spawned subprocess (so it's truly killable
via the process GROUP) + a live output buffer the agent can poll.
"""
from __future__ import annotations
import os
import signal
import subprocess
import threading
import time


class _BgJob:
    """A tool call that overran its timeout and DETACHED to the background (the Claude way: a slow/
    hung call never wedges the loop — it becomes a tracked bg job, the agent keeps going). Holds the
    worker thread + any spawned subprocess (so it's truly killable via the process GROUP) + a live
    output buffer the agent can poll with check_bg(n). See ToolRegistry.execute."""
    def __init__(self, n: int, name: str, args: dict):
        self.n = n
        self.name = name
        self.args = args
        self.thread: threading.Thread | None = None
        self.proc: subprocess.Popen | None = None   # set by run_bash when it spawns
        self.result: str | None = None              # set when the work finishes
        self.killed = False
        self.started = time.time()

    def alive(self) -> bool:
        return self.result is None and not self.killed

    def kill(self) -> bool:
        """Truly terminate the work. A subprocess-backed job kills the whole process GROUP (so a
        pipe-holding child like headless Chrome dies too — the wedge fix). A pure-Python thread
        can't be force-killed in CPython, so it's abandoned as a daemon (its result discarded)."""
        self.killed = True
        if self.proc and self.proc.poll() is None:
            try:
                if os.name == "nt":
                    self.proc.send_signal(signal.CTRL_BREAK_EVENT)  # group, needs CREATE_NEW_PROCESS_GROUP
                    self.proc.kill()
                    subprocess.run(["taskkill", "/F", "/T", "/PID", str(self.proc.pid)],
                                   capture_output=True)              # /T = kill the whole tree
                else:
                    os.killpg(os.getpgid(self.proc.pid), signal.SIGKILL)
                return True
            except Exception:
                return False
        return True
