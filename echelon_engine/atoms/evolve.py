"""evolve — the first machine turn of the Evolution loop (OPEN-0112).

Mines the reflex fire ledger, the compiled ruleset, and room receipts for
HOT (promotion), COLD (lifecycle/demotion) and RECURRENCE (new-trap)
candidates. `scan` is pure read. `demote`/`health` operate a SIDECAR file
(reflex_health.json) that survives `reflex compile` (which replaces
reflexes.json wholesale) -- a habit never becomes law on its own.

`rulings` (OPEN-0113 win 2) is the SAME tool, second source: it mines the
rulings ledger and owner-note board rows for repeated owner law and emits
LAW PROPOSALS with citations. It is read-only like `scan` -- it has NO code
path that opens CLAUDE.md or MEMORY.md for writing, since those are
owner-authored and only the owner declares law; the miner only proposes.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from .. import estate as _estate_cfg

FIRES_DEFAULT = "~/.echelon/reflex_fires.jsonl"
RULES_DEFAULT = "~/.echelon/reflexes.json"
HEALTH_DEFAULT = "~/.echelon/reflex_health.json"

_HEX = re.compile(r"[0-9a-f]{6,}", re.I)
_WORD = re.compile(r"[a-z]+")


def _p(path: str) -> Path:
    return Path(os.path.expanduser(os.path.expandvars(path)))


def _read_fires(path: Path):
    rows = []
    if not path.exists():
        return rows
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except Exception:
                continue
    return rows


def _read_rules(path: Path):
    if not path.exists():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []
    rules = payload.get("rules", payload if isinstance(payload, list) else [])
    return [r for r in rules if r.get("event") == "PreToolUse"]


def _shape(payload: str) -> str:
    s = (payload or "")[:40].lower()
    return _HEX.sub("#", s)


def _fmt_ts(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")


def _proposal_for_entropy(entropy: float) -> str:
    if entropy < 0.2:
        return "compile to an L0 pre-check or tool rewrite (low entropy)"
    if entropy <= 0.6:
        return "narrow the match (mixed shapes)"
    return "retune or split (high entropy, guard is noise)"


def _compute_hot(fires, window_start: int, hot_threshold: int):
    by_reflex = defaultdict(list)
    for r in fires:
        by_reflex[r.get("reflex", "?")].append(r)

    rows = []
    for name, rs in by_reflex.items():
        total = len(rs)
        window_rs = [r for r in rs if r.get("ts", 0) >= window_start]
        fires_window = len(window_rs)
        if fires_window < hot_threshold:
            continue
        tss = [r.get("ts", 0) for r in rs]
        first, last = min(tss), max(tss)
        window_tss = [r.get("ts", 0) for r in window_rs]
        span_days = max(1.0, (max(window_tss) - min(window_tss)) / 86400.0)
        fires_per_day = fires_window / span_days
        tools = Counter(r.get("tool", "?") for r in window_rs)
        top_tools = [t for t, _ in tools.most_common(3)]
        shapes = {_shape(r.get("payload", "")) for r in window_rs}
        entropy = len(shapes) / fires_window if fires_window else 0.0
        action = window_rs[-1].get("action", "warn")
        weight = 1.0 if action == "warn" else 0.5
        score = fires_per_day * weight * (1.0 - entropy)
        rows.append({
            "reflex": name,
            "scope": name.split(":", 1)[0] if ":" in name else "?",
            "action": action,
            "fires_window": fires_window,
            "fires_total": total,
            "fires_per_day": round(fires_per_day, 3),
            "first": _fmt_ts(first),
            "last": _fmt_ts(last),
            "top_tools": top_tools,
            "entropy_proxy": round(entropy, 4),
            "score": round(score, 4),
            "proposal": _proposal_for_entropy(entropy),
            "top_shapes": sorted(shapes)[:3],
        })
    rows.sort(key=lambda r: -r["score"])
    return rows


def _compute_cold(rules, fires, now: int, cool_days: int, dormant_days: int):
    last_fire = {}
    total_fire = Counter()
    for r in fires:
        key = r.get("reflex", "?")
        total_fire[key] += 1
        ts = r.get("ts", 0)
        if ts > last_fire.get(key, -1):
            last_fire[key] = ts

    rows = []
    counts = Counter()
    for rule in rules:
        name = rule.get("name", "?")
        scope = rule.get("scope", "?")
        key = f"{scope}:{name}"
        last = last_fire.get(key)
        never = last is None
        if never:
            lifecycle = "DORMANT"
        else:
            silence_days = (now - last) / 86400.0
            if silence_days <= cool_days:
                lifecycle = "ACTIVE"
            elif silence_days <= dormant_days:
                lifecycle = "COOLING"
            else:
                lifecycle = "DORMANT"
        counts[lifecycle] += 1
        rows.append({
            "reflex": key,
            "lifecycle": lifecycle,
            "never_fired": never,
            "last": _fmt_ts(last) if last else None,
            "fires_total": total_fire.get(key, 0),
            "proposal": "demote candidate -- owner/moderator act" if lifecycle != "ACTIVE" else "",
        })
    return rows, counts


_STRIP_WORD = re.compile(r"\d+|[a-f0-9]{6,}|/[^\s]+", re.I)


def _recur_key(text: str) -> str:
    t = (text or "").lower()
    t = _STRIP_WORD.sub(" ", t)
    words = _WORD.findall(t)
    return " ".join(words[:8])


_MACHINE_KINDS = {"checkpoint", "staff_beat", "staff_skip", "check", "run_state", "item_annotation"}


def _epoch(ts) -> float:
    """Receipt `ts` is an ISO-8601 string (workcycle._now()); fires are int epochs. 0 = unknown."""
    if isinstance(ts, (int, float)):
        return float(ts)
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00")).timestamp()
    except Exception:
        return 0.0


def _compute_recurrence(room_names, room_dir_fn, window_start: int):
    keyed = defaultdict(lambda: {"count": 0, "rooms": set()})
    skipped = 0
    for room in room_names:
        try:
            # workcycle.room_dir() returns the room STATE dir (<repo>/.echelon) itself.
            rp = Path(room_dir_fn(room)) / "receipts.jsonl"
        except Exception:
            skipped += 1
            continue
        if not rp.exists():
            skipped += 1
            continue
        try:
            with open(rp, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        row = json.loads(line)
                    except Exception:
                        continue
                    ts = _epoch(row.get("ts", 0))   # receipts stamp ISO strings, not epochs
                    if ts and ts < window_start:
                        continue
                    # Machine rows (the room pulse's own beats/checkpoints, run-state, annotations)
                    # recur by construction: counting them is pattern poisoning from our own sensor.
                    if str(row.get("origin", "")).startswith("room-pulse") or                             row.get("kind") in _MACHINE_KINDS:
                        continue
                    key = _recur_key(row.get("text", ""))
                    if len(key) < 8:
                        continue
                    keyed[key]["count"] += 1
                    keyed[key]["rooms"].add(room)
        except Exception:
            skipped += 1
            continue

    rows = []
    for key, info in keyed.items():
        rooms = info["rooms"]
        count = info["count"]
        if (count >= 3 and len(rooms) >= 2) or (count >= 5 and len(rooms) == 1):
            rows.append({
                "key": key[:60],
                "count": count,
                "rooms": sorted(rooms),
                "proposal": "reflex/tool candidate -- same trap named repeatedly",
            })
    rows.sort(key=lambda r: -r["count"])
    return rows, skipped


def _load_health(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _write_health(path: Path, cold_rows, window_days: int) -> dict:
    existing = _load_health(path)
    existing_reflexes = (existing.get("reflexes") or {}) if isinstance(existing, dict) else {}
    reflexes = {}
    for row in cold_rows:
        key = row["reflex"]
        demoted = bool(existing_reflexes.get(key, {}).get("demoted", False))
        reflexes[key] = {
            "fires_total": row["fires_total"],
            "fires_window": row["fires_total"],
            "first": row.get("last"),
            "last": row.get("last"),
            "lifecycle": row["lifecycle"],
            "never_fired": row["never_fired"],
            "demoted": demoted,
        }
    doc = {
        "version": 1,
        "as_of": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "window_days": window_days,
        "reflexes": reflexes,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc, indent=2), encoding="utf-8")
    return doc


def _main_scan(args) -> int:
    now = int(datetime.now(timezone.utc).timestamp())
    window_start = now - args.days * 86400

    fires_path = _p(args.fires or FIRES_DEFAULT)
    rules_path = _p(args.rules or RULES_DEFAULT)

    fires = _read_fires(fires_path)
    rules = _read_rules(rules_path)

    hot = _compute_hot(fires, window_start, args.hot)
    cold, cold_counts = _compute_cold(rules, fires, now, args.cool_days, args.dormant_days)

    try:
        from echelon_engine import workcycle
        room_names = workcycle.registered_rooms()
        room_dir_fn = workcycle.room_dir
    except Exception:
        room_names, room_dir_fn = [], None

    recurrence, skipped_rooms = ([], 0)
    if room_dir_fn is not None:
        recurrence, skipped_rooms = _compute_recurrence(room_names, room_dir_fn, window_start)

    summary = (f"evolve scan: fires={len(fires)} reflexes={len(rules)} hot={len(hot)} "
               f"cooling={cold_counts.get('COOLING', 0)} dormant={cold_counts.get('DORMANT', 0)} "
               f"recurrence={len(recurrence)} window={args.days}d")

    if args.write_health:
        health_path = _p(args.health or HEALTH_DEFAULT)
        _write_health(health_path, cold, args.days)

    if args.json:
        out = {
            "hot": hot,
            "cold": {"counts": dict(cold_counts), "rows": [r for r in cold if r["lifecycle"] != "ACTIVE"]},
            "recurrence": recurrence[:10],
            "summary": summary,
        }
        payload = json.dumps(out, indent=2)
        if args.out:
            _p(args.out).write_text(payload, encoding="utf-8")
        else:
            print(payload)
        print(summary, file=sys.stderr)
        return 0

    lines = ["== HOT (promotion candidates) =="]
    for row in hot[:20]:
        lines.append(f"  {row['reflex']} action={row['action']} K={row['score']} "
                      f"fires/day={row['fires_per_day']} entropy={row['entropy_proxy']} "
                      f"tools={','.join(row['top_tools'])}")
        lines.append(f"    proposal: {row['proposal']}")
    if not hot:
        lines.append("  (none)")

    lines.append("")
    lines.append(f"== COLD (lifecycle) == ACTIVE={cold_counts.get('ACTIVE', 0)} "
                 f"COOLING={cold_counts.get('COOLING', 0)} DORMANT={cold_counts.get('DORMANT', 0)}")
    for row in cold:
        if row["lifecycle"] == "ACTIVE":
            continue
        never = " never_fired" if row["never_fired"] else ""
        lines.append(f"  {row['reflex']} [{row['lifecycle']}]{never} last={row['last']}")
        lines.append(f"    proposal: {row['proposal']}")

    lines.append("")
    lines.append(f"== RECURRENCE (skipped_rooms={skipped_rooms}) ==")
    for row in recurrence[:10]:
        lines.append(f"  \"{row['key']}\" count={row['count']} rooms={','.join(row['rooms'])}")
        lines.append(f"    proposal: {row['proposal']}")
    if not recurrence:
        lines.append("  (none)")

    report = "\n".join(lines)
    if args.out:
        _p(args.out).write_text(report, encoding="utf-8")
    else:
        print(report)
    print(summary, file=sys.stderr)
    return 0


def _main_demote(args) -> int:
    rules_path = _p(args.rules or RULES_DEFAULT)
    health_path = _p(args.health or HEALTH_DEFAULT)
    rules = _read_rules(rules_path)
    known = {f"{r.get('scope', '?')}:{r.get('name', '?')}" for r in rules}
    if args.name not in known:
        print(f"evolve demote: {args.name!r} is not a known PreToolUse rule", file=sys.stderr)
        return 1
    health = _load_health(health_path)
    if not isinstance(health, dict):
        health = {}
    reflexes = health.setdefault("reflexes", {})
    entry = reflexes.setdefault(args.name, {})
    entry["demoted"] = not args.undo
    health["version"] = health.get("version", 1)
    health["as_of"] = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    health_path.parent.mkdir(parents=True, exist_ok=True)
    health_path.write_text(json.dumps(health, indent=2), encoding="utf-8")
    print(f"evolve demote: {args.name} demoted={entry['demoted']}")
    return 0


def _main_health(args) -> int:
    health_path = _p(args.health or HEALTH_DEFAULT)
    health = _load_health(health_path)
    reflexes = health.get("reflexes", {}) if isinstance(health, dict) else {}
    if args.json:
        print(json.dumps(health, indent=2))
        return 0
    counts = Counter(v.get("lifecycle", "?") for v in reflexes.values())
    demoted = [k for k, v in reflexes.items() if v.get("demoted")]
    print(f"evolve health: as_of={health.get('as_of', '?')} window_days={health.get('window_days', '?')}")
    print(f"  counts: {dict(counts)}")
    print(f"  demoted ({len(demoted)}): {', '.join(sorted(demoted)) or '(none)'}")
    return 0



# ---------------------------------------------------------------------------
# evolve rulings -- rulings ledger + owner notes -> LAW PROPOSALS (OPEN-0113 win 2)
# ---------------------------------------------------------------------------

RULINGS_REL = "_scratch/hello/rulings.jsonl"
INBOX_REL = ".echelon/inbox"

_MACHINE_FROM = {"system", "room-pulse"}


def _estate() -> Path:
    return _p(str(_estate_cfg.estate_root_for("command_root")))


def _is_machine_from(frm: str) -> bool:
    frm = (frm or "").strip().lower()
    return frm in _MACHINE_FROM or frm.startswith("room-pulse")


def _fold_rulings(path: Path) -> list:
    """Fold ruling-ledger events (create/say/rule/acted/room) the way
    _scratch/hello/board.py:rulings() does, without importing the board.
    Returns only RULED rulings (a rule event has landed); say/acted are
    context, never law -- the OWNER's words live in `rule.text`."""
    out = {}
    for e in _read_fires(path):
        evt = e.get("evt")
        rid = e.get("id")
        if evt == "create":
            out[rid] = {
                "id": rid,
                "room": e.get("room", ""),
                "title": e.get("title", ""),
                "options": e.get("options", []) or [],
                "ruled_text": None,
                "ruled_ts": None,
                "status": "open",
                "board_refs": [],
            }
            continue
        if rid not in out:
            continue
        if evt == "say":
            # machine-origin chatter (the estate's own sensors) is never law
            # and is not folded into the ruling at all -- see _is_machine_from.
            continue
        if evt == "rule":
            out[rid]["status"] = "ruled"
            out[rid]["ruled_text"] = e.get("text", "")
            out[rid]["ruled_ts"] = e.get("ts", "")
        elif evt == "acted":
            if out[rid]["status"] != "open":
                out[rid]["status"] = "acted"
        elif evt == "room":
            out[rid]["room"] = e.get("room") or out[rid]["room"]
    return [r for r in out.values() if r["ruled_text"]]


