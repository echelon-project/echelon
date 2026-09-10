"""propose.py — the PROPOSAL GATE: read-before-code, enforced at the plan.

The check/bind half of PROPOSAL-GATE v3 (`propose_scan.py` is the scanner half
this module CALLS and never re-implements). A builder writes a PROPOSAL JSON
naming every file it will touch and every existing symbol it will use; `check`
scans its edges against the room's index + contracts + change ledger and
returns a feedback REPORT; a green report seals the proposal and opens its
CHG-*; `bind` seals the finished diff back to the proposal and writes the 2c
verification receipt. Stdlib only; imports ONLY echelon_engine.propose_scan /
contracts / workcycle (the scanner rejects upward imports).

REPORT SHAPE (§4): {"v","proposal","proposal_hash","verdict"
(PROCEED|REVISE|REJECT|AMEND — see POST-BUILD below),
"findings":[{rule,severity,entry,evidence,fix,acked?}],"resolved_edges",
"applicable_contracts","deferred_to_bind","budget","base","mode",
"static_rules"} — written to <room>/proposals/<id>.report.json, overwritten
per check; every check journals one propose_check line.

PILOT MODE degradations (§7) — each a VISIBLE report line, never a silent
skip: similarity=name-only (no semantic_tags until 6b; DUP_ERR disabled,
DUP_WARN 0.50), REMOVE-CALLERS-UNKNOWN (used_by not indexed until 6b),
CONTRACTS-NOT-LOADED / CODE-CONTRACT-ABSENT / API-CONTRACT-ABSENT /
CONTRACT-LAW-MANUAL, static_rules "none — laws listed,
verified by witness at bind/gate". The pilot orchestrator is the owner:
unruled unknowns and disputes print FIRST (API-READ-ONLY also joins _FIRST,
2e — a hard freeze violation the orchestrator must see first).

POST-BUILD / AMEND (2e deliverable C, RULINGS A8-A10): when a real build
already happened for this proposal (chg.bound is True, OR a sealed ok:true
propose-bind receipt exists for it in verification/history/ — a bare CHG
opened by check() on PROCEED is NOT evidence of a build), check() re-runs in
POST-BUILD mode: an INTRO-ALREADY-EXISTS collision is suppressed (downgraded
to an advisory) ONLY when the symbol is ATTRIBUTABLE to this proposal (its
name is in the CHG's introduces[], or its index row's active_change is owned
by this proposal id — path alone is not enough). If every remaining error was
suppressed this way, verdict is AMEND (never PROCEED — the build already
happened); a foreign collision or any other error still REJECTs; an unacked
warning still REVISEs. AMEND never opens a second CHG.

EXIT CODES: check 0 PROCEED / 1 REVISE / 1 AMEND / 2 REJECT; bind 0 clean /
1 warnings / 2 errors. Degrades (no room/index/git HEAD, malformed/missing/
v>1 proposal) print ONE stderr line and exit 2 — never a traceback.

KNOWN RESIDUAL (S4, gate round-1, OUT OF 2e SCOPE): propose_scan.rescan
writes index/symbols.jsonl and index/endpoints.jsonl as two SEPARATE
_write_jsonl calls (CT-B3). A crash between the two leaves the index in a
state where one file reflects the new commit and the other does not — bind's
byte-identity guarantee (deliverable B) covers the FAILING-bind case (no
write at all) but not this narrower interrupted-write-between-the-two-files
case. Neither fixed nor tested in 2e; a real fix needs either an atomic
two-file commit (write both to temp paths, rename both) or a single combined
index file.
"""
from __future__ import annotations

import argparse, fnmatch, hashlib, json, re, subprocess, sys, tempfile
from pathlib import Path

from echelon_engine import contracts, propose_scan as ps, workcycle

VERSION = 1

# §3 step 2 — DUP thresholds. 0.50/0.90/0.60 are STARTING POINTS, not
# calibrated values: constants in propose.py, adjusted from the pilot's
# measured false-block rate (§6) — never by picking another number.
DUP_ERR, DUP_WARN = 0.90, 0.60          # semantic mode (index rows carry tags)
DUP_ERR_PILOT, DUP_WARN_PILOT = None, 0.50   # name-only mode (before 6b)

# §3 step 6 — TYPE defaults (START values, documented as such): ratcheted
# from measured use, not tuned per-build.
TYPE_BUDGETS = {
    "engine": {"files": 3, "loc": 400, "deps": 0}, "console-app": {"files": 3, "loc": 300, "deps": 0},
    "prod-service": {"files": 2, "loc": 250, "deps": 0}, "static-site": {"files": 3, "loc": 200, "deps": 0},
    "library": {"files": 2, "loc": 300, "deps": 0}, "experiment": {"files": 6, "loc": 800, "deps": 1},
}
_DEFAULT_TYPE = "engine"
_SIBLING_KINDS = {"function": ["method"], "method": ["function"],
                  "component": ["const", "token"], "const": ["component", "token"],
                  "token": ["component", "const"], "endpoint": ["function"]}
_USAGE_KINDS = {"function", "class", "const", "endpoint", "component", "token", "export", "method", "module"}
_REQUIRED = ("id", "intent", "kind", "budget", "steps", "touches", "introduces", "references", "removes", "replaces")
_KNOWN = _REQUIRED + ("v", "contracts_claimed", "invariants_claimed", "unknowns", "change")
_BRACKET = re.compile(r"(\w+)\[(\d+)\]")
_FIRST = ("DUP-DISPUTED", "UNKNOWN-RULING-NEEDED", "API-READ-ONLY")   # owner rows print first


def _finding(rule, severity, entry, evidence, fix):
    return {"rule": rule, "severity": severity, "entry": entry, "evidence": evidence, "fix": fix}


def _git(repo_root, *args):
    try:
        out = subprocess.run(["git", "-C", str(repo_root), *args], capture_output=True, text=True, encoding="utf-8", errors="replace")
    except (OSError, ValueError):
        return None
    # 2026-08-31 (witnessed on the 12b13b retroactive bind): a `git show
    # <base>:<rel>` leg crashed AttributeError - returncode 0 with stdout
    # None. Guard BOTH legs: no exit-0-with-None ever reaches .strip(), and
    # binary/undecodable blobs ride errors="replace" instead of raising.
    if out.returncode != 0 or out.stdout is None:
        return None
    return out.stdout.strip()


def _repo_root(start):
    cur = Path(start).resolve()
    while True:
        if (cur / ".git").exists():
            return cur
        if cur == cur.parent:
            return None
        cur = cur.parent


def _git_common_dir(repo_root):
    """RULINGS A15: repo IDENTITY, resolved absolute — `git rev-parse
    --git-common-dir` so worktrees of ONE repo share an identity (a plain
    `.git` dir compare would treat every worktree as a different repo)."""
    if repo_root is None:
        return None
    out = _git(repo_root, "rev-parse", "--git-common-dir")
    if out is None:
        return None
    p = Path(out)
    return (repo_root / p).resolve() if not p.is_absolute() else p.resolve()


def _room_repo_mismatch(room, repo_root):
    """M2 heal (gate round-1, RULINGS A15 restored AS RULED): the shared
    identity guard for BOTH the CLI (`main`'s --room) AND the library API
    (`check`/`bind` called directly with a `room` that does not match
    `repo_root` — gate probe8's exact shape: no --room flag at all, just a
    library call with `room` at repo A and `repo_root` pointing at unrelated
    repo B). Compares git-common-dirs so a REAL worktree of one repo (shared
    common-dir despite different .git PATHS) passes; a genuinely unrelated
    repo, or either side lacking git identity while the paths differ, is a
    mismatch. `room` may be a .echelon dir or its parent repo root."""
    room = Path(room).resolve()
    room_repo_root = _repo_root(room if room.name != ".echelon" else room.parent)
    cwd_id, room_id = _git_common_dir(repo_root), _git_common_dir(room_repo_root)
    if cwd_id is not None and room_id is not None:
        return cwd_id != room_id
    if cwd_id is None and room_id is None:
        base_repo = Path(repo_root).resolve() if repo_root else None
        return base_repo is not None and base_repo != (room_repo_root.resolve() if room_repo_root else room)
    return False   # one side has git identity, the other doesn't resolve a repo at all — not enough signal to block


def _loc(diff_text):
    """Added diff lines surviving the scanner's stripper — source-only LOC (§3's own measure; reuses _strip_line)."""
    state = {"block": False, "quote": ""}
    n = 0
    for raw in diff_text.splitlines():
        if not raw.startswith("+") or raw.startswith("+++"):
            continue
        if ps._strip_line(raw[1:], state, hash_comments=True).strip():
            n += 1
    return n


# ── the public API ───────────────────────────────────────────────────────────

