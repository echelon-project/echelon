"""cross_scope_miner.py — the VECTORIZED cross-scope near-dup miner (smart-recall slice 1, Track B).

THE PROBLEM (owner, 2026-07-08): scopes act as a regression — a trap banked in one scope repeats,
word-for-word or paraphrased, in a new scope, because traps are homed where LEARNED not where they
APPLY, new scopes are born walled, and lexical recall misses same-trap-different-words. This miner is
the MECHANICAL half of the fix: it finds every cluster of near-duplicate atoms that cross scope
boundaries, pre-sorts each cluster on mechanically-checkable signals, and emits a report. It does NOT
decide echo-vs-trap — that judged classification is the gate's job (above floor-model reliability). The
miner's contract: surface the clusters + the mechanical evidence, quantify runtime, write the report.

DESIGN CONSTRAINTS (spec slice 1):
  - VECTORIZED cosine only. Pure-Python O(n²) loops are seconds-per-pass at 927+ atoms (6252 here);
    the pairwise cosine is a single numpy matmul over the L2-normalized embedding matrix.
  - Embeddings from SPINE TEXT (slug + claim + directive) via a service-free own path — the miner is
    NOT hostage to the LM Studio floor. A deterministic token-hashing embedder gives uniform-dim dense
    vectors with no download, no service, no randomness (resumable/testable). MiniLM is a pluggable
    upgrade when installed, but the miner never REQUIRES it.
  - atoms / cards / atom_links rows are READ-ONLY. The only writes are to `atom_sidecar.embedding`
    (the rebuildable index, spec-sanctioned) and the report files. No atom body, edge, or score moves.

OUTPUT: architecture/mining-report.json (machine) + .md (human summary) in the ECHELON estate. Each
cross-scope cluster carries its members, pairwise cosines, and the mechanical pre-sort signals (kind
agreement, born-from-failure marks, content markers) so the gate can classify trap / echo / coincidence.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path

# Reuse the estate tokenizer/stopwords so the miner's "meaning" matches the rest of the substrate.
from echelon_sdk.bank_embed_own import _tokens

# ── the service-free spine-text embedder (deterministic token-hashing, uniform dim) ────────────────
# WHY HASHING (not OwnEmbedder's sparse dicts): the miner needs a UNIFORM fixed-dim dense matrix to
# vectorize the pairwise cosine as one numpy matmul. A hashing embedder maps each token to a fixed
# bucket (signed) — the classic feature-hashing trick — giving every atom a length-D vector with ZERO
# training, ZERO service, and full determinism. It captures token-overlap semantics (same words ->
# near vectors), which is exactly what near-DUP detection needs. It is weaker than MiniLM on pure
# paraphrase (no synonymy), so the cosine floor is set generously and the gate does the judged pass.
EMBED_DIM = 256
EMBED_MODEL = "spine-hash-256-v1"   # tagged into the sidecar so a future re-embed can invalidate cleanly


def spine_text(slug: str, claim: str, directive: str) -> str:
    """The text a spine atom is embedded FROM: slug + claim + directive (the HOT, served-every-recall
    layer — the body is COLD drill-only, so near-dup on the spine is near-dup on what recall compares)."""
    return " ".join(p for p in (slug or "", claim or "", directive or "") if p).strip()


def hash_embed(text: str, dim: int = EMBED_DIM):
    """Deterministic signed feature-hash of a text into a length-`dim` numpy float vector, L2-normalized.
    Each token hashes to a bucket index and a sign; collisions are the accepted hashing-trick trade-off.
    Returns a zero vector for empty text (its norm-guard keeps cosine at 0). numpy required (spec)."""
    import numpy as np
    v = np.zeros(dim, dtype=np.float64)
    toks = _tokens(text)
    if not toks:
        return v
    for t in toks:
        h = hashlib.blake2b(t.encode("utf-8"), digest_size=8).digest()
        idx = int.from_bytes(h[:4], "little") % dim
        sign = 1.0 if (h[4] & 1) else -1.0
        v[idx] += sign
    n = np.linalg.norm(v)
    return v / n if n else v


# ── data model ─────────────────────────────────────────────────────────────────────────────────────
@dataclass
class MinedAtom:
    atom_id: str
    slug: str
    scope: str
    kind: str
    claim: str
    directive: str
    born_from: str
    coordinate: str


@dataclass
class Cluster:
    """A group of near-duplicate atoms spanning ≥2 scopes (the cross-scope regression the owner named)."""
    members: list[dict]                       # MinedAtom dicts
    scopes: list[str]
    max_cosine: float
    min_cosine: float
    # mechanical pre-sort signals (NOT a classification — evidence for the gate's judged pass)
    presort: dict = field(default_factory=dict)


# ── the miner ──────────────────────────────────────────────────────────────────────────────────────
class CrossScopeMiner:
    """Read-only over atoms; writes only atom_sidecar.embedding + the report files."""

    def __init__(self, db_path: str | Path):
        self.db_path = str(db_path)
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row

    def close(self):
        self.conn.close()

    # ── load spine atoms (READ-ONLY join of atoms + atom_spine) ──────────────────────────────────────
    def load_atoms(self, scope: str | None = None) -> list[MinedAtom]:
        """Current-version spine atoms, optionally filtered to one scope. Reads atoms + atom_spine only."""
        q = ("SELECT a.id, s.slug, a.scope, a.kind, s.claim, s.directive, a.born_from, a.coordinate "
             "FROM atoms a JOIN atom_spine s ON s.atom_id = a.id")
        params: tuple = ()
        if scope is not None:
            q += " WHERE a.scope = ?"
            params = (scope,)
        rows = self.conn.execute(q, params).fetchall()
        out = []
        for r in rows:
            out.append(MinedAtom(
                atom_id=r["id"], slug=r["slug"] or "", scope=r["scope"] or "",
                kind=r["kind"] or "", claim=r["claim"] or "", directive=r["directive"] or "",
                born_from=r["born_from"] or "", coordinate=r["coordinate"] or ""))
        return out

    # ── sidecar fill (the only atom-adjacent WRITE; spec-sanctioned) ─────────────────────────────────
    def fill_sidecar(self, atoms: list[MinedAtom], vectors, *, only_empty: bool = True) -> int:
        """Write the spine-text embedding into atom_sidecar.embedding for each atom, tagged with the
        model so a future re-embed can invalidate cleanly. `only_empty=True` (default) skips rows that
        already carry a vector (never clobbers foreign-provenance legacy vectors). Returns rows written.
        This is the ONLY write to any atom-adjacent table — atoms/cards/atom_links are untouched."""
        import numpy as np
        written = 0
        cur = self.conn.cursor()
        for a, v in zip(atoms, vectors):
            row = cur.execute("SELECT embedding FROM atom_sidecar WHERE atom_id=?",
                              (a.atom_id,)).fetchone()
            existing = row["embedding"] if row else None
            if only_empty and existing:
                continue
            payload = json.dumps({"model": EMBED_MODEL,
                                  "vec": [round(float(x), 6) for x in np.asarray(v).tolist()]})
            if row is None:
                cur.execute("INSERT INTO atom_sidecar (atom_id, embedding, trigger_signals) "
                            "VALUES (?,?,?)", (a.atom_id, payload, "[]"))
            else:
                cur.execute("UPDATE atom_sidecar SET embedding=? WHERE atom_id=?",
                            (payload, a.atom_id))
            written += 1
        self.conn.commit()
        return written

    # ── the VECTORIZED near-dup pass ─────────────────────────────────────────────────────────────────
    def embed_matrix(self, atoms: list[MinedAtom]):
        """Stack every atom's spine-text hash-embedding into one (N, D) numpy matrix (L2-normalized rows).
        This is what makes the cosine a single matmul instead of an N² Python loop."""
        import numpy as np
        if not atoms:
            return np.zeros((0, EMBED_DIM))
        return np.vstack([hash_embed(spine_text(a.slug, a.claim, a.directive)) for a in atoms])

    def near_dup_pairs(self, atoms: list[MinedAtom], matrix, *, threshold: float):
        """Vectorized cosine over the L2-normalized matrix (M @ M.T), then extract the upper-triangle
        pairs above `threshold` whose two members are in DIFFERENT scopes. Returns a list of
        (i, j, cosine) sorted by cosine desc. The whole pairwise similarity is ONE matmul — O(N²) memory
        but no Python-level pair loop; the only loop is over the already-thresholded sparse hit set."""
        import numpy as np
        n = len(atoms)
        if n < 2:
            return []
        sims = matrix @ matrix.T                       # (N,N) cosines (rows are unit vectors)
        iu, ju = np.triu_indices(n, k=1)               # upper triangle, no diagonal, no dup pairs
        cvals = sims[iu, ju]
        mask = cvals >= threshold
        iu, ju, cvals = iu[mask], ju[mask], cvals[mask]
        # cross-scope filter — the whole point: same-scope near-dups are a different (consolidation) job
        scopes = np.array([a.scope for a in atoms])
        cross = scopes[iu] != scopes[ju]
        iu, ju, cvals = iu[cross], ju[cross], cvals[cross]
        order = np.argsort(-cvals)
        return [(int(iu[k]), int(ju[k]), float(cvals[k])) for k in order]

    def cluster(self, atoms: list[MinedAtom], pairs) -> list[Cluster]:
        """Union-find the cross-scope pairs into connected components (a repeated trap may span 3+ scopes).
        Each component with members in ≥2 distinct scopes becomes a Cluster."""
        parent = list(range(len(atoms)))

        def find(x):
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        def union(a, b):
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[ra] = rb

        pair_cos: dict[tuple[int, int], float] = {}
        for i, j, c in pairs:
            union(i, j)
            pair_cos[(min(i, j), max(i, j))] = c

        comps: dict[int, list[int]] = {}
        for idx in range(len(atoms)):
            comps.setdefault(find(idx), []).append(idx)

        clusters: list[Cluster] = []
        for members in comps.values():
            if len(members) < 2:
                continue
            member_atoms = [atoms[m] for m in members]
            scopes = sorted({a.scope for a in member_atoms})
            if len(scopes) < 2:
                continue   # a single-scope component is NOT the cross-scope regression we mine
            # cosines within this component (only the pairs we actually found)
            cs = [c for (i, j), c in pair_cos.items() if i in members and j in members]
            clusters.append(Cluster(
                members=[asdict(a) for a in member_atoms],
                scopes=scopes,
                max_cosine=round(max(cs), 4) if cs else 0.0,
                min_cosine=round(min(cs), 4) if cs else 0.0,
                presort=self._presort(member_atoms)))
        # tightest, most-cross-scope clusters first — the gate reviews the strongest evidence first
        clusters.sort(key=lambda c: (len(c.scopes), c.max_cosine), reverse=True)
        return clusters

    # ── mechanical pre-sort (evidence, NOT classification) ───────────────────────────────────────────
    _FAILURE_MARKS = re.compile(
        r"\b(trap|bug|fail(?:ed|ure)?|broke|broken|regress|crash|silent|swallow|wrong|mistake|"
        r"gotcha|footgun|pitfall|don'?t|never|avoid|caught|caused|corrupt|stale|leak)\b", re.I)

    def _born_from_failure(self, born_from: str) -> bool:
        """A mechanically-checkable birth-from-failure signal: the born_from provenance names a
        disclaim/dispute/dream-consolidation or carries a failure verb. One leg of the trap tag the
        spec asserts at wrap — surfaced here as evidence for the gate, never asserted by the miner."""
        b = (born_from or "").lower()
        return bool(b) and (
            "disclaim" in b or "dispute" in b or "superseded" in b or "dream" in b
            or bool(self._FAILURE_MARKS.search(b)))

    # Scopes whose name marks them as throwaway/synthetic test fixtures — NOT the owner's real estate.
    # A cluster living in these is almost certainly test pollution, not a lived cross-scope trap. Surfaced
    # as a mechanical flag so the gate discounts it; the miner never DELETES or filters (read-only law).
    _TEST_SCOPE_MARK = re.compile(
        r"(^_|\b)(test|synth|synthesis|wire|tmp|scratch|demo|fixture|sample|throwaway|drift|thrash|"
        r"regress|selftest|ingesttest|reflextest|echtest)\b", re.I)

    def _is_test_scope(self, scope: str) -> bool:
        return bool(self._TEST_SCOPE_MARK.search(scope or ""))

    def _presort(self, member_atoms: list[MinedAtom]) -> dict:
        """Mechanical signals the gate uses to bin trap / echo / coincidence. NO judgement here —
        every field is a boolean/count a machine can check, so the gate's judged pass has a scaffold."""
        kinds = {a.kind for a in member_atoms}
        failure_flags = [self._born_from_failure(a.born_from) for a in member_atoms]
        claim_marks = [bool(self._FAILURE_MARKS.search(a.claim)) for a in member_atoms]
        test_scopes = sorted({a.scope for a in member_atoms if self._is_test_scope(a.scope)})
        return {
            # trap candidate = MULTIPLE members born from failure (spec: "both members born-from-failure")
            "all_born_from_failure": all(failure_flags) and len(failure_flags) >= 2,
            "any_born_from_failure": any(failure_flags),
            "born_from_failure_count": sum(failure_flags),
            # doctrine-echo hint: same kind + no failure marks = the soul being coherent, not a repeat trap
            "same_kind": len(kinds) == 1,
            "kinds": sorted(kinds),
            "claim_has_failure_marker": any(claim_marks),
            "claim_failure_marker_count": sum(claim_marks),
            # test-fixture pollution flag: the gate should discount clusters that live in throwaway scopes
            "has_test_scope": bool(test_scopes),
            "test_scopes": test_scopes,
            "all_test_scopes": len(test_scopes) == len({a.scope for a in member_atoms}),
            "member_count": len(member_atoms),
            "scope_count": len({a.scope for a in member_atoms}),
        }

    # ── orchestration ────────────────────────────────────────────────────────────────────────────────
    def run(self, *, scope: str | None = None, threshold: float = 0.82,
            fill: bool = True) -> dict:
        """End-to-end: load -> embed -> (fill sidecar) -> vectorized near-dup -> cluster -> report dict.
        `scope=None` mines ACROSS ALL scopes (the cross-scope regression). Pass a scope to restrict the
        corpus (e.g. only echelon-related). Returns the report dict (also written to disk by write_report)."""
        t0 = time.time()
        atoms = self.load_atoms(scope=scope)
        t_load = time.time()
        matrix = self.embed_matrix(atoms)
        t_embed = time.time()
        written = self.fill_sidecar(atoms, matrix, only_empty=True) if fill else 0
        t_fill = time.time()
        pairs = self.near_dup_pairs(atoms, matrix, threshold=threshold)
        clusters = self.cluster(atoms, pairs)
        t_cluster = time.time()

        # mechanical pre-sort tallies (NOT the deliverable count — the gate collapses echoes)
        trap_candidates = sum(1 for c in clusters if c.presort.get("all_born_from_failure"))
        echo_hints = sum(1 for c in clusters
                         if c.presort.get("same_kind") and not c.presort.get("any_born_from_failure"))
        real_clusters = [c for c in clusters if not c.presort.get("all_test_scopes")]
        real_failure_clusters = sum(1 for c in real_clusters
                                    if c.presort.get("any_born_from_failure")
                                    or c.presort.get("claim_has_failure_marker"))
        return {
            "generated_by": "cross_scope_miner (slice 1) — MECHANICAL only; gate does echo-vs-trap",
            "embed_model": EMBED_MODEL,
            "embed_dim": EMBED_DIM,
            "cosine_threshold": threshold,
            "scope_filter": scope or "(all scopes)",
            "atom_count": len(atoms),
            "scope_count": len({a.scope for a in atoms}),
            "sidecar_rows_written": written,
            "cross_scope_pair_count": len(pairs),
            "cross_scope_cluster_count": len(clusters),
            "mechanical_presort": {
                "trap_candidates_all_born_from_failure": trap_candidates,
                "echo_hints_same_kind_no_failure": echo_hints,
                "clusters_excluding_pure_test_scopes": len(real_clusters),
                "real_estate_failure_signal_clusters": real_failure_clusters,
                "note": "these are PRE-SORT bins for the gate, NOT the final trap/echo/coincidence counts. "
                        "'real_estate_failure_signal_clusters' = non-test clusters carrying a born-from-failure "
                        "OR claim failure marker — the gate's highest-priority review queue.",
            },
            "runtime_seconds": {
                "load": round(t_load - t0, 3),
                "embed": round(t_embed - t_load, 3),
                "sidecar_fill": round(t_fill - t_embed, 3),
                "near_dup_and_cluster": round(t_cluster - t_fill, 3),
                "total": round(t_cluster - t0, 3),
            },
            "clusters": [asdict(c) for c in clusters],
        }