def _owner_notes(inbox_dir: Path) -> list:
    """Owner-authored inbox notes only ({id, board_n, ts, text, ruling})."""
    out = []
    if not inbox_dir.exists():
        return out
    for f in sorted(inbox_dir.glob("NOTE-*.json")):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        if data.get("from") != "owner":
            continue
        out.append({
            "id": data.get("id", f.stem),
            "board_n": data.get("board_n"),
            "ts": data.get("ts", ""),
            "text": data.get("text", ""),
            "ruling": data.get("ruling"),
        })
    return out


_SENTENCE_SPLIT = re.compile(r"(?<=[.!?;])\s+|\n+")
_LAW_LEXICON = re.compile(
    r"\b(never|always|every|default|only|must|no|not|before|after|first|then|by\s+default)\b",
    re.I,
)
_OPTION_ARROW = re.compile(r"->|\u2192")
_QUOTE_RE = re.compile(r'"[^"]{4,}"|\u201c[^\u201d]{4,}\u201d|\'[^\']{4,}\'')


def _law_sentences(text: str) -> list:
    """Split a ruled/note text into candidate law sentences, keeping only
    imperative/declarative shapes (a small verb-lead + never/always/every/
    default/only/must/by-default lexicon, or a `-> X` option pick). Drops
    pure narration."""
    text = (text or "").strip()
    if not text:
        return []
    parts = [p.strip() for p in _SENTENCE_SPLIT.split(text) if p.strip()]
    if not parts:
        parts = [text]
    out = []
    for part in parts:
        if len(part) < 8:
            continue
        if _LAW_LEXICON.search(part) or _OPTION_ARROW.search(part):
            out.append(part)
    return out


