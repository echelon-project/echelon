"""add_steering.py — the echelon-Agent-Driven-Development steering controller.

This is the OS↔gemma steering surface for the ADD pipeline: the loop where
OS (opus) + gemma (T1) keep DISPATCHING in parallel while smollm (T2) agents
generate code as Intents into the append-only IntentPool (the ledger), each
task passing the two-stage ADD review gate (spec-compliance FIRST, then
code-quality), and compile() — the single, AST-safe writer — emitting only
review-APPROVED intents to disk.

WHY this file exists (the integration of echelon-agent-Driven-Development):
  - Fresh agent per task, CONSTRUCTED context: the implementer / spec-reviewer /
    quality-reviewer are composed live HERE as cards (not inherited session, not
    static .md prompts). OS curates exactly what each needs — this is the
    pointer-array / agent-as-card principle made operational.
  - The two-stage gate rides INSIDE the async tiered loop: an intent is not
    "done" when generated, it is done when spec-review ✅ THEN quality-review ✅.
  - compile() is the only writer → the model→file escape-mangle (proven 3×)
    cannot happen: the path is model→declare→IntentPool→compile→file.
  - Status is APPEND-ONLY (a status-event referencing the intent id, newest
    wins at read) — never a mutated field. Same honesty law as the soul.
  - observe-only + stop-on-exception: OS/gemma watch the stream; silence =
    proceed; the only interrupt is STOP.

ENDPOINT: http://a LAN LM Studio endpoint  (LM Studio, Bearer auth via load_lmstudio_key).
  T1 architect/reviewer = gemma-4 ; T2 implementer = smollm3 (per the bake-off floor).

STATE OF THE SUBSTRATE (honest): IntentPool today is append-only with .post/.read
(no status-event table yet). This controller is written against the TARGET API
(the six ADD seam-tasks) and degrades gracefully: if the status-event surface is
absent it falls back to a local in-process verdict ledger so the loop still runs
and can be observed — but the REAL build wires verdicts into IntentPool (task 3).
The four ADD statuses are the verdict vocabulary, defined here as the source of
truth until task 1 lands StatusEnum in compiler/intent.py.
"""

from __future__ import annotations

import json
import re
import time
import urllib.request
import urllib.error
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Optional

from echelon_sdk.keys import load_lmstudio_key
from echelon_sdk.compiler.intent import Intent
from echelon_sdk.compiler.intent_pool import IntentPool
from echelon_sdk.compiler.declare import declare_impl


# --------------------------------------------------------------------------- #
# Endpoint + models (the proven local floor)                                  #
# --------------------------------------------------------------------------- #

from echelon_sdk.config import floor_endpoint
ENDPOINT = floor_endpoint("/v1/chat/completions")  # config 'floor.host' / LM_ENDPOINT env, not a hardcoded LAN IP
T1_ARCHITECT = "gemma-4-e4b-uncensored-hauhaucs-aggressive"   # frames / reviews
T2_IMPLEMENTER = "smollm3-3b-gabliterated-i1"                  # generates intents
# The Cloudflare-1010 trap: a non-browser UA gets banned. Always send a browser UA.
_UA = "Mozilla/5.0"


# --------------------------------------------------------------------------- #
# The four ADD implementer statuses (the verdict vocabulary)                  #
# --------------------------------------------------------------------------- #

class Status(str, Enum):
    """The ADD implementer statuses + the review verdicts. Append-only events;
    newest event for an intent id wins at read. NEVER a mutable field."""
    DONE = "DONE"
    DONE_WITH_CONCERNS = "DONE_WITH_CONCERNS"
    NEEDS_CONTEXT = "NEEDS_CONTEXT"
    BLOCKED = "BLOCKED"
    SPEC_APPROVED = "SPEC_APPROVED"        # passed stage 1
    QUALITY_APPROVED = "QUALITY_APPROVED"  # passed stage 2 -> eligible for compile()
    REJECTED = "REJECTED"                  # a reviewer found issues -> fix-loop


# A review gate is two ordered stages: spec compliance FIRST, then code quality.
REVIEW_STAGES = ("spec", "quality")


@dataclass
class StatusEvent:
    """An appended status-event. The local fallback ledger when IntentPool has
    no status table yet (task 3 moves this into the pool)."""
    intent_id: str
    status: Status
    stage: Optional[str] = None      # "spec" | "quality" | None (implementer status)
    note: str = ""
    at: float = field(default_factory=time.time)


# --------------------------------------------------------------------------- #
# The LLM call (text-action: the model dumps the body; that dump IS the action)#
# --------------------------------------------------------------------------- #

# Cloud escalation tiers (ADD's BLOCKED protocol: escalate when the local floor
# can't finish). Same OpenAI wire, different endpoint + key — routed by model id.
DS_IMPLEMENTER = "deepseek-chat"

# BURST-T1: an ephemeral GPU (MI300X serving Qwen3-32B) reached as a stateless
# /chat ENDPOINT over the eos reverse-tunnel (the Copilot-bridge pattern). The
# substrate stays 100% local; the VM only transforms text. _chat enqueues a
# 'chat' command to the eos_host control server (localhost:8888) and polls the
# result — no inbound route to the pod needed, survives kernel recycles.
BURST_T1 = "qwen3-32b-burst"
_EOS_HOST = "http://127.0.0.1:8888"
_THINK_RE = re.compile(r"<think>.*?</think>\s*", re.DOTALL)


def _resolve_endpoint(model: str) -> tuple[str, str]:
    """Route a model id to its (endpoint, bearer-key). Local floor by default;
    cloud tiers (deepseek) escalated for what the local models mangle."""
    if model.startswith("deepseek"):
        from echelon_sdk.keys import load_deepseek_key
        return "https://api.deepseek.com/v1/chat/completions", load_deepseek_key()
    return ENDPOINT, load_lmstudio_key()


def _chat_via_eos(model: str, messages: list, max_tokens: int, temperature: float,
                  timeout: float) -> str:
    """Reach the burst-T1 GPU through the eos channel: POST a 'chat' command to the
    control server, poll /result. The VM agent curls its localhost vllm and returns
    the completion. Strips the <think> block (Qwen3 is a reasoning model)."""
    body = {"model": model, "messages": messages,
            "max_tokens": max_tokens, "temperature": temperature}
    enq = urllib.request.Request(f"{_EOS_HOST}/cmd",
        data=json.dumps({"kind": "chat", "body": body, "timeout": timeout}).encode(),
        headers={"Content-Type": "application/json"})
    cid = json.loads(urllib.request.urlopen(enq, timeout=15).read())["id"]
    deadline = time.time() + timeout + 30
    while time.time() < deadline:
        time.sleep(2)
        r = json.loads(urllib.request.urlopen(f"{_EOS_HOST}/result/{cid}", timeout=10).read())
        if not r.get("pending"):
            if not r.get("ok"):
                raise RuntimeError(f"burst chat failed: {r.get('stderr')}")
            return _THINK_RE.sub("", r.get("content", "")).strip()
    raise TimeoutError(f"burst chat {cid} timed out after {timeout}s")


def _chat(model: str, system: str, user: str, *, max_tokens: int = 1600,
          temperature: float = 0.4, timeout: float = 120.0) -> str:
    """One OpenAI-shaped /chat/completions call — delegates to the floor_chat primitive
    (the single source of truth for floor transport). Routes by model id to the local
    floor (gemma/smollm), a cloud tier (deepseek), or the BURST-T1 GPU over the eos
    channel, and AUTO-FAILS-OVER a downed local floor to deepseek-chat (see floor_chat).

    The whole ADD pipeline (implement / review / audit / consult_partner) calls this, so
    routing the transport through one primitive means the failover covers all of it. The
    local module-level _resolve_endpoint/_chat_via_eos/BURST_T1 constants are kept for the
    --ping smoke and back-compat, but the live path is the floor_chat primitive."""
    from echelon_engine.atoms.providers.floor_chat import _chat as _floor_chat
    return _floor_chat(model, system, user,
                       max_tokens=max_tokens, temperature=temperature, timeout=timeout)


# --------------------------------------------------------------------------- #
# Card composition — fresh agent per task, CONSTRUCTED context (ADD core)     #
# --------------------------------------------------------------------------- #
# Each card is composed live from the task + pointer-context. No inherited
# history, no static .md prompt files — the three ADD prompt templates
# (implementer / spec-reviewer / code-quality-reviewer) live HERE as cards.

def compose_implementer_card(task: str, context: str) -> str:
    """The implementer card (T2/smollm). Generates a file body as a text-action;
    it does NOT write the file — it declares an Intent into the pool."""
    return (
        "You are a fresh ECHELON implementer agent with no prior context. You "
        "implement EXACTLY the task below — no more, no less (over-building fails "
        "spec review). You output ONLY the complete file body for the target. You "
        "do NOT write to disk; your output is declared as an Intent and the "
        "compiler writes it (this is why escapes/f-strings are safe — emit them "
        "literally, do not pre-escape). If you cannot proceed, reply with a single "
        "line: STATUS: NEEDS_CONTEXT <what you need>  or  STATUS: BLOCKED <why>.\n\n"
        f"CONSTRUCTED CONTEXT (everything you need, curated):\n{context}\n\n"
        f"TASK:\n{task}"
    )


