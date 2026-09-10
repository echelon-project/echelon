"""reflex — compile reflex-FLAGGED atoms into the body's static ruleset.

THE ORGAN (owner, 2026-07-08): "current reflex are more like a mini note on what to
remember. what reflex in my mind are like claudecode tools discipline — read the file
before edit; or auto mode blocking the risk action. but it was pre-determined with
programmed code. we need a way to make reflex a flagged atom, surface when text in/out
happen." The insight: a reflex's trigger lives in EVENT-space, not topic-space. Warmth
fires on what a prompt is ABOUT; a real reflex fires on what is about to HAPPEN
(a tool call, an outgoing answer) — it lives at the choke point, not in the context
window, which is exactly why the harness's own read-before-edit never gets forgotten.

THE MECHANISM — learned content, programmed firing:
  - An atom stays an authored .md lesson (memory). Adding `reflex: true` + a trigger
    spec under `metadata:` FLAGS it for the body.
  - `echelon reflex compile --root <memory dir> --scope <s>` compiles every flagged
    atom into ~/.echelon/reflexes.json — a tiny static ruleset.
  - The event hooks (PreToolUse / UserPromptSubmit / Stop) load that JSON in
    milliseconds and fire matching rules. THE BANK IS CONSULTED AT COMPILE TIME,
    NEVER AT FIRE TIME — a reflex is fast BECAUSE it does not consult the brain
    (tool events fire dozens of times per turn; a warmth probe there would be
    cortical latency on a spinal path).

ATOM FRONTMATTER CONTRACT (flat keys under metadata:, fits ingest.parse_atom):
    metadata:
      type: feedback
      reflex: true
      reflex-event: PreToolUse        # PreToolUse | UserPromptSubmit | Stop
      reflex-tool: Bash               # regex on tool_name (PreToolUse only; default .*)
      reflex-match: <regex>           # regex on the serialized event payload
                                      # (keep it BACKSLASH-FREE: use [.] not backslash-dot —
                                      # YAML-normalizing writers double backslashes and
                                      # parse_atom does not unescape)
      reflex-action: warn             # warn (inject guard text) | block (deny the action)
      reflex-tier: scope              # scope (default) | global | portable — see TIER DOCTRINE
      reflex-cwd: <regex>             # OPTIONAL: rule fires only when the session cwd
                                      # matches (narrowing lever for estate-local traps —
                                      # the gamma-support flux-venv reflex firing in echelon
                                      # sessions was the 2026-07-31 spurious-fire champion).
                                      # For tier: scope this is AUTO-DERIVED when omitted.
      reflex-guard: full              # OPTIONAL: carry the FULL atom body as guard text
                                      # (composite checklist rules need their whole body;
                                      # default remains first-paragraph, GUARD_MAX-capped)
    The atom BODY's first paragraph is the guard text the model sees when it fires.

TEETH DOCTRINE: warn-by-default, block-by-declaration. A false-firing hard block on
the owner's channel violates the severability law the nerve lives by — so an atom
must explicitly declare `reflex-action: block` to get teeth.

TIER DOCTRINE (2026-08-15) — WHERE a reflex fires, orthogonal to teeth (which is how
hard it fires). Born from an audit finding 140 of 141 compiled rules had NO cwd fence:
`scope` was a display label only, so every estate's rules were evaluated against every
other estate's tool calls. Same defect class as the arc-card global-spine bug — the
scope was present on the object and simply absent from the decision.

  scope    (DEFAULT) — estate-local. Fires ONLY inside its own estate. If the atom
           declares no reflex-cwd, one is AUTO-DERIVED at compile time from the
           compile --root, so an estate trap CANNOT leak into another estate through
           forgetfulness. This is the safe default precisely because most reflexes
           are estate traps (deploy paths, box hostnames, service users).
  global   — machine-wide truths that hold in EVERY estate: bank integrity, secret
           handling, harness traps (the claude-deep port race), git/tooling laws.
           Never auto-fenced. Declaring global is an ASSERTION that the rule is
           estate-independent — if it names a path, a host, or a service, it is not.
  portable — a global that ALSO ships in the starter seed for a new install. The
           subset of hard-won discipline a brand-new user should be protected by on
           day one, before they have earned any scars of their own. Fires like global.

Idempotent: compile REPLACES the rules for its scope and keeps other scopes' rules.
"""
from __future__ import annotations

import argparse
import json
import os
import re
from pathlib import Path

