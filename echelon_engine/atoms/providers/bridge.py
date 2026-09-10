"""BridgeProvider — the Copilot tier, reached over the live bridge socket.

This wraps the PROVEN bridge wire (D:\\WORK\\ECHELON-MCP\\eos\\copilot_bridge_client.py):
socket-first to 127.0.0.1:8765 (streaming), .txt file as fallback. We do NOT re-implement
the protocol — we call send_over_socket. The bridge speaks prompt->text (no native
tool-calls on this wire), so a BridgeProvider is a TEXT TRANSFORMER, not a tool-loop driver:
it answers "transform this text" calls, which is exactly the copilot-as-api-layered-worker
frame (substrate does I/O, the model only transforms text). Grok stays the loop driver; the
agent reaches Copilot models via the `reason` tool for heavy text work.

Models are addressed by their EXACT bridge id (gpt-5-mini, gpt-5.4-mini, claude-opus-4.8,
gpt-5.5, ...) — listed live via copilot_bridge_client.list_models(). No model is privileged
by position; which tier to reach is the agent's choice, guided by reasoning + seed, bounded
by the resource truth (the cost meter). See copilot-bridge-bypass, copilot-as-api-layered-worker.

The bridge does not return token counts, so tokens_in/out here are CHAR-BASED ESTIMATES
(~4 chars/token) used only to feed the cost meter — they are approximate by construction.
"""
from __future__ import annotations
from typing import Any

from .base import ProviderBase, LLMResponse

# Owner retired <mcp-checkout> on 2026-09-08. Do not import its
# client or revive it as a dependency of the current Agent runtime.
class RetiredBridgeError(RuntimeError):
    pass


def _load_client():
    raise RetiredBridgeError("ECHELON-MCP is retired; configure a supported provider")


def _flatten(messages: list[dict[str, Any]]) -> str:
    """Collapse the OpenAI chat array into a single prompt the bridge accepts. Tool-call /
    tool-result turns are rendered as plain text (the bridge wire has no tool channel)."""
    parts: list[str] = []
    for m in messages:
        role = m.get("role", "user").upper()
        content = m.get("content") or ""
        if m.get("tool_calls"):
            calls = "; ".join(
                f"{c['function']['name']}({c['function']['arguments']})"
                for c in m["tool_calls"]
            )
            content = (content + f"\n[tool call: {calls}]").strip()
        if role == "TOOL":
            role = "TOOL_RESULT"
        parts.append(f"{role}: {content}")
    return "\n\n".join(parts)


def _est_tokens(text: str) -> int:
    return max(1, len(text) // 4)


class BridgeProvider(ProviderBase):
    """The Copilot bridge as a ProviderBase. Prompt->text over the live socket."""

    def __init__(self, timeout: float = 120.0):
        self.timeout = timeout
        self._client = None

    @property
    def name(self) -> str:
        return "copilot-bridge"

    def _ensure(self):
        if self._client is None:
            self._client = _load_client()
        return self._client

    def list_models(self) -> str:
        return self._ensure().list_models()

    def send(
        self,
        messages: list[dict[str, Any]],
        model_id: str,
        tools: list[dict[str, Any]] | None = None,  # ignored: this wire has no tool channel
        **kwargs: Any,
    ) -> LLMResponse:
        prompt = _flatten(messages)
        try:
            client = self._ensure()
            # The legacy bridge silently picks models[0] when its substring hint
            # misses. Never send task content until the exact ID is advertised
            # and server-confirmed response provenance is supported.
            catalog = client.send_structured_request("__list_models__", timeout=self.timeout)
            if 2 not in catalog.get("protocol_versions", []):
                return LLMResponse(content="[bridge refused] structured model provenance protocol unavailable; no task prompt sent",
                                   model_id=model_id, status="error",
                                   raw={"requested_model": model_id, "model_identity_status": "protocol_unavailable"})
            models = catalog.get("models")
            if not isinstance(models, list) or not all(isinstance(m, dict) for m in models):
                raise ValueError("bridge model catalog invalid")
            if not any(m.get("id") == model_id for m in models):
                return LLMResponse(content="[bridge refused] requested model ID is not advertised; no task prompt sent",
                                   model_id=model_id, status="error",
                                   raw={"requested_model": model_id, "model_identity_status": "unavailable"})
            result = client.send_structured_request(prompt, model_id=model_id, timeout=self.timeout)
            if result.get("protocol_version") != 2 or result.get("requested_model") != model_id:
                raise ValueError("bridge response contract mismatch")
            selected = result.get("selected_model")
            if result.get("status") != "success" or result.get("complete") is not True:
                return LLMResponse(content="[bridge error] model response incomplete or failed",
                                   model_id=model_id, status="error", raw=result)
            if not isinstance(selected, dict) or selected.get("id") != model_id:
                raise ValueError("bridge selected model identity mismatch")
            text = result.get("content")
            if not isinstance(text, str):
                raise ValueError("bridge content must be text")
        except RetiredBridgeError:
            return LLMResponse(content="[bridge retired] ECHELON-MCP is obsolete; select a supported provider",
                               model_id=model_id, status="error",
                               raw={"requested_model": model_id, "error_code": "retired_bridge",
                                    "model_identity_status": "unavailable"})
        except Exception as e:  # noqa: BLE001 — the loop must always get a response object
            return LLMResponse(content=f"[bridge error] {type(e).__name__}; inspect bridge availability",
                               model_id=model_id, status="error",
                               raw={"requested_model": model_id, "model_identity_status": "unverified"})
        status = "success" if text else "error"
        return LLMResponse(
            content=text or "",
            tokens_in=_est_tokens(prompt),
            tokens_out=_est_tokens(text or ""),
            model_id=model_id,
            status=status,
            raw={**result, "model_identity_status": "server_confirmed",
                 "usage_basis": "character_estimate"},
        )