def compose_spec_reviewer_card(task: str, body: str) -> str:
    """Stage 1: spec-compliance reviewer (gemma). Checks the body does exactly
    what the spec says — nothing missing, nothing extra."""
    return (
        "You are a fresh ECHELON spec-compliance reviewer. You do not judge code "
        "quality — ONLY whether the implementation matches the spec: nothing "
        "MISSING, nothing EXTRA. Be strict; 'close enough' is a failure. Reply on "
        "the FIRST line with exactly: VERDICT: PASS  or  VERDICT: FAIL, then a "
        "terse reason. If FAIL, list each gap as one line.\n\n"
        f"SPEC (the task):\n{task}\n\nIMPLEMENTATION:\n{body}"
    )


def compose_quality_reviewer_card(task: str, body: str) -> str:
    """Stage 2: code-quality reviewer (gemma). Runs ONLY after spec PASS."""
    return (
        "You are a fresh ECHELON code-quality reviewer. Spec compliance is already "
        "confirmed — judge ONLY quality: correctness, clarity, no magic numbers, "
        "guards, error handling, tests where the spec implies them. Reply on the "
        "FIRST line with exactly: VERDICT: PASS  or  VERDICT: FAIL, then terse "
        "issues (each one line, severity-tagged).\n\n"
        f"TASK (for reference):\n{task}\n\nIMPLEMENTATION:\n{body}"
    )


# --------------------------------------------------------------------------- #
# Verdict ledger — append-only, newest-wins-at-read (task 3 folds into pool)  #
# --------------------------------------------------------------------------- #

class VerdictLedger:
    """In-process append-only status ledger. The HONEST fallback until the
    status-event surface lands in IntentPool (ADD task 3). Same law: append,
    resolve latest at read — never mutate."""

    def __init__(self) -> None:
        self._events: list[StatusEvent] = []

    def post(self, ev: StatusEvent) -> None:
        self._events.append(ev)

    def latest(self, intent_id: str) -> Optional[StatusEvent]:
        for ev in reversed(self._events):
            if ev.intent_id == intent_id:
                return ev
        return None

    def approved_ids(self) -> list[str]:
        """Intent ids whose latest status is QUALITY_APPROVED (eligible for
        compile()). This is what task B's get_ready_intents() will enforce."""
        seen: dict[str, StatusEvent] = {}
        for ev in self._events:
            seen[ev.intent_id] = ev
        return [iid for iid, ev in seen.items()
                if ev.status is Status.QUALITY_APPROVED]


# --------------------------------------------------------------------------- #
# The MECHANICAL pre-gate (deterministic truth before the soft LLM review)    #
# --------------------------------------------------------------------------- #
# A small model rubber-stamps broken code (proven: it passed a body that used
# `from datetime import time`, dropped a required member, wrapped itself in
# markdown fences). So correctness must be gated by MECHANISM first, not by
# asking a model nicely — the same law as honesty-by-mechanism in the bank.
# This gate runs BEFORE the LLM reviewers; a failure is an immediate REJECTED
# with a concrete reason fed back to the implementer's fix-loop.

import ast as _ast
import re as _re

_FENCE_RE = _re.compile(r"^\s*```[a-zA-Z0-9_+-]*\s*\n(.*?)\n\s*```\s*$", _re.DOTALL)


def strip_code_fences(body: str) -> str:
    """Strip a single surrounding markdown ```lang ... ``` fence and any
    leading/trailing chat prose. Models emit fences + commentary; the compiler
    needs clean source. If no fence is found, return the body unchanged."""
    m = _FENCE_RE.match(body.strip())
    if m:
        return m.group(1).strip() + "\n"
    # no full-body fence: drop any stray fence lines + obvious trailing prose
    lines = [ln for ln in body.splitlines() if not ln.strip().startswith("```")]
    return "\n".join(lines).strip() + "\n"


# --- JS validation: node --check is the JS analogue of compile() -------------
# The ADD pipeline was Python-shaped; for the Mol JS repos the mechanical gate
# was a no-op (is_python=False -> skip). This runs `node --check` so a broken JS
# body is REJECTED. Fragments (a bare class-method or express-route block) are not
# whole modules, so they are wrapped in a minimal harness before the check; the
# wrap is heuristic and only used when the raw body fails to parse standalone.

def _node_check(body: str, *, required_symbols: Optional[list[str]] = None) -> tuple[bool, str]:
    """Validate a JS body with `node --check`. Tries the body as-is first; on a
    parse error that looks like a fragment, retries wrapped (method -> class,
    route -> function). For an HTML body, validates only the JS inside <script>
    blocks (markup has no cheap CLI validator). Returns (passed, reason)."""
    import tempfile as _tf, subprocess as _sp, os as _o, re as _re2

    # HTML path: don't node-check the markup; extract <script> JS and check that.
    _stripped = body.lstrip()
    if _stripped.startswith("<") or "<!doctype html" in _stripped[:200].lower():
        scripts = _re2.findall(r"<script\b[^>]*>(.*?)</script>", body, _re2.DOTALL | _re2.IGNORECASE)
        # only inline module/script blocks with real code (skip empty / src-only)
        code = "\n;\n".join(s for s in scripts if s.strip())
        if not code.strip():
            return True, "html: no inline script to check"
        # reuse the JS check on the extracted code (module syntax allowed)
        return _node_check(code, required_symbols=required_symbols)

    def _check(src: str) -> tuple[bool, str]:
        fd, path = _tf.mkstemp(suffix=".mjs"); _o.close(fd)
        try:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(src)
            p = _sp.run(["node", "--check", path], capture_output=True, text=True, timeout=30)
            return (p.returncode == 0, (p.stderr or p.stdout).strip()[-400:])
        except Exception as e:  # noqa: BLE001
            return False, f"node --check error: {e}"
        finally:
            try: _o.remove(path)
            except OSError: pass

    # required symbols (regex presence — JS has no cheap AST here)
    if required_symbols:
        missing = [s for s in required_symbols
                   if not _re2.search(r"\b" + _re2.escape(s) + r"\b", body)]
        if missing:
            return False, f"missing required symbols: {', '.join(missing)}"

    ok, reason = _check(body)
    if ok:
        return True, "node --check passed"
    # fragment retries: a class method -> wrap in a class; a bare statement block ->
    # wrap in an async function. Use whichever makes it parse.
    looks_method = bool(__import__("re").match(r"^\s*(async\s+)?[A-Za-z_$][\w$]*\s*\(", body))
    for wrap in ([f"class _W {{\n{body}\n}}"] if looks_method else []) + \
                [f"async function _w() {{\n{body}\n}}", f"const _w = () => {{\n{body}\n}};"]:
        wok, _ = _check(wrap)
        if wok:
            return True, "node --check passed (as fragment)"
    return False, f"node --check failed: {reason}"


def _lang_of(target_path: str) -> str:
    """Classify a target into a gate lane by extension: 'python' | 'js' | 'prose'.
    'prose' covers data/doc/script bodies (yaml/md/sh/json/txt/css) that have no
    AST the python/js syntax engines understand — the gate would FALSELY reject
    valid YAML/Markdown/bash by running node --check on it (proven: the core-9
    sweep MECH_FAIL'd all 9 because non-.py routed to node --check)."""
    p = target_path.lower()
    if p.endswith(".py"):
        return "python"
    if p.endswith((".js", ".mjs", ".cjs", ".ts", ".html")):
        return "js"
    return "prose"  # .yaml/.yml/.md/.sh/.json/.txt/.css/...


