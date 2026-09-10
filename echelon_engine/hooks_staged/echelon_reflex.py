"""echelon_reflex.py — the REFLEX ARC (PreToolUse). Fast, dumb, spinal.

The owner's spec (2026-07-08): a real reflex is like the harness's own read-before-edit
or auto-mode's risk-block — pre-determined, fired by the EVENT (a tool about to run),
not by the topic of the conversation. ECHELON's version: atoms flagged `reflex: true`
are compiled by `echelon reflex compile` into ~/.echelon/reflexes.json; THIS hook loads
that static ruleset in milliseconds and fires matching arcs. The bank is consulted at
compile time, never here — tool events fire dozens of times per turn; a warmth probe
on this path would be cortical latency on a spinal circuit.

FIRING:
  action=block -> exit 2, guard text on stderr (the harness denies the tool call and
                  shows the guard to the model). Teeth are DECLARED per-atom, never default.
  action=warn  -> additionalContext with the guard text (involuntary injection at the
                  choke point; the model still chooses).

SEVERABILITY (the nerve's own law): ANY failure -> exit 0, the tool call proceeds
un-guarded. A broken ruleset must never block the owner's channel.
Kill switch: ECHELON_REFLEX=off.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time

RULESET = os.path.expanduser("~/.echelon/reflexes.json")
FIRELOG = os.path.expanduser("~/.echelon/reflex_fires.jsonl")
HEALTH = os.path.expanduser("~/.echelon/reflex_health.json")


def _drop_demoted(rules: list, health_path: str) -> list:
    try:
        with open(health_path, encoding="utf-8") as f:
            reflexes = json.load(f).get("reflexes", {})
        return [r for r in rules
                if not reflexes.get(f"{r.get('scope', '?')}:{r.get('name', '?')}", {}).get("demoted")]
    except Exception:
        return rules

# TIER (2026-08-15): where a rule fires, orthogonal to teeth (how hard it fires).
#   scope    — estate-local; fires ONLY when the session's resolved scope matches the
#              rule's scope. The DEFAULT, and the default for legacy untiered rules.
#   global   — machine-wide truth (bank integrity, secrets, harness traps); fires anywhere.
#   portable — a global that also ships in the starter seed; fires anywhere.
# Before this existed, `scope` was a display label only: an audit found 140 of 141 rules
# unfenced, so every estate's traps were evaluated against every other estate's tool calls.
DEFAULT_TIER = "scope"
WIDE_TIERS = ("global", "portable")


def _session_scope(cwd: str) -> str:
    """Resolve the session's estate through the engine's canonical resolver — the SAME
    one ingest/wrap/gate use, so a rule's home and a session's home are decided by one
    source of truth (a path regex disagreed with it at the ECHELON-AGENT boundary).

    Severable: any failure returns "" and the caller then fires scope-rules rather than
    swallowing them — a reflex that fails open is a warning too many, which is recoverable;
    one that fails closed is a missing guard, which is not.
    """
    try:
        try:
            import echelon_engine  # already importable in most sessions
        except ImportError:
            root = os.environ.get("ECHELON_ENGINE", "") or os.path.join(
                os.path.dirname(os.path.normpath(os.getcwd())), "ECHELON-AGENT")
            if root and root not in sys.path:
                sys.path.insert(0, root)
        from echelon_engine.atoms.resolve_scope import resolve_scope  # type: ignore
        return (resolve_scope(cwd) or "").strip().lower()
    except Exception:
        return ""


def _graph_precheck(cwd: str, payload: str):
    """Replace the grep-reflex lecture with the real graph answer (OPEN-0075).
    Severable: any failure or no-answer -> None, caller keeps the original guard."""
    if os.environ.get("ECHELON_GRAPH_PRECHECK", "").lower() == "off":
        return None
    try:
        from echelon_engine.atoms import cgraph  # type: ignore
        from pathlib import Path
        define_dir = cgraph.find_atlas(Path(cwd))
        if not define_dir:
            return None
        data = json.loads(payload)
        command = data.get("command", "") or ""
        if not command:
            return None
        return cgraph.answer_for_grep(cgraph.load_graph(define_dir), command, define_dir)
    except Exception:
        return None


def _log_fires(fired: list[dict], tool: str, payload: str) -> None:
    """Append one jsonl line per fire — the data feed for the long-horizon proof
    (M1 trap recurrence, M2 reflex precision). Severable like everything here."""
    try:
        with open(FIRELOG, "a", encoding="utf-8") as f:
            for r in fired:
                f.write(json.dumps({
                    "ts": int(time.time()),
                    "reflex": f"{r.get('scope', '?')}:{r.get('name', '?')}",
                    "tier": r.get("tier", "scope"),
                    "action": r.get("action", "warn"),
                    "tool": tool,
                    "payload": payload[:200],
                }, ensure_ascii=False) + "\n")
    except Exception:
        pass


def main() -> int:
    if os.environ.get("ECHELON_REFLEX", "").lower() in ("off", "0", "false"):
        return 0
    try:
        if not os.path.exists(RULESET):
            return 0
        with open(RULESET, encoding="utf-8") as f:
            rules = json.load(f).get("rules", [])
        rules = [r for r in rules if r.get("event") == "PreToolUse"]
        rules = _drop_demoted(rules, HEALTH)
        if not rules:
            return 0

        raw = sys.stdin.read().lstrip("﻿")
        data = json.loads(raw or "{}")
        tool = data.get("tool_name", "")
        payload = json.dumps(data.get("tool_input", {}), ensure_ascii=False)
        cwd = str(data.get("cwd", "")).replace("\\", "/")

        # THE TIER GATE — resolved lazily and ONCE (resolve_scope touches the filesystem;
        # this hook runs on every tool call, so it stays off the path when no scope-tier
        # rule is present, e.g. a fresh install carrying only the portable seed).
        sess_scope = None

        # THE SHARED MATCHER (2026-08-15): the firing decision lives in
        # echelon_engine.atoms.reflex.rule_fires so that `echelon reflex test` asserts
        # against the SAME law this hook applies. A locally re-derived copy is how a
        # reflex passes its test and never fires. Severable: if the engine is not
        # importable, fall back to the inline path below (the hook must never break).
        _shared = None
        try:
            from echelon_engine.atoms.reflex import rule_fires as _shared  # type: ignore
        except Exception:
            _shared = None

        # SESSION FACTS (2026-08-15) — the condition gate a regex cannot express. Loaded ONCE
        # per event and only when some rule actually declares `when`, so the common path stays
        # spinal. None = unknown, and every consumer fails OPEN on unknown.
        _facts = None
        if any(r.get("when") for r in rules):
            try:
                sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
                from echelon_session_facts import load_facts  # type: ignore
                _facts = load_facts(str(data.get("session_id", "") or "unknown"))
            except Exception:
                _facts = None

        # THE TYPE GATE (charter Part 4, 2026-08-29) — the room's declared TYPE arms or
        # hides a rule carrying `worktype`; a live `type snooze` mutes by name. Loaded ONCE
        # per event, only when some rule could be affected; unknown type = fail OPEN.
        _room_type, _snoozed = "", ()
        try:
            from echelon_engine.worktype import session_gate  # type: ignore
            _room_type, _snoozed = session_gate(cwd)
        except Exception:
            _room_type, _snoozed = "", ()

        blocks, warns = [], []
        for r in rules:
            try:
                tier = r.get("tier", DEFAULT_TIER)
                if tier not in WIDE_TIERS:
                    # estate-local: fire only where this rule lives. An unresolvable scope
                    # fires anyway (fail OPEN — a spurious warning beats a missing guard).
                    if sess_scope is None:
                        sess_scope = _session_scope(cwd)
                if _shared is not None:
                    try:
                        fired, _why = _shared(r, tool, payload, sess_scope or "", cwd, _facts,
                                              _room_type, _snoozed)
                    except TypeError:  # an older engine without the type gate
                        fired, _why = _shared(r, tool, payload, sess_scope or "", cwd, _facts)
                    if not fired:
                        continue
                else:
                    if tier not in WIDE_TIERS and sess_scope and \
                            (r.get("scope", "") or "").strip().lower() != sess_scope:
                        continue
                    types = r.get("worktype")
                    if types and _room_type and _room_type.lower() not in [t.lower() for t in types]:
                        continue
                    if _snoozed and (r.get("name") in _snoozed
                                     or f"{r.get('scope', '')}:{r.get('name')}" in _snoozed):
                        continue
                    if r.get("cwd") and not re.search(r["cwd"], cwd):
                        continue  # hand-authored NARROWING within an estate
                    if not re.fullmatch(r.get("tool", ".*"), tool):
                        continue
                    if not re.search(r["match"], payload):
                        continue
            except Exception:
                continue  # a bad pattern/rule severs that one arc, not the turn (gate S-3)
            (blocks if r.get("action") == "block" else warns).append(r)

        if blocks or warns:
            _log_fires(blocks + warns, tool, payload)
        if blocks:
            for r in blocks:
                print(f"[echelon-reflex BLOCK «{r['scope']}:{r['name']}»] {r['guard']}",
                      file=sys.stderr)
            return 2
        if warns:
            patched = []
            for r in warns:
                if r.get("name") == "reflex-graph-before-brute-force" and tool in ("Bash", "PowerShell"):
                    ans = _graph_precheck(cwd, payload)
                    if ans:
                        r = dict(r, guard=ans)
                patched.append(r)
            warns = patched
            ctx = "\n".join(f"[echelon-reflex «{r['scope']}:{r['name']}»] {r['guard']}"
                            for r in warns)
            print(json.dumps({"hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "additionalContext": ctx,
            }}))
        return 0
    except Exception:
        return 0  # severed: the reflex organ must never block the body it serves


if __name__ == "__main__":
    raise SystemExit(main())
