"""atlas — manage the GitHub beta-svc-atlas (structured deploy/topology map) per scope.

One disciplined state-change call. The point: today, updating an atlas node is a manual
multi-step chore (hand-edit define/<node>.json -> node tools/validate.js ->
node tools/contract-check.js -> git PR), so it gets SKIPPED and the map DRIFTS. This
command makes a state change ONE call that ALWAYS runs the full discipline
(mutate -> validate -> contract-check), so "the atlas is part of done" is enforced by
tooling.

Subcommands:
  atlas link      --scope <s> --path <repo>       bind a scope to its atlas repo
  atlas status    --scope <s>                      repo path, node count, git dirty, last validate
  atlas set       --scope <s> --node <name>        THE core state-change (atomic: revert on gate fail)
                   --field <k> --value <v> [--reason ...] [--commit]
  atlas nodes     --scope <s>                      list nodes + status
  atlas validate  --scope <s>                      run validate.js + contract-check.js (read-only)

PER-SCOPE BINDING: resolution order:
  1. --atlas <path> flag (explicit override)
  2. scope's configured atlas_repo in ~/.echelon/config.json
  3. error with a clear message

ATOMIC-OR-REVERT: `atlas set` loads the node JSON, mutates the field, writes it back, runs
validate.js AND contract-check.js. If EITHER fails, the file is REVERTED to its original bytes.
A half-applied invalid node is the exact drift this command exists to prevent.

WRAP, DON'T REIMPLEMENT: subprocess calls to `node tools/validate.js` / `contract-check.js`
in the atlas repo — the JS source of truth is authoritative.

OUTWARD IS THE OWNER'S BUTTON: default = mutate + validate + stage (git add), print the
commit command. Only --commit commits. No push, ever, from this tool.

NAMESPACE: This is the GitHub beta-svc-atlas deploy map. NOT the bank's edge-graph atlas
(group-scope, graph). The two are different atlases.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from collections import OrderedDict
from pathlib import Path


# ── Config helpers (same store as config_report: ~/.echelon/config.json) ─────

def _config_path() -> Path:
    return Path.home() / ".echelon" / "config.json"


def _load_config() -> dict:
    cfg = _config_path()
    if not cfg.exists():
        return {}
    try:
        return json.loads(cfg.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _save_config(data: dict) -> None:
    cfg = _config_path()
    cfg.parent.mkdir(parents=True, exist_ok=True)
    tmp = cfg.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
                   encoding="utf-8")
    tmp.replace(cfg)


def _get_atlas_repo(scope: str) -> str | None:
    """Read scope's atlas_repo from config."""
    data = _load_config()
    repos = data.get("atlas_repos", {})
    if isinstance(repos, dict):
        return repos.get(scope)
    return None


def _set_atlas_repo(scope: str, path: str) -> None:
    """Write scope's atlas_repo to config."""
    data = _load_config()
    repos = data.setdefault("atlas_repos", {})
    repos[scope] = path
    data["atlas_repos"] = repos
    _save_config(data)


# ── Resolution ───────────────────────────────────────────────────────────────

def _resolve_atlas_path(scope: str, explicit: str | None = None) -> Path:
    """Resolve atlas repo path for a scope.

    Order:
      1. --atlas <path> explicit override
      2. scope's configured atlas_repo in config
      3. error
    """
    if explicit:
        p = Path(explicit).resolve()
        if not p.exists():
            print(f"atlas: explicit path does not exist: {p}", file=sys.stderr)
            sys.exit(1)
        return p

    configured = _get_atlas_repo(scope)
    if configured:
        p = Path(configured).resolve()
        if not p.exists():
            print(f"atlas: configured path for scope '{scope}' does not exist: {p}", file=sys.stderr)
            print(f"  re-link it: echelon atlas link --scope {scope} --path <repo>", file=sys.stderr)
            sys.exit(1)
        return p

    print(f"atlas: no atlas repo configured for scope '{scope}'.", file=sys.stderr)
    print(f"  set it: echelon atlas link --scope {scope} --path <repo>", file=sys.stderr)
    print(f"  or pass: --atlas <path>", file=sys.stderr)
    sys.exit(1)


