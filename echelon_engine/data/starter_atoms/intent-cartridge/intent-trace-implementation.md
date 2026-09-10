---
name: intent-trace-implementation
description: After reading the intent, trace every change you made — does each one serve the intent?
metadata:
  type: project
---

# Trace implementation against intent

After reading the intent, list EVERY change made. For each change, ask: "Does this directly serve the stated intent?"

## Method
1. **List every file changed** — git diff, or manual audit
2. **For each change**, state the reason it was made
3. **Check:** does the reason match the intent? Or was it scope creep / instinct / "while I'm here"?
4. **Flag mismatches** — change X was made for reason Y, but the intent was Z

## The mismatch taxonomy
- **SCOPE CREEP** — "while optimizing recall, I also refactored the Atom dataclass" — not asked for
- **WRONG FIX** — "recall is slow so I'll cache the scores" — caches don't survive, scores change
- **OVER-ENGINEERING** — "I'll build a full caching layer with TTL and invalidation" — for a 9ms query
- **UNDER-DELIVERY** — "I added indexes but didn't fix the BFS walk" — the real bottleneck untouched
- **CORRECT** — change directly serves the intent, proportionate to the problem

## Real example (2026-06-26)
Intent: optimize recall, no regressions.

Changes made:
- `cards.py`: added indexes, FTS, lightweight queries → ✓ SERVES INTENT (optimize queries)
- `cards.py`: added `_score_cache` → ✗ SCOPE CREEP (caching was NOT asked for, adds staleness risk)
- `warmth.py`: added `pre_filter` parameter → ✓ SERVES INTENT (narrow candidates before scoring)
- `proxy.py`: switched to `count_atoms_in_scope` → ✓ SERVES INTENT (removes dead work)
- 7 files: switched count callers → ✓ SERVES INTENT (removes dead work, no regression)

The cache was flagged and removed — it didn't serve the intent and added risk.
