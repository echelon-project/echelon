"""Tests for echelon_engine.agent.loop — unit-testable seams.

The full run() loop requires a live provider (network). This file tests:
  - Module import (the most important gate: proves all imports resolve).
  - The _next_floor() helper (pure logic — no network).
  - SYSTEM_PROMPT and _FLOOR_LADDER exist and are well-formed.
  - run_task construction (session=None path, scripted fake provider that calls finish()
    immediately — proves the loop terminates and returns an AgentResult).

NOTE: run() with a real provider is network-bound and not tested here. The scripted
fake provider path IS the unit smoke; the note is honest.
"""
from __future__ import annotations
import pytest
from unittest.mock import MagicMock

from echelon_engine.agent.loop import (
    run, run_task, SYSTEM_PROMPT, _FLOOR_LADDER, _next_floor,
)
from echelon_engine.agent.loop_models import AgentResult


# ── Module constants ──────────────────────────────────────────────────────────

class TestModuleConstants:
    def test_system_prompt_is_string(self):
        assert isinstance(SYSTEM_PROMPT, str)
        assert len(SYSTEM_PROMPT) > 50

    def test_system_prompt_contains_echelon(self):
        assert "ECHELON" in SYSTEM_PROMPT

    def test_floor_ladder_is_list(self):
        assert isinstance(_FLOOR_LADDER, list)
        assert len(_FLOOR_LADDER) >= 2

    def test_floor_ladder_contains_known_models(self):
        combined = " ".join(_FLOOR_LADDER).lower()
        assert "deepseek" in combined
        assert "grok" in combined


# ── _next_floor ───────────────────────────────────────────────────────────────

class TestNextFloor:
    def test_deepseek_goes_to_grok(self):
        nf = _next_floor("deepseek-chat")
        assert nf is not None
        assert "grok" in nf.lower()

    def test_grok_goes_up(self):
        nf = _next_floor("grok-4.3")
        assert nf is not None
        # Must be higher than grok in the ladder
        grok_idx = next(i for i, m in enumerate(_FLOOR_LADDER) if "grok" in m.lower())
        nf_idx = _FLOOR_LADDER.index(nf) if nf in _FLOOR_LADDER else len(_FLOOR_LADDER)
        assert nf_idx > grok_idx

    def test_top_model_returns_none(self):
        top = _FLOOR_LADDER[-1]
        nf = _next_floor(top)
        assert nf is None

    def test_unknown_model_goes_to_first_rung(self):
        nf = _next_floor("unknown-model-xyz")
        assert nf == _FLOOR_LADDER[0]

    def test_empty_string_goes_to_first_rung(self):
        nf = _next_floor("")
        assert nf == _FLOOR_LADDER[0]

    def test_opus_returns_none(self):
        nf = _next_floor("claude-opus-4-8")
        assert nf is None

    def test_sonnet_goes_to_opus(self):
        nf = _next_floor("claude-sonnet-4-6")
        assert nf is not None
        assert "opus" in nf.lower()


# ── run() with scripted fake provider ────────────────────────────────────────

def _make_scripted_provider(answer: str = "done"):
    """A fake provider that calls `finish` on the very first tool call."""
    provider = MagicMock()

    def _send(messages, model_id=None, tools=None, **kwargs):
        resp = MagicMock()
        resp.status = "success"
        resp.content = "I will now finish."
        resp.tokens_in = 10
        resp.tokens_out = 5
        resp.tokens_cached = 0

        finish_call = MagicMock()
        finish_call.name = "finish"
        finish_call.args = {"answer": answer}
        finish_call.id = "call_finish_001"
        resp.tool_calls = [finish_call]
        return resp

    provider.send.side_effect = _send
    return provider


def _make_scripted_tools(answer: str = "done"):
    """A fake ToolRegistry that handles finish() and schemas()."""
    tools = MagicMock()
    tools.schemas.return_value = [
        {"function": {"name": "finish", "parameters": {}}}
    ]
    tools._plan = []

    def _execute(name, args):
        if name == "finish":
            return args.get("answer", "done")
        return "ok"

    tools.execute.side_effect = _execute
    return tools


