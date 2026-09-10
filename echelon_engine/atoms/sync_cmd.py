"""sync — the two-way bank sync client (wave 2, 2026-08-02).

    echelon sync                 # one cycle: pull, apply, push
    echelon sync --dry-run       # show what WOULD move, change nothing
    echelon sync --status        # cursors + generations, no network

THE CYCLE (design: ECHELON/DELTA-SYNC-DESIGN.md, council -> skeptic -> tests -> box gate):

  1. HANDSHAKE — exchange generations BEFORE cursors. The peer's generation changing means
     its journal was rewound underneath our cursor (restore / vault heal / fresh bank), and
     replaying from a stale seq would silently skip everything written before the rewind. On
     mismatch we re-baseline from seq 0 instead.
  2. PULL  — GET /sync/pull?since=<our cursor for them>. Apply under the merge law, THEN
     advance the cursor (ack-after-apply: a crash mid-batch re-sends and re-applies
     idempotently, rather than acking work that never landed).
  3. PUSH  — POST /sync/push with our ops since their cursor for us. The gateway applies with
     the same merge law and returns the seq it consumed through.

SCOPE IS MANDATORY, not optional. The local bank holds 100+ scopes including other clients'
estates (alpha-app, mol, epsilon-co); the cloud bank is one identity's. Every pull and push is
scope-filtered, and the gateway ALSO filters server-side from the token's own scope — a client
bug must not be able to leak an estate.

The merge law itself lives in sync_journal.apply_ops (history EVENTS keyed by witnessing bank,
never a replayed score delta — decay here is read-time).
"""
from __future__ import annotations

import argparse
import gzip
import json
import os
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from . import sync_journal as sj

# No built-in gateway: an archive/sync endpoint is deployment-specific.
# Set ECHELON_GATEWAY to your own host to enable the remote verbs.
_GATEWAY = os.environ.get("ECHELON_GATEWAY", "").rstrip("/")
_PEER = "cloud"          # cursor key for the hub; a second peer would get its own row
_BATCH = 2000            # ops per round trip; the loop keeps going while a peer has more


def engine_version() -> str:
    """This side's engine version: pyproject.toml -> installed package -> git -> 'unknown'.

    pyproject FIRST, and that order is load-bearing (caught 2026-08-02): with an editable
    install, `importlib.metadata.version()` resolves against a CACHED dist-info and returns
    the version at install time — 0.1.0 — whenever the cwd is not the repo. The scheduled
    task runs from another directory, so a metadata-based reading NEVER sees a bump and the
    ship signal silently never fires. The repo's pyproject.toml is the actual source of truth
    for "what version is this tree", which is exactly the question the ship signal asks.

    Resolved inline rather than via export_import: an older deploy may not have that module
    (box4 did not), and a swallowed ImportError would report 'unknown' forever."""
    try:
        import re
        root = Path(__file__).resolve().parent.parent.parent
        pj = root / "pyproject.toml"
        if pj.exists():
            m = re.search(r'^version\s*=\s*["\']([^"\']+)["\']', pj.read_text(encoding="utf-8"), re.M)
            if m:
                return m.group(1)
    except Exception:
        pass
    try:
        from importlib.metadata import version
        return version("echelon")
    except Exception:
        pass
    try:
        import subprocess
        root = Path(__file__).resolve().parent.parent
        r = subprocess.run(["git", "describe", "--always", "--dirty"], cwd=str(root),
                           capture_output=True, text=True, timeout=10)
        if r.returncode == 0 and r.stdout.strip():
            return f"git:{r.stdout.strip()}"
    except Exception:
        pass
    return "unknown"


def _version_verdict(mine: str, theirs: str) -> tuple[str, str]:
    """Compare engine versions across the wire. Returns (state, human line).

    THE POINT (owner 2026-08-02): sync runs on a schedule and reports version drift, so
    shipping the engine becomes 'bump the version' rather than a manual deploy hunt. This
    DETECTS and REPORTS; it never auto-deploys — pushing code because a string differed is
    exactly the kind of unattended action that should stay a human decision."""
    if not theirs or theirs == "unknown":
        return "unknown", f"peer engine version unknown (mine {mine})"
    if mine == theirs:
        return "same", f"engine {mine} (peer matches)"
    return "drift", f"ENGINE DRIFT — local {mine} vs peer {theirs} (ship: deploy + bump)"


