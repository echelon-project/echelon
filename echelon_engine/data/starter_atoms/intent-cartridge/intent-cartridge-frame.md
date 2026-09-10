---
name: intent-cartridge-frame
description: Code honesty cartridge — audit whether code honestly does what its names, docstrings, and claims say it does
metadata:
  type: project
---

# Code honesty cartridge — the complete discipline

A reusable capability for auditing whether code honestly does what it claims to do. Equip this when reviewing code for trustworthiness.

## The chain (firing order)
1. **Read the names** — every function, method, and variable name is a CLAIM about what it does
2. **Read the docstrings** — what does the code SAY it does?
3. **Trace the implementation** — what does the code ACTUALLY do?
4. **Check ID bridges** — when two systems (v1/v2) meet, do the IDs actually match?
5. **Test known inputs** — if you search for X and you KNOW X exists, does it find it?
6. **Flag the gaps** — name vs implementation, docstring vs behavior, claim vs reality

## Dishonesty taxonomy
- **OVER-LOADED FUNCTION** — does more than its name claims (forces callers to pay for unused work)
- **DEAD CLAIM** — claims to do something but the logic never produces correct results
- **WRONG ID SCHEME** — passes v1 IDs to v2 APIs or vice versa
- **SILENT NO-OP** — code runs without errors but never actually changes anything
- **MISLEADING NAME** — `_send_to_provider` that only sends to DeepSeek

## Anti-patterns this cartridge prevents
- Adding a filter that silently matches nothing (dead claim)
- Function loading full objects when callers only need counts (over-loaded)
- Cache that doesn't survive between instances (hidden no-op)
- "It compiled and ran" = "it works" (dead claims compile and run too)
