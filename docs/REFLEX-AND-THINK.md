# Reflex and think — two speeds of knowing

Not every lesson deserves deliberation. Some should fire before you finish typing.

## The split

**THINK** is the recall path. You state an intent, the bank ranks atoms by warmth,
you take up what matters and reason with it. It costs a turn and it is where
judgment lives.

**REFLEX** is the guard path. A compiled rule watches for a *shape* — a command
about to run, a tool about to be called — and fires a warning at that moment,
without being asked and without a recall.

The difference is not importance, it is **event-space versus topic-space.** A reflex
does not trigger on a subject; it triggers on an action. "Anything about databases"
is a topic and makes a bad reflex — it will fire constantly and be tuned out. "A
`copy` invoked against a live SQLite bank with WAL enabled" is an event, and a rule
that fires exactly there is worth its interruption every time.

## When a lesson deserves to be a reflex

Promote an atom to a reflex when all of these hold:

1. **It is tool-shaped.** The trap is visible in the command, not in the reasoning.
2. **It is expensive.** Data loss, a corrupted store, an hour of debugging a
   measurement that was wrong rather than a system that was broken.
3. **It recurs.** You have hit it more than once, and being careful did not work.
4. **The check is mechanical.** A rule can decide it, without needing to understand
   the goal.

Everything else stays an atom. A bank of 2,000 atoms and 90 reflexes is healthy; a
bank with 2,000 reflexes is a bank nobody reads, because a guard that fires on
everything communicates nothing.

## Why the interrupt beats the instruction

A reflex is not a note in a prompt asking a model to remember something. It is a
guard in code that fires at the moment of the act. The distinction is the difference
between "please be careful with pipelines" — which every model agrees to and then
forgets by the third tool call — and a rule that prints, at the instant the pipe is
typed, that `$?` is about to report the wrong thing.

This is the general law, and it applies well beyond reflexes: **anything that must
ALWAYS hold belongs in code, not in instructions you hope a model follows.** The
model proposes; the code keeps it honest. ECHELON applies this to itself — the
layering rules are enforced by an import-time AST scan that raises `ImportError`,
not by a style guide.

## The loop

    THINK   recall -> remember -> act -> the act succeeds or fails
                                              |
    EARN    credit flows back to the atoms that rode the success
                                              |
    PROMOTE a lesson that keeps costing you, and is mechanically checkable,
            is compiled into a reflex and stops needing to be recalled at all

A reflex is what a lesson becomes when you are tired of learning it.

See also: [RECALL-LAW.md](RECALL-LAW.md), [GATES.md](GATES.md).
