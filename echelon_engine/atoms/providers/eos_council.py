"""EosCouncilProvider — AMD-T1 (the Qwen3-32B council) as a FIRST-CLASS provider tier.

Replaces the ad-hoc runs/ shim: AMD-T1 becomes a normal ProviderBase the loop / fork-field can
target like any other tier. The council is a free (rented-GPU, owner-run) capable tier — the cheap
floor that does heavy generation so the costly OS tier (Opus) only frames + gates.

TOPOLOGY (the eos reverse tunnel):
    OS (here)  --HTTP-->  eos_host.py (localhost:8888)  --SSE-->  VM agent (on the MI300X pod)
                                                                    runs vLLM at localhost:33769
    We enqueue {"kind":"chat", url, body} via POST /cmd; the VM proxies it to vLLM; the result comes
    back to eos_host. We read it via:
      - GET /wait/<id>   — BLOCKS until the result lands (push, no polling) — PREFERRED.
      - GET /result/<id> — one-shot poll — FALLBACK for an older eos_host without /wait.
    The provider auto-detects /wait and falls back to a poll loop, so it works against either host.

Stdlib only. See [[eos-reverse-tunnel-burst-t1-gpu-bridge]], [[agentic-models-are-text-action-not-tool-based]],
runs/v2-cos-audit-20260610/pipeline_design.md (the toolchain this anchors).
"""
from __future__ import annotations

import json
import time
import urllib.request
import urllib.error
from typing import Any

from .base import ProviderBase, LLMResponse, ToolCall

DEFAULT_HOST = "http://127.0.0.1:8888"
DEFAULT_VLLM = "http://localhost:33769/v1/chat/completions"  # from the VM agent's POV (it runs on the pod)
COUNCIL_MODEL = "qwen3-32b-burst"


