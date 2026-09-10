"""Contract-driven projections for the Claude and native Codex ECHELON harnesses.

This is deliberately separate from ``echelon sync``.  The latter replicates bank
events to a remote peer; this module projects the *local* scope into the markdown
surfaces that the two harnesses load.  The contract is the only writable policy
input.  The bank, compiled reflex rules, and gate renderer remain source data.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path


BEGIN = "<!-- ECHELON-HARNESS-SYNC:BEGIN -->"
END = "<!-- ECHELON-HARNESS-SYNC:END -->"

# The room slice (contract version 2): resume_brief() projected between these markers,
# regenerated on every sync, bounded by room_max_bytes so the MEMORY.md read ceiling holds.
ROOM_BEGIN = "<!-- ECHELON-ROOM:BEGIN -->"
ROOM_END = "<!-- ECHELON-ROOM:END -->"
ROOM_MAX_BYTES = 1200
ROOM_TARGETS_DEFAULT = ("claude_memory", "codex_memory", "codex_banner")
# The gate banner region ends at the first `---` rule (same convention as atoms/gate.split_banner).
_RULE = re.compile(r"^---\s*$", re.M)


def load_contract(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("version") not in (1, 2):
        raise ValueError("contract.version must be 1 or 2")
    if not isinstance(data.get("targets"), dict) or not data["targets"]:
        raise ValueError("contract.targets must be a non-empty object")
    return data


def _target(contract: dict, name: str) -> Path:
    raw = contract["targets"].get(name)
    if not raw:
        raise ValueError(f"contract.targets.{name} is required")
    return Path(os.path.expandvars(os.path.expanduser(raw)))


def _reflexes(path: Path, scope: str) -> list[dict]:
    if not path.exists():
        return []
    payload = json.loads(path.read_text(encoding="utf-8"))
    rules = payload.get("rules", payload if isinstance(payload, list) else [])
    return [r for r in rules if r.get("scope") in (scope, "", "global", None)]


def _resolve_room(contract: dict, cwd: str | None = None) -> tuple[Path | None, str]:
    """The room for this sync — ladder (spec S8 V2, INC-0002): the contract's
    optional `room` key → the registry room whose scope == contract.scope →
    cwd. Returns (state_dir_or_None, source_name). Never raises: a broken
    room leg falls through to cwd, exactly like the ancestor walk."""
    from echelon_engine import workcycle  # guarded: sibling-owned module (W3 doors)
    try:
        raw = contract.get("room")
        if raw:
            p = Path(os.path.expandvars(os.path.expanduser(raw)))
            state = workcycle.room_path(str(p))
            if state is None and (p / "room.json").exists():
                state = p  # the key already IS the state dir
            if state is not None:
                return state, "contract"
        scope = contract.get("scope")
        if scope and scope != "auto":
            reg = getattr(workcycle, "registry_room_by_scope", None)
            state = reg(scope) if reg else None
            if state is not None:
                return state, "registry"
    except Exception:
        pass
    try:
        return workcycle.room_path(cwd or None), "cwd"
    except Exception:
        return None, "cwd"


def _child_binding(contract: dict, parent_name: str) -> tuple[Path, str]:
    """SPEC-S9 step 2 child contract (gate 6): `parent` names the registered
    parent room; the child state comes from contract.room (registry name or
    path) or the cwd ancestor walk. The child scope must EQUAL the parent's
    scope — inheritance is explicit, never name-based; a mismatch (or a room
    that declares a different parent) is a HARD error. Returns
    (child_state_dir, inherited_scope); the child owns no independent scope."""
    from echelon_engine import workcycle  # guarded: sibling-owned module (W3 doors)
    pentry = workcycle.registry_entry(parent_name)
    pstate = Path(pentry["path"]).resolve() if pentry and pentry.get("path") else None
    if pstate is None or not (pstate / "room.json").exists():
        raise ValueError(f"contract.parent {parent_name!r} is not a registered room")
    parent_scope = pentry.get("scope")
    if not parent_scope:
        try:
            parent_scope = (json.loads((pstate / "room.json").read_text(encoding="utf-8")) or {}).get("scope")
        except Exception:
            parent_scope = None
    if not parent_scope:
        raise ValueError(f"parent room {parent_name!r} declares no scope")
    raw = contract.get("room")
    if raw:
        p = Path(os.path.expandvars(os.path.expanduser(raw)))
        try:
            state = Path(workcycle.room_dir(str(p))).resolve()
        except FileNotFoundError:
            raise ValueError(
                f"child contract.room {raw!r} is not a room (parent {parent_name!r})") from None
    else:
        state = workcycle.room_path(None)
        if state is None:
            raise ValueError(
                f"child contract names no room and cwd resolves none (parent {parent_name!r})")
        state = Path(state).resolve()
    try:
        rj = json.loads((state / "room.json").read_text(encoding="utf-8")) or {}
    except Exception:
        rj = {}
    if rj.get("parent") != parent_name:
        raise ValueError(
            f"room at {state} declares parent {rj.get('parent')!r}, not {parent_name!r} — "
            f"inheritance is explicit, not name-based")
    if rj.get("scope") != parent_scope:
        raise ValueError(
            f"child scope {rj.get('scope')!r} does not equal parent {parent_name!r} scope "
            f"{parent_scope!r} — inheritance is explicit, not name-based")
    return state, parent_scope


def _room_region_from(room: Path | None, max_bytes: int) -> str:
    """The ECHELON-ROOM slice for a resolved room: `resume_brief(room)` between the room
    markers, truncated to `max_bytes` with a `…(+N bytes)` tail. '' when no room exists —
    the region is then REMOVED from targets, never left stale."""
    try:
        from echelon_engine import workcycle  # guarded: sibling-owned module (W3 doors)
        brief = (workcycle.resume_brief(room) or "").strip() if room else ""
    except Exception:
        return ""
    if not brief:
        return ""
    raw = brief.encode("utf-8")
    if len(raw) > max_bytes:  # cap on BYTES (gate C2: a char count let a 1200-"byte" cap keep ~4800 bytes)
        brief = raw[:max_bytes].decode("utf-8", "ignore") + f"\n…(+{len(raw) - max_bytes} bytes)"
    return f"{ROOM_BEGIN}\n{brief}\n{ROOM_END}"


def _room_region(cwd: str | None, max_bytes: int) -> str:
    """The cwd-based ECHELON-ROOM slice (the pre-S8 door, kept for back-compat)."""
    try:
        from echelon_engine import workcycle  # guarded: sibling-owned module (W3 doors)
        room = workcycle.room_path(cwd or None)
    except Exception:
        room = None
    return _room_region_from(room, max_bytes)


def _apply_room(text: str, room_region: str) -> str:
    """Replace the ECHELON-ROOM region in `text` — in place when the markers bound it (same
    semantics as the harness region, so re-syncs never drift), inserted directly AFTER the gate
    banner region (first `---` rule) when the file carries a gate banner, else before the
    harness-sync region, else appended. An empty `room_region` removes any stale room slice."""
    start, end = text.find(ROOM_BEGIN), text.find(ROOM_END)
    if start >= 0 and end >= start:
        text = text[:start] + room_region + text[end + len(ROOM_END):]
        if not room_region:
            # collapse the blank-line run left where the region stood
            text = re.sub(r"\n{3,}", "\n\n", text)
        return text
    if not room_region:
        return text
    if "ECHELON-GATE-BANNER" in text:
        rule = _RULE.search(text)
        if rule:
            cut = rule.end()
            return text[:cut] + "\n" + room_region + "\n" + text[cut:]
    begin = text.find(BEGIN)
    if begin >= 0:
        return text[:begin] + room_region + "\n" + text[begin:]
    return text.rstrip() + "\n\n" + room_region + "\n"


def _write_text(path: Path, text: str) -> None:
    """Write projected text, creating parent dirs if needed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _snapshot(scope: str, reflex_path: Path, atom_limit: int) -> dict:
    from .cards import CardStore
    from .store import SeedStore

    cards = CardStore()
    atoms = sorted(SeedStore().seeds(scope=scope), key=lambda s: (-s.score, -s.ts, s.coordinate))
    return {
        "scope": scope,
        "atom_count": cards.count_atoms_in_scope(scope),
        "atoms": atoms[:atom_limit],
        "reflexes": _reflexes(reflex_path, scope),
        "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
    }


