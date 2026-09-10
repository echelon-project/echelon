"""relive — RESUME a session as a CHAIN OF WEIGHT, not a context injection (built 2026-06-17; promoted from
eval/ into the real package 2026-06-17 — it holds a real schema (relive_evidence.db) + API that
/wrap-session imports, so it is production, not a proof).

The owner's design (the clean loop): warm-up writes the estate INTO you for a genuinely NEW session;
wrap writes the session OUT; relive brings you back IN on a fresh context window to resume the SAME
work — without re-running warm-up and without re-injecting the noisy transcript.

THE INVERSION (owner correction): the CHAIN is primary, the conversation is SUPPORTING EVIDENCE.
- WRONG (retrieval): conversation -> distill -> atoms -> recall the atoms. That makes the talk the source.
- RIGHT (memory): you recall the EXPERIENCE (the chain of weight-bearing moves the session earned), and
  THEN the conversation comes back, anchored to each move — like a human remembers. You relive what you
  DID; the words return attached to the moments. The talk rides up WITH the chain, it is not queried.

So this is RELIVE, not resume (relive-dont-migrate, think-is-compose-and-replay): you do not RESTORE a
record (the cache-lie in a new mask) — you re-walk the earned chain so the weight re-forms, and each move
surfaces the exchange that earned it. Noise (tool dumps, dead ends, giant file reads) was NEVER attached to
a move, so it never returns — noise-subtraction by construction.

Structure (no soul-schema change — the atom stays a clean weight-seed):
  - the CHAIN = a session's arc-CARD (ordered atom refs + the prev card->card edge). Built by wrap.
  - the EVIDENCE = a SIDECAR table (outside core_v2) keyed atom_id -> the exchange that earned it.
  - relive(card) = walk the chain in order -> re-fire each atom's warmth -> surface its evidence exchange.

The clean loop: wrap composes the arc-card + records evidence, THEN OFFERS relive. relive is the
consumer of what wrap produced.   Run: python -X utf8 -m echelon_engine.atoms.relive [--list] [card_id]
"""
from __future__ import annotations
import sys, sqlite3, time
from pathlib import Path

from .cards import CardStore


# ── compact handle routing (B4) ─────────────────────────────────────────
def _compact_relive(scope: str, handle: str) -> tuple[int, str]:
    """Try to resolve and consume a compact by handle. Returns (exit_code, output).
    exit_code 0 = success (consumed + printed), 1 = error (ambiguous/not found),
    -1 = not a compact handle (fall through to card relive)."""
    from echelon_engine.session_state import (
        resolve_compact_by_id, consume_compact_by_id, compact_id_for,
        compact_handle_status,
    )
    resolved = resolve_compact_by_id(scope, handle)
    if resolved is None:
        # Distinguish "already consumed this compact" from "not a compact handle at all"
        # so the user doesn't get a misleading "no card <handle>" for a handle they just used.
        if compact_handle_status(scope, handle) == "consumed":
            return 1, f"compact {handle} is already consumed (cheap relive is one-shot)."
        return -1, ""  # Not a compact — fall through to card relive
    if resolved.get("_ambiguous"):
        ids = [compact_id_for(m) for m in resolved["matches"]]
        return 1, f"ambiguous handle, matches {len(resolved['matches'])} compacts: {', '.join(ids)}"
    # Cheap relive — consume and print continuity
    consumed = consume_compact_by_id(scope, handle, consumed_by="relive")
    if consumed is None:
        return 1, "ERROR: compact could not be consumed (already taken or token mismatch)"
    cid = compact_id_for(consumed)
    continuity = consumed.get("continuity", "")
    meta = consumed.get("meta", {})
    goal = meta.get("current_goal", "") or ""
    lines = [f"=== RELIVE (cheap/quick — consumed compact {cid}) ==="]
    if goal:
        lines.append(f"Goal: {goal}")
    lines.append("")
    if continuity:
        lines.append(f"── Continuity block ({len(continuity)} chars) ──")
        lines.append(continuity)
    else:
        lines.append("(no continuity block)")
    return 0, "\n".join(lines)

