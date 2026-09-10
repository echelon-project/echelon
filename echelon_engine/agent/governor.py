"""governor.py — the CONTEXT GOVERNOR (v0). The B-arm organ of PAPER-0029.

THE INVERSION. Every existing recall door is PULL: the agent decides it wants
context, words an intent, and queries (`recall --warm "<intent>"`). That makes the
retrieval quality a function of how well the agent *phrases its wish* — and an
agent mid-step is exactly the worst narrator of what it needs.

The Governor is PUSH: the agent never queries, it is FED. Composition keys on the
EVENT (what actually happened this step — the task text, the files touched, the
commands run, the errors seen), never on intent wording. That is the whole point
(prereg P4): an event is observed, not authored, so the context arriving cannot be
poisoned by a badly-worded wish.

Grounded on two organs, inventing neither:
  - JOV (`EROS/ENGINE/context/jov.py`, the era's Context Governor) — the canary
    token, the FOVEA (a hard budget of what gets seen), and above all the
    `passed_over` audit trail: what the governor *declined* to show is evidence,
    and hiding it would make the arm unevaluable (prereg P5 accounting).
  - the fork-field pager (`agent/fork_field.py`) — the live warmth read against the
    bank. `make_bank_pager` scores ONE task string; compose() runs that same
    `atoms.warmth.warmth` seam once per FACET and keeps the best-scoring facet per
    atom. No new scorer, no embeddings, no new deps (judge_provider=None -> the
    deterministic lexical floor, $0).

CHARTER: compress the decision, never copy-whole. The package carries clipped
previews and scores — enough to re-form the weight — not atom bodies.

LAWS (spec-bound, and each one is load-bearing):
  - READ-ONLY on the bank. compose() never earns, never kindles, never writes an
    atom or a weight. The ledger is a FILE, not a bank write (sibling law:
    restore-is-not-a-write-path). warmth() is called with reinforce left off.
  - OPT-IN. Nothing existing changes behaviour; no hooks into any live loop yet.
  - DETERMINISTIC. Same StepEvent + same bank state -> same package, ties sorted
    stably (score desc, then slug, then id) and the canary derived from the event
    content rather than the clock.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Optional

# How many atoms the underlying warmth read is allowed to return per facet. warmth()
# takes a top_k and slices; the governor wants the whole scored tail so `passed_over`
# is a real audit trail and not a second, hidden fovea.
SCAN_TOP_K = 500
# The B'-arm (foveate=False) dump control is still bounded — an unbounded dump would
# blow the worker's window and stop being a control at all.
DUMP_CAP = 200
PREVIEW_CLIP = 160        # per-entry preview width (compress the decision)
ERROR_CLIP = 200          # a stack-trace line is not a query; truncate it to its head


@dataclass
class StepEvent:
    """What HAPPENED in a step. The composition key (P4) — observed, never authored.

    `task` is the same string the fork-field pager scores; the rest are the step's
    observable residue. `step_id` / `deps_done` carry DAG position for the ledger.
    """
    task: str
    files: list[str] = field(default_factory=list)
    commands: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    step_id: str = ""
    deps_done: list[str] = field(default_factory=list)


@dataclass
class GovernorPackage:
    """The composed context handed to the worker, plus the full accounting."""
    canary: str
    event: dict
    entries: list[dict]
    passed_over: list[dict]
    composed_text: str
    ts: int
    budget: int = 0
    foveate: bool = True
    scope: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


# ── facets: the event, decomposed into queries ─────────────────────────────────
def facets(event: StepEvent) -> list[tuple[str, str]]:
    """Decompose an event into (facet_name, query_text) pairs — one query per thing
    that actually happened. Order is stable (task, files, errors, commands) so the
    same event always produces the same facet sequence.

    A file becomes its BASENAME: the bank remembers `ingest.py`, not a machine-local
    absolute path, and a path would drag its directory tokens into the lexical score.
    """
    out: list[tuple[str, str]] = []
    if (event.task or "").strip():
        out.append(("task", event.task.strip()))
    for f in event.files:
        base = os.path.basename(str(f).replace("\\", "/").rstrip("/"))
        if base:
            out.append((f"file:{base}", base))
    for e in event.errors:
        line = " ".join(str(e).split())[:ERROR_CLIP]
        if line:
            out.append((f"error:{line[:60]}", line))
    for c in event.commands:
        cmd = " ".join(str(c).split())
        if cmd:
            # the HEAD of the command carries the meaning ('pytest tests/x.py' -> the
            # tail is a path, already covered by the file facet).
            out.append((f"cmd:{cmd.split()[0]}", cmd))
    # de-dup on the facet name, keeping first occurrence (determinism + no double count)
    seen: set[str] = set()
    uniq: list[tuple[str, str]] = []
    for name, q in out:
        if name in seen:
            continue
        seen.add(name)
        uniq.append((name, q))
    return uniq


def _slug(seed) -> str:
    coord = getattr(seed, "coordinate", "") or ""
    if coord:
        return coord.rsplit(":", 1)[-1]
    return str(getattr(seed, "id", "") or "")


def _preview(seed) -> str:
    """The clipped seed preview — first line (atoms are stored '[name] <desc>\\n\\n<body>'),
    squeezed to one line and clipped. The leading '[name]' tag is stripped because the
    entry already prints the slug — repeating it would spend budget on nothing.
    Compress the decision, never copy-whole."""
    content = getattr(seed, "content", "") or ""
    first = content.splitlines()[0] if content else ""
    first = " ".join(first.split())
    if first.startswith("["):
        end = first.find("]")
        if end != -1 and first[end + 1:].strip():
            first = first[end + 1:].strip()
    return first[:PREVIEW_CLIP]


def _canary(event: StepEvent, ids: list[str]) -> str:
    """JOV-style rotating token `#ECH:<hex>#`, derived from the EVENT (not the clock)
    so the determinism law holds: same event + same bank state -> same canary."""
    payload = json.dumps({"e": asdict(event), "ids": ids}, sort_keys=True, ensure_ascii=False)
    return "#ECH:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:10] + "#"


def _default_warmth_fn(judge_provider=None, judge_model: str = "grok-4.3", scope_graph=None):
    """The scoring seam — the engine's OWN warmth organ, exactly as the fork-field
    pager reads it, only asking for the whole scored tail instead of a top-3.
    judge_provider=None -> lexical floor: deterministic, $0, no new deps."""
    from echelon_engine.atoms.warmth import warmth as _warmth

    def score(query: str, store, scope: str):
        r = _warmth(query, store, scope, top_k=SCAN_TOP_K, judge_provider=judge_provider,
                    judge_model=judge_model, scope_graph=scope_graph)
        return list(getattr(r, "warmest", []) or [])

    return score


# ── compose: the organ ─────────────────────────────────────────────────────────
def room_slice(room) -> tuple[str, list[tuple[str, str]]]:
    """WORKSPACE BEFORE MEMORY (charter Part 2): the boot slice assembled FROM THE ROOM —
    brief (goal/where/next/open), open items oldest-first, unresolved incidents, the last
    journal line — plus the facets the cursor implies (task, working-set basenames) so the
    bank is asked about what the room says we are doing. Read-only; None -> ("", [])."""
    if room is None:
        return "", []
    try:
        from echelon_engine import workcycle as wc
        e = Path(room)
        lines = [wc.resume_brief(e)]
        opens = []
        for p in sorted((e / "open").glob("*.json")):
            d = wc._read_json(p, {}) or {}
            if d.get("id"):
                opens.append((d.get("opened") or "", f"{d['id']} {(d.get('text') or '')[:70]}"))
        for _, t in sorted(opens)[:5]:
            lines.append("  OPEN " + t)
        inc = [p for p in (e / "incidents").glob("*.json")
               if not (wc._read_json(p, {}) or {}).get("resolved")]
        if inc:
            lines.append(f"  INCIDENTS unresolved {len(inc)}: " + ", ".join(p.stem for p in inc[:3]))
        last = wc._last_journal_line(wc._latest_journal_file(e / "journal"))
        if last:
            lines.append(f"  JOURNAL last {last.get('kind')} @ {str(last.get('ts'))[:19]}")
        cursor = wc._read_json(e / "cursor.json", {}) or {}
        facets_: list[tuple[str, str]] = []
        if (cursor.get("task") or "").strip():
            facets_.append(("room:task", str(cursor["task"]).strip()))
        for f in cursor.get("working_set") or []:
            base = os.path.basename(str(f).replace("\\", "/").rstrip("/"))
            if base:
                facets_.append((f"file:{base}", base))
        return "\n".join(lines), facets_
    except Exception:
        return "", []   # a broken room must never wedge the governor (pager's law)


def compose(event: StepEvent, store, scope: str, *, budget: int = 15, foveate: bool = True,
            ledger_dir: Optional[str] = None,
            warmth_fn: Optional[Callable[[str, Any, str], list]] = None,
            room=None) -> GovernorPackage:
    """FEED a step. Score the bank once per facet, keep each atom's best facet, foveate
    to `budget`, and account for everything else in `passed_over`.

    READ-ONLY: nothing here earns, kindles, or writes to the bank.
    """
    score_fn = warmth_fn or _default_warmth_fn()

    # 1+2. facet queries -> per-atom MAX score, remembering which facet won.
    best: dict[str, dict] = {}
    room_text, room_facets = room_slice(room)
    seen_f = {n for n, _ in facets(event)}
    all_facets = facets(event) + [(n, q) for n, q in room_facets if n not in seen_f]
    for facet_name, query in all_facets:
        try:
            hits = score_fn(query, store, scope)
        except Exception:
            continue   # a bank hiccup must never wedge the governor (pager's law)
        for sw in hits:
            seed = getattr(sw, "seed", None)
            s = float(getattr(sw, "score", 0.0) or 0.0)
            if seed is None or s <= 0:
                continue
            sid = str(getattr(seed, "id", "") or _slug(seed))
            prior = best.get(sid)
            if prior is None or s > prior["score"]:
                best[sid] = {"id": sid, "slug": _slug(seed), "score": round(s, 4),
                             "facet": facet_name, "preview": _preview(seed)}

    # stable order: score desc, then slug, then id — ties never rotate between runs.
    ranked = sorted(best.values(), key=lambda d: (-d["score"], d["slug"], d["id"]))

    # 3. FOVEATE. The fovea is the arm; the dump control (foveate=False) is still capped.
    cut = max(0, int(budget)) if foveate else DUMP_CAP
    selected, rest = ranked[:cut], ranked[cut:]
    entries = [dict(e) for e in selected]
    passed_over = [{"id": d["id"], "slug": d["slug"], "score": d["score"], "facet": d["facet"]}
                   for d in rest]

    # 4. PACKAGE.
    canary = _canary(event, [e["id"] for e in entries])
    noun = "entry" if len(entries) == 1 else "entries"
    head = f"{canary} governed context — {len(entries)} {noun}"
    if (event.task or "").strip():
        head += f" (event: {event.task.strip()[:80]})"
    lines = [head]
    if room_text:
        lines += ["-- ROOM (state, first) --", room_text, "-- BANK (lessons) --"]
    for e in entries:
        lines.append(f"[{e['slug']}] {e['preview']}")
    composed_text = "\n".join(lines)

    pkg = GovernorPackage(canary=canary, event=asdict(event), entries=entries,
                          passed_over=passed_over, composed_text=composed_text,
                          ts=int(time.time()), budget=int(budget), foveate=bool(foveate),
                          scope=scope or "")

    # 5. LEDGER — the P1..P5 evaluation input. A FILE, not a bank write.
    if ledger_dir:
        write_ledger(pkg, ledger_dir)
    return pkg


def write_ledger(pkg: GovernorPackage, ledger_dir: str) -> str:
    """Persist the FULL package (entries with scores, passed_over, budget, foveate flag)
    as `governor-<ts>-<canary hex>.json`. Returns the path written."""
    os.makedirs(ledger_dir, exist_ok=True)
    tag = pkg.canary.strip("#").replace("ECH:", "")
    path = os.path.join(ledger_dir, f"governor-{pkg.ts}-{tag}.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(pkg.to_dict(), fh, ensure_ascii=False, indent=2, sort_keys=True)
    return path


# ── CLI ────────────────────────────────────────────────────────────────────────
def main(argv=None) -> int:
    """`echelon govern "<task>" [--file F].. [--cmd C].. [--error E].. [--budget N]
    [--no-foveate] [--ledger DIR] [--scope S] [--json]`

    Scope/store resolution mirrors the recall door: --db overrides the bank path,
    --scope names the scope, and an unnamed scope resolves from the cwd.
    """
    import argparse

    ap = argparse.ArgumentParser(
        prog="echelon govern",
        description="GOVERNOR v0 — compose per-step context from an EVENT (the agent is fed, "
                    "never queries). Read-only on the bank.")
    ap.add_argument("task", help="the step's action text (the composition key)")
    ap.add_argument("--file", dest="files", action="append", default=[],
                    help="a file the step touched (repeatable; matched by basename)")
    ap.add_argument("--cmd", dest="commands", action="append", default=[],
                    help="a command the step ran (repeatable)")
    ap.add_argument("--error", dest="errors", action="append", default=[],
                    help="an error line the step saw (repeatable)")
    ap.add_argument("--step-id", default="", help="DAG step id (recorded in the ledger)")
    ap.add_argument("--dep-done", dest="deps_done", action="append", default=[],
                    help="a dependency already finished (repeatable; recorded in the ledger)")
    ap.add_argument("--budget", type=int, default=15, help="fovea size — entries kept (default 15)")
    ap.add_argument("--no-foveate", action="store_true",
                    help="the B' dump-control arm: keep every scored atom (hard cap %d)" % DUMP_CAP)
    ap.add_argument("--ledger", default=None, metavar="DIR",
                    help="write the full package JSON here (the evaluation input)")
    ap.add_argument("--scope", default=None, help="memory scope (default: resolved from cwd)")
    ap.add_argument("--db", default=None, help="override bank db path (default ~/.echelon/echelon.db)")
    ap.add_argument("--no-room", action="store_true", help="skip the ROOM slice (bank only)")
    ap.add_argument("--json", dest="as_json", action="store_true",
                    help="print the full package as JSON instead of the composed block")
    a = ap.parse_args(argv)

    from echelon_engine.atoms.store import SeedStore
    from echelon_engine.atoms.resolve_scope import resolve_scope, UnknownScopeError

    if a.scope:
        scope = a.scope
    else:
        try:
            scope = resolve_scope(os.getcwd())
        except UnknownScopeError as e:
            # fail closed (OPEN-0036): composing a package against a scope nobody banked is
            # how atoms leaked into a minted scope. --scope is the explicit door.
            raise SystemExit(f"[governor] {e}\n  Pass --scope <name> to name the scope explicitly.")
    store = SeedStore(a.db) if a.db else SeedStore()

    event = StepEvent(task=a.task, files=list(a.files), commands=list(a.commands),
                      errors=list(a.errors), step_id=a.step_id, deps_done=list(a.deps_done))
    room = None
    if not a.no_room:
        from echelon_engine.workcycle import room_path
        room = room_path()
    pkg = compose(event, store, scope, budget=a.budget, foveate=not a.no_foveate,
                  ledger_dir=a.ledger, room=room)

    if a.as_json:
        print(json.dumps(pkg.to_dict(), ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(pkg.composed_text)
        print(f"\n-- entries {len(pkg.entries)} | passed_over {len(pkg.passed_over)} "
              f"| scope {scope} | foveate {pkg.foveate} | budget {pkg.budget}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
