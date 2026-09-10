---
name: reflex-command-substitution-captures-stdout-logs-with-the-secret
description: Capturing a token/secret via $(...) from a module that logs to stdout swallows the LOG LINE too — the value looks plausible but is corrupt, and a space in it breaks the HTTP header as a 400
metadata:
  node_type: memory
  type: feedback
  reflex: true
  reflex-tier: portable
  reflex-event: PreToolUse
  reflex-tool: Bash
  reflex-match: (TOKEN|SECRET|KEY|PASS|CRED)[A-Z_]*=[$][(].*(node|python|php|ruby)
  reflex-action: warn
---

STOP — capturing a secret with `VAR=$(node -e '...')`? **Anything the module prints to stdout on load lands in your variable along with the value.** Init/warm-up code logs (`[secrets] loaded 5 creds from broker`, `[db] driver=sqlite`), so `$VAR` becomes log-text + token. Write the value to **stderr** and redirect (`process.stderr.write(v)` … `2>&1 >/dev/null`), or silence the logger first (`console.log=()=>{}`), then verify `${#VAR}` is the EXPECTED length before using it.

**Why:** witnessed while verifying a live endpoint. A `$(...)`-captured token returned a plausible-looking 80-char string — actually a log line plus the real 43-char token. Every authed curl returned **HTTP 400 with an empty body**, because the embedded space made an invalid `Authorization` header. The freshly-deployed endpoint looked broken. It was the test harness, not the code.

**The tell:** an unauthed request 401s (handler reached) but an authed one **400s with an empty body**. A 400 your handler never emits means the request died *before* routing — suspect a malformed header, not your logic. Length-check the captured secret (`echo ${#TOKEN}`); a token is a known fixed size, and a wrong length is instant proof.

**How to apply:** never trust a `$(...)`-captured secret without asserting its length or shape first. Prefer stderr for the value + `2>&1 >/dev/null`, or a dedicated print-only flag that guarantees clean stdout. Same class of failure as any "parse a value out of a stream that also carries logs" — separate the channels rather than filtering the noise.
