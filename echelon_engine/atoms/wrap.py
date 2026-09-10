"""wrap — the close-out ritual as a COMPOSABLE engine module, not hand-rolled per-session scripts.

WHY THIS EXISTS (owner, 2026-06-19): wrapping a session means running five phases against the bank —
offer the experience, decide it as author, compose the arc-card, record the evidence anchored to each
move, run the immune scan. Until now those were typed out as throwaway `_wrap_*.py` driver scripts every
single session — the exact hand-rolled-driver trap the estate already named ([[use-the-cli-front-door-not-
handrolled-drivers]]). The fix is the same one we keep relearning: author a SPEC, fire ONE front door,
read the result. This module is that door. The `/wrap-session` skill composes a spec and runs `wrap`,
instead of re-deriving five scripts.

WHAT IT DOES NOT DO: Phase 1 (authoring the lesson `.md` files) and Phase 4 (the git commit) stay the
session's deliberate hand-work — a lesson is authored, not generated, and a commit message is the real
arc, not a template. wrap.py covers the MECHANICAL bank phases (2, 2.5, 3) that were the scripts.

The phases, each a real method composing the existing leaves (SessionOffer / CardStore / relive):
  - offer_and_decide  (Phase 2)   — record episodes as neutral cards, apply author consent (promote/decline)
  - compose_card      (Phase 2.5) — chain the episode atoms into the arc-card + typed edges + optional fork
  - record_evidence   (Phase 2.5) — anchor the conversation exchange to each card ref (the relive witness)
  - scan              (Phase 3)   — the immune scan (read-only): FAIL = broken ref to heal, WARN = drift
  - run_spec                      — the front door: take a declarative spec, run all the bank phases in order

See: session_offer.py, cards.py, relive.py, the-card-layer-must-be-chained, v2-earns-from-direct-sessions-via-consent-offer.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import uuid
from pathlib import Path

from .cards import CardStore, DEFAULT_V2_DB
from .coord_norm import _norm_coord
from .session_offer import SessionOffer
from .relive import record_evidence, record_fork


def _resolve(cs: CardStore, slug: str, scope: str = "echelon") -> str | None:
    """Slug/coordinate -> atom id, tolerating a missing scope prefix (the relive/ingest convention).
    Tries the slug as given, then the wrap's own scope, then the echelon scope (back-compat) — a
    wrap run against any estate resolves that estate's atoms, not only echelon's."""
    return (cs.atom_id_for_coordinate(slug)
            or cs.atom_id_for_coordinate(f"{scope}:{slug}")
            or cs.atom_id_for_coordinate("echelon:" + slug))


class WrapSession:
    """The mechanical bank phases of a wrap, composed over the existing leaves. One CardStore is shared
    across offer/card/scan so it is one db, one lock, one transaction surface."""

    def __init__(self, store: CardStore | None = None, db_path: str = DEFAULT_V2_DB, scope: str = "echelon"):
        self.cards = store if store is not None else CardStore(db_path)
        self.offers = SessionOffer(self.cards)
        self.scope = scope

    # ── Phase 2 — offer the session's experience + decide it as author ──────────────────────────────
    def offer_and_decide(self, session: str, episodes: list[dict]) -> list[dict]:
        """Record each episode as a NEUTRAL card chained to the prior, then apply the author's decision.
        `episodes` = [{summary, coords:[...], decision:'promote'|'decline'}]. The decision is the author's
        consent (never a self-grade — promote applies one capped CONSENT_Q bump, decline leaves it to
        decay). Returns one result row per episode: its card id, decision, and the consent outcome."""
        out, prev = [], ""
        for ep in episodes:
            cid = self.offers.record_episode(session, ep["summary"], ep["coords"], prev_card_id=prev)
            prev = cid or prev
            decision = (ep.get("decision") or "promote").lower()
            if not cid:
                res = {"ok": False, "reason": "no coords — nothing to offer"}
            elif decision == "decline":
                res = self.offers.decline(cid)
            else:
                res = self.offers.promote(cid)
            out.append({"card_id": cid, "summary": ep["summary"], "decision": decision, "result": res})
        return out

    # ── Phase 2.5 — compose the arc-card from the ordered move sequence ──────────────────────────────
    def compose_card(self, label: str, slugs: list[str], born_from: str,
                     edges: list[dict] | None = None, forked_from: str = "") -> dict:
        """Chain the session's atoms (IN ORDER) into ONE neutral arc-card, wiring typed atom->atom edges
        along the arc. `edges` = [{from_idx, to_idx, relation}] indexing into `slugs` (default: link each
        atom to the previous with 'refines' so the chain is at least connected). If `forked_from` is a
        prior arc-card id, stamp the fork edge so relive --forward can reach this branch. Returns the card
        id + ref count, or an error naming any unresolved slug (a missing atom is a wrap defect, not a
        silent skip — the card needs the coordinates to exist; run the ingest first)."""
        ids = [_resolve(self.cards, s, self.scope) for s in slugs]
        missing = [s for s, i in zip(slugs, ids) if not i]
        if missing:
            return {"ok": False, "reason": f"unresolved atoms (ingest first?): {missing}"}
        if edges is None:
            edges = [{"from_idx": k, "to_idx": k - 1, "relation": "refines"} for k in range(1, len(slugs))]
        for e in edges:
            self.cards.link(ids[e["from_idx"]], ids[e["to_idx"]], e.get("relation", "refs"))
        # CHAIN THE SESSION TO THE PRIOR ONE ([[arc-cards-need-prev-edges-to-chain]], fixed 2026-06-19):
        # set `prev` to the newest existing wrap/relive arc-card so the sessions form a real rope —
        # `relive --reverse` walks it by lineage (not a timestamp guess), and reinforce_card's chain-rule
        # can propagate a credited session's weight BACK to the session it built on. Without this, prev=''
        # and the self-compounds-across-sessions claim is only half-wired. An explicit forked_from (a
        # /relive RESUME branch) takes precedence as the prev — the branch's parent IS its root.
        prev = forked_from or self._prev_session_card()
        # Store each ref as the atom's CANONICAL coordinate (not a hardcoded echelon: prefix —
        # that stored dead coordinates for every non-echelon estate, breaking scan + evidence).
        refs = [self._canonical_coordinate(i) or f"{self.scope}:{s}" for s, i in zip(slugs, ids)]
        cid = self.cards.add_card(label, refs, born_from=born_from, prev=prev or "")
        forked = record_fork(cid, forked_from) if forked_from else False
        return {"ok": True, "card_id": cid, "refs": len(self.cards.card(cid).refs),
                "prev": prev or None, "forked_from": forked_from or None, "forked": forked}

    def _canonical_coordinate(self, atom_id: str) -> str | None:
        """The coordinate the bank actually stores for an atom id — the only ref that is
        guaranteed to resolve back (scan-safe, evidence-safe) whatever the estate's scope."""
        with self.cards._lock:
            r = self.cards.conn.execute(
                "SELECT coordinate FROM atoms WHERE id=? ORDER BY ts DESC LIMIT 1", (atom_id,)).fetchone()
        return r["coordinate"] if r else None

    def _prev_session_card(self) -> str:
        """The newest existing session arc-card (wrap/relive born_from) — the one THIS session follows.
        Returns its id, or '' at the start of history. Run BEFORE add_card so it can't pick up self.
        SCOPED TO THIS ESTATE ([[open-arc-card-prev-is-a-global-spine]]): a card's refs are normalised
        coordinates, so only session cards whose refs carry the `<norm_scope>:` prefix are the in-estate
        chain tip. Without the predicate, `prev` was the GLOBAL newest session card whatever estate
        wrapped last — one cross-estate spine, and relive --reverse walked out of the scope it started
        in (proven: delta-shop card 7b18254c4cfe4b2b chained to gamma-support card 6fac271f129e6c67)."""
        from ..registry import SESSION_ARC_CODES
        kind_sql = "kind IN (" + ",".join(str(c) for c in SESSION_ARC_CODES) + ")"
        norm_scope = _norm_coord(self.scope)   # 'delta-shop' scope -> 'delta-shop:' prefix
        scope_sql = "refs LIKE ?" if norm_scope else "1=1"
        params = (f'%"{norm_scope}:%',) if norm_scope else ()
        with self.cards._lock:
            r = self.cards.conn.execute(
                f"SELECT id FROM cards WHERE (({kind_sql}) OR born_from LIKE 'wrap-session%' "
                "OR born_from LIKE 'relive%' OR born_from LIKE 'session-wrap%' "
                f"OR born_from LIKE 'session_wrap%') AND {scope_sql} "
                "ORDER BY ts DESC LIMIT 1", params).fetchone()
        return r["id"] if r else ""

    # ── Phase 2.5 — anchor the conversation to each move (the relive witness) ────────────────────────
    def record_evidence(self, card_id: str, session: str, exchanges: list[str]) -> dict:
        """Record the distilled human<->assistant exchange behind each card ref, BY ORDINAL (the coord
        normaliser underscores slugs, so keying by hyphenated slug silently misses — order is stable and
        immune). Refuses unless there is one NON-EMPTY exchange per ref (the teeth: an empty exchange is a
        wrap defect that starves the relive)."""
        card = self.cards.card(card_id)
        if card is None:
            return {"ok": False, "reason": f"no card {card_id}"}
        refs = card.refs
        if len(exchanges) != len(refs) or not all(e and e.strip() for e in exchanges):
            return {"ok": False, "reason": f"need one non-empty exchange per ref ({len(refs)}); got {len(exchanges)}"}
        for i, coord in enumerate(refs):
            record_evidence(card_id, coord, session, i, exchanges[i])
        return {"ok": True, "moves": len(refs)}

    # ── Phase 3 — the immune scan (read-only) ───────────────────────────────────────────────────────
    def scan(self) -> dict:
        """Scan the v2 graph for broken memories. FAIL = a broken reference to heal before it rots; WARN =
        drift to note. Read-only — it reports, it never mutates."""
        return self.cards.scan(scope=self.scope)

    # ── The front door — run the whole spec ─────────────────────────────────────────────────────────
    def run_spec(self, spec: dict) -> dict:
        """Run the mechanical bank phases from ONE declarative spec, in order. Spec shape:
            {
              "session": "<YYYY-MM-DD-arc-slug>",
              "born_from": "wrap-session <date>",
              "episodes": [{"summary": "...", "coords": ["memory:a", ...], "decision": "promote"}],
              "card":     {"label": "...", "slugs": ["a","b","c"], "edges": [...], "forked_from": ""},
              "evidence": ["exchange for ref 0", "exchange for ref 1", ...]
            }
        `card`/`evidence` are optional (a single-lesson session may have no arc). Returns the per-phase
        results so the caller verifies each landed (no phase silently skipped)."""
        session = spec["session"]
        result: dict = {"session": session}
        result["offer"] = self.offer_and_decide(session, spec.get("episodes", []))
        if spec.get("card"):
            c = spec["card"]
            cres = self.compose_card(c["label"], c["slugs"], spec.get("born_from", f"wrap-session {session}"),
                                     edges=c.get("edges"), forked_from=c.get("forked_from", ""))
            result["card"] = cres
            if cres.get("ok") and spec.get("evidence"):
                result["evidence"] = self.record_evidence(cres["card_id"], session, spec["evidence"])
        result["atlas"] = self._refresh_code_atlas()
        result["scan"] = self.scan()
        result["liveness"] = self._queue_liveness()
        # View-through decay — atoms surfaced but passed over get a small score decay
        try:
            result["decay"] = self.cards.view_decay_pass(self.scope)
        except Exception as e:
            result["decay"] = {"ok": False, "error": str(e)}
        return result

    def _queue_liveness(self) -> dict:
        """WRAP PHASE 'liveness' — queue the atom-anchor sweep for the daily backup.

        THE VERB MUST CARRY THE RITUAL, NOT THE SKILL (owner 2026-08-18): "harness with no
        skills or echelon agent itself will run those." `~/.claude/skills/wrap/SKILL.md` is a
        Claude-harness surface — a bare `echelon wrap`, the agent's own close-out, or any
        non-Claude harness never reads it. A ritual step that lives ONLY in the skill is a
        step that silently does not happen for every other caller.

        Enqueue only: the sweep itself rides `backup --push` (it gates nothing in a wrap, and
        it was the ritual's slowest step). Never fatal — hygiene must not fail a wrap."""
        try:
            from .liveness import enqueue
            # BOTH roots, or the sweep reports FALSE DEATHS: an engine atom cites paths in
            # ECHELON-AGENT while an estate atom cites paths in the estate repo, and an
            # anchor checked against only one root looks GONE when it is merely elsewhere —
            # which invites a dispute that would destroy earned weight on a live atom.
            #
            # The estate is derived from THE SCOPE'S OWN memory dir, not from cwd: a wrap is
            # normally run FROM the engine repo, where a cwd fallback collapses both roots
            # into one and silently reintroduces the false-death problem.
            roots = {str(Path(__file__).resolve().parents[2])}
            try:
                mem_dirs = json.loads(
                    (Path.home() / ".echelon" / "mem_dirs.json").read_text(encoding="utf-8"))
                for d in mem_dirs.get(self.scope, []):
                    p = Path(d)
                    roots.add(str(p.parent if p.name == "memory" else p))
            except (OSError, json.JSONDecodeError):
                pass                      # fall through to the engine root alone
            if os.environ.get("ECHELON_ESTATE"):
                roots.add(os.environ["ECHELON_ESTATE"])
            # EXTRA ROOTS (liveness verdicts 2026-09-01): a scope's atoms cite paths in
            # SIBLING repos the two derivations above can never reach (echelon atoms anchor
            # in ECHELON-OS, the R9 archive, even a client repo) — 0 of 6 "anchor-gone"
            # verdicts that day were real deaths; all were root-coverage gaps. The extra
            # roots are DATA, not code: ~/.echelon/liveness_roots.json {scope: [dirs]}.
            try:
                extra = json.loads(
                    (Path.home() / ".echelon" / "liveness_roots.json").read_text(encoding="utf-8"))
                for d in extra.get(self.scope, []):
                    roots.add(str(Path(d)))
            except (OSError, json.JSONDecodeError):
                pass                      # optional file — absence is the common case
            spec = enqueue(self.scope, sorted(r for r in roots if r and Path(r).is_dir()))
            return {"ok": True, "queued": self.scope, "roots": len(spec["roots"]),
                    "note": "sweep runs with the next daily backup — "
                            "read it with `echelon liveness --report`"}
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    def _refresh_code_atlas(self) -> dict:
        """WRAP PHASE 'atlas' for the ENGINE's own code (Phase D, 2026-07-22). The wrap pipeline is
        distill→ATLAS→atom→ingest→scanner→summary, but the 'atlas' step never had a mechanical action
        for the engine's SELF-MAP — so it drifted (960 soul-atoms, 0 code-atoms; the relive coupling
        rotted unseen for 10 days). This regenerates the deterministic code-atlas so 'the map is part
        of done'. Only fires when wrapping the ENGINE scope ('echelon') — other estates' wraps skip it.
        Best-effort: a failure here NEVER breaks the wrap (the atlas is upkeep, not the bank). See
        engine-had-no-code-atlas, gen_code_atlas."""
        if self.scope != "echelon":
            return {"skipped": f"scope '{self.scope}' is not the engine — no code-atlas to refresh"}
        try:
            from pathlib import Path
            from ..gen_code_atlas import build_atlas, surface_commands, write_atlas
            root = Path(__file__).resolve().parents[2]   # …/ECHELON-AGENT
            pkgs = ["echelon_engine", "echelon_sdk", "apps"]
            cards, meta = build_atlas(root, pkgs)
            commands = surface_commands(root, cards)
            res = write_atlas(root / "docs" / "c-atlas", cards, meta, commands)
            und = [c["verb"] for c in commands["commands"] if c["exposure"] == "undocumented"]
            return {"ok": True, "nodes": res["written"], "commands": commands["summary"],
                    "undocumented_verbs": und}
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}


