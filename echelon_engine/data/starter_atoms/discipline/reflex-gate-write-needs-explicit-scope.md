---
name: reflex-gate-write-needs-explicit-scope
description: "REFLEX (warn) — `gate --write` without `--scope` resolves the scope from CWD; run from the wrong directory it stamps placeholder text into the live MEMORY.md banner, silently breaking every recall door the next session reads"
metadata:
  node_type: memory
  type: feedback
  reflex: true
  reflex-tier: portable
  reflex-event: PreToolUse
  reflex-tool: Bash|PowerShell
  reflex-match: "gate --write(?![^|;&]*--scope)"
  reflex-action: warn
---

STOP — `gate --write` WITHOUT `--scope` resolves the scope from your CURRENT DIRECTORY. Run it
from the wrong place and it writes `<this-project-scope>` placeholders into the live banner —
and every recall/continuity command the next session copies out of that banner is then broken.

Pass `--scope <scope>` explicitly. Always.

**Why this is a reflex:** the damage is silent and DELAYED. The write succeeds, the banner looks
plausible, and the breakage only surfaces in a later session that reads the placeholders as real
commands. Nothing at the moment of the mistake tells you anything is wrong.

**How to apply:** `echelon gate --write <path> --scope <scope>`. If you are unsure what scope you
are in, run `echelon resolve-scope` first rather than letting the default decide for you.