from .ingest import parse_atom

RULESET = Path(os.path.expanduser("~/.echelon/reflexes.json"))
EVENTS = {"PreToolUse", "UserPromptSubmit", "Stop"}
ACTIONS = {"warn", "block"}
TIERS = {"scope", "global", "portable"}
DEFAULT_TIER = "scope"  # estate-local by default; global is an explicit assertion
GUARD_MAX = 400  # chars of the atom body carried into the ruleset (the guard text)
GUARD_MAX_FULL = 4000  # cap for reflex-guard: full atoms (composite checklists)


def _load_ruleset() -> dict:
    if RULESET.exists():
        try:
            return json.loads(RULESET.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"version": 1, "rules": []}


def _guard_text(body: str, mode: str = "") -> str:
    """First paragraph of the atom body = what the model is told when the arc fires.
    `reflex-guard: full` carries the whole body (composite checklists need it)."""
    if mode.lower() == "full":
        return body.strip()[:GUARD_MAX_FULL]
    first = body.split("\n\n", 1)[0].strip()
    return first[:GUARD_MAX]


"""NOTE — why `tier: scope` carries NO auto-derived cwd regex.

The first cut of this change derived a path regex from the compile --root. It worked for
the simple case and was WRONG at the estate boundary: `<engine-checkout>` (the engine
repo) failed an `ECHELON`-anchored fence, yet `resolve_scope()` maps it to scope `echelon`.
A regex fence is a second, weaker source of truth that disagrees with the canonical
resolver — exactly the "two sources of truth" defect the discipline canon warns about.

So scope-tier isolation is enforced in the HOOK by comparing the session's RESOLVED SCOPE
against the rule's scope, using the same resolver the rest of the engine uses. `reflex-cwd`
stays available as a hand-authored NARROWING lever (sub-estate paths), never as the
mechanism of estate isolation.
"""


def compile_reflexes(root: Path, scope: str) -> tuple[list[dict], list[str]]:
    """Scan root for reflex-flagged atoms; return (compiled rules, skip reports)."""
    rules: list[dict] = []
    skipped: list[str] = []
    for md in sorted(root.rglob("*.md")):
        if md.name.upper() == "MEMORY.MD":
            continue
        fm, body = parse_atom(md.read_text(encoding="utf-8", errors="replace"))
        if fm.get("metadata.reflex", "").lower() not in ("true", "yes", "1"):
            continue
        name = fm.get("name") or md.stem
        event = fm.get("metadata.reflex-event", "PreToolUse")
        match = fm.get("metadata.reflex-match", "")
        action = fm.get("metadata.reflex-action", "warn").lower()
        if event not in EVENTS:
            skipped.append(f"{name}: unknown reflex-event '{event}' (allowed: {sorted(EVENTS)})")
            continue
        if action not in ACTIONS:
            skipped.append(f"{name}: unknown reflex-action '{action}' (allowed: warn|block)")
            continue
        if not match:
            skipped.append(f"{name}: missing reflex-match (a reflex without a stimulus pattern cannot fire)")
            continue
        try:
            re.compile(match)
        except re.error as e:
            skipped.append(f"{name}: reflex-match does not compile: {e}")
            continue
        tier = fm.get("metadata.reflex-tier", DEFAULT_TIER).strip().lower() or DEFAULT_TIER
        if tier not in TIERS:
            skipped.append(f"{name}: unknown reflex-tier '{tier}' (allowed: {sorted(TIERS)})")
            continue
        cwd = fm.get("metadata.reflex-cwd", "")
        if cwd:
            try:
                re.compile(cwd)
            except re.error as e:
                skipped.append(f"{name}: reflex-cwd does not compile: {e}")
                continue
        # tier 'scope' needs no derived fence — the hook isolates by resolved scope
        # (see the NOTE above: a path regex disagrees with resolve_scope at the boundary).
        guard = _guard_text(body, fm.get("metadata.reflex-guard", ""))
        if guard.lstrip().startswith("<!--"):
            # the ddl-atom near-miss 2026-07-30: an annotation comment sitting as the
            # body's first paragraph BECOMES the guard on recompile — corrupt, not fatal
            skipped.append(f"{name}: WARN guard text starts with an HTML comment — move the "
                           "annotation below the first paragraph (the first paragraph IS the guard)")
        if tier in ("global", "portable"):
            # Declaring global is an ASSERTION of estate-independence. A pattern naming a
            # concrete path/host/service user is estate-shaped no matter what it declares —
            # warn loudly rather than silently arm it machine-wide. (Warn, not skip: the
            # author may genuinely mean it, and a false skip would drop a real guard.)
            # The generic half (deploy paths, IPv4 literals, drive letters) is built in.
            # Estate-specific project names are DATA: list them in ECHELON_ESTATE_NAMES
            # (comma-separated) and they join the alternation for this check.
            _extra = [re.escape(x.strip())
                      for x in os.environ.get("ECHELON_ESTATE_NAMES", "").split(",")
                      if x.strip()]
            _leak_rx = (r"(srv-captain|/opt/|/srv/|/var/www/|"
                        + ("|".join(_extra) + "|" if _extra else "")
                        + r"[0-9]{1,3}(?:[.][0-9]{1,3}){3}|[A-Za-z]:[\/])")
            leak = re.search(_leak_rx, match)
            if leak:
                skipped.append(f"{name}: WARN tier '{tier}' but reflex-match names something "
                               f"estate-specific ({leak.group(0)!r}) — should this be tier: scope?")
        rule = {
            "name": name,
            "scope": scope,
            "tier": tier,
            "event": event,
            "tool": fm.get("metadata.reflex-tool", ".*"),
            "match": match,
            "action": action,
            "guard": guard,
        }
        if cwd:
            rule["cwd"] = cwd
        # reflex-when: the SESSION-FACTS predicate (2026-08-15). Validated at COMPILE time so
        # a typo is reported here rather than silently failing open on every tool call forever.
        when = fm.get("metadata.reflex-when", "").strip()
        if when:
            bad = [c for c in re.split(r"\s+and\s+", when) if not _WHEN_RE.match(c)]
            if bad:
                skipped.append(f"{name}: WARN reflex-when clause(s) {bad} unparseable "
                               f"(grammar: '<fact> <op> <int>' joined by 'and') — will FAIL OPEN")
            rule["when"] = when
        # worktype: (charter Part 4) — the room TYPES this reflex is armed for. Absent or
        # `all` = fires in every room (so untagged atoms keep firing); a list narrows it.
        wt = (fm.get("metadata.worktype") or fm.get("worktype") or "").strip().lower()
        if wt and wt != "all":
            rule["worktype"] = [t for t in re.split(r"[,\s]+", wt) if t]
        rules.append(rule)
    return rules, skipped