def _validate_prose(cleaned: str, target_path: str) -> tuple[bool, str]:
    """Real validators for the prose lane, by extension. Substring presence is
    NOT enough — proven: smollm emitted MALFORMED YAML and a TRUNCATED bash script
    that both passed a substring-only gate. .yaml/.yml -> yaml.safe_load; .sh ->
    `bash -n`. Other prose types (md/txt) have no parser -> presence-only."""
    p = (target_path or "").lower()
    if p.endswith((".yaml", ".yml")):
        try:
            import yaml
            yaml.safe_load(cleaned)
            return True, "yaml parse ok"
        except ImportError:
            return True, "yaml parse skipped (pyyaml absent)"
        except Exception as e:
            return False, f"YAML parse fail: {str(e).splitlines()[0]}"
    if p.endswith(".sh"):
        import shutil, subprocess, tempfile, os
        bash = shutil.which("bash")
        if not bash:
            return True, "bash -n skipped (no bash)"
        fd, fn = tempfile.mkstemp(suffix=".sh")
        try:
            os.write(fd, cleaned.encode("utf-8")); os.close(fd)
            r = subprocess.run([bash, "-n", fn], capture_output=True, text=True, timeout=15)
            if r.returncode != 0:
                return False, f"bash -n fail: {r.stderr.strip().splitlines()[-1] if r.stderr.strip() else 'syntax error'}"
            return True, "bash -n ok"
        finally:
            try: os.unlink(fn)
            except OSError: pass
    if p.endswith(".json"):
        try:
            json.loads(cleaned); return True, "json parse ok"
        except Exception as e:
            return False, f"JSON parse fail: {e}"
    if p.endswith(".css"):
        # A CSS body must be RULES, not prose mentioning CSS. Proven escape
        # (2026-06-09): smollm echoed the task text ("apply white-space:nowrap to
        # .chat-row__name ...") which CONTAINED the required selectors as words, so
        # the presence-only gate passed and 580 real lines were overwritten with
        # prose. Require actual rule blocks; require each selector-shaped required
        # string to appear as a RULE HEAD (followed by '{'), not a bare substring.
        import re as _re_css
        if "{" not in cleaned or "}" not in cleaned:
            return False, "CSS body has no rule blocks ({...}) — looks like prose, not CSS"
        # at least one real `selector { ... : ... ; }` shape
        if not _re_css.search(r"[^{}]+\{[^}]*:[^}]*\}", cleaned, _re_css.DOTALL):
            return False, "CSS body has no valid `selector { prop: value }` rule"
        return True, "css rule-shape ok"
    return True, "no parser for type (presence-only)"


def mechanical_gate(body: str, *, required_symbols: Optional[list[str]] = None,
                    is_python: bool = True, lang: Optional[str] = None,
                    target_path: Optional[str] = None) -> tuple[bool, str, str]:
    """Deterministic pre-gate. Returns (passed, reason, cleaned_body).

    Lane is `lang` ('python'|'js'|'prose') if given, else derived from is_python
    for back-compat (True->python, False->js). The 'prose' lane is for data/doc/
    script bodies (yaml/md/sh/...): it strips fences, checks required_symbols
    PRESENCE, AND runs a real validator by extension (yaml.safe_load / bash -n /
    json.loads) when `target_path` is given — substring-only was proven too weak.

    Checks, in order:
      1. strip markdown fences / chat prose to get clean source
      2. (python) compile() / (js) node --check / (prose) presence + real validator
      3. every spec-named symbol in required_symbols is actually present/DEFINED
    A failure reason is concrete enough for the implementer to fix."""
    cleaned = strip_code_fences(body)
    if lang is None:
        lang = "python" if is_python else "js"

    # UNIVERSAL prose-leak guard (every lane): a body whose first non-empty line is
    # the implementer's own status template ("STATUS: NEEDS_CONTEXT/BLOCKED ...") is
    # a GENERATION FAILURE leaking into the body, never valid code/content. Proven
    # escape 2026-06-09: this template text passed the CSS presence gate and was
    # written over a real stylesheet. Reject it mechanically, in any lane.
    _first = next((ln for ln in cleaned.splitlines() if ln.strip()), "")
    if _first.strip().upper().startswith(("STATUS: NEEDS_CONTEXT", "STATUS: BLOCKED")):
        return False, "body is a STATUS template leak (generation failed, not real content)", cleaned

    if lang == "prose":
        if not cleaned.strip():
            return False, "empty body", cleaned
        if required_symbols:
            # For CSS, a selector must appear as a RULE HEAD (followed by '{'), not a
            # bare substring (prose can mention a selector without defining it).
            is_css = bool(target_path) and target_path.lower().endswith(".css")
            if is_css:
                import re as _re_sym
                missing = [s for s in required_symbols
                           if not _re_sym.search(re.escape(s) + r"\s*(?:,[^{}]*)?\{", cleaned)]
                if missing:
                    return False, f"required selectors not defined as rules: {', '.join(missing)}", cleaned
            else:
                missing = [s for s in required_symbols if s not in cleaned]
                if missing:
                    return False, f"missing required strings: {', '.join(missing)}", cleaned
        if target_path:
            ok_v, reason_v = _validate_prose(cleaned, target_path)
            if not ok_v:
                return False, reason_v, cleaned
            return True, f"mechanical gate passed (prose: {reason_v})", cleaned
        return True, "mechanical gate passed (prose)", cleaned

    if lang == "js":
        # JS path: validate with `node --check` (the JS analogue of compile()) so a
        # broken body cannot pass the gate just because it isn't Python. Without this
        # the gate was a no-op for the Mol JS repos (proven gap). A fragment that is
        # not a whole module (a bare method body / route block) won't parse standalone,
        # so we wrap obvious fragments in a minimal harness before checking.
        ok_js, reason_js = _node_check(cleaned, required_symbols=required_symbols)
        return ok_js, reason_js, cleaned

    # compile(), NOT just ast.parse(): ast.parse accepts scope-invalid code like
    # `return`/`yield`/`await` outside a function and `break`/`continue` outside a
    # loop (a bad-dedent splice produces exactly this). Only compile() enforces
    # those rules — the gate must, or a broken body passes (proven: a dedented
    # `return` slipped through ast.parse and a broken file briefly landed).
    try:
        compile(cleaned, "<mechanical_gate>", "exec")
        tree = _ast.parse(cleaned)  # tree for the symbol-presence walk below
    except SyntaxError as e:
        return False, f"SyntaxError: {e.msg} (line {e.lineno})", cleaned

    if required_symbols:
        defined: set[str] = set()
        for node in _ast.walk(tree):
            if isinstance(node, (_ast.ClassDef, _ast.FunctionDef, _ast.AsyncFunctionDef)):
                defined.add(node.name)
            elif isinstance(node, _ast.Assign):
                for t in node.targets:
                    if isinstance(t, _ast.Name):
                        defined.add(t.id)
        missing = [s for s in required_symbols if s not in defined]
        if missing:
            return False, f"missing required symbols: {', '.join(missing)}", cleaned

    return True, "mechanical gate passed", cleaned


# --------------------------------------------------------------------------- #
# The TEST gate — behavioral truth the LLM review cannot provide              #
# --------------------------------------------------------------------------- #
# For an EDIT to an existing file (the seam chain regenerates whole files), the
# mechanical gate catches structure but NOT behavioral regression — gemma could
# keep a symbol's name and break its logic. The existing tests are the only
# behavioral truth. So: stage the candidate body at the real path, run the
# associated pytest, ALWAYS restore the original. A failure REJECTS and feeds
# the pytest output back to the fix-loop. Correctness by MECHANISM, not by trust.

import os as _os
import subprocess as _subprocess
import tempfile as _tempfile


def _guess_test_file(target_path: str) -> Optional[str]:
    """Find the test file for a module: sibling test_<name>.py or tests/test_<name>.py."""
    import os.path as _p
    d, fn = _p.split(target_path)
    name = fn[:-3] if fn.endswith(".py") else fn
    candidates = [
        _p.join(d, f"test_{name}.py"),
        _p.join("tests", f"test_{name}.py"),
    ]
    for c in candidates:
        if _os.path.exists(c):
            return c
    return None


# --------------------------------------------------------------------------- #
# PATH gate — the filesystem is the authority, NOT the model                  #
# --------------------------------------------------------------------------- #
# A planner (even a 32B) hallucinates target paths (said static/css/app.css when
# the real repo nests it at api_app/static/app.css). So resolve every task path
# MECHANICALLY against the real tree by basename, correct the ledger, and report
# the corrections as a WARNING the planner is re-fed (gates-by-mechanism for paths).

def resolve_paths(tasks: list[dict], repo_root: str = ".") -> tuple[list[dict], list[str]]:
    """For each task, verify target_path exists under repo_root; if not, try to
    resolve it by BASENAME (unique match wins). Returns (corrected_tasks, warnings).
    A NEW-file task (new_or_edit == 'new') whose path is absent is left as-is (it's
    meant to not exist) but its parent dir is checked. Warnings name every drift."""
    # index the repo by basename once
    by_base: dict[str, list[str]] = {}
    for dp, _dirs, files in _os.walk(repo_root):
        # skip noise
        if any(seg in dp for seg in ("__pycache__", "node_modules", "/.git", "\\.git",
                                     "_back", "_archive", ".flux", "_work")):
            continue
        for fn in files:
            full = _os.path.relpath(_os.path.join(dp, fn), repo_root).replace("\\", "/")
            by_base.setdefault(fn, []).append(full)

    warnings: list[str] = []
    out: list[dict] = []
    for t in tasks:
        tp = (t.get("target_path") or "").replace("\\", "/")
        is_new = t.get("new_or_edit") == "new" or t.get("action") == "create"
        exists = _os.path.exists(_os.path.join(repo_root, tp))
        if exists:
            out.append(t); continue
        base = tp.rsplit("/", 1)[-1]
        matches = by_base.get(base, [])
        if not is_new and len(matches) == 1 and matches[0] != tp:
            warnings.append(f"{t.get('id')}: PATH CORRECTED {tp!r} -> {matches[0]!r} (basename resolve)")
            t = {**t, "target_path": matches[0]}
        elif not is_new and len(matches) > 1:
            warnings.append(f"{t.get('id')}: AMBIGUOUS {tp!r} -> {matches} (left as-is; needs disambiguation)")
        elif not is_new and not matches:
            warnings.append(f"{t.get('id')}: PATH NOT FOUND {tp!r} (no file named {base!r}; edit target missing)")
        # NEW files: just note if a sibling dir exists to anchor it
        out.append(t)
    return out, warnings