# ── THE ARC-SPEC COMPOSER (--auto) ──────────────────────────────────────────────────────
# WHY (owner, 2026-08-15): step 8 of /wrap was hand-authored JSON — N slugs, N episode
# summaries, N evidence strings — ~8k tokens of composition per wrap, plus a footgun the
# skill itself documents (`need one non-empty exchange per ref`). The owner's insight: the
# ADD is the expensive half; removing is cheap. So the engine proposes the card from what it
# already recorded and the author DELETES what doesn't belong.
#
# NO NEW SCHEMA WAS NEEDED. atoms.ts is the plant time, atom_earned.use_count/last_fetch_ts
# is the promotion event, and the previous session arc-card's ts is the window start (the same
# lineage compose_card chains to). So the candidate set is a WINDOW QUERY, not a new tag
# threaded through ingest.
#
# THE EARNED GUARD (the owner's own caveat, and the reason this doesn't become a new trap):
# an atom that was ingested but never earned weight must NOT propose itself into a card —
# otherwise the card fills with neutral atoms and "removing is simpler than adding" inverts
# into "removing is most of the work". Default: only atoms with a witnessed take-up
# (use_count > 0 or an in-window fetch) are PROPOSED; the rest are listed as `held_back` so
# the author can promote one deliberately rather than never seeing it.
def compose_auto_spec(scope: str, since: int | None = None, db_path: str | None = None,
                      include_unearned: bool = False, label: str = "") -> dict:
    """Propose a wrap spec from the atoms planted in this scope since the last session card.

    Returns {spec, held_back, window}. The spec is a SKELETON to edit down: slugs are filled
    and ordered by plant time, episodes/evidence are pre-sized 1:1 with the slugs (so the
    'one non-empty exchange per ref' error is structurally unreachable) but carry TODO text —
    the summaries and the owner-exchange evidence stay authored, because they are the part
    that carries meaning. The engine fills what it KNOWS and refuses to invent what it doesn't."""
    from .cards import CardStore, DEFAULT_V2_DB
    cs = CardStore(db_path or DEFAULT_V2_DB)
    ws = WrapSession(store=cs, scope=scope)
    start = since if since else 0
    prev_card = ws._prev_session_card()
    if not start:
        if prev_card:
            with cs._lock:
                r = cs.conn.execute("SELECT ts FROM cards WHERE id=?", (prev_card,)).fetchone()
            start = int(r["ts"]) if r else 0
        if not start:
            start = int(time.time()) - 86400   # first wrap in this estate: last 24h
    with cs._lock:
        rows = cs.conn.execute(
            "SELECT a.id, a.coordinate, a.ts, e.use_count, e.last_fetch_ts "
            "FROM atoms a LEFT JOIN atom_earned e ON e.atom_id = a.id "
            "WHERE a.scope = ? AND a.ts >= ? ORDER BY a.ts ASC", (scope, start)).fetchall()

    proposed, held = [], []
    for r in rows:
        slug = (r["coordinate"] or "").split(":", 1)[-1]
        if not slug:
            continue
        # The wrap atom itself is the session's RECEIPT, not a lesson in its arc — it would
        # make every card self-referential. Skip it from the proposal (never from the bank).
        if slug.startswith("session_wrap") or slug.startswith("session-wrap"):
            held.append({"slug": slug, "reason": "wrap receipt, not an arc lesson"})
            continue
        # TWO SIGNALS, NOT ONE (fixed 2026-08-15 by dogfooding this verb on its own wrap):
        # the earned-weight gate alone proposed NOTHING on the very wrap it was built for,
        # because atoms authored THIS session were planted minutes ago and cannot have
        # use_count > 0 yet. Being BANKED IN THIS WINDOW is itself the signal for a
        # same-session atom — it is this session's work by construction. The earned gate
        # still governs atoms that PREDATE the window (re-touched old atoms), which is the
        # case it was actually protecting against: a stale atom drifting into a new card.
        earned = (r["use_count"] or 0) > 0 or ((r["last_fetch_ts"] or 0) >= start)
        planted_this_window = int(r["ts"] or 0) >= start
        if planted_this_window or earned or include_unearned:
            why = ("banked this session" if planted_this_window
                   else "earned weight this window" if earned else "unearned (--include-unearned)")
            proposed.append({"slug": slug, "ts": int(r["ts"]),
                             "use_count": r["use_count"] or 0,
                             "earned": bool(earned), "why": why})
        else:
            held.append({"slug": slug, "reason": "predates this window and earned no weight in it"})

    slugs = [p["slug"] for p in proposed]
    date = time.strftime("%Y-%m-%d", time.localtime(start))
    # Stamp the ECHELON session id into born_from so the arc-card JOINS the commit trailer
    # (`Echelon-Session: <id>`). Without it `echelon stakes orphans` reports a session as
    # "committed but never wrapped" FOREVER — the card exists but carries no id to match on.
    # Witnessed 2026-08-17: the wrap that shipped the detector could not close its own loop.
    # Severable: if the meter/facts are unavailable, fall back to the plain date form.
    try:
        from .stakes import current_facts as _cf
        _sid = (_cf() or {}).get("echelon_session_id", "")
    except Exception:
        _sid = ""
    born = f"wrap-session {date}" + (f" session:{_sid}" if _sid else "")
    # IDENTITY BLOCK (owner defect 2026-08-28: concurrent sessions' auto-specs collided on the
    # date-keyed filename and the JSON carried NO identifier — a same-day wrap silently REPLACED
    # the other session's composed arcs). spec_id makes every composition distinguishable;
    # scope + composed_by make a cross-estate or cross-session mixup visible AND refusable
    # (the run path hard-refuses a scope mismatch).
    spec_id = uuid.uuid4().hex[:12]
    spec = {
        "spec_id": spec_id,
        "scope": scope,
        "composed_ts": int(time.time()),
        "composed_by": _sid or "-",
        "session": f"{date}-TODO-arc-slug",
        "born_from": born,
        "episodes": [{"summary": f"TODO: what happened in the arc that produced {s}",
                      "coords": [f"memory:{s}"], "decision": "promote"} for s in slugs],
        "card": {"label": label or "TODO: one line naming this session's arc",
                 "slugs": slugs},
        "evidence": [f"TODO: the owner exchange behind {s}" for s in slugs],
    }
    return {"spec": spec, "held_back": held, "proposed": proposed,
            "window": {"start": start, "prev_card": prev_card,
                       "hours": round((time.time() - start) / 3600, 1)}}