def load_proposal(path: Path) -> dict:
    """A PROPOSAL.json as a dict; raises nothing — {} + ONE stderr line on a missing/unreadable/malformed/v>1 file."""
    path = Path(path)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except OSError:
        print(f"propose: {path}: unreadable or missing, skipped", file=sys.stderr)
        return {}
    except json.JSONDecodeError:
        print(f"propose: {path}: bad json, skipped", file=sys.stderr)
        return {}
    if isinstance(data, dict) and isinstance(data.get("v"), int) and data["v"] > VERSION:
        print(f"propose: {path}: v{data['v']} unsupported, skipped", file=sys.stderr)
        return {}
    return data if isinstance(data, dict) else {}


def _node_at(proposal, entry):
    """The proposal node an entry names ("introduces.renderX", "unknowns[0]", "budget", "proposal") — where its ack key lives."""
    if not entry or entry == "proposal" or entry.startswith("proposal."):
        return proposal
    m = _BRACKET.match(entry)
    if m:
        lst = proposal.get(m.group(1))
        try:
            return lst[int(m.group(2))] if isinstance(lst, list) else None
        except (IndexError, ValueError):
            return None
    head, _, tail = entry.partition(".")
    if head == "budget":
        return proposal.get("budget")
    if head in ("introduces", "references") and tail:
        for item in proposal.get(head) or []:
            if isinstance(item, dict) and item.get("name") == tail:
                return item
    # TOUCHES-KEYED ACK (OPEN-0049): a `touches.<path>` warning (only TOUCH-CHG-OVERLAP is a
    # warning; the rest of the touch family are errors, which are never ackable) had NO node to
    # carry its ack, so the gate REVISEd forever with no ack path (the worktree-bind trap). A
    # touches entry is keyed by its `path`; resolve it so ack_<rule> can live on that entry.
    if head == "touches" and tail:
        for item in proposal.get("touches") or []:
            if isinstance(item, dict) and item.get("path") == tail:
                return item
    return None


def _ack(proposal, entry, rule):
    """A warning is ACKED when its entry's node carries ack_<rule-lower>: true (e.g. introduces[].ack_dup_candidate)."""
    node = _node_at(proposal, entry)
    return bool(isinstance(node, dict) and node.get("ack_" + rule.lower().replace("-", "_")))


def verdict(findings: list[dict], *, post_build: bool = False) -> str:
    """REJECT if any error; REVISE if any warning without ack; PROCEED otherwise
    (§3 tail — acks ride as `acked`). RULINGS A10: in POST-BUILD mode (a real
    build already happened — see _post_build_mode), the ladder is IDENTICAL
    except PROCEED becomes AMEND (a post-build re-check should never say
    PROCEED — the build already happened): post_build + zero errors + zero
    unacked warnings -> AMEND; post_build + unacked warnings -> REVISE
    (unchanged); post_build + any surviving error -> REJECT (unchanged,
    REJECT always wins over AMEND)."""
    if any(f.get("severity") == "error" for f in findings):
        return "REJECT"
    if any(f.get("severity") == "warning" and not f.get("acked") for f in findings):
        return "REVISE"
    return "AMEND" if post_build else "PROCEED"


def _semantic(idx):
    """PILOT MODE detection: any index row with non-empty semantic_tags flips similarity to semantic (§7)."""
    rows = ps._read_jsonl(idx / "symbols.jsonl") + ps._read_jsonl(idx / "endpoints.jsonl")
    return any((r.get("semantic_tags") or []) for r in rows)


def _candidates(idx, name, kind, keywords, signature):
    """similar() over [kind] + sibling kinds, merged by row id, max score."""
    merged = {}
    for k in [kind] + _SIBLING_KINDS.get(kind, []):
        for h in ps.similar(idx, name, k, keywords, signature):
            rid = h["row"]["id"]
            if rid not in merged or h["score"] > merged[rid]["score"]:
                merged[rid] = h
    return sorted(merged.values(), key=lambda h: h["score"], reverse=True)


def _cap_gap(why, cand_name):
    """Lexical why_not_reuse check (no model in the gate): cites the candidate by name and carries substance
    BEYOND the name — ≥20 chars after stripping every occurrence of the name and collapsing whitespace,
    and ≥3 distinct words that are not the candidate's stems (name-repetition does not count)."""
    if not why or not cand_name:
        return False
    text = re.sub(r"\s+", " ", why.replace(cand_name, " ")).strip()
    stems = {cand_name.lower(), cand_name.lower().rstrip("es"), cand_name.lower().rstrip("s"),
             cand_name.lower().rstrip("ing"), cand_name.lower().rstrip("ed")}
    words = [w.strip("[]();,.:\"'") for w in text.split(" ")]
    distinct = {w.lower() for w in words if w and w.lower() not in stems}
    return len(text) >= 20 and len(distinct) >= 3


def _own_artifact(path, proposal_id, room):
    """The gate's own files — <room>/proposals/<id>.json and <id>.report.json (repo-relative
    .echelon/proposals/...) — are written before/by the check; exempt from TOUCH-CREATE-EXISTS.
    A same-named basename ANYWHERE else (e.g. lib/PROP-X.json) is a real file, not the gate's."""
    rel = str(path).replace("\\", "/")
    rname = Path(room).resolve().name
    own = {f"{rname}/proposals/{proposal_id}.json", f"{rname}/proposals/{proposal_id}.report.json"}
    return rel in own


def _overlaps(chg_touches, path):
    """fnmatch either way (spec 2d pin): CHG.touches ↔ t.path."""
    return any(fnmatch.fnmatch(t, path) or fnmatch.fnmatch(path, t) for t in chg_touches or [])


def _covered(path, touches):
    """files_changed ⊆ touches: exact, or under a declared directory touch."""
    path = str(path).replace("\\", "/")
    return any(path == t.rstrip("/") or path.startswith(str(t).rstrip("/") + "/") for t in touches)


def _edge_path(edge):
    """"path:line" → path (colons in the path survive)."""
    if not isinstance(edge, str) or edge == "stdlib":
        return edge
    head, _, tail = edge.rpartition(":")
    return head if tail.isdigit() else edge


# ── the §3 check steps (rule-for-rule: same ids, same severities) ────────────

def _step_shape(proposal, findings):
    for field in _REQUIRED:
        if field not in proposal:
            findings.append(_finding("SHAPE-MISSING-FIELD", "error", f"proposal.{field}", f"required field {field!r} missing", "add the field per §2"))
    for key in proposal:
        if key not in _KNOWN and not key.startswith("ack_"):
            findings.append(_finding("SHAPE-UNKNOWN-FIELD", "advisory", f"proposal.{key}", f"unknown top-level key {key!r}", "drop it or it stays visible"))


def _step_refs(proposal, idx, repo_root, findings, resolved_edges):
    for ref in proposal.get("references") or []:
        name, kind, rpath = ref.get("name"), ref.get("kind"), ref.get("path")
        if not isinstance(name, str) or not name:
            continue
        if rpath == "stdlib":   # the 2b proposal's stdlib marker — verified, never trusted
            top = (name or "").split(".", 1)[0]
            if top not in sys.stdlib_module_names:
                findings.append(_finding("REF-UNRESOLVED", "error", f"references.{name}", f"declared stdlib but {top!r} is not a stdlib module", "drop the ref or point at a real stdlib module"))
                continue
            resolved_edges[name] = "stdlib"
            continue
        hits = ps.lookup(idx, name, kind)
        if rpath and (len(hits) != 1 or not hits):
            hits = [h for h in hits if h.get("path") == rpath]      # path-qualified pass
        healed = False
        if not hits and rpath and (repo_root / rpath).exists():
            ps.rescan(idx, [repo_root / rpath], root=repo_root)     # SELF-HEAL (v2)
            hits = ps.lookup(idx, name, kind)
            healed = True
        if not hits:
            findings.append(_finding("REF-UNRESOLVED", "error", f"references.{name}", f"{name!r} not in the index AND not found by re-scanning {rpath or 'its named path'}; it does not exist", "drop the ref or fix the path"))
            continue
        hit = hits[0]
        if healed:
            findings.append(_finding("INDEX-WAS-STALE", "advisory", f"references.{name}", f"index was stale — re-scanned {rpath} (self-heal does NOT exempt the symbol from bind's DRIFT-INTRO)", "keep the index warm — rescan after edits"))
        if rpath and hit.get("path") != rpath:
            findings.append(_finding("REF-PATH-DRIFT", "warning", f"references.{name}", f"index says {hit.get('path')}", "update the path"))
        if hit.get("deprecated") or hit.get("scheduled_for_removal"):
            findings.append(_finding("REF-OBSOLETE", "error", f"references.{name}", f"replaced_by {hit.get('replaced_by') or '?'}", "use the replacement or drop the ref"))
        chg = hit.get("active_change")
        owner = chg.get("owner") if isinstance(chg, dict) else chg
        if owner and owner != proposal.get("id"):
            findings.append(_finding("REF-IN-FLIGHT", "warning", f"references.{name}", f"a CHG owned by {owner} is touching this; coordinate or wait", "coordinate or wait"))
        resolved_edges[name] = f"{hit.get('path')}:{hit.get('line')}"