# Sidecar lives BESIDE the soul, never IN it (never raw-write core_v2; the atom must stay a weight-seed).
EVIDENCE_DB = str(Path.home() / ".echelon" / "relive_evidence.db")


def _conn():
    c = sqlite3.connect(EVIDENCE_DB); c.row_factory = sqlite3.Row
    c.execute("""CREATE TABLE IF NOT EXISTS relive_evidence (
                   atom_coord TEXT, card_id TEXT, session TEXT,
                   ordinal INTEGER, exchange TEXT, ts REAL )""")
    c.execute("CREATE INDEX IF NOT EXISTS ix_ev_card ON relive_evidence(card_id, ordinal)")
    return c


# The governed session-arc filter: a card is a walkable session if its CARD_KIND code is in the
# session band (wrap/relive/resume/inferred). This REPLACES the fragile `born_from LIKE
# 'wrap-session%'` that silently missed 35 sessions when wrap wrote narrative born_froms. The
# born_from LIKE is kept as a belt-and-suspenders fallback for any pre-backfill row still at kind=0.
from ..registry import SESSION_ARC_CODES as _SESSION_CODES

_SESSION_KIND_SQL = "kind IN (" + ",".join(str(c) for c in _SESSION_CODES) + ")"
_LEGACY_BORN_SQL = "born_from LIKE 'wrap-session%' OR born_from LIKE 'relive%' " \
                   "OR born_from LIKE 'session-wrap%' OR born_from LIKE 'session_wrap%'"
_ARC_WHERE = f"(({_SESSION_KIND_SQL}) OR ({_LEGACY_BORN_SQL}))"


def recent_arc_cards(cs=None, limit=8, scope=None, before=None):
    """List recent session arc-cards (the ones wrap composes), newest first — so relive can find the
    latest session without the user memorising a card id. Filters on the governed CARD_KIND session
    band (kind 100-109), with the legacy born_from prefixes as a fallback for un-backfilled rows.

    When `scope` is given, restrict to sessions of THAT project — a card's scope is DERIVED from the
    scopes of the atoms it refs (cards carry no scope column; an arc-card's atoms are all one scope).
    This is what keeps the shared bank from clashing: `relive --list --scope alpha-app` shows only
    alpha-app sessions, not the flat pool where echelon/alpha-app/gamma-support/mol mix. Without scope, all
    projects' sessions are listed (the prior behaviour).

    `before` is a PAGINATION CURSOR — a card ts; only sessions strictly OLDER than it are returned, so
    `relive --list --before <cursor>` walks back through history a page at a time (the cursor for the
    next page is the `ts` of the last card shown). None = the newest page. Returns a list; each dict
    carries `ts` so the caller can use the last one as the next cursor."""
    import json as _json
    cs = cs or CardStore()
    out: list = []
    cursor = None if before is None else int(before)
    # Page through the arc-cards in ts-descending batches, resolving each card's scope, until we
    # have `limit` matches or run out. A fixed over-fetch multiplier under-delivered when a scope is
    # SPARSE in the recent pool (limit*8 rows might hold <limit echelon cards among many other
    # projects' — the limit-3-returned-2 bug); batching until satisfied is exact at any sparsity.
    batch = max(limit * 4, 32)
    while len(out) < limit:
        where = _ARC_WHERE
        params: list = []
        if cursor is not None:
            where = f"({_ARC_WHERE}) AND ts < ?"
            params.append(cursor)
        params.append(batch)
        rows = cs.conn.execute(
            f"SELECT id, label, refs, born_from, ts FROM cards WHERE {where} "
            "ORDER BY ts DESC LIMIT ?", params).fetchall()
        if not rows:
            break                          # exhausted history
        for r in rows:
            cursor = r["ts"]               # advance the cursor as we consume rows
            card_scope = cs.card_scope(_json.loads(r["refs"] or "[]"))
            if scope and card_scope != scope:
                continue
            out.append({"id": r["id"], "label": r["label"], "born_from": r["born_from"],
                        "ts": r["ts"], "scope": card_scope or "?"})
            if len(out) >= limit:
                break
        if len(rows) < batch:
            break                          # last batch was partial -> no more rows
    return out


