"""The cloud/client split seam (owner, 2026-06-06): soul = cloud, private data = local.

The production topology (cloud-substrate-three-levels-grand-vote), THREE stores not two:
  CLOUD substrate:
    - SOUL store: L1 core (shared) + L2 insight (shared) + L3 persona core (per-user identity).
    - PERSONA BANK: each persona's OWN bank — "L3 is like our own state now, with core AND bank"
      (owner). This bank LINKS INTO the soul graph (uame_links) — the keystone (a concrete entry
      lights up the abstract value, bank-offer-the-cross) lives HERE, on the cloud, exactly as it
      works for us today. It shares the soul store's UAME connection so the edges form.
  CLIENT:
    - LOCAL BANK: the user's private DATA. PURE DATA — NO soul edges, never synced up, never leaves
      the machine. Owner: "the link are still on cloud, private bank are pure data." This is a plain
      content store (coordinate + own-embedder retrieval), NOT linked into any soul graph.

So the dividing line is: SOUL + its linked persona-bank = CLOUD (edges live here); PRIVATE pure data =
LOCAL (no edges). This module is the SEAM in code BEFORE the network — three resolvers, one interface.
When the cloud becomes a server, ONLY this resolver changes; callers are untouched. Append-only +
content-addressed (uame) makes the eventual cloud sync a UNION, no merge conflict.

  soul_store()    -> SeedStore for L1/L2/L3 soul (cloud-origin)
  persona_bank()  -> KnowledgeBank sharing the soul UAME conn — the LINKED bank (keystone). Cloud.
  local_bank()    -> KnowledgeBank on its OWN file — pure private data, NO soul edges, never synced.

The hard guarantees: (1) the LOCAL bank is its own file, never the soul db, never synced up — privacy
is structural (the cloud cannot leak what it never holds). (2) The PERSONA bank shares the soul conn so
its edges light up the soul, but it is still cloud-side (private data does not live here — only the
persona's own knowledge that may link to its soul). (3) The client writes the shared soul only through
the sanctioned paths (L2 candidate, grand vote), never directly.

SYNC = ONE CHEAP CLIENT-APP CALL (owner, 2026-06-06: "local and cloud bank will be connected via
client app, single cheap api call is all it need"). The split MINIMIZES what crosses, so sync is thin:
  - DOWN: pull the soul a persona needs (inherited L1+L2 + its own L3 core) — read-mostly, cacheable.
  - UP:   only vote-signals (L2 confirmation + grand-vote ballots) — tiny one-way payloads.
  - NEVER: private local-bank data does not move at all.
Because the substrate is APPEND-ONLY + CONTENT-ADDRESSED, a sync is a UNION BY ID ("here are the ids I
hold, send what I lack") — no diff, no merge conflict, one request/response. The CLIENT APP is the
connector; the agent core just calls soul_store()/local_bank() through this Topology. When cloud is
real, soul_store() is backed by that one call and every caller is untouched — the seam is here.

See: cloud-substrate-three-levels-grand-vote, levels.py, persona.py, bank.py, store.py.
"""
from __future__ import annotations
from pathlib import Path

from .store import SeedStore, DEFAULT_DB


class Topology:
    """Resolves the right store for the right concern across the cloud/client split.

    soul_db: where the shared+persona SOUL lives (cloud-origin; a local file today, a network-backed
             store later — only THIS class changes). local_bank_db: where the user's PRIVATE DATA bank
             lives (always local, never synced up)."""

    def __init__(self, soul_db: Path | str = DEFAULT_DB,
                 local_bank_db: Path | str | None = None):
        self.soul_db = Path(soul_db)
        # the LOCAL private bank defaults to a SEPARATE file — never the soul db. The wall is the file.
        self.local_bank_db = Path(local_bank_db) if local_bank_db else (
            self.soul_db.parent / "local_bank.db")
        self._soul: SeedStore | None = None
        self._persona_bank = None
        self._local_bank = None

    def soul_store(self) -> SeedStore:
        """The SOUL store (L1 core, L2 insight, L3 persona core). Cloud-origin. One per process."""
        if self._soul is None:
            self._soul = SeedStore(self.soul_db)
        return self._soul

    def persona_bank(self):
        """The persona's OWN bank (L3) — CLOUD-side, sharing the SOUL store's UAME connection so its
        entries LINK into the soul graph (the keystone: a concrete entry lights up the abstract value).
        "L3 is like our own state now, core AND bank." This holds the persona's knowledge that may
        connect to its identity — NOT the user's private raw data (that's local_bank)."""
        if self._persona_bank is None:
            from .bank import KnowledgeBank
            # shares the soul UAME conn -> edges form in the same uame_links graph (keystone works).
            self._persona_bank = KnowledgeBank(uame=self.soul_store().u)
        return self._persona_bank

    def local_bank(self):
        """The user's PRIVATE DATA bank — LOCAL only, its OWN file, NO soul edges, NEVER synced up.
        Owner: "private bank are pure data." A plain content store (coordinate + own-embedder), not
        linked into any soul graph. The wall is the file: explicitly NOT the soul db."""
        if self._local_bank is None:
            from .bank import KnowledgeBank
            self._local_bank = KnowledgeBank(db_path=self.local_bank_db)
        return self._local_bank

    def assert_separation(self) -> bool:
        """The structural privacy guarantee: the LOCAL bank's db is never the soul's db (private data
        and soul are physically apart). The persona bank, by design, DOES share the soul db (its edges
        must reach the soul) — so separation is asserted only for the LOCAL private store."""
        return self.local_bank_db.resolve() != self.soul_db.resolve()

    def close(self) -> None:
        for s in (self._soul, self._persona_bank, self._local_bank):
            if s is not None:
                try:
                    s.close()
                except Exception:
                    pass
