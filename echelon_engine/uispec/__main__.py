#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
echelon uispec — the ChainBoard REWORK/DESIGN pipelines (gemini generate -> skeptic gate).

The judgment half of ChainBoard. `echelon pagemodel` reads a page into a deterministic GRAPH;
`echelon uispec` runs the PRO-model pipelines that turn that understanding into gated rework
artifacts — forensic audits, two-layer component specs, blueprint contracts, envelope migrations,
kit components — each paired with a skeptic gate run by a DIFFERENT model.

Verbs:
  audit      --os-dir D --slug S --out F           forensic audit + blueprint + scale sim (one page)
  audit-gate --os-dir D --slug S --doc F --out G   skeptic refutes the audit doc against real code
  synth      --slug S --audit F --gate G --out O   reconcile audit+gate -> build-ordered rework doc
  spec       --os-dir D --slug S --out F.json      two-layer component spec (JSON, validated)
  spec-gate  --os-dir D --slug S --spec F --out G  skeptic refutes the spec's dependency edges
  kit        --name N --contract C --refs a,b --spec "…" --out F   build one LXComponent
  kit-gate   --name N --component F --contract C --refs a,b --out G  gate the kit component
  run        --os-dir D --slugs a,b,c --out-dir O  BATCH worst-first: audit -> gate -> synth per page