def test_gate(target_path: str, body: str, *, repo_root: str = ".",
              test_file: Optional[str] = None,
              timeout: float = 120.0) -> tuple[bool, str]:
    """Stage *body* at *target_path*, run its pytest, ALWAYS restore the original.
    Returns (passed, output_tail). If there is no test file, the gate PASSES
    vacuously (a create with no tests yet is not a regression). Never leaves the
    tree mutated — the original bytes are restored in a finally block."""
    tf = test_file or _guess_test_file(target_path)
    if tf is None:
        return True, "no associated test file — test gate skipped"

    full = _os.path.join(repo_root, target_path)
    existed = _os.path.exists(full)
    backup = None
    if existed:
        with open(full, "rb") as fh:
            backup = fh.read()
    try:
        _os.makedirs(_os.path.dirname(full) or ".", exist_ok=True)
        with open(full, "w", encoding="utf-8") as fh:
            fh.write(body)
        proc = _subprocess.run(
            ["python", "-X", "utf8", "-m", "pytest", "-q", "--tb=short",
             "-p", "no:warnings", tf],
            cwd=repo_root, capture_output=True, text=True, timeout=timeout,
            env={**_os.environ, "PYTHONUTF8": "1"})
        ok = proc.returncode == 0
        # Extract the SIGNAL (failure tracebacks + summary), not the warning flood —
        # the feedback must tell the implementer WHAT broke, or it can't fix it.
        out = proc.stdout + proc.stderr
        lines = out.splitlines()
        signal: list[str] = []
        for ln in lines:
            s = ln.strip()
            if (s.startswith(("FAILED", "ERROR", "E   ", ">", "_ _ _"))
                    or s.startswith("assert") or "Error" in s
                    or s.endswith(".py") and "::" in s):
                signal.append(ln)
        tail = ("\n".join(signal)[-1400:]) if signal else out.strip()[-1400:]
        return ok, tail
    except Exception as e:  # noqa: BLE001 — surface as a gate failure, not a crash
        return False, f"test gate error: {e}"
    finally:
        # ALWAYS restore — the gate must never mutate the real tree
        if existed and backup is not None:
            with open(full, "wb") as fh:
                fh.write(backup)
        elif not existed and _os.path.exists(full):
            _os.remove(full)


# --------------------------------------------------------------------------- #
# T2_patch — scoped-span regen (NOT a line-by-line diff: that is a bypass)     #
# --------------------------------------------------------------------------- #
# Whole-file regen mangles for tiny same-file edits (proven: gemma+deepseek both
# broke livebridge.py at a tricky-quote line). A line-by-line diff insert is just
# Edit in a costume — the model hand-places LOC, which is the bypass we removed.
# T2_patch is the disciplined middle: give the model a CURSOR SPAN [start,end] +
# guidance; it regenerates ONLY that contiguous block as a text-action; the
# mechanism splices it back deterministically; then the FULL gate runs on the
# whole spliced file (ast-parse + the existing tests). Small span = no mangle;
# scoped regen = not a diff; full-file gate = no behavioral regression.

def _compose_patch_card(target_path: str, span_text: str, instruction: str,
                        file_context: str) -> str:
    """Compose the implementer card for a SCOPED span rewrite."""
    return (
        "You are a fresh ECHELON implementer. You are rewriting ONLY a marked span "
        "of an existing file — not the whole file. Output ONLY the replacement text "
        "for the span (raw source, NO markdown fences, NO prose, NO line numbers). "
        "Your output replaces the span EXACTLY; preserve indentation so it splices "
        "cleanly. Add what the instruction asks and nothing else.\n\n"
        f"FILE (read-only context): {target_path}\n----\n{file_context}\n----\n\n"
        f"THE SPAN YOU ARE REWRITING (replace this entire block):\n----\n{span_text}\n----\n\n"
        f"INSTRUCTION:\n{instruction}"
    )


def t2_patch(target_path: str, start_line: int, end_line: int, instruction: str,
             *, model: str = T1_ARCHITECT, repo_root: str = ".",
             required_symbols: Optional[list[str]] = None,
             max_rounds: int = 3,
             emit: Optional[Callable[[str, dict], None]] = None) -> tuple[bool, str, str]:
    """Rewrite lines [start_line, end_line] (1-based, inclusive) of *target_path*
    via the model, splice the result back, and gate the WHOLE spliced file
    (mechanical ast-parse + the target's existing tests). Returns
    (passed, reason, spliced_full_body). Does NOT write the file — the caller
    declares/compiles the approved body (compile() stays the only writer)."""
    full = _os.path.join(repo_root, target_path)
    with open(full, "r", encoding="utf-8") as fh:
        lines = fh.read().splitlines(keepends=True)
    if not (1 <= start_line <= end_line <= len(lines)):
        return False, f"span [{start_line},{end_line}] out of range (file has {len(lines)} lines)", ""
    span_text = "".join(lines[start_line - 1:end_line])
    before, after = "".join(lines[:start_line - 1]), "".join(lines[end_line:])
    # read-only context: a window around the span so the model sees the shape
    ctx_lo, ctx_hi = max(0, start_line - 30), min(len(lines), end_line + 30)
    file_context = "".join(lines[ctx_lo:ctx_hi])

    def _emit(kind: str, payload: dict) -> None:
        if emit:
            emit(kind, payload)

    feedback = ""
    for round_n in range(max_rounds):
        prompt = _compose_patch_card(
            target_path, span_text,
            instruction + (f"\n\nFIX:\n{feedback}" if feedback else ""), file_context)
        raw = _chat(model, prompt, "", max_tokens=1200)
        # de-fence WITHOUT .strip() (that would eat the span's leading indentation);
        # drop only fence lines + blank edges, then normalize the trailing boundary
        # to exactly one newline so the splice against `after` keeps indentation.
        body_lines = [ln for ln in raw.splitlines() if not ln.strip().startswith("```")]
        while body_lines and not body_lines[0].strip():
            body_lines.pop(0)
        while body_lines and not body_lines[-1].strip():
            body_lines.pop()
        new_span = "\n".join(body_lines) + "\n"
        # one separating newline before `after` (which already carries its own indent)
        spliced = before + new_span + ("" if after.startswith("\n") else "\n") + after

        mech_ok, mech_reason, cleaned = mechanical_gate(
            spliced, required_symbols=required_symbols, is_python=target_path.endswith(".py"))
        _emit("patch", {"target": target_path, "round": round_n, "stage": "mechanical",
                        "pass": mech_ok, "reason": mech_reason[:160]})
        if not mech_ok:
            feedback = f"SPLICED FILE FAILED PARSE: {mech_reason}"
            continue

        test_ok, test_out = test_gate(target_path, cleaned, repo_root=repo_root)
        _emit("patch", {"target": target_path, "round": round_n, "stage": "test",
                        "pass": test_ok, "reason": test_out[:160]})
        if test_ok:
            return True, "patch gated clean (mechanical + tests)", cleaned
        feedback = f"EXISTING TESTS FAILED:\n{test_out[-700:]}"

    return False, f"patch failed after {max_rounds} rounds: {feedback[:300]}", ""


# --------------------------------------------------------------------------- #
# The two-stage review fix-loop (the ADD gate, per task)                      #
# --------------------------------------------------------------------------- #

def _parse_verdict(text: str) -> tuple[bool, str]:
    """Read the reviewer's first-line VERDICT. PASS => True."""
    first = text.strip().splitlines()[0] if text.strip() else ""
    passed = "VERDICT: PASS" in first.upper()
    return passed, text.strip()


