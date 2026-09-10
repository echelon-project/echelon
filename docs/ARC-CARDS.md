# Arc-cards — resuming a session that already ended

An **arc-card** is the closing artifact of a work session: a compact record that
lets a later session pick the thread up without re-reading the transcript.

## The problem it solves

Context windows end. A session that ran for hours holds a great deal of accumulated
understanding — what was tried, what failed, why the current approach was chosen over
the obvious one — and almost all of it evaporates when the window closes. The next
session starts from the code, which records *what* was decided and never *why*.

Re-deriving that context is expensive, and worse, it is often re-derived *wrongly*:
the next session sees an odd-looking choice, assumes it was an oversight, and
"fixes" it back into the bug it was working around.

## What a card holds

- **What this arc was for** — the goal in a sentence.
- **What actually happened** — the outcome, honestly, including what did not work.
- **The atoms it planted** — lessons banked during the session, by slug.
- **Open threads** — what the next session should pick up, and what is deliberately
  left alone.
- **A pointer to its predecessor** — cards chain, so an arc has a spine.

The chain is the important part. A single card is a note; a chain of cards is a
history you can walk backwards until you find the decision you are questioning.

## Wrap: the act that produces one

Closing a session is a discipline, not a save:

1. **Distill** — what was learned that generalizes past this session?
2. **Write atoms** — one lesson per file, self-contained, in `memory/`.
3. **Route by scope** — each atom is planted where it belongs, not where you stand.
4. **Ingest** — plant the atoms into the bank so recall can reach them.
5. **Promote** — a trap that is tool-shaped and expensive becomes a reflex.
6. **Card** — write the arc-card and link it to the previous one.

The discipline that makes it worth doing: **distill for the reader, not the writer.**
A card that says "continued work on the parser" helps nobody. A card that says "the
parser rewrite was abandoned because the tokenizer's lookahead is load-bearing for
error recovery — see atom `lookahead-is-not-an-optimization`" saves the next session
a day.

## Relive

`echelon relive <card-id>` re-opens an arc: it reads the card, re-warms the atoms it
planted, and puts the reader back into the posture the session ended in. This is the
payoff — the reason to spend ten minutes wrapping is that the resume costs one
command instead of an afternoon.

See also: [MEMORY-AS-WEIGHT-ADJUSTOR.md](MEMORY-AS-WEIGHT-ADJUSTOR.md).
