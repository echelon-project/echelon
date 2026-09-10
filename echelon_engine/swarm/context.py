"""context — cache-optimized context builder for swarm dispatches.

THE CACHE-HIT STRATEGY:
  Anthropic's prompt cache caches the LONGEST byte-identical PREFIX of a prompt.
  To keep the cache warm across multiple swarm dispatches, we split context into:

    FROZEN  — byte-identical across ALL dispatches (system prompt, cartridge atoms,
              output format spec, instructions). This gets cached on the first call
              and stays cached for every subsequent call.
    DYNAMIC — changes per dispatch (the specific goal, files, context). Appended
              AFTER the frozen block so it doesn't break the cache.

  The frozen block is built ONCE and reused. Only the dynamic suffix changes.
  Workers share the same frozen prefix → all get cache hits after the first.

USAGE:
  ctx = SwarmContext("architect")
  ctx.equip("engagement")           # add more cartridges
  frozen = ctx.frozen_block()       # byte-identical, cacheable prefix
  dynamic = ctx.dynamic_block(      # per-dispatch suffix
      goal="Design a rate limiter",
      context="We have 10K req/s, Redis available"
  )
"""
from __future__ import annotations

import hashlib
from pathlib import Path


class SwarmContext:
    """Builds cache-optimized context blocks for swarm dispatches.

    The frozen block contains everything that's IDENTICAL across dispatches —
    cartridge atoms, output format, system instructions. The dynamic block
    contains the goal-specific context that changes per dispatch.

    The caller concatenates: frozen + dynamic → the full prompt.
    Anthropic caches the frozen prefix automatically after the first call.
    """

    def __init__(self, *cartridge_names: str):
        self._cartridges: list[str] = list(cartridge_names)
        self._frozen: str | None = None  # built lazily
        self._frozen_hash: str = ""

    def equip(self, *names: str) -> "SwarmContext":
        """Add cartridges to the frozen block. Chainable."""
        for n in names:
            if n not in self._cartridges:
                self._cartridges.append(n)
        self._frozen = None  # invalidate
        return self

    # ── frozen block (cacheable) ────────────────────────────────────────────

    def frozen_block(self) -> str:
        """The byte-identical context prefix. Cached after first build."""
        if self._frozen is not None:
            return self._frozen
        parts: list[str] = []

        # ── 1. Output format spec (MUST be first — the most stable bytes) ──
        parts.append(_OUTPUT_FORMAT)

        # ── 2. Cartridge atoms (equipped, in stable order) ──
        for name in sorted(self._cartridges):
            atoms = _load_cartridge_atoms(name)
            if atoms:
                parts.append(f"<!-- CARTRIDGE: {name} -->\n{atoms}")

        # ── 3. Operating instructions ──
        parts.append(_SWARM_INSTRUCTIONS)

        self._frozen = "\n\n".join(parts)
        self._frozen_hash = hashlib.sha256(self._frozen.encode()).hexdigest()[:8]
        return self._frozen

    @property
    def frozen_hash(self) -> str:
        """Short hash of the frozen block — for cache-bust detection."""
        if self._frozen_hash:
            return self._frozen_hash
        self.frozen_block()
        return self._frozen_hash

    # ── dynamic block (per-dispatch) ────────────────────────────────────────

    def dynamic_block(self, goal: str, context: str = "",
                      files: list[str] | None = None) -> str:
        """The per-dispatch suffix. Changes every call — appended AFTER frozen."""
        parts = [f"## GOAL\n{goal}"]
        if context:
            parts.append(f"## CONTEXT\n{context}")
        if files:
            parts.append(f"## FILES\n" + "\n".join(f"  - {f}" for f in files))
        parts.append("## TASK\nProduce the output in the format specified above.")
        return "\n\n".join(parts)

    def build(self, goal: str, context: str = "",
              files: list[str] | None = None) -> tuple[str, str]:
        """Return (full_prompt, frozen_hash) for one dispatch."""
        frozen = self.frozen_block()
        dynamic = self.dynamic_block(goal, context, files)
        return frozen + "\n\n" + dynamic, self.frozen_hash


# ── shared text blocks ────────────────────────────────────────────────────────

# THE OUTPUT SPEC IS GENERATED FROM THE CONTRACT, NOT WRITTEN HERE.
# It used to be a hand-written prose block describing a shape that a regex elsewhere tried to
# scrape back out. Two copies of one shape drift, and this pair did: the prose asked for
# ```json fences plus trailing prose, while the ingest regex could not match nested objects
# unless they were fenced — so an unfenced-but-valid answer lost every finding silently.
# One declaration (contracts/lens_report.json) now feeds both ends.
def _output_format() -> str:
    from .contract import prompt_spec
    return prompt_spec()


_OUTPUT_FORMAT = _output_format()

_SWARM_INSTRUCTIONS = """\
You are a specialized agent in an ECHELON swarm. You have been equipped with
earned cartridge atoms (injected above) that shape your reasoning. Your job:
  1. Apply the cartridge's frame + rules to the GOAL
  2. Produce output in the exact format specified
  3. Be comprehensive — cover security, performance, architecture, UX, reliability, cost
  4. Be honest — flag uncertainty; never fabricate confidence
  5. After your output, include SWARM-FOLLOW-UP instructions telling the calling
     agent to run: `swarm --council` (multi-model deliberation to critique this plan)
     and `swarm --skeptic` (red-team review to find flaws)
"""


# ── cartridge loader ──────────────────────────────────────────────────────────

def _load_cartridge_atoms(name: str) -> str:
    """Load a cartridge's atoms as text. Best-effort — returns '' on failure."""
    try:
        import subprocess
        import sys
        r = subprocess.run(
            [sys.executable, "-X", "utf8", "-m", "echelon_engine", "cartridge",
             "equip", name, "swarm dispatch"],
            capture_output=True, text=True, encoding="utf-8", timeout=60)
        return (r.stdout or "").strip()
    except Exception:
        return ""
