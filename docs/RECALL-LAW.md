# The recall law — foveated vision

**Recall is not search.** Search asks "which documents contain these words?" Recall
asks "given what I am about to do, what do I already know?"

## The eye metaphor

The bank behaves like an eye. Atoms matching your current intent come into sharp
focus; everything else stays dormant-but-present in the periphery, available if the
intent shifts. You do not load the bank. You point it at something.

This is a deliberate rejection of the "stuff the context window" approach. A model
handed 200 atoms is a model that reads none of them carefully. A model handed the
three that bear on its actual next move is a model that behaves like it has
experience.

## Warm, lukewarm, cold

Every recall returns a **verdict**, not just results:

| Verdict | Meaning | What to do |
|---|---|---|
| **warm** | Known ground. Atoms with earned weight match this intent closely. | Take up the full atom before acting. You have been here. |
| **lukewarm** | Partial recognition. Something rhymes but nothing matches cleanly. | Look at the warmest seed. You may be near a known path. |
| **cold** | New ground. Nothing in the bank speaks to this. | Proceed carefully, and expect to write an atom afterwards. |

The verdict is the point. A recall that returns five weak matches and calls them
results is lying about what it knows; a recall that says *cold* is telling you
something true and useful — that you are about to learn something worth banking.

## Recall is free; take-up earns

`recall --warm` is a cheap ranking pass over descriptions. It returns clipped seeds.
`remember <slug>` fetches the full body — and *that* is the witnessed act that earns
the atom weight.

The split is not an optimization. It is what keeps the earned signal honest: an atom
that merely *appeared* in a ranking has demonstrated nothing, while an atom that was
deliberately taken up and then rode a successful procedure has demonstrated exactly
the thing weight is supposed to measure.

The corollary, and it is a real rule: **never `cat` the .md file.** An out-of-band
read is unwitnessed. The atom shaped your behavior and the bank never learned that it
did, so it cannot be credited when the work succeeds.

## Query by intent, not by keyword

Recall wants the sentence you would say to a colleague:

    echelon recall --warm "my build script reports success even when it fails"

not

    echelon recall --warm "exit code"

The first states a situation and matches the lesson that resolves it. The second
states a topic and matches everything that mentions it.

## Honest tiering

A recall reports *how* it ranked: a lexical wording-match, or a semantic judge pass.
It never presents a lexical guess as semantic understanding. When you see
`tier lexical (wording-match only — no semantic judge ran)`, that is the substrate
declining to overclaim — the same discipline it asks of the models that run on it.

See also: [MEMORY-AS-WEIGHT-ADJUSTOR.md](MEMORY-AS-WEIGHT-ADJUSTOR.md).