# ── THE SHARED MATCHER — one decision path for the hook and for `reflex test` ────────────
# WHY THIS IS SHARED (2026-08-15): step 6 of /wrap says "sanity-test the regex against a
# real command string", and that was done with ad-hoc echo/pipe. An ad-hoc test re-derives
# the firing rule by hand and therefore tests a FICTION — it checks `re.search(match)` while
# the hook ALSO gates on tier/scope, cwd, and a `fullmatch` on the tool name. A reflex can
# pass the hand test and never fire (the 140-of-141 unfenced-rule audit was exactly this
# class: the scope was on the object and absent from the decision). So the hook's decision
# lives HERE, and echelon_reflex.py calls it. Two callers, one law.
_WHEN_RE = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*(==|!=|>=|<=|>|<)\s*(-?\d+)\s*$")


def eval_when(expr: str, facts: dict | None) -> tuple[bool, str]:
    """Evaluate a `reflex-when:` predicate against the SESSION FACTS.

    Grammar is deliberately tiny — `<fact> <op> <int>` (e.g. `workers_dispatched > 0`), with
    `and` to join clauses. No eval(), no arbitrary expressions: this runs inside a PreToolUse
    hook on every tool call, and a reflex ruleset is data that must never become executable.

    FAIL OPEN (the nerve's severability law, and the owner's explicit call 2026-08-15):
    unknown facts (None) or an unparseable predicate return True — the guard still fires.
    A spurious warning is recoverable; a guard that silently disappears because a file was
    missing is the exact failure mode reflexes exist to prevent."""
    if not expr:
        return True, ""
    if facts is None:
        return True, "session facts unavailable — firing anyway (fail open)"
    for clause in re.split(r"\s+and\s+", expr.strip()):
        m = _WHEN_RE.match(clause)
        if not m:
            return True, f"unparseable reflex-when clause {clause!r} — firing anyway (fail open)"
        key, op, num = m.group(1), m.group(2), int(m.group(3))
        val = facts.get(key)
        if val is None:
            val = 0            # an unrecorded counter is genuinely zero, not unknown
        try:
            val = int(val)
        except (TypeError, ValueError):
            return True, f"fact {key!r} is not numeric — firing anyway (fail open)"
        ok = {"==": val == num, "!=": val != num, ">": val > num,
              "<": val < num, ">=": val >= num, "<=": val <= num}[op]
        if not ok:
            return False, f"reflex-when unmet: {key}={val} fails {clause.strip()!r}"
    return True, ""