def _render_auto(res: dict, out_path: str) -> str:
    w = res["window"]
    lines = [f"=== WRAP --auto: proposed arc-spec ===",
             f"  window: {w['hours']}h since "
             + (f"card {w['prev_card']}" if w["prev_card"] else "24h-ago (no prior session card)")]
    if res["proposed"]:
        lines.append(f"  PROPOSED {len(res['proposed'])} atom(s):")
        for p in res["proposed"]:
            lines.append(f"    [{p.get('why', 'proposed')}] {p['slug']}  (use_count={p['use_count']})")
    else:
        lines.append("  PROPOSED none — no atom in this window earned weight. "
                     "Either nothing was banked, or nothing was taken up through the door.")
    if res["held_back"]:
        lines.append(f"  held back {len(res['held_back'])} (add deliberately if one belongs):")
        for h in res["held_back"]:
            lines.append(f"    - {h['slug']}  — {h['reason']}")
    lines += ["", f"  spec written: {out_path}",
              "  NEXT: edit it down (delete what doesn't belong), replace every TODO,",
              f"        then run: python -X utf8 -m echelon_engine wrap --scope {res.get('scope','<scope>')} {out_path}"]
    return "\n".join(lines)


def _render(res: dict) -> str:
    out = [f"=== WRAP: {res['session']} ==="]
    for o in res.get("offer", []):
        r = o["result"]
        verdict = (f"score {r.get('new_score')}" if r.get("ok") else r.get("reason", "?"))
        out.append(f"  [{o['decision']}] {o['summary'][:70]}  -> {verdict}")
    if "card" in res:
        c = res["card"]
        out.append(f"  card: {c.get('card_id','-')} | {c.get('refs','?')} refs"
                   + (f" | forked_from {c['forked_from']}" if c.get("forked") else "")
                   if c.get("ok") else f"  card: ERROR {c.get('reason')}")
    if "evidence" in res:
        e = res["evidence"]
        out.append(f"  evidence: {e.get('moves','?')} moves anchored" if e.get("ok") else f"  evidence: ERROR {e.get('reason')}")
    a = res.get("atlas")
    if a and not a.get("skipped"):
        if a.get("ok"):
            line = f"  code-atlas: {a['nodes']} nodes refreshed  cmds={a['commands']}"
            if a.get("undocumented_verbs"):
                line += f"  ⚠ undocumented: {','.join(a['undocumented_verbs'])}"
            out.append(line)
        else:
            out.append(f"  code-atlas: ERROR {a.get('error')}")
    s = res.get("scan", {})
    out.append(f"  immune scan: {s.get('stats', s)}")
    for f in s.get("fail", []):
        out.append(f"    FAIL {f['rule']} | {f['detail']}")
    lv = res.get("liveness", {})
    if lv:
        out.append(f"  liveness: queued for the daily sweep ({lv['roots']} root(s)) — "
                   f"`echelon liveness --report`" if lv.get("ok")
                   else f"  liveness: NOT queued — {lv.get('error')}")
    d = res.get("decay", {})
    if d and d.get("atom_count"):
        out.append(f"  view-decay: {d['atom_count']} atoms, {d['deltas_applied']} deltas, "
                   f"total_delta={round(d.get('total_delta', 0), 2)}")
    elif d and not d.get("ok", True):
        out.append(f"  view-decay: ERROR {d.get('error')}")
    return "\n".join(out)


