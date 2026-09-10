# The tier law — routing work by what it costs to be wrong

Models differ in price by orders of magnitude. The tier law is how you spend that
difference deliberately instead of defaulting to the most expensive thing available.

## The shape

    ORCHESTRATOR   plans, decides, gates, merges        — strongest model
        |
    EXECUTORS      build, sweep, wire, test             — cheap models, many
        |
    GATE           re-derives the executors' claims     — strong again

Work flows down. Judgment stays up. Nothing an executor *claims* becomes true until
something at the gate tier confirms it against evidence.

## The two rules that matter

**Cheap models build; strong models gate.** Writing code to a clear specification is
mechanical, parallelizable, and cheap to verify. *Deciding what to build*, and
*deciding whether it is actually done*, are neither. Spend accordingly.

**A model may not gate its own work.** This is the same law as
"nothing grades its own homework", applied to routing. An executor's self-report is
an input to the gate, never a substitute for it. The clearing must come from a
context that did not produce the artifact.

## Why the substrate changes the arithmetic

Ordinarily, routing down a tier means accepting worse judgment. A cheap model lacks
the accumulated sense of *what usually goes wrong here* that makes an expensive model
worth its price.

An earned bank supplies exactly that, externally. A cheap executor equipped with the
right atoms — the traps this codebase actually has, the gate that catches this
mistake, the reason the obvious approach was already rejected — behaves far closer
to an expensive one, because most of the gap was never raw reasoning. It was
*knowing what already happened here*.

This is the whole thesis, stated as an operating rule: **the substrate is what makes
cheap tiers safe to use.** Without it, routing down is cost-cutting. With it, routing
down is delegation.

## In practice

- Name the unit and its interface *before* dispatching (see
  [BOUNDARY-DRIVEN.md](BOUNDARY-DRIVEN.md)). An executor with a vague brief produces
  work the gate cannot evaluate.
- Give the executor its recall. Dispatching a cheap model with no bank access
  throws away the only reason it was safe to dispatch.
- Gate by evidence — exit codes, diffs, rendered output — not by the executor's
  summary of itself.
- Announce the model and the cost before spawning. Routing decisions that nobody
  can see cannot be reviewed.

See also: [GATES.md](GATES.md), [BOUNDARY-DRIVEN.md](BOUNDARY-DRIVEN.md).
