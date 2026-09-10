"""18899 BOARD WAKE — the owner board reaches every session, but only ONE session watches it.

Owner orders: 2026-08-30 #71 "prep so every session I could monitor and reply from here";
#78 "make a moderator role ... that way not all session need to monitor the board".

ROLES (decided per hook call, nothing to configure in the session):
  moderator  the session named in ~/.echelon/moderator.json with a live heartbeat (<15 min),
             OR any session when no moderator is alive (fallback: the board is never unattended).
             Gets every unread OWNER row at each prompt and is BLOCKED at Stop while an owner
             row is unanswered after its last [room] post.
  worker     everyone else. Never reads the whole board, never blocked. At each prompt it receives
             only what is addressed to ITS room: owner rows prefixed "[<room>]" and inbox notes the
             moderator routed into <room>/.echelon/inbox/*.json.

Doors (argv[1]): start | prompt | stop.  Claim the seat: `python tools/moderator.py claim` (or
launch a dedicated one: `python tools/moderator.py launch`; the launcher sets ECHELON_MODERATOR=1
and the start door claims automatically).
State: ~/.echelon/board_cursor/<session_id>.json · ~/.echelon/sessions/<session_id>.json (who is
alive, which room) · ~/.echelon/moderator.json. Silent on any failure - a hook never breaks a session.
Test overrides: BOARD_WAKE_FILE=<board.jsonl>, ECHELON_HOME=<dir instead of ~/.echelon>.
"""
import json
import os
import sys
import time
import urllib.request
from pathlib import Path

# SILENT SEAT (OPEN-0096): a generator seat launched by cli/oneshot_generator.py
# runs in a foreign cwd but still fires the user-level Stop hooks — which posted
# its generated bodies to the 18899 board as noise. A seat marked silent must
# make zero side effects on stop: exit immediately, before any board/bus/room I/O.
if os.environ.get("ECHELON_SILENT_SEAT") == "1":
    sys.exit(0)

def _estate():
    """The estate resolver, importable from this hook's own directory."""
    import sys as _sys
    from pathlib import Path as _Path
    _here = str(_Path(__file__).resolve().parent)
    if _here not in _sys.path:
        _sys.path.insert(0, _here)
    import _estate_boot
    return _estate_boot


BOARD = Path(os.environ.get("BOARD_WAKE_FILE")
             or Path(_estate().command_root()) / "_scratch" / "hello" / "board.jsonl")
API = "http://127.0.0.1:18899/api"
ECH = Path(os.environ.get("ECHELON_HOME") or (Path.home() / ".echelon"))
CUR_DIR = ECH / "board_cursor"
SESS_DIR = ECH / "sessions"
MOD = ECH / "moderator.json"
ROOMS = ECH / "rooms.json"
MOD_TTL = 15 * 60
MAX_ROWS = 8


def _jload(p: Path, default):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return default


def _jsave(p: Path, obj):
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


def _rows():
    try:
        out = []
        for line in BOARD.read_text(encoding="utf-8").splitlines():
            if line.strip():
                try:
                    out.append(json.loads(line))
                except Exception:
                    pass
        return out
    except Exception:
        return []


def _rooms():
    return _jload(ROOMS, {}).get("rooms", {})


def _worktree_main(cwd: str):
    """A git WORKTREE (<estate-root>/delta-shop-wt) has a `.git` FILE pointing at
    <main>/.git/worktrees/<name>; return <main> or None. Owner #932 (2026-09-02): the worktree
    resolved to no room, so its worker posted as [delta-shop-wt], got no inbox, no checkpoint."""
    try:
        d = os.path.abspath(cwd or "")
        for _ in range(12):
            g = os.path.join(d, ".git")
            if os.path.isfile(g):
                line = open(g, encoding="utf-8", errors="replace").read().strip()
                if line.startswith("gitdir:"):
                    gd = line[len("gitdir:"):].strip().replace("\\", "/")
                    if "/.git/worktrees/" in gd:
                        return gd.split("/.git/worktrees/")[0]
                return None
            if os.path.isdir(g):
                return None
            nd = os.path.dirname(d)
            if nd == d:
                return None
            d = nd
    except Exception:
        return None
    return None


def _room_for(cwd: str, _hop=True):
    """(room name, room .echelon path) by longest registered path prefix; a git worktree resolves to
    its main checkout's room; ('<basename>', None) if none."""
    try:
        c = os.path.normcase(os.path.abspath(cwd or ""))
        best = ("", "", None)
        for name, r in _rooms().items():
            root = os.path.normcase(os.path.abspath(os.path.dirname(r.get("path", ""))))
            if root and (c == root or c.startswith(root + os.sep)) and len(root) > len(best[1]):
                best = (name, root, r.get("path"))
        if not best[0] and _hop:
            main = _worktree_main(cwd)
            if main:
                return _room_for(main, _hop=False)
        return (best[0] or os.path.basename(c) or "?", best[2])
    except Exception:
        return (os.path.basename(cwd or "") or "?", None)