def room_gates(rule: dict, room_type: str = "",
               snoozed: tuple[str, ...] | list[str] = ()) -> tuple[bool, str]:
    """The two ROOM gates (charter Part 4), shared by rule_fires and `type show` so the
    survey can never disagree with the hook: a rule carrying `worktype` is armed only in
    rooms of those types (unknown room type = armed, fail OPEN); a live `type snooze`
    mutes by bare name or <scope>:<name>."""
    types = rule.get("worktype")
    if types and room_type and room_type.strip().lower() not in [t.lower() for t in types]:
        return False, f"worktype {types}: not armed for room type '{room_type}'"
    name = rule.get("name", "")
    if snoozed and (name in snoozed or f"{rule.get('scope', '')}:{name}" in snoozed):
        return False, f"snoozed: «{rule.get('scope', '')}:{name}» is muted by `type snooze`"
    return True, ""


def rule_fires(rule: dict, tool: str, payload: str, sess_scope: str = "", cwd: str = "",
               facts: dict | None = None, room_type: str = "",
               snoozed: tuple[str, ...] | list[str] = ()) -> tuple[bool, str]:
    """Would `rule` fire for this event? Returns (fired, reason-it-did-not).

    Mirrors hooks/echelon_reflex.py exactly: tier gate (scope-tier fires only in its own
    estate; an unresolvable scope fires anyway — fail OPEN), the room TYPE gate (a rule
    with `worktype` fires only in rooms of those types; an unknown room type fires anyway
    — fail OPEN), the snooze gate (a live `type snooze` mutes by name), optional
    hand-authored cwd narrowing, tool fullmatch, payload search, and finally the optional
    `reflex-when` SESSION-FACTS predicate — the condition gate that a regex over the
    command string cannot express. A bad pattern severs that one arc."""
    tier = rule.get("tier", DEFAULT_TIER)
    if tier not in ("global", "portable"):
        if sess_scope and (rule.get("scope", "") or "").strip().lower() != sess_scope.strip().lower():
            return False, f"tier '{tier}': rule is estate-local to '{rule.get('scope')}', session scope is '{sess_scope}'"
    ok, why = room_gates(rule, room_type, snoozed)
    if not ok:
        return False, why
    try:
        if rule.get("cwd") and not re.search(rule["cwd"], cwd):
            return False, f"reflex-cwd {rule['cwd']!r} does not match cwd {cwd!r}"
        if not re.fullmatch(rule.get("tool", ".*"), tool):
            return False, f"reflex-tool {rule.get('tool', '.*')!r} does not fullmatch tool {tool!r}"
        if not re.search(rule["match"], payload):
            return False, "reflex-match does not match the payload"
    except re.error as e:
        return False, f"pattern error (arc severed): {e}"
    if rule.get("when"):
        ok, why = eval_when(rule["when"], facts)
        if not ok:
            return False, why
    return True, ""


def _payload_for(tool: str, command: str) -> str:
    """Serialize a command the way the harness serializes tool_input, so a test string is
    matched against the SAME text the hook sees (json.dumps of the tool_input dict)."""
    key = {"Bash": "command", "Read": "file_path", "Edit": "file_path", "Write": "file_path"}.get(tool, "command")
    return json.dumps({key: command}, ensure_ascii=False)


