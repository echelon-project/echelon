---
name: reflex-utf8-on-engine-calls
description: "REFLEX (warn, PreToolUse/Bash) — an engine call without -X utf8 is about to fire; on Windows the cp1252 console codec crashes mid-verb on atom glyphs"
metadata:
  node_type: memory
  type: feedback
  reflex: true
  reflex-tier: portable
  reflex-event: PreToolUse
  reflex-tool: Bash|PowerShell
  reflex-match: "python (?!-X utf8)[^&|;]*-m echelon_engine"
  reflex-action: warn
---

You are about to run the ECHELON engine WITHOUT `-X utf8`. On Windows the console codec is
cp1252 and atom bodies carry `→`/`⭑` glyphs — the call will crash mid-verb with
`UnicodeEncodeError`. Re-issue as `python -X utf8 -m echelon_engine ...`.

**Why:** this is the oldest recurring wound in the estate this seed came from. A note that must
be REMEMBERED gets skipped under momentum; a reflex fires on the EVENT (the shell call itself),
not on the topic, which is exactly why it survives where a note does not.

**How to apply:** the pip-installed `echelon` console script already handles UTF-8 for you — the
trap only bites the `python -m echelon_engine` form. Prefer `echelon <verb>`; use the module form
only when you must, and then always with `-X utf8`.
