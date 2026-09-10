"""Our OWN embedder — soul-shaped, pure stdlib, learned from the substrate's own material.

Owner (2026-06-06): don't reach for a downloaded embedding model or a hosted API — BUILD OUR OWN. An
embedding is just text->vector where similar meanings land near each other; you can CONSTRUCT that from
co-occurrence in your OWN corpus + proximity in the SOUL GRAPH, no borrowed weights. A downloaded
embedder is the embedding CACHE-LIE: frozen weights that SEEM to know your domain but never lived it.
Co-occurrence-from-your-own-content is the embedding equivalent of warmth — meaning from lived material.
See memory: own-embedder-soul-shaped, memory-is-a-weight-adjustor, warmth-is-emotional.

THE METHOD (distributional semantics — "you shall know a word by the company it keeps"), pure stdlib:
  - The FLOOR has no numpy (the agent's clean dependency floor is a value — repo_graph note: "adds
    ZERO dependencies"). So vectors are SPARSE DICTS, not dense arrays; similarity is sparse cosine
    (dict dot-product). No matrix library, no SVD, no download, $0.
  - Each TERM gets a context vector: the bag of OTHER terms it co-occurs with across bank entries,
    PPMI-weighted (positive pointwise mutual information — co-occur more than chance => positive
    weight; this is the classic count-based word-embedding, pre-neural and still strong on a focused
    corpus). gzip/zcat/decompress end up with overlapping contexts because they share neighbours in
    OUR memories -> their vectors are close -> "unzip" finds "decompress" with no shared surface word.
  - SOUL-GRAPH BLEND (the ECHELON-only signal, no off-the-shelf embedder has this): an entry that
    links into a soul seed via uame_links (instances/refines, multi-hop) ABSORBS that seed's terms
    into its context. Two entries reaching the same VALUE then share context THROUGH THE SOUL — meaning
    shaped by what the agent has lived, not just lexical company.
  - An ENTRY/QUERY vector = the (sparse, summed, L2-normalized) term vectors of its tokens. Cosine of
    two such vectors = semantic similarity.

HONEST LIMIT (owner accepted): weaker on NEVER-SEEN words — a term with no co-occurrences yet is cold
until the bank accumulates context around it. That "warms up over time vs works out-of-the-box" is the
SAME SHAPE as warmth itself (cold on new ground, warm with experience) — coherent for THIS substrate.
The model/API tier stays available as a pluggable escalation for strong out-of-box general paraphrase.
"""
from __future__ import annotations
import math
import re
from collections import defaultdict

_WORD = re.compile(r"[a-z0-9]+")
_STOP = frozenset(
    "the a an and or but to of in on at for with by is are was were be been being "
    "this that these those it its as do did does done from not no yes i you he she we "
    "they them his her their our your my me will would can could should have has had "
    "if then else when what which who how why where".split())

# How far an entry's soul-graph links reach when absorbing seed terms into its context (the blend).
# 1 = direct seeds an entry instances; deeper hops fade (graph-distance like multi-hop warmth).
_SOUL_HOPS = 2
_SOUL_HOP_DECAY = 0.6   # a seed two hops away contributes 0.6^1 of its term weight, etc.
_SOUL_BOND = 1.5        # strength of a soul-link term injection (post-PPMI overlay). A deliberate
                        # authored bond is stronger than a statistical co-occurrence; this weight
                        # makes the ECHELON signal survive PPMI's tiny-corpus degeneracy at any scale.


def _tokens(text: str) -> list[str]:
    return [w for w in _WORD.findall((text or "").lower()) if w not in _STOP and len(w) > 2]