def test_reflex(rules: list[dict], slug: str, fire: list[str], quiet: list[str],
                tool: str = "Bash", scope: str = "", cwd: str = "") -> dict:
    """Assert a compiled reflex's behaviour: every --fire string MUST match, every --quiet
    string MUST NOT. Returns a result dict; the caller exits non-zero on failure.

    THE TEETH (owner's bar, step 6): "a reflex that doesn't fire on its own trap is not
    banked — it's decoration." This turns that from a prose claim into an exit code."""
    matches = [r for r in rules if r.get("name") == slug
               or f"{r.get('scope')}:{r.get('name')}" == slug]
    if not matches:
        near = [f"{r.get('scope')}:{r.get('name')}" for r in rules if slug.lower() in (r.get("name", "").lower())]
        return {"ok": False, "reason": f"no compiled reflex named {slug!r}"
                + (f" — did you mean: {', '.join(near[:5])}?" if near else
                   " (run `echelon reflex compile` first — an uncompiled atom cannot fire)")}
    if len(matches) > 1:
        return {"ok": False, "reason": f"{slug!r} is ambiguous across scopes: "
                + ", ".join(f"{r['scope']}:{r['name']}" for r in matches) + " — qualify it as <scope>:<name>"}
    rule = matches[0]
    sess_scope = scope or rule.get("scope", "")
    checks = []
    # The room gates are deliberately OPEN here (a teeth-test asserts the trigger, not the
    # room) — but a rule that would be hidden where the test runs must say so, or it
    # passes a test it would fail in the room (gate S-1).
    notes = []
    if rule.get("worktype"):
        try:
            from echelon_engine.worktype import session_gate
            rtype, snoozed = session_gate(cwd or None)
        except Exception:
            rtype, snoozed = "", ()
        ok, why = room_gates(rule, rtype, snoozed)
        notes.append(f"this rule carries worktype {rule['worktype']}; "
                     + (f"HIDDEN in this room (type '{rtype}'): {why}" if not ok
                        else f"armed in this room (type '{rtype or '?'}')"))
    for s in fire:
        fired, why = rule_fires(rule, tool, _payload_for(tool, s), sess_scope, cwd)
        checks.append({"kind": "fire", "input": s, "fired": fired, "pass": fired, "why": why})
    for s in quiet:
        fired, why = rule_fires(rule, tool, _payload_for(tool, s), sess_scope, cwd)
        checks.append({"kind": "quiet", "input": s, "fired": fired, "pass": not fired,
                       "why": "FIRED but should have stayed quiet" if fired else ""})
    return {"ok": all(c["pass"] for c in checks), "rule": rule, "checks": checks, "notes": notes}


