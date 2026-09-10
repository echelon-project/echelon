"""gate — THE ECHELON GATE: the provider-agnostic banner that opens the substrate.

THE LOOP-CLOSER (owner, 2026-06-20). Every project's MEMORY.md begins with an ECHELON banner so that
ANY model, on ANY provider, reading that file COLD can (a) understand what ECHELON is and (b) start using
it correctly — without a human re-explaining it. The old banner explained the IDEA but routed weakly:
it assumed "an ECHELON estate" and bolted the not-yet-adopted case on as a footnote. This GATE inverts
that — it opens with the four claims, then routes the reader into exactly ONE of three doors:

    1. EXISTING ECHELON USER  — this estate already has a bank; warm up + recall + cartridges, here's how.
    2. NEW PROJECT            — no memory yet; how to start writing atoms + light the bank from zero.
    3. MIGRATING A PROJECT    — a project with notes/docs but not in ECHELON shape; how to adopt it.

It is BOTH the banner that gets prepended to a MEMORY.md AND the standalone gate a provider reads to be
admitted. One source of truth (this module); `print` emits it, `--write <MEMORY.md>` splices it in
(replacing the old banner block, preserving everything below it verbatim — never fabricates content).

  python -m echelon_engine gate                       # print the gate (read it / pipe it to a model)
  python -m echelon_engine gate --write <MEMORY.md>   # replace that file's banner with the canon gate
  python -m echelon_engine gate --write <MEMORY.md> --scope <scope>   # name the project's scope inline
  python -m echelon_engine gate --write <MEMORY.md> --full            # force the full form over a lean banner

ONE BANNER WRITER (spec S8 V3, INC-0001): the SessionStart hook is the banner's only
writer on every boot. The CLI full form refuses to clobber a lean (v9-lean) banner —
pass `--full` to force; `harness-sync` and `gate` both honour `lean_gate: true`.

See: [[boot-card-collapses-catalog-to-bootshape]] (the REFLEX+THINK boot shape this banner heads),
[[banners-state-now-atoms-record-history]] (a banner states NOW; it is machine-refreshable, not history),
[[stale-banners-point-at-the-legacy-engine-path]] (why a single canon source matters — banners drift).
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

# The HTML-comment sentinel that marks a banner this gate owns. Versioned so a re-write can detect+replace
# its own prior output exactly, and so a human can see at a glance the banner is machine-managed.
BANNER_SENTINEL = "ECHELON-GATE-BANNER"
BANNER_VERSION = "v8 (2026-08-02) — +LAW 4 loop-shape & tool-wait"
BANNER_VERSION_LEAN = "v9-lean (2026-08-02) — +LAW 4 loop-shape & tool-wait"

# ── Path discovery — no hardcoded machine paths ──────────────────────────────────
# The gate is provider-agnostic; it must work on ANY machine. These are discovered
# at gate-write time, not hardcoded to the canonical authoring machine.

def _engine_root() -> str:
    """Discover the engine install directory from this file's location."""
    # gate.py lives in echelon_engine/atoms/ — up two levels is the engine root
    return str(Path(__file__).resolve().parent.parent.parent)

def _estate_root(write_target: str = "") -> str:
    """Discover the estate (project) directory.

    1. ECHELON_ESTATE env var (explicit override)
    2. Derived from the MEMORY.md write target (parent of memory/ dir)
    3. cwd fallback
    """
    if os.environ.get("ECHELON_ESTATE"):
        return os.environ["ECHELON_ESTATE"]
    if write_target:
        mem = Path(write_target).resolve()
        # MEMORY.md is typically at <estate>/memory/MEMORY.md
        if mem.parent.name == "memory":
            return str(mem.parent.parent)
        # Could also be at <estate>/MEMORY.md
        return str(mem.parent)
    return str(Path.cwd())

