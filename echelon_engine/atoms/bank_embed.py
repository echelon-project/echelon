"""The Knowledge Bank's SEMANTIC tier (COSys_DESIGN §10 P10, §23.5) — embeddings as the >200-entry
scale escalation above the lexical floor.

WHY EMBEDDINGS ARE LEGITIMATE HERE (and NOT in the soul): the owner's law "weight is process-based"
makes embeddings WRONG for the SOUL — recognition/identity is a process (the judge), not a frozen
vector (memory: warmth-is-emotional, memory-is-a-weight-adjustor). But the KNOWLEDGE BANK is a
different organ: content lookup, "what do I factually know about X." COSys_DESIGN itself names vector
embeddings as the scale path for semantic retrieval (§10 P10: "< 200 = context, > 200 = embeddings";
§23.5 "embedding infrastructure for scale"). So embeddings serve the bank's RETRIEVAL; they never
touch the soul's recognition. Two organs, two methods — exactly the COS × ENTRY vs COS × UAME split.

TIERING (compute-tiering-strategy): the LEXICAL floor (bank.query/resolve, free, deterministic,
always-on) stays the floor. The semantic tier escalates ONLY when (a) the corpus is large enough to
need it (>SEMANTIC_MIN entries, §10 P10) AND (b) the caller asks. Embeddings come from the LOCAL
FLOOR FIRST (LM Studio :19999, OpenAI-shaped /v1/embeddings — the free RX-6800 tier, local-llm-floor-
setup); if no embedding model is loaded it degrades to lexical, exactly as the judge degrades. No
paid API for routine recall.

Vectors are CACHED on the entry (content-addressed: an entry's id already pins its content, so its
embedding is stable until the content versions — a new version is a new id, a new vector). The cache
lives in a sidecar table `bank_vectors` so the bank's content rows stay clean and the soul is
untouched. A cache miss embeds on demand and stores.

See: cos-x-entry-coordinate-spine, knowledge-bank-cos-x-entry-rag, compute-tiering-strategy,
local-llm-floor-setup.
"""
from __future__ import annotations
import json
import math
import struct
import urllib.request
from dataclasses import dataclass

# §10 P10: below this entry count, lexical-in-context is sufficient; embeddings are a migration, not
# a prerequisite. The tier refuses to escalate below it (don't pay for vectors you don't need).
SEMANTIC_MIN = 200

# Local floor (local-llm-floor-setup): LM Studio, OpenAI-shaped. An embedding model must be loaded
# (e.g. nomic-embed-text / bge). If the endpoint 400s/refuses, we degrade to lexical.
from echelon_sdk.config import floor_endpoint
LOCAL_EMBED_URL = floor_endpoint("/v1/embeddings")  # config 'floor.host' / LM_ENDPOINT env, not a hardcoded host
LOCAL_EMBED_MODEL = "text-embedding-nomic-embed-text-v1.5"   # override via embed_model=

# NOT under the bank_ prefix: bank_* is the CONTENT family (walked by KnowledgeBank._scan_bank).
# A sidecar with a different schema must sit outside that prefix or it gets swept into a content scan
# (the bug that proved this). `embed_vectors` is the vector cache, addressed by entry id.
_VECTORS_TABLE = "embed_vectors"
_VEC_COLS = """
    id     TEXT PRIMARY KEY,   -- the entry id this vector belongs to (content-addressed -> stable)
    model  TEXT NOT NULL,      -- which embed model produced it (a model change invalidates the cache)
    dim    INTEGER NOT NULL,
    vec    BLOB NOT NULL,      -- float32 little-endian packed
    ts     INTEGER NOT NULL
"""


def _pack(v: list[float]) -> bytes:
    return struct.pack(f"<{len(v)}f", *v)


def _unpack(b: bytes, dim: int) -> list[float]:
    return list(struct.unpack(f"<{dim}f", b))


def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


@dataclass
class SemHit:
    entry: object        # a uame.Entry (bank family)
    similarity: float    # cosine similarity 0..1
    via_coord: str = ""


