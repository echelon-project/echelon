---
name: reflex-git-commit-heredoc-is-bash-not-powershell
description: "git commit -m @'...'@ is PowerShell here-string syntax; in a Bash shell the @ becomes the first character of the subject line, and the obvious repair doubles it"
metadata:
  type: feedback
  reflex: true
  reflex-tier: portable
  reflex-event: PreToolUse
  reflex-tool: Bash
  reflex-match: "git commit[^|]*-m @'"
  reflex-action: warn
---

**STOP — `@'...'@` is a PowerShell here-string, and this is a Bash shell. The `@` will be
committed as the first character of your subject line.** Use a heredoc (`git commit -F -` with
`<<'EOF'`) or write the message to a file and `git commit -F <file>`.

Hit TWICE in a single session on a machine that runs both shells. Both times the subject landed
as `@ Close the layout-approval gate: ...`. Worse, the obvious repair — reconstructing the message
with `git log -1 --format=%B | tail -n +2` and re-prepending the subject — produced a DOUBLED
subject line and needed a second amend to clean up.

**Why this bites:** on a mixed Windows machine both syntaxes are muscle memory, and the wrong one
does not error. Git accepts `@` as a perfectly valid first character. Nothing fails; the commit is
just quietly malformed.

**How to apply:** in Bash use `<<'EOF'` heredocs. In PowerShell use `@'...'@` with the closing `'@`
at column 0. Know which shell you are in before composing a multi-line message.