def run_review_gate(intent_id: str, task: str, body: str, ledger: VerdictLedger,
                    *, emit: Optional[Callable[[str, dict], None]] = None,
                    max_fix_rounds: int = 2,
                    required_symbols: Optional[list[str]] = None,
                    is_python: bool = True,
                    target_path: Optional[str] = None,
                    repo_root: str = ".",
                    regenerate: Optional[Callable[[str], str]] = None) -> tuple[Status, str]:
    """Run MECHANICAL pre-gate, then the TEST gate (if the target has tests),
    then spec-review THEN quality-review. On FAIL, post a REJECTED event and (if
    a regenerate callback is given) re-implement and re-review up to
    ``max_fix_rounds``. Returns (final Status, cleaned_body) — the cleaned body
    is what the compiler should write. Spec before quality (wrong order is a red flag)."""
    def _emit(kind: str, payload: dict) -> None:
        if emit:
            emit(kind, payload)

    current_body = body
    for round_n in range(max_fix_rounds + 1):
        # --- stage 0: MECHANICAL pre-gate (deterministic, before any LLM) ---
        mech_pass, mech_reason, cleaned = mechanical_gate(
            current_body, required_symbols=required_symbols, is_python=is_python)
        _emit("review", {"intent": intent_id, "stage": "mechanical",
                         "pass": mech_pass, "round": round_n, "reason": mech_reason[:200]})
        if not mech_pass:
            ledger.post(StatusEvent(intent_id, Status.REJECTED, "mechanical", mech_reason))
            if regenerate and round_n < max_fix_rounds:
                current_body = regenerate(f"MECHANICAL GATE FAILED: {mech_reason}")
                continue
            return Status.REJECTED, cleaned
        current_body = cleaned  # the cleaned source is what we review + compile

        # --- stage 0.5: TEST gate (behavioral truth; only if target has tests) ---
        if target_path is not None:
            test_pass, test_out = test_gate(target_path, current_body, repo_root=repo_root)
            _emit("review", {"intent": intent_id, "stage": "test",
                             "pass": test_pass, "round": round_n, "reason": test_out[:200]})
            if not test_pass:
                ledger.post(StatusEvent(intent_id, Status.REJECTED, "test", test_out[:400]))
                if regenerate and round_n < max_fix_rounds:
                    current_body = regenerate(
                        f"EXISTING TESTS FAILED — you broke behavior. Fix without "
                        f"dropping existing functionality:\n{test_out[-800:]}")
                    continue
                return Status.REJECTED, current_body

        # --- stage 1: spec compliance ---
        spec_text = _chat(T1_ARCHITECT, compose_spec_reviewer_card(task, current_body),
                          "", max_tokens=600)
        spec_pass, spec_reason = _parse_verdict(spec_text)
        _emit("review", {"intent": intent_id, "stage": "spec",
                         "pass": spec_pass, "round": round_n, "reason": spec_reason[:200]})
        if not spec_pass:
            ledger.post(StatusEvent(intent_id, Status.REJECTED, "spec", spec_reason))
            if regenerate and round_n < max_fix_rounds:
                current_body = regenerate(spec_reason)
                continue
            return Status.REJECTED, current_body
        ledger.post(StatusEvent(intent_id, Status.SPEC_APPROVED, "spec", spec_reason))

        # --- stage 2: code quality (only after spec PASS) ---
        q_text = _chat(T1_ARCHITECT, compose_quality_reviewer_card(task, current_body),
                       "", max_tokens=700)
        q_pass, q_reason = _parse_verdict(q_text)
        _emit("review", {"intent": intent_id, "stage": "quality",
                         "pass": q_pass, "round": round_n, "reason": q_reason[:200]})
        if q_pass:
            ledger.post(StatusEvent(intent_id, Status.QUALITY_APPROVED, "quality", q_reason))
            return Status.QUALITY_APPROVED, current_body
        ledger.post(StatusEvent(intent_id, Status.REJECTED, "quality", q_reason))
        if regenerate and round_n < max_fix_rounds:
            current_body = regenerate(q_reason)
            continue
        return Status.REJECTED, current_body
    return Status.REJECTED, current_body


# --------------------------------------------------------------------------- #
# consult_partner — AMD-T1 (burst GPU) is the consultant for the weak local T2  #
# --------------------------------------------------------------------------- #
# CV-001/CV-003: every model where it thrives. The local smollm does cheap volume;
# when it hits its CEILING (BLOCKED/NEEDS_CONTEXT) or is about to EDIT a real file
# (clobber risk), it asks the strong free GPU partner. The partner-check IS both
# the escalation AND the clobber guard — not a cloud-cost escalation, a free peer.

def consult_partner(question: str, context: str = "", *, partner: str = BURST_T1,
                    max_tokens: int = 1500) -> str:
    """Ask the burst-T1 partner (over eos) a hard question the local model can't
    resolve. Returns the partner's answer. If the partner is unreachable, returns
    an empty string so the caller degrades (no answer) rather than crashes."""
    try:
        sys_p = ("You are AMD-T1, the senior partner consulted by a smaller local agent that hit its "
                 "ceiling. Answer the specific question DIRECTLY and concretely — give the reasoning / "
                 "decision / correction the small model needs to continue. Terse, actionable.")
        return _chat(partner, sys_p, f"{question}\n\nCONTEXT:\n{context}", max_tokens=max_tokens, timeout=300)
    except Exception:  # noqa: BLE001 — partner offline: degrade, don't crash the task
        return ""


def _clobber_check(target_path: str, original: str, new_body: str, *,
                   partner: str = BURST_T1) -> tuple[bool, str]:
    """Before an EDIT declares: ask AMD-T1 whether the new whole-file body DROPS
    existing behavior present in the original. Returns (safe, reason). On a
    partner-offline / unparseable answer, defaults SAFE (the test-gate is the
    backstop) — the consult is a guard, not a hard dependency."""
    q = ("This is a WHOLE-FILE rewrite of an existing file. Compare ORIGINAL vs NEW: does NEW drop any "
         "function, class, route, import, or behavior present in ORIGINAL? Reply on the FIRST line "
         "exactly: CLOBBER: YES or CLOBBER: NO. If YES, list each dropped item on its own line.")
    ans = consult_partner(q, f"FILE: {target_path}\n\nORIGINAL:\n{original[:8000]}\n\nNEW:\n{new_body[:8000]}",
                          partner=partner, max_tokens=800)
    if not ans:
        return True, "partner offline — defaulting safe (test-gate is the backstop)"
    first = ans.strip().splitlines()[0].upper() if ans.strip() else ""
    if "CLOBBER: YES" in first:
        return False, ans.strip()
    return True, "partner: no clobber"


# --------------------------------------------------------------------------- #
# Implement one task: generate -> declare -> gate                             #
# --------------------------------------------------------------------------- #

def implement_task(pool: IntentPool, ledger: VerdictLedger, *, intent_id: str,
                   part_of: str, target_path: str, action: str, task: str,
                   context: str, deps: Optional[list[str]] = None,
                   required_symbols: Optional[list[str]] = None,
                   implementer_model: str = T2_IMPLEMENTER,
                   partner_model: str = BURST_T1,
                   repo_root: str = ".",
                   emit: Optional[Callable[[str, dict], None]] = None) -> Status:
    """The per-task ADD cycle: a fresh implementer card generates the body
    (text-action), it is GATED (mechanical pre-gate + two-stage review with a
    regenerate fix-loop), and ONLY the cleaned+APPROVED body is declared into the
    pool. Returns the final Status. compile() writes only QUALITY_APPROVED intents.

    ``implementer_model`` selects the T2 generator per ADD's complexity routing:
    mechanical fan-out -> smollm3 (cheap); reasoning/multi-symbol code -> gemma
    (stronger). The BLOCKED protocol escalates the tier, never retries the same
    weak model unchanged.

    Note the order: gate BEFORE declare. The pool must never hold a fenced/broken
    body — declaring the cleaned approved source is the contract the compiler relies on."""
    def _gen(feedback: str = "") -> str:
        prompt = compose_implementer_card(
            task + (f"\n\nFIX THESE REVIEW ISSUES:\n{feedback}" if feedback else ""),
            context)
        return _chat(implementer_model, prompt, "", max_tokens=2000)

    body = _gen()
    # implementer self-reported a non-DONE status? CONSULT the AMD-T1 partner before
    # giving up (CV-001: the strong free GPU is on tap when the small model is stuck).
    head = body.strip().splitlines()[0].upper() if body.strip() else ""
    if head.startswith(("STATUS: NEEDS_CONTEXT", "STATUS: BLOCKED")):
        if emit:
            emit("consult", {"intent": intent_id, "reason": head[:60]})
        answer = consult_partner(
            f"A local agent reported '{head}' on this task. Resolve it so it can proceed:\n{task}",
            context, partner=partner_model)
        if answer:
            body = _gen(f"SENIOR PARTNER (AMD-T1) GUIDANCE:\n{answer}")
            head = body.strip().splitlines()[0].upper() if body.strip() else ""
        if head.startswith("STATUS: NEEDS_CONTEXT"):
            ledger.post(StatusEvent(intent_id, Status.NEEDS_CONTEXT, None, body[:300]))
            return Status.NEEDS_CONTEXT
        if head.startswith("STATUS: BLOCKED"):
            ledger.post(StatusEvent(intent_id, Status.BLOCKED, None, body[:300]))
            return Status.BLOCKED

    # gate FIRST — mechanical pre-gate cleans + validates, then the two-stage review
    is_py = target_path.endswith(".py")
    status, clean_body = run_review_gate(
        intent_id, task, body, ledger, emit=emit,
        required_symbols=required_symbols, is_python=is_py,
        target_path=target_path, repo_root=repo_root, regenerate=_gen)

    if status is not Status.QUALITY_APPROVED:
        return status  # rejected/blocked — nothing goes into the pool

    # EDIT clobber-check: before declaring a whole-file rewrite of an EXISTING file,
    # ask the AMD-T1 partner whether it drops existing behavior (the partner-check IS
    # the clobber guard). New files have nothing to clobber; skip.
    full_target = _os.path.join(repo_root, target_path)
    if action == "edit" and _os.path.exists(full_target):
        try:
            original = open(full_target, "r", encoding="utf-8", errors="replace").read()
        except OSError:
            original = ""
        if original:
            safe, reason = _clobber_check(target_path, original, clean_body, partner=partner_model)
            if emit:
                emit("clobber_check", {"intent": intent_id, "safe": safe, "reason": reason[:160]})
            if not safe:
                # partner flagged dropped behavior — regen with the warning, re-gate once
                body = _gen(f"PARTNER CLOBBER WARNING — your rewrite DROPS existing behavior. "
                            f"Preserve ALL of it:\n{reason}")
                status, clean_body = run_review_gate(
                    intent_id, task, body, ledger, emit=emit,
                    required_symbols=required_symbols, is_python=is_py,
                    target_path=target_path, repo_root=repo_root, regenerate=_gen)
                if status is not Status.QUALITY_APPROVED:
                    return status

    # declare ONLY the cleaned, approved body (does NOT write to disk)
    result = declare_impl(pool, id=intent_id, part_of=part_of, target_path=target_path,
                          action=action, body=clean_body, deps=deps or [], emit=None)
    if result.startswith("ERROR"):
        ledger.post(StatusEvent(intent_id, Status.BLOCKED, None, result))
        return Status.BLOCKED
    if emit:
        emit("declare", {"intent": intent_id, "target_path": target_path, "action": action})
    return status


