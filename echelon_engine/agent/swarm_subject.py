"""swarm_subject — the SUBJECT-SWARM TRUNK: fan-out → partition → dispatch → leaf-split → verify, with a
swappable SUBJECT HEAD (ui / code / scribe / qc / …). The reusable, general half of the audit→fix pipeline.

THE SHAPE THE OWNER NAMED (2026-06-21): "swarm first, then branching to subject." workflow.py is the DAG
swarm (steps with deps, Gantt waves). THIS is the other swarm shape — a FAN-OUT over N independent units
(files/screens/modules), no deps, with a subject-specific frame+rules+output, hardened by the discipline a
real run earned this session:
  • NO-CAP FAN-OUT — one worker per unit, all parallel (the owner: "full power of swarm").
  • COLLISION-PARTITION — group writes so no two workers touch the same file (one-domain-one-file; the
    shared sink, e.g. app.css, is the BASE and each domain layers OVER it). The audit pass is read-only so
    it never collides; the FIX pass MUST be partitioned or parallel writers clobber.
  • LEAF-SPLIT — when one worker's output overflows its token budget it TRUNCATES (silent: parses to 0
    blocks). The fix is not a bigger budget, it is to SPLIT THE LEAF: re-dispatch that group one-unit-per-
    worker. Proven this session: a 13-file domain writer truncated; per-file writers all passed.
  • VERIFY-IT-RAN — the dispatcher is NOT the taste-maker; it ORCHESTRATES and proves the run is STRUCTURALLY
    sound (parse/scripts-preserved/no-invented-symbols/brace-balance), surfacing regressions instead of
    trusting the low-floor writer. (This session the verify gate caught a dropped <script> + an invented
    Jinja filter — both would have shipped silently.)

A SUBJECT HEAD supplies the four things that change per subject; the trunk supplies everything else:
  frame()   -> the consumer/context block (who this is for, the hard rules)
  rules()   -> the grading/standard the workers apply
  output()  -> the per-unit output contract (what each worker must emit)
  verify()  -> structural checks over the produced artifacts (returns list[str] of problems)
Heads register in HEADS; add a subject by writing a head, not by touching the trunk. This module is
provider-agnostic via a `send` callable (default: the Gemini provider) so it tests without a live key.

Layer: echelon_engine.agent (orchestration). See workflow.py (the DAG swarm sibling) and the
swarm-subject-pipeline card (the doctrine this code earns weight from).
"""
from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable


# ── the unit + the result ─────────────────────────────────────────────────────
@dataclass
class Unit:
    """One independent target of the fan-out (a screen, a file, a module). `key` is its stable id;
    `path` the artifact it owns; `payload` whatever the head needs (source text, findings, …)."""
    key: str
    path: Path | None = None
    payload: dict = field(default_factory=dict)


@dataclass
class WorkerResult:
    key: str
    ok: bool
    detail: str
    artifact: str | None = None     # what the worker produced (md report / new file content)
    truncated: bool = False         # the leaf-split trigger


# ── the subject head contract ─────────────────────────────────────────────────
@dataclass
class SubjectHead:
    """A subject (ui/code/scribe/qc). The trunk calls these; the head owns the subject knowledge."""
    name: str
    frame: Callable[[Unit], str]                 # consumer/context block for this unit
    rules: Callable[[Unit], str]                 # the standard the worker grades/builds against
    output: Callable[[Unit], str]                # the per-unit output contract
    parse: Callable[[str, Unit], str | None]     # pull the artifact out of the raw model text (None=truncated)
    verify: Callable[[list[WorkerResult]], list[str]]   # structural checks; [] = clean
    model: str = "gemini-3.5-flash"              # worker tier (judge=3.5-flash, writer=3-flash-preview; flash-LITE BANNED)
    max_tokens: int = 4000
    # how to PARTITION units so concurrent writers never share a file. Default: each unit is its own
    # partition (already disjoint, e.g. a read-only audit or per-file writes). A fix pass overrides this
    # to group-by-domain and route the shared sink to the base layer.
    partition: Callable[[list[Unit]], list[list[Unit]]] | None = None


HEADS: dict[str, SubjectHead] = {}


def register_head(head: SubjectHead) -> None:
    HEADS[head.name] = head