def _is_verbatim(sentence: str) -> bool:
    """True only when the sentence is inside a quoted owner passage
    ('...' / "..." / (verbatim)) -- else it is a moderator paraphrase."""
    low = sentence.lower()
    if "(verbatim)" in low:
        return True
    return bool(_QUOTE_RE.search(sentence))


def _sentence_records(rulings: list, owner_notes: list) -> list:
    """Flatten ruled rulings + owner notes into {text, source_id, ts, room,
    verbatim} sentence records -- the clustering unit."""
    records = []
    for r in rulings:
        for sent in _law_sentences(r["ruled_text"]):
            records.append({
                "text": sent,
                "source_id": r["id"],
                "ts": r["ruled_ts"],
                "room": r["room"],
                "verbatim": _is_verbatim(sent),
            })
    for n in owner_notes:
        for sent in _law_sentences(n["text"]):
            records.append({
                "text": sent,
                "source_id": n.get("ruling") or n["id"],
                "ts": n["ts"],
                "room": "",
                "verbatim": _is_verbatim(sent),
            })
    return records


_CLUSTER_SYNONYMS = {
    "privately": "private", "repository": "repo", "repositories": "repo",
    "repos": "repo", "session": "seat", "sessions": "seat", "seats": "seat",
    "page": "page", "pages": "page",
}


