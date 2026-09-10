"""swarm — the ECHELON swarm command: plan, council, skeptic.

Usage:
  echelon swarm --plan "<goal>" [--context "..."] [--files f1 f2] [--output report.md]
  echelon swarm --council --on plan_report.md
  echelon swarm --skeptic --on plan_report.md

The swarm is packaged as one command with three flags. Each flag triggers a
different orchestration pattern, all sharing the same cache-optimized context
builder and cartridge-equip pipeline.

FLOW:
  swarm --plan     → fan-out goal analysis (6 lenses, parallel)
    ↓
  swarm --council  → multi-model deliberation critique
    ↓
  swarm --skeptic  → red-team break-the-plan review
    ↓
  (iterate: fix plan → re-run plan → council → skeptic)
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys
from pathlib import Path


# ── Codebase scanner ───────────────────────────────────────────────────────────

_SOURCE_EXTS = {".py", ".js", ".ts", ".tsx", ".jsx", ".html", ".css", ".scss",
                ".json", ".yaml", ".yml", ".toml", ".md", ".sql", ".sh", ".ps1",
                ".go", ".rs", ".java", ".c", ".h", ".cpp", ".hpp"}
_SKIP_DIRS = {"node_modules", ".git", "__pycache__", ".venv", "venv", "dist",
              "build", ".next", ".turbo", "target", ".tox", ".eggs",
              "*.egg-info", ".mypy_cache", ".pytest_cache", ".ruff_cache"}
_SKIP_PREFIXES = (".",)
_MAX_TOTAL_BYTES = 256_000   # hard cap on total file content injected
_MAX_FILE_BYTES = 32_000     # per-file cap


def _scan_root(root: str) -> list[str]:
    """Scan a directory for source files. Returns list of relative paths."""
    root_path = Path(root).resolve()
    if not root_path.is_dir():
        print(f"warning: --root {root} is not a directory, skipping scan",
              file=sys.stderr)
        return []
    files: list[Path] = []
    for p in root_path.rglob("*"):
        if not p.is_file():
            continue
        # Skip hidden dirs and known noise
        parts = set(p.relative_to(root_path).parts)
        if parts & _SKIP_DIRS:
            continue
        if any(pp.startswith(_SKIP_PREFIXES) for pp in p.parts):
            continue
        if p.suffix.lower() in _SOURCE_EXTS:
            files.append(p)
    # Sort by extension then name for stable output
    files.sort(key=lambda f: (f.suffix, str(f)))
    return [str(f.relative_to(root_path)) for f in files]


def _read_files(root: str, file_list: list[str]) -> str:
    """Read file contents and return a formatted context block.
    Caps total size at _MAX_TOTAL_BYTES and per-file at _MAX_FILE_BYTES."""
    root_path = Path(root).resolve() if root else Path.cwd()
    blocks: list[str] = []
    total = 0
    for fname in file_list:
        fpath = root_path / fname if root else Path(fname)
        if not fpath.is_file():
            # Try as direct path
            fpath = Path(fname)
        if not fpath.is_file():
            continue
        try:
            content = fpath.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        if len(content) > _MAX_FILE_BYTES:
            content = content[:_MAX_FILE_BYTES] + "\n... [truncated]"
        lang = fpath.suffix.lstrip(".")
        blocks.append(f"### {fname}\n```{lang}\n{content}\n```")
        total += len(content)
        if total > _MAX_TOTAL_BYTES:
            blocks.append(f"\n... [remaining files skipped — {total} bytes cap reached]")
            break
    return "\n\n".join(blocks)


_EXAMPLES = """\
THE TWO MODES (choose first — what IS a seat?):
  api    each seat is ONE direct provider call reading frozen context (cheap,
         parallel, no tools). The read-kind lenses live here.
  agent  each seat is an EQUIPPED AGENT (partner.dispatch): tools, acts in a
         --folder, outcome-verified. Cartridges are the seat's whole equip.

EXAMPLES (copy-paste shapes):
  echelon swarm api --type audit --goal "audit the sync module" \\
      --root . --pick "echelon_engine/sync*.py"
  echelon swarm api --type council --on PLAN.md
  echelon swarm api --type plan --goal "design a rate limiter" --cartridge architect
  echelon swarm agent --cartridge scribe --goal "document the swarm package" --folder .
  echelon swarm types            # every registered type (read/execute/author kind)
  echelon swarm api --board      # the routing recipe board (rungs, costs, gates)

