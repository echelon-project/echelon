"""tool_harvest.py — the POWERFUL MOVE made repeatable (owner, 2026-06-07).

THE MOVE: when you want a tool layer for a target (core.db, a bank, a config, an API), do NOT
guess what tools to build. SCAN the code that already touches the target raw, DUMP every raw
command/query it uses + the shape it returns — and that dump IS the endpoint template. Each distinct
raw access is a candidate endpoint; its output shape is the endpoint's return schema. The bypass
becomes the blueprint. (The anti-bypass rule never-raw-sql-the-soul says 'don't'; this says 'here is
exactly what to build instead, derived mechanically from what you tried to do raw'.)

This module harvests the RAW SQL surface of a Python package (the most common target). It is pure
stdlib (ast + regex over source) — it does NOT execute anything, so it is safe to run on any tree.
Output: a ranked catalog of distinct raw queries → the endpoint each implies, ready to turn into a
real API. Run:  python -X utf8 -m echelon_sdk.tool_harvest <pkg_dir>

For a LIVE target whose outputs you also want captured, pair this with a probe session that records
(query, output) pairs — the static surface here is the WHAT; a recorded run is the SHAPE.
"""
from __future__ import annotations
import re
import sys
from collections import Counter
from pathlib import Path

# raw DB-access call patterns (the bypass sites): conn.execute / executemany / a _retry wrapper.
_CALL = re.compile(r"(?:\.execute(?:many)?|_retry)\s*\(", re.I)
# the SQL verb that starts a raw statement (the query kind).
_SQL = re.compile(r"\b(SELECT|INSERT|UPDATE|DELETE|CREATE|ALTER|DROP|PRAGMA|REPLACE)\b", re.I)

# verb -> the endpoint family it usually implies (the template mapping).
_ENDPOINT = {
    "SELECT": "a READ tool (get/list/count/top by the human axes — never a table name)",
    "INSERT": "a WRITE tool (append/append_many — batch it; append-only makes order free)",
    "REPLACE": "a WRITE tool (upsert/supersede — append a new version, don't mutate)",
    "UPDATE": "a MUTATE tool (reinforce/rate — UAME score-delta, not a raw column write)",
    "DELETE": "an EVICT tool (expire/evict — and ONLY where the wall permits, e.g. bank/working)",
    "CREATE": "a SCHEMA tool (table ensure — internal; never a caller endpoint)",
    "ALTER":  "a MIGRATION tool (_ensure_columns — internal self-heal)",
    "PRAGMA": "an INTROSPECTION tool (stats/tables/domains)",
    "DROP":   "(should not exist on an append-only soul — flag it)",
}


def _extract_sql(text: str) -> list[str]:
    """Pull the SQL string literals out of raw DB-access calls. Normalises {placeholders} and ?-runs
    so two structurally-identical queries collapse to one endpoint candidate."""
    out: list[str] = []
    for m in _CALL.finditer(text):
        tail = text[m.end():m.end() + 400]
        for q in re.findall(r"""["']([^"']*?(?:SELECT|INSERT|UPDATE|DELETE|CREATE|ALTER|DROP|PRAGMA|REPLACE)[^"']*)["']""",
                            tail, re.I):
            norm = re.sub(r"\?+(\s*,\s*\?+)*", "?", q.strip())
            norm = re.sub(r"\s+", " ", norm)
            if _SQL.search(norm):
                out.append(norm)
    return out


def harvest(pkg_dir: str | Path) -> dict:
    """Scan a package's .py files for raw DB access; return the endpoint template."""
    pkg = Path(pkg_dir)
    files = sorted(pkg.rglob("*.py"))
    queries: Counter = Counter()
    per_file: dict[str, list[str]] = {}
    for f in files:
        if "__pycache__" in f.parts:
            continue
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        qs = _extract_sql(text)
        if qs:
            per_file[str(f.relative_to(pkg))] = qs
            queries.update(qs)
    # group by verb -> the endpoint each implies
    by_verb: dict[str, list[tuple[str, int]]] = {}
    for q, n in queries.most_common():
        v = (_SQL.search(q).group(1).upper() if _SQL.search(q) else "?")
        by_verb.setdefault(v, []).append((q, n))
    return {"files_scanned": len(files), "distinct_queries": len(queries),
            "by_verb": by_verb, "per_file": per_file}


def render(report: dict) -> str:
    lines = [f"RAW-QUERY CENSUS — {report['distinct_queries']} distinct queries across "
             f"{report['files_scanned']} files. Each row is a candidate ENDPOINT.\n"]
    for verb in ("SELECT", "INSERT", "REPLACE", "UPDATE", "DELETE", "PRAGMA", "CREATE", "ALTER", "DROP"):
        rows = report["by_verb"].get(verb)
        if not rows:
            continue
        lines.append(f"\n## {verb} → {_ENDPOINT.get(verb, '?')}")
        for q, n in rows:
            lines.append(f"  [{n}x] {q}")
    return "\n".join(lines)