class OwnEmbedder:
    """A count-based, soul-blended, sparse embedder fit on the bank's own corpus + soul graph.

    fit(entries, store) builds the term-context model; embed(text) returns a sparse vector (dict);
    similarity(a, b) is sparse cosine. Re-fit after the corpus grows materially (cheap — pure dict
    counting). No persistence needed: the model IS derived from the bank, so it's rebuilt from the
    bank — storing it would be a (small) cache-lie; recompute is honest and fast at this scale."""

    def __init__(self):
        self._ctx: dict[str, dict[str, float]] = {}   # term -> {co-term: ppmi weight} (L2-normalized)
        self._fitted = False

    def fit(self, entries, store=None) -> "OwnEmbedder":
        """Learn term contexts from entry texts (co-occurrence within an entry) + the soul graph.
        `entries` = iterable with `.content` (and `.id` if soul-blending). `store` = the SeedStore/
        bank whose `.u` carries uame_links + seed lookup, for the soul blend (optional)."""
        # 1) raw co-occurrence counts: how often term i and term j appear in the same entry.
        cooc: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
        term_count: dict[str, float] = defaultdict(float)
        pair_total = 0.0
        docs = []
        for e in entries:
            toks = set(_tokens(getattr(e, "content", "") or getattr(e, "text", "")))
            docs.append((getattr(e, "id", None), toks))
            for t in toks:
                term_count[t] += 1.0
            for a in toks:
                for b in toks:
                    if a != b:
                        cooc[a][b] += 1.0
                        pair_total += 1.0

        # 2) PPMI weighting of the corpus co-occurrence: w(i,j) = max(0, log( P(i,j)/(P(i)P(j)) )).
        # Positive => co-occur more than chance. Classic count-based embedding weight. NOTE PPMI is
        # degenerate on a TINY corpus (every pair co-occurs ~once -> ppmi<=0); that's fine — the
        # semantic tier only ENGAGES above SEMANTIC_MIN entries, where counts are meaningful. The soul
        # overlay (step 3) is INDEPENDENT of corpus volume, so the ECHELON signal works at any scale.
        total_terms = sum(term_count.values()) or 1.0
        ctx: dict[str, dict[str, float]] = defaultdict(dict)
        for a, neigh in cooc.items():
            pa = term_count[a] / total_terms
            for b, c in neigh.items():
                pab = c / (pair_total or 1.0)
                pb = term_count[b] / total_terms
                if pab > 0 and pa > 0 and pb > 0:
                    ppmi = math.log(pab / (pa * pb))
                    if ppmi > 0:
                        ctx[a][b] = ppmi

        # 3) SOUL-GRAPH OVERLAY (the ECHELON-only signal): a uame_link is a DELIBERATE semantic bond
        # the agent authored — far stronger than a statistical co-occurrence whisper, and it must NOT
        # be washed out by PPMI's tiny-corpus degeneracy. So we add the reached seeds' terms DIRECTLY
        # to each linked entry-token's context with a fixed strong weight (post-PPMI, never zeroed).
        # Two entries reaching the same soul VALUE then share these injected dimensions -> their vectors
        # are pulled close THROUGH the soul, at any corpus size. This is meaning from lived structure.
        if store is not None:
            for eid, toks in docs:
                if not eid:
                    continue
                for seed_terms, w in self._soul_terms(eid, store):
                    bond = _SOUL_BOND * w
                    for a in toks:
                        for st in seed_terms:
                            if a != st:
                                ctx[a][st] = ctx[a].get(st, 0.0) + bond

        # 4) L2-normalize each term's context vector.
        self._ctx = {}
        for a, row in ctx.items():
            norm = math.sqrt(sum(v * v for v in row.values())) or 1.0
            self._ctx[a] = {b: v / norm for b, v in row.items()}
        self._fitted = True
        return self

    def _soul_terms(self, entry_id: str, store):
        """Yield (set_of_seed_terms, weight) for seeds the entry reaches via uame_links, up to
        _SOUL_HOPS, weight decaying per hop. Uses the store's UAME links_of + seed_by_id."""
        u = getattr(store, "u", None)
        if u is None or not hasattr(u, "links_of"):
            return
        seen = {entry_id}
        frontier = [(entry_id, 0)]
        while frontier:
            nid, hop = frontier.pop()
            if hop >= _SOUL_HOPS:
                continue
            try:
                edges = u.links_of(nid)
            except Exception:
                continue
            for e in edges:
                other = e["to_id"] if e["from_id"] == nid else e["from_id"]
                if other in seen:
                    continue
                seen.add(other)
                seed = store.seed_by_id(other) if hasattr(store, "seed_by_id") else None
                if seed is not None:
                    yield set(_tokens(seed.content)), (_SOUL_HOP_DECAY ** hop)
                frontier.append((other, hop + 1))

    def embed(self, text: str) -> dict[str, float]:
        """A sparse vector for text: sum of its terms' context vectors, L2-normalized. A term unseen
        in the corpus contributes only itself (so identical words still match; meaning-match needs the
        corpus to have placed the term). Empty if no usable tokens."""
        toks = _tokens(text)
        if not toks:
            return {}
        acc: dict[str, float] = defaultdict(float)
        for t in toks:
            ctx = self._ctx.get(t)
            if ctx:
                for b, v in ctx.items():
                    acc[b] += v
            # self-term anchor so exact-word overlap always contributes (lexical floor inside the vec)
            acc[t] += 1.0
        norm = math.sqrt(sum(v * v for v in acc.values())) or 1.0
        return {k: v / norm for k, v in acc.items()}

    @staticmethod
    def similarity(a: dict[str, float], b: dict[str, float]) -> float:
        """Sparse cosine — dot product of two L2-normalized sparse dicts (0..1-ish)."""
        if not a or not b:
            return 0.0
        # iterate the smaller dict
        if len(a) > len(b):
            a, b = b, a
        return sum(v * b.get(k, 0.0) for k, v in a.items())

    @property
    def fitted(self) -> bool:
        return self._fitted
