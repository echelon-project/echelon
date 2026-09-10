"""The Knowledge Bank — COS × ENTRY. The missing organ (owner, 2026-06-06).

A THIN COS layer over UAME. An entry IS a uame.Entry (family="bank"): same row, same scoring, same
append/scan/compute_score machinery. The bank re-implements NOTHING the substrate already owns
(owner: "UAME already handle the scoring, so..."). It adds ONLY what UAME lacks for CONTENT:
coordinate-path resolution with depth-fallback, TTL eviction, and a lexical within-node rank.

THE DISTINCTION (see knowledge-bank-cos-x-entry-rag, cos-x-entry-coordinate-spine):
  SEED (soul: store.py/warmth.py) = a conclusion / weight-adjustor; retrieval = recognition
    (warmth); append-only, no TTL; content-addressed (same conclusion = same seed).
  ENTRY (bank: this file)        = actual CONTENT at a COS coordinate; retrieval = coordinate
    resolution + score-ranked top-k; TTL legal (a content cache); content+ts addressed (a repeated
    event is a NEW entry). Injected as content — it IS the thing you wanted back.

THE WALL: entries live in the `bank_<domain>` table family. UAME's `_all_tables` (which scan() and
warmth use) matches ONLY core_*/working_*, so bank_* is INVISIBLE to the soul — a content entry can
never surface as a seed, and TTL eviction on bank_* can never reach the soul. The prefix IS the wall
(same structural argument as UAME's core/working two-table wall).

THE COS SHAPE (COSys_DESIGN §4,5,7,14 — the spec's settled decisions):
  - COORDINATE = the address: `general:topic:domain:specific:...` broad→atomic. The COS hierarchy
    lives in the `coordinate` STRING (queried by prefix), not in table topology — flat records (§14).
  - PATH-DEPTH FALLBACK (§5): every entry is a valid (broader) answer at every level ABOVE its
    coordinate. Resolve at depth N; if empty, ASCEND to N-1 and retry. The hierarchy IS the fallback.
  - SCORE = fallback order (§7): B=100, time-decay weighted — but that math is UAME's (compute_score);
    the bank only READS it to rank and delegates reinforcement to the same formula store.py uses.
  - RAG is a layer COS SURPASSES (§22.3: entries carry score history; RAG retrieval doesn't). Lexical
    overlap is the cheap within-node floor; the embedding tier is the >200-entry migration (§23.5).

Shares the UAME connection (one core.db, one lock, ONE uame_links relationship-index). An entry's
[[refs]] become uame_links edges INTO the soul, so the keystone (warmth-travels-the-edge) lights up
the abstract seed an entry instances. The bank plugs into the soul graph; it is not a separate island.
"""
from __future__ import annotations
import json
import re
import time
from dataclasses import dataclass
from pathlib import Path

from .uame import UAME, Entry, DEFAULT_DB, compute_score, domain_of, SCORE_BENCHMARK

# entries ride this UAME prefix family — walled from the soul (core_*/working_*).
BANK_FAMILY = "bank"
SCORE_K = 1.0   # COS delta scale, same as store.py (the soul's rating constant).

# A [[name]] in an entry = an authored edge INTO the soul graph (seed-and-link-while-warm).
# A [[ref]] may be a single slug OR a full COS coordinate path (colon-delimited) — the colon MUST be
# allowed or [[domain:topic:atom]] refs silently fail to link (the keystone gap that bit once).
_REF = re.compile(r"\[\[([a-z0-9][a-z0-9 _:-]*?)\]\]", re.I)
_WORD = re.compile(r"[a-z0-9]+")
_STOP = frozenset(
    "the a an and or but to of in on at for with by is are was were be been being "
    "this that these those it its as do did does done from not no yes i you he she we "
    "they them his her their our your my me will would can could should have has had "
    "if then else when what which who how why where".split()
)


def _tokens(text: str) -> list[str]:
    return [w for w in _WORD.findall(text.lower()) if w not in _STOP and len(w) > 2]


def _norm_coord(coordinate: str) -> str:
    segs = [s.strip().lower() for s in (coordinate or "").split(":") if s.strip()]
    return ":".join(segs)


