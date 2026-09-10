---
name: intent-dead-code-claims
description: Code that claims to do something but silently fails — the worst kind of dishonesty because it looks correct
metadata:
  type: project
---

# Dead claims — code that says it works but silently fails

The most dangerous dishonesty: code that appears correct (clean logic, good comments) but silently produces wrong results because of a hidden mismatch.

## Audit method
1. **Trace every data path** — does the output of function A actually match what function B expects?
2. **Check ID schemes** — are v1 IDs being passed where v2 IDs are expected? Do hashes match?
3. **Verify empty results aren't silent failures** — an empty list can mean "nothing found" OR "the filter matched nothing because of a bug"
4. **Test with known data** — if you search for "memory weight," you KNOW that atom exists. If the search returns 0 results, the search is broken.

## Real example (2026-06-26)
FTS pre-filter in `warmth()`:
- Code claimed: "FTS5 index narrows candidates before n-gram scoring"
- What it did: `search_spine_candidates()` returned v2 `atom_id` values. The filter matched `s.id` (v1 content hash) against them. Different ID schemes → zero matches → fell through to full scan.
- The code ran without errors. The logic looked correct. But it was a dead claim — the filter NEVER worked.
- Caught by: testing with a query that should match known atoms ("memory weight" → "memory-is-a-weight-adjustor" exists). Returned 0 FTS candidates matched → the filter was a no-op.
- Fixed by: matching on slug (common between v1 coordinates and v2 atom_spine) instead of atom_id.

## Prevention
- When a filter/pre-filter is added, TEST that it actually filters to a non-empty set for a known-good query
- When two systems use different ID schemes, verify the bridge explicitly — don't assume
- "It ran without errors" is NOT the same as "it worked"