# ── JSON load/save with key-order preservation ────────────────────────────────

def _load_node_json(path: Path) -> OrderedDict:
    """Load a define/*.json file preserving key order."""
    raw = path.read_text(encoding="utf-8")
    return json.loads(raw, object_pairs_hook=OrderedDict)


def _save_node_json(path: Path, data: OrderedDict) -> None:
    """Write JSON with 2-space indent, preserving key order, trailing newline."""
    text = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    path.write_text(text, encoding="utf-8")


def _set_dotted(data: OrderedDict, key_path: str, value) -> None:
    """Set a dotted-path key in an OrderedDict. Appends new keys at the end."""
    parts = key_path.split(".")
    d = data
    for part in parts[:-1]:
        if part not in d or not isinstance(d[part], dict):
            d[part] = OrderedDict()
        d = d[part]
    d[parts[-1]] = value


# ── Gate runners ──────────────────────────────────────────────────────────────

def _check_node_available() -> None:
    """Verify `node` is on PATH."""
    try:
        subprocess.run(["node", "--version"], capture_output=True, check=True, timeout=10)
    except (subprocess.CalledProcessError, FileNotFoundError):
        print("atlas: 'node' not found on PATH. The atlas JS gate tools require Node.js.",
              file=sys.stderr)
        sys.exit(1)


def _run_gate(atlas_path: Path, tool: str) -> subprocess.CompletedProcess:
    """Run a JS gate tool from the atlas repo's tools/ directory."""
    script = atlas_path / "tools" / tool
    if not script.exists():
        print(f"atlas: gate script not found: {script}", file=sys.stderr)
        sys.exit(1)
    return subprocess.run(
        ["node", str(script)],
        cwd=str(atlas_path),
        capture_output=True,
        text=True,
        timeout=60,
    )


def _run_validate(atlas_path: Path) -> subprocess.CompletedProcess:
    return _run_gate(atlas_path, "validate.js")


def _run_contract_check(atlas_path: Path) -> subprocess.CompletedProcess:
    return _run_gate(atlas_path, "contract-check.js")


def _gate_result_text(proc: subprocess.CompletedProcess) -> str:
    """Combine stdout and stderr for display."""
    parts = []
    if proc.stdout and proc.stdout.strip():
        parts.append(proc.stdout.strip())
    if proc.stderr and proc.stderr.strip():
        parts.append(proc.stderr.strip())
    return "\n".join(parts)


# ── Subcommands ──────────────────────────────────────────────────────────────

def cmd_link(args: argparse.Namespace) -> int:
    """Bind a scope to its atlas repo path."""
    scope = args.scope
    path = str(Path(args.path).resolve())
    if not Path(path).exists():
        print(f"atlas link: path does not exist: {path}", file=sys.stderr)
        return 1
    _set_atlas_repo(scope, path)
    print(f"atlas: scope '{scope}' linked to atlas repo: {path}")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    """Show repo path, node count, git dirty state, last validate result."""
    _check_node_available()
    atlas_path = _resolve_atlas_path(args.scope, args.atlas)

    define_dir = atlas_path / "define"
    if not define_dir.exists():
        print(f"atlas status: define/ directory not found in {atlas_path}", file=sys.stderr)
        return 1

    nodes = sorted(define_dir.glob("*.json"))
    # Exclude _meta.json from the count
    node_count = sum(1 for n in nodes if not n.name.startswith("_"))

    print(f"scope:      {args.scope}")
    print(f"atlas repo: {atlas_path}")
    print(f"node count: {node_count}")

    # Git dirty state
    try:
        git_status = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=str(atlas_path),
            capture_output=True,
            text=True,
            timeout=15,
        )
        if git_status.returncode == 0:
            dirty = [l for l in git_status.stdout.splitlines() if l.strip()]
            print(f"git state:  {'clean' if not dirty else f'{len(dirty)} dirty file(s)'}")
            if dirty:
                for line in dirty[:10]:
                    print(f"  {line}")
                if len(dirty) > 10:
                    print(f"  ... and {len(dirty) - 10} more")
        else:
            print("git state:  (not a git repo or git failed)")
    except Exception:
        print("git state:  (git not available)")

    # Last validate result (read-only)
    print()
    print("--- validate.js ---")
    vr = _run_validate(atlas_path)
    print(_gate_result_text(vr))
    if vr.returncode != 0:
        print(f"  (exit {vr.returncode})")

    print()
    print("--- contract-check.js ---")
    cr = _run_contract_check(atlas_path)
    print(_gate_result_text(cr))
    if cr.returncode != 0:
        print(f"  (exit {cr.returncode})")

    return 0


