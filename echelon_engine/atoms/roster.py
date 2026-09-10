"""Roster — the OpenRouter free-tier weather report (owner law 2026-09-01).

"For openrouter, you must refresh the roster every-day. there would be a new
free tier, or past free tier that retired." The free roster is WEATHER, not
ground: a routing pick that names a retired free model would 404 forever, and
a new free seat nobody notices is money left on the table.

refresh() pulls /api/v1/models, keeps the FREE ids (prompt+completion price 0),
and writes ~/.echelon/openrouter-roster.json with arrivals/retirements vs the
previous snapshot AND which of routing's OpenRouter picks retired. It rides the
daily backup task beside the liveness drain — report-only there (the routing
table is DECLARED; a retirement falls through the chain via routing.model_chain,
it never rewrites the table silently).

Severable: any failure returns {"ok": False} and never blocks the daily task.
"""
from __future__ import annotations

import json
import re
import time
import urllib.request
from pathlib import Path

ROSTER_PATH = Path.home() / ".echelon" / "openrouter-roster.json"
API = "https://openrouter.ai/api/v1/models"
FRESH_SECONDS = 48 * 3600   # roster older than this is STALE: consumers must not trust it


def _free_ids(models: list[dict]) -> list[str]:
    out = []
    for m in models:
        p = m.get("pricing") or {}
        try:
            if float(p.get("prompt") or 1) == 0 and float(p.get("completion") or 1) == 0:
                out.append(str(m.get("id") or ""))
        except (TypeError, ValueError):
            continue
    return sorted(i for i in out if i)


