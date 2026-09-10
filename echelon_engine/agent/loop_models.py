"""Data models for the agent loop — MemoryContext and AgentResult.

Extracted from loop.py to keep the loop module slim.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any

from echelon_engine.atoms.store import SeedStore


@dataclass
class MemoryContext:
    """The memory organ as a loop participant. Optional — absent = the plain loop.

    Carries the store + scope so the loop can compute WARMTH (the 3rd loop parameter,
    owner 2026-06-05) each step and act on it. Does NOT inject memory content — feeds
    the model the TEMPERATURE of its own reasoning and lets it re-choose (rediscovery)."""
    store: SeedStore
    scope: str
    auto_seed: bool = True   # close the frontier behind us: cold-resolved -> new warm seed
    judge_provider: Any = None   # if set, escalate ambiguous warmth to a process-based judge
    judge_model: str = "grok-4.3"
    bank: Any = None         # optional KnowledgeBank (COS × ENTRY). If set, the loop persists each
                             # waking as a ttl'd snapshot, SEEDS a mid-run re-wake from it, and
                             # SURFACES a knowledge OFFER on the warmth×bank cross (bank_offer — the
                             # agent chooses to pull it). See wake_snapshots / knowledge-bank-cos-x-entry-rag.
    bank_semantic: Any = None  # optional SemanticTier (own embedder) for the offer's meaning-match;
                               # None -> the offer uses the bank's lexical floor.
    confirmation: Any = None   # optional ConfirmationLedger (L2). If set, warmth filters out
                               # UNCONFIRMED insight-claims ("unconfirmed = lie"). See confirmation.py.


@dataclass
class AgentResult:
    status: str           # completed | blocked | timeout | error
    answer: str
    steps: int
    tokens_in: int = 0
    tokens_out: int = 0
    transcript: list[dict] = field(default_factory=list)
    warmth_trace: list[dict] = field(default_factory=list)  # per-step {step, score, verdict}
    # Did the model OPEN the rediscovery gate (call `recall`) before working? The texture
    # signal: a waking that is genuinely ECHELON reaches for its own past; one that runs
    # cold never does (canary as absence). None = no boot ritual ran this run.
    woke: bool | None = None