_PAGE_PRIVATE_V = 1


def _page_private(e):
    """RULINGS A11: ui.json's versioned page_private param block —
    [{glob, page_key: "stem"|"parent-dir"}] — plus an optional exempt_globs
    list (tests/stubs, CT-D7). Fail-CLOSED: absent block, absent ui.json, or
    a future version -> ([], []) with the SAME behaviour as baseline (every
    DUP still errors) — never a silent partial apply (CT-D8/D9)."""
    ui = next((c for cid, c in sorted(contracts.load(e).items()) if c.get("kind") == "ui"), None)
    if not isinstance(ui, dict):
        return [], []
    block = ui.get("page_private")
    if not isinstance(block, dict):
        return [], []
    if block.get("v") is not None and block.get("v") != _PAGE_PRIVATE_V:
        print(f"propose: contracts ui page_private v{block.get('v')} unsupported, ignored", file=sys.stderr)
        return [], []
    tiers = block.get("tiers")
    tiers = tiers if isinstance(tiers, list) else []
    clean = [t for t in tiers if isinstance(t, dict) and isinstance(t.get("glob"), str)
             and t.get("page_key") in ("stem", "parent-dir")]
    exempt = block.get("exempt_globs")
    exempt = [g for g in exempt if isinstance(g, str)] if isinstance(exempt, list) else []
    return clean, exempt


def _page_key(path, glob_key):
    """A path's page identity under a declared page_key rule; None when
    underivable — the caller must then treat it as SAME page (A11 fail-closed:
    an unresolvable page identity must never enable an auto-ack)."""
    path = str(path).replace("\\", "/")
    parts = [p for p in path.split("/") if p]
    if not parts:
        return None
    if glob_key == "stem":
        return parts[-1].rsplit(".", 1)[0] if parts[-1] else None
    if glob_key == "parent-dir":
        return parts[-2] if len(parts) >= 2 else None
    return None


def _page_tier_of(path, tiers):
    """The FIRST matching tier's page_key rule for `path`, or None (not page-private)."""
    path = str(path).replace("\\", "/")
    for t in tiers:
        if fnmatch.fnmatch(path, str(t["glob"]).replace("\\", "/")):
            return t["page_key"]
    return None


def _sibling_page_idiom(e, name, sym_path, cands, dup_warn):
    """RULINGS A11-A13, D: a DUP candidate set (already exempt-filtered by the
    caller — CT-D7) auto-acks as DUP-PAGE-IDIOM ONLY when EVERY candidate
    scoring >= dup_warn is page-private AND a DIFFERENT page (page_key
    mismatch, both sides resolvable) from `sym_path`. One shared-lib or
    same-page match at or above threshold kills the ack and the CALLER must
    fall through to the normal DUP finding naming THAT candidate (A13/CT-D5 —
    never cands[0] alone). Returns (ok, killer_candidate_or_None)."""
    tiers, _exempt = _page_private(e)
    if not tiers:
        return False, None
    sym_key = _page_tier_of(sym_path, tiers)
    if sym_key is None:
        return False, None                     # the introduced symbol isn't page-private itself
    my_page = _page_key(sym_path, sym_key)
    high = [c for c in cands if c["score"] >= dup_warn]
    if not high:
        return False, None
    for c in high:
        cpath = c["row"].get("path") or ""
        ckey = _page_tier_of(cpath, tiers)
        if ckey is None:
            return False, c                     # shared-lib / non-page-private file — kills the ack
        cand_page = _page_key(cpath, ckey)
        if my_page is None or cand_page is None or cand_page == my_page:
            return False, c                     # underivable or SAME page — fail closed, kills the ack
    return True, None


def _step_dups(proposal, idx, findings, dup_err, dup_warn, *, e=None, post_build=False, chg=None):
    touches = {t.get("path") for t in proposal.get("touches") or []}
    proposal_id = proposal.get("id")
    for sym in proposal.get("introduces") or []:
        name, kind = sym.get("name"), sym.get("kind")
        if not isinstance(name, str) or not name:
            continue
        # RULINGS A9/A10 (post-build): a symbol already attributable to THIS
        # proposal is its own shipped artifact — skip DUP scoring entirely
        # (re-scoring introduces[] against its own already-indexed self is a
        # false collision at 1.0 similarity, not a real duplicate) and mark
        # INTRO-ALREADY-EXISTS as a suppressed advisory instead of an error.
        own_hit = (post_build and _own_symbol(e, proposal_id, chg, name, sym.get("path")))
        if not own_hit:
            _tiers, _exempt = _page_private(e)
            cands = _candidates(idx, name, kind, sym.get("keywords") or [], sym.get("signature") or "")
            if _exempt:
                # RULINGS CT-D7: an exempt (test/stub) row is never a DUP
                # candidate at all — not scored, not a finding, not an
                # auto-ack target. Filtered BEFORE top/_sibling_page_idiom so
                # an exempt-only candidate set produces NO finding.
                cands = [c for c in cands if not any(
                    fnmatch.fnmatch(str(c["row"].get("path") or "").replace("\\", "/"), g.replace("\\", "/"))
                    for g in _exempt)]
            top = cands[0] if cands and cands[0]["score"] > 0 else None
            acked_page_idiom, killer = ((False, None) if top is None or top["score"] < dup_warn
                                        else _sibling_page_idiom(e, name, sym.get("path"), cands, dup_warn))
            if acked_page_idiom:
                # RULINGS A12: DUP-PAGE-IDIOM is a DISTINCT rule id, warning
                # severity, acked:true PLUS auto_acked:true + ack_rule — a
                # machine-ack must be distinguishable from a builder-ack in
                # the receipt trail (CT-D10). Set BEFORE the check()-level
                # acked post-pass, which must PRESERVE this (see check()).
                findings.append({**_finding("DUP-PAGE-IDIOM", "warning", f"introduces.{name}",
                                            f"sibling-page idiom: every candidate >= {dup_warn} lives in a different page-private file ({top['row'].get('name')!r} at {top['row'].get('path')})",
                                            "auto-acked — sibling-page idiom copy, no action needed"),
                                 "acked": True, "auto_acked": True, "ack_rule": "DUP-PAGE-IDIOM"})
            elif top is not None and dup_err is not None and top["score"] >= dup_err:
                cand = killer or top
                if sym.get("dispute"):
                    findings.append(_finding("DUP-DISPUTED", "warning", f"introduces.{name}", f"disputed: {sym['dispute']}", "orchestrator ruling before re-check"))
                elif not _cap_gap(sym.get("why_not_reuse"), cand["row"].get("name")):
                    findings.append(_finding("DUP-SEMANTIC", "error", f"introduces.{name}", f"reuse {cand['row'].get('name')} ({cand['row'].get('path')}) score {cand['score']}", "why_not_reuse citing the candidate AND a concrete capability gap, or reuse it"))
            elif top is not None and top["score"] >= dup_warn:
                cand = killer or top
                why = sym.get("why_not_reuse")
                findings.append(_finding("DUP-CANDIDATE", "error" if not why else "warning", f"introduces.{name}", f"{cand['row'].get('name')} ({cand['row'].get('path')}) at {cand['score']}; why_not_reuse required", "add why_not_reuse naming the candidate, or reuse it"))
        if sym.get("path") not in touches:
            findings.append(_finding("INTRO-OUTSIDE-TOUCHES", "error", f"introduces.{name}", f"introduces {name} at {sym.get('path')} — not in touches", "add the path to touches or drop the symbol"))
        hits = ps.lookup(idx, name, kind)
        if hits and hits[0].get("path") == sym.get("path"):
            if own_hit:
                findings.append(_finding("INTRO-ALREADY-EXISTS", "advisory", f"introduces.{name}", f"{name} at {sym.get('path')} is this proposal's OWN shipped artifact (post-build) — suppressed, not an error", "none — this is the AMEND state"))
            else:
                findings.append(_finding("INTRO-ALREADY-EXISTS", "error", f"introduces.{name}", f"{name} already exists at {sym.get('path')} — it's an edit", "declare it in references + replaces"))


