"""echelon xray — the project-comprehension organ (audit a repo until you UNDERSTAND it).

THE ASK (owner, 2026-07-09): "one special command, where it will audit the project to know
what the project is about, the user or customer who is using it, what kind of UI design and
system it needs — and so on. A complete and powerful pipeline/flow/loop."

THE PIPELINE (five phases, each feeding the next):

  0. GROUND   ($0, deterministic)  walk the repo: manifests, languages, frameworks, routes,
              data layer, UI surface, tests, git pulse → a bounded DOSSIER (no LLM).
  1. COMPREHEND (1 cheap call)     what IS this: purpose, domain, product type, maturity —
              grounded in the dossier + README, evidence-cited.
  2. LENSES   (parallel, equipped) each lens = a CARTRIDGE (pm/ux/architect/...) + a focused
              question, run concurrently. pm → who uses it & JTBD; ux → the UI design system
              it needs; architect → the system it needs. --deep adds engagement/qa/security/ops.
  3. GAP LOOP (the loop)           a critic reads all lens output and asks "what stayed
              unanswered that the repo could answer?" → names PROBE FILES → the engine reads
              them (bounded) → re-runs the starved lenses with the new evidence. Repeats
              --loops times or until no probes remain. Completeness is earned, not assumed.
  4. VERDICT  (1 strong call)      a chair synthesizes everything into the dossier the owner
              actually wanted: WHAT / WHO / UI-NEEDS / SYSTEM-NEEDS / GAPS / ROADMAP.
              Written to <repo>/.echelon/xray/XRAY.md (+ xray.json, machine-readable).
  5. PLANT    (--plant, optional)  distill the verdict into atoms in <repo>/memory/ and
              ingest them — the audit ends as WEIGHT in the bank, not a dead report.

Reuses the swarm organs: swarm.dispatch.send (provider ladder), SwarmContext (cartridge-
equipped cache-stable prompts). Layer: engine top-level organ (like summon/chat).

  echelon xray                       # audit the cwd (pm+ux+architect, 1 gap loop)
  echelon xray <estate-root>/THETA-DASH --deep # all seven lenses, 2 gap loops
  echelon xray --lenses ux --loops 0 # one cheap lens, no loop
  echelon xray --plant               # and plant the verdict as atoms in the bank
"""
from __future__ import annotations

import json
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

# ── bounds (keep every prompt honest about size) ─────────────────────────────
_DIGEST_CAP = 9_000        # chars of ground dossier fed to any seat
_README_CAP = 3_000
_MANIFEST_CAP = 2_000      # per manifest excerpt
_PROBE_FILE_CAP = 6_000    # chars per probe file read in the gap loop
_PROBES_PER_ROUND = 6
_SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build",
              ".next", ".nuxt", "vendor", ".flux", "coverage", ".idea", ".vscode",
              "target", "bin", "obj", ".pytest_cache", ".mypy_cache", ".echelon"}

# card keys the atlas vocabulary allows (shared by _emit_atlas and blueprint's LOCK);
# fields/form_rules/importance/disclosure/group are the skeleton generator's contract keys.
_CARD_KEYS = {"id", "type", "title", "what_it_is", "role", "part_of", "status", "stack",
              "entrypoints", "source", "endpoints", "data_source", "reads_fields",
              "writes", "links_to", "canonical_for", "decision", "gap_reason", "confidence",
              "fields", "form_rules", "importance", "disclosure", "group"}
# Windows device names are reserved with ANY extension — con.json breaks the final write.
_WIN_RESERVED = {"con", "prn", "aux", "nul",
                 *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))}


def _safe_card_id(raw) -> str:
    """Kebab-sanitize a model-supplied card id into a safe filename stem ('' = unusable)."""
    cid = re.sub(r"[^a-z0-9-]", "-", str(raw or "").lower()).strip("-")
    if cid and cid.split("-")[0] in _WIN_RESERVED:
        cid = "x-" + cid
    return cid


_MANIFESTS = ["package.json", "pyproject.toml", "requirements.txt", "composer.json",
              "go.mod", "Cargo.toml", "pom.xml", "Gemfile", "mix.exs", "build.gradle",
              "Dockerfile", "docker-compose.yml", "docker-compose.yaml", ".env.example"]

_LANG_EXT = {".py": "python", ".js": "javascript", ".ts": "typescript", ".tsx": "typescript",
             ".jsx": "javascript", ".php": "php", ".go": "go", ".rs": "rust", ".java": "java",
             ".rb": "ruby", ".cs": "csharp", ".vue": "vue", ".svelte": "svelte",
             ".html": "html", ".css": "css", ".scss": "css", ".sql": "sql", ".kt": "kotlin",
             ".swift": "swift", ".dart": "dart"}

# signal-dir name → what it tells us
_SIGNAL_DIRS = {
    "routes": "routing", "pages": "page-ui", "views": "page-ui", "screens": "page-ui",
    "components": "component-ui", "controllers": "mvc-backend", "models": "data-models",
    "migrations": "db-migrations", "prisma": "db-prisma", "api": "api-layer",
    "tests": "tests", "test": "tests", "__tests__": "tests", "spec": "tests",
    "docs": "docs", "public": "static-assets", "static": "static-assets",
    "templates": "server-templates", "middleware": "middleware", "hooks": "react-hooks",
    "store": "state-store", "services": "service-layer", "jobs": "background-jobs",
    "workers": "background-jobs", "lambda": "serverless", "functions": "serverless",
}


# ══ PHASE 0 — GROUND ══════════════════════════════════════════════════════════