# --------------------------------------------------------------------------- #
# The continuous OS+gemma dispatch loop (observe-only, stop-on-exception)     #
# --------------------------------------------------------------------------- #

@dataclass
class Task:
    intent_id: str
    part_of: str
    target_path: str
    action: str
    task: str
    context: str
    deps: list[str] = field(default_factory=list)
    # spec-named symbols the mechanical gate must find DEFINED in the body
    # (classes/functions/top-level assignments). The deterministic half of "spec compliance".
    required_symbols: list[str] = field(default_factory=list)
    # T2 generator model: smollm3 for mechanical fan-out, gemma for reasoning-heavy code.
    implementer_model: str = T2_IMPLEMENTER
    # the senior partner consulted when the local model hits its ceiling / on edit-clobber.
    partner_model: str = BURST_T1
    # repo root for the TEST gate (run the target's existing pytest against the candidate)
    repo_root: str = "."
    # AUDIT phase: if set, audit_dispatch reads these files and grounds this task's
    # context from the SANITISED audit (a pointer), so OS never hand-writes the context.
    audit_targets: list = field(default_factory=list)


# --------------------------------------------------------------------------- #
# ground_context — the AUDIT phase: ground each task's context from a pointer  #
# --------------------------------------------------------------------------- #
# AUDIT -> PLAN -> IMPLEMENT made real: for every task with audit_targets, a
# delegated audit reads the real files and the task's context is set from the
# SANITISED audit (paged via a pointer), NOT hand-written by OS. Tasks without
# audit_targets keep their existing context (back-compat). OS holds pointers.

def ground_context(tasks: list, *, ram=None, card_store=None, repo_root: str = ".",
                   directories=None,
                   emit: Optional[Callable[[str, dict], None]] = None) -> list:
    """Ground each task's context from a delegated audit of its audit_targets.
    Mutates and returns the task list. Tasks with no audit_targets are untouched.
    The audit runs on a shared FileRAM (read-once, pointer-shared). ``directories``
    (a list of repo roots) builds a CROSS-REPO FileRAM so one task's audit_targets
    can name files in any of the given repos (the owner's --directory [array]);
    falls back to ``repo_root`` when not given."""
    if ram is None:
        ram = FileRAM(directories if directories else repo_root)
    for t in tasks:
        targets = getattr(t, "audit_targets", None)
        if not targets:
            continue
        if emit:
            emit("ground_start", {"intent": t.intent_id, "targets": len(targets)})
        result = audit_dispatch(targets, t.task, ram=ram, card_store=card_store, emit=emit)
        # the task's context is now the SANITISED audit, paged behind result['pointer']
        grounded = "[AUDIT pointer " + result["pointer"] + "]\n" + result["audit"]
        t.context = (grounded + ("\n\n" + t.context if t.context else ""))
        if emit:
            emit("ground_done", {"intent": t.intent_id, "pointer": result["pointer"],
                                 "card_worthy": result["card_worthy"]})
    return tasks

class StopSignal(Exception):
    """Raised to halt the loop — the ONLY interrupt (ADD: stop-on-exception)."""


def run_add_loop(tasks: list[Task], *, db_path: str = ":memory:",
                 emit: Optional[Callable[[str, dict], None]] = None,
                 stop_check: Optional[Callable[[], bool]] = None,
                 card_store=None, directories=None) -> dict:
    """Drive the full ADD pipeline over a task list. Continuous: never pauses to
    check in (ADD), only halts on exception or an external STOP. Each task runs
    the generate→declare→two-stage-gate cycle; approved intents accumulate in the
    pool for compile(). Returns a summary dict.

    ``emit``       — observe-only stream sink (wire to livebridge at scale).
    ``stop_check`` — polled between tasks; True => raise StopSignal (the owner's
                     interrupt, never a fan-in relay)."""
    pool = IntentPool(db_path)
    ledger = VerdictLedger()
    results: dict[str, str] = {}
    ground_context(tasks, repo_root=tasks[0].repo_root if tasks else ".",
                   directories=directories, card_store=card_store, emit=emit)

    def _emit(kind: str, payload: dict) -> None:
        if emit:
            emit(kind, payload)

    try:
        for t in tasks:
            if stop_check and stop_check():
                raise StopSignal("external STOP")
            _emit("task_start", {"intent": t.intent_id, "target": t.target_path})
            status = implement_task(pool, ledger, intent_id=t.intent_id,
                                    part_of=t.part_of, target_path=t.target_path,
                                    action=t.action, task=t.task, context=t.context,
                                    deps=t.deps, required_symbols=t.required_symbols,
                                    implementer_model=t.implementer_model,
                                    partner_model=t.partner_model,
                                    repo_root=t.repo_root, emit=emit)
            results[t.intent_id] = status.value
            _emit("task_done", {"intent": t.intent_id, "status": status.value})
            # stop-on-exception: a hard BLOCKED is the swarm's "raise to OS"
            if status is Status.BLOCKED:
                _emit("exception", {"intent": t.intent_id, "reason": "BLOCKED"})
                raise StopSignal(f"task {t.intent_id} BLOCKED")
    except StopSignal as s:
        _emit("stopped", {"reason": str(s)})
        return {"results": results, "approved": ledger.approved_ids(),
                "stopped": str(s), "pool": pool}

    return {"results": results, "approved": ledger.approved_ids(),
            "stopped": None, "pool": pool}


# --------------------------------------------------------------------------- #
# The SWARM path: collect-then-review (the sequence ledger as coordinator)     #
# --------------------------------------------------------------------------- #
# The owner's architecture: the swarm GENERATES wide into ONE IntentPool (each
# intent carries its content-addressed id + rowid sequence + target_path
# collision marks). THEN a SINGLE collected pass reviews + re-orders the whole
# set with full visibility, before the compiler implements it. This differs from
# run_add_loop (per-task gate, serial/bootstrap) — here review is GLOBAL, so
# conflict-resolution and ordering see every intent at once.
#
# Division of labour (proven routing): smollm GENERATES (cheap fan-out); the
# review pass = the mechanical gate + GEMMA for spec/quality/conflict-resolution;
# compile() does the deterministic re-order (topo-sort). check() does structural
# review (unique ids / deps resolve / canonical / cycles) over the collected set.

from echelon_sdk.compiler.check import check as _check
from echelon_sdk.compiler.status import (
    Status as _CStatus, StatusEvent as _CEvent,
    latest_status as _c_latest, is_compile_eligible as _c_eligible,
)