def _step_touch(proposal, e, repo_root, findings):
    code = next((c for cid, c in sorted(contracts.load(e).items()) if c.get("kind") == "code"), None) or {}
    orch, frozen = code.get("orchestrator_only") or [], code.get("frozen") or []
    if not code:
        findings.append(_finding("CODE-CONTRACT-ABSENT", "advisory", "proposal", "contracts/code.json missing — orchestrator_only/frozen unknown", "add contracts/code.json when the drawer ships (2e)"))
    own = proposal.get("id")
    chgs = []
    active = e / "changes" / "active"
    if active.is_dir():
        chgs = [d for p in sorted(active.glob("CHG-*.json")) if isinstance(d := workcycle._read_json(p, {}), dict) and d.get("proposal") != own]
    for t in proposal.get("touches") or []:
        path, mode = t.get("path"), t.get("mode")
        if any(fnmatch.fnmatch(path, g) for g in orch):
            findings.append(_finding("TOUCH-SHARED-FILE", "error", f"touches.{path}", "orchestrator-only at merge", "put the need in WIRING_NOTE"))
        if any(fnmatch.fnmatch(path, g) for g in frozen):
            findings.append(_finding("TOUCH-FROZEN", "error", f"touches.{path}", "frozen per contracts/code.json", "contract the change with the owner"))
        exists = (repo_root / path).exists()
        if mode == "create" and exists and not _own_artifact(path, own, e):
            findings.append(_finding("TOUCH-CREATE-EXISTS", "error", f"touches.{path}", "declared create but the file already exists", "it's an edit — declare mode edit"))
        if mode == "edit" and not exists:
            findings.append(_finding("TOUCH-EDIT-MISSING", "error", f"touches.{path}", "declared edit but the file does not exist", "fix the path or declare create"))
        for chg in chgs:
            if _overlaps(chg.get("touches"), path):
                findings.append(_finding("TOUCH-CHG-OVERLAP", "warning", f"touches.{path}", f"CHG-{chg.get('id')} is touching this", "coordinate or wait"))


def _api_contracts(e, findings):
    """kind=="api" contracts, fail-closed per RULINGS A1/A3/A4/A5. Shape (A1
    dual-keyed drawer file): the DRAWER ENVELOPE (v/id/kind/scope_globs/laws)
    is authoritative and load()-visible; the registry's API-shape fields
    (version/exact/read_only/base_path/...) ride NESTED under c["api"].
    Degradations: absence of any loaded kind:api contract is advisory
    (API-CONTRACT-ABSENT); a file present on disk but dropped by
    contracts.load() (bad v/id) is a distinct error (API-CONTRACT-UNREADABLE);
    a loaded api contract with a non-bool read_only, or an empty/missing/
    malformed drawer-level scope_globs, or a missing nested api block, is
    API-CONTRACT-FIELD-INVALID (error) — never inferred, never routed through
    applicable()'s empty-globs-means-everything semantics."""
    # M1 heal (gate probe5): "looks like an api contract" is filename OR
    # field-shape (contracts.looks_like_api) — NEVER raw.get("kind")=="api"
    # alone, since the registry's OWN schema example has no "kind" key.
    loaded = [c for c in contracts.load(e).values() if contracts.looks_like_api(None, c)]
    cdir = Path(e) / "contracts"
    on_disk = sorted(cdir.glob("*.json")) if cdir.is_dir() else []
    loaded_ids = {c.get("id") for c in loaded}
    for p in on_disk:
        raw = contracts._read_json(p)
        if contracts.looks_like_api(p, raw) and (not isinstance(raw, dict) or raw.get("id") not in loaded_ids):
            findings.append(_finding("API-CONTRACT-UNREADABLE", "error", "proposal", f"{p} looks like an api contract but was dropped by contracts.load() (missing/invalid v or id)", "fix v:1 and a non-empty id per the drawer envelope shape"))
    if not loaded:
        findings.append(_finding("API-CONTRACT-ABSENT", "advisory", "proposal", "no kind:api contract loaded — API-READ-ONLY fence inactive", "seed contracts/api.json when the surface is frozen"))
    ok = []
    for c in loaded:
        api = c.get("api")
        globs = c.get("scope_globs")
        bad = []
        if not isinstance(api, dict):
            bad.append("api (nested block)")
        elif not isinstance(api.get("read_only"), bool):
            bad.append("api.read_only")
        if not isinstance(globs, list) or not globs or not all(isinstance(g, str) for g in globs):
            bad.append("scope_globs")
        if bad:
            findings.append(_finding("API-CONTRACT-FIELD-INVALID", "error", f"contracts.{c.get('id')}", f"invalid/missing field(s) {bad} on kind:api contract {c.get('id')!r}", "fix the field(s) per the registry schema (01-contract-schemas.md)"))
            continue
        ok.append(c)
    return ok


def _step_api_readonly(proposal, e, findings):
    """API-READ-ONLY (2e deliverable A, RULINGS A1-A5): any well-formed kind:api
    contract with read_only IS True (identity, never truthiness) fences every
    touches[].path (regardless of mode — a read_only surface forbids ANY
    proposed mutation) matching scope_globs, POSIX-normalized, fnmatch (never
    applicable()'s empty-globs-means-everything — an api contract with a bad
    scope_globs is already flagged API-CONTRACT-FIELD-INVALID and excluded
    here). Fail-closed BY KIND (never id — the 2026-08-27 gate-caught heal
    ac057ab: a wrong-key lookup fails open to every path)."""
    for c in _api_contracts(e, findings):
        if c["api"].get("read_only") is not True:
            continue
        for t in proposal.get("touches") or []:
            path = str(t.get("path") or "").replace("\\", "/")
            path = path[2:] if path.startswith("./") else path       # S2: leading ./ stripped before fnmatch
            if any(fnmatch.fnmatch(path, g) for g in c["scope_globs"]):
                findings.append(_finding("API-READ-ONLY", "error", f"touches.{t.get('path')}", f"read_only per contracts/{c.get('id')} scope_globs — no proposed mutation allowed", "the API surface is frozen; contract the change with the owner"))


def _step_api_readonly_bind(files_changed, e):
    """MIRROR of _step_api_readonly for bind (RULINGS A2): a proposal can
    PROCEED with clean touches and then drift a real edit into the frozen
    surface — DRIFT-FILE only catches paths outside touches, never frozen
    paths that were declared inside them."""
    findings = []
    for c in _api_contracts(e, findings):
        if c["api"].get("read_only") is not True:
            continue
        for f in files_changed or []:
            path = str(f or "").replace("\\", "/")
            path = path[2:] if path.startswith("./") else path       # S2: leading ./ stripped before fnmatch
            if any(fnmatch.fnmatch(path, g) for g in c["scope_globs"]):
                findings.append(_finding("API-READ-ONLY", "error", f"files_changed.{f}", f"read_only per contracts/{c.get('id')} scope_globs — no mutation allowed", "the API surface is frozen; contract the change with the owner"))
    return [f for f in findings if f["rule"] == "API-READ-ONLY"]


def _replacement_for(proposal, r):
    """A replacement TIED to the removed symbol: an introduces[] entry whose
    replaces/replaces_for names it, or the symbol itself declared in replaces[]."""
    for i in proposal.get("introduces") or []:
        if isinstance(i, dict) and (i.get("replaces") == r or i.get("replaces_for") == r):
            return True
    repl = [x if isinstance(x, str) else (x or {}).get("name") for x in proposal.get("replaces") or []]
    return r in repl


def _step_removes(proposal, idx, findings):
    touches = {t.get("path") for t in proposal.get("touches") or []}
    removes = [r if isinstance(r, str) else r.get("name") for r in proposal.get("removes") or []]
    names = removes + [r if isinstance(r, str) else r.get("name") for r in proposal.get("replaces") or []]
    for r in [n for n in names if isinstance(n, str)]:
        hits = ps.lookup(idx, r)
        if not hits:
            continue
        hit = hits[0]
        users = hit.get("used_by")
        if not users:
            findings.append(_finding("REMOVE-CALLERS-UNKNOWN", "advisory", f"removes.{r}", "used_by not indexed until 6b; the Opus gate re-derives callers", "re-derive callers at the Opus gate"))
        else:
            outside = sorted(set(users) - touches)
            if outside:
                findings.append(_finding("REMOVE-HAS-CALLERS", "error", f"removes.{r}", f"{len(outside)} callers outside touches: {outside}", "declare the callers' files in touches or keep the symbol"))
        if hit.get("canonical") and r in removes and not _replacement_for(proposal, r):
            findings.append(_finding("REMOVE-CANONICAL-NO-REPLACEMENT", "error", f"removes.{r}", "removing a canonical symbol with no replacement introduced", "declare the replacement in introduces"))


def _step_contracts(proposal, e, findings, applicable_ids, deferred):
    loaded = contracts.load(e)
    if not loaded:
        findings.append(_finding("CONTRACTS-NOT-LOADED", "warning", "proposal", "contract drawer empty — projection skipped (visible, not silent)", "seed contracts/ or ack"))
    claimed = [c for c in proposal.get("contracts_claimed") or [] if isinstance(c, str)]
    law_owner = {lid: cid for cid, c in loaded.items() for lid in (c.get("laws") or {})}
    claimed_owners = set()
    for cid in claimed:                     # a claim names a contract id OR a law id inside one (§2's UI-TOKENS example)
        if cid in loaded:
            claimed_owners.add(cid)
        elif cid in law_owner:
            claimed_owners.add(law_owner[cid])
        else:
            findings.append(_finding("CONTRACT-CLAIMED-NOT-FOUND", "warning", f"contracts.{cid}", f"claimed contract or law {cid!r} does not exist in contracts/", "drop the claim or add the contract"))
    touches = [t.get("path") for t in proposal.get("touches") or []]
    applicable_ids.extend(sorted(set(contracts.applicable(e, proposal.get("kind"), touches)) | claimed_owners))
    for cid in applicable_ids:
        for lid, law in (contracts.laws(e, cid) or {}).items():
            if law.get("witness") == "manual":
                findings.append(_finding("CONTRACT-LAW-MANUAL", "advisory", f"contracts.{cid}.{lid}", f"law {lid} witness=manual — listed, not verified", "verify by witness at bind/gate"))
            else:
                deferred.append(f"{cid}/{lid}")


