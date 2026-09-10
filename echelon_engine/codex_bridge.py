"""Local Anthropic-to-Codex App Server adapter used by :command:`claude-gpt`.

Codex App Server is deliberately the only component that handles ChatGPT OAuth.
The adapter never reads, exports, or logs its credentials.  A request receives a
fresh ephemeral Codex thread, so Claude Code remains the owner of conversation
history.  This is a compatibility bridge, not an OpenAI API replacement.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
import uvicorn


LOG = logging.getLogger("echelon.codex_bridge")


def _codex_executable() -> str:
    """Resolve Codex before spawning App Server; Claude Code may launch with a reduced PATH."""
    pinned = os.environ.get("ECHELON_CODEX_BIN", "")
    if pinned and Path(pinned).is_file():
        return pinned
    found = shutil.which("codex")
    if found:
        return found
    # The Windows Codex desktop/VS Code integration ships its bundled binary
    # here.  Claude Code can be launched with a reduced PATH, so this explicit
    # discovery is the reliable Plus/OAuth door on that installation.
    extension_root = Path.home() / ".vscode" / "extensions"
    candidates = sorted(extension_root.glob("openai.chatgpt-*/bin/windows-x86_64/codex.exe"),
                        key=lambda p: (p.stat().st_mtime_ns, str(p).lower()), reverse=True) if extension_root.exists() else []
    if candidates:
        return str(candidates[0])
    raise RuntimeError("Codex executable not found; set ECHELON_CODEX_BIN to the codex.exe path")


def _tool_contract(tools: Any) -> str:
    """Render the Claude tool surface Codex may request, never execute.

    Claude Code remains the tool owner.  Codex receives only the names,
    descriptions, and JSON schemas needed to choose its next action.
    """
    if not isinstance(tools, list):
        return "No Claude tools are available; return a final response."
    rows = []
    for tool in tools:
        if not isinstance(tool, dict) or not isinstance(tool.get("name"), str):
            continue
        rows.append({"name": tool["name"], "description": tool.get("description", ""),
                     "input_schema": tool.get("input_schema", {})})
    return json.dumps(rows, ensure_ascii=False, separators=(",", ":"))


def _translated_decision(text: str, tools: Any) -> tuple[str, list[dict[str, Any]]]:
    """Decode Codex's constrained JSON response into valid Claude tool uses.

    Invalid JSON, unknown tools, and malformed inputs intentionally degrade to
    plain text.  The bridge must never invent an executable Claude tool call.
    """
    source = (text or "").strip()
    if source.startswith("```"):
        source = source.split("\n", 1)[1] if "\n" in source else ""
        source = source.rsplit("```", 1)[0].strip()
    try:
        data = json.loads(source)
    except json.JSONDecodeError:
        return text, []
    if not isinstance(data, dict):
        return text, []
    if data.get("type") == "final" and isinstance(data.get("text"), str):
        return data["text"], []
    allowed = {t.get("name") for t in tools if isinstance(t, dict)} if isinstance(tools, list) else set()
    calls = data.get("tool_uses")
    if data.get("type") == "tool_use" and isinstance(data.get("name"), str):
        calls = [{"name": data["name"], "input": data.get("input", {})}]
    if not isinstance(calls, list) or not calls:
        return text, []
    normalized = []
    for call in calls:
        if not isinstance(call, dict) or call.get("name") not in allowed or not isinstance(call.get("input", {}), dict):
            return text, []
        normalized.append({"id": "toolu_" + uuid.uuid4().hex, "name": call["name"], "input": call.get("input", {})})
    return "", normalized


def _translator_instruction(tools: Any) -> str:
    return """You are the reasoning provider behind Claude Code. Claude Code owns every tool and executes the loop. You MUST NOT invoke tools yourself, edit files, or merely promise work. Decide the next concrete Claude action from the supplied conversation and tool results.

Return ONLY one JSON object, with no Markdown:
  {"type":"tool_use","tool_uses":[{"name":"EXACT_CLAUDE_TOOL_NAME","input":{...}}]}
or, only when the task is actually complete or requires no tool:
  {"type":"final","text":"concise completed answer"}