THE CONTEXT SNIPER — build the block yourself, or pay for nothing
─────────────────────────────────────────────────────────────────
This is a RAW API HIT. It does not open your files for you. The lenses read the
DYNAMIC block (--context / --context-file). Everything else is frozen and
cache-pinned. Hand them nothing and they still print [OK], answering from the
goal string alone with every finding stamped "Evidence: NOT CHECKED".

  # 1. SNIPE the bytes — only the thing being compared, each slice LABELLED
  {
    echo "===== db_pg.py:2405-2440  product_cogs_map ====="
    sed -n '2405,2440p' module/atoms/db_pg.py
    echo "===== sql_primitives.py:240-262  base_cost subquery ====="
    sed -n '240,262p'  module/atoms/sql_primitives.py
  } > ctx.txt

  # 2. FIRE (--context-file has no argv limit; --context caps at ~32KB)
  echelon swarm api --type audit --context-file ctx.txt --output REPORT.md \\
    --goal "For EVERY PAIR state AGREE or DIVERGE and quote the exact differing
            clause. Do NOT speculate about code not shown."

  # 3. GATE it — a generator cannot validate itself
  echelon swarm api --skeptic --on REPORT.md --goal "Which findings are the SAME
     finding recounted? Which rest on code NOT shown? Which 'divergences' are
     documented intentional design?"

VERIFY EVERY RUN (three cheap checks — the failures are SILENT):
  · the tokenomics "in N" must JUMP above the cached baseline (~24-27k)
  · grep -c "NOT CHECKED" REPORT.md          → want 0
  · grep -cE 'yourfile\\.py:[0-9]+' REPORT.md → want > 0
  If findings name no real symbol from your block, the block never rode.

SIZING (dossier §4.3): grain is a SECOND knob and volume can HURT. The one
measured rescue came at the SMALLEST grain (220 chars) and SMALLEST budget
(5 atoms) — context SUBSTITUTED for escalating to a dearer model at zero API
premium. The same budget at coarse grain ballooned to 48k chars for a one-line
task: "more dilution than the item has signal". WHOLE-DOMAIN BUT CURATED —
strip everything that is not the thing being compared.

WHY IT IS WORTH IT (dossier §2.1/§3, all [CONFIRMED]): config choice spans 17x
in cost at delta-Q = 0.0000 — equal quality at 1/17th the cost. Effort is NOT
monotonic: flash-HIGH ($0.0053) beat flash-LOW ($0.0110) because low effort
emitted 2.6x the output tokens. N lenses run in PARALLEL — six questions in the
wall-clock of one. ⚠ The blind judging pass never ran, so there is NO validated
quality claim: gate the findings, always.
FULL EVIDENCE: DEEPSEEK-EVIDENCE-DOSSIER.md — §4.3 grain, §2.1 cost, §7 what is
NOT wired into this verb (no tier routing, no screen-then-climb; cost meter only).

