"""Tool registry — the agent's hands. Curated + guarded.

Distilled from a Qwen tool-dump (qwen_tools_dump.py): the local model emitted 4000
lines of repeating file-ops with no guards and an unsafe rmtree. We took the MENU,
not the code — a small safe subset the loop actually needs, each with hard path-guards
so the agent cannot escape its sandbox. Enforce-don't-request applied to the hands:
the structure makes the wrong file-write impossible, it isn't asked of the model.

Each tool is (schema, fn). schema is OpenAI function-calling shape so GrokProvider
hands it straight to the wire. fn(**args) -> str (always a string the loop feeds back).
"""
from __future__ import annotations
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable

from echelon_sdk import config
from echelon_sdk.exceptions import ToolError   # WELD #4: the type lives in the sdk now (cycle gone)
from .tools_attach import _AttachMixin
from .tools_fileops import _FileOpsMixin        # no ordering hack needed: ToolError is from sdk, not here
from .bg_job import _BgJob
from .shell_select import pick_shell
from .tool_policy import (
    _DESTRUCTIVE, _DESTRUCTIVE_TOOLS,
    classify_destructive, target_exists,
)
from .sandbox_path import normalize_path, resolve_safe

# Gather tools locked by the convergence hard-gate (loop sets registry.gather_locked on persistent
# saturation): once the agent has provably enough evidence, only consult/finish remain. Owner 2026-06-07:
# run_bash is DELIBERATELY excluded — it is dual-use (gather AND mutate). Locking it would trap a
# judge agent that legitimately needs to act, and would deadlock a build run that got misclassified.
# The lock now covers only the UNAMBIGUOUS read tools; the loop's task-class gate (build vs judge) is
# the real protection against over-research. See classify_task / convergence_can_hardgate in loop.py.
_GATHER_LOCK_TOOLS = {"read_file", "search_file", "list_files", "map_repo"}


# ToolError now imported from echelon_sdk.exceptions at the top (WELD #4 cut): the type is pure,
# so it moved to the library and the tools<->tools_fileops cycle that the old import-ordering hack
# papered over is structurally gone — both tool modules import it from sdk.
# _BgJob extracted to bg_job.py (leaf atom — self-contained data class + kill logic).
# shell selection extracted to shell_select.py; destructive policy to tool_policy.py;
# path normalization/sandbox resolution to sandbox_path.py.


