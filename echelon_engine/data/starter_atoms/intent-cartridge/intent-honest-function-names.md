---
name: intent-honest-function-names
description: A function's NAME is its contract — if the name says one thing and the code does another, that's dishonesty
metadata:
  type: project
---

# Function names are contracts — code must match

A function called `effective_scores_by_content` should return scores keyed by content. If it also constructs full Atom objects, loads unused columns, and iterates for side effects — the name is lying.

## Audit method
1. **Read the function name** — what does it CLAIM to do?
2. **Read the docstring** — what does it SAY it does?
3. **Read the implementation** — what does it ACTUALLY do?
4. **Flag gaps** where name/docstring and implementation diverge

## Dishonesty patterns
- **OVER-LOADING** — `atoms_in_scope` returns full Atom objects with content blobs, but 80% of callers only need `len()`. The name doesn't say "with full content," but the implementation forces it.
- **HIDDEN WORK** — `effective_scores_by_content` does `SELECT *`, constructs Atom objects, computes effective scores. The "by_content" name suggests a lightweight lookup, not full object hydration.
- **WRONG ID SCHEME** — FTS pre-filter matches `atom_id` (v2) against `seed.id` (v1 content hash) — different ID schemes, so it silently filters to zero. The code CLAIMS to filter but doesn't.
- **NAME DRIFT** — `_send_to_provider` hardcodes DeepSeekProvider. The name says "send to provider" (generic) but the code only sends to one provider.

## Real example (2026-06-26)
`effective_scores_by_content` before optimization:
- Name claims: "effective scores, keyed by content"
- Docstring claims: "content-prefix -> effective_score for v2 atoms"
- Implementation: `SELECT * FROM atoms`, construct Atom objects (loading full content blobs, score_history JSON, valence, arousal), compute effective_score per atom
- Honesty gap: the name says "scores by content" but the implementation hydrates full Atom objects. The `SELECT *` loads columns (score_history, valence, arousal) that are never used. Fixed by selecting only `id, content, score, use_count, born_from` and joining `atom_earned` inline.