_VERB_LINE = re.compile(r"^ {4}([A-Za-z][\w-]*)(?:\s{2,}|\s*$)")


def _framework_probe(entry: str) -> tuple[bool, list[str]]:
    """Probe door: does the framework's console entry answer, and which verbs does it
    advertise? shutil.which first (cheap, no subprocess if absent — mirrors fw_cmd.py's own
    probe), then a bounded `--help` run so a stale/broken shim on PATH is not reported as
    installed. The verb list is PARSED from that same `--help` (argparse's positional block:
    four-space-indented names) so the projection can never drift from the delivered verb
    set — gate round 2 (2026-09-02) caught a hand-picked 8-of-18 list. Never raises."""
    exe = shutil.which(entry) if entry else None
    if not exe:
        return False, []
    try:
        r = subprocess.run([exe, "--help"], capture_output=True, text=True,
                           timeout=15, encoding="utf-8", errors="replace")
    except Exception:
        return False, []
    if r.returncode != 0:
        return False, []
    verbs: list[str] = []
    in_positional = False
    for line in (r.stdout or "").splitlines():
        if line.startswith("positional arguments"):
            in_positional = True
            continue
        if in_positional and line and not line.startswith(" "):
            break  # next argparse section (options/epilog)
        if in_positional:
            m = _VERB_LINE.match(line)
            if m and m.group(1) != "VERB" and m.group(1) not in verbs:
                verbs.append(m.group(1))
    return True, verbs