def ground_scan(root: Path) -> dict:
    """Deterministic repo census — no LLM, bounded output. The evidence floor every
    later phase stands on (a lens that reasons off the real file tree can't invent
    a Rails app inside a Vite repo)."""
    langs: dict[str, int] = {}
    signals: dict[str, int] = {}
    manifests: dict[str, str] = {}
    total_files = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS and not d.startswith(".")]
        rel_dir = Path(dirpath).name.lower()
        if rel_dir in _SIGNAL_DIRS:
            signals[_SIGNAL_DIRS[rel_dir]] = signals.get(_SIGNAL_DIRS[rel_dir], 0) + len(filenames)
        for fn in filenames:
            total_files += 1
            ext = Path(fn).suffix.lower()
            if ext in _LANG_EXT:
                langs[_LANG_EXT[ext]] = langs.get(_LANG_EXT[ext], 0) + 1
            if fn in _MANIFESTS and fn not in manifests:
                try:
                    manifests[fn] = (Path(dirpath) / fn).read_text(
                        encoding="utf-8", errors="replace")[:_MANIFEST_CAP]
                except OSError:
                    pass

    readme = ""
    for cand in ("README.md", "readme.md", "README.rst", "README.txt", "README"):
        p = root / cand
        if p.exists():
            try:
                readme = p.read_text(encoding="utf-8", errors="replace")[:_README_CAP]
            except OSError:
                pass
            break

    git = {}
    try:
        import subprocess
        r = subprocess.run(["git", "log", "--oneline", "-10"], cwd=root,
                           capture_output=True, text=True, timeout=10)
        if r.returncode == 0:
            git["recent_commits"] = r.stdout.strip()
        r2 = subprocess.run(["git", "log", "-1", "--format=%ci"], cwd=root,
                            capture_output=True, text=True, timeout=10)
        if r2.returncode == 0:
            git["last_commit"] = r2.stdout.strip()
    except Exception:
        pass

    # top-level layout (one level, names only — the shape of the house)
    try:
        top = sorted(p.name + ("/" if p.is_dir() else "")
                     for p in root.iterdir()
                     if p.name not in _SKIP_DIRS and not p.name.startswith("."))[:60]
    except OSError:
        top = []

    return {"root": str(root), "total_files": total_files,
            "languages": dict(sorted(langs.items(), key=lambda kv: -kv[1])),
            "signals": dict(sorted(signals.items(), key=lambda kv: -kv[1])),
            "manifests": manifests, "readme": readme, "git": git, "top_level": top}


def ground_digest(g: dict) -> str:
    """The bounded text form of the ground scan that rides in every prompt."""
    parts = [f"REPO: {g['root']}  ({g['total_files']} files)"]
    if g["top_level"]:
        parts.append("TOP-LEVEL: " + "  ".join(g["top_level"]))
    if g["languages"]:
        parts.append("LANGUAGES (file counts): " +
                      ", ".join(f"{k}={v}" for k, v in list(g["languages"].items())[:10]))
    if g["signals"]:
        parts.append("STRUCTURE SIGNALS: " +
                      ", ".join(f"{k}({v})" for k, v in g["signals"].items()))
    if g["git"]:
        parts.append("GIT: last commit " + g["git"].get("last_commit", "?") +
                      "\n  " + g["git"].get("recent_commits", "").replace("\n", "\n  "))
    for name, body in g["manifests"].items():
        parts.append(f"── MANIFEST {name} ──\n{body}")
    if g["readme"]:
        parts.append(f"── README (excerpt) ──\n{g['readme']}")
    return "\n\n".join(parts)[:_DIGEST_CAP]


# ══ PHASE 0b — THE ATLAS LADDER (e-atlas / c-atlas, married into xray) ═════════
# A c-atlas (docs/c-atlas: define/<id>.json cards + keys.schema.json,
# the model ported from ECHELON's atlas) is a repo SELF-MAP: every component typed, with
# data lineage (endpoints/data_source/reads_fields) and nav relations. When a repo
# carries one, it is FAR richer ground than a file census — so xray auto-detects it and
# feeds the inventory to every lens. When a repo lacks one, --atlas makes xray PROPOSE
# one (the cartographer seat) so the project starts self-mapping.

# THE ATLAS LADDER (owner's naming, 2026-07-09): E-ATLAS (estate altitude — nodes are
# whole systems/services, define/ at the repo top) → C-ATLAS (component altitude —
# pages/components, usually docs/c-atlas) → ATOMS (memory altitude, parked as slug@hash
# on cards). "." first: an atlas repo ITSELF is an e-atlas — xray audits the PORTFOLIO.
# Legacy dir names (component-atlas) stay detected for back-compat.
_ATLAS_DIR_CANDIDATES = [".", "docs/c-atlas", "c-atlas", "e-atlas",
                         "docs/component-atlas", "component-atlas", "docs/atlas", "atlas"]
_ATLAS_INV_CAP = 6_000


def atlas_scan(root: Path) -> dict | None:
    """Find and inventory a c-atlas (or e-atlas) in the repo. None if absent."""
    atlas_dir = None
    for cand in _ATLAS_DIR_CANDIDATES:
        d = (root / cand).resolve() if cand != "." else root
        if (d / "define" / "_meta.json").is_file() or (
                (d / "define").is_dir() and (d / "keys.schema.json").is_file()):
            atlas_dir = d
            break
    if atlas_dir is None:
        return None
    meta = {}
    try:
        meta = json.loads((atlas_dir / "define" / "_meta.json").read_text(encoding="utf-8"))
    except Exception:
        pass
    cards = []
    for f in sorted((atlas_dir / "define").glob("*.json")):
        if f.name == "_meta.json":
            continue
        try:
            c = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        deps = c.get("depends_on") or []
        dep_names = [str(d.get("node", d.get("id", "?"))) if isinstance(d, dict) else str(d)
                     for d in deps]
        cards.append({"id": f.stem, "type": c.get("type", "?"),
                      "title": c.get("title", ""), "status": c.get("status", ""),
                      "part_of": c.get("part_of", ""),
                      "canonical_for": c.get("canonical_for", ""),
                      "data_source": c.get("data_source", ""),
                      "endpoints": len(c.get("endpoints") or []),
                      "gap_reason": c.get("gap_reason", ""),
                      "depends_on": dep_names,
                      "port": c.get("port", ""),
                      "public_url": c.get("public_url", "")})
    # ESTATE altitude: the atlas IS the target repo and its nodes are systems/servers,
    # not page components — the e-atlas, one altitude above a c-atlas.
    types = {c["type"] for c in cards}
    estate = (atlas_dir == root and bool(types & {"system", "server", "repo"})
              and not (types & {"component", "page"}))
    # a proposal is a real, usable atlas (blueprint anchors on it) but every card is
    # confidence:'inferred' — surfaced so downstream can note it's unverified.
    proposal = str(meta.get("status", "")).lower() == "proposal"
    return {"dir": str(atlas_dir), "meta": meta, "cards": cards, "estate": estate,
            "proposal": proposal}