def _room_block(cwd: str = "") -> str:
    """The live workroom block for the lean gate: goal · task/stage · next action.

    Sourced from the sibling `echelon_engine.workcycle` module (GUARDED — it may not
    exist yet, or the bank may hold no room): `resume_brief(room_path(cwd))` lines are
    picked by prefix (WHERE / TASK|STAGE / NEXT), capped at 3 lines. No room or no
    module -> '' — the banner never shows a placeholder for state it cannot name,
    and the MEMORY.md read ceiling is unaffected (this block is ≤3 short lines)."""
    try:
        from echelon_engine import workcycle  # guarded: sibling-owned module (W3 doors)
    except Exception:
        return ""
    try:
        room = workcycle.room_path(cwd or None)
        if not room:
            return ""
        brief = (workcycle.resume_brief(room) or "").splitlines()
    except Exception:
        return ""
    lines: list[str] = []
    for line in brief:
        head, _, rest = line.partition(" ")
        key = head.strip(": ").upper()
        if key == "WHERE":
            lines.append(f"> **ROOM** — goal: {rest.strip()}")
        elif key in ("TASK", "STAGE") and not any(l.startswith(">   task") for l in lines):
            lines.append(f">   task/stage: {rest.strip()}")
        elif key == "NEXT":
            lines.append(f">   next: {rest.strip()}")
    if not lines:
        return ""
    return "\n" + "\n".join(lines[:3]) + "\n"


def _bank_path_display() -> str:
    """The bank path — a portable convention, not a machine path."""
    try:
        from echelon_engine.atoms.compact import BANK_PATH_DISPLAY
        return BANK_PATH_DISPLAY
    except Exception:
        return "~/.echelon/echelon.db"

def _handoff_path_display() -> str:
    """The compact handoff path — from the compact module (single source of truth)."""
    try:
        from echelon_engine.atoms.compact import HANDOFF_PATH_DISPLAY
        return HANDOFF_PATH_DISPLAY
    except Exception:
        return "~/.echelon/compact_handoff.md"


def _cartridge_registry_block(scope: str) -> str:
    """The LIVE cartridge catalog, machine-generated from both the built-in registry
    (cartridge_registry.py) and the user registry (~/.echelon/cartridges.json).

    Two-tier: built-in cartridges ship with the engine; user cartridges are registered
    via `echelon cartridge register` and survive pip upgrades. all_specs() merges both;
    built-in names shadow user duplicates. The count + names can never go out of sync —
    they're queried live, never hand-typed.

    Estate-scoped on purpose: only the estate that AUTHORS cartridges (whose scope is a
    cartridge home, i.e. `echelon` and the `*-cartridge` scopes) gets the catalog — every
    other estate's banner stays the identical provider-agnostic gate, un-polluted.
    Returns '' when this scope authors no cartridges."""
    try:
        from echelon_engine.atoms.cartridge_registry import all_specs
    except Exception:
        return ""  # registry unavailable -> emit nothing (the banner must never fail to write)
    specs = all_specs()
    builtin_n = sum(1 for s in specs if getattr(s, "source", "builtin") == "builtin")
    user_n = sum(1 for s in specs if getattr(s, "source", "") == "user")
    # The cartridge-authoring estate is `echelon` (the canonical home) or any cartridge's own home scope.
    homes = {s.scope for s in specs} | {"echelon"}
    if scope not in homes or not specs:
        return ""
    lines = []
    for s in specs:
        tag = " (user)" if getattr(s, "source", "") == "user" else ""
        # HOOK, not catalog (2026-07-08): full summaries pushed MEMORY.md to 20KB of its
        # 24.4KB read ceiling (~9.3KB was this block alone). The banner's job is recall-by-
        # name — the first clause is the hook; the full description is one `cartridge list`
        # away and can't go stale here because it was never copied here.
        hook = s.summary.strip()
        if len(hook) > 110:
            hook = hook[:110].rsplit(" ", 1)[0] + "…"
        lines.append(
            f">   - **{s.name}**{tag}" + (f" (`/{s.skill}`)" if s.skill else "") + f" — {hook}")
    body = "\n".join(lines)
    source_note = f" ({builtin_n} built-in, {user_n} user)" if user_n else ""
    return f""">
> **LIVE CARTRIDGE REGISTRY ({len(specs)} registered{source_note})** — machine-generated from the
> built-in registry + `~/.echelon/cartridges.json`. Equip one by NAME:
> `python -X utf8 -m echelon_engine cartridge equip <name> "<goal>"` (or the `/cartridge` skill). Detail:
> `cartridge list` / `cartridge registry`. Register a user cartridge: `cartridge register <name> --scope <s>`.
{body}
>"""