def _step_budget(proposal, e, findings):
    budget = proposal.get("budget")
    room = workcycle._read_json(e / "room.json", {}) or {}
    tdef = TYPE_BUDGETS.get(room.get("type") or _DEFAULT_TYPE, TYPE_BUDGETS[_DEFAULT_TYPE])
    if not isinstance(budget, dict):
        findings.append(_finding("BUDGET-DEFAULTED", "warning", "budget", f"budget missing — type default {tdef}", "declare a budget per §2"))
        budget = {}
    bfiles, bloc, bdeps = (budget.get("files") or 0), (budget.get("loc") or 0), (budget.get("deps") or 0)
    if (bfiles > tdef["files"] or bloc > tdef["loc"] or bdeps > tdef["deps"]) and not budget.get("override_reason"):
        findings.append(_finding("BUDGET-EXCEEDS-TYPE", "warning", "budget", f"declared exceeds the {room.get('type')} type default {tdef}", "declare override_reason"))
    if bdeps > 0:
        findings.append(_finding("BUDGET-NEW-DEP", "error" if bdeps > tdef["deps"] else "warning", "budget", f"declares {bdeps} dep(s) vs type default {tdef['deps']}", "drop the dep or ack"))
    if len(proposal.get("touches") or []) > bfiles:
        findings.append(_finding("BUDGET-FILES", "error", "budget", f"{len(proposal.get('touches') or [])} touches > budget.files {bfiles}", "trim touches or raise the budget with override_reason"))
    return {"declared": proposal.get("budget"), "type_default": tdef}


def _step_unknowns(proposal, findings):
    for i, u in enumerate(proposal.get("unknowns") or []):
        if not isinstance(u, dict) or u.get("ruling") is None:
            findings.append(_finding("UNKNOWN-RULING-NEEDED", "warning", f"unknowns[{i}]", (u or {}).get("q") or "unruled unknown", "the orchestrator rules it before dispatch"))


# ── check ────────────────────────────────────────────────────────────────────

def _reject_report(proposal, why):
    return {"v": VERSION, "proposal": (proposal or {}).get("id", ""), "proposal_hash": "", "verdict": "REJECT", "findings": [_finding(why, "error", "proposal", why, "fix the precondition")], "resolved_edges": {}, "applicable_contracts": [], "deferred_to_bind": [], "budget": {}, "base": None, "mode": {"similarity": "name-only"}, "static_rules": "none — laws listed, verified by witness at bind/gate"}


def _find_chg(e, proposal_id):
    """(path, data) of the proposal's CHG across active+completed; (None, None) when none exists."""
    for d in (e / "changes" / "active", e / "changes" / "completed"):
        for p in sorted(d.glob("CHG-*.json")):
            data = workcycle._read_json(p, {})
            if isinstance(data, dict) and data.get("proposal") == proposal_id:
                return p, data
    return None, None


def _base_symbols(repo_root, base, rel):
    """R3 (INC-0004): index rows of the BASE BLOB for a touched file with
    ZERO index rows — `git show <base>:<rel>` into a temp tree at the SAME
    rel path, scanned with root=tmp so ids match an indexed scan. [] = absent
    at base (a create); None = git/base unavailable (caller's advisory)."""
    if repo_root is None or base is None:
        return None
    blob = _git(repo_root, "show", f"{base}:{rel}")
    if blob is None:
        ok = _git(repo_root, "rev-parse", "--verify", "-q", f"{base}^{{commit}}")
        return [] if ok is not None else None
    with tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp) / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(blob, encoding="utf-8")
        return ps.scan_file(p, root=Path(tmp))


def _close_chg(e, chg_path, chg, hist):
    """R1 (INC-0001): an ok:true bind is the CHG CLOSER — active → completed/
    (completed = now, completed_by = 'propose bind ok: <receipt>'); a CHG
    already in completed/ re-seals in place (completed untouched). Returns
    the path the CHG now lives at."""
    dst = chg_path
    if Path(chg_path).parent != (e / "changes" / "completed"):
        completed = e / "changes" / "completed"
        completed.mkdir(parents=True, exist_ok=True)
        chg["completed"] = contracts._now()
        dst = completed / Path(chg_path).name
        Path(chg_path).unlink()
    chg["completed_by"] = f"propose bind ok: {hist}"
    workcycle._write_json(dst, chg)
    return dst


def _post_build_mode(e, proposal_id):
    """RULINGS A8 (spec error conceded): a bare CHG opened by check() on
    PROCEED (bound:false) is NOT evidence of a build — that is the trapdoor
    (check-twice-build-nothing suppresses its own collisions). POST-BUILD mode
    requires chg.get("bound") is True OR a sealed ok:true propose-bind receipt
    for this subject in verification/history/ (never latest.json alone — that
    holds only the last gate of ANY subject)."""
    _, chg = _find_chg(e, proposal_id)
    if isinstance(chg, dict) and chg.get("bound") is True:
        return True
    hist = e / "verification" / "history"
    if not hist.is_dir():
        return False
    for p in hist.glob(f"*-propose-bind-{proposal_id}.json"):
        doc = workcycle._read_json(p, {})
        if isinstance(doc, dict) and doc.get("gate") == "propose-bind" and doc.get("subject") == proposal_id and doc.get("ok") is True:
            return True
    return False


def _own_symbol(e, proposal_id, chg, name, path):
    """RULINGS A9: own-artifact requires the symbol name to be ATTRIBUTABLE to
    THIS proposal, not merely co-located in a touched file — defining file in
    touches[] (already true by construction: INTRO-OUTSIDE-TOUCHES forces it)
    AND the symbol name is in the CHG's introduces[] OR the index row's
    active_change is owned by this proposal id. Path alone is tautological
    (every introduce that survives INTRO-OUTSIDE-TOUCHES lives in a touched
    file) and would suppress collisions with FOREIGN symbols too.

    The index row's recorded owner is AUTHORITATIVE when present: CHG.
    introduces[] is only the proposal's own INTENT (every name it planned to
    introduce, recorded by _open_chg BEFORE any build — CT-C2's own_hit trap:
    a foreign symbol that merely shares a NAME with something this proposal
    once intended can still sit in that list). A row whose active_change
    names a DIFFERENT owner is never own-artifact regardless of CHG intent."""
    idx = e / "index"
    matched = [row for row in ps.lookup(idx, name) if row.get("path") == path]
    for row in matched:
        ac = row.get("active_change")
        owner = ac.get("owner") if isinstance(ac, dict) else ac
        if owner and owner != proposal_id:
            return False                       # a recorded foreign owner always wins
        if owner == proposal_id:
            return True
    if isinstance(chg, dict):
        # ATTRIBUTION GAP (OPEN-0048): the legacy `introduces` snapshot is NAMES ONLY, so a
        # foreign symbol at a DIFFERENT path that merely shares a name was falsely attributed as
        # own whenever no index row overrode it (CT-C2's own_hit trap). `introduces_at` is the
        # path-carrying snapshot {name: path}; when present, attribution requires the PATH to
        # match too. Falls back to name-only for a legacy CHG that predates the snapshot.
        intro_at = chg.get("introduces_at")
        if isinstance(intro_at, dict):
            return intro_at.get(name) == path
        if name in (chg.get("introduces") or []):
            return True
    return False


