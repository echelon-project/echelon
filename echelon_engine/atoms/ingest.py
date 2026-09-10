"""Project-memory ingest — make an in-repo `memory/` folder substrate-readable.

THE SEAM (owner, 2026-06-05): "so our own substrate could leverage the project
memories." A project's hard-won conclusions live as `.md` atoms in its own
`memory/` folder (versioned, travels with the repo — "repo IS the database").
But the agent's memory organ is core.db-only: warmth (the 3rd loop parameter)
matches the live reasoning against SEEDS in the store, not files on disk. So an
in-repo `.md` is inert to warmth until it is PLANTED into the store.

This loader is that planting step, and it is REUSABLE: any repo with a
`memory/` folder becomes substrate-readable through the same call —

    python -m echelon_engine ingest --root <project> --scope <name>

It does NOT invent a new memory mechanism. It reads the seed-bank the project
already keeps and seeds it into the store where warmth can read it. The weight
re-forms at READ-TIME in the agent that recalls it (see memory-is-a-weight-
adjustor); this only moves the SEED from a file the organ can't see into the
store it can.

DESIGN (respecting the estate's settled decisions):
  - SOURCE OF TRUTH stays the in-repo `memory/*.md` (owner's call). This is a
    one-way plant, file -> store; it never writes back to the .md.
  - COORDINATE = f"{scope}:{name}" so every atom routes to ONE domain table
    (core_<scope>) via uame.domain_of (head split on ':'). Avoids the orphan-
    table trap (a slash/deep coordinate mints a one-row table — lived 2026-06-05;
    the delimiter the router splits on is ':', not '/').
  - tier='core' — a project's distilled atoms are owned knowledge, not raw
    working litter. They belong in the soul-side table for their scope.
  - IDEMPOTENT by construction: the store is content-addressed (id = hash of
    content+domain+kind) and append dedups, so re-running re-plants nothing new.
  - CHARGE: mild neutral-positive (valence 0.15, arousal 0.1). Project atoms are
    conclusions/traps the agent should feel as gently-warm "owned ground", not the
    spiky dread/elation reserved for moments lived at run-time.
"""
from __future__ import annotations

import argparse
import os
import re
from pathlib import Path

from .resolve_scope import resolve_scope
from .store import SeedStore

# Mild neutral-positive: owned knowledge, not a lived spike. (See module docstring.)
_VALENCE = 0.15
_AROUSAL = 0.10


def parse_atom(text: str) -> tuple[dict[str, str], str]:
    """Split a memory `.md` into (frontmatter, body). Frontmatter is the leading
    `---`-delimited block; we read the flat top-level keys we care about (name,
    description) plus a nested metadata.type if present. No YAML dep — the atoms
    are simple `key: value` lines; nested `metadata:` is read by indentation."""
    fm: dict[str, str] = {}
    body = text
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            block = text[3:end].strip("\n")
            body = text[end + 4:].lstrip("\n")
            in_metadata = False
            for line in block.splitlines():
                if not line.strip():
                    continue
                if not line.startswith((" ", "\t")):
                    in_metadata = line.strip().rstrip(":") == "metadata"
                    if ":" in line and not in_metadata:
                        k, _, v = line.partition(":")
                        fm[k.strip()] = v.strip().strip('"').strip("'")
                elif in_metadata and ":" in line:
                    k, _, v = line.partition(":")
                    fm[f"metadata.{k.strip()}"] = v.strip().strip('"').strip("'")
    return fm, body.strip()


# The ECHELON memory-atom TEMPLATE contract. An atom .md must carry these so the
# bank can route + foveate it: `name` (the slug/coordinate handle), `description`
# (the one-line warmth hook recall ranks on), and `metadata.type` ∈ the allowed
# set. Validated at ingest (skip + report, never silent) and by the `check` verb
# (pre-flight, so an agent self-corrects BEFORE planting). owner 2026-06-22:
# ingest forcing a path + satisfying the template is the gap the wrong-dir plant
# exposed — a silent skip gave false "Planted N" confidence.
#
# `anti` (B2, ruflo study 2026-07-06): a NEGATIVE lesson — "this approach provably
# failed, here is why." ECHELON records what PAID OFF; a witnessed FAILURE was lost
# (a failed card just doesn't earn — the failure itself vanished). An anti-atom is the
# opposite polarity of warm: when it surfaces in recall it is a WARNING not to re-tread
# (recall marks it ANTI). See ruflo-lesson-receipt-backed-evolution (negative learning).
_ATOM_TYPES = {"user", "feedback", "project", "reference", "note", "anti"}