def embed_text(text: str, *, url: str = LOCAL_EMBED_URL, model: str = LOCAL_EMBED_MODEL,
               timeout: float = 30.0) -> list[float] | None:
    """Embed one text via the local floor (/v1/embeddings, OpenAI shape). Returns the vector, or None
    if the floor is unavailable / no embed model loaded (caller degrades to lexical). Stdlib only.

    AUTH (2026-06-20, the dead-floor-embedder fix): the floor requires a Bearer key — without it every
    embed 401s and the tier degraded SILENTLY to lexical, so the REAL local embedder
    (text-embedding-nomic-embed-text-v1.5, served on :19999) never actually ran. This was the substance
    behind 'swap the FAKE embedder': the real one was wired but unauthenticated. Send the floor key (best-
    effort — if it can't load, fall through keyless so a no-auth endpoint still works)."""
    headers = {"Content-Type": "application/json"}
    try:
        from echelon_sdk.keys import load_lmstudio_key
        key = load_lmstudio_key()
        if key:
            headers["Authorization"] = f"Bearer {key}"
    except Exception:
        pass
    body = json.dumps({"model": model, "input": text}).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return data["data"][0]["embedding"]
    except Exception as exc:
        # LOUD FLOOR (slice 0): the degrade is fine, a SILENT degrade is the bug. Announce which organ
        # died and why (auth-refused = the silent-401 this kills), then fall to lexical exactly as before.
        from echelon_sdk.floor_degrade import announce_degrade
        announce_degrade(f"floor-embedder ({url})", exc)
        return None   # no floor / no embed model / shape mismatch -> degrade to lexical