def _post(text: str):
    try:
        req = urllib.request.Request(API, data=json.dumps({"from": "claude", "text": text}).encode("utf-8"),
                                     headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=4) as r:
            return json.loads(r.read().decode("utf-8")).get("n")
    except Exception:
        return None


def _ts(r) -> float:
    try:
        return time.mktime(time.strptime(str(r.get("ts", ""))[:19], "%Y-%m-%d %H:%M:%S"))
    except Exception:
        return 0.0


def _files(r):
    """owner #1628 (2026-09-02): an owner row's attachments were invisible to the responder -
    the hook printed only `text`. List them so the moderator OPENS the image (Read on the
    local path under _scratch/hello/uploads/) instead of answering the caption alone."""
    fs = r.get("files") or []
    if not fs:
        return ""
    parts = []
    for f in fs:
        url = str(f.get("url", ""))
        local = (str(Path(_estate().command_root()) / "_scratch" / "hello" / "uploads")
                 + "/" + url.rsplit("/", 1)[-1]) if url else "?"
        parts.append(f"{f.get('name','?')} ({f.get('mime','?')}, {int(f.get('size') or 0)//1024} KB) -> READ {local}")
    return "  [ATTACHED: " + "; ".join(parts) + "]"


def _fmt(rs):
    return "\n".join(f"  #{r.get('n')} {str(r.get('ts',''))[:16]} owner: {str(r.get('text',''))[:600]}{_files(r)}" for r in rs[-MAX_ROWS:])


def _claude_pid():
    """PID of the claude process this hook runs under (walk the parent chain; 0 = not found).
    One CIM call, paid only at CLAIM time — never on the per-prompt path."""
    try:
        import subprocess
        ps = ("$p=%d; while($p){$pr=Get-CimInstance Win32_Process -Filter \"ProcessId=$p\";"
              "if(-not $pr){break}; if($pr.Name -match 'claude'){$pr.ProcessId; break};"
              "$p=$pr.ParentProcessId}" % os.getppid())
        out = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                             capture_output=True, text=True, timeout=15).stdout.strip()
        return int(out) if out.isdigit() else 0
    except Exception:
        return 0


def _pid_alive(pid):
    """True when the process still runs; True also for pid 0/unknown (fall back to heartbeat).
    A -p responder EXITS after its one answer and nothing clears mod.json (seen 2026-09-02
    #1018/#1023: ghost seat blocked relaunch for the whole 15-min TTL) — probing the stored
    pid is what turns 'heartbeat not yet expired' into 'actually alive'."""
    if not pid:
        return True
    try:
        import ctypes
        k = ctypes.windll.kernel32
        h = k.OpenProcess(0x1000, False, int(pid))   # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return False
        code = ctypes.c_ulong()
        ok = k.GetExitCodeProcess(h, ctypes.byref(code))
        k.CloseHandle(h)
        return bool(ok) and code.value == 259        # STILL_ACTIVE
    except Exception:
        return True


def _moderator():
    m = _jload(MOD, {})
    alive = (bool(m.get("session_id"))
             and (time.time() - float(m.get("heartbeat") or 0)) < MOD_TTL
             and _pid_alive(m.get("pid")))
    return m, alive


def _invalidate_fresh(sid):
    previous = _jload(MOD, {})
    if previous.get("session_id") == sid:
        previous["harness_freshness"] = {"ok": False}
        previous["heartbeat"] = 0
        _jsave(MOD, previous)


def _claim_fresh(sid: str, cwd: str):
    """All automatic claim paths share the same admission door as any CLI."""
    try:
        root = Path(_estate().command_root())
        sys.path.insert(0, str(root))
        from tools.harness_freshness import ensure_fresh
        freshness = ensure_fresh(root=root, apply=True)
        if not freshness["ok"]:
            _invalidate_fresh(sid)
            print("[ECHELON] Moderator claim refused: harness freshness unverified. " +
                  json.dumps(freshness, ensure_ascii=False), file=sys.stderr)
            return False
        _jsave(MOD, {"session_id": sid, "cwd": cwd, "since": time.strftime("%Y-%m-%d %H:%M:%S"),
                     "heartbeat": time.time(), "pid": _claude_pid(), "harness_freshness": freshness})
        if _jload(MOD, {}).get("session_id") != sid:
            raise RuntimeError("moderator seat write did not persist")
        return True
    except Exception as exc:
        _invalidate_fresh(sid)
        print(f"[ECHELON] Moderator claim refused: {type(exc).__name__}: {exc}", file=sys.stderr)
        return False