def _cartridge_registry_block_lean(scope: str) -> str:
    """Lean variant: NAMES ONLY, comma-run wrapped to ~100-char lines.

    CONTEXT-ECONOMY CUT (2026-08-12, [[context-economy-discipline-adopted-from-fabel]] law 3:
    a description's one job is the load decision). The prior 5-word hooks were truncation
    artifacts ("UX UI design KNOWLEDGE device…") that routed nothing — the NAME is the router
    for cartridges (debug, refactor, qa, port…), and `cartridge list` is the detail door one
    call away. 34 hook-lines ≈ 1.6KB always-on → names-only ≈ 0.4KB, paid on every boot of
    every estate. This block's history IS the law's proof: 9.3KB (full catalog, pre-2026-07-08)
    → 1.6KB (hooks) → this."""
    try:
        from echelon_engine.atoms.cartridge_registry import all_specs
    except Exception:
        return ""
    specs = all_specs()
    homes = {s.scope for s in specs} | {"echelon"}
    if scope not in homes or not specs:
        return ""
    names = [s.name + (" (user)" if getattr(s, "source", "") == "user" else "") for s in specs]
    lines, cur = [], ">   "
    for i, n in enumerate(names):
        piece = n + ("," if i < len(names) - 1 else "")
        if len(cur) + len(piece) + 1 > 100 and cur.strip():
            lines.append(cur.rstrip())
            cur = ">   "
        cur += piece + " "
    if cur.strip() != ">":
        lines.append(cur.rstrip())
    body = "\n".join(lines)
    return f""">
> **CARTRIDGES ({len(specs)})** — equip by NAME: `cartridge equip <name> "<goal>"`. Hooks/detail: `cartridge list`.
{body}
>"""


def _compact_handoff_notice(scope: str, estate: str = "") -> str:
    """Check for an unconsumed compact (ESTATE-AWARE, see C5) and return a notice
    line for the gate banner. Returns empty string if none is available in THIS
    estate — but if a newer one exists in ANOTHER estate, surfaces a cross-estate
    hint instead of silently showing nothing (the boot mis-fire fix: the banner
    used to announce a compact from a different project as if it were ours).

    `estate` is the resolved estate root this gate is being written FOR. When
    known, discovery is scoped to it; when unknown and multiple compacts exist,
    reports the count rather than guessing."""
    try:
        from echelon_engine.session_state import discover_compact, discover_all_unconsumed
        compact = discover_compact(scope, project_path=estate or None)
        if not compact:
            # Nothing in THIS estate — is there a newer one in another estate?
            other = discover_compact(scope, cross_estate=True)
            if other:
                return (
                    f"\n> **⮕ CONTINUITY ELSEWHERE** — an unconsumed compact exists for "
                    f"ANOTHER estate (`{other.get('project_path','?')}`), not this one.\n"
                    f">   Not auto-offered here (estate-scoped). If that's where the live work is, "
                    f"resume there — or take it deliberately:\n"
                    f">   `echelon session-state discover --consume --cross-estate --scope {scope}`\n"
                )
            if not estate:
                # Estate unknown and cross-estate also nothing — check if ANY exist unfiltered
                all_uc = discover_all_unconsumed(scope)
                n = len(all_uc)
                if n > 0:
                    return (
                        f"\n> **⮕ CONTINUITY — {n} unconsumed compact(s) across estates; "
                        f"run `session-state discover --scope {scope}` to pick.**\n"
                    )
            return ""
        meta = compact.get("meta", {})
        tasks = len(meta.get("open_tasks", []))
        goal = meta.get("current_goal", "")[:100]
        tokens = meta.get("tokens_used", 0)
        handoff = _handoff_path_display()
        warn = ""
        if compact.get("_cross_estate_warning"):
            w = compact["_cross_estate_warning"]
            warn = (f">   ⚠ a NEWER compact exists for another estate "
                    f"(`{w['newer_project']}`) — not this one.\n")
        # Show target_estates if available (C1)
        targets = compact.get("target_estates") or meta.get("target_estates")
        estates_line = ""
        if targets:
            estates_line = f"  Estates: {', '.join(targets)}\n"
        return (
            f"\n> **⮕ CONTINUITY AVAILABLE — an unconsumed compact whose work targets THIS estate.**\n"
            f">   {tasks} open task(s). Goal: {goal}\n"
            f"{estates_line}"
            f">   Tokens used: {tokens}. Compact saved: {compact['ts'][:19]}\n"
            f"{warn}"
            f">   Consume: `echelon session-state discover --consume --scope {scope}`\n"
            f">   Handoff (protocol + full state): `{handoff}`\n"
        )
    except Exception:
        return ""