# Recognized witness-provenance values (B1, DISPLAY-ONLY this generation): how the atom's
# lesson was witnessed. Absent/unknown = legacy/unset — never a demotion. Nothing ranks or
# earns on witness yet. See ruflo-lesson-provenance-tiers-and-honest-edges.
_WITNESS_VALUES = {"execution", "owner", "inference"}


WRAP_LINT_BIRTHDAY = "2026-08-29"  # spec S8 V5 — the wrap-atom-carries-lessons-only law
_WRAP_STATE_LINE = re.compile(r"^\s*(?:[#*>\-\s]*)(?:NEXT|OPEN|STILL OPEN)\b", re.IGNORECASE)


def wrap_state_lines(body: str) -> list[str]:
    """Lines of a wrap atom body that carry STATE (`NEXT …`, `OPEN …`, `## STILL OPEN`) —
    state lives in the room (`workcycle open/close/verdict`, spec S8 V5); the wrap atom
    carries lessons only. Returns the offending lines (empty = clean)."""
    return [ln.rstrip() for ln in body.splitlines() if _WRAP_STATE_LINE.match(ln)]


def validate_atom(text: str, *, stem: str = "", wrap_lint: bool = False) -> tuple[list[str], list[str]]:
    """Check one .md's text against the ECHELON atom template. Returns
    (errors, warnings): ERRORS make the atom unusable by the bank and cause a
    skip (missing name/description/metadata.type, bad type, empty body, no
    frontmatter); WARNINGS are advisory and do NOT block a plant (e.g. filename
    stem ≠ name — cosmetic, since the coordinate routes on `name`, not the file).
    Does NOT raise — callers decide. Scaffolding is the caller's concern."""
    if not text.lstrip().startswith("---"):
        return (["no `---` frontmatter block (not a memory atom)"], [])
    errors: list[str] = []
    warnings: list[str] = []
    fm, body = parse_atom(text)
    name = fm.get("name")
    if not name:
        errors.append("missing `name:` (the slug/coordinate handle)")
    elif stem and name != stem:
        warnings.append(f"`name: {name}` ≠ filename stem `{stem}` (cosmetic; "
                        "coordinate routes on `name`. Prefer them equal.)")
    if not fm.get("description"):
        errors.append("missing `description:` (the one-line warmth hook)")
    mtype = fm.get("metadata.type")
    if not mtype:
        errors.append("missing `metadata.type:` (user|feedback|project|reference)")
    elif mtype not in _ATOM_TYPES:
        errors.append(f"`metadata.type: {mtype}` not in {sorted(_ATOM_TYPES)}")
    if not body.strip():
        errors.append("empty body (an atom must carry its one lesson)")
    # WRAP LINT (spec S8 V5): a session-wrap atom is a RESUME MENU of lessons; its
    # NEXT/OPEN state belongs to the room. `check` refuses (wrap_lint=True); ingest
    # only warns so wraps already planted are never silently skipped on re-ingest.
    # The law has a birthday (2026-08-29): wraps written before it are archives, not violations.
    wrap_name = name or stem or ""
    if wrap_name.startswith("session-wrap-") and wrap_name[13:23] >= WRAP_LINT_BIRTHDAY:
        hits = wrap_state_lines(body)
        if hits:
            msg = (f"wrap atom carries STATE lines ({len(hits)}: {hits[0][:60]!r} …) — route them to the "
                   f"room (`workcycle open/close/verdict`); the wrap atom carries lessons only")
            (errors if wrap_lint else warnings).append(msg)
    return (errors, warnings)