def _token(explicit: str | None) -> str:
    """The token for the REMOTE gateway.

    ECHELON_TOKEN wins over ECHELON_MCP_TOKEN, and that order matters: on a machine that also
    RUNS a gateway, ECHELON_MCP_TOKEN is *that local server's own* token, which the remote has
    never heard of. Presenting it gets a 401 from the real gateway while everything looks
    configured. ECHELON_TOKEN is the client-side credential. (Caught 2026-08-02: the desktop
    held both, and sync authenticated with the wrong one.)"""
    tok = explicit or os.environ.get("ECHELON_TOKEN") or ""
    if not tok:
        env = Path.home() / ".echelon" / ".env"
        if env.exists():
            lines = env.read_text(encoding="utf-8").splitlines()
            for key in ("ECHELON_TOKEN=", "ECHELON_MCP_TOKEN="):   # order = precedence
                for line in lines:
                    if line.startswith(key):
                        tok = line.split("=", 1)[1].strip()
                        break
                if tok:
                    break
    if not tok:
        tok = os.environ.get("ECHELON_MCP_TOKEN") or ""
    if not tok:
        raise SystemExit("no gateway token — pass --token, or set ECHELON_TOKEN in the env "
                         "or ~/.echelon/.env")
    return tok


def _call(gateway: str, route: str, token: str, *, payload: dict | None = None,
          query: str = "", timeout: int = 300) -> dict:
    url = f"{gateway.rstrip('/')}{route}{query}"
    headers = {"Authorization": f"Bearer {token}", "User-Agent": "echelon-sync/1.0"}
    data = None
    if payload is not None:
        data = gzip.compress(json.dumps(payload).encode("utf-8"), compresslevel=6)
        headers["Content-Type"] = "application/gzip"
        headers["Content-Encoding"] = "gzip"
    req = urllib.request.Request(url, data=data, headers=headers,
                                 method="POST" if payload is not None else "GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")[:400]
        raise SystemExit(f"gateway {route} -> HTTP {e.code}: {body}")
    except urllib.error.URLError as e:
        raise SystemExit(f"gateway {route} unreachable: {e.reason}")