def gate_text(scope: str = "<this-project-scope>", write_target: str = "") -> str:
    """The canonical ECHELON gate — the four claims, then the three entry doors. `scope` is the project's
    bank scope (kebab of its dir); left as a placeholder if unknown so the reader fills it in. For a
    cartridge-authoring estate, a machine-generated LIVE CARTRIDGE REGISTRY block is spliced in (else '').
    `write_target` is the MEMORY.md path being written to — used to discover the estate directory."""
    cartridges = _cartridge_registry_block(scope)
    engine = _engine_root()
    estate = _estate_root(write_target)
    handoff = _compact_handoff_notice(scope, estate=estate)
    bank = _bank_path_display()
    return f"""<!-- {BANNER_SENTINEL} {BANNER_VERSION} — the LEAN gate: doctrine compressed, ceremony tiered by stakes.
     Machine-managed: regenerate with `python -m echelon_engine gate --write <this file>`. Everything BELOW
     the first `---` is this project's own content, preserved verbatim on rewrite. Do not hand-edit this
     block — edit the canon in echelon_engine/atoms/gate.py and re-run the gate. -->{handoff}
> ## ⚡ ECHELON — memory + reasoning substrate. Estate scope `{scope}`.
>
> **Compressed doctrine** (full version lives in the bank, not here): each `.md` in `memory/` is ONE atom —
> a WEIGHT-ADJUSTOR storing the seed of an understanding, not a record. Recall is FOVEATED: query your
> intent, matching atoms warm up, the rest stay dormant. Significance is EARNED BY TRACE — an atom gains
> weight only when a card that used it succeeds. A CARTRIDGE is the pluggable earned core of a capability.
>
> **Reach.** Engine `{engine}` (always `-X utf8`) · bank `{bank}` · estate `{estate}`.
>
> **LAW 1 — REFLEX vs THINK.** For real work, query before acting:
> `python -X utf8 -m echelon_engine recall --scope {scope} --warm "<what I'm about to do>"`
> **WARM → re-tread** the proven path (those atoms are your own earned experience). **COLD → think fresh**,
> witness by real work, then bank what paid off as a new atom. Dormant ≠ gone — query for it. Read a full
> atom via `remember <slug>`, never `cat` the `.md` (a file-read is out-of-band; the bank never witnesses it).
>
> **LAW 2 — CEREMONY MATCHES STAKES.** Ritual is a COST, paid only where it buys accuracy:
> - **SMALL** (mechanical / single-file / a question): NO ceremony — no warm-up, no council, no cartridge
>   hunt. Do it, verify the one thing that matters. Running ceremony here is a DEFECT, not diligence.
> - **MEDIUM** (a real change, known shape): Law 1 recall, use what surfaces, done.
> - **LOAD-BEARING** (schema / deploy / architecture / multi-file build / anything outward): warm up first
>   (`/memories-warm-up`), delegate BULK to a worker, and GATE from a context that didn't do the work —
>   self-review shares the blind spot that made the bug. Close a real session with `/wrap`.
>
> **LAW 3 — TAKE THE SURFACED MOVE.** When the per-turn nerve (or Law 1 recall) surfaces an earned MOVE —
> a card, a cartridge (`cartridge equip <name> "<goal>"`), a delegate/swarm verdict — take it by default:
> it is your own prior experience, not a suggestion to re-litigate. Consume a ⮕ CONTINUITY compact when
> the header offers one: `echelon session-state discover --consume --scope {scope}`.
>
> **LAW 4 — THE LOOP IS SMALL; DON'T PAY TOOL-WAIT.** Work the shape
> `change → gate → test → repeat → ship`: ONE change, checked at once, so a failure names itself.
> The older shape (`change → change → test → gate → change → test`) debugs COMPOUND failures over
> big diffs — slower, not faster. **The gates are what keep the loop small; never cut them to save
> time.** The real waste is TOOL-WAIT (owner measured ~70% of a session's wall-clock):
> - **Test at the scope of the change.** Targeted test file for what you touched (seconds). The FULL
>   suite is a *pre-ship* gate — run it ONCE before it goes out, not after every edit.
> - **Batch remote work.** One SSH running one script; never N round-trips (224 files × one process
>   each = 90s to answer one question).
> - **Never sleep-poll.** Background the long run and check once; read a file's mtime instead of waiting.
> - **Never re-run a command to re-read output you already have.**
> - **Narrate findings and direction changes, not steps** — commentary between calls serializes your
>   thinking against tool latency.
{cartridges}
> **New estate?** One fact per `.md` (frontmatter: name/description/metadata.type), index a pointer line
> here, then from the engine dir: `ingest --root "<this memory dir>" --scope <scope>`.
> **Migrating dense notes?** `/echelon-init` splits molecules into atoms (archive, never delete).
> **Bank not lit?** The `.md` files below ARE the memory — read them directly until you ingest.

---
"""