# ── report writers ──────────────────────────────────────────────────────────────────────────────────
def write_report(report: dict, json_path: str | Path, md_path: str | Path) -> None:
    """Write the machine report (.json) + a human summary (.md). The .json is the gate's input; the .md
    is the readable digest. Neither touches the bank — reports live in the estate's architecture dir."""
    json_path, md_path = Path(json_path), Path(md_path)
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    md_path.write_text(_render_md(report), encoding="utf-8")


def _render_md(r: dict) -> str:
    L: list[str] = []
    L.append("# Cross-Scope Near-Dup Mining Report (smart-recall slice 1)\n")
    L.append(f"- **Embedder:** `{r['embed_model']}` ({r['embed_dim']}-dim, service-free spine-text hash)")
    L.append(f"- **Cosine threshold:** {r['cosine_threshold']}")
    L.append(f"- **Corpus:** {r['atom_count']} atoms across {r['scope_count']} scopes "
             f"(filter: {r['scope_filter']})")
    L.append(f"- **Sidecar rows filled:** {r['sidecar_rows_written']}")
    L.append(f"- **Cross-scope near-dup pairs:** {r['cross_scope_pair_count']}")
    L.append(f"- **Cross-scope clusters:** {r['cross_scope_cluster_count']}")
    ps = r["mechanical_presort"]
    L.append(f"- **Mechanical pre-sort:** {ps['trap_candidates_all_born_from_failure']} trap-candidate "
             f"clusters (all members born-from-failure), "
             f"{ps['echo_hints_same_kind_no_failure']} echo-hint clusters (same kind, no failure marks)")
    rt = r["runtime_seconds"]
    L.append(f"- **Runtime:** {rt['total']}s total "
             f"(embed {rt['embed']}s, near-dup+cluster {rt['near_dup_and_cluster']}s)\n")
    L.append("> The raw cluster count is NOT the trap count — it is inflated by doctrine echoes. The "
             "GATE classifies each cluster trap / echo / coincidence; this report is the mechanical scaffold.\n")
    L.append("## Clusters (tightest, most cross-scope first)\n")
    for i, c in enumerate(r["clusters"], 1):
        L.append(f"### Cluster {i} — scopes: {', '.join(c['scopes'])} "
                 f"(cosine {c['min_cosine']}–{c['max_cosine']})")
        p = c["presort"]
        L.append(f"- pre-sort: all_born_from_failure={p['all_born_from_failure']}, "
                 f"born_from_failure_count={p['born_from_failure_count']}/{p['member_count']}, "
                 f"same_kind={p['same_kind']} ({', '.join(p['kinds'])}), "
                 f"claim_failure_marks={p['claim_failure_marker_count']}")
        for m in c["members"]:
            L.append(f"  - `[{m['scope']}]` **{m['slug']}** — {m['claim'][:110]}")
        L.append("")
    return "\n".join(L)