def get_head(name: str) -> SubjectHead | None:
    return HEADS.get(name)


# ── the trunk ─────────────────────────────────────────────────────────────────
def _default_send(prompt: str, model: str, max_tokens: int) -> str:
    """Lazy default sender: the estate's Gemini provider. Kept lazy so the module imports without the dep."""
    from echelon_engine.atoms.providers.gemini import GeminiProvider
    r = GeminiProvider().send([{"role": "user", "content": prompt}],
                              model_id=model, max_tokens=max_tokens, temperature=0.3)
    return r.content or ""


def _build_prompt(head: SubjectHead, unit: Unit) -> str:
    return (f"{head.frame(unit)}\n\n{head.rules(unit)}\n\n{head.output(unit)}\n\n"
            f"────────── UNIT: {unit.key} ──────────\n{unit.payload.get('source','')}\n"
            + (f"\n────────── PRIOR FINDINGS ──────────\n{unit.payload['findings']}\n"
               if unit.payload.get('findings') else ""))


def _run_one(head: SubjectHead, unit: Unit, send: Callable[[str, str, int], str]) -> WorkerResult:
    try:
        raw = send(_build_prompt(head, unit), head.model, head.max_tokens)
        artifact = head.parse(raw, unit)
        if artifact is None or not artifact.strip():
            return WorkerResult(unit.key, False, "truncated/no-block", truncated=True)
        return WorkerResult(unit.key, True, f"{len(artifact.splitlines())} lines", artifact=artifact)
    except Exception as e:  # noqa: BLE001
        return WorkerResult(unit.key, False, f"{type(e).__name__}: {e}")


def fan_out(head: SubjectHead, units: list[Unit],
            send: Callable[[str, str, int], str] | None = None,
            max_workers: int | None = None,
            leaf_split: bool = True,
            on_event: Callable[[str, dict], None] | None = None) -> list[WorkerResult]:
    """Run the subject swarm over `units`: no-cap parallel, collision-partitioned, leaf-split on overflow,
    then the head's verify gate. Returns the WorkerResults (caller applies/writes; the trunk stays the
    orchestrator, never the taste-maker)."""
    send = send or _default_send
    emit = on_event or (lambda ev, d: None)

    # 1. partition (collision-safety). default = each unit alone (already disjoint).
    groups = head.partition(units) if head.partition else [[u] for u in units]
    flat = [u for g in groups for u in g]
    emit("fanout_start", {"subject": head.name, "units": len(flat), "groups": len(groups),
                          "model": head.model})

    # 2. dispatch every unit, no cap (one worker per unit within every group, all parallel).
    results: dict[str, WorkerResult] = {}
    with ThreadPoolExecutor(max_workers=max_workers or max(1, len(flat))) as pool:
        futs = {pool.submit(_run_one, head, u, send): u for u in flat}
        for fut in as_completed(futs):
            res = fut.result()
            results[res.key] = res
            emit("unit_done", {"key": res.key, "ok": res.ok, "detail": res.detail})

    # 3. LEAF-SPLIT: any truncated unit is re-run alone (already alone here, but a head whose worker
    #    handled a MULTI-unit group would re-dispatch its members one-at-a-time). The trunk retries
    #    truncated keys once, isolated, with the same head.
    if leaf_split:
        truncated = [u for u in flat if results[u.key].truncated]
        if truncated:
            emit("leaf_split", {"count": len(truncated), "keys": [u.key for u in truncated]})
            with ThreadPoolExecutor(max_workers=max(1, len(truncated))) as pool:
                futs = {pool.submit(_run_one, head, u, send): u for u in truncated}
                for fut in as_completed(futs):
                    res = fut.result()
                    results[res.key] = res
                    emit("unit_retry", {"key": res.key, "ok": res.ok, "detail": res.detail})

    ordered = [results[u.key] for u in flat]

    # 4. VERIFY-IT-RAN (structural, the head owns the checks). Surface, don't swallow.
    problems = head.verify(ordered)
    emit("verify", {"problems": problems, "ok": not problems})
    return ordered