def _seat_role() -> str:
    """The role the launcher gave this seat (ECHELON_SEAT_ROLE: orchestrator|builder|moderator|...),
    '' when the session was not launched through a role-aware door."""
    return (os.environ.get("ECHELON_SEAT_ROLE") or "").strip().lower()


def _role(sid: str, cwd: str, *, revalidate=False):
    """moderator | worker. ECHELON_MODERATOR=1 (launcher) claims the seat when it is free or stale.
    An explicit launched role (ECHELON_SEAT_ROLE != moderator) is a worker, full stop (owner #4780)."""
    m, alive = _moderator()
    if sid and m.get("session_id") == sid and _seat_role() not in ("", "moderator"):
        return "worker"                              # seat file points at a launched non-moderator: never honour it
    if sid and m.get("session_id") == sid:
        if revalidate or not alive or not m.get("harness_freshness", {}).get("ok"):
            return "moderator" if _claim_fresh(sid, cwd) else "worker"
        return "moderator"
    # the launcher's env var did not reach the hook once (2026-08-30 12:00, seat stayed in fallback) -> also
    # accept a claim-intent file the launcher writes: ~/.echelon/moderator.claim (consumed on first start)
    intent = ECH / "moderator.claim"
    # a LAUNCHED seat (orchestrator / builder: ECHELON_WORKER_ID or ECHELON_RUN_ID set) is a worker by
    # birth and never claims the moderator seat, whatever env it inherited (2026-09-05 seat theft x2)
    launched = bool(os.environ.get("ECHELON_WORKER_ID") or os.environ.get("ECHELON_RUN_ID"))
    if launched and not (sid and m.get("session_id") == sid):
        return "worker"
    # an EXPLICIT non-moderator seat role (runs.py --role -> ECHELON_SEAT_ROLE in the launch .cmd,
    # owner #4780 2026-09-08) is a worker whatever the seat state or the cwd: an Opus ORCHESTRATOR
    # standing in the HQ room must never fall through to the stale-seat fallback below.
    if _seat_role() not in ("", "moderator"):
        return "worker"
    if (os.environ.get("ECHELON_MODERATOR") == "1" or intent.exists()) and sid and not alive:
        if not _claim_fresh(sid, cwd):
            return "worker"
        try:
            intent.unlink()
        except Exception:
            pass
        return "moderator"
    if alive:
        return "worker"                              # seat taken by a live other session -> worker
    # nobody alive (owner 2026-09-04, after every builder seat announced itself moderator on a dead
    # seat): the fallback no longer makes EVERY session a moderator. Only a session standing in the
    # HQ room (echelon) claims the stale seat; every other cwd (worktrees, other estates) is a worker.
    # owner #4780 (2026-09-08): "standing in the HQ room" means the HQ ROOT itself. An isolated
    # candidate tree under it (_scratch/cc-extract, an orchestrator launched before the seat-role
    # stamp existed) is a workspace, not the HQ desk, and never inherits the seat.
    room, room_path = _room_for(cwd)
    if sid and room == "echelon" and _is_room_root(cwd, room_path):
        return "moderator" if _claim_fresh(sid, cwd) else "worker"
    return "worker"


def _is_room_root(cwd: str, room_path) -> bool:
    """True when cwd IS the room's root directory (the parent of its .echelon), not a subtree."""
    try:
        if not room_path:
            return False
        return os.path.normcase(os.path.abspath(cwd or "")) ==             os.path.normcase(os.path.abspath(os.path.dirname(str(room_path))))
    except Exception:
        return False


def _heartbeat(sid: str):
    m = _jload(MOD, {})
    if m.get("session_id") == sid:
        m["heartbeat"] = time.time()
        _jsave(MOD, m)


def _touch_session(sid: str, room: str, cwd: str, role: str):
    if not sid:
        return
    p = SESS_DIR / f"{sid}.json"
    s = _jload(p, {"session_id": sid, "started": time.strftime("%Y-%m-%d %H:%M:%S")})
    s.update({"room": room, "cwd": cwd, "role": role, "last_seen": time.strftime("%Y-%m-%d %H:%M:%S")})
    if _seat_role():
        s["seat_role"] = _seat_role()                # the launcher's explicit role, for status / war
    _jsave(p, s)