CONTEXT IS SELECTED, NEVER SLURPED: a bare --root is refused — narrow with
--pick globs or declare --budget-kb / --all. Every dispatch prints the
CONTEXT RECEIPT (what rode, what was cut, the manifest hash).
"""


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv

    # ── Subcommands: the mode fork first, then the registry verbs ─────────
    if argv and argv[0] == "types":
        return _cmd_types()
    if argv and argv[0] == "register":
        return _cmd_register(argv[1:])
    if argv and argv[0] == "unregister":
        return _cmd_unregister(argv[1:])
    if argv and argv[0] == "agent":
        return _agent_main(argv[1:])
    if argv and argv[0] == "api":
        argv = argv[1:]
    elif argv and not argv[0].startswith("-"):
        print(f"echelon swarm: unknown mode {argv[0]!r} — modes are api | agent "
              f"(plus: types, register, unregister)", file=sys.stderr)
        print(_EXAMPLES, file=sys.stderr)
        return 2

    ap = argparse.ArgumentParser(
        prog="echelon swarm api",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description="Direct-call swarm: N read-kind lenses over frozen, receipted context.",
        epilog=_EXAMPLES)
    ap.add_argument("--type", default="",
                    help="Swarm type to run (`echelon swarm types` lists all). "
                         "NOTE: a cartridge is not a type — equip one with --cartridge.")
    ap.add_argument("--cartridge", action="append", default=[],
                    metavar="NAME",
                    help="Equip this cartridge into every seat's frozen block "
                         "(repeatable; replaces the type's default equip)")
    ap.add_argument("--pick", action="append", default=[], metavar="GLOB",
                    help="With --root: only files matching this glob ride as "
                         "context (repeatable). The anti-slurp narrowing.")
    ap.add_argument("--budget-kb", type=int, default=None, metavar="N",
                    help="Context byte budget in KB (default 128). Explicitly "
                         "declaring it also satisfies the anti-slurp law.")
    ap.add_argument("--all", action="store_true",
                    help="Declare a whole-tree --root slurp ON PURPOSE (still "
                         "budgeted and receipted)")
    ap.add_argument("--board", action="store_true",
                    help="Print the routing recipe board (rungs, costs, gates) and exit")
    ap.add_argument("--plan", action="store_true",
                    help="Shortcut for --type plan")
    ap.add_argument("--council", action="store_true",
                    help="Shortcut for --type council")
    ap.add_argument("--skeptic", action="store_true",
                    help="Shortcut for --type skeptic")
    ap.add_argument("--goal", default="", help="The goal to analyze")
    ap.add_argument("--context", default="",
                    help="THE DYNAMIC BLOCK — the only door for source you want the "
                         "lenses to actually READ. ⚠ HARD CAP ~32KB: this rides in argv, "
                         "so a bigger block dies 'Argument list too long' (measured: 68KB "
                         "failed). For anything larger use --context-file, or split into "
                         "sub-32KB chunks and run one dispatch each.")
    ap.add_argument("--context-file", dest="context_file", default="", metavar="PATH",
                    help="Same as --context but read from a FILE — no argv limit. "
                         "Use this for big curated blocks (whole DDL, a route table). "
                         "Appended after --context when both are given.")
    ap.add_argument("--root", default="",
                    help="Scan this directory for source files to include as context. "
                         "⚠ Rides the FROZEN block, which is cache-pinned and may be "
                         "byte-identical across dispatches — verify with the CONTEXT "
                         "RECEIPT that your files actually rode.")
    ap.add_argument("--file", dest="file_list", nargs="*", default=None,
                    help="Specific files to include in analysis context. ⚠ Same frozen-block "
                         "caveat as --root: if the receipt hash does not change when you "
                         "change --file, nothing loaded — pass the code via --context-file.")
    ap.add_argument("--on", dest="on_file", default="",
                    help="Read a PLAN from this file (for council/skeptic chaining). "
                         "⚠ NOT a source-loader: --on feeds the deliberation seats, not the "
                         "audit lenses. Passing source here yields confident findings about "
                         "code nothing read. Use --context-file for source.")
    ap.add_argument("--nest", action="append", default=[], metavar="PATH",
                    help="THE TOKENOMICS HACK (measured 2026-08-19). Repeatable: give N "
                         "context files that are TRUE BYTE-PREFIXES of each other "
                         "(n1 ⊂ n2 ⊂ n3). They are sorted shortest-first and dispatched "
                         "SERIALLY, so each call reads the previous call's prefix from "
                         "cache — measured 18%% → 74%% cache on the first pass (37%% cheaper), "
                         "and ~99.8%% on a re-run over the same bytes. Refuses to run if the "
                         "files are not truly nested. Costs wall-clock (serial, ~3x); use "
                         "plain --context-file + parallel when latency matters. "
                         "--output NAME.md writes NAME.1.md, NAME.2.md, ...")
    ap.add_argument("--output", default="", help="Write report to this file")
    ap.add_argument("--provider", default="auto", help="LLM provider")
    ap.add_argument("--model", default="", help="Model override")
    ap.add_argument("--effort", default="", choices=["", "low", "high", "max", "none"],
                    help="Reasoning effort for DeepSeek workers ('' = provider "
                         "default). NOT monotonic in cost: measured 2026-08-17, "
                         "flash-high cost HALF of flash-low because low effort "
                         "emitted 2.6x the output tokens. Other providers ignore it.")
    ap.add_argument("--route", action="store_true",
                    help="Print the routing decision for --goal and exit. Spends nothing "
                         "— the board is pure, so you can audit a rung before paying for it.")
    ap.add_argument("--no-screen", action="store_true",
                    help="Skip cheap screening; start at the strong rung. (A veto path "
                         "never screens anyway.)")
    ap.add_argument("--design", default="",
                    help="JSON task graph file for execute-kind types (e.g., board)")
    ap.add_argument("--folder", default="",
                    help="Repository folder to execute board work in")
    ap.add_argument("--timeout", type=int, default=300,
                    help="Per-worker timeout in seconds")
    a = ap.parse_args(argv)

    # ── --context-file: the no-argv-limit door for the DYNAMIC block ──────
    # THE OPUS TRAP (owner 2026-08-19): --context rides argv, so a 68KB curated
    # block died "Argument list too long" — and the fallbacks fail SILENTLY:
    # --file/--root ride the cache-pinned FROZEN block (three runs with three
    # different --file args produced a byte-identical hash), and --on feeds the
    # deliberation seats, not the audit lenses. Both still print [OK] while the
    # lenses answer from the goal string alone. A file read here has no limit.
    if a.context_file:
        try:
            _extra = pathlib.Path(a.context_file).read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            print(f"echelon swarm: --context-file unreadable: {exc}", file=sys.stderr)
            return 2
        if not _extra.strip():
            print(f"echelon swarm: --context-file {a.context_file!r} is EMPTY — "
                  f"refusing to dispatch lenses over nothing (they would still "
                  f"report OK while speculating).", file=sys.stderr)
            return 2
        a.context = (a.context + "\n\n" + _extra) if a.context else _extra
        print(f"   context-file: {a.context_file} ({len(_extra):,} chars rode the dynamic block)",
              file=sys.stderr)

    # ── --nest: serial dispatch over TRUE byte-prefix contexts ────────────
    # Measured 2026-08-19 on 4 nested DDL chunks (7.3KB→69.8KB, 6 lenses each):
    #   all-parallel cold : 57s  $0.187  cached 23,808 @ n4  (18%)
    #   SERIAL NESTED     : 168s $0.117  cached 96,768 @ n4  (74%)  <- this mode
    #   re-run when warm  : 56s  $0.073  cached 130,560       (99.8%)
    # Pipelining (prime then fan out) was TESTED AND REFUTED: the cache lands
    # per-COMPLETED-REQUEST, so concurrent calls race and cannot warm each other
    # — it came out slower than parallel AND dearer than serial. Don't add it.
    if a.nest:
        paths = [pathlib.Path(p) for p in a.nest]
        try:
            blobs = [(p, p.read_bytes()) for p in paths]
        except OSError as exc:
            print(f"echelon swarm: --nest unreadable: {exc}", file=sys.stderr)
            return 2
        blobs.sort(key=lambda pb: len(pb[1]))          # shortest first
        for (pa, ba), (pb, bb) in zip(blobs, blobs[1:]):
            if not bb.startswith(ba):
                print(f"echelon swarm: --nest REFUSED — {pa.name} is not a true byte-prefix "
                      f"of {pb.name}. Nesting is what buys the cache; 'contains the same "
                      f"content' is NOT a prefix. Build them cumulatively:\n"
                      f"  cat c1 > n1 ; cat c1 c2 > n2 ; cat c1 c2 c3 > n3", file=sys.stderr)
                return 2
        print(f"   nest: {len(blobs)} contexts verified as true byte-prefixes, "
              f"dispatching SERIALLY shortest-first "
              f"({', '.join(str(len(b)) for _, b in blobs)} bytes)", file=sys.stderr)
        base_out = a.output
        rc = 0
        for idx, (p, blob) in enumerate(blobs, start=1):
            sub = list(argv)
            for flag in ("--nest", "--output"):        # strip; re-added per-run
                while flag in sub:
                    i = sub.index(flag)
                    del sub[i:i + 2]
            sub += ["--context-file", str(p)]
            if base_out:
                stem = base_out[:-3] if base_out.endswith(".md") else base_out
                sub += ["--output", f"{stem}.{idx}.md"]
            print(f"   nest[{idx}/{len(blobs)}] {p.name} ({len(blob):,} bytes)", file=sys.stderr)
            rc = main(sub) or rc
        return rc

    if a.board:
        from .recipe import board as _recipe_board
        print(_recipe_board())
        return 0

    # ── Resolve swarm type ────────────────────────────────────────────────
    from .types import get as get_type, all_types
    type_name = a.type
    if a.plan:
        type_name = "plan"
    elif a.council:
        type_name = "council"
    elif a.skeptic:
        type_name = "skeptic"

    if not type_name:
        if a.goal:
            type_name = "plan"
        elif a.on_file:
            type_name = "council"
        else:
            _print_swarm_help()
            return 1

    st = get_type(type_name)
    if st is None:
        print(f"Unknown swarm type: {type_name}", file=sys.stderr)
        # THE OPUS TRAP (owner 2026-08-19): `--type scribe` failed with only
        # "unknown type" — but scribe IS a cartridge. Say so, with both doors.
        if _is_cartridge(type_name):
            print(f"\n`{type_name}` is a CARTRIDGE, not a swarm type. Two doors:",
                  file=sys.stderr)
            print(f"  equip it into read-kind seats:", file=sys.stderr)
            print(f"    echelon swarm api --type audit --cartridge {type_name} "
                  f"--goal \"...\" [--root DIR --pick GLOB]", file=sys.stderr)
            print(f"  or give it an agent seat (tools, acts in a folder):",
                  file=sys.stderr)
            print(f"    echelon swarm agent --cartridge {type_name} "
                  f"--goal \"...\" --folder .", file=sys.stderr)
            return 2
        print(f"Available types: {', '.join(t.name for t in all_types())}",
              file=sys.stderr)
        print(f"Run `echelon swarm types` to list all.", file=sys.stderr)
        return 2

    # ── ROUTE ─────────────────────────────────────────────────────────────
    # The board decides (rung, gate, context) from the goal's CLASS, floored by
    # irreversibility. An explicit --effort is an operator override: it may choose to
    # pay MORE, but a veto's gate requirement is not negotiable. Routing is pure —
    # --route prints the decision and exits without spending anything.
    from .recipe import route as _route, explain as _explain
    # Classify on the goal; --on plan text is reviewed material, not the act being routed.
    rt = _route(a.goal, kind=st.kind, mode=st.mode,
                force_rung="", allow_screen=not a.no_screen)
    if a.effort:
        rt.why.append(f"--effort {a.effort} overrides the routed effort "
                      f"({rt.effort}); the gate requirement is unchanged")

    if a.route:
        print(_explain(rt))
        if a.effort:
            print(f"  NOTE: --effort {a.effort} would override effort={rt.effort}")
        return 0

    effective_effort = a.effort or rt.effort
    print(_explain(rt))
    print()

    # ── Context: SELECTED, BUDGETED, RECEIPTED (never slurped) ────────────
    from .manifest import build as _build_manifest, SlurpRefused
    file_list: list[str] = list(a.file_list) if a.file_list else []
    try:
        mani = _build_manifest(root=a.root, files=file_list, picks=a.pick,
                               budget_kb=a.budget_kb, allow_all=a.all)
    except SlurpRefused as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    except NotADirectoryError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    if mani.included or mani.excluded:
        print(mani.receipt())
        file_list = [rel for rel, _ in mani.included]
    if mani.text:
        a.context = (a.context + "\n\n## CODEBASE\n" + mani.text) if a.context \
            else ("## CODEBASE\n" + mani.text)

    # ── Load plan text if --on is given ───────────────────────────────────
    plan_text = ""
    if a.on_file:
        try:
            plan_text = Path(a.on_file).read_text(encoding="utf-8")
            print(f"   loaded plan from {a.on_file} ({len(plan_text)} chars)")
        except FileNotFoundError:
            print(f"error: file not found: {a.on_file}", file=sys.stderr)
            return 1

    # ── Dispatch by kind ──────────────────────────────────────────────────
    kind_tag = "⚡" if st.kind in ("execute", "author") else "📖"
    print(f"⚡ ECHELON SWARM — {st.name} (kind={st.kind}, mode={st.mode})")
    print(f"   {st.description}")

    # ── AUTHOR kind: produce a large artifact by driving the agent loop stepwise ──
    if st.kind == "author":
        from .author import run_author_swarm
        result = run_author_swarm(
            st,
            goal=a.goal or plan_text[:500],
            context=a.context,
            folder=a.folder or "",
            output=a.output,
            provider=a.provider,
            timeout=a.timeout,
        )
        # honest exit code: author succeeds only when the artifact actually changed on disk
        from .dispatch import METER as _METER
        print(_METER.report())
        return 0 if result.get("verified") else 1

    # ── EXECUTE kind: managed work through the shared ledger ───────────────
    if st.kind == "execute":
        if not a.design:
            print()
            print("board is execute-kind: draw the design first with "
                  "`swarm --type board-plan \"<goal>\"`, review it, "
                  "then run `swarm --type board --design BOARD_DESIGN.json`.",
                  file=sys.stderr)
            return 1

        design_path = Path(a.design)
        if not design_path.is_file():
            print(f"error: --design file not found: {a.design}", file=sys.stderr)
            return 1

        try:
            design = json.loads(design_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            print(f"error: --design file is not valid JSON: {e}", file=sys.stderr)
            return 1

        if not isinstance(design, list):
            print("error: --design file must contain a JSON array of task items",
                  file=sys.stderr)
            return 1

        print(f"   loaded design: {len(design)} task items")
        print(f"   dispatching to board() coordinator...")

        from echelon_engine.agent.partner import board
        result = board(
            goals=design,
            scope="echelon",
            folder=a.folder if a.folder else None,
            provider=a.provider if a.provider != "auto" else None,
            model=a.model or None,
        )
        print(f"   board result: {result.get('status', '?')}")
        for k, v in result.items():
            if k != "ledger":  # ledger can be large
                print(f"     {k}: {v}")
        from .dispatch import METER as _METER
        print(_METER.report())
        return 0

    # ── READ kind: analyze → produce a report ─────────────────────────────
    if st.mode == "plan":
        from .plan import run_plan_swarm
        lenses = st.lenses if st.lenses else None
        lens_defs = None
        if st.lenses:
            lens_defs = {}
            for l in st.lenses:
                lens_defs[l["name"]] = {
                    "prompt": l.get("prompt", ""),
                    "cartridge": l.get("cartridge", "architect"),
                }
        report = run_plan_swarm(
            goal=a.goal or plan_text[:500],
            context=a.context,
            files=file_list if file_list else None,
            lenses=[l["name"] for l in lenses] if lenses else None,
            lens_defs=lens_defs,
            provider=a.provider,
            model=a.model or st.model,
            output_file=a.output,
            timeout=a.timeout,
            effort=effective_effort,
            cartridges=a.cartridge or None,
        )
        if not a.output and report.get("findings"):
            import time
            out = f"SWARM_{st.name.upper()}_{time.strftime('%Y%m%d_%H%M%S')}.md"
            Path(out).write_text(_format_plan_md(report), encoding="utf-8")
            print(f"   written to {out}")

    elif st.mode == "council":
        from .council import run_council
        seats = [s["name"] for s in st.seats] if st.seats else None
        # Build custom seat_defs if SwarmType defines seats with prompts
        seat_defs = None
        if st.seats:
            seat_defs = {}
            for s in st.seats:
                seat_defs[s["name"]] = {
                    "role": s.get("role", s["name"]),
                    "prompt": s.get("prompt", ""),
                }
        report = run_council(
            plan_text=plan_text, goal=a.goal, context=a.context,
            seats=seats, seat_defs=seat_defs,
            provider=a.provider, model=a.model or st.model,
            timeout=a.timeout, effort=effective_effort,
        )
        if a.output:
            _write_council_md(report, a.output)

        # board-plan: emit BOARD_DESIGN.json alongside the council report
        if st.name == "board-plan":
            design = _synthesize_board_design(report, a)
            design_path = Path("BOARD_DESIGN.json")
            design_path.write_text(
                json.dumps(design, indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8")
            print(f"   BOARD_DESIGN.json written ({len(design)} items)")
            print(f"   next: swarm --type board --design BOARD_DESIGN.json --folder <repo>")

    elif st.mode == "solo":
        from .council import run_skeptic
        report = run_skeptic(
            plan_text=plan_text, goal=a.goal, context=a.context,
            provider=a.provider, model=a.model or st.model,
            timeout=a.timeout, effort=effective_effort,
            frame=st.frame or "",
        )
        if a.output and report.get("ok"):
            Path(a.output).write_text(
                f"# ECHELON SWARM — {st.name} Report\n\n{report.get('content', '')}",
                encoding="utf-8")
            print(f"   written to {a.output}")

    # TOKENOMICS — printed at the ONE shared exit rather than in each report writer, so
    # every kind (plan/council/skeptic/author/execute) reports its spend by construction and
    # a new swarm type cannot forget to. `echelon run` has always printed this; `swarm` never
    # did, which is how "what did that cost?" became unanswerable after the fact.
    from .dispatch import METER as _METER
    print(_METER.report())

    return 0


def _is_cartridge(name: str) -> bool:
    """True when `name` is a registered cartridge (best-effort, $0)."""
    try:
        from echelon_engine.atoms import cartridge_registry as reg
        return reg.get(name) is not None
    except Exception:
        return False


def _agent_main(argv) -> int:
    """swarm agent — each seat is an EQUIPPED AGENT through partner.dispatch
    (the one confirmed seam to a real tool loop). One seat per --cartridge;
    seats run sequentially, each outcome-verified, spend budget-gated."""
    ap = argparse.ArgumentParser(
        prog="echelon swarm agent",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description="Agent-seat swarm: one equipped partner per cartridge, "
                    "acting with tools in --folder.",
        epilog="EXAMPLE:\n  echelon swarm agent --cartridge scribe "
               "--goal \"document the swarm package accurately\" --folder .\n\n"
               "Context law here: an agent READS FOR ITSELF — hand it --file "
               "grounding patterns, never a slurped tree.")
    ap.add_argument("--cartridge", action="append", required=True, metavar="NAME",
                    help="A seat's equip (repeatable = one seat per cartridge)")
    ap.add_argument("--goal", required=True, help="One sentence; every seat gets it")
    ap.add_argument("--folder", default=".", help="The repo the seats act in (default .)")
    ap.add_argument("--scope", default="",
                    help="Bank scope for the seats (default: each cartridge's home scope)")
    ap.add_argument("--file", dest="file_list", nargs="*", default=None,
                    help="Grounding pattern files handed to every seat")
    ap.add_argument("--rules", default="", help="Explicit constraints for every seat")
    ap.add_argument("--model", default=None, help="Model override")
    ap.add_argument("--budget-usd", type=float, default=0.50,
                    help="Per-seat spend cap (default 0.50)")
    ap.add_argument("--max-steps", type=int, default=25, help="Per-seat step cap")
    a = ap.parse_args(argv)

    from echelon_engine.atoms import cartridge_registry as reg
    from echelon_engine.agent.partner import dispatch as _dispatch

    seats = []
    for name in a.cartridge:
        spec = reg.get(name)
        if spec is None and not a.scope:
            print(f"error: `{name}` is not a registered cartridge and no --scope "
                  f"given. `echelon cartridge list` shows the registry.",
                  file=sys.stderr)
            return 2
        seats.append((name, a.scope or (spec.scope if spec else "echelon")))

    print(f"⚡ ECHELON SWARM — agent mode: {len(seats)} seat(s), "
          f"budget ${a.budget_usd:.2f}/seat, folder={a.folder}")
    failures = 0
    for name, scope in seats:
        print(f"\n── seat: {name} (scope={scope}) ─────────────────────────")
        result = _dispatch(
            a.goal, scope=scope, folder=a.folder, rules=a.rules or None,
            pattern_files=a.file_list, cartridges=[name], model=a.model,
            budget_usd=a.budget_usd, max_steps=a.max_steps,
        )
        status = result.get("status")
        outcome = (result.get("outcome") or {})
        ok = status in ("completed", "done", "wrap-approve") and outcome.get("ok", True)
        print(f"   seat {name}: status={status} outcome_ok={outcome.get('ok')} "
              f"detail={str(outcome.get('detail', ''))[:120]}")
        if not ok:
            failures += 1
    print(f"\nagent swarm: {len(seats) - failures}/{len(seats)} seat(s) verified ok")
    return 0 if failures == 0 else 1


def _print_swarm_help():
    from .types import all_types
    print("echelon swarm — pluggable swarm orchestrator")
    print()
    print("  MODES (the first fork — what is a seat?):")
    print("    echelon swarm api   --type <t> --goal \"...\"   direct provider calls over frozen context")
    print("    echelon swarm agent --cartridge <c> --goal \"...\" --folder .   equipped tool-using seats")
    print()
    print("  echelon swarm types                    list available types")
    print("  echelon swarm register <name> ...      register a user swarm type")
    print("  echelon swarm unregister <name>        remove a user swarm type")
    print()
    print("Built-in types:")
    for t in all_types():
        tag = " (user)" if t.source == "user" else ""
        kind_tag = "⚡" if t.kind in ("execute", "author") else "📖"
        print(f"  --type {t.name:<12} [{t.kind:<7}] [{t.mode:<7}] {kind_tag} {t.description[:80]}{tag}")
    print()
    print("Shortcuts: --plan = --type plan, --council = --type council, --skeptic = --type skeptic")


def _cmd_types() -> int:
    from .types import all_types, builtin_names
    types = all_types()
    builtin = [t for t in types if t.name in builtin_names()]
    user = [t for t in types if t.name not in builtin_names()]
    print(f"SWARM TYPES ({len(builtin)} built-in, {len(user)} user):\n")
    for t in builtin:
        kind_tag = "⚡" if t.kind in ("execute", "author") else "📖"
        print(f"  ◆ {t.name:<12} [{t.kind:<7}] [{t.mode:<7}] {kind_tag} {t.description}")
    if user:
        print(f"\n  USER-REGISTERED (~/.echelon/swarms.json):")
        for t in user:
            print(f"  ◇ {t.name:<12} [{t.kind:<7}] [{t.mode:<7}] {t.description}")
    return 0


def _cmd_register(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="echelon swarm register")
    ap.add_argument("name", help="swarm type name (kebab-case)")
    ap.add_argument("--kind", default="read", choices=["read"],
                    help="Swarm kind (default: read). Execute types are built-in only.")
    ap.add_argument("--mode", default="plan", choices=["plan", "council", "solo"],
                    help="Dispatch mode for read-kind types (default: plan)")
    ap.add_argument("--description", default="", help="one-line description")
    ap.add_argument("--cartridge", default="architect", help="default cartridge")
    ap.add_argument("--model", default="", help="model override")
    a = ap.parse_args(argv)
    from .types import register_user
    st = register_user(name=a.name, kind=a.kind, mode=a.mode, description=a.description,
                       cartridge=a.cartridge, model=a.model)
    print(f"registered: {st.name} [kind={st.kind}, mode={st.mode}] → ~/.echelon/swarms.json")
    return 0


def _cmd_unregister(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="echelon swarm unregister")
    ap.add_argument("name", help="swarm type name to remove")
    a = ap.parse_args(argv)
    from .types import unregister_user
    ok = unregister_user(a.name)
    if ok:
        print(f"unregistered: {a.name} from ~/.echelon/swarms.json")
    else:
        print(f"not found: {a.name}")
        return 1
    return 0


def _format_plan_md(report: dict) -> str:
    """Format a plan report as markdown."""
    cov = report.get("coverage") or {}
    lines = [
        f"# ECHELON SWARM — Plan Report",
        f"",
        f"**Goal:** {report.get('goal', '')}",
        f"**Lenses:** {', '.join(report.get('lenses', []))}",
        f"**Elapsed:** {report.get('elapsed_secs', '?')}s",
    ]
    # A saved report outlives the console, so the coverage warning must live IN the
    # artifact. A markdown file headed "Findings (40)" with two dead lenses is read months
    # later as a complete audit by someone who never saw the ✗ glyphs scroll past.
    if cov and not cov.get("complete", True):
        failed = ", ".join(cov.get("lenses_failed", []))
        lines += [
            f"",
            f"> ⚠ **PARTIAL COVERAGE — {cov.get('lenses_ok')}/{cov.get('lenses_requested')} "
            f"lenses succeeded.** FAILED: {failed}.",
            f"> Those angles were NOT audited; absence of findings there is not evidence "
            f"of absence. Re-run before treating this report as complete.",
        ]
        # Name the REASON per lens: "it failed" invites a re-run, "the provider returned no
        # JSON" tells you whether a re-run would even help.
        for name, why in (cov.get("failures") or {}).items():
            lines.append(f"> - `{name}`: {why}")
    elif cov:
        lines.append(f"**Coverage:** {cov.get('lenses_ok')}/{cov.get('lenses_requested')} lenses OK")
    lines += [
        f"",
        f"## Findings ({len(report.get('findings', []))}"
        + ("" if not cov or cov.get("complete", True) else " — PARTIAL")
        + ")",
    ]
    for f in report.get("findings", []):
        sev = f.get("severity", "?")
        cat = f.get("category", "?")
        title = f.get("title", "?")
        # THE VERDICT LAW must reach the READER — an artifact that collapses the tier throws
        # the mechanism away at the last step. Unlabelled renders as PLAUSIBLE, never as
        # hard truth. (Kept identical to plan.py's renderer: two renderers that drift are
        # how a saved report and a console report tell different stories.)
        verdict = f.get("verdict") or "PLAUSIBLE"
        lines.append(f"- **[{verdict}] [{sev}] [{cat}]** {title}")
        if f.get("detail"):
            lines.append(f"  {f['detail']}")
        if f.get("evidence"):
            lines.append(f"  ⟐ Evidence: {f['evidence']}")
        if f.get("fix"):
            lines.append(f"  → Fix: {f['fix']}")
    lines.append("")
    lines.append("## Recommendations")
    for r in report.get("recommendations", []):
        lines.append(f"- {r}")
    lines.append("")
    lines.append("## Risks")
    for r in report.get("risks", []):
        lines.append(f"- {r}")
    lines.append("")
    lines.append("## Swarm Follow-Up")
    for step in report.get("swarm_follow_up", []):
        lines.append(f"- `{step}`")
    return "\n".join(lines) + "\n"


def _write_council_md(report: dict, path: str) -> None:
    """Write council report to markdown."""
    lines = [
        f"# ECHELON SWARM — Council Report",
        f"",
        f"**Goal:** {report.get('goal', '')}",
        f"**Seats:** {', '.join(report.get('seats', []))}",
        f"",
    ]
    for name, detail in report.get("findings", {}).items():
        lines.append(f"## {name} — {detail.get('role', '')}")
        lines.append(detail.get("content", "(no output)"))
        lines.append("")
    lines.append("## Swarm Follow-Up")
    for step in report.get("swarm_follow_up", []):
        lines.append(f"- `{step}`")
    Path(path).write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"   council report written to {path}")


def _synthesize_board_design(report: dict, a: argparse.Namespace) -> list[dict]:
    """Extract a BOARD_DESIGN.json task graph from board-plan council findings.

    Uses the 'decide' seat output (which is the decision-synthesizer) to produce
    a structured list of {id, task, role?, rules?} items. Falls back to a stub
    if the LLM output cannot be parsed.
    """
    findings = report.get("findings", {})
    # Collect all council outputs as synthesis material
    parts: list[str] = []
    for name in ("decompose", "sequence", "risk", "decide"):
        detail = findings.get(name, {})
        if detail.get("content"):
            parts.append(f"## {name}\n{detail['content']}\n")

    if not parts:
        return [{"id": "task-1", "task": "See council report for details", "role": "architect"}]

    synthesis_text = "\n".join(parts)

    # Ask the LLM to extract the structured task graph from the council output
    synth_prompt = (
        "You are a structured-output extractor. Given the council deliberation below, "
        "extract the board-ready task plan as a JSON array. Each item must be:\n"
        '  {"id": "kebab-case-id", "task": "one-line task description", '
        '"role": "cartridge-or-role-name", "rules": "any constraints or input rules"}\n'
        "The 'decide' seat contains the final synthesized plan — prefer its ordering. "
        'Output ONLY the JSON array (starts with "["), no markdown, no explanation.\n\n'
        f"{synthesis_text[:10000]}\n\nJSON:"
    )

    try:
        from .dispatch import send
        raw = send(synth_prompt, provider=a.provider,
                    model=a.model or "", timeout=a.timeout,
                    effort=getattr(a, "effort", ""))
        # Extract JSON array from response
        import re
        match = re.search(r'\[.*\]', raw, re.DOTALL)
        if match:
            design = json.loads(match.group(0))
        else:
            design = json.loads(raw)
        if isinstance(design, list) and len(design) > 0:
            return design
    except Exception:
        pass

    # Fallback: stub with the raw decide content as context
    decide = findings.get("decide", {}).get("content", "")
    return [{
        "id": "board-plan-output",
        "task": "Review the council report and extract tasks manually",
        "role": "architect",
        "rules": decide[:500] if decide else "See council report for the full plan",
    }]
