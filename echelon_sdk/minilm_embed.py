"""MiniLMEmbedder — a REAL semantic embedder (all-MiniLM-L6-v2), the one honest harvest from jcode.

THE HARVEST (owner, 2026-06-20). A multi-model brainstorm council (run on the substrate's own brainstorm
cartridge, with the corrected facts + the honesty disclaimer) decided: of everything in the battle-tested
jcode repo, the ONE thing ECHELON should actually take is the EMBEDDER — all-MiniLM-L6-v2, 384-dim, local,
jcode's production choice. Everything else (their decay/score curves) ECHELON already had, in a MORE honest
form (a trace-weighted average vs jcode's self-asserted scalar confidence). So this is that single harvest.

WHY IT MATTERS: ECHELON's warmth pre-filter + v2_reflex_match used `OwnEmbedder` — a $0 stdlib co-occurrence
embedder fit on the bank's own content. Honest and dependency-free, but WEAK: meaning-match needs the corpus
to have co-placed the terms, so a true paraphrase ('piece above the king' vs 'rook over the king') only
matches once the corpus is large. A pretrained sentence model KNOWS that synonymy out of the box — turning
'weight kindled' into 'recall surfaces the RIGHT atom' (the long-standing open debt this closes).

THE DROP-IN CONTRACT — identical to OwnEmbedder so it swaps with zero call-site change:
  .fit(entries, store=None) -> self     (no-op: a pretrained model does NOT fit on the corpus)
  .embed(text) -> dict[str, float]      (the 384-dim DENSE vector as {str(index): value}, L2-normalized)
  .similarity(a, b) -> float            (dot product of two L2-normalized dicts = cosine; SAME as OwnEmbedder)
  .fitted -> bool
Representing the dense vector as a {index: value} dict lets OwnEmbedder.similarity (a sparse dot product)
score it unchanged — a MiniLM vector and an OwnEmbedder vector are both just L2-normalized coordinate maps.

GRACEFUL — never forces a heavy dep on the substrate:
  available() probes whether sentence-transformers (+ the model) actually loads. The factory make_embedder()
  picks REAL if available, else degrades to OwnEmbedder — exactly as the judge/floor degrade. So a box
  without the model keeps working on the honest weak embedder; a box WITH it is upgraded. No crash, no API.
"""
from __future__ import annotations

import math
from typing import Iterable

MODEL_NAME = "all-MiniLM-L6-v2"   # jcode's production choice; 384-dim, ~80MB, CPU-fine, fully local.
DIM = 384


def _l2_dict(vec) -> dict[str, float]:
    """A dense vector -> the {str(index): value} L2-normalized dict the OwnEmbedder similarity expects."""
    norm = math.sqrt(sum(float(x) * float(x) for x in vec)) or 1.0
    return {str(i): float(x) / norm for i, x in enumerate(vec) if x}


# THE LOCAL FLOOR embedder, inlined here (SDK is the bottom layer — it may NOT import echelon_engine, so
# the floor /v1/embeddings call lives here as pure stdlib + SDK config/keys). This is the same OpenAI-shape
# call bank_embed.embed_text makes; kept in sync (both send the Bearer key — the dead-floor-embedder fix).
_FLOOR_EMBED_MODEL = "text-embedding-nomic-embed-text-v1.5"


def _floor_embed(text: str, *, timeout: float = 30.0) -> list[float] | None:
    """Embed via the local floor (/v1/embeddings). Returns the vector or None (floor unreachable / no
    embed model / auth fail) so the caller degrades. Stdlib + SDK config/keys only — no upward import."""
    import json
    import urllib.request
    from echelon_sdk.config import floor_endpoint
    headers = {"Content-Type": "application/json"}
    try:
        from echelon_sdk.keys import load_lmstudio_key
        key = load_lmstudio_key()
        if key:
            headers["Authorization"] = f"Bearer {key}"
    except Exception:
        pass
    body = json.dumps({"model": _FLOOR_EMBED_MODEL, "input": text}).encode("utf-8")
    url = floor_endpoint("/v1/embeddings")
    try:
        req = urllib.request.Request(url, data=body, headers=headers)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))["data"][0]["embedding"]
    except Exception as exc:
        # LOUD FLOOR (slice 0): announce the failing organ + cause (auth-refused = the silent-401) before
        # degrading, so a revoked floor key can never pass silently again. See echelon_sdk.floor_degrade.
        from echelon_sdk.floor_degrade import announce_degrade
        announce_degrade(f"floor-embedder ({url})", exc)
        return None