def _room_tag(text: str) -> str:
    t = (text or "").lstrip()
    if t.startswith("[") and "]" in t[:40]:
        return t[1:t.index("]")].strip().lower()
    return ""


def _inbox_notes(room_path, seen_ids, sid=""):
    """Unread notes routed into <room>/inbox by the moderator (or by tools/moderator.py route).
    Owner #932 (2026-09-02): delivery is stamped ON THE NOTE (`delivered: [{session, ts}]`) — before,
    the only record lived in the reading session's private board_cursor, so /war could not tell an
    order nobody has seen from one a dead session swallowed."""
    if not room_path:
        return []
    d = Path(room_path) / "inbox"
    out = []
    try:
        for f in sorted(d.glob("*.json")):
            n = _jload(f, {})
            if n.get("id") and n["id"] not in seen_ids:
                out.append(n)
                try:
                    n.setdefault("delivered", []).append({"session": (sid or "")[:8], "ts": time.strftime("%Y-%m-%d %H:%M:%S")})
                    f.write_text(json.dumps(n, ensure_ascii=False, indent=1), encoding="utf-8")
                except Exception:
                    pass
    except Exception:
        pass
    return out


def _mirror_turn(ev, sid, room, cur, cp):
    """TERMINAL -> BOARD mirror (owner 2026-09-02: 'i replied on web are the same like i replied
    in terminal' — the conversation must be one surface). At Stop, post a digest of the turn's
    final assistant text as a [room] TURN row so the command center shows the terminal side live.
    TURN rows are transparent to the answer-gate (board.py) and rate-limited per session; never
    blocks, never raises."""
    try:
        tp = ev.get("transcript_path")
        if not tp or not os.path.isfile(tp):
            return
        if time.time() - float(cur.get("mirror_ts") or 0) < 60:
            return
        last = ""
        with open(tp, encoding="utf-8", errors="replace") as f:
            size = os.path.getsize(tp)
            f.seek(max(0, size - 2_000_000))
            for line in f:
                if '"assistant"' not in line:
                    continue
                try:
                    o = json.loads(line)
                except Exception:
                    continue
                m = o.get("message") or {}
                if o.get("type") == "assistant" or m.get("role") == "assistant":
                    txt = "".join(b.get("text", "") for b in (m.get("content") or [])
                                  if isinstance(b, dict) and b.get("type") == "text")
                    if txt.strip():
                        last = txt.strip()
        digest = " ".join(last.split())[:500]
        if not digest or cur.get("mirror_last") == digest:
            return
        _post(f"[{room}] TURN {sid[:8]}: {digest}")
        cur["mirror_ts"] = time.time()
        cur["mirror_last"] = digest
        _jsave(cp, cur)
    except Exception:
        pass