def _framework_installed(entry: str) -> bool:
    return _framework_probe(entry)[0]


def _framework_region(contract: dict) -> str:
    """Deliverable 3 (OPEN-0060 phase A): a `## ECHELON framework door` region, generated
    from contract.framework — one line: installed?, version, verbs. '' when the contract
    carries no `framework` block (nothing to project). Never raises: an install-probe
    failure degrades to 'installed: unknown', it never drops the region silently."""
    fw = contract.get("framework")
    if not isinstance(fw, dict) or not fw:
        return ""
    entry = fw.get("entry", "echelon-fw")
    version = fw.get("version", "?")
    path = fw.get("path", "")
    verbs: list[str] = []
    try:
        installed, verbs = _framework_probe(entry)
        status = "yes" if installed else "no"
    except Exception:
        status = "unknown"
    verbs_line = (f"- Verbs (from `{entry} --help`): `{'|'.join(verbs)}` "
                  if verbs else f"- Verbs: unknown (`{entry} --help` did not answer) ")
    return "\n".join((
        "## ECHELON framework door",
        f"- Installed: **{status}** · entry `{entry}` · version `{version}`"
        + (f" · path `{path}`" if path else "") + ".",
        verbs_line + f"(also reachable via `python -X utf8 -m echelon_engine fw <args...>`).",
    ))


def _banner(snapshot: dict, contract_path: Path, contract: dict | None = None) -> str:
    text = "\n".join((
        "## ECHELON harness projection",
        f"- Scope: `{snapshot['scope']}` · live atoms: **{snapshot['atom_count']}** · active reflexes: **{len(snapshot['reflexes'])}**.",
        "- **Harness-sync contract:** this projection is derived state; keep the contract authoritative and refresh it after bank/reflex changes.",
        "- Refresh after an ingest, reflex compile, or scope change: "
        f"`python -X utf8 -m echelon_engine harness-sync --contract \"{contract_path}\"`.",
        "- This region is generated from the contract and local bank; surrounding instructions stay owner-authored.",
    ))
    fw_region = _framework_region(contract or {})
    if fw_region:
        text = text + "\n\n" + fw_region
    return text


def _codex_banner(snapshot: dict, contract_path: Path, contract: dict | None = None) -> str:
    return _banner(snapshot, contract_path, contract) + "\n" + "\n".join(("", "### Native Codex surfaces", "",
        "- [Current memory](.codex/ECHELON_MEMORY.md) · [atom list](.codex/ECHELON_ATOMS.md) · [memory index](.codex/MEMORY_INDEX.md) · [scope reflexes](.codex/ECHELON_REFLEXES.md)."))


def _memory(snapshot: dict, contract_path: Path, contract: dict | None = None) -> str:
    return "\n".join((_banner(snapshot, contract_path, contract), "", "### Current substrate", "",
        "Use `echelon recall --scope " + snapshot["scope"] + " --warm \"<intent>\"` before load-bearing work.",
        "Use `echelon remember <slug>` for full atom bodies; do not treat this projection as the source of truth."))


def _atom_list(snapshot: dict) -> str:
    lines = [f"# ECHELON atoms — {snapshot['scope']}", "", f"Live v2 atoms: {snapshot['atom_count']}. Projection shows the top {len(snapshot['atoms'])} by earned score.", ""]
    for atom in snapshot["atoms"]:
        claim = " ".join((atom.content or "").splitlines()[:1]).strip().replace("|", "\\|")
        lines.append(f"- `{atom.coordinate or atom.id[:12]}` · score {atom.score:.1f} · {claim[:180]}")
    return "\n".join(lines)