def _open_chg(e, proposal):
    """PROCEED side-effect: open (or reuse) changes/active/CHG-<NNN>.json; NNN = 1 + max across active+completed, zero-padded 3. R2 (INC-0002): on REUSE the found CHG is REFRESHED — introduces/touches/must_remove = sorted union(existing, current proposal), intent = current, amended = now — written back wherever _find_chg found it (a completed CHG refreshes in place). UNION, never replace: a name once planned stays attributable (_own_symbol)."""
    p, data = _find_chg(e, proposal.get("id"))
    if data is not None:
        names = [r if isinstance(r, str) else r.get("name") for r in (proposal.get("removes") or []) + (proposal.get("replaces") or [])]
        data["introduces"] = sorted(set(data.get("introduces") or []) | {i.get("name") for i in proposal.get("introduces") or []})
        # REFRESH the path-carrying snapshot too (OPEN-0049): union of the existing {name:path}
        # map with the current proposal's, current proposal wins on a name re-introduced at a new
        # path (the live intent). A name once planned stays attributable (union, never replace).
        intro_at = dict(data.get("introduces_at") or {})
        intro_at.update({i.get("name"): i.get("path")
                         for i in proposal.get("introduces") or [] if i.get("name")})
        data["introduces_at"] = intro_at
        data["touches"] = sorted(set(data.get("touches") or []) | {t.get("path") for t in proposal.get("touches") or []})
        data["must_remove"] = sorted(set(data.get("must_remove") or []) | {n for n in names if isinstance(n, str)})
        data["intent"] = proposal.get("intent")
        data["amended"] = contracts._now()
        workcycle._write_json(p, data)
        return data
    hi = 0
    for d in (e / "changes" / "active", e / "changes" / "completed"):
        for f in d.glob("CHG-*.json"):
            m = re.fullmatch(r"CHG-(\d+)", f.stem)
            if m:
                hi = max(hi, int(m.group(1)))
    names = [r if isinstance(r, str) else r.get("name") for r in (proposal.get("removes") or []) + (proposal.get("replaces") or [])]
    data = {"v": VERSION, "id": f"CHG-{hi + 1:03d}", "proposal": proposal.get("id"), "intent": proposal.get("intent"), "touches": [t.get("path") for t in proposal.get("touches") or []], "introduces": [i.get("name") for i in proposal.get("introduces") or []], "introduces_at": {i.get("name"): i.get("path") for i in proposal.get("introduces") or [] if i.get("name")}, "must_remove": names, "budget": proposal.get("budget") if isinstance(proposal.get("budget"), dict) else None, "opened": contracts._now(), "bound": False}
    workcycle._write_json(e / "changes" / "active" / f"{data['id']}.json", data)
    return data


def check(room: Path, proposal: dict, *, repo_root: Path) -> dict:
    """§3 → §4: the gate, rule-for-rule. NEVER raises — missing room/index/git HEAD print ONE stderr line and return a REJECT skeleton (CLI exit 2). Writes the report + journals; PROCEED opens the proposal's CHG-* (re-checks reuse it)."""
    e = Path(room).resolve()
    if not (e / "room.json").exists():
        print(f"propose: no room at {room} (run `workcycle init`)", file=sys.stderr)
        return _reject_report(proposal, "ROOM-UNAVAILABLE")
    if repo_root is not None and _room_repo_mismatch(e, repo_root):
        print(f"propose: ROOM-REPO-MISMATCH — room {e} is a different repo than repo_root {repo_root}", file=sys.stderr)
        return _reject_report(proposal, "ROOM-REPO-MISMATCH")
    idx = e / "index"
    if not idx.is_dir():
        print(f"propose: index missing at {idx} — run `index scan`", file=sys.stderr)
        return _reject_report(proposal, "INDEX-UNAVAILABLE")
    if repo_root is None:
        print("propose: no git repo root found", file=sys.stderr)
        return _reject_report(proposal, "GIT-UNAVAILABLE")
    base = _git(repo_root, "rev-parse", "HEAD")
    if base is None:
        print(f"propose: no git HEAD at {repo_root} — not a git repo or no commits", file=sys.stderr)
        return _reject_report(proposal, "GIT-UNAVAILABLE")
    findings, resolved_edges, applicable_ids, deferred = [], {}, [], []
    _step_shape(proposal, findings)
    semantic = _semantic(idx)
    dup_err, dup_warn = (DUP_ERR if semantic else DUP_ERR_PILOT), (DUP_WARN if semantic else DUP_WARN_PILOT)
    _step_refs(proposal, idx, repo_root, findings, resolved_edges)
    post_build = _post_build_mode(e, proposal.get("id"))       # RULINGS A8/A9/A10 (spec deliverable C)
    _, chg_for_own = _find_chg(e, proposal.get("id"))
    _step_dups(proposal, idx, findings, dup_err, dup_warn, e=e, post_build=post_build, chg=chg_for_own)
    _step_touch(proposal, e, repo_root, findings)
    _step_api_readonly(proposal, e, findings)
    _step_removes(proposal, idx, findings)
    _step_contracts(proposal, e, findings, applicable_ids, deferred)
    deferred.extend(p for p in proposal.get("invariants_claimed") or [] if isinstance(p, str))
    budget = _step_budget(proposal, e, findings)
    _step_unknowns(proposal, findings)
    for f in findings:
        # RULINGS A12's trap: this post-pass must PRESERVE an already-true
        # acked (an auto_acked:true finding from _step_dups' DUP-PAGE-IDIOM)
        # — overwriting it with _ack()'s builder-ack lookup would silently
        # revert every auto-ack to REVISE.
        if f["severity"] == "warning" and not f.get("auto_acked"):
            f["acked"] = _ack(proposal, f["entry"], f["rule"])
    findings.sort(key=lambda f: 0 if f["rule"] in _FIRST else 1)    # owner rows first
    v = verdict(findings, post_build=post_build)
    rep = {"v": VERSION, "proposal": proposal.get("id", ""), "proposal_hash": "sha256:" + hashlib.sha256(json.dumps(proposal, sort_keys=True).encode("utf-8")).hexdigest(), "verdict": v, "findings": findings, "resolved_edges": resolved_edges, "applicable_contracts": applicable_ids, "deferred_to_bind": deferred, "budget": budget, "base": base, "mode": {"similarity": "semantic" if semantic else "name-only"}, "static_rules": "none — laws listed, verified by witness at bind/gate"}
    if rep["proposal"]:
        workcycle._write_json(e / "proposals" / f"{rep['proposal']}.report.json", rep)
        workcycle.journal(e, "propose_check", {"proposal": rep["proposal"], "verdict": v, "findings": len(findings), "mode": rep["mode"]})
        if v == "PROCEED":                # RULINGS CT-C7: AMEND never opens a CHG (one already exists)
            _open_chg(e, proposal)
        elif v == "AMEND" and _find_chg(e, proposal.get("id"))[0] is not None:
            _open_chg(e, proposal)        # R2 (INC-0002): AMEND refreshes an EXISTING CHG only
    return rep


# ── bind ─────────────────────────────────────────────────────────────────────

def _preview_merge(idx, files_changed, repo_root):
    """RULINGS A7: an IN-MEMORY preview of what ps.rescan(idx, files_changed)
    WOULD write, without writing — mirrors ps._upsert's merge/drop logic
    exactly (same-id replace, curated-field carry-over, vanished-scan-row
    drop) so DRIFT-EDGE can read the post-build view before the real commit
    is gated on `ok`. Returns {"symbols.jsonl": [...], "endpoints.jsonl": [...]}.

    MAINTENANCE DEBT (S3, gate round-1): this duplicates ps._upsert's merge
    algorithm by hand rather than sharing it — a future change to _upsert's
    curated-field/drop semantics must be mirrored here manually or DRIFT-EDGE
    silently diverges from what bind actually commits. Left as-is for 2e
    (do NOT refactor propose_scan now); a real fix would give propose_scan a
    dry_run=True parameter on _upsert/rescan so both paths share one
    implementation."""
    symbols, endpoints, scanned = [], [], set()
    for f in files_changed:
        p = repo_root / f
        scanned.add(f.replace("\\", "/"))
        if p.exists():
            for row in ps.scan_file(p, root=repo_root):
                (endpoints if row["kind"] == "endpoint" else symbols).append(row)
    out = {}
    for fname, new_rows in (("symbols.jsonl", symbols), ("endpoints.jsonl", endpoints)):
        existing = ps._read_jsonl(idx / fname)
        new_ids = {r["id"] for r in new_rows}
        keep = [old for old in existing
                if old.get("id") not in new_ids
                and not (old.get("source", "scan") == "scan" and old.get("path") in scanned)]
        merged = []
        for row in new_rows:
            old = next((o for o in existing if o.get("id") == row["id"]), None)
            if old is None:
                merged.append(row)
                continue
            fresh = dict(row)
            for key in ps._CURATED:
                if old.get(key) not in (None, [], False, ""):
                    fresh[key] = old[key]
            merged.append(fresh)
        out[fname] = keep + merged
    return out


def _bind_degraded(proposal_id, why):
    return {"v": VERSION, "gate": "propose-bind", "subject": proposal_id, "ok": False, "findings": [_finding(why, "error", "proposal", why, "fix the precondition and re-run")], "checks": {}, "loc": 0, "files_changed": [], "new_symbols": [], "removed_symbols": [], "receipt": None}


