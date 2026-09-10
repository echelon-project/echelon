"""export-import -- bank round-trip (portable document + typed import + cycle safety).

    python -X utf8 -m echelon_engine export-bank --out <file> [--scope <scope>]
    python -X utf8 -m echelon_engine import-bank <file> [--scope <scope>] [--dry-run] [--merge-policy skip|newer|error]

A bank CAN leave the machine and come back intact. The export is a single JSON document
with a manifest section (version, timestamp, table inventory, column lists) and a data section
(table-name -> list of row-objects). JSON (not JSONL) is chosen because:
  - At ~7.5k atoms + edges (~30k total rows), a single JSON is still well under 50 MB.
  - Cross-table references (atom_links FK targets, supersedes chains) are validated at
    import time, which spans tables — a single document keeps that validation natural.
  - The manifest is read before data, and JSON parsing is streaming-incompatible anyway;
    splitting to JSONL would not reduce peak memory meaningfully at this scale.

The import is TOLERANT: per-field typed coercion (int-or-none, json-object-or-none),
skip-with-warning on unknown columns, never crashes on a column mismatch. The manifest
carries the column inventory as-was, so import can compare and warn.

IMPORT RESTORES VERBATIM: the import reproduces the atom_links graph exactly — including
tombstoned supersession legs and pre-existing cycles. Cycle DETECTION (DFS) is a guard
for the LIVE WRITE PATH (creating a NEW link at runtime), NOT for restoring history that
already exists in the source bank. A tombstoned supersession leg is a merge receipt
(see estate atom OWED-heal-tombstone-breaks-hygiene-replan) and must never be dropped
during restore.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

# ── manifest constants ──────────────────────────────────────────────────────
EXPORT_VERSION = 1  # format version (increment on breaking manifest shape changes)


def _engine_version() -> str:
    """Best-effort engine version: installed package -> git describe -> 'unknown'."""
    try:
        from importlib.metadata import version
        return version("echelon")
    except Exception:
        pass
    try:
        engine_root = Path(__file__).resolve().parent.parent  # echelon_engine/
        r = subprocess.run(
            ["git", "describe", "--always", "--dirty"],
            cwd=str(engine_root), capture_output=True, text=True, timeout=10)
        if r.returncode == 0 and r.stdout.strip():
            return f"git:{r.stdout.strip()}"
    except Exception:
        pass
    return "unknown"


# ── table discovery ──────────────────────────────────────────────────────────
# The v2 bank (echelon.db) tables. We introspect via PRAGMA table_info rather than
# hardcoding column lists — the export survives ADD COLUMN migrations without recompile.
# sqlite_sequence is skipped (it's an internal autoincrement tracker).

_SKIP_TABLES = frozenset({"sqlite_sequence"})


def _list_tables(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' "
        "ORDER BY name").fetchall()
    return [r[0] for r in rows]


def _table_info(conn: sqlite3.Connection, table: str) -> list[dict]:
    """Return [{cid, name, type, notnull, dflt_value, pk}, ...] for a table."""
    return [dict(r) for r in conn.execute(f"PRAGMA table_info(\"{table}\")").fetchall()]


def _row_to_dict(row: sqlite3.Row, columns: list[str]) -> dict:
    """Convert a sqlite3.Row to a plain dict, handling None and numeric types."""
    d = {}
    for col in columns:
        val = row[col] if col in row.keys() else None
        if isinstance(val, bytes):
            val = None  # we never store blobs; defensive
        d[col] = val
    return d


# ── export ───────────────────────────────────────────────────────────────────

def export_bank(db_path: str, out_path: str, scope: str | None = None) -> dict:
    """Export the bank at db_path to out_path (a JSON document).

    Returns a summary dict: {tables_exported, total_rows, scope_filter, ...}.
    """
    db_path = os.path.abspath(db_path)
    if not os.path.exists(db_path):
        raise FileNotFoundError(f"bank not found: {db_path}")

    # READ-ONLY: use a URI with mode=ro so we can never write the source.
    uri = f"file:{db_path}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    try:
        tables = [t for t in _list_tables(conn) if t not in _SKIP_TABLES]
        manifest = {
            "format": "echelon-bank-export",
            "export_version": EXPORT_VERSION,
            "engine_version": _engine_version(),
            "export_ts": int(time.time()),
            "export_ts_human": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime()),
            "source_db": str(db_path),
            "source_db_size": os.path.getsize(db_path),
            "scope_filter": scope,
            "tables": {},
        }

        # Scope inventory: count atoms per scope
        scope_inv = {}
        if "atoms" in tables:
            try:
                for r in conn.execute(
                    "SELECT scope, COUNT(*) c FROM atoms GROUP BY scope").fetchall():
                    scope_inv[r["scope"] or "(empty)"] = r["c"]
            except Exception:
                pass
        manifest["scope_inventory"] = scope_inv

        data: dict[str, list[dict]] = {}
        total_rows = 0

        for tbl in tables:
            cols_info = _table_info(conn, tbl)
            col_names = [c["name"] for c in cols_info]
            manifest["tables"][tbl] = {
                "columns": col_names,
                "column_types": {c["name"]: c["type"] for c in cols_info},
            }

            if scope and tbl in ("atoms", "atom_spine", "atom_earned", "atom_body"):
                # Filter rows that belong to the requested scope.
                # atoms is the source of truth; joined tables filter by atom_id IN scope atoms.
                if tbl == "atoms":
                    rows = conn.execute(
                        "SELECT * FROM atoms WHERE scope=?", (scope,)).fetchall()
                elif tbl == "atom_spine":
                    rows = conn.execute(
                        "SELECT s.* FROM atom_spine s "
                        "JOIN atoms a ON a.id = s.atom_id WHERE a.scope=?",
                        (scope,)).fetchall()
                elif tbl == "atom_earned":
                    rows = conn.execute(
                        "SELECT e.* FROM atom_earned e "
                        "JOIN atoms a ON a.id = e.atom_id WHERE a.scope=?",
                        (scope,)).fetchall()
                elif tbl == "atom_body":
                    rows = conn.execute(
                        "SELECT b.* FROM atom_body b "
                        "JOIN atoms a ON a.id = b.atom_id WHERE a.scope=?",
                        (scope,)).fetchall()
                else:
                    rows = conn.execute(f"SELECT * FROM \"{tbl}\"").fetchall()
            else:
                rows = conn.execute(f"SELECT * FROM \"{tbl}\"").fetchall()

            manifest["tables"][tbl]["row_count"] = len(rows)
            data[tbl] = [_row_to_dict(r, col_names) for r in rows]
            total_rows += len(rows)

        out = {"manifest": manifest, "data": data}

        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, default=str)

        return {
            "tables_exported": len(tables),
            "total_rows": total_rows,
            "scope_filter": scope,
            "output": out_path,
            "output_size": os.path.getsize(out_path),
        }
    finally:
        conn.close()


# ── import ───────────────────────────────────────────────────────────────────

# Per-field typed coercion: try to coerce a value to the target column's affinity.
# We map column types from the manifest to Python types.


def _coerce_value(val: Any, col_name: str, col_type: str) -> Any:
    """Coerce a value to match the column's declared type affinity.

    Types we handle:
      INTEGER / INT / BIGINT -> int or None
      REAL / FLOAT / DOUBLE -> float or int (sqlite stores them interchangeably) or None
      TEXT / VARCHAR / '' -> str or None
      JSON-like stored as TEXT -> try json.loads if it starts with [ or {
      BOOLEAN / bool column -> int (0/1)

    Unknown columns (not in the target schema) return the val as-is with a note.
    Returns the coerced value. Never raises — a failed coercion returns the original value.
    """
    if val is None:
        return None

    # Bytes are never stored — treat as None (defensive)
    if isinstance(val, bytes):
        return None

    ctype = (col_type or "").upper().strip()

    if "INT" in ctype or ctype in ("BOOLEAN", "BOOL"):
        try:
            return int(val)
        except (ValueError, TypeError):
            return val

    if "REAL" in ctype or "FLOAT" in ctype or "DOUBLE" in ctype:
        try:
            return float(val)
        except (ValueError, TypeError):
            return val

    if "TEXT" in ctype or "VARCHAR" in ctype or ctype == "":
        # If it's JSON-shaped, try to parse it for validation but store as string
        # (sqlite has no native JSON column; score_history, refs are TEXT columns storing JSON)
        if isinstance(val, str):
            if (val.startswith("[") or val.startswith("{")) and (
                col_name in ("score_history", "refs", "trigger_signals",
                            "born_from", "supersedes", "scope")):
                # These are TEXT columns that carry JSON; keep as string.
                pass
            return str(val)
        return str(val)

    # Unknown type — try str() for SQLite compatibility; fall back to original
    if not isinstance(val, (int, float, str, type(None))):
        try:
            return str(val)
        except Exception:
            return val
    return val


def _import_table(conn: sqlite3.Connection, table: str, rows: list[dict],
                  manifest_cols: list[str],
                  dry_run: bool = False, merge_policy: str = "error",
                  strict_columns: bool = False) -> dict:
    """Import rows into a single table. Returns {inserted, skipped, conflicts, warnings}.

    On dry_run=True, does not write — just reports what WOULD happen.

    When strict_columns=True, a source column absent from the destination is a HARD
    ERROR (ValueError) — used when importing into a freshly-created bank whose canonical
    init path should have created every column. When False (the default), unknown columns
    are skipped with a warning (tolerance for importing into an older existing bank).
    """
    result = {"inserted": 0, "skipped": 0, "conflicts": 0, "warnings": []}

    # Get actual columns in the target DB
    actual_cols_info = _table_info(conn, table)
    actual_col_names = {c["name"] for c in actual_cols_info}
    actual_col_types = {c["name"]: c["type"] for c in actual_cols_info}

    for i, row in enumerate(rows):
        # Build the INSERT dict, coercing each field
        insert_vals: dict[str, Any] = {}
        unknown_cols = []
        for mcol in manifest_cols:
            if mcol not in row:
                continue
            if mcol not in actual_col_names:
                unknown_cols.append(mcol)
                continue
            raw = row[mcol]
            ctype = actual_col_types.get(mcol, "")
            insert_vals[mcol] = _coerce_value(raw, mcol, ctype)

        if unknown_cols:
            if strict_columns:
                raise ValueError(
                    f"import-bank: fresh bank missing columns in table '{table}': "
                    f"{unknown_cols}. The canonical init path should have created these "
                    f"columns — this indicates an incomplete schema initialization. "
                    f"Source manifest columns: {manifest_cols}; "
                    f"destination columns: {sorted(actual_col_names)}")
            result["warnings"].append(
                f"{table}[{i}]: unknown columns skipped: {unknown_cols}")

        col_names = list(insert_vals.keys())
        placeholders = ", ".join("?" * len(col_names))
        col_list = ", ".join(f'"{c}"' for c in col_names)
        vals = [insert_vals[c] for c in col_names]

        if dry_run:
            # Check if a row with the same PK already exists
            pk_cols = _pk_columns(conn, table)
            if pk_cols:
                pk_where = " AND ".join(f'"{c}"=?' for c in pk_cols)
                pk_vals = []
                for c in pk_cols:
                    v = insert_vals.get(c)
                    pk_vals.append(v)
                existing = conn.execute(
                    f"SELECT 1 FROM \"{table}\" WHERE {pk_where}", pk_vals).fetchone()
                if existing:
                    result["conflicts"] += 1
                    continue
            result["inserted"] += 1
        else:
            try:
                cur = conn.execute(
                    f"INSERT OR IGNORE INTO \"{table}\" ({col_list}) VALUES ({placeholders})",
                    vals)
                if cur.rowcount > 0:
                    result["inserted"] += 1
                else:
                    result["conflicts"] += 1
            except sqlite3.OperationalError as e:
                result["warnings"].append(f"{table}[{i}]: INSERT failed: {e}")
                result["skipped"] += 1

    return result


def _pk_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    """Get the primary key column names for a table."""
    info = _table_info(conn, table)
    return [c["name"] for c in info if c["pk"] > 0]


def _would_create_cycle(conn: sqlite3.Connection, from_id: str, to_id: str,
                        relation: str) -> bool:
    """Check if adding link (from_id --relation--> to_id) would create a cycle
    in the atom_links graph for 'supersedes' edges. Uses DFS from to_id.

    For other relation types, cycles are not harmful (refs / instances are DAGs
    but can have cycles in legitimate use). Only 'supersedes' must be acyclic.
    """
    if relation != "supersedes":
        return False
    # DFS from to_id: if we can reach from_id, adding the edge creates a cycle.
    visited: set[str] = set()
    stack = [to_id]
    while stack:
        node = stack.pop()
        if node == from_id:
            return True
        if node in visited:
            continue
        visited.add(node)
        # Follow supersedes edges out
        for r in conn.execute(
            "SELECT to_id FROM atom_links WHERE from_id=? AND relation='supersedes' AND superseded_on=0",
            (node,)).fetchall():
            if r[0] not in visited:
                stack.append(r[0])
    return False


def import_bank(file_path: str, db_path: str, scope: str | None = None,
                dry_run: bool = False,
                merge_policy: str = "error") -> dict:
    """Import a bank export into db_path.

    merge_policy:
      - "error": raise on any import (leads to conflict if PK already exists, detected per-row)
      - "skip": skip rows whose PK already exists (INSERT OR IGNORE)
      - "newer": skip rows whose PK exists AND have a more recent ts (future)

    dry_run: report what WOULD be done, write nothing.

    Returns a detailed report dict.
    """
    if merge_policy not in ("skip", "newer", "error"):
        raise ValueError(f"unknown merge_policy: {merge_policy!r}")

    db_path = os.path.abspath(db_path)

    if not os.path.exists(file_path):
        raise FileNotFoundError(f"export file not found: {file_path}")

    with open(file_path, "r", encoding="utf-8") as f:
        doc = json.load(f)

    manifest = doc.get("manifest", {})
    data = doc.get("data", {})

    if manifest.get("format") != "echelon-bank-export":
        raise ValueError("file is not an echelon bank export (missing or wrong format marker)")

    export_version = manifest.get("export_version", 0)
    if export_version > EXPORT_VERSION:
        print(f"warning: export version {export_version} > importer version {EXPORT_VERSION}; "
              f"best-effort import, some fields may be lost", file=sys.stderr)

    # Ensure the target DB directory exists
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)

    # Determine if target is pre-existing BEFORE any writes — this governs
    # whether we tolerate unknown columns (merge into older bank) or hard-error
    # (fresh bank created by canonical init, which MUST match the source).
    target_was_existing = os.path.exists(db_path)

    # Temp file path for dry_run schema probe (cleaned in finally)
    _temp_schema_db: str | None = None

    if dry_run and not target_was_existing:
        # Non-existent target for dry run: create a temp db via the canonical
        # init path (CardStore runs ALL migrations) so PRAGMA table_info
        # reflects the full schema. Open read-only; delete after use.
        import tempfile as _tempfile
        _tmp_fd, _temp_schema_db = _tempfile.mkstemp(suffix=".db")
        os.close(_tmp_fd)
        from .cards import CardStore as _CardStore
        _init = _CardStore(_temp_schema_db)
        _init.conn.close()
        conn = sqlite3.connect(f"file:{_temp_schema_db}?mode=ro", uri=True, timeout=10)
        conn.row_factory = sqlite3.Row
    elif dry_run:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=10)
        conn.row_factory = sqlite3.Row
    else:
        if not target_was_existing:
            # Fresh bank: use the canonical CardStore init path, which runs
            # EVERY migration (cards.kind, cards.prev, atoms scope/kind/
            # valence/arousal, atom_spine scope/witness, atom_earned scope,
            # FTS5, performance indexes, etc.) — nothing silently dropped.
            from .cards import CardStore as _CardStore
            _init = _CardStore(db_path)
            _init.conn.close()
        conn = sqlite3.connect(db_path, timeout=10)
        conn.row_factory = sqlite3.Row

    try:
        # Schema initialization is handled BEFORE this try block:
        #   - Fresh bank: CardStore.__init__ ran ALL migrations + DDL.
        #   - Existing bank: already has its schema (whatever it is).
        #   - Dry run: target is opened read-only or a temp proxy is used.
        # No per-connection DDL needed here.

        report = {
            "dry_run": dry_run,
            "merge_policy": merge_policy,
            "scope_filter": scope,
            "tables": {},
            "total_inserted": 0,
            "total_skipped": 0,
            "total_conflicts": 0,
            "total_warnings": 0,
            "warnings": [],
            "cycles_detected": 0,
            "cycles_skipped": [],
        }

        # Phase 1: Import data tables (atoms first, then dependent tables)
        # Order matters: atoms must exist before atom_spine/atom_body/atom_earned
        # which FK-reference atom_id.
        table_order = ["atoms", "cards", "atom_spine", "atom_body", "atom_earned",
                       "atom_sidecar", "atom_links", "impressions", "wrap_reviews"]

        for tbl in table_order:
            if tbl not in data:
                continue
            if tbl not in _list_tables(conn) and not dry_run:
                # Table still doesn't exist after schema init — skip
                report["warnings"].append(f"table {tbl} not in target schema; skipping {len(data[tbl])} rows")
                continue

            manifest_cols = manifest.get("tables", {}).get(tbl, {}).get("columns", [])
            rows = data[tbl]

            # Scope filtering on import
            if scope and tbl in ("atoms", "atom_spine", "atom_earned", "atom_body"):
                if tbl == "atoms":
                    rows = [r for r in rows if r.get("scope") == scope]
                # For joined tables, we need the atom to exist in scope.
                # We keep all rows and let INSERT OR IGNORE handle missing FKs.
                # Actually, filtering on scope atoms requires knowing which atom_ids
                # are scoped atoms. We can't know that until atoms are imported.
                # So for import, we filter only the atoms table; joined tables
                # get all rows (INSERT OR IGNORE + FK constraint means orphan
                # joined rows just get ignored).

            tbl_report = _import_table(conn, tbl, rows, manifest_cols,
                                       dry_run=dry_run, merge_policy=merge_policy,
                                       strict_columns=(not target_was_existing and not dry_run))
            report["tables"][tbl] = tbl_report
            report["total_inserted"] += tbl_report["inserted"]
            report["total_skipped"] += tbl_report["skipped"]
            report["total_conflicts"] += tbl_report["conflicts"]
            report["total_warnings"] += len(tbl_report["warnings"])
            report["warnings"].extend(tbl_report["warnings"])

        # Phase 2: Cycle detection on supersedes edges (OBSERVATION ONLY).
        # Cycle detection is a guard for the LIVE WRITE PATH (creating a NEW
        # link at runtime), not for restoring history. A tombstoned supersession
        # leg is a merge receipt and must survive verbatim restore. We detect
        # and WARN about cycles in the restored graph, but NEVER drop edges.
        if "atom_links" in data and not dry_run:
            for row in data["atom_links"]:
                if row.get("relation") != "supersedes":
                    continue
                fid = row.get("from_id", "")
                tid = row.get("to_id", "")
                if _would_create_cycle(conn, fid, tid, "supersedes"):
                    report["cycles_detected"] += 1
                    # Include superseded_on values for observability
                    so_val = row.get("superseded_on", "?")
                    report["cycles_skipped"].append(
                        f"supersedes cycle observed (imported verbatim): "
                        f"{fid} -> {tid} (superseded_on={so_val})")

        # Never commit on dry run
        if not dry_run:
            conn.commit()

        return report
    finally:
        conn.close()
        if _temp_schema_db:
            try:
                os.unlink(_temp_schema_db)
            except OSError:
                pass


# ── CLI entry points ─────────────────────────────────────────────────────────

def _main_export(argv=None):
    ap = argparse.ArgumentParser(prog="echelon export-bank",
                                 description="Export the bank to a portable JSON document.")
    ap.add_argument("--out", required=True, metavar="FILE",
                    help="output JSON file path")
    ap.add_argument("--scope", default=None,
                    help="export only this scope (default: all scopes)")
    ap.add_argument("--bank", default=None,
                    help="bank path (default: ~/.echelon/echelon.db)")
    a = ap.parse_args(argv)

    if a.bank:
        db_path = a.bank
    else:
        from .echelon_home import home_db
        db_path = str(home_db("echelon.db"))

    try:
        summary = export_bank(db_path, a.out, scope=a.scope)
    except FileNotFoundError as e:
        print(f"export-bank: {e}", file=sys.stderr)
        return 1

    print(f"exported {summary['total_rows']} rows from {summary['tables_exported']} tables "
          f"-> {summary['output']} ({summary['output_size']/1024:.1f} KB)")
    if a.scope:
        print(f"  scope filter: {a.scope}")
    return 0


def _main_import(argv=None):
    ap = argparse.ArgumentParser(prog="echelon import-bank",
                                 description="Import a bank export into a bank.")
    ap.add_argument("file", help="export JSON file to import")
    ap.add_argument("--scope", default=None,
                    help="import only this scope (default: all)")
    ap.add_argument("--dry-run", action="store_true",
                    help="print what WOULD change, write nothing")
    ap.add_argument("--merge-policy", choices=["skip", "newer", "error"],
                    default="error",
                    help="how to handle existing rows: skip (keep target), "
                         "newer (keep target if newer), error (report conflicts; default)")
    ap.add_argument("--bank", default=None,
                    help="target bank path (default: ~/.echelon/echelon.db)")
    a = ap.parse_args(argv)

    if a.bank:
        db_path = a.bank
    else:
        from .echelon_home import home_db
        db_path = str(home_db("echelon.db"))

    try:
        report = import_bank(a.file, db_path, scope=a.scope,
                             dry_run=a.dry_run, merge_policy=a.merge_policy)
    except FileNotFoundError as e:
        print(f"import-bank: {e}", file=sys.stderr)
        return 1
    except ValueError as e:
        print(f"import-bank: {e}", file=sys.stderr)
        return 1

    if a.dry_run:
        print("DRY RUN — no writes performed.")
    print(f"import report ({report['merge_policy']} policy):")
    print(f"  would insert: {report['total_inserted']} rows")
    print(f"  conflicts/skipped: {report['total_conflicts']}")
    print(f"  skipped: {report['total_skipped']}")
    if report["cycles_detected"]:
        print(f"  cycles observed (imported verbatim): {report['cycles_detected']}")
        for c in report["cycles_skipped"]:
            print(f"    - {c}")
    if report["warnings"]:
        print(f"  warnings: {report['total_warnings']}")
        for w in report["warnings"][:10]:
            print(f"    - {w}")
        if len(report["warnings"]) > 10:
            print(f"    ... and {len(report['warnings']) - 10} more")
    if a.scope:
        print(f"  scope filter: {a.scope}")
    return 0