def main():
    mode = (sys.argv[1] if len(sys.argv) > 1 else "prompt").strip()
    try:
        ev = json.load(sys.stdin)
    except Exception:
        ev = {}
    sid = str(ev.get("session_id") or "")
    cwd = ev.get("cwd") or os.getcwd()
    room, room_path = _room_for(cwd)
    role = _role(sid, cwd, revalidate=mode == "start")
    # owner 2026-09-04: "add a return on top of SessionStart: if the moderator seat is taken, return".
    # A worker session says nothing at start (no announcement row, no duties banner); its prompt
    # door still delivers rows addressed to its room, and it is never blocked at stop.
    if mode == "start" and role == "worker":
        _touch_session(sid, room, cwd, role)
        cp0 = CUR_DIR / f"{sid or 'nosession'}.json"
        c0 = _jload(cp0, {})
        c0.update({"seen": len(_rows()), "room": room, "notes": []})
        _jsave(cp0, c0)
        return
    rows = _rows()
    cp = CUR_DIR / f"{sid or 'nosession'}.json"
    cur = _jload(cp, {})
    _touch_session(sid, room, cwd, role)
    if role == "moderator":
        _heartbeat(sid)
    if mode == "stop":
        _mirror_turn(ev, sid, room, cur, cp)

    # ── MODERATOR: the whole board, and the stop-block ────────────────────────────
    if role == "moderator":
        if mode == "start":
            recent = [r for r in rows if r.get("from") == "owner" and time.time() - _ts(r) < 6 * 3600]
            n = _post(f"[{room}] MODERATOR session {sid[:8] or 'no-id'} on duty in {cwd} - watching this board; owner rows block my stop until answered.")
            # the announcement is NOT an answer: keeping it out of last_post_n is what stops a
            # responder from booting, posting "on duty", and exiting past an owner row it never
            # answered (#1017/#1018, 2026-09-02).
            cur.update({"seen": len(rows) + (1 if n else 0), "last_post_n": 0, "announce_n": n or 0, "room": room})
            _jsave(cp, cur)
            print(f"[18899 board] You are the MODERATOR (announced as #{n}). Duties: answer the owner here; keep War/Rulings truthful; ROUTE owner rows "
                  f"addressed to another room with `python {Path(_estate().command_root()) / 'tools' / 'moderator.py'} route --room <room> --n <board n>`; an unanswered owner row BLOCKS your stop. "
                  f"Post: curl -s -X POST {API} -H 'Content-Type: application/json' --data-binary '{{\"from\":\"claude\",\"text\":\"[{room}] ...\"}}'")
            if recent:
                print(f"[18899 board] recent OWNER rows (last 6h, may be unanswered):\n{_fmt(recent)}")
            return
        if mode == "prompt":
            if "seen" not in cur:
                cur["seen"] = len([r for r in rows if time.time() - _ts(r) >= 2 * 3600])
            fresh = [r for r in rows[int(cur.get("seen") or 0):] if r.get("from") == "owner"]
            if fresh:
                print(f"[18899 board] UNREAD OWNER rows (you are MODERATOR - answer on the board, tag [{room}]; route room-addressed ones with tools/moderator.py route):\n{_fmt(fresh)}")
            cur["seen"] = len(rows)
            _jsave(cp, cur)
            return
        if mode == "stop":
            if ev.get("stop_hook_active"):
                return
            last_post = int(cur.get("last_post_n") or 0)
            ann = int(cur.get("announce_n") or 0)
            mine = [int(r.get("n") or 0) for r in rows if r.get("from") == "claude" and r.get("n")
                    and int(r.get("n") or 0) != ann and _room_tag(r.get("text")) == room.lower()]
            if mine:
                last_post = max(last_post, max(mine))
            pending = [r for r in rows if r.get("from") == "owner" and int(r.get("n") or 0) > last_post
                       and int(r.get("n") or 0) > int(cur.get("blocked_n") or 0)]
            if not pending:
                return
            cur["blocked_n"] = max(int(r.get("n") or 0) for r in pending)
            cur["seen"] = len(rows)
            _jsave(cp, cur)
            print(json.dumps({"decision": "block",
                              "reason": f"You are the MODERATOR and the owner wrote on the 18899 board after your last board post. Answer on the board now (POST {API} {{from:claude,text:'[{room}] ...'}}), then you may stop:\n{_fmt(pending)}"}))
            return
        return

    # ── WORKER: only what is addressed to this room; never blocked ─────────────────
    if mode == "stop":
        return
    m, _ = _moderator()
    if mode == "start":
        cur.update({"seen": len(rows), "room": room, "notes": []})
        _jsave(cp, cur)
        print(f"[18899 board] role: WORKER in room {room} (moderator session {str(m.get('session_id',''))[:8]} watches the board). "
              f"Owner rows tagged [{room}] and routed inbox notes reach you at each prompt; you are not blocked at stop. "
              f"Report through the board with the [{room}] prefix when a door closes: POST {API}.")
        return
    if mode == "prompt":
        if "seen" not in cur:
            cur["seen"] = len([r for r in rows if time.time() - _ts(r) >= 2 * 3600])
        seen = int(cur.get("seen") or 0)
        mine = [r for r in rows[seen:] if r.get("from") == "owner" and _room_tag(r.get("text")) == room.lower()]
        # ROOM STATUS updates (owner 2026-09-01): system rows for THIS room — items
        # opened/closed, claims taken/released — reach the room's workers each prompt.
        status = [r for r in rows[seen:] if r.get("from") == "system" and _room_tag(r.get("text")) == room.lower()]
        seen_ids = set(cur.get("notes") or [])
        notes = _inbox_notes(room_path, seen_ids, sid)
        if mine:
            print(f"[18899 board] OWNER rows addressed to [{room}]:\n{_fmt(mine)}")
        if status:
            print(f"[{room} status] room updates since your last prompt:\n" + "\n".join(
                f"  #{r.get('n')} {str(r.get('ts',''))[:16]} {str(r.get('text',''))[:300]}" for r in status[-MAX_ROWS:]))
        if notes:
            print(f"[{room} inbox] notes routed by the moderator:\n" + "\n".join(
                f"  {n.get('id')} {str(n.get('ts',''))[:16]} {n.get('from','owner')}: {str(n.get('text',''))[:600]}" for n in notes[-MAX_ROWS:]))
            cur["notes"] = sorted(seen_ids | {n["id"] for n in notes})[-500:]
        cur["seen"] = len(rows)
        _jsave(cp, cur)
        return


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