def atlas_digest(a: dict) -> str:
    """One line per card — the inventory every lens reasons from. At ESTATE altitude the
    depends_on edges ride along (they are the blast-radius graph of the portfolio)."""
    kind = "E-ATLAS (estate altitude — nodes are whole SYSTEMS/services; audit the " \
           "PORTFOLIO: duplicated capabilities, unsurfaced data, dependency blast-radius)" \
           if a.get("estate") else "C-ATLAS (component altitude — the repo's self-map)"
    lines = [f"{kind} at {a['dir']} ({len(a['cards'])} nodes). "
             f"Laws: absence of a key = UNKNOWN (never infer); status live/partial/"
             f"empty_state(gap_reason)/planned; canonical_for collisions = duplicate "
             f"surfaces to resolve."]
    for c in a["cards"]:
        line = f"  [{c['type']}] {c['id']}: {c['title']}"
        bits = [b for b in (
            f"status={c['status']}" if c['status'] else "",
            f"part_of={c['part_of']}" if c['part_of'] else "",
            f"data={c['data_source']}" if c['data_source'] else "",
            f"endpoints={c['endpoints']}" if c['endpoints'] else "",
            f"port={c['port']}" if c['port'] else "",
            f"url={c['public_url']}" if c['public_url'] else "",
            f"depends_on={','.join(c['depends_on'])}" if c['depends_on'] else "",
            f"canonical_for={c['canonical_for']}" if c['canonical_for'] else "",
            f"GAP:{c['gap_reason']}" if c['gap_reason'] else "") if b]
        if bits:
            line += "  (" + ", ".join(bits) + ")"
        lines.append(line)
    return "\n".join(lines)[:_ATLAS_INV_CAP]


# ══ LLM plumbing (reuses swarm organs) ════════════════════════════════════════

def _extract_json(text: str):
    """Pull the first ```json fence (or bare object/array) out of a model reply."""
    m = re.search(r"```json\s*([\[{].*?[\]}])\s*```", text, re.DOTALL)
    raw = m.group(1) if m else None
    if raw is None:
        m = re.search(r"([\[{].*[\]}])", text, re.DOTALL)
        raw = m.group(1) if m else None
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except Exception:
        return None


def _extract_json_tail(text: str, prefer_key: str | None = None):
    """Chair-style extraction: the machine payload rides AFTER the report, but the report
    body may legitimately contain earlier ```json example fences (the chair prompt itself
    shows the model examples, which it echoes). So scan ALL fences and pick the LAST one
    that parses — preferring one that carries `prefer_key` when given."""
    parsed = []
    for m in re.finditer(r"```json\s*([\[{].*?[\]}])\s*```", text, re.DOTALL):
        try:
            parsed.append(json.loads(m.group(1)))
        except Exception:
            continue
    if prefer_key:
        for j in reversed(parsed):
            if isinstance(j, dict) and prefer_key in j:
                return j
    return parsed[-1] if parsed else _extract_json(text)


def _as_dict(j, fallback: dict) -> dict:
    """Model JSON where an OBJECT is the contract — a top-level array is truthy, so
    `_extract_json(...) or {}` doesn't catch it and `.get` crashes downstream."""
    return j if isinstance(j, dict) else fallback


def _ask(prompt: str, provider: str, model: str, label: str, log) -> str:
    """One seat call. A provider exception here must never kill the whole run (a lens
    pool re-raises it and every completed seat's paid output is lost) — degrade to ''
    and let the caller's fallbacks carry it."""
    from echelon_engine.swarm.dispatch import send
    log(f"  → {label} ...")
    try:
        out = send(prompt, provider=provider, model=model) or ""
    except Exception as e:
        log(f"  ✗ {label} FAILED ({type(e).__name__}: {e}) — continuing without it")
        return ""
    log(f"  ← {label} ({len(out)} chars)")
    return out


# ══ PHASE 1 — COMPREHEND ══════════════════════════════════════════════════════

_COMPREHEND_PROMPT = """\
You are auditing a software project to UNDERSTAND it. Below is a deterministic census of
its repository (real file tree, manifests, README). Reason ONLY from this evidence — when
you infer, say what you inferred FROM. Return a ```json fence:

```json
{{
  "purpose": "one paragraph: what this project does and why it exists",
  "domain": "the business/problem domain",
  "product_type": "e.g. dashboard | e-commerce | api-service | cli-tool | cms | mobile-app | library",
  "maturity": "prototype | active-development | production | legacy",
  "stack": {{"frontend": "...", "backend": "...", "data": "...", "deploy": "..."}},
  "evidence": ["what in the census supports each conclusion"],
  "unknowns": ["what the census does NOT reveal that matters"]
}}
```

── REPOSITORY CENSUS ──
{digest}
"""


# ══ PHASE 2 — LENSES ═════════════════════════════════════════════════════════