def _memory_index(snapshot: dict) -> str:
    lines = [f"# ECHELON memory index — {snapshot['scope']}", "", "| Coordinate | Score | Kind |", "| --- | ---: | --- |"]
    for atom in snapshot["atoms"]:
        lines.append(f"| `{atom.coordinate or atom.id[:12]}` | {atom.score:.1f} | {atom.kind} |")
    return "\n".join(lines)


def _reflex_list(snapshot: dict) -> str:
    lines = [f"# ECHELON reflexes — {snapshot['scope']}", "", "Compiled rules selected for this scope. They are action guards, not memory ranking.", ""]
    if not snapshot["reflexes"]:
        lines.append("_No compiled reflexes for this scope._")
    for rule in snapshot["reflexes"]:
        action = rule.get("action", "warn")
        tool = rule.get("tool", rule.get("reflex-tool", "*"))
        name = rule.get("name", rule.get("id", "unnamed-rule"))
        lines.append(f"- `{name}` · {action} · tool `{tool}`")
    return "\n".join(lines)


def _existing_room(text: str) -> str | None:
    """The ROOM slice already in `text` (markers inclusive), or None when it carries none."""
    start, end = text.find(ROOM_BEGIN), text.find(ROOM_END)
    if start >= 0 and end >= start:
        return text[start:end + len(ROOM_END)]
    return None