def prior_arc_card(card_id, cs=None):
    """The arc-card of the session BEFORE this one — for back-tracing the relive (--reverse).
    Prefers the explicit `prev` card->card edge if wrap recorded one; otherwise falls back to the
    previous wrap-session card by timestamp (the natural session order). Returns the prior card row
    or None at the start of history. This is the move we kept doing by hand: relive a card, then
    walk to the session that PRECEDED it (the 'why' often lives one session back, not in the card
    you're on — two-witnesses / the MiroFish 'why it was the answer' both lived a hop earlier)."""
    cs = cs or CardStore()
    c = cs.card(card_id)
    if c is None:
        return None
    # 1) explicit prev edge (if this arc-card chained from the prior session's card)
    prev = getattr(c, "prev", "") or ""
    if prev:
        p = cs.card(prev)
        if p is not None:
            return {"id": p.id, "label": p.label, "born_from": p.born_from}
    # 2) fall back to the previous wrap-session card by timestamp
    row = cs.conn.execute("SELECT ts FROM cards WHERE id=?", (card_id,)).fetchone()
    if row is None:
        return None
    prior = cs.conn.execute(
        f"SELECT id, label, born_from FROM cards WHERE {_ARC_WHERE} "
        "AND ts < ? ORDER BY ts DESC LIMIT 1", (row["ts"],)).fetchone()
    if prior is None:
        return None
    return {"id": prior["id"], "label": prior["label"], "born_from": prior["born_from"]}


def next_arc_cards(card_id, cs=None):
    """The arc-card(s) of the session(s) that came AFTER this one — the mirror of prior_arc_card, for
    FORWARD-walking the relive (--forward). This is the fix for the fork blind-spot: --reverse only ever
    walks toward the PAST (one linear rope of prev-edges), so standing on a fork ROOT and asking to
    'continue' could never reach the SIBLING branch — a fork's two children both live in the root's
    FUTURE, on different forward lines. This returns ALL children (a fork has >1), so the caller can
    surface a branch MENU instead of silently picking one (no auto-pick on a fork — the honest-navigation
    law, corroboration over confidence, [[two-witnesses-caught-the-checker-being-wrong]]).

    A child is any wrap/relive arc-card that either (1) chained from this card via the explicit `prev`
    edge, or (2) declared it forked from this card via `forked_from=<id>` in born_from (recorded by wrap
    when a session was a /relive resume — an EXACT fork edge, not a timestamp guess). If neither exists,
    fall back to the single next wrap-card by timestamp (the linear successor). Newest-first within a fork."""
    cs = cs or CardStore()
    c = cs.card(card_id)
    if c is None:
        return []
    # 1) explicit children: prev-edge OR forked_from tag point back at this card
    rows = cs.conn.execute(
        f"SELECT id, label, born_from, ts FROM cards WHERE {_ARC_WHERE} "
        "AND (prev=? OR born_from LIKE ?) ORDER BY ts DESC",
        (card_id, f"%forked_from={card_id}%")).fetchall()
    children = [{"id": r["id"], "label": r["label"], "born_from": r["born_from"]}
                for r in rows if r["id"] != card_id]
    if children:
        return children
    # 2) fall back to the single next wrap-session card by timestamp (the linear successor)
    row = cs.conn.execute("SELECT ts FROM cards WHERE id=?", (card_id,)).fetchone()
    if row is None:
        return []
    nxt = cs.conn.execute(
        f"SELECT id, label, born_from FROM cards WHERE {_ARC_WHERE} "
        "AND ts > ? ORDER BY ts ASC LIMIT 1", (row["ts"],)).fetchone()
    return [{"id": nxt["id"], "label": nxt["label"], "born_from": nxt["born_from"]}] if nxt else []