def _cluster_words(text: str) -> list:
    t = _STRIP_WORD.sub(" ", (text or "").lower())
    words = _WORD.findall(t)
    return [_CLUSTER_SYNONYMS.get(w, w) for w in words]


def _cluster_key(text: str) -> str:
    return " ".join(_cluster_words(text)[:8])


def _cluster(sentences: list) -> list:
    """Normalized-key buckets (_cluster_key), then Jaccard >= 0.5 over
    content words merges near-duplicate buckets."""
    buckets = defaultdict(list)
    for s in sentences:
        buckets[_cluster_key(s["text"])].append(s)

    keys = list(buckets.keys())
    parent = {k: k for k in keys}

    def find(k):
        while parent[k] != k:
            k = parent[k]
        return k

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    wordsets = {k: set(k.split()) for k in keys}
    for i in range(len(keys)):
        for j in range(i + 1, len(keys)):
            a, b = keys[i], keys[j]
            wa, wb = wordsets[a], wordsets[b]
            if not wa or not wb:
                continue
            jac = len(wa & wb) / len(wa | wb)
            if jac >= 0.5:
                union(a, b)

    merged = defaultdict(list)
    for k in keys:
        merged[find(k)].extend(buckets[k])

    clusters = []
    for root, members in merged.items():
        rooms = {m["room"] for m in members if m["room"]}
        tss = [m["ts"] for m in members if m["ts"]]
        clusters.append({
            "key": root,
            "members": members,
            "rooms": sorted(rooms),
            "first_ts": min(tss) if tss else "",
            "last_ts": max(tss) if tss else "",
            "n": len(members),
        })
    clusters.sort(key=lambda c: -c["n"])
    return clusters