class ToolRegistry(_FileOpsMixin, _AttachMixin):
    def __init__(self, root: str | Path, allow_write: bool = True, allow_bash: bool = True,
                 shell: str | None = None, read_roots: list[str | Path] | None = None):
        # The sandbox. Every WRITE path MUST live under root. READS may also reach extra
        # read_roots — the agent's OWN artifact dirs (e.g. the run's tool-output offload folder),
        # which are always safe to read back (it produced them). This is what lets the context-slim
        # offload work: raw tool output is written to ~/.echelon/runs/<run>/outputs and the agent
        # reads its own handle back through read_file. Writes are NEVER widened — only reads.
        self.root = Path(root).resolve()
        self.read_roots = [Path(r).resolve() for r in (read_roots or [])]
        self.allow_write = allow_write
        self.allow_bash = allow_bash
        # Decide run_bash's shell ONCE, explicitly, so <env> can tell the agent the truth.
        # `shell` may be a path (e.g. a bash.exe) or None to auto-pick. POSIX shell preferred
        # when present (the agent's natural idiom), else the platform default (cmd.exe on Windows).
        self.shell_exec, self.shell_kind = self._pick_shell(shell)
        self.partner_resolver: Callable[[str, str], str] | None = None  # set by attach_partner
        self._read_paths: set[str] = set()   # GUARD: files read this session (must-read-before-edit)
        self._tools: dict[str, tuple[dict, Callable[..., str]]] = {}
        # Background-execution state (the Claude-way timeout-detach harness). A call that overruns
        # call_timeout detaches to a _BgJob; the active job is what STOP/kill_active terminates.
        self.call_timeout = config.get("call_timeout", 120.0)  # seconds before a tool call detaches to background
        # NO-DETACH tools (owner 2026-06-06): the partner-channel asks are HUMAN round-trips — they
        # are MEANT to block until the partner answers (the gate's whole purpose: pause and ask BEFORE
        # acting). The bg-harness's 25s timeout-detach is for HUNG WORK (headless Chrome), not for a
        # deliberate wait on judgment — detaching a consult lets the agent "keep working" past the gate
        # it just opened, defeating it. These run synchronously (still STOP-interruptible), never auto-
        # detach on the clock. The consult resolver/partner already has its OWN long timeout.
        self.no_detach: set[str] = {
            "consult", "ask_partner",   # human round-trips — must block on the partner's answer
            "reason",                   # one atomic PAID model call — detaching abandons it -> the
                                        #   agent re-asks -> double-spend. Wait for the one result.
            "spawn_subagents",          # a swarm of full sub-agent loops, DESIGNED long (own sub_ttl
                                        #   ~900s) — a 25s detach is nonsensical; the result is the point.
            "edit_file", "replace_in_file", "write_file",  # ATOMIC mutations — a half-done write must
                                        #   never be left running in the background; finish or fail.
        }
        # NOTE (owner 2026-06-06, the deeper design): this static set is the floor. The RIGHT shape is
        # a JUDGMENT, not a list — when ANY call overruns, the agent reasons "is this worth waiting
        # for?"; if yes it waits (or spawns a WATCHER to poll the task while it works on else), if no
        # it detaches. A faculty, not a bar. See [[bg-harness-no-detach-and-deliberate-wait]]. NOT built.
        self._bg: dict[int, _BgJob] = {}     # n -> job
        self._bg_seq = 0
        self._active: _BgJob | None = None    # the call running RIGHT NOW (what STOP kills first)
        # the agent's plan = a TODO list with state (owner: "pair with to do list"). Each item is
        # {"step": str, "status": pending|doing|done}. The loop surfaces it each step + measures
        # progress by what's marked done — a sharper "closer?" signal than vibes. Reclaims the
        # Claude-Code TodoWrite pattern: a plan is a checklist the agent checks off, its live spine.
        self._plan: list[dict] = []
        # Optional callback the loop sets: returns True when the partner wants the ACTIVE call killed
        # (context-aware STOP). Polled while a call is in flight so a kill lands mid-call, not only
        # between steps. None = no mid-call interruption (the call just runs to its timeout).
        self._stop_check: Callable[[], bool] | None = None
        # Destructive action interceptor policy (default: confirm all categories).
        # Configured via set_destructive_policy(). See _check_destructive.
        self._destructive_policy: set[str] = {"delete", "overwrite", "bulk", "process_kill"}
        self._accept_edits: bool = False   # auto/bypass mode -> True (acceptEdits); see set_accept_edits
        self._register_builtins()

    def set_stop_check(self, fn: Callable[[], bool] | None) -> None:
        """Wire the loop's 'partner wants this call killed' signal so execute() can poll it."""
        self._stop_check = fn

    @staticmethod
    def _pick_shell(shell: str | None) -> tuple[str | None, str]:
        """Delegate to shell_select.pick_shell (extracted leaf atom)."""
        return pick_shell(shell)

    # --- the guard: the one place that makes escape impossible ------------------
    @staticmethod
    def _normalize_path(path: str) -> str:
        """Delegate to sandbox_path.normalize_path (extracted leaf atom)."""
        return normalize_path(path)

    def _safe(self, path: str, *, for_write: bool = False) -> Path:
        """Delegate to sandbox_path.resolve_safe (extracted leaf atom)."""
        return resolve_safe(path, self.root, self.read_roots, for_write=for_write)

    # --- registration ----------------------------------------------------------
    def register(self, name: str, description: str, parameters: dict, fn: Callable[..., str]):
        self._tools[name] = (
            {"type": "function",
             "function": {"name": name, "description": description, "parameters": parameters}},
            fn,
        )

    def schemas(self) -> list[dict]:
        return [s for s, _ in self._tools.values()]

    def _invoke(self, name: str, fn, args: dict[str, Any]) -> str:
        """The raw call, exception-mapped to a string the loop can always feed back."""
        try:
            return fn(**args)
        except ToolError as e:
            return f"REFUSED: {e}"
        except TypeError as e:
            return f"ERROR: bad arguments for {name}: {e}"
        except Exception as e:  # noqa: BLE001 — the loop must always get a string back
            return f"ERROR: {type(e).__name__}: {e}"

    def execute(self, name: str, args: dict[str, Any]) -> str:
        """Run a tool call under the TIMEOUT-DETACH harness (the Claude way, owner 2026-06-05): the
        call runs in a worker thread; if it overruns call_timeout it DETACHES to a background _BgJob
        and we return a handle + partial output so the LOOP NEVER WEDGES on a hung call (the headless-
        Chrome freeze that motivated this). check_bg(n)/kill_bg(n) poll/kill it; STOP kills the
        ACTIVE call first (see kill_active). run_bash registers its subprocess on the job so a kill
        takes down the whole process GROUP; pure-Python tools that overrun are abandoned as daemons."""
        if name not in self._tools:
            return f"ERROR: unknown tool '{name}'"
        # CONVERGENCE HARD GATE: once the loop locks gathering (the agent ignored the synthesize
        # signal repeatedly while novelty stayed flat), refuse the gather tools — only consult/finish
        # remain. The felt-sufficiency steer becomes a command. Set by loop.py on persistent saturation.
        if getattr(self, "gather_locked", False) and name in _GATHER_LOCK_TOOLS:
            return (f"[gather LOCKED — you have enough evidence (the convergence signal fired repeatedly). "
                    f"{name} is refused. SYNTHESIZE now: consult your draft verdict, then finish() with "
                    "your ranked verdict from the evidence you already have.]")
        # DESTRUCTIVE ACTION INTERCEPTOR (pre-flight gate): evaluate the call against the
        # configured policy BEFORE execution. If it matches a destructive category, the partner
        # is asked to confirm; the call is blocked until approved. This runs synchronously (no
        # detach) because the partner's judgment IS the point — the agent must not wander past
        # the gate it just opened. See _check_destructive, set_destructive_policy.
        blocked = self._check_destructive(name, args)
        if blocked is not None:
            return blocked
        # finish must be instant + synchronous (it ends the loop) — no harness around it.
        if name == "finish":
            return self._invoke(name, self._tools[name][1], args)
        _, fn = self._tools[name]
        self._bg_seq += 1
        job = _BgJob(self._bg_seq, name, args)

        def _worker(_job=job):
            _job._reg = self   # so run_bash can attach its Popen to the active job
            r = self._invoke(name, fn, args)
            if not _job.killed:
                _job.result = r

        t = threading.Thread(target=_worker, daemon=True)
        job.thread = t
        self._active = job
        t.start()
        # Wait up to call_timeout, but POLL a stop signal every 0.5s so the partner can kill THIS
        # call mid-flight (context-aware STOP) without waiting out the whole timeout — and so a
        # genuinely hung call is interruptible, not just abandoned at the deadline.
        # NO-DETACH tools (consult/ask_partner) are human round-trips: they MUST block until answered,
        # so they get NO deadline — only STOP can end the wait. Otherwise the 25s clock would detach
        # the very gate the agent opened, and it would wander on past the partner's judgment.
        no_detach = name in self.no_detach
        deadline = float("inf") if no_detach else time.time() + self.call_timeout
        while t.is_alive() and time.time() < deadline:
            if self._stop_check is not None:
                try:
                    if self._stop_check():
                        job.kill()
                        break
                except Exception:
                    pass
            t.join(0.5)
        self._active = None

        if job.result is not None:        # finished in time — the normal path
            return job.result
        if job.killed:                    # killed by STOP/kill while we waited
            return f"[call {name} was killed by partner — it did not complete. Continue, ask_partner, or finish.]"
        # OVERRAN — detach to background, hand back a handle + whatever printed so far.
        self._bg[job.n] = job
        partial = ""
        if job.proc is not None:
            partial = self._drain_proc(job, peek=True)
        head = (partial[:600] + " ...[more]") if len(partial) > 600 else partial
        return (f"[bg#{job.n}: {name} still running after {self.call_timeout:.0f}s — DETACHED to "
                f"background so you can keep working. Partial output so far:]\n{head or '(none yet)'}\n"
                f"[poll it with check_bg({job.n}); kill it with kill_bg({job.n}). Continue with "
                f"another action in the meantime.]")

    def _drain_proc(self, job: _BgJob, *, peek: bool = False) -> str:
        """Read whatever a bg subprocess has produced so far (non-destructive peek possible)."""
        if job.proc is None:
            return ""
        if not hasattr(job, "_buf"):
            job._buf = ""
        try:
            if job.proc.poll() is not None:           # finished — drain the rest
                rest, _ = job.proc.communicate(timeout=2)
                job._buf += rest or ""
        except Exception:
            pass
        return job._buf

    def kill_active(self) -> str | None:
        """STOP, context-aware (owner): if a tool call is running RIGHT NOW (or a bg job is live),
        kill THAT call's process group and let the loop continue — do NOT end the agent. Returns a
        note if something was killed, else None (so the caller knows to fall back to agent-halt)."""
        target = self._active
        if target is None:
            # no active call — kill the most-recent live bg job if any
            live = [j for j in self._bg.values() if j.alive()]
            target = live[-1] if live else None
        if target is None:
            return None
        target.kill()
        return f"killed {target.name} (bg#{target.n})"

    # --- the curated builtins --------------------------------------------------
    def _register_builtins(self):
        self.register(
            "read_file",
            "Read a UTF-8 text file under the sandbox root. For a big file, page through it with "
            "offset (1-based start line) + limit (line count); reading to the end (or the whole "
            "file in one call) satisfies the read-before-edit guard so edit_file is then allowed.",
            {"type": "object",
             "properties": {"path": {"type": "string"},
                            "offset": {"type": "integer", "description": "1-based start line (optional)"},
                            "limit": {"type": "integer", "description": "max lines to read (optional)"}},
             "required": ["path"]},
            self._read_file,
        )
        self.register(
            "search_file",
            "Find a string/regex in a file and JUMP to it — returns each match's line number with "
            "a few lines of context (like grep). Use this to LOCATE a function/marker before "
            "editing instead of paging blindly. Reading a match's context counts toward the "
            "read-before-edit guard for those lines, so you can search -> read the window -> edit "
            "the exact spot. Pass context (lines around each hit, default 4) and regex (default true).",
            {"type": "object",
             "properties": {"path": {"type": "string"},
                            "query": {"type": "string", "description": "string or regex to find"},
                            "context": {"type": "integer", "description": "lines of context around each hit (default 4)"},
                            "regex": {"type": "boolean", "default": True}},
             "required": ["path", "query"]},
            self._search_file,
        )
        self.register(
            "list_files", "List files matching a glob (default '*') under a directory in the sandbox.",
            {"type": "object",
             "properties": {"directory": {"type": "string", "default": "."},
                            "pattern": {"type": "string", "default": "*"},
                            "recursive": {"type": "boolean", "default": False}},
             "required": []},
            self._list_files,
        )
        self.register(
            "map_repo",
            "UNDERSTAND A CODEBASE BY ITS STRUCTURE — read this BEFORE cat-ing files. Mechanically "
            "extracts every Python file's imports, classes, and function signatures (with risk markers "
            "like [try,async]) into ONE compact outline. Use this FIRST on any audit/explore task: read "
            "the structure, find the spots that matter (a router, an auth dep, a suspicious import), THEN "
            "read_file only those — do NOT brute-read whole files to learn a repo. `directory` scopes it "
            "to a subtree (e.g. 'module' or 'api_app') to drill in; omit for the whole root. "
            "signatures=false gives just per-file counts (a lighter map).",
            {"type": "object",
             "properties": {"directory": {"type": "string", "default": ".",
                                          "description": "subtree to map, relative to root (default whole repo)"},
                            "signatures": {"type": "boolean", "default": True,
                                           "description": "include function/class signatures (default true)"}},
             "required": []},
            self._map_repo,
        )
        self.register(
            "edit_file",
            "Make a SURGICAL edit to a file: replace an exact old_string with new_string, leaving "
            "the rest untouched. PREFER THIS over write_file for changing part of an existing file "
            "— do NOT rewrite a whole file to change a few lines. old_string must appear EXACTLY "
            "once (include enough surrounding context to be unique); the edit fails if it's missing "
            "or ambiguous. Set replace_all=true to replace every occurrence.",
            {"type": "object",
             "properties": {"path": {"type": "string"},
                            "old_string": {"type": "string", "description": "exact text to replace (unique)"},
                            "new_string": {"type": "string", "description": "text to replace it with"},
                            "replace_all": {"type": "boolean", "default": False}},
             "required": ["path", "old_string", "new_string"]},
            self._edit_file,
        )
        self.register(
            "replace_in_file",
            "Search-and-replace in a file: find a string/regex pattern and replace it. For 'change "
            "every X to Y' across a file — easier than edit_file when the change is a pattern, not "
            "one exact block. By default replaces ALL matches; set first=true for only the first. "
            "regex=true (default) enables patterns + backrefs (\\1) in the replacement. Read-gated "
            "like edit_file (read the file first).",
            {"type": "object",
             "properties": {"path": {"type": "string"},
                            "pattern": {"type": "string", "description": "string or regex to find"},
                            "replacement": {"type": "string", "description": "replacement text (\\1 backrefs if regex)"},
                            "regex": {"type": "boolean", "default": True},
                            "first": {"type": "boolean", "default": False}},
             "required": ["path", "pattern", "replacement"]},
            self._replace_in_file,
        )
        self.register(
            "write_file", "Write a WHOLE file under the sandbox root (creates parents). For a NEW "
            "file or a full rewrite. To change PART of an existing file, use edit_file instead. "
            "Refused if writes disabled.",
            {"type": "object",
             "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
             "required": ["path", "content"]},
            self._write_file,
        )
        self.register(
            "run_bash", "Run a shell command with cwd at the sandbox root. Refused if bash disabled. "
            "If it runs longer than the call timeout it DETACHES to a background job (you get a "
            "bg#N handle + partial output and keep working) — so a long-running or non-returning "
            "command (a server, a watch, headless Chrome) never freezes you. Poll it with "
            "check_bg(n), kill it with kill_bg(n).",
            {"type": "object",
             "properties": {"cmd": {"type": "string"}},
             "required": ["cmd"]},
            self._run_bash,
        )
        self.register(
            "check_bg", "Check a backgrounded tool call (a bg#N from a detached run_bash): is it "
            "still running, and what has it output so far? Use this to collect the result of a long "
            "command you let run in the background.",
            {"type": "object",
             "properties": {"n": {"type": "integer", "description": "the bg job number (from bg#N)"}},
             "required": ["n"]},
            self._check_bg,
        )
        self.register(
            "kill_bg", "Kill a backgrounded tool call (bg#N) — terminates its whole process group "
            "(so a child like headless Chrome dies too). Use when a detached command is hung or no "
            "longer needed.",
            {"type": "object",
             "properties": {"n": {"type": "integer", "description": "the bg job number to kill"}},
             "required": ["n"]},
            self._kill_bg,
        )
        self.register(
            "plan",
            "Your plan = a TODO LIST you check off — your spine for the task. It's shown back to you "
            "each step and progress is measured by what you mark done. Two ways to use it: (1) set "
            "the whole list with `steps` (an ordered list of short TODOs — all start pending); "
            "(2) mark progress with `done` (index of a step you just finished) and/or `doing` (index "
            "of the step you're now on). REVISE freely — replanning is steering, not failure. Plan "
            "EARLY (before diving in), mark steps done as you go, and re-plan if you're circling.",
            {"type": "object",
             "properties": {
                 "steps": {"type": "array", "items": {"type": "string"},
                           "description": "set/replace the whole TODO list (ordered short steps)"},
                 "done": {"type": "integer", "description": "1-based index of a step to mark DONE"},
                 "doing": {"type": "integer", "description": "1-based index of the step you're now ON"}},
             "required": []},
            self._plan_tool,
        )
        self.register(
            "finish", "Report the final answer and end the loop.",
            {"type": "object",
             "properties": {"answer": {"type": "string"}},
             "required": ["answer"]},
            self._finish,
        )

    # --- implementations -------------------------------------------------------
    # --- FILE-OPS (read/search/list/map/write/edit/replace) ---------------------
    # Moved to _FileOpsMixin (tools_fileops.py, 2026-06-07) — the second mixin cut after
    # _AttachMixin. They live on the class via inheritance; the read-before-edit guard and
    # the smart head+tail default move with them. See tools_fileops.py.

    # _re class attribute: tools_fileops.py uses self._re for regex ops in _search_file /
    # _replace_in_file (it accesses it as a class attribute via inheritance). Keep the binding
    # here so tools_fileops.py needs no change (backward-compatible delegation seam).
    import re as _re   # noqa: E402 — class-body import intentional (class attribute, not a module import)

    # --- DESTRUCTIVE ACTION INTERCEPTOR (pre-flight confirmation gate) ----------
    # Patterns + tool set extracted to tool_policy.py (leaf atom). Class-level names kept as
    # aliases so any code that accesses ToolRegistry._DESTRUCTIVE/_DESTRUCTIVE_TOOLS still works.
    # "confirm_on" is a set of categories: "delete", "overwrite", "bulk", "process_kill".
    # The interceptor evaluates every destructive tool call against this policy BEFORE
    # execution, pausing the agent loop and surfacing the proposed action to the partner.

    def set_destructive_policy(self, confirm_on: set[str] | None = None) -> None:
        """Configure which destructive categories require partner confirmation.
        Categories: 'delete', 'overwrite', 'bulk', 'process_kill'.
        Pass None (or omit) to reset to defaults (all categories confirmed)."""
        # An empty set (or None) RESETS to the full default policy — the safe default: you cannot
        # accidentally disable the destructive gate with an empty set. To narrow, pass a non-empty
        # subset; to confirm a single category, pass {that}. (Per the comprehensive gate test suite.)
        self._destructive_policy = set(confirm_on) if confirm_on else {"delete", "overwrite", "bulk", "process_kill"}

    def set_accept_edits(self, accept: bool) -> None:
        """ACCEPT-EDITS (anomaly #3, owner 2026-06-07 — mirror THIS Claude Code harness): in
        Claude Code, `acceptEdits` mode lets edits/overwrites proceed WITHOUT asking, while
        genuinely dangerous ops (delete/bulk/process_kill) stay gated. ECHELON's `auto` mode
        claimed "run freely" but the destructive interceptor fired on every overwrite regardless
        of mode (the gate in tools.py never saw current_mode in loop.py). This flag is the seam:
        the loop sets it from the mode each step; when True, _check_destructive treats the
        'overwrite' category as accepted (skipped), nothing else. Permission becomes config the
        loop reads — not an external file-watcher faking approvals. See loop.py current_mode."""
        self._accept_edits = bool(accept)

    def _classify_destructive(self, name: str, args: dict) -> tuple[str | None, str]:
        """Delegate to tool_policy.classify_destructive (extracted leaf atom)."""
        return classify_destructive(name, args, self.root)

    def _target_exists(self, path: str) -> bool:
        """Delegate to tool_policy.target_exists (extracted leaf atom)."""
        return target_exists(path, self.root)

    def _check_destructive(self, name: str, args: dict) -> str | None:
        """Pre-flight check: if the tool call matches the destructive policy, ask the partner
        for confirmation. Returns None if allowed (no match or partner approved), or a refusal
        string if denied."""
        policy = getattr(self, "_destructive_policy", {"delete", "overwrite", "bulk", "process_kill"})
        # ACCEPT-EDITS (auto mode = Claude Code acceptEdits): overwrites proceed without asking;
        # delete/bulk/process_kill STAY gated (genuinely dangerous ops are never auto-accepted).
        if getattr(self, "_accept_edits", False):
            policy = policy - {"overwrite"}
        if name not in _DESTRUCTIVE_TOOLS:
            return None
        cat, desc = self._classify_destructive(name, args)
        if cat is None or cat not in policy:
            return None
        if self.partner_resolver is None:
            return (f"[DESTRUCTIVE GATE — no partner reachable] {name} matched policy category "
                    f"'{cat}' but no partner is attached to confirm. Action blocked. "
                    f"To proceed, attach a partner or narrow the action.\n  {desc}")
        verdict = self.partner_resolver(
            f"DESTRUCTIVE ACTION — may I proceed? This matched policy category '{cat}'.\n\n"
            f"  Tool: {name}\n  Args: {json.dumps(args, indent=2)[:600]}\n\n"
            f"Reply yes to allow, or no / guidance to block.",
            f"destructive gate: {name} ({cat})")
        v = (verdict or "").strip().lower()
        if v.startswith(("y", "ok", "approve", "go", "allow", "proceed", "run")):
            return None  # approved
        return f"[DESTRUCTIVE GATE — denied by partner] {verdict.strip() or 'denied'}. Action not executed.\n  {desc}"

    def _run_bash(self, cmd: str) -> str:
        if not self.allow_bash:
            raise ToolError("bash is disabled for this run")
        # NOTE: destructive command gating is now handled by the pre-flight interceptor in
        # execute() via _check_destructive (see above). The inner guard was removed to avoid
        # double-asking the partner — the execute-level gate fires first and either blocks or
        # approves; by the time we reach here, the call is cleared.
        # Spawn in a NEW PROCESS GROUP so the WHOLE tree is killable (the headless-Chrome wedge fix:
        # a child holding the pipe can't keep the group alive past a kill). Popen (not run) so the
        # timeout-detach harness in execute() can detach + later kill this exact process group via
        # the active _BgJob. Use a KNOWN shell so env-sense never diverges from real behavior.
        if os.name == "nt":
            popen_kw = dict(creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)
        else:
            popen_kw = dict(start_new_session=True)   # own process group on POSIX
        if self.shell_exec and self.shell_kind == "posix":
            argv, shell = [self.shell_exec, "-c", cmd], False
        elif self.shell_exec:
            argv, shell = cmd, True
            popen_kw["executable"] = self.shell_exec
        else:
            argv, shell = cmd, True
        proc = subprocess.Popen(
            argv, shell=shell, cwd=str(self.root),
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, **popen_kw)
        # Attach to the active job so STOP/kill_bg can terminate THIS group; the harness owns timing.
        job = getattr(self, "_active", None)
        if job is not None:
            job.proc = proc
        out, _ = proc.communicate()   # blocks in the WORKER THREAD; the harness times the thread
        out = (out or "").strip() or "(no output)"
        return out if len(out) <= 10000 else out[:10000] + "\n...[truncated]"

    def _plan_tool(self, steps: list | None = None, done: int | None = None,
                   doing: int | None = None) -> str:
        """The plan = a TODO list with per-step state. `steps` replaces the whole list (all pending);
        `done`/`doing` (1-based) mark progress in place. Cheap, no model call — the loop reads
        self._plan to surface it each step + measure progress by what's marked done."""
        if steps is not None:
            self._plan = [{"step": str(s), "status": "pending"}
                          for s in steps if str(s).strip()][:20]
        for idx, st in ((done, "done"), (doing, "doing")):
            if idx is not None and 1 <= int(idx) <= len(self._plan):
                self._plan[int(idx) - 1]["status"] = st
        if not self._plan:
            return "plan cleared (empty)."
        return "TODO:\n" + self._render_plan()

    _MARK = {"done": "[x]", "doing": "[~]", "pending": "[ ]"}

    def _render_plan(self) -> str:
        """The TODO as checkable lines — used in the tool result AND surfaced by the loop each step."""
        return "\n".join(f"  {self._MARK.get(p['status'],'[ ]')} {i+1}. {p['step']}"
                         for i, p in enumerate(self._plan))

    def _finish(self, answer: str) -> str:
        # The loop watches for this tool by name to terminate; return value is the answer.
        return answer

    def _check_bg(self, n: int) -> str:
        job = self._bg.get(int(n))
        if job is None:
            return f"no background job bg#{n}"
        if job.result is not None:
            out = job.result
            return f"[bg#{n} {job.name} FINISHED]\n{out[:4000]}" + (" ...[truncated]" if len(out) > 4000 else "")
        if job.killed:
            return f"[bg#{n} {job.name} was killed]"
        partial = self._drain_proc(job)
        elapsed = time.time() - job.started
        return (f"[bg#{n} {job.name} still running ({elapsed:.0f}s). Output so far:]\n"
                f"{partial[:3000] or '(none yet)'}")

    def _kill_bg(self, n: int) -> str:
        job = self._bg.get(int(n))
        if job is None:
            return f"no background job bg#{n}"
        return ("killed " if job.kill() else "could not kill ") + f"bg#{n} ({job.name})"

    # --- the recall gate (rediscovery) -----------------------------------------