class TestRunScripted:
    def test_run_completes_with_finish(self):
        provider = _make_scripted_provider("the answer")
        tools = _make_scripted_tools("the answer")
        result = run("What is the answer?", provider, tools, "test-model", max_steps=5)
        assert isinstance(result, AgentResult)
        assert result.status == "completed"
        assert result.answer == "the answer"

    def test_run_steps_counted(self):
        provider = _make_scripted_provider("done")
        tools = _make_scripted_tools("done")
        result = run("Do something.", provider, tools, "test-model", max_steps=5)
        assert result.steps >= 1

    def test_run_events_emitted(self):
        events = []
        def on_event(kind, data):
            events.append(kind)

        provider = _make_scripted_provider("ok")
        tools = _make_scripted_tools("ok")
        run("goal", provider, tools, "model", on_event=on_event, max_steps=5)
        assert "step" in events
        assert "act" in events
        assert "finish" in events

    def test_batched_tools_each_emit_one_terminal_observation(self):
        """A multi-call turn preserves act/observe order and call identity for every extra tool."""
        provider = MagicMock()

        def call(name, args, call_id):
            tool_call = MagicMock()
            tool_call.name = name
            tool_call.args = args
            tool_call.id = call_id
            return tool_call

        responses = [
            [call("write_file", {"path": "first.txt", "content": "one"}, "write-1"),
             call("write_file", {"path": "second.txt", "content": "two"}, "write-2"),
             call("write_file", {"path": "third.txt", "content": "three"}, "write-3")],
            [call("finish", {"answer": "done"}, "finish-1")],
        ]

        def send(messages, model_id=None, tools=None, **kwargs):
            response = MagicMock()
            response.status = "success"
            response.content = "working"
            response.tokens_in = response.tokens_out = response.tokens_cached = 0
            response.tool_calls = responses.pop(0)
            return response

        provider.send.side_effect = send
        tools = _make_scripted_tools()

        def execute(name, args):
            if name == "finish":
                return args["answer"]
            if args["path"] == "second.txt":
                return "ERROR: simulated tool failure"
            return "wrote " + args["path"]

        tools.execute.side_effect = execute
        events = []
        result = run("batch observations", provider, tools, "model",
                     on_event=lambda kind, data: events.append((kind, data)), max_steps=3)

        assert result.status == "completed"
        batched = [(kind, data) for kind, data in events if data.get("batched")]
        assert [(kind, data["tool_call_id"], data["tool"]) for kind, data in batched] == [
            ("act", "write-2", "write_file"),
            ("observe", "write-2", "write_file"),
            ("act", "write-3", "write_file"),
            ("observe", "write-3", "write_file"),
        ]
        observed = [data for kind, data in batched if kind == "observe"]
        assert [data["result"] for data in observed] == [
            "ERROR: simulated tool failure", "wrote third.txt",
        ]
        assert [data["status"] for data in observed] == ["returned", "returned"]

    def test_batched_plan_block_emits_terminal_observation(self):
        provider = MagicMock()

        def call(name, args, call_id):
            tool_call = MagicMock()
            tool_call.name, tool_call.args, tool_call.id = name, args, call_id
            return tool_call

        responses = [
            [call("read_file", {"path": "readme.txt"}, "read-1"),
             call("write_file", {"path": "blocked.txt", "content": "no"}, "write-1")],
            [call("finish", {"answer": "done"}, "finish-1")],
        ]

        def send(messages, model_id=None, tools=None, **kwargs):
            response = MagicMock()
            response.status = "success"
            response.content = "working"
            response.tokens_in = response.tokens_out = response.tokens_cached = 0
            response.tool_calls = responses.pop(0)
            return response

        provider.send.side_effect = send
        tools = _make_scripted_tools()
        events = []
        result = run("plan batch", provider, tools, "model", mode="plan",
                     on_event=lambda kind, data: events.append((kind, data)), max_steps=3)

        assert result.status == "completed"
        blocked = [(kind, data) for kind, data in events if data.get("tool_call_id") == "write-1"]
        assert [(kind, data["tool"]) for kind, data in blocked] == [
            ("act", "write_file"), ("observe", "write_file"),
        ]
        assert blocked[1][1]["result"].startswith("[PLAN MODE")
        assert blocked[1][1]["status"] == "not_executed"
        assert blocked[1][1]["outcome"] == "not_executed"
        assert not any(call_args.args[0] == "write_file" for call_args in tools.execute.call_args_list)

    @pytest.mark.parametrize(
        ("raised", "status"),
        [(RuntimeError("private detail"), "error"), (KeyboardInterrupt(), "cancelled")],
    )
    def test_batched_raised_terminal_observation_reraises_without_later_call(self, raised, status):
        provider = MagicMock()

        def call(name, args, call_id):
            tool_call = MagicMock()
            tool_call.name, tool_call.args, tool_call.id = name, args, call_id
            return tool_call

        response = MagicMock()
        response.status = "success"
        response.content = "working"
        response.tokens_in = response.tokens_out = response.tokens_cached = 0
        response.tool_calls = [
            call("write_file", {"path": "primary.txt"}, "write-1"),
            call("write_file", {"path": "raised.txt"}, "write-2"),
            call("write_file", {"path": "later.txt"}, "write-3"),
        ]
        provider.send.return_value = response
        tools = _make_scripted_tools()
        executed = []

        def execute(name, args):
            executed.append(args.get("path", name))
            if args.get("path") == "raised.txt":
                raise raised
            return "ok"

        tools.execute.side_effect = execute
        events = []
        with pytest.raises(type(raised)):
            run("raised batch", provider, tools, "model",
                on_event=lambda kind, data: events.append((kind, data)), max_steps=2)

        raised_events = [(kind, data) for kind, data in events if data.get("tool_call_id") == "write-2"]
        assert [kind for kind, _ in raised_events] == ["act", "observe"]
        terminal = raised_events[-1][1]
        assert terminal == {
            "tool": "write_file", "batched": True, "tool_call_id": "write-2",
            "status": status, "outcome": "unknown", "error_type": type(raised).__name__,
        }
        assert executed == ["primary.txt", "raised.txt"]

    def test_batched_observation_precedes_offload_failure(self, monkeypatch):
        import echelon_engine.agent.loop as loop_module
        provider = MagicMock()

        def call(name, args, call_id):
            tool_call = MagicMock()
            tool_call.name, tool_call.args, tool_call.id = name, args, call_id
            return tool_call

        response = MagicMock()
        response.status = "success"
        response.content = "working"
        response.tokens_in = response.tokens_out = response.tokens_cached = 0
        response.tool_calls = [
            call("read_file", {"path": "primary.txt"}, "read-1"),
            call("write_file", {"path": "extra.txt"}, "write-2"),
        ]
        provider.send.return_value = response
        tools = _make_scripted_tools()
        offloads = []

        def fail_after_primary(*args, **kwargs):
            offloads.append(args[1])
            if len(offloads) == 2:
                raise RuntimeError("offload failed")
            return args[0]

        monkeypatch.setattr(loop_module, "_offload", fail_after_primary)
        events = []
        with pytest.raises(RuntimeError, match="offload failed"):
            run("offload order", provider, tools, "model", outputs_dir="unused",
                on_event=lambda kind, data: events.append((kind, data)), max_steps=2)

        observed = [data for kind, data in events if kind == "observe" and data.get("tool_call_id") == "write-2"]
        assert len(observed) == 1
        assert observed[0]["status"] == "returned" and observed[0]["outcome"] == "unknown"

    def test_batched_exception_survives_observation_callback_failure(self):
        provider = MagicMock()

        def call(name, args, call_id):
            tool_call = MagicMock()
            tool_call.name, tool_call.args, tool_call.id = name, args, call_id
            return tool_call

        response = MagicMock()
        response.status = "success"
        response.content = "working"
        response.tokens_in = response.tokens_out = response.tokens_cached = 0
        response.tool_calls = [
            call("read_file", {"path": "primary.txt"}, "read-1"),
            call("write_file", {"path": "raised.txt"}, "write-2"),
        ]
        provider.send.return_value = response
        tools = _make_scripted_tools()

        def execute(name, args):
            if args.get("path") == "raised.txt":
                raise RuntimeError("tool-private-detail")
            return "ok"

        def fail_observe(kind, data):
            if kind == "observe" and data.get("tool_call_id") == "write-2":
                raise LookupError("callback-private-detail")

        tools.execute.side_effect = execute
        with pytest.raises(RuntimeError) as caught:
            run("callback failure", provider, tools, "model", on_event=fail_observe, max_steps=2)

        notes = getattr(caught.value, "__notes__", [])
        assert notes == ["batched terminal observation emission failed: LookupError"]

    def test_batched_finish_keeps_existing_terminal_semantics(self):
        provider = MagicMock()

        def call(name, args, call_id):
            tool_call = MagicMock()
            tool_call.name, tool_call.args, tool_call.id = name, args, call_id
            return tool_call

        response = MagicMock()
        response.status = "success"
        response.content = "working"
        response.tokens_in = response.tokens_out = response.tokens_cached = 0
        response.tool_calls = [
            call("read_file", {"path": "first.txt"}, "read-1"),
            call("finish", {"answer": "batched done"}, "finish-2"),
        ]
        provider.send.return_value = response
        tools = _make_scripted_tools()
        events = []
        result = run("batched finish", provider, tools, "model",
                     on_event=lambda kind, data: events.append((kind, data)), max_steps=2)

        assert result.status == "done" and result.answer == "batched done"
        assert [args.args[0] for args in tools.execute.call_args_list] == ["read_file", "finish"]
        assert [kind for kind, _ in events if kind == "finish"] == ["finish"]

    @pytest.mark.parametrize("returned", [
        "REFUSED: destructive gate denied this call",
        "[background job 7 detached; poll check_bg(7)]",
    ])
    def test_batched_return_never_claims_successful_effect(self, returned):
        provider = MagicMock()

        def call(name, args, call_id):
            tool_call = MagicMock()
            tool_call.name, tool_call.args, tool_call.id = name, args, call_id
            return tool_call

        responses = []
        for calls in (
            [call("read_file", {"path": "primary.txt"}, "read-1"),
             call("run_bash", {"command": "bounded"}, "run-2")],
            [call("finish", {"answer": "done"}, "finish-1")],
        ):
            response = MagicMock()
            response.status = "success"
            response.content = "working"
            response.tokens_in = response.tokens_out = response.tokens_cached = 0
            response.tool_calls = calls
            responses.append(response)
        provider.send.side_effect = responses
        tools = _make_scripted_tools()

        def execute(name, args):
            return args.get("answer", returned)

        tools.execute.side_effect = execute
        events = []
        result = run("neutral returned status", provider, tools, "model",
                     on_event=lambda kind, data: events.append((kind, data)), max_steps=3)

        assert result.status == "completed"
        observed = [data for kind, data in events if kind == "observe" and data.get("tool_call_id") == "run-2"]
        assert observed == [{"tool": "run_bash", "result": returned, "batched": True,
                             "tool_call_id": "run-2", "status": "returned", "outcome": "unknown"}]

    def test_run_no_memory_no_boot(self):
        """Minimal run — no memory, no boot, should still complete."""
        provider = _make_scripted_provider("minimal")
        tools = _make_scripted_tools("minimal")
        result = run("minimal goal", provider, tools, "model", max_steps=3)
        assert result.status == "completed"
        assert result.woke is None   # no boot = woke is None

    def test_run_timeout_on_max_steps(self):
        """A provider that never calls finish hits the max_steps ceiling."""
        provider = MagicMock()

        def _send(messages, model_id=None, tools=None, **kwargs):
            resp = MagicMock()
            resp.status = "success"
            resp.content = "thinking"
            resp.tokens_in = 5
            resp.tokens_out = 3
            resp.tokens_cached = 0
            # No tool_calls — bare text, nudged once then blocked
            resp.tool_calls = []
            return resp

        provider.send.side_effect = _send
        tools = _make_scripted_tools()
        tools.schemas.return_value = []
        result = run("infinite goal", provider, tools, "model", max_steps=3)
        # bare text twice -> blocked (before max_steps)
        assert result.status in ("blocked", "timeout")

    def test_run_returns_agent_result_type(self):
        provider = _make_scripted_provider("x")
        tools = _make_scripted_tools("x")
        result = run("g", provider, tools, "m", max_steps=2)
        assert isinstance(result, AgentResult)
        assert isinstance(result.tokens_in, int)
        assert isinstance(result.tokens_out, int)
        assert isinstance(result.transcript, list)
        assert isinstance(result.warmth_trace, list)