def record_evidence(card_id, atom_coord, session, ordinal, exchange):
    """Anchor the supporting CONVERSATION (the exchange that earned a move) to an atom in the chain.
    `exchange` is the distilled human<->assistant exchange behind the move — NOT tool output, NOT the full
    transcript. Idempotent per (card_id, atom_coord)."""
    c = _conn()
    c.execute("DELETE FROM relive_evidence WHERE card_id=? AND atom_coord=?", (card_id, atom_coord))
    c.execute("INSERT INTO relive_evidence(atom_coord, card_id, session, ordinal, exchange, ts) "
              "VALUES (?,?,?,?,?,?)", (atom_coord, card_id, session, ordinal, exchange, time.time()))
    c.commit(); c.close()


def credit_arc(card_id, q=85.0, witness="", cs=None):
    """EARN THE SESSION ITSELF — the missing fourth weight (owner, 2026-06-19). The atom earns (core),
    the edges earn (bank), the files persist — but the ARC-CARD (the session as a unit) stayed flat at
    neutral: 28 sessions, all use_count 0. So the substrate could never answer 'which past session was
    load-bearing / worth reliving / earned its keep' — and with sessions un-weighted, the SELF (the
    accreted weight of which ways-of-working proved out) had no signal to compound. The cartridge thesis
    rests on exactly that experience-weight.

    The mechanism already existed — reinforce_card earns the card, flows §7-P2 credit to its atoms, AND
    propagates a decayed share back along the `prev` edge to the upstream session (the chain rule). relive
    just never CALLED it on the arc-card. This is that call, and it is EARN-BY-OUTCOME, never earn-by-read:
    reliving alone is reading (earning on read would be the warmth-laundering sin). The arc-card earns ONLY
    when the resumed work SUCCEEDED — q is the outcome (>50 = it paid off), witness is the trace receipt
    (source='trace', the honest provenance the redeem/laundering gates require). /wrap-session calls this
    when a /relive-resumed session proved out (it already knows it was a resume — the same place it stamps
    record_fork). Returns the reinforce_card result {ok, before, after}. See witnessed-door-built-warmup-
    is-the-kindle (same door, one layer up), the-card-layer-must-be-chained, reinforce_card §7-P2."""
    cs = cs or CardStore()
    if cs.card(card_id) is None:
        return {"ok": False, "reason": f"no arc-card {card_id}"}
    # stamp the witness into the history via source='trace' — the outcome is the earn, the witness the receipt.
    res = cs.reinforce_card(card_id, q, source="trace")
    res["witness"] = witness or "(unstamped)"
    return res


def record_fork(card_id, root_card_id, cs=None):
    """Stamp an EXACT fork edge: mark `card_id` as a child branch forked from `root_card_id`, so
    next_arc_cards(root) can find this sibling without a timestamp guess. Wrap calls this when the
    session being wrapped was a /relive resume of `root_card_id` (a branch off that root). Idempotent:
    appends ` | forked_from=<root>` to born_from only if not already present. Returns True if stamped."""
    cs = cs or CardStore()
    c = cs.card(card_id)
    if c is None or not root_card_id or root_card_id == card_id:
        return False
    bf = c.born_from or ""
    tag = f"forked_from={root_card_id}"
    if tag in bf:
        return False
    cs.conn.execute("UPDATE cards SET born_from=? WHERE id=?",
                    (f"{bf} | {tag}".strip(" |"), card_id))
    cs.conn.commit()
    return True


def evidence_for(card_id, atom_coord):
    c = _conn()
    r = c.execute("SELECT exchange, session FROM relive_evidence WHERE card_id=? AND atom_coord=?",
                  (card_id, atom_coord)).fetchone()
    c.close()
    return (r["exchange"], r["session"]) if r else (None, None)