def bind(room: Path, proposal_id: str, *, repo_root: Path, base: str | None = None, diff_text: str | None = None, files_changed: list[str] | None = None) -> dict:
    """§5: seal the diff to the proposal. diff_text/files_changed injectable; None → `git -C <root> diff -U0 <base>` / `--name-only`, base = base= kwarg else report["base"] else HEAD. NEVER raises — degrades to ok=False + one stderr line (CLI exit 2). OPEN-0001 law: on a git-backed bind (a base is known), symbol novelty and removal are read from the git BASE, never the index (which stays authoritative only for curated fields and imported rows); the injected path — caller supplies BOTH diff_text and files_changed with no base= — keeps the index authoritative."""
    e = Path(room).resolve()
    if not (e / "room.json").exists():
        print(f"propose: no room at {room} (run `workcycle init`)", file=sys.stderr)
        return _bind_degraded(proposal_id, "ROOM-UNAVAILABLE")
    if repo_root is not None and _room_repo_mismatch(e, repo_root):
        print(f"propose: ROOM-REPO-MISMATCH — room {e} is a different repo than repo_root {repo_root}", file=sys.stderr)
        return _bind_degraded(proposal_id, "ROOM-REPO-MISMATCH")
    prop = load_proposal(e / "proposals" / f"{proposal_id}.json")
    if not prop:
        return _bind_degraded(proposal_id, "PROPOSAL-UNAVAILABLE")
    report = workcycle._read_json(e / "proposals" / f"{proposal_id}.report.json", {}) or {}
    base_arg = base
    base = base_arg or report.get("base") or (None if repo_root is None else _git(repo_root, "rev-parse", "HEAD"))
    # OPEN-0001 (gate M-1 heal): git-backed = a base is KNOWN — the CLI now
    # passes --base through as base=, so `propose bind --base <sha>` runs the
    # law (diff and novelty rows both come from git). The injected path —
    # caller supplies BOTH diff_text and files_changed with no base= — keeps
    # the index authoritative; S-4: mixed injection (only one supplied) is
    # git-backed only when a base is known, never half-git.
    injected = diff_text is not None and files_changed is not None and base_arg is None
    git_backed = not injected
    if diff_text is None or files_changed is None:
        if repo_root is None:
            print("propose: no git repo root found", file=sys.stderr)
            return _bind_degraded(proposal_id, "GIT-UNAVAILABLE")
        if base is None:
            print(f"propose: no git HEAD at {repo_root} — not a git repo or no commits", file=sys.stderr)
            return _bind_degraded(proposal_id, "GIT-UNAVAILABLE")
        if diff_text is None:
            diff_text = _git(repo_root, "diff", "-U0", str(base))
            if diff_text is None:
                print(f"propose: git diff -U0 {base} failed", file=sys.stderr)
                return _bind_degraded(proposal_id, "GIT-UNAVAILABLE")
        if files_changed is None:
            names = _git(repo_root, "diff", "--name-only", str(base))
            if names is None:
                print(f"propose: git diff --name-only {base} failed", file=sys.stderr)
                return _bind_degraded(proposal_id, "GIT-UNAVAILABLE")
            files_changed = [ln for ln in names.splitlines() if ln.strip()]
    idx = e / "index"
    if not idx.is_dir():
        print(f"propose: index missing at {idx} — run `index scan`", file=sys.stderr)
        return _bind_degraded(proposal_id, "INDEX-UNAVAILABLE")
    touches = [t.get("path") for t in prop.get("touches") or []]
    findings = []
    drift = [f for f in files_changed if not _covered(f, touches)]
    if drift:
        findings.append(_finding("DRIFT-FILE", "error", "proposal", f"files changed outside touches: {drift}", "declare them in touches or revert"))
    findings.extend(_step_api_readonly_bind(files_changed, e))
    cur = ps._read_jsonl(idx / "symbols.jsonl") + ps._read_jsonl(idx / "endpoints.jsonl")
    by_path = {f: [r for r in cur if r.get("path") == f] for f in files_changed}
    new_syms, removed_syms = [], []
    for f in files_changed:
        fresh = ps.scan_file(repo_root / f, root=repo_root) if (repo_root / f).exists() else []
        idx_rows = by_path.get(f, [])
        if git_backed:                     # OPEN-0001: novelty reads the git BASE, not the index
            rows = _base_symbols(repo_root, base, f)
            if rows is None:               # git/base unavailable: index rows + advisory (unchanged fallback)
                findings.append(_finding("BASE-UNAVAILABLE", "advisory", f"files_changed.{f}",
                                         "no git base blob — every symbol treated as new", "bind against a git base or ack"))
                rows = idx_rows
        else:
            rows = idx_rows                # injected diff: the index stays authoritative
            if not rows:                   # R3 (INC-0004): seed old_ids from the BASE BLOB
                base_rows = _base_symbols(repo_root, base, f)
                if base_rows is None:
                    findings.append(_finding("BASE-UNAVAILABLE", "advisory", f"files_changed.{f}",
                                             "no git base blob — every symbol treated as new", "bind against a git base or ack"))
                    base_rows = []
                rows = base_rows
        fresh_ids, old_ids = {r["id"] for r in fresh}, {r["id"] for r in rows}
        new_syms += [r["name"] for r in fresh if r["id"] not in old_ids]
        # OPEN-0001: removals read the BASE rows too; imported index rows
        # (source != "scan") were never in the file and never count as removed.
        removed_syms += [r["name"] for r in rows if r["id"] not in fresh_ids and r.get("source") == "scan"]
    intro_names = {i.get("name") for i in prop.get("introduces") or []}
    rm_names = {r if isinstance(r, str) else r.get("name") for r in (prop.get("removes") or []) + (prop.get("replaces") or [])}
    undeclared = sorted(set(new_syms) - intro_names)
    if undeclared:
        findings.append(_finding("DRIFT-INTRO", "error", "proposal", f"new symbols not declared in introduces: {undeclared}", "declare them or revert (the gate exists for this)"))
    ghost = sorted(set(removed_syms) - rm_names)
    if ghost:
        findings.append(_finding("DRIFT-REMOVE", "error", "proposal", f"removed symbols not declared in removes/replaces: {ghost}", "declare them or revert"))
    missing = [i["name"] for i in prop.get("introduces") or [] if i.get("name") not in new_syms]
    if missing:
        findings.append(_finding("INTRO-MISSING", "warning", "proposal", f"introduces never appeared in the diff: {missing}", "plan shrank — fine, but say so"))
    used = ps.usages(diff_text)
    unused = [ref.get("name") for ref in prop.get("references") or [] if ref.get("kind") in _USAGE_KINDS and ref.get("path") != "stdlib" and ref.get("name") not in used]
    if unused:
        findings.append(_finding("REF-DECLARED-NOT-USED", "warning", "proposal", f"referenced but never used in the diff: {unused}", "drop the ref or actually use the symbol"))
    loc = _loc(diff_text)
    bloc = (prop.get("budget") or {}).get("loc") or 0
    if bloc and loc > bloc:
        findings.append(_finding("BUDGET-LOC", "error", "budget", f"{loc} source lines > budget.loc {bloc}", "trim or amend the budget with override_reason"))
    # RULINGS A7 / spec deliverable B: DRIFT-EDGE reads a TRANSIENT in-memory
    # preview of what rescan WOULD write — never the real index — so a
    # failing/degraded bind commits NOTHING and the receipt/CHG seal only
    # happen after `ok` is decided. No write-then-rollback: a crash between
    # write and rollback would leave the index poisoned, the exact failure
    # deliverable B removes.
    preview = _preview_merge(idx, files_changed, repo_root)
    for ref in prop.get("references") or []:                    # DRIFT-EDGE (v3, TOCTOU guard)
        if ref.get("path") == "stdlib":
            continue
        rows = preview["endpoints.jsonl"] if ref.get("kind") == "endpoint" else preview["symbols.jsonl"]
        now = [r for r in rows if r.get("name") == ref.get("name")
               and (ref.get("kind") is None or r.get("kind") == ref.get("kind"))]
        edge = _edge_path((report.get("resolved_edges") or {}).get(ref.get("name")))
        if now and edge and now[0].get("path") != edge:
            findings.append(_finding("DRIFT-EDGE", "warning", f"references.{ref.get('name')}", f"post-build index: {now[0].get('path')} vs check-time {edge}", "the symbol moved mid-build — re-check the proposal"))
    errs = [f for f in findings if f["severity"] == "error"]
    checks = {"loc": loc, "files_changed": files_changed, "new_symbols": sorted(set(new_syms)), "removed_symbols": sorted(set(removed_syms)), "advisories": [f for f in findings if f["severity"] == "advisory"], "scanned_repo_root": str(Path(repo_root).resolve()) if repo_root else None}
    ok = not errs
    hist = None
    if ok:
        # commit: real rescan (writes index/*.jsonl), THEN the receipt seals
        # over the post-commit state, THEN the CHG is sealed bound.
        ps.rescan(idx, [repo_root / f for f in files_changed], root=repo_root)
        hist = contracts.receipt(e, gate="propose-bind", subject=proposal_id, findings=[f for f in findings if f["severity"] == "warning"], checks=checks)
        chg_path, chg = _find_chg(e, proposal_id)
        if chg is not None:
            chg["bound"], chg["bound_at"], chg["history_receipt"] = True, contracts._now(), str(hist)
            _close_chg(e, chg_path, chg, str(hist))   # R1: ok:true CLOSES the CHG
    else:
        # a failing bind still seals a receipt (ok:false) for the honesty
        # trail (workcycle._verify_violations reads it) — but the INDEX is
        # untouched: no rescan call on this path, per B's byte-identity law.
        hist = contracts.receipt(e, gate="propose-bind", subject=proposal_id, findings=errs + [f for f in findings if f["severity"] == "warning"], checks=checks)
    return {"v": VERSION, "gate": "propose-bind", "subject": proposal_id, "ok": ok, "findings": findings, "checks": checks, "loc": loc, "files_changed": files_changed, "new_symbols": sorted(set(new_syms)), "removed_symbols": sorted(set(removed_syms)), "receipt": str(hist)}