def _parse_ruling_ts(ts):
    if not ts:
        return None
    s = str(ts).replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(s)
    except Exception:
        try:
            return datetime.strptime(s[:19], "%Y-%m-%d %H:%M:%S")
        except Exception:
            return None


def _span_days(first_ts, last_ts) -> int:
    a, b = _parse_ruling_ts(first_ts), _parse_ruling_ts(last_ts)
    if not a or not b:
        return 0
    return abs((b - a).days)


def _within_since(ts, since_days: int, now: datetime) -> bool:
    if since_days <= 0:
        return True
    parsed = _parse_ruling_ts(ts)
    if not parsed:
        return True  # unknown ts -- never silently drop it
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return (now - parsed).days <= since_days


_SECTION_KEYWORDS = (
    ("campaign", ("campaign", "os_client", "os client")),
    ("doors", ("door", "entry")),
    ("routing", ("route", "routing", "dispatch")),
    ("gates", ("gate", "witness", "approve", "approval")),
)


def _section_hint(text: str) -> str:
    low = text.lower()
    for hint, keywords in _SECTION_KEYWORDS:
        if any(k in low for k in keywords):
            return hint
    return "other"


def _existing_atoms(law: str, db_path) -> list:
    """Tag a proposal with slugs the bank already holds this law under
    (lexical/warmth search, no network; same atoms-layer call the services
    gate's search_atoms() makes, called directly since atoms may not import
    the gate). Degrades to [] on any failure -- an absent/tmp db_path must
    never raise."""
    try:
        from echelon_engine.atoms.recall import SeedStore
        from echelon_engine.atoms.warmth import warmth
        if db_path:
            p = Path(db_path)
            core = p.parent / "core.db"
            core_path = str(core) if core.exists() else str(p)
            store = SeedStore(core_path, v2_db=str(p))   # v2 is primary -- must redirect explicitly
        else:
            store = SeedStore()
        result = warmth(law, store, scope="echelon", top_k=5)
        slugs = []
        for sw in (result.warmest or [])[:5]:
            seed = getattr(sw, "seed", None)
            if seed is None:
                continue
            slug = getattr(seed, "coordinate", "") or getattr(seed, "slug", "") or getattr(seed, "id", "")
            if slug:
                slugs.append(slug)
        return slugs
    except Exception:
        return []