All stages accept --model to override the default (gen=gemini-3.1-pro-preview, gate=gemini-2.5-pro)
and --max-tokens. Pure routing; the judgment lives in stages.py, the mechanics in _runner.py.
"""
from __future__ import annotations

import argparse
import pathlib
import sys

from echelon_engine.uispec import _runner as R
from echelon_engine.uispec._runner import GEN_MODEL, GATE_MODEL
from echelon_engine.uispec.stages import STAGES


def _role_defaults(role: str) -> tuple[str, float]:
    return (GEN_MODEL, 0.3) if role == "gen" else (GATE_MODEL, 0.1)


def _run_gen_stage(stage: str, page: R.PageCode, out: str, *,
                   model: str | None, max_tokens: int, extra_ctx: str = "",
                   slug_fmt: bool = False) -> R.RunResult:
    """A single GENERATE stage over one page: task + page code (+ extra ctx) -> markdown out."""
    task, role = STAGES[stage]
    if slug_fmt:
        task = task.format(slug=page.slug)
    m, temp = _role_defaults(role)
    prompt = f"{task}\n\n=== PAGE SLUG: {page.slug} ===\n\n{page.block()}"
    if extra_ctx:
        prompt += f"\n{extra_ctx}\n"
    r = R.send(prompt, model=model or m, temperature=temp, max_tokens=max_tokens, label=stage)
    if not r.ok:
        sys.exit(1)
    R.write_markdown(out, r.content, header=R.provenance(stage, page.slug, r))
    return r


def _run_gate_stage(stage: str, page: R.PageCode, artifact_label: str, artifact: str,
                    out: str, *, model: str | None, max_tokens: int) -> R.RunResult:
    """A single GATE stage: task + original page code (A) + artifact under review (B) -> verdict."""
    task, role = STAGES[stage]
    m, temp = _role_defaults(role)
    prompt = (f"{task}\n\n"
              f"=== (A) ORIGINAL PAGE CODE — {page.slug} ===\n{page.block()}\n\n"
              f"=== (B) {artifact_label} ===\n{artifact}\n")
    r = R.send(prompt, model=model or m, temperature=temp, max_tokens=max_tokens, label=stage)
    if not r.ok:
        sys.exit(1)
    R.write_markdown(out, r.content, header=R.provenance(stage, page.slug, r))
    return r


# ── verbs ───────────────────────────────────────────────────────────────────
def _cmd_audit(a) -> int:
    page = R.resolve_page(a.os_dir, a.slug)
    _run_gen_stage("audit", page, a.out, model=a.model, max_tokens=a.max_tokens)
    return 0


def _cmd_audit_gate(a) -> int:
    page = R.resolve_page(a.os_dir, a.slug)
    _run_gate_stage("audit-gate", page, "THE AUDIT DOC TO GATE", R.read(a.doc),
                    a.out, model=a.model, max_tokens=a.max_tokens)
    return 0


def _cmd_synth(a) -> int:
    task, _ = STAGES["synth"]
    council = f"\n=== BINDING COUNCIL DECISION (honor exactly) ===\n{a.council}\n" if a.council else ""
    prompt = (f"{task}\n\n=== PAGE SLUG: {a.slug} ===\n\n"
              f"=== (A) AUDIT DOC ===\n{R.read(a.audit)}\n\n"
              f"=== (B) SKEPTIC GATE VERDICT ===\n{R.read(a.gate)}\n{council}")
    m, temp = _role_defaults("gen")
    r = R.send(prompt, model=a.model or m, temperature=temp, max_tokens=a.max_tokens, label="synth")
    if not r.ok:
        return 1
    R.write_markdown(a.out, r.content, header=R.provenance("synth", a.slug, r))
    return 0


def _cmd_spec(a) -> int:
    page = R.resolve_page(a.os_dir, a.slug)
    task, role = STAGES["spec"]
    task = task.format(slug=page.slug)
    m, temp = _role_defaults(role)
    prompt = f"{task}\n\n{page.block()}"
    r = R.send(prompt, model=a.model or m, temperature=temp, max_tokens=a.max_tokens, label="spec")
    if not r.ok:
        return 1
    _, obj = R.write_json(a.out, r.content)
    print(f"[uispec] spec {page.slug}: {len(obj.get('blocks', []))} blocks, "
          f"{len(obj.get('clusters', []))} clusters", file=sys.stderr)
    return 0


def _cmd_spec_gate(a) -> int:
    page = R.resolve_page(a.os_dir, a.slug)
    _run_gate_stage("spec-gate", page, "THE COMPONENT SPEC (JSON) TO GATE", R.read(a.spec),
                    a.out, model=a.model, max_tokens=a.max_tokens)
    return 0


def _cmd_kit(a) -> int:
    task, role = STAGES["kit"]
    refs = "\n\n".join(f"=== REFERENCE: {pathlib.Path(p).name} ===\n{R.read(p)}"
                       for p in (a.refs.split(",") if a.refs else []))
    prompt = (f"{task}\n\n=== REGISTRY CONTRACT ===\n{R.read(a.contract)}\n\n{refs}\n\n"
              f"=== COMPONENT TO BUILD: {a.name} ===\n{a.spec}\n")
    m, temp = _role_defaults(role)
    r = R.send(prompt, model=a.model or m, temperature=temp, max_tokens=a.max_tokens, label="kit")
    if not r.ok:
        return 1
    R.write_markdown(a.out, R.strip_fence(r.content))  # component JS, fence-stripped, no header
    return 0


def _cmd_kit_gate(a) -> int:
    task, role = STAGES["kit-gate"]
    refs = "\n\n".join(f"=== REFERENCE: {pathlib.Path(p).name} ===\n{R.read(p)}"
                       for p in (a.refs.split(",") if a.refs else []))
    m, temp = _role_defaults(role)
    prompt = (f"{task}\n\n=== REGISTRY CONTRACT ===\n{R.read(a.contract)}\n\n{refs}\n\n"
              f"=== (B) COMPONENT UNDER REVIEW: {a.name} ===\n{R.read(a.component)}\n")
    r = R.send(prompt, model=a.model or m, temperature=temp, max_tokens=a.max_tokens, label="kit-gate")
    if not r.ok:
        return 1
    R.write_markdown(a.out, r.content)
    return 0


def _cmd_run(a) -> int:
    """BATCH worst-first: for each slug, audit -> gate -> synth, into out-dir/<slug>.rework.md."""
    out_dir = pathlib.Path(a.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    slugs = [s.strip() for s in a.slugs.split(",") if s.strip()]
    print(f"[uispec run] {len(slugs)} page(s), worst-first: {', '.join(slugs)}", file=sys.stderr)
    results = []
    for i, slug in enumerate(slugs, 1):
        print(f"\n[uispec run] ── ({i}/{len(slugs)}) {slug} ──", file=sys.stderr)
        try:
            page = R.resolve_page(a.os_dir, slug)
        except SystemExit as e:
            print(f"[uispec run] SKIP {slug}: {e}", file=sys.stderr)
            results.append((slug, "skipped"))
            continue
        audit_p = str(out_dir / f"{slug}.audit.md")
        gate_p = str(out_dir / f"{slug}.gate.md")
        rework_p = str(out_dir / f"{slug}.rework.md")
        _run_gen_stage("audit", page, audit_p, model=a.gen_model, max_tokens=a.gen_tokens)
        _run_gate_stage("audit-gate", page, "THE AUDIT DOC TO GATE", R.read(audit_p),
                        gate_p, model=a.gate_model, max_tokens=a.gate_tokens)
        # synth
        task, _ = STAGES["synth"]
        prompt = (f"{task}\n\n=== PAGE SLUG: {slug} ===\n\n"
                  f"=== (A) AUDIT DOC ===\n{R.read(audit_p)}\n\n"
                  f"=== (B) SKEPTIC GATE VERDICT ===\n{R.read(gate_p)}\n")
        m, temp = _role_defaults("gen")
        rr = R.send(prompt, model=a.gen_model or m, temperature=temp,
                    max_tokens=a.gen_tokens, label="synth")
        if rr.ok:
            R.write_markdown(rework_p, rr.content, header=R.provenance("synth", slug, rr))
            results.append((slug, "ok"))
        else:
            results.append((slug, "synth-failed"))
    print("\n[uispec run] ==== RESULT ====", file=sys.stderr)
    for slug, st in results:
        print(f"  {st:14s} {slug}", file=sys.stderr)
    return 0 if all(st == "ok" for _, st in results) else 1


# ── argparse wiring ──────────────────────────────────────────────────────────
def _main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(
        prog="echelon uispec",
        description="ChainBoard rework/design pipelines: gemini generate -> skeptic gate. "
                    "Sibling of `echelon pagemodel` (the deterministic analysis layer).",
    )
    sub = ap.add_subparsers(dest="cmd", required=True)

    def _common(p, os_dir=True, slug=True, model_default=None):
        if os_dir:
            p.add_argument("--os-dir", required=True, help="OS page dir (<slug>.html + assets/page-<slug>.js).")
        if slug:
            p.add_argument("--slug", required=True, help="Page slug (e.g. products).")
        p.add_argument("--out", required=True, help="Output artifact path.")
        p.add_argument("--model", default=model_default, help="Override the stage's model.")
        p.add_argument("--max-tokens", type=int, default=8000, help="Max output tokens.")

    p = sub.add_parser("audit", help="forensic audit + blueprint + scale sim (one page)")
    _common(p); p.set_defaults(fn=_cmd_audit)

    p = sub.add_parser("audit-gate", help="skeptic refutes the audit doc against real code")
    _common(p); p.add_argument("--doc", required=True, help="The audit doc to gate.")
    p.set_defaults(fn=_cmd_audit_gate, max_tokens=4000)

    p = sub.add_parser("synth", help="reconcile audit+gate -> build-ordered rework doc")
    p.add_argument("--slug", required=True); p.add_argument("--audit", required=True)
    p.add_argument("--gate", required=True); p.add_argument("--out", required=True)
    p.add_argument("--council", default="", help="Optional binding council decision text.")
    p.add_argument("--model", default=None); p.add_argument("--max-tokens", type=int, default=12000)
    p.set_defaults(fn=_cmd_synth)

    p = sub.add_parser("spec", help="two-layer component spec (validated JSON)")
    _common(p); p.set_defaults(fn=_cmd_spec, max_tokens=9000)

    p = sub.add_parser("spec-gate", help="skeptic refutes the spec's dependency edges")
    _common(p); p.add_argument("--spec", required=True, help="The spec JSON to gate.")
    p.set_defaults(fn=_cmd_spec_gate, max_tokens=6000)

    p = sub.add_parser("kit", help="build one LXComponent registry component")
    p.add_argument("--name", required=True); p.add_argument("--contract", required=True)
    p.add_argument("--refs", default="", help="Comma-separated reference component paths.")
    p.add_argument("--spec", required=True, help="Free-text spec of the component to build.")
    p.add_argument("--out", required=True); p.add_argument("--model", default=None)
    p.add_argument("--max-tokens", type=int, default=6000)
    p.set_defaults(fn=_cmd_kit)

    p = sub.add_parser("kit-gate", help="gate a kit component against the v1.2 contract")
    p.add_argument("--name", required=True); p.add_argument("--component", required=True)
    p.add_argument("--contract", required=True); p.add_argument("--refs", default="")
    p.add_argument("--out", required=True); p.add_argument("--model", default=None)
    p.add_argument("--max-tokens", type=int, default=4000)
    p.set_defaults(fn=_cmd_kit_gate)

    p = sub.add_parser("run", help="BATCH worst-first: audit -> gate -> synth per page")
    p.add_argument("--os-dir", required=True); p.add_argument("--slugs", required=True,
                   help="Comma-separated slugs, worst-first.")
    p.add_argument("--out-dir", required=True)
    p.add_argument("--gen-model", default=None); p.add_argument("--gate-model", default=None)
    p.add_argument("--gen-tokens", type=int, default=8000); p.add_argument("--gate-tokens", type=int, default=4000)
    p.set_defaults(fn=_cmd_run)

    args = ap.parse_args(argv)
    return args.fn(args) or 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