def gate_text_lean(scope: str = "<this-project-scope>", write_target: str = "") -> str:
    """Lean gate — ≤40% of full banner bytes. Preserves the three laws, engine reach, entry doors,
    and cartridge names (5-word hook). Doctrine compression: atoms=weight-adjustors, recall=foveated,
    earn-by-trace, cartridge=pluggable core — the full doctrine lives in the bank, surfaced by recall."""
    cartridges = _cartridge_registry_block_lean(scope)
    engine = _engine_root()
    # Checkout locations are unbounded. Keep the hot banner compact while the
    # existing diagnostic door retains the exact installation location.
    engine_hint = f"Engine `{engine}`" if len(engine) <= 48 else "Engine path: `echelon doctor`"
    estate = _estate_root(write_target)
    handoff = _compact_handoff_notice(scope, estate=estate)
    bank = _bank_path_display()
    room = _room_block(estate if write_target else "")
    return f"""<!-- {BANNER_SENTINEL} {BANNER_VERSION_LEAN} — ceremony tiered by stakes; full gate at `echelon gate`.
     Machine-managed: regenerate with `python -m echelon_engine gate --write <this file> --lean`.{handoff}
> ## ⚡ ECHELON — scope `{scope}`. {engine_hint} (-X utf8). Bank `{bank}`.
>{room}
> **Doctrine (compressed).** Atoms: one `.md` = one weight-adjustor, earned by trace. Recall: foveated
> query (`--warm "<intent>"`). Cartridge: pluggable earned capability. Full doctrine in bank — surface
> with `recall --warm`.
>
> **LAW 1 (REFLEX vs THINK).** Query before acting: `recall --scope {scope} --warm "<intent>"`.
> WARM → re-tread. COLD → think fresh, bank what paid off. Read atoms: `remember <slug>` (not cat).
>
> **LAW 2 (CEREMONY ∝ STAKES).** SMALL (mechanical / question) → no ceremony. MEDIUM (real change) →
> Law 1 recall. LOAD-BEARING (schema / deploy / multi-file / outward) → warm-up, delegate, gate from
> independent context. Close sessions: `/wrap`.
>
> **LAW 3 (TAKE THE SURFACED MOVE).** Nerve/recall surfaces an earned move (card, cartridge, verdict)
> → take it by default. Consume ⮕ CONTINUITY: `session-state discover --consume --scope {scope}`.
>
> **LAW 4 (THE LOOP IS SMALL; DON'T PAY TOOL-WAIT).** Work `change → gate → test → repeat → ship`:
> ONE change, checked at once — small diffs fail unambiguously. Gates are what keep the loop
> small; never cut them to "go faster". The waste is TOOL-WAIT, not ceremony (owner measured
> ~70% of a session): run the TARGETED test for what you touched (seconds), full suite ONCE
> pre-ship; batch remote probes into ONE call, never N round-trips; background long runs instead
> of sleep-polling; never re-run a command to re-read output you already have. Narrate findings
> and direction changes, not steps.
{cartridges}
> **New?** One fact per `.md`, index below, `ingest --root "<dir>" --scope <scope>`. **Migrating?**
> `/echelon-init`. **Bank not lit?** Read the `.md` files below directly.

---
"""


# A banner this gate manages starts with the sentinel comment (or the legacy plain blockquote) and runs to
# the FIRST horizontal rule (`---` on its own line) — everything from there down is the project's content.
_RULE = re.compile(r"^---\s*$", re.M)
# The version token follows the sentinel on the first banner line, up to the first em-dash.
_BANNER_VER = re.compile(re.escape(BANNER_SENTINEL) + r"\s+([^\n]*?)\s*(?:—|$)")


def banner_version(text: str) -> str:
    """The version token after the banner sentinel in `text` — '' when absent.
    'v8 (2026-08-02)' for the full gate, 'v9-lean (2026-08-02)' for the lean."""
    m = _BANNER_VER.search(text)
    return m.group(1).strip() if m else ""