def _open_bank(db: str | None) -> sqlite3.Connection:
    from .cards import DEFAULT_V2_DB
    path = Path(db) if db else DEFAULT_V2_DB
    if not path.exists():
        raise SystemExit(f"bank not found: {path}")
    conn = sqlite3.connect(str(path), timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


_GC_RETAIN = 5000     # rows kept below the prunable floor — a safety margin, not a budget


def sync_once(conn: sqlite3.Connection, *, gateway: str, token: str, scope: str,
              dry_run: bool = False, verbose: bool = True) -> dict:
    """One full cycle. Returns a receipt dict."""
    if not sj.generation(conn):
        raise SystemExit("this bank has no sync journal — run: echelon sync-journal attach "
                         "(and set ECHELON_SYNC=1 so new writes are captured)")
    my_gen = sj.generation(conn)
    st = sj.peer_state(conn, _PEER)
    out: dict = {"scope": scope, "pulled": 0, "pushed": 0, "rebaselined": False,
                 "dry_run": dry_run}

    # ── 1. handshake: generations (and engine versions) before cursors ────────
    my_ver = engine_version()
    hello = _call(gateway, "/sync/hello", token,
                  query=f"?scope={scope}&engine={urllib.parse.quote(my_ver)}")
    their_gen = hello.get("generation") or ""
    out["peer_generation"] = their_gen
    out["peer_head"] = hello.get("head", 0)
    out["engine"] = my_ver
    out["peer_engine"] = hello.get("engine") or ""
    out["engine_state"], out["engine_line"] = _version_verdict(my_ver, out["peer_engine"])
    if verbose and out["engine_state"] == "drift":
        print(f"  ! {out['engine_line']}")
    since = st["pulled_through"]
    if st["peer_generation"] and their_gen and st["peer_generation"] != their_gen:
        # their journal was rewound under our cursor — replaying from a stale seq would
        # silently skip everything written before the rewind.
        out["rebaselined"] = True
        since = 0
        if verbose:
            print(f"  ! peer generation changed ({st['peer_generation'][:8]} -> "
                  f"{their_gen[:8]}) — re-baselining from seq 0")

    # ── 2. pull ───────────────────────────────────────────────────────────────
    while True:
        got = _call(gateway, "/sync/pull", token,
                    query=f"?since={since}&scope={scope}&limit={_BATCH}")
        ops = got.get("ops") or []
        if not ops:
            break
        if dry_run:
            out["pulled"] += len(ops)
            break
        sj.apply_ops(conn, ops, origin=their_gen or "cloud")
        since = ops[-1]["seq"]
        # ACK ONLY AFTER the batch landed
        sj.set_peer_state(conn, _PEER, peer_generation=their_gen, pulled_through=since)
        out["pulled"] += len(ops)
        if len(ops) < _BATCH:
            break

    # ── 3. push ───────────────────────────────────────────────────────────────
    their_cursor = int(hello.get("your_cursor") or 0)
    if out["rebaselined"]:
        their_cursor = 0

    # OUR OWN REWIND (the 2026-08-18 break, found 08-18). The generation guard above is
    # ASYMMETRIC: it detects the PEER's journal being rewound, because /sync/hello returns
    # their generation. It cannot see OURS — hello sends only scope+engine, so a client whose
    # own journal was rewound (bank wipe + restore) keeps a cursor the peer minted against the
    # OLD, longer journal. Here: their cursor was 10052 against our head of 9571, so
    # ops_since(10052) returned nothing and EVERY push was a silent no-op for 16 days. The
    # sync reported success the whole time — rc=0, "pushed 0 op(s)" — because zero-to-push and
    # nothing-can-ever-be-pushed render identically.
    #
    # A cursor beyond our head is not a valid state: we cannot have sent ops we never wrote.
    # Treat it as proof of a local rewind and re-baseline from 0. Push is append-only and the
    # far side merges idempotently (history events keyed by origin), so re-offering ops the
    # peer already holds is safe — it costs one batch, not correctness.
    my_head = sj.head(conn)
    if their_cursor > my_head:
        out["local_rewound"] = True
        out["stale_cursor"] = their_cursor
        if verbose:
            print(f"  ! peer's cursor for us ({their_cursor}) is past our journal head "
                  f"({my_head}) — our journal was rewound (restore/wipe). "
                  f"Re-baselining our push from seq 0.")
        their_cursor = 0
    while True:
        mine = sj.ops_since(conn, their_cursor, scope=scope, limit=_BATCH)
        if not mine:
            break
        if dry_run:
            out["pushed"] += len(mine)
            break
        res = _call(gateway, "/sync/push", token,
                    payload={"generation": my_gen, "scope": scope, "ops": mine})
        consumed = int(res.get("through") or mine[-1]["seq"])
        out["pushed"] += int(res.get("applied_ops") or len(mine))
        out["peer_applied"] = res.get("applied")
        their_cursor = consumed
        sj.set_peer_state(conn, _PEER, peer_generation=their_gen, pushed_through=consumed)
        if len(mine) < _BATCH:
            break

    # ── auto-gc: prune-on-ack, silent unless it prunes ─────────────────────────
    # Only after a real (non-dry-run) push that actually advanced pushed_through — gc'ing on
    # a no-op cycle is pointless work, and a dry-run must change nothing on disk by contract.
    if not dry_run and out["pushed"]:
        try:
            gc_res = sj.gc(conn, retain=_GC_RETAIN, vacuum=False)
            out["gc"] = gc_res
            if verbose and gc_res["pruned"]:
                print(f"  gc: pruned {gc_res['pruned']} acked journal row(s) "
                      f"(retain={_GC_RETAIN})")
        except ValueError:
            pass   # no peers / no pushed_through yet — nothing to prune

    out["head"] = sj.head(conn)
    return out


def _main(argv=None):
    ap = argparse.ArgumentParser(
        prog="echelon sync",
        description="Two-way bank sync with the cloud gateway (pull, apply, push).")
    ap.add_argument("--scope", default=os.environ.get("ECHELON_SCOPE", "echelon"),
                    help="the scope to sync (default: $ECHELON_SCOPE or 'echelon'). "
                         "MANDATORY by design — a local bank holds many estates.")
    ap.add_argument("--gateway", default=_GATEWAY, help=f"gateway base URL (default {_GATEWAY})")
    ap.add_argument("--token", default=None, help="bearer token (default: env / ~/.echelon/.env)")
    ap.add_argument("--db", default=None, help="bank path (default: the live bank)")
    ap.add_argument("--dry-run", action="store_true", help="report what would move; change nothing")
    ap.add_argument("--status", action="store_true", help="local cursors + journal state, no network")
    ap.add_argument("--gc", action="store_true",
                    help="prune acked sync_journal rows (seq <= min(pushed_through)-retain); no network")
    ap.add_argument("--retain", type=int, default=_GC_RETAIN,
                    help=f"gc: rows to keep below the prunable floor (default {_GC_RETAIN})")
    ap.add_argument("--vacuum", action="store_true", help="gc: VACUUM after pruning (exclusive lock)")
    a = ap.parse_args(argv)

    conn = _open_bank(a.db)
    try:
        if a.gc:
            try:
                res = sj.gc(conn, retain=a.retain, vacuum=a.vacuum)
            except ValueError as e:
                print(f"sync gc refused: {e}", file=sys.stderr)
                return 1
            print(f"pruned {res['pruned']} row(s) (seq <= {res['cutoff_seq']}, "
                  f"retain={res['retain']})")
            print(f"db size: {res['size_before']:,}B -> {res['size_after']:,}B"
                  + ("  [vacuumed]" if res["vacuumed"] else ""))
            return 0

        if a.status:
            s = sj.journal_status(conn, retain=a.retain)
            if not s.get("attached"):
                print("journal NOT attached — run: echelon sync-journal attach")
                return 1
            st = sj.peer_state(conn, _PEER)
            print(f"generation     : {s['generation']}")
            print(f"journal head   : {s['head']}   ops: {s['total']}   "
                  f"bytes: {s['db_bytes']:,}   prunable: {s['prunable']}")
            print(f"peer '{_PEER}'    : pulled_through={st['pulled_through']} "
                  f"pushed_through={st['pushed_through']}")
            print(f"peer generation: {st['peer_generation'] or '(never synced)'}")
            if st["last_sync_ts"]:
                print(f"last sync      : {time.strftime('%Y-%m-%d %H:%M', time.localtime(st['last_sync_ts']))}")
            unsent = len(sj.ops_since(conn, st["pushed_through"], scope=a.scope, limit=100000))
            print(f"unsent (scope {a.scope}): {unsent} op(s)")
            if s["prunable"]:
                print(f"  ! {s['prunable']} row(s) prunable — run: echelon sync --gc")
            return 0

        t0 = time.time()
        r = sync_once(conn, gateway=a.gateway, token=_token(a.token), scope=a.scope,
                      dry_run=a.dry_run)
        tag = "WOULD sync" if a.dry_run else "synced"
        print(f"{tag} scope '{r['scope']}' with {a.gateway} in {time.time()-t0:.1f}s")
        print(f"  pulled {r['pulled']} op(s), pushed {r['pushed']} op(s)"
              + ("  [re-baselined]" if r["rebaselined"] else ""))
        if r.get("peer_applied"):
            print(f"  peer applied: {r['peer_applied']}")
        print(f"  {r.get('engine_line', '')}")
        # a version bump IS the ship signal — make it a non-zero exit so a scheduled run
        # surfaces in the task's LastTaskResult instead of scrolling past in a log.
        return 3 if r.get("engine_state") == "drift" else 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(_main())