def _main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="echelon reflex",
                                 description="compile reflex-flagged atoms into the body's ruleset")
    sub = ap.add_subparsers(dest="verb", required=True)
    c = sub.add_parser("compile", help="scan a memory dir, replace this scope's rules in ~/.echelon/reflexes.json")
    c.add_argument("--root", required=True, help="memory dir (or repo root) holding the atom .md files")
    c.add_argument("--scope", required=True)
    c.add_argument("--force", action="store_true",
                   help="override the count-drop guard (when new < 50%% of old — the two-roots trap)")
    sub.add_parser("list", help="show the compiled ruleset the hooks fire from")
    t = sub.add_parser("test", help="ASSERT a compiled reflex fires on its trap and stays quiet otherwise")
    t.add_argument("slug", help="the reflex name (or <scope>:<name> if ambiguous)")
    t.add_argument("--fire", action="append", default=[], metavar="CMD",
                   help="a command that MUST fire this reflex (repeatable)")
    t.add_argument("--quiet", action="append", default=[], metavar="CMD",
                   help="a command that must NOT fire it (repeatable) — the false-positive guard")
    t.add_argument("--tool", default="Bash", help="tool name the event carries (default: Bash)")
    t.add_argument("--scope", default="", help="simulate the session scope (default: the rule's own scope)")
    t.add_argument("--cwd", default="", help="simulate the session cwd (only needed for reflex-cwd rules)")
    a = ap.parse_args(argv)

    ruleset = _load_ruleset()
    if a.verb == "list":
        if not ruleset["rules"]:
            print("reflex ruleset: EMPTY — flag an atom (metadata.reflex: true) and run reflex compile")
            return 0
        by_tier: dict[str, int] = {}
        for r in ruleset["rules"]:
            teeth = "BLOCK" if r["action"] == "block" else "warn"
            tier = r.get("tier", DEFAULT_TIER)
            by_tier[tier] = by_tier.get(tier, 0) + 1
            narrow = f"  cwd~{r['cwd']}" if r.get("cwd") else ""
            print(f"[{teeth:5}] {tier:8} {r['event']:16} tool~{r['tool']:12} «{r['scope']}» {r['name']}{narrow}\n"
                  f"        stimulus: {r['match']}")
        print(f"\n  {len(ruleset['rules'])} rule(s): "
              + ", ".join(f"{n} {t}" for t, n in sorted(by_tier.items())))
        untiered = [r for r in ruleset["rules"] if not r.get("tier")]
        if untiered:
            print(f"  NOTE: {len(untiered)} rule(s) predate the tier field and are treated as "
                  f"'{DEFAULT_TIER}' (estate-local). Recompile their scopes to make it explicit.")
        return 0

    if a.verb == "test":
        if not a.fire and not a.quiet:
            print("reflex test: give at least one --fire (the trap) — a test with no assertion proves nothing")
            return 2
        res = test_reflex(ruleset["rules"], a.slug, a.fire, a.quiet,
                          tool=a.tool, scope=a.scope, cwd=a.cwd)
        if not res.get("ok") and "checks" not in res:
            print(f"reflex test: {res['reason']}")
            return 2
        r = res["rule"]
        teeth = "BLOCK" if r["action"] == "block" else "warn"
        print(f"reflex test: «{r['scope']}:{r['name']}»  [{teeth}] tier={r.get('tier', DEFAULT_TIER)} "
              f"tool~{r['tool']}\n  stimulus: {r['match']}")
        for n in res.get("notes", []):
            print(f"  note: {n}")
        for c in res["checks"]:
            mark = "PASS" if c["pass"] else "FAIL"
            want = "fires" if c["kind"] == "fire" else "quiet"
            print(f"  [{mark}] {want:5} <- {c['input'][:90]}")
            if not c["pass"] and c["why"]:
                print(f"         why: {c['why']}")
        n_fail = sum(1 for c in res["checks"] if not c["pass"])
        if n_fail:
            print(f"\n  {n_fail} assertion(s) FAILED — this reflex is decoration, not a guard.")
            return 1
        print(f"\n  {len(res['checks'])} assertion(s) passed — the arc fires on its trap and stays quiet otherwise.")
        return 0

    root = Path(a.root)
    if not root.is_dir():
        print(f"reflex compile: not a directory: {root}")
        return 2
    rules, skipped = compile_reflexes(root, a.scope)
    kept = [r for r in ruleset["rules"] if r.get("scope") != a.scope]
    old_count = len([r for r in ruleset["rules"] if r.get("scope") == a.scope])

    # WRITE-TIME INVARIANT (2026-07-31): the TWO-ROOTS TRAP — compile REPLACES a scope's
    # rules from ONE --root. The estate has TWO roots (repo memory/ + .claude projects estate);
    # compiling from the wrong/partial root silently shrinks the reflex set. Guard: if the new
    # count is < 50% of the existing count, abort with the two-roots explanation. --force overrides.
    if old_count > 0 and len(rules) < old_count * 0.5 and not a.force:
        print(f"reflex compile: ABORTED — count-drop guard triggered (the TWO-ROOTS TRAP)")
        print(f"  old: {old_count} rule(s) for scope '{a.scope}'")
        print(f"  new: {len(rules)} rule(s) from --root {root}")
        print(f"  The estate has TWO roots (repo memory/ + .claude projects estate dir).")
        print(f"  Compiling from only one root silently DROPS the other root's reflexes.")
        print(f"  If this drop is intentional (e.g. you ARE removing rules), re-run with --force.")
        print(f"  Otherwise, compile from the correct root or both roots.")
        return 1

    ruleset["rules"] = kept + rules
    RULESET.parent.mkdir(parents=True, exist_ok=True)
    RULESET.write_text(json.dumps(ruleset, indent=1), encoding="utf-8")
    tier_counts: dict[str, int] = {}
    for r in rules:
        t = r.get("tier", DEFAULT_TIER)
        tier_counts[t] = tier_counts.get(t, 0) + 1
    tier_note = ", ".join(f"{n} {t}" for t, n in sorted(tier_counts.items())) or "none"
    print(f"reflex compile: {len(rules)} arc(s) for scope '{a.scope}' "
          f"(was {old_count}, {len(kept)} kept from other scopes) -> {RULESET}")
    print(f"  tiers: {tier_note}")
    n_scope = tier_counts.get("scope", 0)
    if n_scope:
        print(f"  {n_scope} scope-tier rule(s) fire ONLY in estate '{a.scope}' (resolved by the hook)")
    n_wide = tier_counts.get("global", 0) + tier_counts.get("portable", 0)
    if n_wide:
        print(f"  {n_wide} rule(s) declared machine-wide (global/portable) — these fire in EVERY estate")
    for s in skipped:
        print(f"  SKIP {s}")
    return 0


main = _main
