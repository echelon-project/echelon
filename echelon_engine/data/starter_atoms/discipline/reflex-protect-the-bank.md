---
name: reflex-protect-the-bank
description: "REFLEX (BLOCK, PreToolUse) — a destructive shell command aimed at echelon.db is denied outright; the bank is the memory organ and earned weight cannot be re-ingested"
metadata:
  node_type: memory
  type: feedback
  reflex: true
  reflex-tier: portable
  reflex-event: PreToolUse
  reflex-tool: Bash|PowerShell
  reflex-match: (rm |del |Remove-Item |unlink|DROP TABLE)[^&|;]*echelon[.]db
  reflex-action: block
---

BLOCKED: this command would destroy or mutate `~/.echelon/echelon.db` — your live memory bank. Earned weight cannot be re-earned by re-ingesting: the `.md` files restore the seeds, never the scores, traces, or edges. If you truly want this, take a backup first (`echelon backup`) and run it yourself.

**Why:** this is the auto-mode risk-block pattern — pre-determined, fires on the ACTION event, and does not wait for the model to remember. A bank is not "re-ingestible" the way a code repo is re-clonable; the weight is the part that took real sessions to accumulate.

**How to apply:** declared `reflex-action: block`, so the arc exits 2 and the harness denies the call. Teeth are always a per-atom declaration, never a default — the rest of the starter reflexes only warn.
