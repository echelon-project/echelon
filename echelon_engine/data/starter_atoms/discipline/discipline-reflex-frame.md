---
name: discipline-reflex-frame
description: What the starter discipline reflexes are, why they fire before the tool call instead of living in a note, and how to add your own — the hub atom for the `discipline` starter scope
metadata:
  type: reference
---

# The discipline reflexes — what they are and how to grow them

These atoms arrived with your install. They are not lessons you earned; they are traps
someone else already paid for, handed over so your first week costs less than theirs did.

## Why a reflex and not a note

A note has to be REMEMBERED. Under momentum — mid-task, three tools deep — it isn't.
A reflex fires in EVENT-space: the trigger is a tool call about to happen, not a topic in
the conversation. That is why the harness's own read-before-edit never gets forgotten while
your carefully written note does.

The bank is consulted at COMPILE time, never at fire time. Tool events fire dozens of times
per turn; a memory probe on that path would be cortical latency on a spinal circuit.

## The three tiers

| Tier | Fires | For |
|---|---|---|
| `scope` (default) | only in its own estate | deploy paths, hostnames, service users — anything naming a specific place |
| `global` | every estate on the machine | bank integrity, secrets, tooling and shell laws |
| `portable` | every estate, AND ships to new installs | the subset a brand-new user should be protected by on day one |

**`scope` is the default on purpose.** Declaring `global` is an ASSERTION that a rule is
estate-independent — if it names a path, a host, or a service, it is not global no matter what
the frontmatter says. The compiler warns when a `global` rule's pattern looks estate-shaped.

## Teeth are separate from tier

`reflex-action: warn` (default) injects the guard text and lets you proceed.
`reflex-action: block` denies the tool call outright. Teeth are declared per-atom, never
defaulted — a false-firing hard block is worse than the trap it guards. Of the atoms in this
seed, exactly one blocks: the one protecting your bank from deletion.

## Writing your own

Add these keys to any atom's frontmatter, then recompile:

```yaml
metadata:
  type: feedback
  reflex: true
  reflex-tier: scope          # scope | global | portable
  reflex-event: PreToolUse    # PreToolUse | UserPromptSubmit | Stop
  reflex-tool: Bash           # regex on the tool name
  reflex-match: <regex>       # regex on the serialized event payload
  reflex-action: warn         # warn | block
```

```bash
echelon reflex compile --root memory --scope <your-scope>
echelon reflex list          # confirm it's armed, and at which tier
```

Keep `reflex-match` BACKSLASH-FREE — write `[.]` rather than an escaped dot. YAML-normalizing
writers double backslashes and the parser does not unescape them.

## The discipline that matters most

When a trap costs you real time, write the reflex THAT DAY, while you still remember the exact
shape of the failure. A trap you merely survived teaches nothing; a trap you compiled into a
guard cannot recur silently. That is the whole mechanism.