# ── run_task() session plumbing ───────────────────────────────────────────────

class TestRunTask:
    def test_run_task_returns_result_and_session(self):
        """First call: returns (AgentResult, session_or_None)."""
        provider = _make_scripted_provider("task done")
        tools = _make_scripted_tools("task done")
        res, sess = run_task("task goal", provider, tools, "model", max_steps=3)
        assert isinstance(res, AgentResult)
        # session may be None (if bloated) or a Session object — both valid
        # (a 1-step run is tiny, so session should not be None)
        assert res.status in ("completed", "blocked", "timeout", "error")


def test_saved_warm_resume_preserves_boot_digest_across_tasks(tmp_path):
    from echelon_sdk.session import Session
    provider = _make_scripted_provider('done')
    tools = _make_scripted_tools()
    result, session = run_task('first task', provider, tools, 'test-model', max_steps=3)
    assert result.status == 'completed' and session is not None
    path = tmp_path / 'session.json'
    session.save(path)
    loaded = Session.load(path)
    digest = loaded.boot_sha256
    result, continued = run_task('second task', provider, tools, 'test-model', session=loaded, max_steps=3)
    assert result.status == 'completed'
    continued.save(path)
    restored = Session.load(path)
    assert restored.boot_sha256 == digest
    assert restored.tasks_done == 2