def load() -> dict:
    """The last snapshot, or {} — callers must treat a missing/stale roster as
    'no opinion' (degrade OPEN: never drop a model because the weather report is old)."""
    try:
        return json.loads(ROSTER_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def is_fresh(snapshot: dict | None = None) -> bool:
    s = snapshot if snapshot is not None else load()
    return bool(s.get("ts")) and (time.time() - s["ts"]) < FRESH_SECONDS


def free_set(snapshot: dict | None = None) -> set[str]:
    s = snapshot if snapshot is not None else load()
    return set(s.get("free") or [])


def refresh() -> dict:
    """Pull the live roster, diff against the previous snapshot, persist, report."""
    try:
        prev = load()
        req = urllib.request.Request(API, headers={"User-Agent": "echelon-roster/1"})
        with urllib.request.urlopen(req, timeout=30) as r:
            models = json.loads(r.read()).get("data") or []
        free = _free_ids(models)
        prev_free = set(prev.get("free") or [])
        arrived = sorted(set(free) - prev_free) if prev_free else []
        retired = sorted(prev_free - set(free)) if prev_free else []

        # Which of routing's OpenRouter picks (any id with a '/') just fell off the roster?
        retired_picks = []
        try:
            from . import routing
            for role in list(routing._ROLES):
                for m in routing.model_chain(role):
                    if "/" in m and m not in free:
                        retired_picks.append({"role": role, "model": m})
        except Exception:
            pass

        snap = {"ts": int(time.time()), "ran": time.strftime("%Y-%m-%d %H:%M:%S"),
                "count": len(free), "free": free,
                "arrived": arrived, "retired": retired,
                "retired_routing_picks": retired_picks, "ok": True}
        ROSTER_PATH.parent.mkdir(parents=True, exist_ok=True)
        ROSTER_PATH.write_text(json.dumps(snap, indent=1), encoding="utf-8")
        return snap
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


# ---------------------------------------------------------------------------
# SEAT HEALTH — the probe (R-0171 unit D, owner-ruled 2026-09-09)
#
# "A free seat that is LISTED is not a free seat that ANSWERS." The roster above
# is a CATALOGUE fact (OpenRouter says this id costs nothing today). Answering is
# a LIVE fact, and the 2026-09-09 council trace proved they are different things:
# five listed seats cost 408 s and two answered — one had no provider wired and
# failed in 2 ms, one was rate-limited, one hung 302 s (a bare id, roster-ungoverned,
# with no per-attempt timeout), and two leaked their reasoning into the answer.
#
# probe() sends ONE tiny real request to every id a chain would use and writes
# ~/.echelon/seat-health.json. routing.model_chain reads it and SKIPS a seat whose
# FRESH health says it did not answer (or answered in the wrong shape), exactly the
# way it skips a retired ':free' id. Missing or stale health = no opinion (degrade
# OPEN), never a crash.
# ---------------------------------------------------------------------------

HEALTH_PATH = Path.home() / ".echelon" / "seat-health.json"
HEALTH_FRESH_SECONDS = 3600          # 1 h: seat weather turns over fast (rate limits, capacity)
PROBE_PROMPT = "Reply with exactly one word: OK"
PROBE_MAX_TOKENS = 16
_ERROR_EXCERPT = 200                 # bounded, so a provider error body can never dump a secret

# The reasoning leak the 2026-09-09 trace showed: a model that emits its chain of
# thought INTO `content` instead of answering. Detected by the markers such a reply
# carries, not by length alone (a short honest answer is fine).
_LEAK_MARKERS = ("<think", "</think", "we need to", "we must", "let me think",
                 "okay, the user", "the user says", "the user wants", "first, i",
                 "reasoning:")


def _redact(text: str) -> str:
    """Bound an error excerpt AND strip anything key-shaped. An error body is
    provider-controlled text; it must never carry a credential into a file."""
    t = " ".join(str(text or "").split())
    t = re.sub(r"(sk-|nvapi-|xai-|gsk_)[A-Za-z0-9_\-]{8,}", "<redacted>", t)
    t = re.sub(r"(?i)(bearer\s+)[A-Za-z0-9_\-\.]{8,}", r"\1<redacted>", t)
    return t[:_ERROR_EXCERPT]


def classify_shape(resp: object, max_tokens: int = PROBE_MAX_TOKENS) -> str:
    """ok | empty | leaked-reasoning | truncated — what SHAPE did this seat answer in?

    A seat can return HTTP 200 and still be useless to a council: it can answer with
    nothing, dump its chain of thought where the answer belongs, or get cut at the
    token cap before it concludes. Those are the three failures the trace witnessed."""
    content = (getattr(resp, "content", "") or "").strip()
    if not content:
        return "empty"
    low = content.lower()
    if any(mk in low for mk in _LEAK_MARKERS):
        return "leaked-reasoning"
    raw = getattr(resp, "raw", None) or {}
    try:
        finish = (raw.get("choices") or [{}])[0].get("finish_reason")
    except (AttributeError, IndexError, TypeError):
        finish = None
    if finish == "length":
        return "truncated"
    # No finish_reason from this wire: fall back on the token count against the cap.
    if finish is None and getattr(resp, "tokens_out", 0) >= max_tokens:
        return "truncated"
    return "ok"


def _probe_one(model_id: str, timeout_s: float, send=None) -> dict:
    """One seat, one tiny request, wall-clock timed. NEVER raises: a seat that blows
    up is a REPORTED dead seat, not a dead probe (the whole point is that one bad seat
    cost the council 302 s)."""
    started = time.time()
    try:
        if send is None:
            from . import routing
            provider = routing.provider_for(model_id)
            def send(mid):
                return provider.send([{"role": "user", "content": PROBE_PROMPT}],
                                     model_id=mid, max_tokens=PROBE_MAX_TOKENS,
                                     timeout=timeout_s)
        resp = send(model_id)
        latency_ms = int((time.time() - started) * 1000)
        status = getattr(resp, "status", "success")
        if status != "success":
            return {"answered": False, "latency_ms": latency_ms, "shape": "error",
                    "error": _redact(getattr(resp, "content", "") or status)}
        shape = classify_shape(resp)
        return {"answered": True, "latency_ms": latency_ms, "shape": shape, "error": ""}
    except Exception as e:
        return {"answered": False, "latency_ms": int((time.time() - started) * 1000),
                "shape": "error", "error": _redact(f"{type(e).__name__}: {e}")}


def chain_ids(role: str) -> list[str]:
    """The probe's target set for a role: the chain as the TABLE DECLARES it.

    Deliberately the declared chain and NOT model_chain(), which is already filtered by
    the health file a previous probe wrote. Probing the filtered chain would be a ratchet
    that only ever re-probes survivors, so a seat marked dead once could never come back —
    and these seats fail on WEATHER (rate limits, capacity), which clears. A probe must be
    able to bring a seat back to life, not just kill it."""
    try:
        from . import routing
        return list(routing.declared_chain(role))
    except Exception:
        return []


def probe(ids: list[str], timeout_s: float = 20.0, send=None) -> dict:
    """Probe every id, MERGE into ~/.echelon/seat-health.json, return the new snapshot.

    Merged, not replaced: probing one role's chain must not blank another role's seats.
    Each id carries its own probed_at, so freshness is per seat."""
    seats = dict((load_health().get("seats") or {}))
    for mid in ids:
        if not mid:
            continue
        rec = _probe_one(mid, timeout_s, send=send)
        rec["probed_at"] = int(time.time())
        seats[mid] = rec
    snap = {"ts": int(time.time()), "ran": time.strftime("%Y-%m-%d %H:%M:%S"),
            "timeout_s": timeout_s, "seats": seats, "ok": True}
    try:
        HEALTH_PATH.parent.mkdir(parents=True, exist_ok=True)
        HEALTH_PATH.write_text(json.dumps(snap, indent=1), encoding="utf-8")
    except OSError as e:
        snap = dict(snap, ok=False, error=f"{type(e).__name__}: {e}")
    return snap


def load_health() -> dict:
    try:
        return json.loads(HEALTH_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def seat_health(model_id: str, snapshot: dict | None = None) -> dict | None:
    """This seat's FRESH health record, or None when there is no fresh opinion.

    None is the degrade-OPEN answer and means exactly 'do not skip on my account':
    no file, no record, or a record older than HEALTH_FRESH_SECONDS all return None."""
    s = snapshot if snapshot is not None else load_health()
    rec = (s.get("seats") or {}).get(model_id)
    if not isinstance(rec, dict):
        return None
    at = rec.get("probed_at") or s.get("ts")
    if not at or (time.time() - at) >= HEALTH_FRESH_SECONDS:
        return None
    return rec


def is_dead(model_id: str, snapshot: dict | None = None) -> bool:
    """Does FRESH seat health say this id will not usefully answer? (answered=false,
    or answered in a shape a caller cannot use). No fresh opinion -> False."""
    rec = seat_health(model_id, snapshot)
    if rec is None:
        return False
    return (not rec.get("answered")) or rec.get("shape") != "ok"


def _probe_main(argv: list[str]) -> int:
    """`echelon roster probe [--role R] [--ids a,b] [--timeout-s N]` — send ONE tiny
    real request to every seat a chain would use and record what came back."""
    import argparse
    ap = argparse.ArgumentParser(prog="echelon roster probe")
    ap.add_argument("--role", help="probe every id in this role's model_chain")
    ap.add_argument("--ids", help="comma-separated model ids to probe (overrides --role)")
    ap.add_argument("--timeout-s", type=float, default=20.0,
                    help="per-attempt cap in seconds (default 20; the trace's hang was 302 s)")
    a = ap.parse_args(argv)

    if a.ids:
        ids = [i.strip() for i in a.ids.split(",") if i.strip()]
    elif a.role:
        ids = chain_ids(a.role)
    else:
        print("roster probe: give --role <role> or --ids a,b")
        return 2
    if not ids:
        print(f"roster probe: no ids to probe (role={a.role!r} resolved to an empty chain)")
        return 1

    print(f"probing {len(ids)} seat(s), {a.timeout_s:g}s cap each ...")
    snap = probe(ids, timeout_s=a.timeout_s)
    for mid in ids:
        r = (snap.get("seats") or {}).get(mid) or {}
        mark = "OK " if r.get("answered") and r.get("shape") == "ok" else "DEAD"
        line = f"  {mark} {mid:<44} {r.get('latency_ms', 0):>7} ms  shape={r.get('shape')}"
        if r.get("error"):
            line += f"  err={r['error']}"
        print(line)
    live = [m for m in ids if not is_dead(m, snap)]
    print(f"answered-and-usable: {len(live)}/{len(ids)} -> {', '.join(live) or '(none)'}")
    print(f"written: {HEALTH_PATH}")
    if not snap.get("ok"):
        print(f"WARNING: seat-health not persisted: {snap.get('error')}")
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = list(argv or [])
    if argv and argv[0] == "probe":
        return _probe_main(argv[1:])
    snap = refresh()
    if not snap.get("ok"):
        print(f"roster refresh FAILED: {snap.get('error')}")
        return 1
    print(f"openrouter free roster: {snap['count']} models "
          f"(+{len(snap['arrived'])} arrived, -{len(snap['retired'])} retired)")
    for rp in snap["retired_routing_picks"]:
        print(f"  !! routing pick RETIRED from the free roster: "
              f"{rp['role']} -> {rp['model']} (chain will skip it while the roster is fresh)")
    for m in snap["arrived"][:10]:
        print(f"  + new free seat: {m}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