def _propose(cluster: dict, db_path=None) -> dict:
    members = cluster["members"]
    verbatim_members = [m for m in members if m["verbatim"]]
    pool = verbatim_members if verbatim_members else members
    law = min(pool, key=lambda m: len(m["text"]))["text"]

    evidence = []
    seen = set()
    for m in sorted(members, key=lambda m: m["ts"] or ""):
        if m["source_id"] in seen:
            continue
        seen.add(m["source_id"])
        evidence.append({"id": m["source_id"], "ts": m["ts"], "room": m["room"], "quote": m["text"]})

    confidence = "declared" if verbatim_members else "inferred"
    section_hint = _section_hint(law)
    return {
        "law": law,
        "evidence": evidence,
        "n": cluster["n"],
        "rooms": cluster["rooms"],
        "span_days": _span_days(cluster["first_ts"], cluster["last_ts"]),
        "confidence": confidence,
        "existing_atoms": _existing_atoms(law, db_path),
        "section_hint": section_hint,
        "proposal": f"declare under {section_hint}, after the owner's word (constitution draft)",
    }


def _main_rulings(args) -> int:
    now = datetime.now(timezone.utc)
    estate = _estate()
    rulings_path = _p(args.rulings) if args.rulings else (estate / RULINGS_REL)
    inbox_path = _p(args.inbox) if args.inbox else (estate / INBOX_REL)

    since = args.since or 0
    all_rulings = _fold_rulings(rulings_path)
    rulings_f = [r for r in all_rulings if _within_since(r["ruled_ts"], since, now)]

    all_notes = _owner_notes(inbox_path)
    notes_f = [n for n in all_notes if _within_since(n["ts"], since, now)]

    sentences = _sentence_records(rulings_f, notes_f)
    clusters = _cluster(sentences)

    proposals, singletons = [], []
    for c in clusters:
        if c["n"] >= args.min_cluster:
            proposals.append(_propose(c, db_path=args.db))
        else:
            singletons.append({"key": c["key"], "n": c["n"], "rooms": c["rooms"]})
    proposals.sort(key=lambda p: (-p["n"], -p["span_days"]))

    summary = (f"evolve rulings: rulings={len(all_rulings)} notes={len(all_notes)} "
               f"sentences={len(sentences)} clusters={len(clusters)} proposals={len(proposals)}")

    if args.write_proposals:
        out_dir = _p(args.write_proposals)
        out_dir.mkdir(parents=True, exist_ok=True)
        doc = {
            "version": 1,
            "as_of": now.replace(microsecond=0).isoformat(),
            "sources": {"rulings": str(rulings_path), "inbox": str(inbox_path)},
            "proposals": proposals,
            "singletons": singletons,
        }
        fname = out_dir / f"LAW-PROPOSALS-{now.strftime('%Y-%m-%d')}.json"
        fname.write_text(json.dumps(doc, indent=2), encoding="utf-8")

    if args.json:
        payload = json.dumps({"proposals": proposals, "singletons": singletons, "summary": summary}, indent=2)
        if args.out:
            _p(args.out).write_text(payload, encoding="utf-8")
        else:
            print(payload)
        print(summary, file=sys.stderr)
        return 0

    lines = [f"== LAW PROPOSALS ({len(proposals)}) =="]
    for p in proposals:
        lines.append(f"  {p['law']}")
        lines.append(f"    confidence={p['confidence']} n={p['n']} rooms={','.join(p['rooms']) or '(none)'} "
                      f"span={p['span_days']}d")
        for ev in p["evidence"][:3]:
            lines.append(f"    [{ev['id']} {str(ev['ts'])[:10]} {ev['room']}] \"{ev['quote']}\"")
        lines.append(f"    existing_atoms: {','.join(p['existing_atoms']) or '(none)'}")
        lines.append(f"    proposal: {p['proposal']}")
    if not proposals:
        lines.append("  (none)")

    lines.append("")
    lines.append(f"== SINGLETONS ({len(singletons)}) ==")
    for s in singletons:
        lines.append(f"  \"{s['key']}\" n={s['n']} rooms={','.join(s['rooms']) or '(none)'}")
    if not singletons:
        lines.append("  (none)")

    report = "\n".join(lines)
    if args.out:
        _p(args.out).write_text(report, encoding="utf-8")
    else:
        print(report)
    print(summary, file=sys.stderr)
    return 0