Use only the supplied Claude tools and their schemas. Never invent a tool name. The available Claude tools are:
""" + _tool_contract(tools)


class CodexRpc:
    """Small, synchronous JSON-RPC client for one local App Server process."""

    def __init__(self) -> None:
        neutral_cwd = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "echelon" / "codex-bridge"
        neutral_cwd.mkdir(parents=True, exist_ok=True)
        self.proc = subprocess.Popen(
            [_codex_executable(), "app-server", "--stdio"], cwd=neutral_cwd,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", bufsize=1,
        )
        self._messages: queue.Queue[dict[str, Any]] = queue.Queue()
        self._lock = threading.Lock()
        threading.Thread(target=self._read_stdout, daemon=True).start()
        self._id = 0
        self.call("initialize", {"clientInfo": {"name": "echelon-claude-gpt", "version": "0.1"},
                                 "capabilities": {"experimentalApi": True}}, timeout=20)

    def _read_stdout(self) -> None:
        assert self.proc.stdout is not None
        for line in self.proc.stdout:
            try:
                self._messages.put(json.loads(line))
            except json.JSONDecodeError:
                continue

    def _send(self, method: str, params: dict[str, Any]) -> int:
        self._id += 1
        request_id = self._id
        assert self.proc.stdin is not None
        self.proc.stdin.write(json.dumps({"id": request_id, "method": method, "params": params}) + "\n")
        self.proc.stdin.flush()
        return request_id

    def call(self, method: str, params: dict[str, Any], timeout: float = 60) -> dict[str, Any]:
        request_id = self._send(method, params)
        deadline = time.monotonic() + timeout
        deferred: list[dict[str, Any]] = []
        while time.monotonic() < deadline:
            try:
                message = self._messages.get(timeout=max(0.05, deadline - time.monotonic()))
            except queue.Empty:
                break
            if message.get("id") == request_id:
                for item in deferred:
                    self._messages.put(item)
                if "error" in message:
                    raise RuntimeError(message["error"].get("message", str(message["error"])))
                return message.get("result", {})
            deferred.append(message)
        for item in deferred:
            self._messages.put(item)
        raise RuntimeError(f"Codex App Server timed out while calling {method}")

    def complete(self, prompt: str, model: str, cwd: str, tools: Any = None) -> tuple[str, list[dict[str, Any]]]:
        # Keep all optional Codex instruction/context surfaces off.  The actual
        # Claude request is carried in `prompt`; Codex has no project cwd here.
        config = {
            "include_permissions_instructions": False,
            "include_apps_instructions": False,
            "include_collaboration_mode_instructions": False,
            "include_skill_instructions": False,
            "include_environment_context": False,
        }
        with self._lock:
            started = time.monotonic()
            from echelon_engine.codex_nerve import context_for
            nerve_context, nerve = context_for(prompt, cwd)
            LOG.info("turn started model=%s policy=ephemeral-minimal nerve=%s", model, nerve)
            thread = self.call("thread/start", {
                "ephemeral": True, "cwd": str(Path.cwd()), "sandbox": "read-only",
                "config": config, "model": model,
                "developerInstructions": _translator_instruction(tools) + "\n\n" + nerve_context,
            })
            thread_id = thread["thread"]["id"] if "thread" in thread else thread["id"]
            request_id = self._send("turn/start", {"threadId": thread_id, "input": [{"type": "text", "text": prompt}], "summary": "none"})
            text_parts: list[str] = []
            deadline = time.monotonic() + 600
            while time.monotonic() < deadline:
                message = self._messages.get(timeout=max(0.1, deadline - time.monotonic()))
                if message.get("id") == request_id and "error" in message:
                    raise RuntimeError(message["error"].get("message", str(message["error"])))
                params = message.get("params", {})
                if message.get("method") in ("item/completed", "item/updated"):
                    item = params.get("item", {})
                    if item.get("type") == "agentMessage" and item.get("text"):
                        text_parts.append(item["text"])
                if message.get("method") == "turn/completed" and params.get("turn", {}).get("id"):
                    break
            if not text_parts:
                raise RuntimeError("Codex returned no assistant message (sign in with `codex login --device-auth` and retry)")
            LOG.info("turn completed model=%s seconds=%.2f", model, time.monotonic() - started)
            return _translated_decision("\n".join(dict.fromkeys(text_parts)), tools)


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    pieces = []
    for block in content:
        if isinstance(block, dict):
            if block.get("type") == "text":
                pieces.append(block.get("text", ""))
            elif block.get("type") == "tool_result":
                pieces.append("[tool result]\n" + _content_text(block.get("content", "")))
    return "\n".join(pieces)


def _conversation(payload: dict[str, Any]) -> str:
    system = _content_text(payload.get("system", ""))
    rows = ["<claude-conversation>"]
    if system:
        rows += ["<system>", system, "</system>"]
    for message in payload.get("messages", []):
        rows += [f"<{message.get('role', 'user')}>", _content_text(message.get("content", "")), f"</{message.get('role', 'user')}>" ]
    rows.append("</claude-conversation>")
    return "\n".join(rows)


def _codex_model(claude_model: Any) -> str:
    """Map Claude Code's routing lanes onto the requested GPT-5.6 lanes."""
    name = str(claude_model or "").lower()
    if "fable" in name:
        return "gpt-5.6-sol"
    if "opus" in name:
        return "gpt-5.6-terra"
    # Sonnet, Haiku, and unknown/default requests use the economical worker tier.
    return "gpt-5.6-luna"