def generate_intents(tasks: list["Task"], pool: IntentPool, ledger: VerdictLedger,
                     *, emit: Optional[Callable[[str, dict], None]] = None) -> dict:
    """Phase 1 — fan generation WIDE. Each task's implementer generates a body
    (text-action), the MECHANICAL gate cleans+validates it (the only per-task
    gate during generation — no spec/quality yet, that is the collected pass),
    and the cleaned body is declared into the ONE pool. NEEDS_CONTEXT/BLOCKED
    are recorded but do not halt generation (the collected pass sees the rest).
    Returns {intent_id: 'declared'|'mechanical_fail'|'needs_context'|'blocked'}."""
    def _emit(kind: str, payload: dict) -> None:
        if emit:
            emit(kind, payload)

    gen_status: dict[str, str] = {}
    for t in tasks:
        _emit("gen_start", {"intent": t.intent_id, "target": t.target_path})

        def _gen(feedback: str = "", _t=t) -> str:
            prompt = compose_implementer_card(
                _t.task + (f"\n\nFIX:\n{feedback}" if feedback else ""), _t.context)
            return _chat(_t.implementer_model, prompt, "", max_tokens=2000)

        body = _gen()
        head = body.strip().splitlines()[0].upper() if body.strip() else ""
        # The local implementer hit its CEILING (NEEDS_CONTEXT/BLOCKED). Before
        # recording failure, CONSULT the AMD-T1 partner and regenerate with its
        # guidance — the same escalation implement_task does (CV-001: the strong
        # free GPU is on tap when the small model is stuck). The swarm path used to
        # skip this, so the partner sat idle while weak tasks died — the bug the
        # Epsilon-Co run surfaced.
        if head.startswith(("STATUS: NEEDS_CONTEXT", "STATUS: BLOCKED")):
            _emit("consult", {"intent": t.intent_id, "reason": head[:60]})
            answer = consult_partner(
                f"A local agent reported '{head}' on this task. Resolve it so it can proceed:\n{t.task}",
                t.context, partner=t.partner_model)
            if answer:
                body = _gen(f"SENIOR PARTNER (AMD-T1) GUIDANCE:\n{answer}")
                head = body.strip().splitlines()[0].upper() if body.strip() else ""
        if head.startswith("STATUS: NEEDS_CONTEXT"):
            ledger.post(StatusEvent(t.intent_id, Status.NEEDS_CONTEXT, None, body[:300]))
            gen_status[t.intent_id] = "needs_context"
            _emit("gen_done", {"intent": t.intent_id, "status": "NEEDS_CONTEXT"})
            continue
        if head.startswith("STATUS: BLOCKED"):
            ledger.post(StatusEvent(t.intent_id, Status.BLOCKED, None, body[:300]))
            gen_status[t.intent_id] = "blocked"
            _emit("gen_done", {"intent": t.intent_id, "status": "BLOCKED"})
            continue

        # mechanical gate with a small fix-loop (cheap, deterministic) before declaring
        lang = _lang_of(t.target_path)
        cleaned = body
        ok = False
        for attempt in range(3):
            ok, reason, cleaned = mechanical_gate(
                cleaned, required_symbols=t.required_symbols, lang=lang,
                target_path=t.target_path)
            if ok:
                break
            _emit("gen_mech", {"intent": t.intent_id, "attempt": attempt, "reason": reason[:160]})
            # On the LAST local attempt, escalate to the partner instead of burning
            # the final regen on the same stuck local model.
            if attempt == 1:
                ans = consult_partner(
                    f"A local agent keeps FAILING the mechanical gate on this task. "
                    f"Gate reason: {reason}\nTask: {t.task}\nProduce a corrected, COMPLETE body.",
                    t.context, partner=t.partner_model)
                if ans:
                    _emit("consult", {"intent": t.intent_id, "reason": "mech_escalate"})
                    cleaned = _gen(f"SENIOR PARTNER (AMD-T1) GUIDANCE on the gate failure:\n{ans}")
                    continue
            cleaned = _gen(f"MECHANICAL GATE FAILED: {reason}")
        if not ok:
            ledger.post(StatusEvent(t.intent_id, Status.BLOCKED, "mechanical", reason))
            gen_status[t.intent_id] = "mechanical_fail"
            _emit("gen_done", {"intent": t.intent_id, "status": "MECH_FAIL"})
            continue

        result = declare_impl(pool, id=t.intent_id, part_of=t.part_of,
                              target_path=t.target_path, action=t.action,
                              body=cleaned, deps=t.deps or [], emit=None)
        if result.startswith("ERROR"):
            ledger.post(StatusEvent(t.intent_id, Status.BLOCKED, None, result))
            gen_status[t.intent_id] = "blocked"
        else:
            gen_status[t.intent_id] = "declared"
            _emit("declare", {"intent": t.intent_id, "target_path": t.target_path})
        _emit("gen_done", {"intent": t.intent_id, "status": gen_status[t.intent_id]})
    return gen_status


def review_pass(pool: IntentPool, tasks: list["Task"], ledger: VerdictLedger,
                *, emit: Optional[Callable[[str, dict], None]] = None) -> dict:
    """Phase 2 — the SINGLE collected review pass over everything in the pool.
    Runs check() (structural, whole-set) FIRST, surfacing any conflicts (the
    pool also pre-marked target_path/canonical collisions). Then, per declared
    intent, gemma runs spec+quality with full-set context and the verdict is
    written as an append-only status-event. Returns {intent_id: status_value}.
    Re-ordering for implementation is the compiler's deterministic topo-sort."""
    def _emit(kind: str, payload: dict) -> None:
        if emit:
            emit(kind, payload)

    intents = pool.read()
    task_by_id = {t.intent_id: t for t in tasks}

    # --- structural review of the COLLECTED set (whole-pool, conflict-visible) ---
    conflicts = _check(intents)
    pool_conflicts = [i.id for i in intents if getattr(i, "conflict", 0)]
    _emit("collected_check", {"n_intents": len(intents),
                              "structural_conflicts": len(conflicts),
                              "path_conflicts": pool_conflicts})

    verdicts: dict[str, str] = {}
    for it in intents:
        t = task_by_id.get(it.id)
        spec = t.task if t else f"(intent {it.id} -> {it.target_path})"

        # stage 1: spec compliance (gemma), with the whole-set conflict note
        spec_text = _chat(T1_ARCHITECT, compose_spec_reviewer_card(spec, it.body), "", max_tokens=600)
        spec_pass, spec_reason = _parse_verdict(spec_text)
        if not spec_pass:
            ledger.post(StatusEvent(it.id, Status.REJECTED, "spec", spec_reason))
            pool.post_status(it.id, Status.REJECTED.value, stage="spec", note=spec_reason[:500])
            verdicts[it.id] = Status.REJECTED.value
            _emit("review", {"intent": it.id, "stage": "spec", "pass": False, "reason": spec_reason[:160]})
            continue
        ledger.post(StatusEvent(it.id, Status.SPEC_APPROVED, "spec", spec_reason))
        pool.post_status(it.id, Status.SPEC_APPROVED.value, stage="spec", note=spec_reason[:500])
        _emit("review", {"intent": it.id, "stage": "spec", "pass": True})

        # stage 2: code quality (gemma)
        q_text = _chat(T1_ARCHITECT, compose_quality_reviewer_card(spec, it.body), "", max_tokens=700)
        q_pass, q_reason = _parse_verdict(q_text)
        if q_pass:
            ledger.post(StatusEvent(it.id, Status.QUALITY_APPROVED, "quality", q_reason))
            pool.post_status(it.id, Status.QUALITY_APPROVED.value, stage="quality", note=q_reason[:500])
            verdicts[it.id] = Status.QUALITY_APPROVED.value
        else:
            ledger.post(StatusEvent(it.id, Status.REJECTED, "quality", q_reason))
            pool.post_status(it.id, Status.REJECTED.value, stage="quality", note=q_reason[:500])
            verdicts[it.id] = Status.REJECTED.value
        _emit("review", {"intent": it.id, "stage": "quality", "pass": q_pass})

    return {"verdicts": verdicts, "structural_conflicts": [c.kind for c in conflicts],
            "path_conflicts": pool_conflicts}


def run_add_swarm(tasks: list["Task"], *, db_path: str = ":memory:",
                  emit: Optional[Callable[[str, dict], None]] = None,
                  compile_to: Optional[str] = None,
                  card_store: Optional[str] = None,
                  directories=None) -> dict:
    """The SWARM path — generate wide, then one collected review pass.

    Phase 1: generate_intents — fan smollm wide into ONE pool (mechanical gate
             per body; no spec/quality yet).
    Phase 2: review_pass — single collected pass: check() structural + gemma
             spec/quality, producing append-only status-events.
    The compiler's topo-sort (compile.py) is the deterministic RE-ORDER; once
    seam B lands, compile() filters to is_compile_eligible. Until then this
    returns the eligible set so the caller can verify before compiling.

    If compile_to is given, also compiles approved intents to that directory.

    Returns {gen, review, eligible, pool, written} — observe-only; OS verifies the result."""
    pool = IntentPool(db_path)
    ledger = VerdictLedger()
    ground_context(tasks, repo_root=tasks[0].repo_root if tasks else ".",
                   directories=directories, card_store=card_store, emit=emit)

    def _emit(kind: str, payload: dict) -> None:
        if emit:
            emit(kind, payload)

    _emit("swarm_phase", {"phase": "generate", "n_tasks": len(tasks)})
    gen = generate_intents(tasks, pool, ledger, emit=emit)

    _emit("swarm_phase", {"phase": "review", "n_declared": sum(1 for v in gen.values() if v == "declared")})
    review = review_pass(pool, tasks, ledger, emit=emit)

    eligible = ledger.approved_ids()
    _emit("swarm_phase", {"phase": "done", "eligible": eligible})

    written: list[str] = []
    if compile_to is not None:
        from echelon_sdk.compiler.compile import compile as _compile, CompilerError
        try:
            written = _compile(pool, base_dir=compile_to, require_approved=True)
        except CompilerError as e:
            written = []
            _emit("compile_error", {"message": str(e)})
        _emit("compiled", {"count": len(written)})

    return {"gen": gen, "review": review, "eligible": eligible, "pool": pool, "written": written}