# lens → (cartridge to equip, the focused question it must answer)
_LENSES: dict[str, tuple[str, str]] = {
    "pm": ("pm",
           "WHO uses this product and WHY. Identify: (1) the user/customer personas — who "
           "they are, their skill level, what device/context they use it in; (2) the "
           "jobs-to-be-done — what job each persona hires this product for; (3) the problem "
           "it solves and what users did BEFORE it; (4) product gaps — jobs the personas "
           "have that the product visibly does not serve yet."),
    "ux": ("ux",
           "What UI DESIGN and DESIGN SYSTEM this project needs. Assess: (1) what UI exists "
           "now (from the census signals) and its likely state; (2) the design language that "
           "fits these users (density, tone, color temperature, motion); (3) the component "
           "inventory it needs (tables? forms? dashboards? wizards?); (4) device/viewport "
           "targets; (5) the states that must be designed (empty/loading/error/success); "
           "(6) the top 5 UX moves that would most improve it."),
    "architect": ("architect",
                  "What SYSTEM this project needs. Assess: (1) the architecture it has now "
                  "(from the census); (2) the architecture its purpose actually requires — "
                  "data flow, integration points, background work, auth; (3) scaling posture "
                  "— what breaks first under 10x load; (4) the gap between (1) and (2) as "
                  "concrete moves, each sized S/M/L."),
    "engagement": ("engagement",
                   "The BUSINESS view. What does the paying customer actually buy here? What "
                   "is in-scope vs scope-creep risk? What requirement is implied but never "
                   "written down? What would a client dispute at delivery time?"),
    "qa": ("qa",
           "The QUALITY posture. What test coverage exists (from census signals)? What are "
           "the highest-risk untested paths given the product type? What test pyramid does "
           "this project need, minimally, to be safe to change?"),
    "security": ("security",
                 "The ATTACK SURFACE. Given the stack and product type: where does user "
                 "input enter? What auth/session posture is implied? What are the 5 most "
                 "likely vulnerabilities for THIS kind of codebase, and where to look?"),
    "ops": ("ops",
            "The OPERATIONS posture. How does this deploy (from manifests)? What breaks "
            "on the way to production — env config, migrations, secrets, monitoring? What "
            "is the minimal ops hardening this project needs?"),
}

_DEFAULT_LENSES = ["pm", "ux", "architect"]
_DEEP_LENSES = ["pm", "ux", "architect", "engagement", "qa", "security", "ops"]


def _lens_prompt(lens: str, digest: str, comprehension: dict, extra_evidence: str) -> str:
    from echelon_engine.swarm.context import SwarmContext
    cart, question = _LENSES[lens]
    ctx = SwarmContext(cart)
    goal = (f"PROJECT AUDIT — the '{lens}' lens.\n{question}\n\n"
            "Ground every finding in the evidence below; cite what you reasoned from. "
            "Findings go in the JSON `findings` array; your direct ANSWER to the lens "
            "question goes in `recommendations` (ordered, concrete) and the prose summary.")
    context = (f"── WHAT THE PROJECT IS (phase-1 comprehension) ──\n"
               f"{json.dumps(comprehension, indent=1)[:2500]}\n\n"
               f"── REPOSITORY CENSUS ──\n{digest}")
    if extra_evidence:
        context += f"\n\n── PROBED EVIDENCE (files read on request) ──\n{extra_evidence}"
    prompt, _ = ctx.build(goal, context)
    return prompt


# ══ PHASE 3 — GAP LOOP ═══════════════════════════════════════════════════════

_CRITIC_PROMPT = """\
You are the COMPLETENESS CRITIC of a project audit. Below: the repo census and every
lens's findings so far. Your one job: what stayed UNANSWERED that the repository itself
could answer? Name the specific files to read (paths must plausibly exist given the
census top-level and signals). Return a ```json fence:

```json
{{
  "unanswered": ["question that matters and is still open"],
  "probe_files": ["relative/path/to/file — max {max_probes}, most informative first"],
  "starved_lenses": ["which lenses ({lenses}) would change their answer given those files"]
}}
```
If nothing material is missing, return empty lists — do not invent work.

── REPOSITORY CENSUS ──
{digest}

── LENS FINDINGS SO FAR ──
{findings}
"""


_SENSITIVE_PROBE = re.compile(
    r"(^\.env|\.pem$|\.key$|^id_rsa|^id_ed25519|^id_ecdsa|secret|credential|password|"
    r"\.p12$|\.pfx$|\.keystore$)", re.IGNORECASE)


def _probe_denied(root: Path, p: Path) -> bool:
    """Probe contents are shipped to the LLM provider in the next round's prompts —
    the critic's paths are model-chosen, so a prompt-injected repo could steer it at
    secrets. Deny dot-prefixed components (the census skips them: nothing there was
    ever offered as evidence) and secret-shaped basenames."""
    rel_parts = p.relative_to(root.resolve()).parts
    if any(part.startswith(".") for part in rel_parts):
        return True
    return bool(_SENSITIVE_PROBE.search(p.name))


def _read_probes(root: Path, paths: list[str], log) -> str:
    """Read the critic's requested files, bounded. Missing paths are reported, not fatal —
    the critic guesses from the census and some guesses miss."""
    chunks = []
    for rel in paths[:_PROBES_PER_ROUND]:
        p = (root / str(rel).strip().lstrip("/\\")).resolve()
        try:
            p.relative_to(root.resolve())  # jail: no escaping the repo
        except ValueError:
            continue
        if p.is_file() and _probe_denied(root, p):
            chunks.append(f"═ FILE {rel} ═ (denied: sensitive or census-skipped path)")
            log(f"    probe DENIED: {rel}")
            continue
        if p.is_file():
            try:
                body = p.read_text(encoding="utf-8", errors="replace")[:_PROBE_FILE_CAP]
                chunks.append(f"═ FILE {rel} ═\n{body}")
                log(f"    probe read: {rel} ({p.stat().st_size} bytes)")
            except OSError as e:
                chunks.append(f"═ FILE {rel} ═ (unreadable: {e})")
        else:
            chunks.append(f"═ FILE {rel} ═ (does not exist)")
            log(f"    probe miss: {rel}")
    return "\n\n".join(chunks)


# ══ PHASE 4 — VERDICT ════════════════════════════════════════════════════════