def relive(card_id, cs=None, take_up=True, scope="echelon"):
    """RELIVE the chain THROUGH THE WITNESSED DOOR. Walk the arc-card's atoms IN ORDER; for each, serve it
    via the door — NOT by raw-reading content out of band (that was the dead-link bug this very arc fixed:
    a chain that hands you the payload around the engine never earns). The honest shape (owner, 2026-06-19):
    relive OFFERS a pointer + PEEK (spine: slug/claim/directive, free), and TAKES UP the move through
    `remember_fetch(depth='body')` — that fetch IS the witness, so re-walking the chain EARNS. Reliving is
    re-forming the moves, so take_up=True by default (that is what makes a relive a kindle); --peek-only
    surveys the spine for free without earning. The exchange that earned each move still anchors beneath it.
    Returns an ordered list of relived links."""
    cs = cs or CardStore()
    card = cs.card(card_id)
    if card is None:
        return None, f"no card {card_id}"
    links = []
    for i, coord in enumerate(card.refs):
        aid = cs.atom_id_for_coordinate(coord) or cs.atom_id_for_coordinate(coord.replace("echelon:", ""))
        # Compile the spine on demand for any legacy-uncompiled atom, so the door has something to serve.
        if aid and cs.recall_peek(aid) is None:
            cs.compile_atom_struct(aid)
        # THE DOOR: peek (free spine) when surveying; remember_fetch (witnessed, earns) when taking up.
        served = None
        if aid:
            served = (cs.remember_fetch(aid, depth="body") if take_up else cs.recall_peek(aid))
        if served:
            claim = served.get("claim", "")
            why = served.get("why", "")
            # the MOVE re-forms from the spine claim (+ its why on take-up) — served THROUGH the door
            move = claim + (("\n" + why) if why else "")
            warm = "earned" if take_up else "peek"
        else:
            move, warm = "(atom missing / no spine)", "?"
        exchange, sess = evidence_for(card_id, coord)
        links.append({"ordinal": i, "coord": coord, "move": move,
                      "warm": warm, "exchange": exchange, "session": sess})
    return links, None


def render(links, card_label):
    """Render the relived chain for a resuming session: the EXPERIENCE first (the moves, in order), each
    with its supporting conversation anchored beneath. Not a transcript — a re-lived arc."""
    n_earned = sum(1 for lk in links if lk["warm"] == "earned")
    door = (f"served through the witnessed door — {n_earned} move(s) taken up + earned"
            if n_earned else "peeked (free spine survey — no earn)")
    out = [f"=== RELIVE: {card_label} ===",
           f"(the chain of weight you earned; {door}; the conversation returns anchored to each move)\n"]
    for lk in links:
        head = lk["move"].splitlines()[0][:110]
        out.append(f"[{lk['ordinal']}] {lk['coord'].split(':')[-1]}   (door: {lk['warm']})")
        out.append(f"    MOVE: {head}")
        if lk["exchange"]:
            out.append(f"    ↳ when: {lk['exchange'].strip()[:400]}")
        else:
            out.append("    ↳ (no anchored exchange recorded)")
        out.append("")
    return "\n".join(out)