# ── SESSION-TRANSCRIPT HARVEST — the move applied to what I actually RAN ──────────────────────────
# The static harvest scans source for raw SQL. This scans SESSION TRANSCRIPTS (.jsonl) for the raw
# shell commands I ran — every Bash/PowerShell command IS a raw access, and a command I re-ran across
# sessions is precisely a tool I keep needing and never built. Cluster by command SHAPE (the verb +
# subcommand, args stripped) → the most-repeated shapes are the endpoint template, ranked by real use.
import json as _json

# the leading token(s) that name an operation — keep verb + first subcommand, drop args/paths/flags.
_CMD_HEAD = re.compile(r"^\s*(?:cd\s+\S+\s*(?:&&|;)\s*)?([a-zA-Z0-9_\-./]+)(?:\s+([a-zA-Z0-9_\-]+))?")
# python -m module → the module IS the operation. Tolerate any tokens between `python` and `-m`
# (flags AND their args, e.g. `-X utf8`): scan up to `-m <module>` anywhere after python.
_PY_M = re.compile(r"\bpython[0-9.]*\b.*?\s-m\s+([a-zA-Z0-9_.]+)")
# inline python -c → a raw probe (the strongest "should be a tool" signal)
_PY_C = re.compile(r"python[0-9.]*\s+(?:-[A-Za-z]\S*\s+)*-c\b|<<'?PY'?")


def _cmd_shape(cmd: str) -> str:
    """Reduce a shell command to its OPERATION shape (args/paths stripped) so re-runs cluster."""
    cmd = cmd.strip()
    m = _PY_M.search(cmd)
    if m:
        return f"python -m {m.group(1)}"
    if _PY_C.search(cmd):
        return "python -c / heredoc  (INLINE PROBE → a missing tool)"
    h = _CMD_HEAD.match(cmd)
    if not h:
        return cmd[:40]
    head, sub = h.group(1), h.group(2) or ""
    base = head.rsplit("/", 1)[-1]
    # keep a subcommand only for known multi-verb tools (git status, node atlas.js, etc)
    if base in ("git", "node", "npm", "gh", "docker", "pip") and sub:
        return f"{base} {sub}"
    return base


def harvest_sessions(proj_dir: str | Path, last_n: int = 8) -> dict:
    """Mine the last N session transcripts for raw shell commands; cluster by operation shape."""
    proj = Path(proj_dir)
    files = sorted(proj.glob("*.jsonl"), key=lambda p: (p.stat().st_mtime_ns, p.name), reverse=True)[:last_n]
    shapes: Counter = Counter()
    inline_probes: list[str] = []
    raw_total = 0
    for f in files:
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for line in text.splitlines():
            try:
                o = _json.loads(line)
            except _json.JSONDecodeError:
                continue
            msg = o.get("message", {})
            content = msg.get("content")
            if not isinstance(content, list):
                continue
            for c in content:
                if isinstance(c, dict) and c.get("type") == "tool_use" and c.get("name") in ("Bash", "PowerShell"):
                    cmd = (c.get("input") or {}).get("command", "")
                    if not cmd:
                        continue
                    raw_total += 1
                    shape = _cmd_shape(cmd)
                    shapes[shape] += 1
                    if "INLINE PROBE" in shape and len(inline_probes) < 12:
                        inline_probes.append(cmd.strip().replace("\n", " ")[:100])
    return {"sessions": [f.name for f in files], "raw_commands": raw_total,
            "distinct_shapes": len(shapes), "shapes": shapes.most_common(),
            "inline_probe_examples": inline_probes}


def render_sessions(report: dict) -> str:
    lines = [f"SESSION RAW-COMMAND CENSUS — {report['raw_commands']} shell commands across "
             f"{len(report['sessions'])} sessions, {report['distinct_shapes']} distinct shapes.",
             "Each high-count shape is a tool you keep needing. INLINE PROBEs are the loudest "
             "signal (a raw python -c = a missing endpoint).\n"]
    for shape, n in report["shapes"]:
        if n >= 2:   # a shape run once may be a one-off; >=2 is a pattern worth a tool
            flag = "  ← BUILD THIS" if (n >= 5 or "INLINE PROBE" in shape) else ""
            lines.append(f"  [{n:3}x] {shape}{flag}")
    if report["inline_probe_examples"]:
        lines.append("\nINLINE PROBE examples (each = a raw access that should be a tool):")
        for ex in report["inline_probe_examples"]:
            lines.append(f"    $ {ex}")
    return "\n".join(lines)


def main(argv=None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    if not argv:
        print("usage:\n"
              "  python -X utf8 -m echelon_sdk.tool_harvest <package_dir>          # raw-SQL surface\n"
              "  python -X utf8 -m echelon_sdk.tool_harvest --sessions <proj_dir> [N]  # last-N transcripts")
        return 2
    if argv[0] == "--sessions":
        proj = argv[1] if len(argv) > 1 else str(Path.home() / ".claude" / "projects")
        n = int(argv[2]) if len(argv) > 2 else 8
        print(render_sessions(harvest_sessions(proj, n)))
        return 0
    print(render(harvest(argv[0])))
    return 0


if __name__ == "__main__":
    sys.exit(main())