def split_banner(existing: str) -> tuple[str, str]:
    """Split a MEMORY.md into (old_banner, rest). The banner is everything up to and INCLUDING the first
    `---` rule (the convention every ECHELON MEMORY.md uses to separate the head from the body). If there
    is no rule, the whole file is treated as body (we prepend, never clobber unknown content)."""
    m = _RULE.search(existing)
    if not m:
        return "", existing
    cut = m.end()
    return existing[:cut], existing[cut:]


def write_into(path: Path, scope: str, lean: bool = False, force: bool = False) -> dict:
    """Splice the canon gate into `path`: replace its banner block, preserve the body verbatim. Returns a
    small report. Creates the file (gate + empty body) if it does not exist. Idempotent — re-running with
    the same canon + scope yields the same file. Pass `lean=True` for the lean (≤40% bytes) variant.
    ONE BANNER WRITER (spec S8 V3, INC-0001; gate r1 S8a S-3): a file already carrying the v9-lean
    banner STAYS lean — every caller (the SessionStart hook, the CLI, harness-sync) inherits this
    here; `force=True` (CLI `--full`) is the only way to put the full form over a lean banner."""
    if not path.exists():
        banner = gate_text_lean(scope, write_target=str(path)) if lean else gate_text(scope, write_target=str(path))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(banner + "\n", encoding="utf-8")
        return {"path": str(path), "created": True, "preserved_body_chars": 0, "lean": lean}
    existing = path.read_text(encoding="utf-8")
    if not lean and not force and "v9-lean" in banner_version(existing):
        lean = True
    banner = gate_text_lean(scope, write_target=str(path)) if lean else gate_text(scope, write_target=str(path))
    old_banner, body = split_banner(existing)
    # `gate_text` already ends with the `---` rule + newline; join the preserved body straight after.
    new = banner + body.lstrip("\n") if body.strip() else banner + "\n"
    path.write_text(new, encoding="utf-8")
    return {"path": str(path), "created": False,
            "replaced_banner_chars": len(old_banner),
            "preserved_body_chars": len(body),
            "lean": lean}


def _main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    ap = argparse.ArgumentParser(
        prog="echelon gate",
        description="THE ECHELON GATE — the provider-agnostic banner: what ECHELON is + the three entry doors "
                    "(existing user / new project / migrating project). Print it, or write it into a MEMORY.md.")
    ap.add_argument("--write", metavar="MEMORY_MD",
                    help="splice the gate into this MEMORY.md (replace its banner, keep the body verbatim)")
    ap.add_argument("--scope", default="<this-project-scope>",
                    help="this project's bank scope (kebab of its dir); shown inline in the gate")
    ap.add_argument("--lean", action="store_true",
                    help="emit the LEAN gate variant (≤40%% bytes — draft, feat/boot-lean)")
    ap.add_argument("--full", action="store_true",
                    help="force the full-form write even over a v9-lean banner (spec S8 V3: one banner writer)")
    a = ap.parse_args(argv)

    if not a.write:
        print(gate_text_lean(a.scope) if a.lean else gate_text(a.scope))
        return 0
    # ONE BANNER WRITER (spec S8 V3, INC-0001): the full form never clobbers a
    # lean banner — the SessionStart hook and harness-sync own the lean form via
    # the contract's lean_gate; an accidental full write fights both.
    if not a.lean and not a.full:
        target = Path(a.write)
        existing = target.read_text(encoding="utf-8") if target.exists() else ""
        ver = banner_version(existing)
        if "v9-lean" in ver:
            print(f"gate: refusing full-form write onto {a.write} — it carries the lean banner "
                  f"({ver}); the SessionStart hook is the banner's only writer and the contract's "
                  f"lean_gate: true owns this file — pass --full to force the full form",
                  file=sys.stderr)
            return 2
    rep = write_into(Path(a.write), a.scope, lean=a.lean, force=a.full)
    if rep.get("created"):
        lean_tag = " (lean)" if rep.get("lean") else ""
        print(f"gate: created {rep['path']} with the ECHELON gate banner{lean_tag}.")
    else:
        lean_tag = " (lean)" if rep.get("lean") else ""
        print(f"gate: rewrote banner in {rep['path']}{lean_tag} "
              f"(replaced {rep['replaced_banner_chars']} banner chars, "
              f"preserved {rep['preserved_body_chars']} body chars).")
    return 0


if __name__ == "__main__":
    sys.exit(_main())