# ── resolve ──────────────────────────────────────────────────────────────────

_ENDPOINT_NAME = re.compile(r"^(\S+)\s+(\S+)$")


def _contract_fit(room: Path, name: str, kind: str) -> str:
    """RULINGS A6: annotate-only, never filter/re-rank. "VERB /path" endpoint
    rows are checked against the FIRST well-formed loaded kind:api contract's
    base_path + collection/member verb membership; "unknown" when api.json is
    absent/unloadable/invalid, the row is not an endpoint, or the name does
    not parse as "VERB /path" (import_index emits the literal verb "ANY" —
    tolerated, always "unknown" since no verb list can ever contain it).
    Collection-vs-member (CT-A15, pinned): a path whose LAST segment is
    wrapped in "{...}" (a path parameter) is a MEMBER path; every other path
    under base_path is a COLLECTION path."""
    if kind != "endpoint":
        return "unknown"
    m = _ENDPOINT_NAME.match(name or "")
    if not m:
        return "unknown"
    verb, path = m.group(1).upper(), m.group(2)
    if verb == "ANY":
        return "unknown"
    apis = [c for c in _api_contracts(Path(room).resolve(), []) if isinstance(c["api"].get("base_path"), str)]
    if not apis:
        return "unknown"
    c = apis[0]["api"]
    base_path = c.get("base_path") or ""
    if not path.startswith(base_path):
        return "base-path-mismatch"
    seg = path.rstrip("/").rsplit("/", 1)[-1] if path.rstrip("/") else ""
    is_member = seg.startswith("{") and seg.endswith("}")
    verbs = c.get("member_verbs") if is_member else c.get("collection_verbs")
    if not isinstance(verbs, list):
        return "unknown"
    return "fit" if verb in verbs else "verb-not-allowed"


def resolve(room: Path, capability: str, *, kind: str | None = None, keywords: list[str] = ()) -> list[dict]:
    """propose_scan.similar wrapped for humans: [{name,path,line,kind,score,why,contract_fit}] top 10 across kinds (kind filter honored). `contract_fit` (RULINGS A6) is ANNOTATE-ONLY — "fit"|"base-path-mismatch"|"verb-not-allowed"|"unknown" — never filters or re-ranks. Never raises — a missing index yields []."""
    idx = Path(room).resolve() / "index"
    kinds = [kind] if kind else ["function", "class", "const", "endpoint", "component", "token", "export", "method"]
    merged = {}
    for k in kinds:
        for h in ps.similar(idx, capability, k, list(keywords)):
            rid = h["row"]["id"]
            if rid not in merged or h["score"] > merged[rid]["score"]:
                merged[rid] = h
    top = sorted(merged.values(), key=lambda h: h["score"], reverse=True)[:10]
    return [{"name": h["row"].get("name"), "path": h["row"].get("path"), "line": h["row"].get("line"),
             "kind": h["row"].get("kind"), "score": h["score"], "why": h["why"],
             "contract_fit": _contract_fit(room, h["row"].get("name"), h["row"].get("kind"))} for h in top]


# ── CLI ──────────────────────────────────────────────────────────────────────

def _resolve_room_flag(arg):
    """RULINGS A16: --room accepts EITHER the .echelon dir itself (arg/
    room.json exists) OR the repo root that contains it (arg/.echelon/
    room.json exists) — probe both, resolve absolute, error naming BOTH
    probes on a miss. Never creates the room. Returns
    (room_path_or_None, error_message_or_None)."""
    base = Path(arg).expanduser().resolve()
    p1, p2 = base / "room.json", base / ".echelon" / "room.json"
    if p1.exists():
        return base, None
    if p2.exists():
        return base / ".echelon", None
    return None, f"ROOM-UNAVAILABLE: no room at {p1} or {p2}"


def main(argv: list[str] | None = None) -> int:
    """CLI: check <id|path> [--room P] | bind <id> [--base SHA] [--room P] |
    resolve "<cap>" [--kind K] [--keywords a,b]. --room (RULINGS A14-A16,
    2e deliverable E) is accepted on check|bind ONLY — `index` (propose_scan.
    main) already takes explicit --index/--root and never resolves a room, so
    it needs no flag and stays stdlib-only. --room accepts a repo root or a
    .echelon dir; file scanning stays rooted at the CWD repo root (the
    worktree) while every room read/write (report, CHG, receipt, journal)
    targets the GIVEN room — a repo-identity mismatch between --room and cwd
    is a visible error (ROOM-REPO-MISMATCH), never a silent cross-repo index
    write (A15/CT-E9). Exit 0/1/2; degrades 2 + one stderr line."""
    ap = argparse.ArgumentParser(prog="echelon propose", description="The PROPOSAL GATE — read-before-code (PROPOSAL-GATE v3)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    pc = sub.add_parser("check", help="gate a proposal: check <id|path> [--room P]")
    pc.add_argument("target")
    pc.add_argument("--room", default=None, help="repo root or .echelon dir (default: discovered from cwd)")
    pb = sub.add_parser("bind", help="seal a diff to its proposal: bind <id> [--base SHA] [--room P]")
    pb.add_argument("id")
    pb.add_argument("--base", default=None, help="git base for the diff (default: report base or HEAD)")
    pb.add_argument("--room", default=None, help="repo root or .echelon dir (default: discovered from cwd)")
    pr = sub.add_parser("resolve", help='resolve "<capability>" [--kind K] [--keywords a,b]')
    pr.add_argument("capability")
    pr.add_argument("--kind", default=None)
    pr.add_argument("--keywords", default="")
    args = ap.parse_args(argv)
    repo_root = _repo_root(Path.cwd())
    room_flag = getattr(args, "room", None)
    if room_flag:
        room, err = _resolve_room_flag(room_flag)
        if room is None:
            print(f"propose: {err}", file=sys.stderr)
            return 2
        # M2 heal (gate round-1, A15 restored AS RULED): _room_repo_mismatch
        # compares resolved git-common-dirs — this is what makes a REAL
        # worktree (git worktree add) of the SAME repo pass (worktrees share
        # one .git/common-dir, so cwd_id == room_id even though the plain
        # .git PATHS differ), while catching a genuinely UNRELATED repo
        # (probe8: room at repo A from cwd/repo_root in unrelated repo B
        # silently poisoned A's index with B's rows via ps._upsert dropping
        # A's real rows on path collision). check()/bind() carry the SAME
        # guard for direct library callers who never touch this CLI path.
        if _room_repo_mismatch(room, repo_root):
            print(f"propose: ROOM-REPO-MISMATCH — --room {room} is a different "
                  f"repo than cwd {Path.cwd()} (repo_root {repo_root})", file=sys.stderr)
            return 2
    else:
        room = workcycle.room_path()
        if room is None:
            print("propose: no room (run `workcycle init`)", file=sys.stderr)
            return 2
    if args.cmd == "check":
        t = args.target
        path = Path(t) if (t.endswith(".json") or "/" in t or "\\" in t) else room / "proposals" / f"{t}.json"
        proposal = load_proposal(path)
        if not proposal:
            return 2
        rep = check(room, proposal, repo_root=repo_root)
        print(json.dumps(rep, ensure_ascii=False, indent=2))
        return {"PROCEED": 0, "REVISE": 1, "AMEND": 1, "REJECT": 2}.get(rep.get("verdict"), 2)
    if args.cmd == "bind":
        # M-1 heal (gate r1): --base passes THROUGH to bind() as base=, so the
        # new law runs — pre-computing the diff here made git_backed False and
        # the bind read the index (the INC-triplet incident path).
        res = bind(room, args.id, repo_root=repo_root, base=args.base)
        print(json.dumps(res, ensure_ascii=False, indent=2))
        errs = any(f["severity"] == "error" for f in res["findings"])
        warns = any(f["severity"] == "warning" for f in res["findings"])
        return 2 if errs else (1 if warns else 0)
    kw = [k.strip() for k in args.keywords.split(",") if k.strip()] if args.keywords else []
    print(json.dumps(resolve(room, args.capability, kind=args.kind, keywords=kw), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
