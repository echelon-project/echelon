"""The COS Guard (COSys_DESIGN §8) — keep the taxonomy from rotting.

The problem the guard solves (§8): an LLM placing knowledge will take shortcuts — invent a junk
intermediate node (`important_stuff:misc:other`) to reach an atom faster. That pollutes the taxonomy
so future coordinate retrieval (bank.resolve's path-fallback) gets unreliable. The guard validates a
coordinate BEFORE it commits and re-anchors a bad one onto the existing taxonomy.

ADAPTATION for an AUTONOMOUS agent (reclaim-the-method, not copy): the spec's guard PAUSES to ask the
human to approve each new node. An agent in a tool-loop cannot block on a human per write. So:
  - The SCHEMA is LEARNED from what already exists — the set of node names seen at each depth across
    the bank IS the schema (no hand-maintained JSON schema file; the taxonomy defines itself).
  - VALIDATE = a coordinate's segments are checked against (a) junk-name patterns (misc/other/stuff/
    temp/...) — always rejected; (b) the learned schema — a never-seen node at a depth is a NOVELTY,
    flagged, not silently blessed.
  - RE-ANCHOR = on a junk/novel node, suggest the closest EXISTING sibling at that depth (highest
    score / most-used first, §8 "options in descending score order"), so the agent re-classifies onto
    the real taxonomy instead of forking a junk branch.
  - DEPTH GUARD (§8 P5 fix): reject a placement that's too shallow to be atomic — but check for
    CONTENT-bearing depth, not a hardcoded `detailed` level (levels aren't fixed in count, §4).
  - The guard ADVISES by default (returns a verdict); strict=True makes validate() raise so a caller
    that wants enforcement gets it. Enforce-don't-request stays available; the loop default is advise
    (an agent acts on the advice, like it acts on warmth).

See memory: cos-x-entry-coordinate-spine, reclaim-the-method (adapt, don't copy the human-in-loop).
"""
from __future__ import annotations
import re
from dataclasses import dataclass, field

# Junk node-names an LLM reaches for to skip taxonomy work — always rejected (§8 the core failure).
_JUNK = frozenset(
    "misc other stuff things temp tmp various general_stuff random etc misc_stuff "
    "uncategorized untagged todo placeholder foo bar baz data1 new old".split())
_SEG_OK = re.compile(r"^[a-z0-9][a-z0-9_]*$")   # a legal segment: lowercase token, no separators
_MIN_DEPTH = 2   # a coordinate must reach at least this depth to be content-bearing (not a bare
                 # top-level branch). domain+topic minimum; the spec's depth guard, generalized.


@dataclass
class GuardVerdict:
    ok: bool
    coordinate: str                       # the (normalized) coordinate as judged
    issues: list = field(default_factory=list)        # human-readable problems found
    novel_segments: list = field(default_factory=list)  # segments never seen at their depth
    reanchor: dict = field(default_factory=dict)        # {bad_segment: [suggested existing siblings]}

    def __bool__(self) -> bool:
        return self.ok


class CoordinateGuard:
    """Validates COS coordinates against a taxonomy LEARNED from the bank's existing entries.

    The schema = {depth_index: {node_names seen at that depth, with a use-count}}. Built lazily from
    the bank's live coordinates; refresh() re-reads after a batch of writes."""

    def __init__(self, bank=None):
        self.bank = bank
        # depth -> {segment: count}. Learned, not declared.
        self._schema: dict[int, dict[str, int]] = {}
        if bank is not None:
            self.refresh()

    def refresh(self) -> None:
        """Re-learn the taxonomy from the bank's current (live, all-version) coordinates."""
        self._schema = {}
        if self.bank is None:
            return
        import time
        for e in self.bank._scan_bank(None, int(time.time()), all_versions=True):
            self._observe(e.coordinate)

    def _observe(self, coordinate: str) -> None:
        for depth, seg in enumerate(coordinate.split(":")):
            if not seg:
                continue
            self._schema.setdefault(depth, {})
            self._schema[depth][seg] = self._schema[depth].get(seg, 0) + 1

    def learn(self, coordinate: str) -> None:
        """Accept a coordinate into the schema (call after a validated write, so subsequent writes at
        the same node aren't flagged novel). Keeps the learned schema warm without a full refresh."""
        self._observe(_norm(coordinate))

    def _siblings(self, depth: int, exclude: str = "") -> list[str]:
        """Existing node names at a depth, most-used first (§8 'descending score order' — use-count is
        the bank-native proxy for score here)."""
        nodes = self._schema.get(depth, {})
        return [n for n, _ in sorted(nodes.items(), key=lambda kv: kv[1], reverse=True) if n != exclude]

    def validate(self, coordinate: str, strict: bool = False) -> GuardVerdict:
        """Judge a coordinate. Rejects junk/malformed/too-shallow; flags novel nodes + suggests
        re-anchors. strict=True raises ValueError on a hard failure (enforce); else returns a verdict
        the caller acts on (advise — the loop default)."""
        coord = _norm(coordinate)
        segs = coord.split(":") if coord else []
        issues, novel, reanchor = [], [], {}

        if len(segs) < _MIN_DEPTH:
            issues.append(f"too shallow (depth {len(segs)} < {_MIN_DEPTH}) — not content-bearing")

        for depth, seg in enumerate(segs):
            if not _SEG_OK.match(seg):
                issues.append(f"depth {depth}: '{seg}' is malformed (need lowercase [a-z0-9_])")
                continue
            if seg in _JUNK:
                issues.append(f"depth {depth}: '{seg}' is a junk node (taxonomy shortcut)")
                sib = self._siblings(depth, exclude=seg)
                if sib:
                    reanchor[seg] = sib[:5]
            elif depth in self._schema and seg not in self._schema[depth] and self._schema[depth]:
                # a never-seen name at a populated depth: NOVEL (allowed, but surfaced — and we offer
                # the existing siblings so the agent can re-anchor if this was an accidental fork).
                novel.append(f"depth {depth}: '{seg}'")
                sib = self._siblings(depth, exclude=seg)
                if sib:
                    reanchor.setdefault(seg, sib[:5])

        hard_fail = any("junk" in i or "malformed" in i or "too shallow" in i for i in issues)
        ok = not hard_fail
        if strict and not ok:
            raise ValueError(f"coordinate rejected: {coord} :: {'; '.join(issues)}")
        return GuardVerdict(ok=ok, coordinate=coord, issues=issues,
                            novel_segments=novel, reanchor=reanchor)


_SEG_NORM = re.compile(r"[^a-z0-9]+")


def _norm(coordinate: str) -> str:
    segs = [_SEG_NORM.sub("_", s.strip().lower()).strip("_")
            for s in (coordinate or "").split(":") if s.strip()]
    return ":".join(s for s in segs if s)
