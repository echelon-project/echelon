"""init — `echelon init` and `echelon setup`: the first-contact path from zero to working.

`echelon init` bootstraps a project directory with ECHELON memory infrastructure:
  - memory/MEMORY.md (gate banner)
  - CLAUDE.md (agent instructions)
  - .gitignore entry for .echelon/

`echelon setup` is the guided first-time experience:
  init → install-hooks → seed config → verify providers → summary

Both are idempotent. No LLM calls. Honest reporting — never "NOW", never "REQUIRED".
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path


def _resolve_scope(target: str) -> str:
    """Kebab of the target directory leaf name."""
    import re
    leaf = os.path.basename(os.path.normpath(target))
    return re.sub(r"-{2,}", "-", re.sub(r"[^a-z0-9]+", "-", leaf.lower())).strip("-") or "echelon"


def _ensure_memory_dir(target: str) -> Path:
    """Create memory/ directory, return its path."""
    mem = Path(target) / "memory"
    mem.mkdir(parents=True, exist_ok=True)
    return mem


def _write_gate(mem_dir: Path, scope: str) -> str:
    """Write the ECHELON gate banner into memory/MEMORY.md. Returns 'created' or 'updated'."""
    mem_path = mem_dir / "MEMORY.md"
    try:
        from echelon_engine.atoms.gate import write_into
        rep = write_into(mem_path, scope)
        return "created" if rep.get("created") else "updated"
    except Exception as e:
        # Fallback: write a minimal gate so the file exists
        if not mem_path.exists():
            mem_path.write_text(
                f"<!-- ECHELON-GATE-BANNER — run `echelon gate --write {mem_path} --scope {scope}` to refresh -->\n"
                f"> **ECHELON Memory Bank** — scope: `{scope}`\n"
                f"> Bank: `~/.echelon/echelon.db`\n"
                f"> Run `echelon gate --write {mem_path} --scope {scope}` to generate the full banner.\n\n"
                f"---\n",
                encoding="utf-8",
            )
        return "created (minimal — gate module unavailable)"


def _write_claude_md(target: str, scope: str) -> str:
    """Write a minimal CLAUDE.md that references the gate. Returns 'created' or 'exists'."""
    claude_path = Path(target) / "CLAUDE.md"
    if claude_path.exists():
        return "exists (not overwritten)"

    content = (
        f"# CLAUDE.md — ECHELON agent instructions\n\n"
        f"This project uses ECHELON, a persistent memory + reasoning substrate.\n\n"
        f"## On every session start\n"
        f"Read `memory/MEMORY.md` — it contains the ECHELON gate banner with live bank status,\n"
        f"the REFLEX/THINK routing law, and the three entry doors.\n\n"
        f"## Bank\n"
        f"- Scope: `{scope}`\n"
        f"- Bank: `~/.echelon/echelon.db`\n"
        f"- Query: `echelon recall --scope {scope} --warm \"<intent>\"`\n"
        f"- Status: `echelon status --scope {scope}`\n\n"
        f"## Key verbs\n"
        f"- `echelon recall --warm \"...\"` — foveated recall (free)\n"
        f"- `echelon remember <slug>` — read full atom body (earns weight)\n"
        f"- `echelon cartridge list` — list available capabilities\n"
        f"- `echelon cartridge equip <name> \"<goal>\"` — equip a cartridge\n"
        f"- `echelon wrap` — close session, distill lessons\n"
        f"- `echelon status` — bank overview\n\n"
        f"## Memory\n"
        f"Atoms are stored in `memory/*.md` (one lesson per file). Write new atoms there,\n"
        f"then run `echelon ingest --root memory --scope {scope}` to plant them in the bank.\n"
    )
    claude_path.write_text(content, encoding="utf-8")
    return "created"


def _write_example_atom(mem_dir: Path, scope: str) -> str:
    """Write a valid example atom into memory/_example.md (only if absent).
    Teaches the atom format, [[wiki-link]] syntax, and the ingest+recall commands.
    Returns 'created' or 'exists (not overwritten)'."""
    example_path = mem_dir / "_example.md"
    if example_path.exists():
        return "exists (not overwritten)"

    content = (
        "---\n"
        "name: example-atom\n"
        "description: An example ECHELON atom — teaches the format, wiki-links, and the ingest+recall loop\n"
        "metadata:\n"
        "  type: reference\n"
        "---\n\n"
        "# Example Atom\n\n"
        "This is a valid ECHELON atom. Each atom is ONE lesson or fact in a single `.md` file.\n\n"
        "## Format\n"
        "- **Frontmatter** (the `---` block above): `name` (kebab-case slug), `description` (one-line\n"
        "  warmth hook), and `metadata.type` (one of `user`, `feedback`, `project`, `reference`).\n"
        "- **Body** (below `---`): markdown — explain the lesson, link related atoms, be specific.\n\n"
        "## Wiki-links\n"
        "Link related atoms with `[[double-bracket syntax]]` — e.g. [[example-atom]]. The bank\n"
        "resolves these into edges so recall can travel across related lessons.\n\n"
        "## Commands\n"
        "After writing a new atom, plant it into the bank:\n"
        f"  echelon check --root memory          # validate the .md files\n"
        f"  echelon ingest --root memory --scope {scope}   # plant into the bank\n"
        f"  echelon recall --scope {scope} --warm \"your intent\"   # recall what you planted\n\n"
        "## Delete me\n"
        "This file is a teaching example — delete it once you've written your own atoms.\n"
    )
    example_path.write_text(content, encoding="utf-8")
    return "created"


def _ensure_gitignore(target: str) -> str:
    """Add .echelon/ to .gitignore if not present. Returns what happened."""
    gi_path = Path(target) / ".gitignore"
    entry = ".echelon/"
    if gi_path.exists():
        existing = gi_path.read_text(encoding="utf-8")
        if entry in existing:
            return ".gitignore already has .echelon/"
        gi_path.write_text(existing.rstrip("\n") + f"\n{entry}\n", encoding="utf-8")
        return "added .echelon/ to .gitignore"
    else:
        gi_path.write_text(f"{entry}\n", encoding="utf-8")
        return "created .gitignore with .echelon/"


# ── init ──────────────────────────────────────────────────────────────────────

def cmd_init(target: str, scope: str = "") -> dict:
    """Run `echelon init`. Returns a report dict."""
    target = os.path.abspath(target)
    if not scope:
        scope = _resolve_scope(target)

    report = {"target": target, "scope": scope, "steps": {}}

    mem = _ensure_memory_dir(target)
    report["steps"]["memory_dir"] = str(mem)

    report["steps"]["gate"] = _write_gate(mem, scope)
    report["steps"]["claude_md"] = _write_claude_md(target, scope)
    report["steps"]["gitignore"] = _ensure_gitignore(target)
    report["steps"]["example_atom"] = _write_example_atom(mem, scope)
    report["steps"]["mem_dirs"] = _seed_mem_dirs(scope, str(mem))

    return report


def _seed_mem_dirs(scope: str, mem_dir: str) -> str:
    """Add the scope -> memory/ dir mapping to ~/.echelon/mem_dirs.json.
    Creates the config if missing; merges if present. Idempotent — won't
    duplicate an existing entry for the same scope+dir pair.
    Returns 'created', 'merged', or 'unchanged'."""
    import json
    config_path = Path.home() / ".echelon" / "mem_dirs.json"
    config_path.parent.mkdir(parents=True, exist_ok=True)

    cfg: dict = {}
    if config_path.exists():
        try:
            cfg = json.loads(config_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            cfg = {}

    existing = cfg.get(scope, [])
    existing_list = [existing] if isinstance(existing, str) else list(existing)
    mem_abs = str(Path(mem_dir).resolve())

    if mem_abs in existing_list:
        return "unchanged (scope already mapped)"

    existing_list.append(mem_abs)
    cfg[scope] = existing_list

    existed_before = config_path.exists()
    tmp = config_path.with_suffix(".tmp")
    tmp.write_text(json.dumps(cfg, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.replace(config_path)
    return "created" if not existed_before else "merged"


def _print_init_report(report: dict) -> None:
    """Print the init report in a readable format."""
    print(f"ECHELON init — {report['target']}")
    print(f"  scope: {report['scope']}")
    for step, result in report["steps"].items():
        print(f"  {step}: {result}")
    print()
    print("Ready. Next steps:")
    print(f"  echelon setup --target {report['target']}              (full guided setup)")
    print(f"  echelon config set-key deepseek <key>       (store API keys)")
    print(f"  echelon status --scope {report['scope']}     (bank overview)")
    print()
    print("Write your own atoms in memory/, then plant them:")
    print(f"  echelon check --root memory                   (validate .md files)")
    print(f"  echelon ingest --root memory --scope {report['scope']}   (plant into the bank)")


# ── setup ─────────────────────────────────────────────────────────────────────

def _bootstrap_starter_atoms() -> dict:
    """Ingest each packaged starter scope dir into the bank. Returns {scope: count, ...}.
    Starter atoms ship in echelon_engine/data/starter_atoms/<scope>/ — the ghost-cartridge
    fix: a new user who pip-installed echelon now gets the high-value cartridge atoms
    without needing the ECHELON_ESTATE on disk."""
    import importlib.resources
    from echelon_engine.atoms.ingest import ingest_folder

    result: dict[str, int] = {}
    starter_root = Path(__file__).resolve().parent.parent / "data" / "starter_atoms"
    if not starter_root.is_dir():
        return result

    for scope_dir in sorted(starter_root.iterdir()):
        if not scope_dir.is_dir():
            continue
        scope = scope_dir.name
        # The canonicalization gate resolves the scope from the DIRECTORY PATH, which for
        # packaged starter atoms is the install location (e.g. 'echelon'), never the
        # cartridge scope. This ingest is the deliberate cross-scope case the gate's
        # ECHELON_INGEST_SCOPE escape hatch exists for — set it for this call only.
        _prev = os.environ.get("ECHELON_INGEST_SCOPE")
        os.environ["ECHELON_INGEST_SCOPE"] = "1"
        try:
            # Deliberate cross-scope plant (same reason as the scope-gate escape above):
            # starter bodies may legitimately exist verbatim in their source estate's scope.
            planted = ingest_folder(str(scope_dir), scope, verbose=False,
                                    allow_cross_scope_dup=True)
        finally:
            if _prev is None:
                os.environ.pop("ECHELON_INGEST_SCOPE", None)
            else:
                os.environ["ECHELON_INGEST_SCOPE"] = _prev
        result[scope] = len(planted)

    return result


def _compile_starter_reflexes() -> dict:
    """Arm the reflex-flagged starter atoms into ~/.echelon/reflexes.json.

    Ingesting a reflex atom only puts it in the BANK; the guard does not exist until it is
    COMPILED into the static ruleset the PreToolUse hook reads. Without this step the
    discipline seed ships inert — the new user has the lessons and none of the guards,
    which is precisely backwards for week one, when they are most likely to hit the traps
    and least likely to have their own. Returns {scope: rule_count, ...}.
    """
    from echelon_engine.atoms.reflex import compile_reflexes, _load_ruleset, RULESET

    out: dict[str, int] = {}
    starter_root = Path(__file__).resolve().parent.parent / "data" / "starter_atoms"
    if not starter_root.is_dir():
        return out
    ruleset = _load_ruleset()
    for scope_dir in sorted(starter_root.iterdir()):
        if not scope_dir.is_dir():
            continue
        scope = scope_dir.name
        rules, _skipped = compile_reflexes(scope_dir, scope)
        if not rules:
            continue
        # replace this starter scope's rules, keep everything else (same law as `reflex compile`)
        ruleset["rules"] = [r for r in ruleset["rules"] if r.get("scope") != scope] + rules
        out[scope] = len(rules)
    if out:
        RULESET.parent.mkdir(parents=True, exist_ok=True)
        RULESET.write_text(json.dumps(ruleset, indent=1), encoding="utf-8")
    return out


def cmd_setup(target: str, scope: str = "", skip_hooks: bool = False,
              skip_providers: bool = False, bootstrap: bool = False) -> dict:
    """Run `echelon setup`. Returns a report dict."""
    target = os.path.abspath(target)
    if not scope:
        scope = _resolve_scope(target)

    report = {"target": target, "scope": scope, "steps": {}, "needs_manual": []}

    # 1. init (idempotent)
    init_report = cmd_init(target, scope)
    report["steps"]["init"] = init_report["steps"]

    # 2. seed config
    try:
        from echelon_sdk.paths import ensure
        ensure()
        report["steps"]["config"] = "~/.echelon/config.json ready"
    except Exception as e:
        report["steps"]["config"] = f"unavailable: {e}"
        report["needs_manual"].append("config: create ~/.echelon/config.json")

    # 2b. bootstrap starter atoms (--bootstrap flag)
    if bootstrap:
        try:
            planted = _bootstrap_starter_atoms()
            report["steps"]["bootstrap"] = planted if planted else "no starter scopes found"
            if planted:
                scopes_str = ", ".join(f"{s}({n} atoms)" for s, n in planted.items())
                report["needs_manual"].append(
                    f"bootstrap: planted {scopes_str}. Try: echelon cartridge equip intent \"audit code honesty\"")
        except Exception as e:
            report["steps"]["bootstrap"] = f"unavailable: {e}"
            report["needs_manual"].append("bootstrap: starter atom ingest failed")

        # 2c. ARM the reflex-flagged starter atoms. Ingest fills the bank; only compile
        # creates the guards the PreToolUse hook fires from. Severable: a compile failure
        # must not fail the whole setup — the user still has a working bank.
        try:
            armed = _compile_starter_reflexes()
            report["steps"]["reflexes"] = armed if armed else "no reflex-flagged starter atoms"
            if armed:
                n = sum(armed.values())
                report["needs_manual"].append(
                    f"reflexes: {n} starter guard(s) armed — see `echelon reflex list`")
        except Exception as e:
            report["steps"]["reflexes"] = f"unavailable: {e}"
            report["needs_manual"].append(
                "reflexes: starter guards NOT armed — run `echelon reflex compile` manually")

    # 3. install hooks
    if not skip_hooks:
        try:
            from echelon_engine.atoms.hooks_cmd import cmd_install_hooks
            hooks_result = cmd_install_hooks()
            report["steps"]["hooks"] = hooks_result
        except Exception as e:
            report["steps"]["hooks"] = f"unavailable: {e}"
            report["needs_manual"].append(
                "hooks: copy hook to ~/.claude/hooks/echelon_gate.py")

    # 4. verify echelon is on PATH
    try:
        import subprocess
        result = subprocess.run(
            ["echelon", "--help"], capture_output=True, text=True, encoding="utf-8", timeout=10)
        if result.returncode == 0:
            report["steps"]["path"] = "echelon on PATH"
        else:
            report["steps"]["path"] = "echelon not on PATH — run: pip install -e . from engine dir"
            report["needs_manual"].append(
                "PATH: run `pip install -e .` from the engine directory so `echelon` is globally available")
    except FileNotFoundError:
        report["steps"]["path"] = "echelon not on PATH — run: pip install -e . from engine dir"
        report["needs_manual"].append(
            "PATH: run `pip install -e .` from the engine directory so `echelon` is globally available")
    except Exception as e:
        report["steps"]["path"] = f"PATH check skipped: {e}"

    # 5. check providers
    if not skip_providers:
        try:
            from echelon_engine.atoms.providers_cmd import _main as providers_main
            import io
            old_stdout = sys.stdout
            sys.stdout = io.StringIO()
            try:
                providers_main(["--test"])
                providers_out = sys.stdout.getvalue()
            finally:
                sys.stdout = old_stdout
            # Quick parse: count ready vs missing
            ready = providers_out.count("✓") + providers_out.count("OK")
            missing = providers_out.count("✗") + providers_out.count("missing") + providers_out.count("not set")
            report["steps"]["providers"] = f"{ready} ready, {missing} need keys"
            if missing:
                report["needs_manual"].append(
                    "API keys: run `echelon config set-key deepseek <key>` "
                    "(or gemini / anthropic / xai / lmstudio). "
                    "Or set env vars: DEEPSEEK_API_KEY, GEMINI_API_VERTEX, etc.")
        except Exception as e:
            report["steps"]["providers"] = f"unavailable: {e}"
            report["needs_manual"].append("providers: run `echelon providers --test`")

    return report


def _print_setup_report(report: dict) -> None:
    """Print the setup report."""
    print("=" * 56)
    print("  ECHELON SETUP")
    print("=" * 56)
    print(f"  target: {report['target']}")
    print(f"  scope:  {report['scope']}")
    print()

    steps = report["steps"]
    print("  [init]")
    for s, r in steps.get("init", {}).items():
        print(f"    {s}: {r}")
    print(f"  [config]      {steps.get('config', 'skipped')}")
    bootstrap = steps.get("bootstrap")
    if bootstrap is not None:
        if isinstance(bootstrap, dict) and bootstrap:
            scopes_str = ", ".join(f"{s}: {n} atoms" for s, n in bootstrap.items())
            print(f"  [bootstrap]   planted starter atoms — {scopes_str}")
        else:
            print(f"  [bootstrap]   {bootstrap}")
    print(f"  [hooks]       ", end="")
    hooks = steps.get("hooks", {})
    if isinstance(hooks, dict):
        for k, v in hooks.items():
            print(f"{k}: {v}")
    else:
        print(hooks)
    print(f"  [path]        {steps.get('path', 'skipped')}")
    print(f"  [providers]   {steps.get('providers', 'skipped')}")

    needs = report.get("needs_manual", [])
    if needs:
        print()
        print("  Manual steps remaining:")
        for n in needs:
            print(f"    - {n}")
    else:
        print()
        print("  All steps complete.")

    print()
    print("  Your project is set up. Start with:")
    print(f"    echelon recall --scope {report['scope']} --warm \"your first intent\"")
    print(f"    echelon cartridge list")
    print("=" * 56)


# ── CLI entry points ──────────────────────────────────────────────────────────

def _main_init(argv=None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    ap = argparse.ArgumentParser(
        prog="echelon init",
        description="Bootstrap a project directory with ECHELON memory infrastructure.")
    ap.add_argument("--target", default=".",
                    help="Target directory (default: cwd)")
    ap.add_argument("--scope", default="",
                    help="Bank scope (default: kebab of directory name)")
    a = ap.parse_args(argv)
    report = cmd_init(a.target, a.scope)
    _print_init_report(report)
    return 0


def _main_setup(argv=None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    ap = argparse.ArgumentParser(
        prog="echelon setup",
        description="Guided first-time ECHELON setup: init + hooks + config + providers.")
    ap.add_argument("--target", default=".",
                    help="Target directory (default: cwd)")
    ap.add_argument("--scope", default="",
                    help="Bank scope (default: kebab of directory name)")
    ap.add_argument("--skip-hooks", action="store_true",
                    help="Skip hook installation")
    ap.add_argument("--skip-providers", action="store_true",
                    help="Skip provider verification")
    ap.add_argument("--bootstrap", action="store_true",
                    help="Ingest packaged starter cartridge atoms into the bank")
    a = ap.parse_args(argv)
    report = cmd_setup(a.target, a.scope, skip_hooks=a.skip_hooks,
                       skip_providers=a.skip_providers, bootstrap=a.bootstrap)
    _print_setup_report(report)
    return 0