_CHAIR_PROMPT = """\
You are the CHAIR of a project audit. Below: the repo census, the comprehension pass, and
every lens's findings (pm=who/jtbd, ux=UI needs, architect=system needs, plus any of
engagement/qa/security/ops). Synthesize the FINAL DOSSIER the project owner asked for:
"what the project is about, who the user or customer is, what UI design and system it needs."

Write a complete markdown report with EXACTLY these sections:

# PROJECT X-RAY — <project name>
## 1. WHAT — identity & purpose        (what it is, domain, product type, maturity, stack)
## 2. WHO — users & customers          (personas, their context, jobs-to-be-done)
## 3. UI — the design system it needs  (design language, component inventory, states, devices)
## 4. SYSTEM — the architecture it needs (now vs required, gaps as sized moves)
## 5. GAPS & RISKS                     (ranked; include quality/security/ops if those lenses ran)
## 6. ROADMAP                          (MUST do / SHOULD do / NICE to have — concrete, ordered)
## 7. OPEN QUESTIONS                   (what only the owner can answer)

Rules: ground each claim in the audit evidence; disagreements between lenses get resolved
EXPLICITLY (say which lens you sided with and why); no filler.

Then, AFTER the report, a ```json fence for the machine:
```json
{{"identity": "1-2 sentences", "users": "1-2 sentences", "ui_needs": "1-2 sentences",
  "system_needs": "1-2 sentences", "top_risks": ["..."], "roadmap_must": ["..."]}}
```

── REPOSITORY CENSUS ──
{digest}

── COMPREHENSION ──
{comprehension}

── LENS FINDINGS ──
{findings}

── GAP-LOOP: questions the critic left open ──
{unanswered}
"""


# ══ PHASE 4b — CARTOGRAPHER (--atlas: propose the c-atlas) ═══════════════════

_CARTOGRAPHER_PROMPT = """\
You are the CARTOGRAPHER of a project audit. Your job: propose the C-ATLAS —
the repo's self-map — as typed card stubs, one per node, in the exact vocabulary below.

THE MODEL (the AlphaApp/ECHELON atlas laws):
- One card per node: the root repo (type "repo"), each page/screen (type "page"), each
  component inside a page (type "component"), each backend service/server (type "server").
- ids are kebab-case, globally unique. Every non-root node has "part_of": "[[parent-id]]".
- ABSENCE of a key = UNKNOWN, never a guessed default. Only emit keys you have EVIDENCE
  for from the audit below. status: live | partial | empty_state | planned.
- Allowed keys ONLY: id, type, title, what_it_is, role, part_of, status, stack,
  entrypoints, source, endpoints, data_source, reads_fields, writes, links_to,
  canonical_for, decision, gap_reason, confidence.
- confidence: "confirmed" only if the census/probes showed the file; else "inferred".

{existing_note}

Return ONE ```json fence: an ARRAY of card objects (root first, then pages, then
components). 10–40 cards. Ground each in the audit evidence; prefer fewer honest cards
over many invented ones.

── REPOSITORY CENSUS (+ atlas if present) ──
{digest}

── COMPREHENSION ──
{comprehension}

── LENS FINDINGS (ux = pages/components seen; architect = system nodes) ──
{findings}
"""


def _emit_atlas(define: Path, cards: list, root_name: str, log) -> "tuple[Path, str | None]":
    """Write cartographer cards as <define>/<id>.json stubs + a _meta.json, with the
    structural laws enforced in CODE (kebab ids, reciprocity, allowed keys) — the
    model proposes, the code keeps it honest. `define` is the target define/ dir the
    caller chose (a canonical docs/c-atlas/define on a fresh repo, so blueprint and
    atlas_scan discover it; a .echelon staging dir when a real atlas already exists and
    must not be clobbered). Returns (define_dir, root_card_id)."""
    define.mkdir(parents=True, exist_ok=True)
    from datetime import date
    today = date.today().isoformat()
    by_id: dict[str, dict] = {}
    for c in cards:
        if not isinstance(c, dict):
            continue
        cid = _safe_card_id(c.get("id"))
        if not cid or cid in by_id:
            continue
        card = {k: v for k, v in c.items() if k in _CARD_KEYS and k != "id" and v not in ("", [], None)}
        # normalize part_of to the [[wiki-link]] form the atlas laws require
        if card.get("part_of"):
            pid = str(card["part_of"]).strip("[]")
            card["part_of"] = f"[[{pid}]]"
        card["verified_on"] = today
        card.setdefault("confidence", "inferred")
        by_id[cid] = card
    # reciprocity: parent.contains lists every child that claims part_of it
    for cid, card in by_id.items():
        po = str(card.get("part_of", ""))
        pid = po.strip("[]")
        if pid in by_id:
            by_id[pid].setdefault("contains", [])
            if f"[[{cid}]]" not in by_id[pid]["contains"]:
                by_id[pid]["contains"].append(f"[[{cid}]]")
    for cid, card in by_id.items():
        try:
            (define / f"{cid}.json").write_text(
                json.dumps(card, indent=2, ensure_ascii=False), encoding="utf-8")
        except OSError as e:
            log(f"  cartographer: could not write card '{cid}' ({e}) — skipped")
    # the root card carries the --plant receipts: prefer a parentless repo/system card
    # (any stray parentless card would otherwise steal the root slot)
    root_id = next((cid for cid, c in by_id.items()
                    if not c.get("part_of") and c.get("type") in ("repo", "system")),
                   None) or next((cid for cid, c in by_id.items()
                                  if not c.get("part_of")), None)
    if root_id is None and by_id:
        log("  ⚠ cartographer: no root card (every card claims part_of) — --plant "
            "receipts will have nowhere to park")
    (define / "_meta.json").write_text(json.dumps({
        "atlas": f"{root_name.upper()}-C-ATLAS (xray proposal)",
        "status": "proposal",
        "purpose": "PROPOSED by `echelon xray --atlas` — a starting self-map in the "
                   "ECHELON c-atlas vocabulary. Every card is confidence:'inferred'. "
                   "Verify each against the code and raise confidence to 'confirmed'. "
                   "This proposal is ALREADY at a canonical path, so `echelon blueprint` "
                   "and `xray` auto-detect it as the reuse anchor — no manual promotion "
                   "needed. To harden it into a full contract atlas, add keys.schema.json.",
        "laws": ["Absence of a key = UNKNOWN, never infer.",
                 "structure points both ways (part_of <-> contains).",
                 "collisions (same canonical_for) are flagged, not auto-resolved."],
    }, indent=2), encoding="utf-8")
    log(f"  cartographer: {len(by_id)} card stubs → {define}")
    return define, root_id