# --------------------------------------------------------------------------- #
# FileRAM + ContextMap — the RAM working substrate (disk persists only)        #
# --------------------------------------------------------------------------- #
# ECHELON works through RAM: read a file from disk ONCE, hold it resident, share
# by POINTER (read-mostly => no lock). ContextMap is working-set management (NOT
# a cache): a deliberately composed set of pointers = an agent's field of vision.
# Staleness is solved by content-address + the append-only bank (a mutated object
# is a NEW id; an old pointer resolves to its immutable snapshot). lock() protects
# the pointer-LIST structure, not the data (data is already immutable).

class FileRAM:
    """Read-once resident store: path -> content, handed out by pointer. No lock.

    Multi-root: ``roots`` may be one repo root (back-compat str) or a LIST of
    roots (cross-repo dispatch — the owner's --directory [array]). A path is
    resolved against the first root that actually contains it, so one audit
    ContextMap can span Mol-Data-Engine / Mol-Preliminary / Mol-MRP at once. An
    ABSOLUTE path bypasses root-joining and is read as-is. The cache key is the
    path as given, so the same logical path resolves to one resident copy."""

    def __init__(self, repo_root="." ) -> None:
        # accept a str (one root) or a list/tuple of roots; preserve order (first-match wins)
        if isinstance(repo_root, (list, tuple)):
            self._roots = [str(r) for r in repo_root] or ["."]
        else:
            self._roots = [str(repo_root)]
        self._root = self._roots[0]  # back-compat attribute (first root)
        self._cache: dict[str, str] = {}

    def _resolve(self, path: str) -> str:
        """Return the on-disk path: absolute as-is, else the first root that has it,
        else the first root (so the error names a concrete, expected location)."""
        if _os.path.isabs(path):
            return path
        for root in self._roots:
            cand = _os.path.join(root, path)
            if _os.path.exists(cand):
                return cand
        return _os.path.join(self._roots[0], path)

    def get(self, path: str) -> str:
        if path not in self._cache:
            with open(self._resolve(path), "r", encoding="utf-8") as fh:
                self._cache[path] = fh.read()
        return self._cache[path]

    def pointer(self, path: str) -> str:
        self.get(path)  # eager load: a pointer always resolves (deepseek's compose-load fix)
        return f"ram:{path}"

    def deref(self, pointer: str) -> str:
        if not pointer.startswith("ram:"):
            raise KeyError(f"not a FileRAM pointer: {pointer!r}")
        return self.get(pointer[4:])


class ContextMap:
    """A composed working set of pointers over a FileRAM (an agent's field of vision).
    compose holds the pointer LIST (lazy deref); clone is a cheap list-copy that
    shares resident data; lock pins the pointer-list structure (not the data)."""

    def __init__(self, ram: "FileRAM") -> None:
        self._ram = ram
        self._pointers: list[str] = []
        self._locked = False

    def compose(self, paths: list[str]) -> "ContextMap":
        if self._locked:
            raise RuntimeError("ContextMap is locked; cannot compose")
        for p in paths:
            ptr = self._ram.pointer(p)  # eager load via FileRAM
            if ptr not in self._pointers:
                self._pointers.append(ptr)
        return self

    def view(self) -> dict[str, str]:
        """Lazily deref the working set: pointer -> resident content."""
        return {ptr: self._ram.deref(ptr) for ptr in self._pointers}

    def clone(self) -> "ContextMap":
        c = ContextMap(self._ram)
        c._pointers = list(self._pointers)  # cheap list-copy; shares resident data
        return c

    def lock(self) -> "ContextMap":
        self._locked = True  # pointer-list structure is now immutable
        return self

# --------------------------------------------------------------------------- #
# audit_dispatch — the DELEGATED audit (file-ingest applied; slim ctx, rich data)#
# --------------------------------------------------------------------------- #
# OS (opus, costliest tier) must NOT spend its field-of-vision reading raw files.
# A cheap tier reads via a ContextMap (its vision), gemma distills a SANITISED
# audit, and IF gemma judges it Card-worthy it is collected into the CardStore
# (COS — significance EARNED, never a raw-LOC dump); else it stays run-transient.
# OS receives only a POINTER and pages it to gate the plan. RAM working substrate.

def audit_dispatch(target_paths: list, goal: str, *, ram: "FileRAM",
                   card_store=None,
                   reader: str = T2_IMPLEMENTER, distiller: str = T1_ARCHITECT,
                   read_cap: int = 6000, read_tokens: int = 500,
                   distill_tokens: int = 900,
                   emit: Optional[Callable[[str, dict], None]] = None) -> dict:
    """Delegate the AUDIT. Returns {pointer, card_worthy, audit} — OS holds the
    pointer, not the bytes. If card_store is given AND gemma judges it worthy, the
    audit is collected as a Card (refs = per-file audit atoms) and pointer is the
    card id; else pointer is a transient 'ram:audit:<key>' handle into FileRAM."""
    def _emit(kind, payload):
        if emit:
            emit(kind, payload)

    cm = ContextMap(ram).compose(target_paths).lock()
    view = cm.view()
    _emit("audit_compose", {"n_files": len(view), "goal": goal[:80]})

    # cheap tier reads each resident file (spends ITS context, not OS's)
    observations = []
    for ptr, content in view.items():
        read_card = (
            "You are an ECHELON audit reader. Read this file and report ONLY the "
            "signatures, key line-spans, seams/gaps relevant to the goal. Terse, "
            "factual, no prose.\n\nGOAL: " + goal + "\n\nFILE " + ptr + ":\n" + content[:read_cap])
        obs = _chat(reader, "Terse audit reader.", read_card, max_tokens=read_tokens)
        observations.append((ptr, obs))
        _emit("audit_read", {"pointer": ptr})

    # gemma distills a SANITISED audit + judges Card-worthiness
    joined = "\n\n".join(f"{p}:\n{o}" for p, o in observations)
    distill = _chat(distiller,
        "You are an ECHELON audit distiller. Output a SANITISED audit (signatures, "
        "spans, seams, contract-to-preserve). On the FIRST line output exactly "
        "CARD_WORTHY: YES or CARD_WORTHY: NO (YES only if this is a reusable, stable "
        "contract worth keeping; NO for one-shot run scratch).",
        "GOAL: " + goal + "\n\nOBSERVATIONS:\n" + joined, max_tokens=distill_tokens)
    first = distill.strip().splitlines()[0].upper() if distill.strip() else ""
    card_worthy = "CARD_WORTHY: YES" in first
    audit_body = distill

    key = str(abs(hash((goal, tuple(target_paths)))))
    if card_worthy and card_store is not None:
        refs = []
        for ptr, obs in observations:
            coord = "audit:" + ptr.replace("ram:", "") + ":" + key
            refs.append(card_store.add_atom(coord, obs, born_from="audit_dispatch"))
        pointer = card_store.add_card("audit:" + goal[:40], refs, born_from="audit_dispatch")
        _emit("audit_card", {"pointer": pointer, "refs": len(refs)})
    else:
        pointer = "ram:audit:" + key
        ram._cache[pointer.replace("ram:", "")] = audit_body  # transient, freed on exit
        _emit("audit_transient", {"pointer": pointer})

    return {"pointer": pointer, "card_worthy": card_worthy, "audit": audit_body}

# --------------------------------------------------------------------------- #
# CLI smoke entry                                                             #
# --------------------------------------------------------------------------- #

if __name__ == "__main__":  # pragma: no cover
    import sys

    def _printer(kind: str, payload: dict) -> None:
        print(f"  [{kind}] {json.dumps(payload)[:160]}")

    if "--ping" in sys.argv:
        # prove the endpoint + both tiers are live
        for m in (T1_ARCHITECT, T2_IMPLEMENTER):
            try:
                r = _chat(m, "Reply with one word: OK", "", max_tokens=8, timeout=30)
                print(f"{m}: {r.strip()[:40]}")
            except Exception as e:  # noqa: BLE001
                print(f"{m}: UNREACHABLE ({e})")
        sys.exit(0)

    demo = [Task(intent_id="add-demo-1", part_of="demo", target_path="demo/hello.py",
                 action="create",
                 task="Create a function add(a, b) that returns a+b, with a docstring.",
                 context="Pure python, stdlib only. One file.")]
    out = run_add_loop(demo, emit=_printer)
    print(json.dumps({k: v for k, v in out.items() if k != "pool"}, indent=2))
