---
name: reflex-claude-deep-port-race
description: "claude-deep's proxy on fixed port 18788 is spawned DETACHED and outlives every worker (2026-08-18) — the only remaining race is simultaneous COLD-starts against a dead port; ensure one live proxy (or isolate with ECHELON_PROXY_PORT), then fan out freely"
metadata:
  node_type: memory
  type: feedback
  reflex: true
  reflex-tier: portable
  reflex-event: PreToolUse
  reflex-tool: Bash
  reflex-match: claude-deep.*claude-deep
  reflex-action: warn
---

`claude-deep` boots its DeepSeek proxy on FIXED port **18788**. Since 2026-08-18
(ECHELON-AGENT@80e4acb) the proxy is spawned **DETACHED** — it outlives every worker, so a live
proxy is the normal steady state and workers REUSE it safely at any concurrency. The old
SERIALIZE-everything law is retired.

**The one residual race:** simultaneous COLD-starts. If NO proxy is live and two workers launch
at once, both try to bind 18788; the loser dies `ConnectionRefused` — a **SILENT worker death:
exit-0 at the shell while doing nothing** (`WinError 10048` on Windows, `EADDRINUSE` elsewhere).

Safe shapes:
- **Warm the port once** — launch one worker (or the proxy) first; once `claude-deep` prints
  "reusing live deepseek proxy", fan out with no limit on the same port.
- **ISOLATE** — for cold N-wide fan-out, give each worker its own `ECHELON_PROXY_PORT=<unique>`
  (`$((18790+i))`, stagger ~2s). Proven: 18 concurrent workers on 18790-18807, zero deaths.

**Why this is still a reflex:** the cold-start failure is silent — nothing errors at the shell
and the missing work surfaces much later. Verify boot in the run log (proxy line + a live
`claude` process), and gate on the worker's own exit evidence, never shell exit-0.
