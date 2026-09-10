# The harness mirror

ECHELON is a substrate, not an agent. It runs *underneath* whatever harness you
already use — a coding CLI, an editor integration, an MCP client — and gives that
harness memory and reflexes it did not have.

## One source of truth, many mirrors

The bank, the atoms, and the compiled reflexes live in **one** place: the substrate.
A harness installation is a **mirror** of that source, never a second core.

    substrate  (the bank + the compiled rules)
        |
        +-- harness A hooks   (mirror)
        +-- harness B hooks   (mirror)
        +-- MCP clients       (mirror)

The rule that keeps this honest: **changes originate in the substrate and propagate
outward.** A harness never becomes an independent owner of a rule. When two harnesses
disagree, the substrate is right and both mirrors are stale.

Without that rule you get the failure this design exists to prevent: two harnesses
each holding a slightly different copy of the rules, each convinced it is current,
and a lesson that silently applies in one seat and not the other.

## How a harness gets the substrate

**Hooks.** A harness that can run a script at defined moments — session start, before
a tool call, after a tool call — can carry the whole spine:

| Moment | What the substrate does |
|---|---|
| session start | inject the current banner: live bank state, the entry doors |
| before a tool call | fire matching reflexes — the guard arrives before the act |
| after a tool call | seed the step into the trace so credit can flow later |

`echelon install-hooks` stages these for a harness that supports them.

**MCP.** For clients that speak the Model Context Protocol, `echelon-mcp` exposes
recall, remember, ingest and status as tools. The bank becomes something the model
can consult mid-turn rather than something a wrapper pre-loads.

**Plain CLI.** Everything the hooks do is a verb you can call yourself. The hooks are
convenience, not a private API — which is also what keeps the substrate portable to
a harness nobody has written yet.

## Projection, not duplication

A harness's instruction file may carry a **projection** of bank state — atom counts,
active reflexes, the doors. Treat it as *derived*: refresh it after an ingest or a
reflex compile, and never edit it as though it were the source. Owner-authored
instructions and generated projection live in the same file but are not the same
kind of thing, and a sync that overwrites the former to update the latter is a bug.

See also: [ARCHITECTURE.md](ARCHITECTURE.md), [REFLEX-AND-THINK.md](REFLEX-AND-THINK.md).