app = FastAPI()
_rpc: CodexRpc | None = None


def _message_body(payload: dict[str, Any], codex_model: str, answer: str,
                  tool_uses: list[dict[str, Any]]) -> dict[str, Any]:
    content: list[dict[str, Any]] = ([{"type": "text", "text": answer}] if answer else []) + [
        {"type": "tool_use", **tool} for tool in tool_uses]
    return {"id": "msg_" + uuid.uuid4().hex, "type": "message", "role": "assistant",
            "model": payload.get("model", codex_model), "stop_reason": "tool_use" if tool_uses else "end_turn",
            "stop_sequence": None, "usage": {"input_tokens": 0, "output_tokens": 0}, "content": content}


def _anthropic_events(body: dict[str, Any]):
    """The same synthetic Anthropic ordering used by GeminiProvider.anthropic_sse."""
    start = {**body, "content": [], "stop_reason": None, "stop_sequence": None,
             "usage": {"input_tokens": 0, "output_tokens": 0}}
    yield f"event: message_start\ndata: {json.dumps({'type': 'message_start', 'message': start})}\n\n"
    for index, block in enumerate(body["content"]):
        if block["type"] == "text":
            start_block, delta = {"type": "text", "text": ""}, {"type": "text_delta", "text": block["text"]}
        else:
            start_block, delta = {"type": "tool_use", "id": block["id"], "name": block["name"], "input": {}}, {"type": "input_json_delta", "partial_json": json.dumps(block["input"])}
        yield f"event: content_block_start\ndata: {json.dumps({'type': 'content_block_start', 'index': index, 'content_block': start_block})}\n\n"
        yield f"event: content_block_delta\ndata: {json.dumps({'type': 'content_block_delta', 'index': index, 'delta': delta})}\n\n"
        yield f"event: content_block_stop\ndata: {json.dumps({'type': 'content_block_stop', 'index': index})}\n\n"
    yield f"event: message_delta\ndata: {json.dumps({'type': 'message_delta', 'delta': {'stop_reason': body['stop_reason'], 'stop_sequence': None}, 'usage': {'output_tokens': 0}})}\n\n"
    yield "event: message_stop\ndata: {\"type\": \"message_stop\"}\n\n"


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "provider": "codex"}


@app.post("/v1/messages")
async def messages(request: Request) -> JSONResponse:
    global _rpc
    payload = await request.json()
    try:
        _rpc = _rpc or CodexRpc()
        codex_model = _codex_model(payload.get("model"))
        LOG.info("request route claude_model=%s codex_model=%s stream=%s",
                 payload.get("model"), codex_model, bool(payload.get("stream")))
        if not payload.get("stream"):
            import asyncio
            answer, tool_uses = await asyncio.to_thread(_rpc.complete, _conversation(payload), codex_model,
                                                         os.environ.get("ECHELON_GPT_CWD", os.getcwd()), payload.get("tools"))
            body = _message_body(payload, codex_model, answer, tool_uses)
            return JSONResponse(body)

        # Mirror GeminiProvider: run the blocking provider call off the event
        # loop and keep the Anthropic wire live while it decides the next tool.
        async def events():
            import asyncio
            task = asyncio.create_task(asyncio.to_thread(
                _rpc.complete, _conversation(payload), codex_model,
                os.environ.get("ECHELON_GPT_CWD", os.getcwd()), payload.get("tools")))
            while not task.done():
                try:
                    await asyncio.wait_for(asyncio.shield(task), timeout=2.5)
                except asyncio.TimeoutError:
                    yield "event: ping\ndata: {\"type\": \"ping\"}\n\n"
            answer, tool_uses = await task
            for event in _anthropic_events(_message_body(payload, codex_model, answer, tool_uses)):
                yield event
        return StreamingResponse(events(), media_type="text/event-stream")
    except Exception as exc:
        return JSONResponse({"type": "error", "error": {"type": "api_error", "message": str(exc)}}, status_code=502)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=18789)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