class SemanticTier:
    """Embedding escalation over a KnowledgeBank. Cached vectors in a sidecar table; cosine top-k.

    Usage: bank.query() (lexical) stays the floor; when the corpus is large and the caller wants
    meaning-match across paraphrase, semantic.search(query) ranks by cosine. Degrades to [] (let the
    caller fall back to lexical) if the floor has no embed model."""

    def __init__(self, bank, url: str = LOCAL_EMBED_URL, model: str = LOCAL_EMBED_MODEL,
                 source: str = "own", store=None):
        """source: 'own' (DEFAULT) = our soul-shaped stdlib embedder (bank_embed_own — $0, no deps,
        learned from the bank's own content + the soul graph; owner's call, own-embedder-soul-shaped).
        'model' = the pluggable escalation to an embedding model server / hosted API (LOCAL_EMBED_URL /
        a remote OpenAI-shaped endpoint) for strong out-of-box general paraphrase when worth the dep/cost.
        `store` (the SeedStore) enables the soul-graph blend in 'own' mode."""
        self.bank = bank
        self.u = bank.u
        self.url = url
        self.model = model
        self.source = source
        self._store = store
        self._own = None   # lazily fit OwnEmbedder
        with self.u._lock:
            self.u.conn.execute(f"CREATE TABLE IF NOT EXISTS {_VECTORS_TABLE} ({_VEC_COLS})")
            self.u.conn.commit()

    def _fit_own(self, entries):
        """(Re)fit the ranking embedder. THE HARVEST (2026-06-20, council-decided): prefer the REAL
        all-MiniLM-L6-v2 (jcode's proven local model) when it loads, else degrade to OwnEmbedder (the
        stdlib floor). make_embedder('auto') makes that choice. For MiniLM, fit() is a no-op (a pretrained
        model already knows synonymy — the whole point); for OwnEmbedder it still fits on the corpus +
        soul graph. The drop-in contract (.fit/.embed/.similarity) is identical, so search() is unchanged.
        See minilm-embedder-is-the-one-jcode-harvest."""
        from echelon_sdk.minilm_embed import make_embedder
        self._own = make_embedder("auto").fit(entries, store=self._store)
        return self._own

    def available(self) -> bool:
        """Can the tier embed? 'own' is ALWAYS available (pure stdlib, no service). 'model' probes the
        endpoint (a model must be loaded / the API reachable)."""
        if self.source == "own":
            return True
        return embed_text("probe", url=self.url, model=self.model, timeout=5.0) is not None

    def _get_vec(self, entry_id: str):
        with self.u._lock:
            r = self.u.conn.execute(
                f"SELECT model,dim,vec FROM {_VECTORS_TABLE} WHERE id=?", (entry_id,)).fetchone()
        if r and r["model"] == self.model:
            return _unpack(r["vec"], r["dim"])
        return None

    def _put_vec(self, entry_id: str, vec: list[float]) -> None:
        import time
        with self.u._lock:
            self.u._retry(
                f"INSERT OR REPLACE INTO {_VECTORS_TABLE} (id,model,dim,vec,ts) VALUES (?,?,?,?,?)",
                (entry_id, self.model, len(vec), _pack(vec), int(time.time())))
            self.u.conn.commit()

    def ensure_embedded(self, entries) -> int:
        """Embed any entries missing a current-model vector (cache-fill). Returns how many were
        newly embedded. A no-op for already-cached entries — content-addressed ids make vectors
        stable until a content version creates a new id."""
        n = 0
        for e in entries:
            if self._get_vec(e.id) is None:
                v = embed_text(e.content, url=self.url, model=self.model)
                if v is None:
                    break   # floor went away — stop, caller degrades
                self._put_vec(e.id, v)
                n += 1
        return n

    def search(self, query: str, kind: str | None = None, top_k: int = 5,
               force: bool = False, now: int | None = None) -> list[SemHit]:
        """Semantic top-k over live bank entries by cosine. Escalation-gated: returns [] (telling the
        caller to use the lexical floor) when the corpus is below SEMANTIC_MIN unless force=True, or
        (in 'model' mode) when the embed endpoint is unavailable. 'own' mode never needs a service."""
        import time
        now = now if now is not None else int(time.time())
        entries = self.bank._scan_bank(kind, now)   # current versions only, live
        if not entries:
            return []
        if len(entries) < SEMANTIC_MIN and not force:
            return []   # §10 P10 — below scale, lexical floor is sufficient; don't escalate

        if self.source == "own":
            # OUR embedder: fit on the live corpus + soul graph, sparse-cosine rank. $0, no service.
            own = self._fit_own(entries)
            qv = own.embed(query)
            if not qv:
                return []
            # Rank ALL entries by similarity and let top_k cut — DON'T gate on sim>0. The old gate fit the
            # sparse OwnEmbedder (where sim==0 means "no shared terms" = true absence), but the now-default
            # FloorEmbedder is dense nomic-768 with a SIGNED cosine in [-1,1]: sim<=0 is meaningful signal,
            # not absence, so gating it silently dropped real candidates that the 'model' path below keeps —
            # an asymmetry surfaced by the memanto peer-study (their documented threshold=0 trap). Parity
            # with the 'model' path: no implicit floor; a threshold is the caller's explicit choice.
            # See [[memanto-peerstudy-zero-gate-asymmetry]], [[minilm-embedder-is-the-one-jcode-harvest]].
            hits = [SemHit(entry=e, similarity=round(own.similarity(qv, own.embed(e.content)), 4),
                           via_coord=e.coordinate)
                    for e in entries]
            hits.sort(key=lambda h: h.similarity, reverse=True)
            return hits[:top_k]

        # 'model' mode: an embedding model server / hosted API (the pluggable escalation).
        qv = embed_text(query, url=self.url, model=self.model)
        if qv is None:
            return []   # endpoint unavailable -> caller falls back to lexical
        self.ensure_embedded(entries)
        hits = []
        for e in entries:
            v = self._get_vec(e.id)
            if v is None:
                continue
            hits.append(SemHit(entry=e, similarity=round(_cosine(qv, v), 4), via_coord=e.coordinate))
        hits.sort(key=lambda h: h.similarity, reverse=True)
        return hits[:top_k]