def run_subject(subject: str, units: list[Unit], **kw) -> tuple[list[WorkerResult], list[str]]:
    """Top-level branch: pick the head by subject name, run the trunk. Returns (results, verify_problems)."""
    head = get_head(subject)
    if head is None:
        raise ValueError(f"unknown subject {subject!r}; registered heads: {sorted(HEADS)}")
    results = fan_out(head, units, **kw)
    return results, head.verify(results)


# ── CLI: `python -m echelon_engine swarm-subject --subject ui:audit --dir <repo>/templates --glob '*.html'`
def _main(argv: list[str] | None = None) -> int:
    """Manifest-light CLI so the COMMAND adopts the pipeline. Builds one Unit per file matched by --glob in
    --dir, optionally injects --frame-file as the consumer context, runs the subject, writes artifacts to
    --out (audit) or back in place is left to the caller (act passes write through their own apply step).
    This is the headless front door; the card teaches the doctrine."""
    import argparse
    import sys
    from . import swarm_heads  # noqa: F401  — registers the ui heads

    ap = argparse.ArgumentParser(prog="echelon swarm-subject",
                                 description="fan-out a SUBJECT swarm (swarm-trunk-then-branch) over files")
    ap.add_argument("--subject", required=True, help="head name, e.g. ui:audit / ui:fix / code:audit / code:fix")
    ap.add_argument("--dir", required=True, help="root to glob units from")
    ap.add_argument("--glob", default=None, help="unit glob within --dir (default: *.py for code, *.html for ui)")
    ap.add_argument("--lang", default=None, help="fenced-block language for fix passes (default by subject)")
    ap.add_argument("--frame-file", default=None, help="file holding the project consumer context (the frame)")
    ap.add_argument("--findings-dir", default=None, help="for fix passes: dir of per-unit <key>.md findings")
    ap.add_argument("--out", default=None, help="audit: write each report to <out>/<key>.md")
    a = ap.parse_args(argv if argv is not None else sys.argv[1:])

    subject_kind = a.subject.split(":", 1)[0]   # 'ui' | 'code' | …
    glob = a.glob or ("*.py" if subject_kind == "code" else "*.html")
    lang = a.lang or ("python" if subject_kind == "code" else "html")
    root = Path(a.dir)
    frame = Path(a.frame_file).read_text(encoding="utf-8") if a.frame_file else None
    findings_dir = Path(a.findings_dir) if a.findings_dir else None
    units: list[Unit] = []
    for p in sorted(root.rglob(glob)):
        key = p.relative_to(root).as_posix().rsplit(".", 1)[0].replace("/", "__")
        payload = {"source": p.read_text(encoding="utf-8")}
        if frame:
            payload["frame"] = frame
        if findings_dir and (findings_dir / f"{key}.md").exists():
            payload["findings"] = (findings_dir / f"{key}.md").read_text(encoding="utf-8")
        units.append(Unit(key=key, path=p, payload=payload))

    if not units:
        print(f"swarm-subject: no units matched {a.glob} in {root}", file=sys.stderr)
        return 2

    # rebuild the head with the project frame + correct lang (so consumer/architecture context is right)
    head_name = a.subject
    mode = a.subject.split(":", 1)[1] if ":" in a.subject else "audit"
    from .swarm_heads import make_ui_head, make_code_head, register_head
    if subject_kind == "ui" and (frame or lang):
        register_head(make_ui_head(mode, project_frame=frame, lang=lang))
    elif subject_kind == "code" and (frame or lang):
        register_head(make_code_head(mode, project_frame=frame, lang=lang))

    print(f"[swarm-subject] {a.subject} over {len(units)} units from {root}")
    results = fan_out(get_head(head_name), units,
                      on_event=lambda ev, d: print(f"  · {ev}: {d}"))
    out = Path(a.out) if a.out else None
    if out:
        out.mkdir(parents=True, exist_ok=True)
    ok = 0
    for r in results:
        if r.ok:
            ok += 1
            if out and r.artifact:
                (out / f"{r.key}.md").write_text(r.artifact, encoding="utf-8")
    problems = get_head(head_name).verify(results)
    print(f"[swarm-subject] {ok}/{len(results)} ok | verify problems: {len(problems)}")
    for pr in problems:
        print(f"  VERIFY: {pr}")
    return 0 if ok == len(results) and not problems else 1
