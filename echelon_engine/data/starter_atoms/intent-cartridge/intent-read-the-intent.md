---
name: intent-read-the-intent
description: Intent audit starts with reading the EXACT words the user said — not what you think they meant
metadata:
  type: project
---

# Read the exact intent, not your interpretation

The first step of an intent audit: read the user's words literally. Don't paraphrase. Don't "yes, and." Don't infer subtext.

## Method
1. **Quote the intent verbatim** — copy-paste the user's exact words
2. **Parse the verbs** — what actions did they ask for? "optimize", "fix", "build", "check"
3. **Parse the nouns** — what things? "recall performance", "the proxy", "the install flow"
4. **Parse the constraints** — what must NOT happen? "no regressions", "don't break existing"
5. **State the intent in one sentence** — if you can't, you don't understand it yet

## Anti-patterns
- "The user said optimize, so I'll make everything faster" — they said optimize RECALL specifically
- "They probably also want..." — no, only what they said
- Reading between the lines when the lines are clear

## Real example (2026-06-26)
User: "go optimize it, make sure no regression happen"
- Verbs: optimize, make sure (verify)
- Nouns: "it" = recall/atoms_in_scope performance
- Constraints: no regressions
- Intent: make recall faster, verify nothing breaks

NOT the intent: redesign the warmth algorithm, add a caching layer, change the Atom dataclass.