class EosCouncilProvider(ProviderBase):
    """AMD-T1 over the eos tunnel as a ProviderBase. Text-action tier: it returns assistant TEXT
    (the new-era agentic models don't emit tool_calls arrays — the text IS the action; the compiler /
    caller interprets it). So tool_calls stays empty and content carries the council's output."""

    def __init__(self, host: str = DEFAULT_HOST, vllm_url: str = DEFAULT_VLLM,
                 model: str | None = None):
        self.host = host.rstrip("/")
        self.vllm_url = vllm_url
        # The served model is NOT assumed — the pod may serve ANYTHING (Qwen3-32B today, something
        # else tomorrow). `model` left None means "discover from vLLM /v1/models on first use"; an
        # explicit value pins it. COUNCIL_MODEL is only a last-resort fallback if discovery fails.
        self.model = model
        self._detected_model: str | None = model
        self._wait_supported: bool | None = None   # lazily probed once

    def detect_model(self) -> str:
        """The model vLLM is ACTUALLY serving — asked once via the VM's probe of /v1/models, cached.
        Falls back to COUNCIL_MODEL only if discovery fails. Keeps ECHELON model-agnostic."""
        if self._detected_model:
            return self._detected_model
        try:
            cid = self._enqueue({"kind": "probe",
                                 "url": self.vllm_url.replace("/chat/completions", "/models"),
                                 "timeout": 12})
            r = self._collect(cid, 20)
            out = (r.get("stdout") or "")
            data = json.loads(out[out.index("{"): out.rindex("}") + 1])
            self._detected_model = data["data"][0]["id"]
        except Exception:  # noqa: BLE001 — discovery failed: fall back to the known default
            self._detected_model = COUNCIL_MODEL
        return self._detected_model

    @property
    def name(self) -> str:
        return "eos_council"

    # --- transport ----------------------------------------------------------- #
    def _post(self, path: str, payload: dict, timeout: int = 15) -> dict:
        req = urllib.request.Request(self.host + path, data=json.dumps(payload).encode(),
                                     headers={"Content-Type": "application/json"})
        return json.loads(urllib.request.urlopen(req, timeout=timeout).read().decode())

    def _get(self, path: str, timeout: int = 15) -> dict:
        return json.loads(urllib.request.urlopen(self.host + path, timeout=timeout).read().decode())

    def _enqueue(self, cmd: dict) -> str:
        return self._post("/cmd", cmd)["id"]

    def _probe_wait(self) -> bool:
        """Does this eos_host expose the blocking /wait endpoint? Probe once (a missing id -> 404,
        a present endpoint -> a json body). Cache the answer."""
        if self._wait_supported is not None:
            return self._wait_supported
        try:
            self._get("/wait/__probe__?timeout=1", timeout=5)
            self._wait_supported = True
        except urllib.error.HTTPError as e:
            self._wait_supported = (e.code != 404)   # 404 => no /wait route -> fall back to poll
        except Exception:
            self._wait_supported = False
        return self._wait_supported

    def _collect(self, cid: str, timeout: int) -> dict:
        """Get the result. Prefer the blocking /wait (no poll churn); fall back to polling /result."""
        if self._probe_wait():
            try:
                return self._get(f"/wait/{cid}?timeout={timeout}", timeout=timeout + 15)
            except Exception:
                pass   # fall through to polling
        waited = 0
        while waited < timeout + 30:
            r = self._get(f"/result/{cid}")
            if not r.get("pending"):
                return r
            time.sleep(3); waited += 3
        return {"id": cid, "pending": True, "timeout": True}

    # --- the ProviderBase contract ------------------------------------------- #
    def send(self, messages: list[dict[str, Any]], model_id: str | None = None,
             tools: list[dict[str, Any]] | None = None, **kwargs: Any) -> LLMResponse:
        """One council turn. `messages` is OpenAI chat shape; returns the assistant TEXT as content.
        kwargs: temperature, max_tokens, timeout, strip_think, repo_files.

        FORK-THE-READ: pass `repo_files=[repo-relative paths]` to have the VM FETCH those source files
        from the box's /repo server and prepend them as context — so the OS sends only PATH NAMES, never
        the file bytes through its own context (the thin-trunk cost discipline). Requires an eos_agent
        with the repo_files chat mode (lands on the next clean pod rotation)."""
        body = {"model": model_id or self.model or self.detect_model(),
                "temperature": kwargs.get("temperature", 0.3),
                "max_tokens": kwargs.get("max_tokens", 2000),
                "messages": messages}
        # TOOL-CALLING: if the loop passes a tools schema, forward it so Qwen3 (vLLM launched with
        # --enable-auto-tool-choice --tool-call-parser hermes) emits NATIVE tool_calls — letting AMD-T1
        # DRIVE the loop, not only return text. Without tools it's the text-action tier (text IS the action).
        if tools:
            body["tools"] = tools
            body["tool_choice"] = kwargs.get("tool_choice", "auto")
        timeout = int(kwargs.get("timeout", 600))
        cmd = {"kind": "chat", "url": self.vllm_url, "body": body, "timeout": timeout}
        repo_files = kwargs.get("repo_files")
        if repo_files:
            cmd["repo_files"] = list(repo_files)   # the VM fetches these; OS never carries the bytes
        cid = self._enqueue(cmd)
        res = self._collect(cid, timeout)
        if res.get("timeout"):
            raise TimeoutError(f"council did not answer in {timeout}s (cid {cid})")
        if not res.get("ok"):
            raise RuntimeError("council error: " + str(res.get("stderr"))[:300])
        text = res.get("content", "") or ""
        if kwargs.get("strip_think", True) and "</think>" in text:
            text = text.split("</think>", 1)[1].strip()
        # parse native tool_calls if the VM passed them back (vLLM tool-calling) -> AMD-T1 drives the loop.
        parsed: list[ToolCall] = []
        for tc in res.get("tool_calls") or []:
            fn = tc.get("function", {})
            args = fn.get("arguments", "{}")
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except json.JSONDecodeError:
                    args = {"_raw": args}
            parsed.append(ToolCall(name=fn.get("name", ""), args=args, id=tc.get("id", "")))
        # token counts estimated pre-flight (the pod doesn't bill us — FREE tier — but the meter wants a number)
        tin = self.count_messages(messages)
        tout = self.count_tokens(text)
        return LLMResponse(content=text, tokens_in=tin, tokens_out=tout, tool_calls=parsed)

    def is_online(self) -> tuple[bool, str]:
        """Cheap health check: agent connected AND vLLM serving the council model. (status, detail)."""
        try:
            st = self._get("/status", timeout=8)
        except Exception as e:
            return False, f"eos_host unreachable: {str(e)[:80]}"
        if not st.get("agent_connected"):
            return False, "agent not connected (pod down / tunnel not dialed in)"
        try:
            cid = self._enqueue({"kind": "probe",
                                 "url": self.vllm_url.replace("/chat/completions", "/models"),
                                 "timeout": 12})
            r = self._collect(cid, 20)
            out = (r.get("stdout") or "") + (r.get("stderr") or "")
            if r.get("ok") and '"id"' in out and '"data"' in out:
                # serving — capture WHATEVER model id vLLM reports (model-agnostic; pod may serve anything)
                try:
                    data = json.loads(out[out.index("{"): out.rindex("}") + 1])
                    self._detected_model = data["data"][0]["id"]
                except Exception:  # noqa: BLE001
                    self._detected_model = self._detected_model or COUNCIL_MODEL
                return True, f"online: {self._detected_model}"
            return False, "vLLM not serving yet: " + out.strip()[:80]
        except Exception as e:
            return False, f"probe failed: {str(e)[:80]}"