def cmd_set(args: argparse.Namespace) -> int:
    """THE core state-change. Atomic: mutate -> regenerate derived -> validate -> contract-check.
    Revert on ANY gate failure. Never leave the node in an invalid state."""
    _check_node_available()
    atlas_path = _resolve_atlas_path(args.scope, args.atlas)

    node_file = atlas_path / "define" / f"{args.node}.json"
    if not node_file.exists():
        print(f"atlas set: node not found: {node_file}", file=sys.stderr)
        print(f"  list nodes: echelon atlas nodes --scope {args.scope}", file=sys.stderr)
        print(f"  or scaffold: node tools/scaffold-node.js (in the atlas repo)", file=sys.stderr)
        return 1

    # --- Parse the value ---
    value = _parse_value(args.value)

    # --- Snapshot original bytes (for atomic revert) ---
    spec_file = atlas_path / "SPECIFICATION.md"
    original_node_bytes = node_file.read_bytes()
    original_spec_bytes = spec_file.read_bytes() if spec_file.exists() else None

    # --- Load, mutate, save ---
    try:
        data = _load_node_json(node_file)
    except json.JSONDecodeError as e:
        print(f"atlas set: invalid JSON in {node_file}: {e}", file=sys.stderr)
        return 1

    _set_dotted(data, args.field, value)
    _save_node_json(node_file, data)

    # --- Regenerate derived artifacts (the spec table is built from nodes) ---
    gen_spec_script = atlas_path / "tools" / "gen-spec-table.js"
    if gen_spec_script.exists():
        sp = subprocess.run(
            ["node", str(gen_spec_script)],
            cwd=str(atlas_path),
            capture_output=True,
            text=True,
            timeout=30,
        )
        if sp.returncode != 0:
            # Revert node + spec
            node_file.write_bytes(original_node_bytes)
            if original_spec_bytes is not None:
                spec_file.write_bytes(original_spec_bytes)
            print("atlas set: GATE FAILED — gen-spec-table.js error.", file=sys.stderr)
            if sp.stderr.strip():
                print(sp.stderr.strip(), file=sys.stderr)
            print("The file has been REVERTED. No changes were left in the atlas.",
                  file=sys.stderr)
            return 1

    # --- Run the gate ---
    vr = _run_validate(atlas_path)
    cr = _run_contract_check(atlas_path)

    gate_ok = vr.returncode == 0 and cr.returncode == 0

    if not gate_ok:
        # --- REVERT: restore original bytes ---
        node_file.write_bytes(original_node_bytes)
        if original_spec_bytes is not None:
            spec_file.write_bytes(original_spec_bytes)

        print("atlas set: GATE FAILED — node reverted to original state.", file=sys.stderr)
        if vr.returncode != 0:
            print(file=sys.stderr)
            print("--- validate.js FAILED ---", file=sys.stderr)
            print(_gate_result_text(vr), file=sys.stderr)
        if cr.returncode != 0:
            print(file=sys.stderr)
            print("--- contract-check.js FAILED ---", file=sys.stderr)
            print(_gate_result_text(cr), file=sys.stderr)
        print(file=sys.stderr)
        print("The file has been REVERTED. No changes were left in the atlas.",
              file=sys.stderr)
        return 1

    # --- Gate passed: stage the change ---
    print(f"atlas set: '{args.node}' {args.field} -> {json.dumps(value)}")
    print(f"  validate.js:       OK")
    print(f"  contract-check.js: OK")
    print()

    # Show the diff
    try:
        diff_result = subprocess.run(
            ["git", "diff", "--", f"define/{args.node}.json", "SPECIFICATION.md"],
            cwd=str(atlas_path),
            capture_output=True,
            text=True,
            timeout=15,
        )
        if diff_result.stdout.strip():
            print("--- diff ---")
            print(diff_result.stdout.strip())
    except Exception:
        pass

    # Stage the files (node + derived SPECIFICATION.md)
    stage_files = [f"define/{args.node}.json"]
    if original_spec_bytes is not None:
        stage_files.append("SPECIFICATION.md")
    try:
        subprocess.run(
            ["git", "add", "--", *stage_files],
            cwd=str(atlas_path),
            capture_output=True,
            text=True,
            timeout=15,
            check=True,
        )
        print(f"  staged: {', '.join(stage_files)}")

        if args.commit:
            reason = args.reason or f"atlas: set {args.node}.{args.field} = {json.dumps(value)}"
            subprocess.run(
                ["git", "commit", "-m", reason],
                cwd=str(atlas_path),
                capture_output=True,
                text=True,
                timeout=15,
                check=True,
            )
            print(f"  committed: {reason}")
            print(f"  (no push — outward is the owner's button)")
        else:
            commit_cmd = (
                f'git -C "{atlas_path}" commit -m "'
                f"atlas: set {args.node}.{args.field} = {json.dumps(value)}"
                f'"'
            )
            print(f"  commit with: {commit_cmd}")
    except subprocess.CalledProcessError as e:
        print(f"atlas set: git stage failed: {e.stderr}", file=sys.stderr)
        # File is valid but staging failed — still a partial success
        return 1

    return 0