# ══ PHASE 5 — PLANT ══════════════════════════════════════════════════════════

def _plant(root: Path, scope: str, verdict_json: dict, log) -> list[str]:
    """Distill the verdict into atoms in the SCOPE-ROOT memory/ and ingest them — the audit
    becomes bank weight the next session recalls, not a report that rots.
    Returns the planted ["slug@hash", ...] (parsed from ingest's receipt lines) so the
    caller can PARK them on atlas cards — the structure↔memory spine.

    SCOPE-LEAK FIX: atoms land in the memory/ of the dir that OWNS `scope` (the repo/estate root),
    never a nested leaf's memory/. Auditing <repo>/web plants into <repo>/memory, not <repo>/web/
    memory — so the scope and its store agree with resolve_scope's ingest canonicalization gate."""
    from echelon_engine.atoms.resolve_scope import resolve_scope_root
    mem = Path(resolve_scope_root(str(root))) / "memory"
    mem.mkdir(exist_ok=True)
    atoms = {
        "xray-what-this-project-is": ("project", verdict_json.get("identity", "")),
        "xray-who-uses-it": ("project", verdict_json.get("users", "")),
        "xray-ui-it-needs": ("project", verdict_json.get("ui_needs", "")),
        "xray-system-it-needs": ("project", verdict_json.get("system_needs", "")),
    }
    from datetime import date
    today = date.today().isoformat()
    written = 0
    for slug, (mtype, body) in atoms.items():
        if not body:
            continue
        risks = verdict_json.get("top_risks") or []
        musts = verdict_json.get("roadmap_must") or []
        extra = ""
        if slug == "xray-system-it-needs" and (risks or musts):
            extra = ("\n\nTop risks: " + "; ".join(risks[:4]) +
                     "\nMust-do next: " + "; ".join(musts[:4]))
        # description is YAML — model text with a newline/colon/quote breaks the
        # frontmatter (or injects keys). json.dumps yields a valid YAML scalar.
        desc = json.dumps(body[:140].replace("\n", " ").replace("\r", " "),
                          ensure_ascii=False)
        (mem / f"{slug}.md").write_text(
            f"---\nname: {slug}\ndescription: {desc}\n"
            f"metadata:\n  type: {mtype}\n---\n\n"
            f"From the `echelon xray` audit of {today}: {body}{extra}\n",
            encoding="utf-8")
        written += 1
    if not written:
        log("  plant: verdict carried no machine JSON — nothing planted")
        return []
    parked: list[str] = []
    try:
        import subprocess
        r = subprocess.run([sys.executable, "-X", "utf8", "-m", "echelon_engine",
                            "ingest", "--root", str(mem), "--scope", scope],
                           capture_output=True, text=True, timeout=120,
                           cwd=str(Path(__file__).resolve().parent.parent))
        # harvest the content-addressed receipts: lines like "  + <slug>  -> <hash>"
        for m in re.finditer(r"[+~]\s+(xray-[\w-]+)\s+->\s+([0-9a-f]{8,})",
                             (r.stdout or "") + (r.stderr or "")):
            parked.append(f"{m.group(1)}@{m.group(2)}")
        log(f"  plant: {written} atoms written to {mem}, ingested into scope '{scope}' "
            f"(rc={r.returncode}, receipts={len(parked)})")
    except Exception as e:
        log(f"  plant: atoms written to {mem} but ingest failed ({e}) — "
            f"run: echelon ingest --root {mem} --scope {scope}")
    return parked


# ══ THE PIPELINE ═════════════════════════════════════════════════════════════