def _ascend(coordinate: str):
    """Yield a coordinate then each broader ancestor (the path-depth fallback ladder, §5).
    'a:b:c' -> 'a:b:c','a:b','a'. The empty root is not yielded."""
    segs = _norm_coord(coordinate).split(":") if coordinate else []
    while segs:
        yield ":".join(segs)
        segs = segs[:-1]


def entry_content_id(text: str, coordinate: str, kind: str, ts: int) -> str:
    """ts-INCLUSIVE content address: two identical observations at different times are TWO events
    (unlike a seed, where same conclusion = same seed). content+coordinate+when = the event id."""
    import hashlib
    raw = json.dumps({"t": text, "c": _norm_coord(coordinate), "k": kind, "ts": ts}, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def content_hash(text: str) -> str:
    """A ts-INDEPENDENT hash of just the content. This is the VERSIONING key: two stores at the same
    coordinate with the same content_hash are the SAME version (dedup, no churn); a different hash is
    a genuine change → a new version. (Distinct from entry_content_id, which is ts-inclusive so each
    version row has its own unique id.)"""
    import hashlib
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


@dataclass
class Hit:
    entry: Entry            # a uame.Entry (family="bank") — same type as a seed, different family
    lexical: float = 0.0    # query-vs-text match (0..1) within the resolved node
    via_coord: str = ""     # the coordinate (possibly an ancestor) this entry resolved under
    depth_drop: int = 0     # levels the resolver ascended (0 = exact-depth hit)


def _expired(e: Entry, now: int) -> bool:
    return e.ttl > 0 and now >= e.ts + e.ttl


class KnowledgeBank:
    """COS × ENTRY: coordinate-pathed content over UAME. Pure resolution layer — zero own scoring."""

    def __init__(self, db_path: Path | str = DEFAULT_DB, uame: UAME | None = None,
                 guard: bool = False, strict_guard: bool = False):
        self.u = uame if uame is not None else UAME(db_path)
        self.db_path = self.u.db_path
        # COS guard (§8): keep the taxonomy from rotting. Off by default (the bank stays minimal);
        # guard=True attaches a CoordinateGuard that validates each store() coordinate. strict_guard
        # makes a junk/malformed/too-shallow coordinate RAISE (enforce-don't-request); else it's
        # advisory (the verdict rides the return, the write still lands — an agent acts on advice).
        self.strict_guard = strict_guard
        self._guard = None
        if guard or strict_guard:
            from echelon_sdk.cos_guard import CoordinateGuard   # pure leaf — migrated to the sdk layer
            self._guard = CoordinateGuard(self)
        self.last_verdict = None   # the most recent guard verdict (for the caller to inspect)

    # --- write ---
    def store(self, text: str, coordinate: str, kind: str = "note", source: str = "",
              scope: str = "", ttl: int = 0, meta: dict | None = None) -> str:
        """Place an entry at a COS coordinate. The domain (UAME table) = the coordinate HEAD (so
        `tooling:bash:gzip` lands in bank_tooling, parallel to core_tooling/working_tooling). Returns
        the entry id. Delegates the write to UAME.append — same machinery as a seed, family='bank'.

        meta (source/kind extras) rides the `scope` JSON tail-free: we keep meta in the content? No —
        UAME has no meta column. meta is folded into `source` as a compact suffix only if given; the
        bank's job is content + coordinate, and structured sidecars are a later stone if needed.

        SEED-AND-LINK into the soul: any [[name]] in text becomes a uame_links 'instances' edge to the
        seed carrying that coordinate slug, so warmth-travels-the-edge can light up the value it
        instances. Unresolved refs are skipped (forward ref = not an error)."""
        coord = _norm_coord(coordinate)
        now = int(time.time())
        # COS GUARD (§8): validate the coordinate before it lands. strict -> raise on junk/malformed;
        # advisory -> the verdict is stashed on last_verdict for the caller, the write proceeds.
        if self._guard is not None:
            self.last_verdict = self._guard.validate(coord, strict=self.strict_guard)
            self._guard.learn(coord)   # the validated coordinate joins the learned taxonomy
        # VERSIONING (owner: same path/repo/file/function can hold DIFFERENT content later).
        # Look at the CURRENT live version at this exact coordinate (+kind, the element type):
        #   - none           -> this is v1, a fresh entry.
        #   - same content   -> NO-OP (content_hash match): a re-scan of unchanged code adds nothing.
        #   - changed content-> this is a NEW VERSION that SUPERSEDES the prior (append-only chain +
        #                       a uame_links 'supersedes' edge, exactly like the soul's corrections).
        prior = self._current_at(coord, kind, now)
        if prior is not None and content_hash(prior.content) == content_hash(text):
            return prior.id   # unchanged — same version, no churn
        ts = now
        e = Entry(content=text, domain=domain_of(coord), coordinate=coord, kind=kind,
                  permanent=False, scope=scope, ts=ts, ttl=max(0, int(ttl)), family=BANK_FAMILY,
                  supersedes=(prior.id if prior is not None else ""))
        # ts-inclusive id (content events, not conclusions) — override UAME's content-only id.
        e.id = entry_content_id(text, coord, kind, ts)
        if source:
            e.scope = (scope + " | src:" + source) if scope else ("src:" + source)
        self.u.append(e)
        if prior is not None:
            try:
                self.u.link(e.id, prior.id, "supersedes")   # new --supersedes--> old (the version chain)
            except Exception:
                pass
        for name in _REF.findall(text):
            target = self._resolve_ref(name)
            if target and target != e.id:
                try:
                    self.u.link(e.id, target, "instances")
                except Exception:
                    pass
        return e.id

    def _resolve_ref(self, name: str) -> str | None:
        """Resolve [[name]] to a SEED id by coordinate slug — entries link INTO the soul, so search
        the soul tables (core_*/working_*), not bank_*. Most recent wins."""
        slug = name.strip().lower()
        best_id, best_ts = None, -1
        with self.u._lock:
            for tbl in self.u._all_tables():     # soul tables only — the link target is a seed
                for row in self.u.conn.execute(
                        f"SELECT id, ts FROM {tbl} WHERE lower(coordinate)=?", (slug,)).fetchall():
                    if row["ts"] > best_ts:
                        best_id, best_ts = row["id"], row["ts"]
        return best_id

    # --- read (over the bank family ONLY — never the soul) ---
    def _row_to_entry(self, r, tbl: str) -> Entry:
        e = Entry(content=r["content"], domain=tbl.split("_", 1)[1], coordinate=r["coordinate"],
                  kind=r["kind"], permanent=False, scope=r["scope"], supersedes=r["supersedes"],
                  ts=r["ts"], ttl=r["ttl"] if "ttl" in r.keys() else 0,
                  valence=r["valence"], arousal=r["arousal"],
                  score=r["score"], recall_count=r["recall_count"], family=BANK_FAMILY)
        e.id = r["id"]
        return e

    def _scan_bank(self, kind: str | None, now: int, all_versions: bool = False,
                   include_expired: bool = False) -> list[Entry]:
        """Live entries across the bank family. By default returns CURRENT VERSIONS ONLY — an entry
        that another live entry supersedes is dropped (the version chain reads like the soul's: the
        correction replaces the corrected in default reads). all_versions=True returns the whole chain
        (for history()). include_expired=True keeps ttl-expired rows (history is the chronicle; TTL
        governs the LIVE cache, not the record). (The bank is content; one family to walk — the
        coordinate hierarchy is a string, not a table split. The embedding tier indexes at >200 scale.)"""
        rows = []
        with self.u._lock:
            for tbl in self.u._family_tables(BANK_FAMILY):
                q = f"SELECT * FROM {tbl}"
                params: tuple = ()
                if kind is not None:
                    q += " WHERE kind=?"; params = (kind,)
                for r in self.u._retry(q, params).fetchall():
                    e = self._row_to_entry(r, tbl)
                    if include_expired or not _expired(e, now):
                        rows.append(e)
        if all_versions:
            return rows
        # drop superseded: any entry whose id is the supersede-target of another LIVE entry here.
        superseded = {e.supersedes for e in rows if e.supersedes}
        return [e for e in rows if e.id not in superseded]

    def _current_at(self, coordinate: str, kind: str | None, now: int) -> Entry | None:
        """The current (latest, non-superseded) entry at an EXACT coordinate (+kind). The versioning
        anchor: store() compares against this to decide same-version vs new-version."""
        coord = _norm_coord(coordinate)
        cands = [e for e in self._scan_bank(kind, now) if e.coordinate == coord]
        return max(cands, key=lambda e: e.ts) if cands else None

    @staticmethod
    def _at_prefix(entries: list[Entry], prefix: str) -> list[Entry]:
        """Entries whose coordinate is AT or UNDER `prefix`, respecting segment boundaries
        ('a:b' matches 'a:b' and 'a:b:c', not 'a:bx')."""
        pfx = _norm_coord(prefix)
        if not pfx:
            return list(entries)
        return [e for e in entries if e.coordinate == pfx or e.coordinate.startswith(pfx + ":")]

    # --- the COS retrieval: coordinate resolution + path-depth fallback + score/lexical rank ---
    def resolve(self, coordinate: str, query: str = "", kind: str | None = None,
                top_k: int = 5, now: int | None = None) -> list[Hit]:
        """COS coordinate resolution (§5, §7). Try the target coordinate; if nothing lives at/under
        it, ASCEND a level and retry until a node has entries or the path is exhausted (path-depth
        fallback). At the first non-empty node, rank by lexical match (if a query is given) then COS
        score (the fallback order UAME already maintains). Hits carry how far we ascended."""
        now = now if now is not None else int(time.time())
        universe = self._scan_bank(kind, now)
        if not universe:
            return []
        for i, lvl in enumerate(_ascend(coordinate)):
            cands = self._at_prefix(universe, lvl)
            if cands:
                return self._rank(cands, query, lvl, depth_drop=i)[:top_k]
        # coordinate exhausted with no hit — fall through to a store-wide content match (the broadest
        # possible fallback, depth_drop = full ascent).
        return self._rank(universe, query, "", depth_drop=len(coordinate.split(":")))[:top_k]

    def query(self, query: str, kind: str | None = None, top_k: int = 5,
              now: int | None = None) -> list[Hit]:
        """Coordinate-free 'what do I know about X' — rank all live entries by lexical match."""
        now = now if now is not None else int(time.time())
        return self._rank(self._scan_bank(kind, now), query, "", depth_drop=0)[:top_k]

    def _rank(self, cands: list[Entry], query: str, via: str, depth_drop: int) -> list[Hit]:
        q_set = set(_tokens(query)) if query else set()
        hits = []
        for e in cands:
            lex = (len(q_set & set(_tokens(e.content))) / len(q_set)) if q_set else 0.0
            if q_set and lex == 0.0:
                continue
            hits.append(Hit(entry=e, lexical=round(lex, 4), via_coord=via, depth_drop=depth_drop))
        if q_set:
            hits.sort(key=lambda h: (h.lexical, h.entry.score, h.entry.ts), reverse=True)
        else:
            hits.sort(key=lambda h: (h.entry.score, h.entry.ts), reverse=True)  # §7 score = order
        return hits

    def get(self, entry_id: str) -> Entry | None:
        with self.u._lock:
            for tbl in self.u._family_tables(BANK_FAMILY):
                r = self.u.conn.execute(f"SELECT * FROM {tbl} WHERE id=?", (entry_id,)).fetchone()
                if r:
                    return self._row_to_entry(r, tbl)
        return None

    def latest(self, coordinate: str, kind: str | None = None, now: int | None = None) -> Entry | None:
        """Freshest live entry at/under a coordinate (+optional kind). The wake-up-snapshot read:
        'newest non-expired wake_reply for this brain' — a cache hit the loop injects instead of
        paying for a new wake call. Path-depth fallback like resolve()."""
        now = now if now is not None else int(time.time())
        universe = self._scan_bank(kind, now)
        for lvl in _ascend(coordinate):
            cands = self._at_prefix(universe, lvl)
            if cands:
                return max(cands, key=lambda e: e.ts)
        return max(universe, key=lambda e: e.ts) if universe else None

    def history(self, coordinate: str, kind: str | None = None) -> list[Entry]:
        """Walk the version chain at a coordinate, NEWEST FIRST. Returns every version (current +
        all superseded), each with its own ts — the answer to 'how did this repo:file:function change
        over time'. Includes expired rows (history is the record; TTL governs the LIVE cache, not the
        chronicle). Follows the supersedes pointer back through the chain."""
        coord = _norm_coord(coordinate)
        # gather ALL versions (incl. superseded + expired) at this exact coordinate. include_expired:
        # the chronicle keeps expired rows (TTL governs the live cache, not the record of what changed).
        allv = [e for e in self._scan_bank(kind, now=int(time.time()),
                                           all_versions=True, include_expired=True)
                if e.coordinate == coord]
        if not allv:
            return []
        by_id = {e.id: e for e in allv}
        # start from the current head (newest with no live successor pointing at it) and walk back.
        superseded_ids = {e.supersedes for e in allv if e.supersedes}
        heads = [e for e in allv if e.id not in superseded_ids] or allv
        head = max(heads, key=lambda e: e.ts)
        chain, seen = [], set()
        cur = head
        while cur is not None and cur.id not in seen:
            chain.append(cur); seen.add(cur.id)
            cur = by_id.get(cur.supersedes) if cur.supersedes else None
        # append any stragglers not on the linear chain (defensive — branched history), newest first.
        for e in sorted(allv, key=lambda e: e.ts, reverse=True):
            if e.id not in seen:
                chain.append(e); seen.add(e.id)
        return chain

    def history_diff(self, coordinate: str, kind: str | None = None) -> list[dict]:
        """history() + a line-level DIFF between each version and the one it superseded. Returns, for
        each version newest-first: {ts, content, supersedes, diff} where `diff` is a unified-diff
        string from the PRIOR version to THIS one ('' for the root). Answers not just 'how did it
        change over time' but 'WHAT changed at each step' — the audit-grade version timeline."""
        import difflib
        chain = self.history(coordinate, kind)   # newest-first
        out = []
        for i, e in enumerate(chain):
            prior = chain[i + 1] if i + 1 < len(chain) else None
            if prior is None:
                diff = ""   # root version — nothing before it
            else:
                diff = "\n".join(difflib.unified_diff(
                    prior.content.splitlines(), e.content.splitlines(),
                    fromfile=f"v@{prior.ts}", tofile=f"v@{e.ts}", lineterm=""))
            out.append({"ts": e.ts, "content": e.content, "supersedes": e.supersedes, "diff": diff})
        return out

    # --- COS rating: delegate to UAME's exact formula (no duplicated scoring) ---
    def reinforce(self, entry_id: str, q: float) -> dict:
        """Rate an entry after use: Q∈[0,100] → delta=(Q−50)·k → time-decay weighted score, using
        the SAME compute_score UAME/store use for seeds. Score is fallback ORDER, never a delete
        trigger (§7 'nothing is ever deleted'; a sunk entry is a deeper fallback)."""
        with self.u._lock:
            for tbl in self.u._family_tables(BANK_FAMILY):
                r = self.u.conn.execute(
                    f"SELECT score_history,recall_count FROM {tbl} WHERE id=?", (entry_id,)).fetchone()
                if r is None:
                    continue
                hist = json.loads(r["score_history"] or "[]")
                hist.append([int(time.time()), round((q - 50.0) * SCORE_K, 3)])
                new_score = compute_score(hist)              # UAME's formula — not re-derived here
                new_recalls = r["recall_count"] + 1
                self.u._retry(f"UPDATE {tbl} SET score=?, score_history=?, recall_count=? WHERE id=?",
                              (new_score, json.dumps(hist), new_recalls, entry_id))
                self.u.conn.commit()
                return {"ok": True, "score": round(new_score, 2), "recalls": new_recalls}
        return {"ok": False, "reason": "no such entry"}

    # --- TTL / eviction (touches ONLY bank_* — the soul is structurally unreachable) ---
    def evict_expired(self, now: int | None = None) -> int:
        """Delete expired cache entries (ttl>0 and past). Legal HERE (a content cache, not the soul).
        Structurally can only DELETE FROM bank_* (the family wall). Returns the count removed."""
        now = now if now is not None else int(time.time())
        removed = 0
        with self.u._lock:
            for tbl in self.u._family_tables(BANK_FAMILY):
                cur = self.u._retry(f"DELETE FROM {tbl} WHERE ttl>0 AND (ts+ttl)<=?", (now,))
                removed += cur.rowcount if cur and cur.rowcount and cur.rowcount > 0 else 0
            self.u.conn.commit()
        return removed

    def count(self, include_expired: bool = True, now: int | None = None) -> int:
        now = now if now is not None else int(time.time())
        if not include_expired:
            return len(self._scan_bank(None, now))
        total = 0
        with self.u._lock:
            for tbl in self.u._family_tables(BANK_FAMILY):
                total += self.u.conn.execute(f"SELECT COUNT(*) c FROM {tbl}").fetchone()["c"]
        return total

    def close(self):
        self.u.close()