def _main(argv=None):
    ap = argparse.ArgumentParser(description="Run the mechanical bank phases of a session wrap from a spec.")
    ap.add_argument("spec", nargs="?", help="path to the wrap spec JSON (or '-' to read stdin)")
    ap.add_argument("--scope", default="echelon")
    ap.add_argument("--auto", action="store_true",
                    help="COMPOSE a spec skeleton from atoms banked since the last session card "
                         "(earned-weight guarded), instead of running one")
    ap.add_argument("--since", type=int, default=0, help="--auto: window start unix ts (default: prev session card)")
    ap.add_argument("--out", default="", help="--auto: where to write the spec (default: _wrap/auto-spec-<date>.json)")
    ap.add_argument("--include-unearned", action="store_true",
                    help="--auto: also propose atoms that never earned weight (off by default — "
                         "an unearned atom in the card makes deleting the majority of the work)")
    ap.add_argument("--label", default="", help="--auto: the arc-card label, if you already know it")
    a = ap.parse_args(argv)

    if a.auto:
        res = compose_auto_spec(a.scope, a.since or None, include_unearned=a.include_unearned,
                                label=a.label)
        res["scope"] = a.scope
        # Collision-proof default name (owner defect 2026-08-28): date alone made two same-day
        # sessions overwrite each other's spec. date + time + scope + spec_id can't collide,
        # and the exists-check keeps even a hand-passed --out from silently replacing a spec.
        sid = res["spec"].get("spec_id", "x")
        out = Path(a.out) if a.out else (
            Path("_wrap") / f"auto-spec-{time.strftime('%Y-%m-%d-%H%M%S')}-{a.scope}-{sid[:6]}.json")
        out.parent.mkdir(parents=True, exist_ok=True)
        n = 1
        while out.exists():
            out = out.with_name(out.stem.rsplit("~", 1)[0] + f"~{n}" + out.suffix)
            n += 1
        out.write_text(json.dumps(res["spec"], indent=2, ensure_ascii=False), encoding="utf-8")
        print(_render_auto(res, str(out)))
        return 0 if res["proposed"] else 1

    if not a.spec:
        ap.error("give a spec path, or use --auto to compose one")
    raw = sys.stdin.read() if a.spec == "-" else Path(a.spec).read_text(encoding="utf-8")
    spec = json.loads(raw)
    # SCOPE GUARD (owner defect 2026-08-28): a spec composed for one estate run under another
    # scope mints the card into the wrong chain. A spec that declares its scope is refused on
    # mismatch; legacy specs without the field run as before.
    spec_scope = spec.get("scope")
    if spec_scope and spec_scope != a.scope:
        print(f"wrap: REFUSED — spec was composed for scope '{spec_scope}' "
              f"(spec_id {spec.get('spec_id', '?')}), but --scope is '{a.scope}'. "
              "Pass the matching --scope or recompose with --auto.", file=sys.stderr)
        return 2
    if spec.get("spec_id"):
        print(f"spec_id: {spec['spec_id']} (scope {spec_scope or '-'}, "
              f"composed_by {spec.get('composed_by', '-')})")
    ws = WrapSession(scope=a.scope)
    res = ws.run_spec(spec)
    print(_render(res))
    # exit non-zero if a phase errored, so a wrap that half-failed is visible
    bad = any(not o["result"].get("ok") for o in res.get("offer", [])) \
        or (res.get("card") and not res["card"].get("ok")) \
        or (res.get("evidence") and not res["evidence"].get("ok")) \
        or bool(res.get("scan", {}).get("fail"))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(_main())