def _main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(description="Relive a session arc-card as a chain of weight.")
    ap.add_argument("card_id", nargs="?", help="arc-card id; omit with --list to discover recent sessions")
    ap.add_argument("--list", action="store_true", help="list recent session arc-cards, newest first")
    ap.add_argument("--reverse", nargs="?", type=int, const=0, default=None, metavar="N",
                    help="after reliving this card, keep BACK-TRACING prior sessions' arc-cards and "
                         "relive each — bare --reverse walks ALL the way back (auto, your freedom to "
                         "relive); --reverse N caps it at N hops. The 'why' often lives a hop back.")
    ap.add_argument("--forward", nargs="?", type=int, const=0, default=None, metavar="N",
                    help="after reliving this card, walk FORWARD to the session(s) that came AFTER it — "
                         "the mirror of --reverse. On a FORK (>1 child) it prints a branch MENU and STOPS "
                         "(no auto-pick — you choose which branch to relive). bare --forward walks the "
                         "linear future; --forward N caps at N hops. Use this to cross from a fork root to "
                         "a sibling branch (--reverse can't reach a sibling: it only walks toward the past).")
    ap.add_argument("--scope", default="echelon", help="the project scope. --list restricts to "
                    "this project's sessions (derived from each card's refs' atom scope); pass "
                    "--all-scopes to see every project's sessions in one list.")
    ap.add_argument("--all-scopes", action="store_true",
                    help="--list shows sessions across ALL projects (the shared-bank flat pool)")
    ap.add_argument("--before", type=int, default=None, metavar="TS",
                    help="pagination cursor: --list shows sessions OLDER than this ts (use the "
                         "'next page' cursor printed at the bottom of the previous --list)")
    ap.add_argument("--limit", type=int, default=8, help="how many sessions per --list page (default 8)")
    ap.add_argument("--peek-only", action="store_true",
                    help="survey the chain through a FREE spine peek WITHOUT earning (recall_peek, not the "
                         "witnessed door). Default relive TAKES UP each move via remember_fetch — that fetch "
                         "is the witness, so re-walking the chain earns (a relive IS a kindle).")
    ap.add_argument("--credit", nargs="?", type=float, const=85.0, default=None, metavar="Q",
                    help="EARN THE SESSION ITSELF by outcome (not by reading). Call this when a /relive-"
                         "resumed session SUCCEEDED: it reinforces the arc-card (Q in [0,100], default 85), "
                         "flows §7-P2 credit to its atoms, and propagates back along prev to the upstream "
                         "session. The missing fourth weight — the self earns. Earn-by-OUTCOME only.")
    ap.add_argument("--witness", default="", help="(--credit) the trace receipt proving the session paid off")
    a = ap.parse_args(argv)
    cs = CardStore()
    if a.credit is not None and a.card_id:
        res = credit_arc(a.card_id, q=a.credit, witness=a.witness, cs=cs)
        print(f"CREDIT arc-card {a.card_id}: ok={res.get('ok')}  "
              f"{res.get('before')} -> {res.get('after')}  (q={a.credit}, witness={res.get('witness')})")
        return 0 if res.get("ok") else 1
    if a.list or not a.card_id:
        list_scope = None if a.all_scopes else a.scope
        scope_note = " (all projects)" if a.all_scopes else f" in scope '{a.scope}'"
        print(f"recent session arc-cards{scope_note} (newest first) — relive one with its id:\n")
        arcs = recent_arc_cards(cs, limit=a.limit, scope=list_scope, before=a.before)
        if not arcs:
            if a.before:
                print("  (no older sessions — start of history)")
            elif list_scope:
                print(f"  (none in scope '{list_scope}' — try --all-scopes for every project)")
        for c in arcs:
            print(f"  {c['id']}  [{c.get('scope','?')}]  {c['label']}")
        # pagination cursor: the ts of the last card shown → walk back with --before <cursor>.
        if len(arcs) >= a.limit:
            nxt = arcs[-1]["ts"]
            more = " --all-scopes" if a.all_scopes else (f" --scope {a.scope}" if a.scope else "")
            print(f"\n  … older → relive --list --before {nxt}{more}")
        # B3: also surface unconsumed compacts — the CHEAP/quick continuity, same menu, by handle.
        from echelon_engine.session_state import list_unconsumed_compacts
        compacts = list_unconsumed_compacts(a.scope)
        if compacts:
            print("\nunconsumed compacts (cheap/quick continuity — relive one with its handle):\n")
            for c in compacts:
                goal = c.get("goal") or "(no goal)"
                ests = ",".join(Path(e).name for e in (c.get("target_estates") or []) if e) or "-"
                print(f"  {c['compact_id']}  [compact·cheap]  {goal[:60]}  ·  {c['ts'][:19]}  ·  {ests}")
        return 0

    # B4: a handle may name a COMPACT (cheap relive) rather than a card — try that first,
    # fall through to the chain-walk only if it is not a compact handle.
    code, out = _compact_relive(a.scope, a.card_id)
    if code >= 0:
        if out:
            print(out)
        return code

    def _relive_one(cid):
        links, err = relive(cid, cs, take_up=not a.peek_only, scope=a.scope)
        if err:
            print("ERROR:", err); return False
        print(render(links, cs.card(cid).label))
        return True

    if not _relive_one(a.card_id):
        return 1
    # Scope-boundary guard ([[open-arc-card-prev-is-a-global-spine]]): legacy arc-cards can carry a
    # CROSS-ESTATE `prev` (the old wrap picked the GLOBAL newest session card, not the in-scope one),
    # so a walk could leave the estate it started in on the first hop. The walk stays in the estate
    # the START card belongs to: if a hop's refs are from another scope, STOP with a notice instead
    # of silently reliving another project's chain. card_scope derives the scope from the atoms.
    start_scope = cs.card_scope(cs.card(a.card_id).refs or [])
    # --reverse: keep walking BACKWARD through prior sessions (the manual back-trace, automated).
    # bare --reverse (cap==0) walks ALL the way back to the start of history — freedom to relive;
    # --reverse N caps at N hops. A `seen` guard makes a malformed prev-cycle terminate honestly.
    if a.reverse is not None:
        cap = a.reverse          # 0 = unbounded (walk to the start of history)
        cur, hops, seen = a.card_id, 0, {a.card_id}
        while cap == 0 or hops < cap:
            prior = prior_arc_card(cur, cs)
            if prior is None:
                print("\n=== (start of history — no prior session arc-card) ===")
                break
            hop_scope = cs.card_scope(cs.card(prior["id"]).refs or [])
            if start_scope and hop_scope and hop_scope != start_scope:
                print(f"\n=== (chain leaves scope {start_scope} at {prior['id']} — that card is "
                      f"scope {hop_scope}; stopping — see open-arc-card-prev-is-a-global-spine) ===")
                break
            if prior["id"] in seen:   # cycle guard — a prev-edge loop would otherwise spin forever
                print("\n=== (chain loops back on itself — stopping the back-trace) ===")
                break
            print(f"\n{'<'*8} BACK-TRACE hop {hops+1}: the session BEFORE this {'<'*8}")
            if not _relive_one(prior["id"]):
                break
            seen.add(prior["id"])
            cur, hops = prior["id"], hops + 1

    # --forward: walk toward the FUTURE. On a FORK (>1 child) STOP at a branch menu — never auto-pick
    # one child (the honest-navigation law: a fork has two real futures; surface both, let the user
    # choose, the same reason a cache hit is reviewed not blindly served). A single child walks on.
    if a.forward is not None:
        cap = a.forward
        cur, hops, seen = a.card_id, 0, {a.card_id}
        while cap == 0 or hops < cap:
            kids = next_arc_cards(cur, cs)
            kids = [k for k in kids if k["id"] not in seen]
            if not kids:
                print("\n=== (end of history — no later session arc-card) ===")
                break
            if len(kids) > 1:
                print(f"\n{'>'*8} FORK: this session branched into {len(kids)} futures {'>'*8}")
                print("(no auto-pick — relive the branch you want with its id):\n")
                for k in kids:
                    print(f"  {k['id']}  {k['label']}")
                print("\n=== (stopped at the fork — pick a branch above) ===")
                break
            child = kids[0]
            hop_scope = cs.card_scope(cs.card(child["id"]).refs or [])
            if start_scope and hop_scope and hop_scope != start_scope:
                print(f"\n=== (chain leaves scope {start_scope} at {child['id']} — that card is "
                      f"scope {hop_scope}; stopping — see open-arc-card-prev-is-a-global-spine) ===")
                break
            print(f"\n{'>'*8} FORWARD hop {hops+1}: the session AFTER this {'>'*8}")
            if not _relive_one(child["id"]):
                break
            seen.add(child["id"])
            cur, hops = child["id"], hops + 1
    return 0


if __name__ == "__main__":
    sys.exit(_main())