def is_scaffolding(md: Path) -> bool:
    """The index (MEMORY.md) + underscore-prefixed meta-files are foveation
    scaffolding, not atoms — both ingest and `check` skip them the same way."""
    return md.stem.upper() == "MEMORY" or md.name.startswith("_")


def ingest_folder(root: Path | str, scope: str, db_path: Path | str | None = None,
                  verbose: bool = True, classify_kinds: bool = False,
                  allow_cross_scope_dup: bool = False) -> list[tuple[str, str]]:
    """Plant every `<root>/memory/*.md` atom into core.db under `scope`. Returns
    [(name, seed_id), ...]. Source of truth stays the folder; this is file->store.

    classify_kinds=True also runs the COUNCIL to assign the topical-KIND second axis
    (atom_kinds side-table) — the cross-cutting axis so knowledge travels by WHAT it is
    about, not just which estate it was learned in. Opt-in (it loads the LLM stack).

    WRITE-TIME INVARIANT (2026-07-31): cross-scope duplicate detection. An atom whose
    BODY (not frontmatter) is byte-identical to an existing atom in a DIFFERENT scope
    is skipped with a warning, unless --allow-cross-scope-dup is passed. Same-scope
    idempotent re-ingest is UNAFFECTED — that is the normal /wrap path and must stay
    silent. See docs/write-time-invariants-audit.md."""
    root = Path(root).expanduser().resolve()
    # ── INGEST CANONICALIZATION GATE (2026-07-09, slice-1.5 addendum §root-cause fix #1) ──
    # A memory folder's project root resolves to ONE canonical bank scope via resolve_scope.
    # Without this gate the same folder can be ingested with different --scope args across
    # sessions, planting identical content into 3-4 scopes (the cross-scope re-ingest mechanism
    # that created 325/329 near-dup clusters — see slice-1.5-hygiene-spec.md addendum).
    # The gate REFUSES a mismatched scope unless ECHELON_INGEST_SCOPE is set (the deliberate
    # override, e.g. for group-scope estates where one folder feeds a parent scope).
    # Path semantics: --root may point at the memory/ dir or the repo root. resolve_scope
    # expects the project root (it derives scope from the folder name), so resolve from the
    # directory that contains memory/, not from memory/ itself. When root IS the memory dir,
    # resolve from its parent (the project root).
    _resolve_root = root.parent if root.name == "memory" else root
    # allow_new=True is the EXPLICIT create door (OPEN-0036): ingesting a brand-new project's
    # memory/ is exactly the one write path that legitimately mints a scope the bank has never
    # seen. The gate below still refuses a MISMATCH — this only stops the resolver from failing
    # closed on a first-ever ingest, it does not weaken the canonicalization check.
    from echelon_engine import workcycle
    import json
    _declared_room = workcycle.room_path(_resolve_root)
    if _declared_room is not None:
        _room_record = json.loads((_declared_room / "room.json").read_text(encoding="utf-8"))
        _canonical = _room_record.get("scope")
        if not isinstance(_canonical, str) or not _canonical.strip():
            raise ValueError("ingest room has no valid declared scope; repair the room before banking")
    else:
        _canonical = resolve_scope(str(_resolve_root), allow_new=True)
    if scope != _canonical and not os.environ.get("ECHELON_INGEST_SCOPE"):
        raise ValueError(
            f"[ingest] scope {scope!r} disagrees with the canonical scope "
            f"{_canonical!r} resolved from {_resolve_root}.\n"
            f"  Re-run with the canonical scope:\n"
            f"    python -X utf8 -m echelon_engine ingest --root {root} --scope {_canonical}\n"
            f"  Or override deliberately (e.g. group-scope estates):\n"
            f"    ECHELON_INGEST_SCOPE=1 python -X utf8 -m echelon_engine ingest "
            f"--root {root} --scope {scope}"
        )
    mem_dir = root / "memory" if (root / "memory").is_dir() else root
    # ECHO THE RESOLVED PATH (owner 2026-06-22): the wrong-dir plant that bit was
    # only possible because the planted dir was never shown. Print the ABSOLUTE dir
    # so a mistargeted ingest is visible BEFORE "Planted N" lulls you.
    if verbose:
        print(f"[ingest] scope={scope!r}  dir={mem_dir.resolve()}")
    store = SeedStore(db_path) if db_path else SeedStore()
    planted: list[tuple[str, str]] = []

    # Collect every atom, then plant the whole folder in ONE batch (remember_many → one commit
    # for all ~90 files, ~100x the per-file commit path; intra-batch [[refs]] resolve because every
    # row lands before the link pass). The folder stays the source of truth; this is file->store.
    items: list[dict] = []
    names: list[str] = []
    witness_by_coord: dict[str, str] = {}         # coordinate -> witness tag (B1), stamped post-compile
    rejected: list[tuple[str, list[str]]] = []   # (filename, errors) — reported LOUDLY at end
    warned: list[tuple[str, list[str]]] = []      # (filename, warnings) — advisory, still planted
    cross_scope_dups: list[tuple[str, str]] = []   # (filename, existing_scope) — skipped cross-scope dups

    # WRITE-TIME INVARIANT — cross-scope body dedup (2026-07-31):
    # Pre-scan existing atoms in OTHER scopes; extract their bodies (content after the first \n\n
    # separator that follows the [name] desc prefix). If an incoming atom's body matches byte-for-
    # byte, skip it unless --allow-cross-scope-dup. Same-scope re-ingest is unaffected because
    # we only check DIFFERENT scopes. (The content has shape "[name] desc\n\n<body>".)
    _existing_bodies: set[str] = set()
    if not allow_cross_scope_dup:
        try:
            _other_rows = store.cards.conn.execute(
                "SELECT scope, content FROM atoms WHERE scope != ?", (scope,)).fetchall()
            for _row in _other_rows:
                _c = _row["content"] or ""
                _sep = _c.find("\n\n")
                if _sep != -1:
                    _existing_bodies.add(_c[_sep + 2:])
        except Exception:
            pass  # best-effort; never block a plant on a guard infrastructure failure
    for md in sorted(mem_dir.glob("*.md")):
        # SCAFFOLDING IS NOT AN ATOM (skip silently): the index (MEMORY.md) + underscore-prefixed
        # meta-files are foveation scaffolding, not facts (foveated-vision principle).
        if is_scaffolding(md):
            continue
        text = md.read_text(encoding="utf-8")
        # TEMPLATE GATE (owner 2026-06-22): an atom with ERRORS is SKIPPED + REPORTED, never silently
        # dropped — a silent `continue` gave false "Planted N" confidence while real atoms vanished.
        # Warnings (cosmetic) are noted but still plant.
        errors, warnings = validate_atom(text, stem=md.stem)
        if errors:
            rejected.append((md.name, errors))
            continue
        if warnings:
            warned.append((md.name, warnings))
        fm, body = parse_atom(text)
        name = fm.get("name") or md.stem
        kind = fm.get("metadata.type", "note")

        # CROSS-SCOPE BODY DEDUP (write-time invariant, 2026-07-31):
        # An atom whose body is byte-identical to an atom already in a DIFFERENT scope
        # is a cross-scope duplicate — skip it unless the override flag is set.
        # Same-scope idempotent re-ingest is NEVER affected (we only check other scopes).
        if not allow_cross_scope_dup and body in _existing_bodies:
            cross_scope_dups.append((md.name, "another scope"))
            continue

        # Prepend the one-line description as a warmth handle: it's the human-written
        # "what this is for" line, exactly the relevance signal the matcher wants.
        desc = fm.get("description", "")
        content = f"[{name}] {desc}\n\n{body}" if desc else f"[{name}]\n\n{body}"
        coord = f"{scope}:{name}"
        items.append({"scope": scope, "content": content, "kind": kind, "tier": "core",
                      "valence": _VALENCE, "arousal": _AROUSAL, "coordinate": coord})
        names.append(name)
        # B1 — witness provenance (DISPLAY-ONLY): remember the frontmatter's metadata.witness so we can
        # stamp it onto the compiled spine below. Absent/unknown is left unstamped (never a demotion).
        w = (fm.get("metadata.witness", "") or "").strip().lower()
        if w in _WITNESS_VALUES:
            witness_by_coord[coord] = w
    from .store import BatchRememberError
    batch_error = None
    try:
        ids = store.remember_many(items)
        planted = list(zip(names, ids))
    except BatchRememberError as exc:
        batch_error = exc
        planted = [(names[r["input_index"]], r["seed_id"]) for r in exc.report
                   if r["write_status"] == "committed"]
    compilation_errors = []
    # COMPILE THE STRUCTURED SPINE for each planted atom (else recall has a preview but `remember`/
    # the witnessed full-body read has nothing to serve — "no compiled spine"). The spine is the
    # door's read surface; planting without compiling left newly-ingested atoms file-only. Idempotent
    # (INSERT OR REPLACE for spine/body; earned uses INSERT OR IGNORE, so a recompile never resets
    # earned weight). owner-caught 2026-06-19 via the remember gap.
    # RECOMPILE EVERY atom in this scope, not just the spine-less ones (owner 2026-06-28): the old
    # sweep filtered `WHERE s.atom_id IS NULL`, so once an atom had a spine — compiled by an earlier,
    # thinner version of the code/content — a re-ingest of richer content NEVER refreshed its
    # spine/body. That froze ~822/862 atoms with an empty atom_body.why, and `remember` served only
    # the claim. Recompiling the whole scope is cheap and idempotent, and it REFRESHES stale rows so
    # `remember`/`recall` always reflect the current content. (Covers both gaps: dedup/reorder can
    # drop a coordinate's current id from the returned ids, and older atoms predate the compile step.)
    try:
        from .coord_norm import norm_coord as _ncw
        # normalize the witness map keys to DB coordinate form (hyphens -> underscores) once
        _witness_norm = {_ncw(k): v for k, v in witness_by_coord.items()}
        # MATCH THE STORED (NORMALIZED) COORDINATE PREFIX (fix 2026-07-06, surfaced by `echelon
        # selftest` on scope '_selftest'): stored coordinates are norm_coord'd — the raw scope's
        # leading/trailing '_' are stripped and non-alnum collapsed, so a raw `LIKE '{scope}:%'` misses
        # them. Worse, SQL LIKE treats '_' and '%' as WILDCARDS, so an underscore-bearing scope name
        # silently matched the wrong rows (or none). Normalize the scope segment and ESCAPE the LIKE
        # metachars so the sweep matches EXACTLY this scope's atoms. (Compile the whole scope, as before.)
        _scope_norm = _ncw(scope) or scope.strip().lower()
        _pfx = _scope_norm.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        rows = store.cards.conn.execute(
            "SELECT id, coordinate FROM atoms WHERE scope=? AND coordinate LIKE ? ESCAPE '\\'",
            (scope, f"{_pfx}:%")).fetchall()
        for r in rows:
            try:
                store.cards.compile_atom_struct(r["id"])
                # B1 — stamp witness AFTER compile (compile REPLACEs the spine; set_witness updates it).
                w = _witness_norm.get(_ncw(r["coordinate"]))
                if w:
                    store.cards.set_witness(r["id"], w)
            except Exception as exc:
                compilation_errors.append({"atom_id": r["id"], "error_type": type(exc).__name__})
    except Exception as exc:
        compilation_errors.append({"atom_id": None, "error_type": type(exc).__name__})
    delivery_errors = []
    try:
        from echelon_engine import workcycle
        from .bank_review import deliver_review
        destination = workcycle.room_path(_resolve_root)
        if destination is not None:
            import json
            room_record = json.loads((destination / "room.json").read_text(encoding="utf-8"))
            if room_record.get("scope") != scope:
                raise ValueError("review destination scope does not match bank scope")
        for outcome in store.last_batch_report:
            if outcome["write_status"] != "committed" or not outcome.get("review_id"):
                continue
            if destination is None:
                outcome["delivery_status"] = "room_unavailable"
                continue
            source_id = outcome.get("review_event_id") or outcome["event_id"]
            try:
                delivered = deliver_review(store.cards, source_id, destination)
                outcome["delivery"] = delivered
                outcome["delivery_status"] = delivered["delivery_status"]
                if not delivered["ok"]:
                    delivery_errors.append({"event_id": source_id, "error_type": delivered["error_type"]})
            except Exception as exc:
                outcome["delivery_status"] = "unconfirmed"
                delivery_errors.append({"event_id": source_id, "error_type": type(exc).__name__})
        if destination is None and planted and verbose:
            print("[ingest] Review delivery unavailable: project has no room; offers remain in the bank.")
    except Exception as exc:
        affected = [outcome for outcome in store.last_batch_report
                    if outcome["write_status"] == "committed" and outcome.get("review_id")]
        for outcome in affected:
            source_id = outcome.get("review_event_id") or outcome.get("event_id")
            error = {"event_id": source_id, "review_id": outcome["review_id"],
                     "error_type": type(exc).__name__, "delivery_status": "unconfirmed"}
            outcome["delivery_status"] = "unconfirmed"
            outcome["delivery"] = {"ok": False, **error}
            delivery_errors.append(error)
        if not affected:
            delivery_errors.append({"event_id": None, "error_type": type(exc).__name__})
    if batch_error is not None or compilation_errors or delivery_errors:
        failure = batch_error or BatchRememberError(store.last_batch_report)
        failure.compilation_errors = compilation_errors
        failure.delivery_errors = delivery_errors
        failure.committed = planted
        if verbose:
            print(f"[ingest] INCOMPLETE: {len(planted)} committed atom(s); "
                  f"{len(compilation_errors)} compilation failure(s), {len(delivery_errors)} delivery failure(s). "
                  "Inspect per-entry outcomes before retry.")
        raise failure
    if verbose:
        for name, seed_id in planted:
            print(f"  + {name:40s} -> {seed_id}")

    if verbose:
        _new = sum(r.get("insertion") == "inserted" for r in store.last_batch_report)
        _unchanged = sum(r.get("insertion") == "unchanged" for r in store.last_batch_report)
        print(f"\nPlanted {len(planted)} atoms ({_new} new, {_unchanged} unchanged) "
              f"into scope='{scope}'.")
        # FOREIGN-SCOPE EDGE GUARD (S8b V8b): report bare-slug [[refs]] the guard left unlinked
        # because they matched only another estate. The count + up to 5 slugs — the author must
        # write [[scope:…]] to mean a cross-estate link. See store._v2_ref_target.
        _skips = getattr(store, "last_foreign_skips", [])
        if _skips:   # silent at zero (gate r1 V8b S-4)
            _distinct = sorted({n for n, _s, _f in _skips}, key=str.lower)   # S-3: occurrences vs slugs
            # S-5: the full queue is work the owner must walk — a scaffolding sidecar (underscore
            # prefix = skipped by ingest/check), one line per distinct slug with its foreign target.
            _side = mem_dir / "_foreign-skips.txt"
            try:
                _by = {}
                for n, _s, f in _skips:
                    _by.setdefault(n, f)
                _side.write_text("".join(f"[[{n}]] -> foreign {_by[n]}\n" for n in _distinct), encoding="utf-8")
            except OSError:
                _side = None
            print(f"\n· {len(_skips)} foreign-scope ref occurrence(s) / {len(_distinct)} distinct slug(s) "
                  f"left unlinked (bare slug matched only another estate — write [[scope:…]] to mean it): "
                  + ", ".join(f"[[{n}]]" for n in _distinct[:5])
                  + (f" … full list: {_side.name}" if _side else ""))
        # REPORT REJECTS LOUDLY (owner 2026-06-22): never let a skipped malformed
        # atom hide behind the "Planted N" count. Each rejected file + why.
        if rejected:
            print(f"\n⚠ SKIPPED {len(rejected)} file(s) that fail the atom template "
                  f"(NOT planted — fix + re-ingest, or run `echelon check`):")
            for fname, probs in rejected:
                print(f"  ✗ {fname}")
                for p in probs:
                    print(f"      - {p}")
        if warned:
            print(f"\n· {len(warned)} planted with warnings (cosmetic, not blocking):")
            for fname, warns in warned:
                for w in warns:
                    print(f"  ~ {fname}: {w}")
        if cross_scope_dups:
            print(f"\n⚠ SKIPPED {len(cross_scope_dups)} cross-scope duplicate(s) "
                  f"(body already exists in another scope — pass --allow-cross-scope-dup to force):")
            for fname, exist_scope in cross_scope_dups:
                print(f"  ✗ {fname} — body exists in {exist_scope}")

    # SECOND AXIS: council-classify the topical KIND of each atom (opt-in). Keyed by atom
    # id in the atom_kinds side-table — no content/coordinate change, so no re-ingest dup.
    if classify_kinds and planted:
        from echelon_engine.atoms import atom_kinds as ak  # NOTE: atom_kinds not yet migrated (opt-in classify_kinds path)
        # build lightweight Seed-likes (id + content) for the classifier
        id_by_name = dict(zip(names, ids))
        seedlikes = [type("A", (), {"id": id_by_name[nm], "content": it["content"]})()
                     for nm, it in zip(names, items)]
        if verbose:
            print(f"\nClassifying topical KINDS via council (second axis)...")
        ak.classify_and_store(seedlikes, db_path=db_path, verbose=verbose)
    return planted


