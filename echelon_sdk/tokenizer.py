"""Pure-stdlib token counter — the pre-flight token estimator every provider shares.

WHY: the cost meter bills off the API-returned `usage` AFTER a call lands (accurate but
reactive — you learn the cost only once it's spent). To gate a call BEFORE it draws
(Budget.would_exceed), the loop needs a token count UP FRONT. This module gives it, with
NO third-party dependency (the estate's stdlib-only / no-borrowed-weights law) — it loads the
DeepSeek tokenizer.json (byte-level BPE, 128k vocab) and runs BPE in plain Python.

ACCURACY (honest): EXACT for DeepSeek (its own tokenizer); a strong ESTIMATE (~few %) for
Grok/Claude/GPT/Gemini, which use different BPE vocabs. That is correct for a budget pre-flight
gate ("will this call roughly blow the budget?") — the API's returned `usage` stays the billing
truth post-call. count_tokens is for FORESIGHT, not for billing.

The tokenizer.json lives in the ECHELON estate root (D:\\WORK\\ECHELON\\tokenizer), resolved the
same way keys.py finds the estate .apikey; override via config `tokenizer.dir` or env
ECHELON_TOKENIZER_DIR. If the asset is missing, count_tokens degrades to a char/4 heuristic
(never crashes a run over a missing estimator).
"""
from __future__ import annotations

import json
import os
import unicodedata
from functools import lru_cache
from pathlib import Path
from typing import Optional
from .estate_paths import command_root as _command_root

# Estate-root default (mirrors keys.py's hard estate path) + overrides.
_ROOT = _command_root()
_ESTATE_TOKENIZER = (_ROOT / "tokenizer") if _ROOT else None


def _tokenizer_dir() -> Optional[Path]:
    """Resolve the tokenizer asset dir: env > config > estate default. None if unfindable."""
    env = os.environ.get("ECHELON_TOKENIZER_DIR")
    if env and Path(env).exists():
        return Path(env)
    try:
        from . import config
        cfg = config.get("tokenizer.dir", "")
        if cfg and Path(cfg).exists():
            return Path(cfg)
    except Exception:
        pass
    if _ESTATE_TOKENIZER.exists():
        return _ESTATE_TOKENIZER
    return None


# ── byte-level encoder (GPT-2 family: map raw bytes <-> printable unicode) ─────────────────
@lru_cache(maxsize=1)
def _byte_encoder() -> dict[int, str]:
    """The reversible byte<->unicode table tokenizer.json's ByteLevel uses (verbatim GPT-2)."""
    bs = (list(range(ord("!"), ord("~") + 1))
          + list(range(ord("¡"), ord("¬") + 1))
          + list(range(ord("®"), ord("ÿ") + 1)))
    cs = bs[:]
    n = 0
    for b in range(256):
        if b not in bs:
            bs.append(b)
            cs.append(256 + n)
            n += 1
    return {b: chr(c) for b, c in zip(bs, cs)}


# ── pre-tokenizer: the 3 Split regexes from tokenizer.json, in stdlib re + unicodedata ─────
# tokenizer.json pre_tokenizer Splits (Isolated):
#   1) \p{N}{1,3}                      — runs of 1-3 digits
#   2) [CJK/Hiragana/Katakana]+        — CJK blocks
#   3) the GPT-style word/punct/space pattern
# Python's re has no \p{}, so we classify char-by-char via unicodedata.category and group runs
# the same way the Isolated Split behavior does. This reproduces the SPLIT BOUNDARIES (what BPE
# sees as a "word"), which is what determines the token count.
_CJK_RANGES = ((0x4E00, 0x9FA5), (0x3040, 0x309F), (0x30A0, 0x30FF))


def _is_cjk(ch: str) -> bool:
    o = ord(ch)
    return any(a <= o <= b for a, b in _CJK_RANGES)


def _cat(ch: str) -> str:
    return unicodedata.category(ch)  # 'L*', 'N*', 'P*', 'S*', 'Z*', 'C*'