def run_xray(root: Path, lenses: list[str], loops: int, provider: str, model: str,
             chair_model: str, out_dir: Path | None, plant_scope: str | None,
             as_json: bool, want_atlas: bool = False) -> int:
    def log(msg: str) -> None:
        if not as_json:
            print(msg, flush=True)

    log(f"\n══ ECHELON X-RAY ══  {root}")
    log(f"   lenses: {', '.join(lenses)}   gap-loops: {loops}   provider: {provider}")

    # ── 0. GROUND ──
    log("\n[0/4] GROUND — deterministic census ($0)")
    g = ground_scan(root)
    digest = ground_digest(g)
    log(f"  {g['total_files']} files, languages: "
        + ", ".join(list(g['languages'])[:5]) + f", {len(g['manifests'])} manifests")

    # ── 0b. ATLAS LADDER (auto-detected — an e-atlas/c-atlas beats any census) ──
    atlas = atlas_scan(root)
    if atlas:
        log(f"  {'e-atlas' if atlas['estate'] else 'c-atlas'} FOUND: {atlas['dir']} ({len(atlas['cards'])} nodes) — "
            f"feeding the inventory to every lens")
        digest = digest + "\n\n── COMPONENT ATLAS (the repo's own component map) ──\n" \
                 + atlas_digest(atlas)

    # ── 1. COMPREHEND ──
    log("\n[1/4] COMPREHEND — what is this?")
    comp_raw = _ask(_COMPREHEND_PROMPT.format(digest=digest), provider, model,
                    "comprehend", log)
    comprehension = _as_dict(_extract_json(comp_raw), {"purpose": comp_raw[:600]})
    log(f"  → {comprehension.get('product_type', '?')} | "
        f"{comprehension.get('domain', '?')} | {comprehension.get('maturity', '?')}")

    # ── 2. LENSES (parallel) + 3. GAP LOOP ──
    lens_out: dict[str, dict] = {}
    extra_evidence = ""
    unanswered: list[str] = []
    to_run = list(lenses)
    for round_no in range(loops + 1):
        if not to_run:
            break
        log(f"\n[2/4] LENSES — round {round_no + 1}: {', '.join(to_run)}")
        with ThreadPoolExecutor(max_workers=max(1, min(4, len(to_run)))) as pool:
            futs = {pool.submit(
                _ask, _lens_prompt(ln, digest, comprehension, extra_evidence),
                provider, model, f"lens:{ln}", log): ln for ln in to_run}
            for fut in as_completed(futs):
                ln = futs[fut]
                try:
                    raw = fut.result() or ""
                except Exception as e:  # one dead lens must not kill the run
                    log(f"  ✗ lens:{ln} crashed ({type(e).__name__}: {e})")
                    raw = ""
                lens_out[ln] = {"json": _extract_json(raw), "raw": raw}

        if round_no >= loops:
            break
        # critic: what's still unanswered, which files would answer it
        log(f"\n[3/4] GAP LOOP — completeness critic (round {round_no + 1})")
        findings_txt = _findings_digest(lens_out)
        critic_raw = _ask(_CRITIC_PROMPT.format(
            digest=digest, findings=findings_txt,
            max_probes=_PROBES_PER_ROUND, lenses=", ".join(lenses)),
            provider, model, "critic", log)
        critic = _as_dict(_extract_json(critic_raw), {})
        unanswered = critic.get("unanswered") or []
        probes = critic.get("probe_files") or []
        starved = [l for l in (critic.get("starved_lenses") or []) if l in lenses]
        if not probes or not starved:
            log("  critic: nothing material missing — loop closes early")
            break
        evidence = _read_probes(root, probes, log)
        if not evidence.strip():
            break
        extra_evidence = (extra_evidence + "\n\n" + evidence)[-3 * _PROBE_FILE_CAP:]
        to_run = starved

    # ── persist the paid lens work BEFORE the chair (a chair failure must not
    #    discard every completed seat's output) ──
    out = out_dir or (root / ".echelon" / "xray")
    out.mkdir(parents=True, exist_ok=True)
    (out / "xray.partial.json").write_text(json.dumps(
        {"comprehension": comprehension,
         "lenses": {k: v["json"] for k, v in lens_out.items()},
         "unanswered": unanswered}, indent=1, ensure_ascii=False), encoding="utf-8")

    # ── 4. VERDICT ──
    log("\n[4/4] VERDICT — the chair synthesizes")
    chair_raw = _ask(_CHAIR_PROMPT.format(
        digest=digest,
        comprehension=json.dumps(comprehension, indent=1)[:3000],
        findings=_findings_digest(lens_out),
        unanswered="\n".join(f"- {u}" for u in unanswered) or "(none)"),
        provider, chair_model or model, "chair", log)
    verdict_json = _as_dict(_extract_json_tail(chair_raw, prefer_key="identity"), {})
    report_md = re.sub(r"```json\s*[\[{].*?[\]}]\s*```", "", chair_raw,
                       flags=re.DOTALL).strip()
    if not report_md:
        log("  ⚠ chair produced no report — publishing the raw lens findings instead")
        report_md = ("# PROJECT X-RAY (chair failed — raw lens findings)\n\n"
                     + _findings_digest(lens_out))

    # ── 4b. CARTOGRAPHER (--atlas) — propose the repo's component self-map ──
    if want_atlas:
        log("\n[4b] CARTOGRAPHER — proposing the component atlas")
        existing_note = ("An atlas ALREADY EXISTS (see the census). Propose only NEW or "
                         "CHANGED cards — gaps the lenses found, missing components, "
                         "collisions to resolve. Do not restate existing cards."
                         if atlas else
                         "No atlas exists yet — propose the full starting map.")
        carto_raw = _ask(_CARTOGRAPHER_PROMPT.format(
            existing_note=existing_note, digest=digest,
            comprehension=json.dumps(comprehension, indent=1)[:2500],
            findings=_findings_digest(lens_out)),
            provider, chair_model or model, "cartographer", log)
        carto = _extract_json_tail(carto_raw, prefer_key="cards")
        cards = carto if isinstance(carto, list) else (carto or {}).get("cards") or []
        if cards:
            # WHERE the proposal lands is the difference between a usable atlas and a
            # dead-end (the fresh-repo trap): a real atlas already exists → stage the
            # proposed *additions* under .echelon so we never clobber the live contract;
            # NO atlas yet → land it at the canonical docs/c-atlas/define so `blueprint`
            # and `atlas_scan` discover it immediately, no manual `cp` promotion needed.
            if atlas:
                atlas_define_target = out / "atlas-proposal" / "define"
            else:
                atlas_define_target = root / "docs" / "c-atlas" / "define"
            atlas_define, atlas_root_id = _emit_atlas(
                atlas_define_target, cards, root.name, log)
            if not atlas:
                log(f"  → proposal landed at the canonical path {atlas_define.parent} "
                    f"— `echelon blueprint` will auto-detect it (no promotion needed)")
        else:
            log("  cartographer returned no parsable cards — raw saved to atlas-raw.txt")
            (out / "atlas-raw.txt").write_text(carto_raw, encoding="utf-8")
            atlas_define = atlas_root_id = None
    else:
        atlas_define = atlas_root_id = None
    (out / "XRAY.md").write_text(report_md, encoding="utf-8")
    machine = {"ground": {k: v for k, v in g.items() if k != "manifests"},
               "comprehension": comprehension,
               "lenses": {k: v["json"] for k, v in lens_out.items()},
               "unanswered": unanswered, "verdict": verdict_json}
    (out / "xray.json").write_text(json.dumps(machine, indent=1, ensure_ascii=False),
                                   encoding="utf-8")

    # ── 5. PLANT ──
    if plant_scope:
        log("\n[5] PLANT — verdict → atoms → bank")
        parked = _plant(root, plant_scope, verdict_json, log)
        # PARK THE ATOM HASHES ON THE ATLAS (the structure↔memory spine): the root
        # card carries slug@hash receipts — slug is the LIVING pointer (recall/remember
        # it, witnessed), hash is the VERSION receipt (current hash ≠ parked hash =
        # the memory moved on since the card was drawn: free drift detection).
        if parked and atlas_define and atlas_root_id:
            root_card_path = atlas_define / f"{atlas_root_id}.json"
            try:
                rc = json.loads(root_card_path.read_text(encoding="utf-8"))
                rc["atoms"] = parked
                rc["atoms_note"] = ("take up via the witnessed door: echelon remember "
                                    "<slug> — never cat the .md. slug@hash: hash pins "
                                    "the version this card was drawn from.")
                root_card_path.write_text(json.dumps(rc, indent=2, ensure_ascii=False),
                                          encoding="utf-8")
                log(f"  parked {len(parked)} atom hash(es) on root card "
                    f"[[{atlas_root_id}]] — structure↔memory spine linked")
            except Exception as e:
                log(f"  parking failed ({e}) — receipts: {parked}")

    if as_json:
        print(json.dumps(machine, ensure_ascii=False))
    else:
        log(f"\n══ X-RAY COMPLETE ══")
        log(f"  report:  {out / 'XRAY.md'}")
        log(f"  machine: {out / 'xray.json'}")
        for key, label in (("identity", "WHAT"), ("users", "WHO"),
                           ("ui_needs", "UI"), ("system_needs", "SYSTEM")):
            if verdict_json.get(key):
                log(f"  {label:>6}: {verdict_json[key]}")
    return 0