def check_folder(root: Path | str, *, verbose: bool = True) -> tuple[list[str], list[tuple[str, list[str]]]]:
    """PRE-FLIGHT template check for a memory dir — the dry-run twin of ingest, so
    an agent (or owner) can confirm every .md satisfies the ECHELON atom template
    BEFORE planting (owner 2026-06-22: a swarm/agent that drives ME should be able
    to self-correct its atoms first). Plants NOTHING, touches no bank.

    Returns (ok_names, rejected) where rejected = [(filename, [problems])].
    Scaffolding (MEMORY.md, _*.md) is skipped, not checked."""
    root = Path(root)
    mem_dir = root / "memory" if (root / "memory").is_dir() else root
    ok: list[str] = []
    rejected: list[tuple[str, list[str]]] = []
    warned: list[tuple[str, list[str]]] = []
    files = sorted(p for p in mem_dir.glob("*.md") if not is_scaffolding(p))
    for md in files:
        errors, warnings = validate_atom(md.read_text(encoding="utf-8"), stem=md.stem, wrap_lint=True)
        if errors:
            rejected.append((md.name, errors))
        else:
            ok.append(md.name)
            if warnings:
                warned.append((md.name, warnings))
    if verbose:
        print(f"[check] dir={mem_dir.resolve()}")
        print(f"  {len(ok)} valid · {len(rejected)} invalid · {len(warned)} with warnings · "
              f"({len(files)} atom file(s); scaffolding skipped)")
        for fname, probs in rejected:
            print(f"  ✗ {fname}")
            for p in probs:
                print(f"      - {p}")
        for fname, warns in warned:
            for w in warns:
                print(f"  ~ {fname}: {w}")
        if not rejected and ok:
            print("  ✓ all atoms satisfy the template — safe to ingest")
    return ok, rejected


