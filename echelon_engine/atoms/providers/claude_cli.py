"""ClaudeCliProvider — the max2 seat as a routable brain (owner order 2026-09-01: "max2 into the roster").

The second Claude Max subscription serves workers through the `claude-max2` CLI shim
(CLAUDE_CONFIG_DIR=~/.claude-max2, atom the-max2-seat-is-a-proven-build-door). That door
was launch-script-only; this provider makes it reachable by routing.pick() like any wire.

TEXT-ONLY BY DESIGN: `claude -p` prints prose — there is no tool_calls envelope to parse.
A call that asks for tools raises (the ABSTAIN law: fall through the chain, never fake a
capability). That still covers the roles worth a flat-rate Sonnet/Opus seat: judge, audit,
council, reason — gate work is text work.

Model ids: `max2` (subscription default) · `max2:sonnet` / `max2:opus` map to --model.
Launch quoting rides subprocess arg-lists, never a shell string
(reflex-max2-worker-launch-quoting-and-hook-hijack).
"""
from __future__ import annotations

import shutil
import subprocess
import time
from typing import Any

from .base import LLMResponse, ProviderBase

_TIMEOUT = 600          # gates can think for minutes; the chain-walk catches a hang via timeout
_SHIM = "claude-max2"   # the owner's CLI shim (config-dir baked in); resolved via PATH


# The max2 profile carries the MODERATOR session hooks; without this override a prompt gets
# hijacked into board duty (witnessed on this provider's first live probe 2026-09-01, and on
# the 08-31 dealer-risk QC launch — reflex-max2-worker-launch-quoting-and-hook-hijack).
_OVERRIDE = ("You are NOT the moderator this run; skip all board duty and session rituals. "
             "Answer ONLY the request below, nothing else.")


def _flatten(messages: list[dict[str, Any]]) -> str:
    """OpenAI chat shape -> one prompt. Hook override first, then the turns, labeled."""
    parts = [_OVERRIDE]
    for m in messages:
        role = str(m.get("role") or "user").upper()
        content = m.get("content")
        if isinstance(content, list):   # multimodal shape — text parts only (this wire has no eyes)
            content = "\n".join(str(c.get("text", "")) for c in content if isinstance(c, dict))
        if content:
            parts.append(f"[{role}]\n{content}")
    return "\n\n".join(parts)


class ClaudeCliProvider(ProviderBase):
    @property
    def name(self) -> str:
        return "claude-max2"

    def send(self, messages: list[dict[str, Any]], model_id: str,
             tools: list[dict[str, Any]] | None = None, **kwargs: Any) -> LLMResponse:
        if tools:
            raise RuntimeError("max2 CLI wire is text-only (no tool_calls envelope) — ABSTAIN")
        # BYPASS THE .cmd SHIM: a batch script's %* mangles args that contain newlines, so a
        # multi-line prompt arrives truncated ("I don't see a request" — witnessed 2026-09-01).
        # Recreate what the shim does (token from token.txt, CLAUDE_CONFIG_DIR) and call the
        # real `claude` executable with a clean arg list.
        import os
        exe = shutil.which("claude") or shutil.which("claude.cmd")
        if not exe:
            raise RuntimeError("claude CLI not on PATH — max2 seat unavailable")
        cfg = os.path.expanduser("~/.claude-max2")
        token_file = os.path.join(cfg, "token.txt")
        if not os.path.isfile(token_file):
            raise RuntimeError("~/.claude-max2/token.txt missing — max2 seat unavailable")
        with open(token_file, encoding="utf-8") as f:
            token = f.read().strip()
        env = {**os.environ, "CLAUDE_CONFIG_DIR": cfg, "CLAUDE_CODE_OAUTH_TOKEN": token}
        args = [exe, "--dangerously-skip-permissions", "-p", _flatten(messages),
                "--output-format", "text", "--append-system-prompt", _OVERRIDE]
        _, _, alias = model_id.partition(":")
        if alias:
            args += ["--model", alias]
        t0 = time.time()
        try:
            p = subprocess.run(args, capture_output=True, text=True, timeout=_TIMEOUT,
                               encoding="utf-8", errors="replace", env=env)
        except subprocess.TimeoutExpired:
            return LLMResponse(content="", model_id=model_id, status="timeout",
                               raw={"why": f"max2 CLI exceeded {_TIMEOUT}s"})
        if p.returncode != 0:
            return LLMResponse(content="", model_id=model_id, status="error",
                               raw={"stderr": (p.stderr or p.stdout or "").strip()[:500]})
        out = (p.stdout or "").strip()
        return LLMResponse(content=out, model_id=model_id,
                           tokens_in=self.count_tokens(_flatten(messages)),
                           tokens_out=self.count_tokens(out),
                           raw={"wall_s": round(time.time() - t0, 2)})