def _main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="echelon evolve", description="Mine reflex fires/rules/receipts for promotion, demotion, and recurrence proposals.")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sc = sub.add_parser("scan")
    sc.add_argument("--days", type=int, default=30)
    sc.add_argument("--hot", type=int, default=200)
    sc.add_argument("--cool-days", type=int, default=14)
    sc.add_argument("--dormant-days", type=int, default=30)
    sc.add_argument("--json", action="store_true")
    sc.add_argument("--out", default=None)
    sc.add_argument("--fires", default=None)
    sc.add_argument("--rules", default=None)
    sc.add_argument("--write-health", action="store_true")
    sc.add_argument("--health", default=None)
    sc.set_defaults(func=_main_scan)

    dm = sub.add_parser("demote")
    dm.add_argument("name")
    dm.add_argument("--undo", action="store_true")
    dm.add_argument("--rules", default=None)
    dm.add_argument("--health", default=None)
    dm.set_defaults(func=_main_demote)

    hl = sub.add_parser("health")
    hl.add_argument("--json", action="store_true")
    hl.add_argument("--health", default=None)
    hl.set_defaults(func=_main_health)

    ru = sub.add_parser("rulings")
    ru.add_argument("--rulings", default=None)
    ru.add_argument("--inbox", default=None)
    ru.add_argument("--since", type=int, default=0)
    ru.add_argument("--min-cluster", type=int, default=2)
    ru.add_argument("--json", action="store_true")
    ru.add_argument("--out", default=None)
    ru.add_argument("--write-proposals", default=None)
    ru.set_defaults(func=_main_rulings, db=None)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(_main())