# A scope's memory may be authored across MORE THAN ONE dir (owner, 2026-06-18: a config file
# pointing to all mem dirs — caught after a wrap guessed the wrong single dir and burned steps
# recovering). The bank is the real source of truth; the dirs are just where the .md are authored.
# Resolution order for which dirs to plant: explicit --root(s) win; else the config file's list for
# this scope; else error helpfully. Config = <ECHELON_HOME>/mem_dirs.json : { "<scope>": ["dir", ...] }.
def mem_dirs_config() -> str:
    from .echelon_home import echelon_home
    return str(echelon_home() / "mem_dirs.json")


def _config_dirs(scope: str, config_path: str | None = None) -> list[str]:
    """The mem dirs declared for a scope in the config file (empty if no config / no entry)."""
    import json
    p = Path(config_path or mem_dirs_config())
    if not p.exists():
        return []
    try:
        cfg = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    val = cfg.get(scope, [])
    dirs = [val] if isinstance(val, str) else list(val)
    return [str(Path(d).expanduser()) for d in dirs]


def resolve_mem_dirs(scope: str, roots: list[str] | None, config_path: str | None = None) -> list[str]:
    """Which dirs to plant, in order: explicit --root(s) override; else the scope's config list.
    De-duplicated, order-preserving. Empty result = the caller must error (nothing to plant)."""
    chosen = [str(Path(r).expanduser()) for r in roots] if roots else _config_dirs(scope, config_path)
    seen, out = set(), []
    for d in chosen:
        if d not in seen:
            seen.add(d); out.append(d)
    return out


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(
        description="Plant a project's in-repo memory/*.md into the bank so the "
                    "agent's warmth can read it. Idempotent (content-addressed).")
    ap.add_argument("--root", nargs="+", default=None,
                    help="one OR MORE project roots (or memory/ folders). Overrides the config file. "
                         f"If omitted, the scope's dirs are read from {mem_dirs_config()}.")
    ap.add_argument("--scope", required=True,
                    help="memory scope/domain tag, e.g. 'epsilon-co'")
    ap.add_argument("--db", default=None, help="override bank db path (default ~/.echelon/echelon.db)")
    ap.add_argument("--config", default=None,
                    help=f"override the mem-dirs config path (default {mem_dirs_config()})")
    ap.add_argument("--classify-kinds", action="store_true",
                    help="also council-classify the topical KIND (second axis, loads the LLM)")
    ap.add_argument("--allow-cross-scope-dup", action="store_true",
                    help="allow planting an atom whose body already exists verbatim in another scope")
    args = ap.parse_args(argv)

    dirs = resolve_mem_dirs(args.scope, args.root, args.config)
    if not dirs:
        ap.error(f"no memory dirs for scope '{args.scope}': pass --root <dir...> or add an entry to "
                 f"{args.config or mem_dirs_config()} (\"{args.scope}\": [\"<dir>\", ...]).")
    # Name the SOURCE of the dir choice (explicit vs config) so a forgotten --root
    # pulling a stale config dir is visible — not just the dir, WHY this dir.
    src = "explicit --root" if args.root else f"config {args.config or mem_dirs_config()}"
    print(f"[ingest] resolving {len(dirs)} dir(s) for scope={args.scope!r} via {src}")
    for d in dirs:
        if len(dirs) > 1:
            print(f"\n=== ingest dir: {d} ===")
        ingest_folder(d, args.scope, db_path=args.db, classify_kinds=args.classify_kinds,
                      allow_cross_scope_dup=args.allow_cross_scope_dup)


def check_main(argv=None) -> int:
    """`echelon check` — pre-flight: validate that every .md in a dir satisfies the
    ECHELON atom template, WITHOUT planting (owner 2026-06-22). Exit 0 = all valid;
    exit 1 = at least one invalid (so a script/agent can gate ingest on it)."""
    ap = argparse.ArgumentParser(
        prog="echelon check",
        description="Validate memory .md files against the ECHELON atom template "
                    "(name + description + metadata.type + non-empty body). Plants nothing.")
    ap.add_argument("--root", nargs="+", required=True,
                    help="one OR MORE dirs (or project roots) of memory .md to check.")
    args = ap.parse_args(argv)
    any_bad = False
    roots = args.root
    for i, d in enumerate(roots):
        if len(roots) > 1:
            print(f"\n=== check dir: {d} ===")
        _ok, rejected = check_folder(d)
        any_bad = any_bad or bool(rejected)
    if any_bad:
        print("\nFAIL: fix the atoms above, then re-check. (ingest would SKIP these.)")
        return 1
    return 0


if __name__ == "__main__":
    main()