class MiniLMEmbedder:
    """all-MiniLM-L6-v2 via sentence-transformers, behind the OwnEmbedder interface. Lazy-loads the model
    on first embed (so importing this module is free); caches embeddings per-process by text."""

    def __init__(self, model_name: str = MODEL_NAME):
        self.model_name = model_name
        self._model = None
        self._fitted = True          # pretrained — always "fitted"; fit() is a no-op
        self._cache: dict[str, dict[str, float]] = {}

    def _load(self):
        if self._model is None:
            from sentence_transformers import SentenceTransformer   # heavy; imported only on real use
            self._model = SentenceTransformer(self.model_name)
        return self._model

    def fit(self, entries: Iterable = (), store=None) -> "MiniLMEmbedder":
        """No-op (drop-in parity). A pretrained model does not learn from the corpus — that's the whole
        point of the harvest: it KNOWS synonymy already, where OwnEmbedder had to be fit each call."""
        return self

    def embed(self, text: str) -> dict[str, float]:
        text = (text or "").strip()
        if not text:
            return {}
        hit = self._cache.get(text)
        if hit is not None:
            return hit
        vec = self._load().encode(text, normalize_embeddings=False)
        out = _l2_dict(vec)
        self._cache[text] = out
        return out

    @staticmethod
    def similarity(a: dict[str, float], b: dict[str, float]) -> float:
        """Cosine = dot product of two L2-normalized dicts — SAME math as OwnEmbedder.similarity, so the
        two embedders are interchangeable to every caller."""
        if not a or not b:
            return 0.0
        if len(a) > len(b):
            a, b = b, a
        return sum(v * b.get(k, 0.0) for k, v in a.items())

    @property
    def fitted(self) -> bool:
        return self._fitted


class FloorEmbedder:
    """A REAL embedder served by the LOCAL FLOOR (LM Studio :19999, /v1/embeddings) — no Python dep, no
    model download into our venv (owner's call: 'use localprovider'). The floor already serves
    text-embedding-nomic-embed-text-v1.5 (768-dim), stronger than MiniLM and fully local. Behind the same
    .fit/.embed/.similarity contract so it drops in for OwnEmbedder. embed() returns the dense floor vector
    as a {str(i): val} L2-normalized dict; degrades (returns {} ) if the floor can't be reached, and the
    factory only hands this back when a probe succeeded, so callers still get real vectors or a clean
    OwnEmbedder fallback. Per-process text cache. See the dead-floor-embedder auth fix in bank_embed."""

    def __init__(self):
        self._fitted = True
        self._cache: dict[str, dict[str, float]] = {}

    def fit(self, entries: Iterable = (), store=None) -> "FloorEmbedder":
        return self   # served model — nothing to fit

    def embed(self, text: str) -> dict[str, float]:
        text = (text or "").strip()
        if not text:
            return {}
        hit = self._cache.get(text)
        if hit is not None:
            return hit
        vec = _floor_embed(text)
        out = _l2_dict(vec) if vec else {}
        if out:
            self._cache[text] = out
        return out

    @staticmethod
    def similarity(a: dict[str, float], b: dict[str, float]) -> float:
        if not a or not b:
            return 0.0
        if len(a) > len(b):
            a, b = b, a
        return sum(v * b.get(k, 0.0) for k, v in a.items())

    @property
    def fitted(self) -> bool:
        return self._fitted


def floor_available() -> bool:
    """Is the local floor embedder reachable (auth ok + a model loaded)? One cheap probe."""
    return _floor_embed("probe", timeout=5.0) is not None


def available(model_name: str = MODEL_NAME) -> bool:
    """Can the real embedder actually run here? True only if sentence-transformers imports AND the model
    loads. Probed lazily; any failure (no dep, no model, offline) -> False, and the factory degrades."""
    try:
        from sentence_transformers import SentenceTransformer  # noqa: F401
        MiniLMEmbedder(model_name)._load()
        return True
    except Exception:
        return False


def make_embedder(prefer: str = "auto", *, model_name: str = MODEL_NAME):
    """THE EMBEDDER FACTORY — pick the best REAL embedder available, degrade honestly. Preference order
    (best first): the LOCAL FLOOR embedder (nomic, 768-dim, no dep, owner's 'use localprovider') ->
    sentence-transformers MiniLM (if installed) -> OwnEmbedder (the stdlib floor, always works).
      prefer='floor' -> FloorEmbedder if the floor answers, else raise.
      prefer='real'  -> floor, else MiniLM, else raise (caller demands a real one).
      prefer='own'   -> always OwnEmbedder (the stdlib floor).
      prefer='auto' (DEFAULT) -> floor -> MiniLM -> OwnEmbedder (upgrade where you can, never break).
    Every returned object honors the .fit/.embed/.similarity/.fitted contract, so call sites don't change."""
    from echelon_sdk.bank_embed_own import OwnEmbedder
    if prefer == "own":
        return OwnEmbedder()
    if prefer == "floor":
        if not floor_available():
            raise RuntimeError("floor embedder unavailable (LM Studio /v1/embeddings not reachable)")
        return FloorEmbedder()
    if prefer == "real":
        if floor_available():
            return FloorEmbedder()
        if available(model_name):
            return MiniLMEmbedder(model_name)
        raise RuntimeError("no real embedder available (floor down, sentence-transformers absent)")
    # auto — the upgrade-where-you-can path
    if floor_available():
        return FloorEmbedder()
    if available(model_name):
        return MiniLMEmbedder(model_name)
    return OwnEmbedder()