def cmd_nodes(args: argparse.Namespace) -> int:
    """List nodes + their status."""
    atlas_path = _resolve_atlas_path(args.scope, args.atlas)
    define_dir = atlas_path / "define"
    if not define_dir.exists():
        print(f"atlas nodes: define/ directory not found in {atlas_path}", file=sys.stderr)
        return 1

    nodes = sorted(
        [n for n in define_dir.glob("*.json") if not n.name.startswith("_")],
        key=lambda n: n.name,
    )

    if not nodes:
        print("(no nodes)")
        return 0

    # Determine column widths
    max_name = max(len(n.stem) for n in nodes)
    max_name = max(max_name, 4)

    print(f"{'NODE':<{max_name}}  {'STATUS':<12}  {'TYPE':<14}  TITLE")
    print(f"{'─' * max_name}  {'─' * 12}  {'─' * 14}  {'─' * 40}")

    for n in nodes:
        try:
            data = json.loads(n.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            print(f"{n.stem:<{max_name}}  {'(invalid JSON)':<12}")
            continue
        status = data.get("status", "?")
        ntype = data.get("type", "?")
        title = (data.get("title") or "")[:60]
        print(f"{n.stem:<{max_name}}  {status:<12}  {ntype:<14}  {title}")

    print(f"\n{len(nodes)} node(s) in scope '{args.scope}'")
    return 0


def cmd_validate(args: argparse.Namespace) -> int:
    """Run validate.js + contract-check.js, report results (read-only)."""
    _check_node_available()
    atlas_path = _resolve_atlas_path(args.scope, args.atlas)

    print(f"atlas validate: running gate against {atlas_path}")
    print()

    vr = _run_validate(atlas_path)
    print("--- validate.js ---")
    print(_gate_result_text(vr))

    print()

    cr = _run_contract_check(atlas_path)
    print("--- contract-check.js ---")
    print(_gate_result_text(cr))

    if vr.returncode == 0 and cr.returncode == 0:
        print("\natlas validate: CLEAN — both gates passed.")
        return 0
    else:
        print("\natlas validate: PROBLEMS FOUND — review output above.", file=sys.stderr)
        return 1


# ── Value parsing ─────────────────────────────────────────────────────────────

def _parse_value(raw: str):
    """Parse --value into a Python object. Tries JSON first, falls back to string."""
    if raw is None:
        return None
    stripped = raw.strip()
    # Try JSON parse (handles numbers, booleans, null, arrays, objects, quoted strings)
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        pass
    # Fallback: return as-is (plain string)
    return raw


# ── CLI entry ─────────────────────────────────────────────────────────────────

def _build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="echelon atlas",
        description="Manage the GitHub beta-svc-atlas (structured deploy/topology map) "
                    "per scope — one disciplined state-change call.",
    )
    sub = ap.add_subparsers(dest="subcommand", title="subcommands")

    # atlas link
    p_link = sub.add_parser("link", help="bind a scope to its atlas repo path")
    p_link.add_argument("--scope", required=True, help="the bank scope")
    p_link.add_argument("--path", required=True, help="path to the atlas repo on disk")

    # atlas status
    p_status = sub.add_parser("status", help="show repo path, node count, git dirty state, "
                                              "last validate result")
    p_status.add_argument("--scope", required=True, help="the bank scope")
    p_status.add_argument("--atlas", default=None, help="explicit atlas repo path (overrides config)")

    # atlas set — THE core state-change
    p_set = sub.add_parser("set", help="mutate a node field with full discipline "
                                       "(mutate -> validate -> contract-check; revert on gate fail)")
    p_set.add_argument("--scope", required=True, help="the bank scope")
    p_set.add_argument("--node", required=True, help="node name (matches define/<node>.json stem)")
    p_set.add_argument("--field", required=True, help="field key (supports dotted paths e.g. status, port, bind)")
    p_set.add_argument("--value", required=True, help="new value (JSON for complex types: arrays, objects, numbers, booleans; plain text for strings)")
    p_set.add_argument("--reason", default=None, help="why this change (used as commit message if --commit)")
    p_set.add_argument("--commit", action="store_true", help="also commit the change (no push — outward is the owner's button)")
    p_set.add_argument("--atlas", default=None, help="explicit atlas repo path (overrides config)")

    # atlas nodes
    p_nodes = sub.add_parser("nodes", help="list nodes + their status")
    p_nodes.add_argument("--scope", required=True, help="the bank scope")
    p_nodes.add_argument("--atlas", default=None, help="explicit atlas repo path (overrides config)")

    # atlas validate
    p_val = sub.add_parser("validate", help="run validate.js + contract-check.js (read-only)")
    p_val.add_argument("--scope", required=True, help="the bank scope")
    p_val.add_argument("--atlas", default=None, help="explicit atlas repo path (overrides config)")

    return ap


def main(argv=None) -> int:
    if argv is None:
        argv = sys.argv[1:]

    ap = _build_parser()
    args = ap.parse_args(argv)

    if args.subcommand is None:
        ap.print_help()
        return 1

    if args.subcommand == "link":
        return cmd_link(args)
    elif args.subcommand == "status":
        return cmd_status(args)
    elif args.subcommand == "set":
        return cmd_set(args)
    elif args.subcommand == "nodes":
        return cmd_nodes(args)
    elif args.subcommand == "validate":
        return cmd_validate(args)
    else:
        ap.print_help()
        return 1


if __name__ == "__main__":
    sys.exit(main())