def _findings_digest(lens_out: dict[str, dict]) -> str:
    """Bounded text of every lens's output for critic/chair prompts."""
    parts = []
    for ln, d in lens_out.items():
        j = d.get("json")
        if j:
            parts.append(f"═ LENS {ln} ═\n{json.dumps(j, indent=1, ensure_ascii=False)[:4000]}")
        else:
            parts.append(f"═ LENS {ln} (unstructured) ═\n{d.get('raw', '')[:2500]}")
    return "\n\n".join(parts)


# ══ CLI ══════════════════════════════════════════════════════════════════════

def main(argv: list[str] | None = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(
        prog="echelon xray",
        description="Audit a project until you UNDERSTAND it: what it is, who uses it, "
                    "what UI design and system it needs. Ground scan → comprehension → "
                    "cartridge-equipped lens swarm → gap loop → chair verdict.")
    ap.add_argument("path", nargs="?", default=".", help="project root (default: cwd)")
    ap.add_argument("--deep", action="store_true",
                    help=f"all lenses ({', '.join(_DEEP_LENSES)}) + 2 gap loops")
    ap.add_argument("--lenses", default=None,
                    help=f"comma list from: {', '.join(_LENSES)} "
                         f"(default: {','.join(_DEFAULT_LENSES)})")
    ap.add_argument("--loops", type=int, default=None,
                    help="gap-loop rounds (default: 1; --deep: 2; 0 = no loop)")
    ap.add_argument("--provider", default="auto",
                    help="auto | deepseek | gemini | anthropic (default: auto)")
    ap.add_argument("--model", default="", help="model override for scan/lens seats")
    ap.add_argument("--chair-model", default="",
                    help="stronger model for the final verdict (default: same as --model)")
    ap.add_argument("--out", default=None, help="output dir (default: <repo>/.echelon/xray)")
    ap.add_argument("--atlas", action="store_true",
                    help="propose the repo's c-atlas (define/*.json card stubs in the "
                         "AlphaApp/ECHELON atlas vocabulary; an existing c-atlas or "
                         "e-atlas is auto-detected as evidence either way)")
    ap.add_argument("--plant", action="store_true",
                    help="distill the verdict into atoms in <repo>/memory/ and ingest")
    ap.add_argument("--scope", default=None,
                    help="bank scope for --plant (default: resolve-scope of the target dir — the "
                         "repo/estate that OWNS it, not the kebab'd leaf dir name)")
    ap.add_argument("--json", action="store_true", help="machine output only")
    a = ap.parse_args(argv)

    root = Path(a.path).resolve()
    if not root.is_dir():
        print(f"xray: not a directory: {root}", file=sys.stderr)
        return 1

    if a.lenses:
        lenses = [x.strip() for x in a.lenses.split(",") if x.strip()]
        bad = [x for x in lenses if x not in _LENSES]
        if bad:
            print(f"xray: unknown lens(es): {', '.join(bad)} — "
                  f"choose from {', '.join(_LENSES)}", file=sys.stderr)
            return 2
        if not lenses:
            print(f"xray: --lenses named none — choose from {', '.join(_LENSES)}",
                  file=sys.stderr)
            return 2
    else:
        lenses = list(_DEEP_LENSES if a.deep else _DEFAULT_LENSES)
    loops = max(0, a.loops) if a.loops is not None else (2 if a.deep else 1)

    plant_scope = None
    if a.plant:
        # SCOPE-LEAK FIX: route atoms to the RESOLVED scope (the repo/estate that owns the target
        # dir), NOT the kebab'd leaf dir name. `xray <repo>/hokiemaspool/mirror/pages --plant` used
        # to plant under bogus scope 'pages'; resolve_scope walks up to the owning 'gamma-support'.
        from echelon_engine.atoms.resolve_scope import resolve_scope
        plant_scope = a.scope or resolve_scope(str(root))

    return run_xray(root, lenses, loops, a.provider, a.model, a.chair_model,
                    Path(a.out) if a.out else None, plant_scope, a.json,
                    want_atlas=a.atlas)


if __name__ == "__main__":
    sys.exit(main())
