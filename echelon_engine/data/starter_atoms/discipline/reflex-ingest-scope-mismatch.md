---
name: reflex-ingest-scope-mismatch
description: "REFLEX — `echelon ingest` whose --scope differs from the home-dir NAME hard-errors ('scope disagrees with canonical scope resolved from <dir>') and plants ZERO atoms; set ECHELON_INGEST_SCOPE=1 to override deliberately"
metadata:
  type: feedback
  reflex: true
  reflex-tier: portable
  reflex-event: PreToolUse
  reflex-tool: Bash
  reflex-match: "ingest.*--root.*--scope"
  reflex-action: warn
---

STOP — if the `--scope` you passed differs from the home DIRECTORY NAME, plain `ingest`
HARD-ERRORS: *"scope '<x>' disagrees with the canonical scope '<y>' resolved from <dir>"* —
and plants **ZERO atoms**. Callers that don't check the exit code just report "0 atoms" and
move on, so this reads as "nothing to ingest" rather than as a refusal.

The gate is correct behavior: it is what stops one estate's atoms leaking into another's
scope. Do not reflexively override it — first check whether your `--scope` is simply wrong.

When the mismatch IS intended (a cartridge registered `--scope <name>-cartridge` but living
in `<home>/<name>/`), override deliberately for that one call:

```
ECHELON_INGEST_SCOPE=1 echelon ingest --root <home> --scope <name>-cartridge
```

**How to apply:** treat a 0-atom ingest as a FAILURE to investigate, never as a no-op. Gate on
the exit code, not on the prose.