def project(contract_path: Path, *, cwd: str | None = None, check: bool = False,
            room_tolerant: bool = False) -> dict:
    """Project the harness surfaces. `room_tolerant` (check mode only) keeps a target's
    EXISTING room slice when judging drift, so live receipt churn between an apply and its
    verify does not read as harness drift; such targets land in `room_drift` instead
    (the room slice is derived state that carries its own staleness -- a freshness gate
    that races every seat writing receipts can never verify)."""
    contract = load_contract(contract_path)
    child_state = None
    if contract.get("parent"):
        # CHILD CONTRACT (SPEC-S9 step 2, gate 6): the child names its parent and
        # omits an independent bank scope — the scope is INHERITED after verifying
        # the child room declares the parent's scope. Mismatch is a hard error.
        if int(contract.get("version", 1)) < 2:
            raise ValueError("contract.parent requires contract.version >= 2")
        child_state, scope = _child_binding(contract, contract["parent"])
        declared = contract.get("scope")
        if declared not in (None, "auto") and declared != scope:
            raise ValueError(f"contract.scope {declared!r} conflicts with the inherited "
                             f"parent scope {scope!r} — a child contract inherits, never overrides")
    else:
        configured_scope = contract.get("scope", "auto")
        if configured_scope == "auto":
            from .resolve_scope import resolve_scope, UnknownScopeError
            try:
                scope = resolve_scope(cwd)
            except UnknownScopeError as e:
                # fail closed (OPEN-0036): projecting a harness for a minted scope writes a
                # CLAUDE.md region for an estate that does not exist. The contract's own
                # "scope" field is the explicit door.
                raise ValueError(
                    f'contract.scope is "auto" but {e}\n'
                    f'  Set "scope" in {contract_path} to name the scope explicitly.') from e
        else:
            scope = configured_scope
    atom_limit = int(contract.get("atom_limit", 100))
    if atom_limit < 1 or atom_limit > 500:
        raise ValueError("contract.atom_limit must be between 1 and 500")
    reflex_path = Path(os.path.expanduser(contract.get("reflex_path", "~/.echelon/reflexes.json")))
    reflex_health_path = Path(os.path.expanduser(contract.get("reflex_health_path", "~/.echelon/reflex_health.json")))
    snapshot = _snapshot(scope, reflex_path, atom_limit)
    # Contract version 2 room slice. v1 (or absent version) behaves exactly as before: no markers.
    version = int(contract.get("version", 1))
    room_targets: list[str] = []
    if version >= 2:
        room_targets = contract.get("room_targets", list(ROOM_TARGETS_DEFAULT))
        if not isinstance(room_targets, list):
            raise ValueError("contract.room_targets must be a list")
        for name in room_targets:
            if name not in contract["targets"]:
                raise ValueError(f"contract.room_targets.{name} is not a declared target")
        room_max_bytes = int(contract.get("room_max_bytes", ROOM_MAX_BYTES))
        if room_max_bytes < 1:
            raise ValueError("contract.room_max_bytes must be >= 1")
        # spec S8 V2 (INC-0002): the room comes from the CONTRACT, not cwd —
        # contract `room` key > registry by contract.scope > cwd fallback.
        # A child contract is resolved by _child_binding and reported via "child".
        if child_state is not None:
            room, room_via = child_state, "child"
        else:
            room, room_via = _resolve_room(contract, cwd)
        room_region = _room_region_from(room, room_max_bytes)
    else:
        room, room_via, room_region = None, "", ""
    # The gate writer owns the top banner grammar.  It replaces only the banner
    # before the first rule and preserves the memory index/body verbatim.
    gate_targets = contract.get("gate_memory_targets", [])
    if not isinstance(gate_targets, list):
        raise ValueError("contract.gate_memory_targets must be a list")
    gate_drift = []
    for name in gate_targets:
        if name not in ("claude_memory", "codex_memory"):
            raise ValueError("gate_memory_targets accepts only claude_memory or codex_memory")
        path = _target(contract, name)
        if check:
            from .gate import gate_text, gate_text_lean, split_banner
            existing = path.read_text(encoding="utf-8") if path.exists() else ""
            old_banner, _ = split_banner(existing)
            expected = gate_text_lean(scope, write_target=str(path)) if contract.get("lean_gate", True) else gate_text(scope, write_target=str(path))
            # split_banner intentionally excludes the newline after the rule;
            # gate_text includes it.  Normalize only that separator, never body text.
            if old_banner.rstrip("\n") != expected.rstrip("\n"):
                gate_drift.append(name)
        else:
            from .gate import write_into
            write_into(path, scope, lean=bool(contract.get("lean_gate", True)))
    contents = {
        "claude_memory": _memory(snapshot, contract_path, contract),
        "codex_memory": _memory(snapshot, contract_path, contract),
        "claude_banner": _banner(snapshot, contract_path, contract),
        "codex_banner": _codex_banner(snapshot, contract_path, contract),
        "atom_list": _atom_list(snapshot),
        "memory_index": _memory_index(snapshot),
        "reflex_list": _reflex_list(snapshot),
    }
    changed, drift, room_drift = [], [], []
    for name, content in contents.items():
        path = _target(contract, name)
        before = path.read_text(encoding="utf-8") if path.exists() else ""
        proposed = before
        # Reuse the exact safe replacement calculation without writing in --check mode.
        if BEGIN in before and END in before and before.find(BEGIN) <= before.find(END):
            proposed = before[:before.find(BEGIN)] + f"{BEGIN}\n{content.rstrip()}\n{END}" + before[before.find(END) + len(END):]
        elif before:
            proposed = before.rstrip() + "\n\n" + f"{BEGIN}\n{content.rstrip()}\n{END}\n"
        else:
            proposed = f"{BEGIN}\n{content.rstrip()}\n{END}\n"
        if version >= 2 and name in room_targets:
            fresh = _apply_room(proposed, room_region)
            existing = _existing_room(before) if (check and room_tolerant and room_region) else None
            # tolerant verify: judge drift with the slice the file already carries; a target
            # with NO slice still drifts (a missing region is a repair, not churn).
            proposed = _apply_room(proposed, existing) if existing is not None else fresh
            if existing is not None and fresh != proposed:
                room_drift.append(name)
        if proposed != before:
            drift.append(name)
            if not check:
                _write_text(path, proposed)
                changed.append(name)
    return {"scope": scope, "atoms": snapshot["atom_count"], "reflexes": len(snapshot["reflexes"]),
            "changed": changed, "drift": sorted(set(drift + gate_drift)), "check": check,
            "room_drift": sorted(room_drift),
            "room": str(room) if room else None, "room_via": room_via,
            "child": child_state is not None,
            "parent": contract.get("parent") if child_state is not None else None}


def _main(argv=None):
    ap = argparse.ArgumentParser(prog="echelon harness-sync", description="Project the local ECHELON scope into Claude and Codex markdown harness surfaces.")
    ap.add_argument("--contract", required=True, help="JSON projection contract")
    ap.add_argument("--cwd", default=None, help="scope resolution directory when contract scope is auto")
    ap.add_argument("--check", action="store_true", help="report projection drift without writing")
    ap.add_argument("--json", action="store_true", help="emit a machine-readable receipt")
    args = ap.parse_args(argv)
    receipt = project(Path(args.contract), cwd=args.cwd, check=args.check)
    if args.json:
        print(json.dumps(receipt, indent=2))
    else:
        state = "DRIFT" if receipt["drift"] else "CURRENT"
        room = receipt.get("room")
        if room:
            print(f"room: {room} (via {receipt.get('room_via') or 'cwd'})")
        print(f"harness-sync {state}: scope={receipt['scope']} atoms={receipt['atoms']} reflexes={receipt['reflexes']} targets={','.join(receipt['drift']) or 'none'}")
    return 1 if args.check and receipt["drift"] else 0
