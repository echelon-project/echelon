# Memory as a weight-adjustor

The claim that makes ECHELON different from a document store: **a memory is not a
record of what happened. It is the seed that re-creates an understanding.**

## The distinction

A record answers "what did I do on Tuesday?" A weight-adjustor answers "what should
I *be* right now?" Storing a transcript gives you the first. ECHELON is built for
the second.

When a model reads an atom, the atom does not merely inform it — it *re-shapes* the
distribution the model samples from for the rest of the turn. That is the same job a
fine-tune does, performed at read time instead of at training time, and reversibly.
The practical consequence: a small model reading the right atom behaves like a larger
model that already knew. Discovery gets decoupled from raw intelligence.

## What follows from it

**One lesson per file.** An atom carries exactly one transferable understanding.
Two lessons in one file means recall can only ever fetch both, and the irrelevant
half is noise that shifts weights the wrong way.

**Self-contained bodies.** An atom restates the context it needs — which script,
which database, which date, absolute not relative. The reader is a future model with
no memory of the session that produced it. An atom that only makes sense in situ is
a note, not an atom.

**The description is the hook.** Recall ranks on the one-line description, so it
must state the *lesson*, not the topic. "Notes on shell exit codes" is a filing
label. "In a shell pipeline `$?` reports the LAST command's status, so a failing
build piped to `tail` reads as success" is a weight-adjustor: reading only that
line already changes what you do next.

**The body says WHY, not just WHAT.** A rule with no mechanism cannot generalize. A
model that knows *why* the pipe hides the exit code can recognize the same trap in a
shape it has never seen. A model that only knows the rule pattern-matches and misses.

## Earned, never asserted

An atom is born neutral. It gains weight only when a procedure that *used* it
succeeds, and the credit flows back along the trace. Nothing grades its own
homework, and nothing is important because its author said so.

This matters more than it sounds. A bank where anyone can assert importance decays
into a pile of everything-is-critical. A bank where weight is earned by outcome
converges on what actually worked — and, just as usefully, lets a lesson that stops
paying off fade without anyone having to relitigate it.

See also: [RECALL-LAW.md](RECALL-LAW.md), [REFLEX-AND-THINK.md](REFLEX-AND-THINK.md).