def _pretokenize(text: str) -> list[str]:
    """Split text into the pieces BPE will independently encode (estate-faithful boundaries).

    Reproduces the three Isolated Splits: digit-runs (<=3), CJK runs, and the GPT word/punct/
    space grouping. Leading spaces attach to the following word (the ByteLevel 'Ġ' convention)."""
    pieces: list[str] = []
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        cat = _cat(ch)

        # 1) digits: runs of up to 3
        if cat.startswith("N"):
            j = i
            while j < n and j - i < 3 and _cat(text[j]).startswith("N"):
                j += 1
            pieces.append(text[i:j]); i = j; continue

        # 2) CJK: contiguous run
        if _is_cjk(ch):
            j = i
            while j < n and _is_cjk(text[j]):
                j += 1
            pieces.append(text[i:j]); i = j; continue

        # 3a) a single leading space then a letter-run -> " word" (the Ġword piece)
        if ch == " " and i + 1 < n and _cat(text[i + 1]).startswith(("L", "M")):
            j = i + 1
            while j < n and _cat(text[j]).startswith(("L", "M")):
                j += 1
            pieces.append(text[i:j]); i = j; continue

        # 3b) a letter-run (no leading space)
        if cat.startswith(("L", "M")):
            j = i
            while j < n and _cat(text[j]).startswith(("L", "M")):
                j += 1
            pieces.append(text[i:j]); i = j; continue

        # 3c) punctuation/symbol run (optionally one leading space)
        if cat.startswith(("P", "S")) or (ch == " " and i + 1 < n and _cat(text[i + 1]).startswith(("P", "S"))):
            j = i
            if text[j] == " ":
                j += 1
            while j < n and _cat(text[j]).startswith(("P", "S")):
                j += 1
            pieces.append(text[i:j]); i = j; continue

        # 3d) whitespace runs (newlines / multiple spaces)
        if cat.startswith(("Z", "C")) or ch in "\r\n\t":
            j = i
            while j < n and (_cat(text[j]).startswith(("Z", "C")) or text[j] in "\r\n\t "):
                j += 1
            pieces.append(text[i:j]); i = j; continue

        # fallback: a single char (never drop input)
        pieces.append(ch); i += 1

    return pieces


# ── the BPE model (vocab + merge ranks), loaded once from tokenizer.json ───────────────────
class _BPE:
    def __init__(self, tdir: Path):
        data = json.loads((tdir / "tokenizer.json").read_text(encoding="utf-8"))
        model = data["model"]
        self.vocab: dict[str, int] = model["vocab"]
        merges = model["merges"]
        # merges may be ["a b", ...] or [["a","b"], ...] depending on tokenizers version.
        self.ranks: dict[tuple[str, str], int] = {}
        for idx, m in enumerate(merges):
            pair = tuple(m.split(" ")) if isinstance(m, str) else tuple(m)
            if len(pair) == 2:
                self.ranks[pair] = idx
        # added/special tokens count as exactly one token each.
        self.specials: dict[str, int] = {t["content"]: t["id"] for t in data.get("added_tokens", [])}
        self._byte_enc = _byte_encoder()

    def _bpe(self, token: str) -> int:
        """Number of BPE merges' worth of symbols for one pre-token (byte-level encoded)."""
        symbols = [self._byte_enc[b] for b in token.encode("utf-8")]
        if not symbols:
            return 0
        while len(symbols) > 1:
            # find the lowest-rank adjacent pair
            best_rank, best_i = None, -1
            for i in range(len(symbols) - 1):
                r = self.ranks.get((symbols[i], symbols[i + 1]))
                if r is not None and (best_rank is None or r < best_rank):
                    best_rank, best_i = r, i
            if best_rank is None:
                break
            symbols[best_i:best_i + 2] = [symbols[best_i] + symbols[best_i + 1]]
        return len(symbols)

    def count(self, text: str) -> int:
        if not text:
            return 0
        total = 0
        # specials are atomic; split the text around them so each counts as 1.
        remaining = [text]
        for sp in self.specials:
            nxt: list[str] = []
            for chunk in remaining:
                if sp and sp in chunk:
                    parts = chunk.split(sp)
                    for k, p in enumerate(parts):
                        if k:
                            total += 1  # the special token itself
                        if p:
                            nxt.append(p)
                else:
                    nxt.append(chunk)
            remaining = nxt
        for chunk in remaining:
            for piece in _pretokenize(chunk):
                total += self._bpe(piece)
        return total


@lru_cache(maxsize=1)
def _model() -> Optional[_BPE]:
    tdir = _tokenizer_dir()
    if tdir is None:
        return None
    try:
        return _BPE(tdir)
    except Exception:
        return None


def count_tokens(text: str) -> int:
    """Pre-flight token estimate for `text`. Exact for DeepSeek, strong estimate elsewhere.

    Degrades to a char/4 heuristic if the tokenizer asset is missing (never crashes a run)."""
    if not text:
        return 0
    m = _model()
    if m is None:
        return max(1, len(text) // 4)  # honest fallback heuristic
    return m.count(text)


def count_messages(messages: list[dict]) -> int:
    """Estimate tokens for an OpenAI-shape message list (role + content), plus a small per-message
    overhead the chat template adds. For pre-flight budgeting, not exact billing."""
    total = 0
    for msg in messages or []:
        c = msg.get("content")
        if isinstance(c, str):
            total += count_tokens(c)
        elif isinstance(c, list):  # content-parts (vision etc.) — count text parts only
            for part in c:
                if isinstance(part, dict) and isinstance(part.get("text"), str):
                    total += count_tokens(part["text"])
        total += 4  # per-message role/format overhead (chat-template framing)
    return total
