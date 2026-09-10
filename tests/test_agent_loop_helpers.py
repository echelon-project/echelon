"""Tests for echelon_engine.agent.loop_helpers — pure / mock-provider paths.

Tests the constants, _offload (pure path: small results, structured-tool bypass,
re-offload guard), _reflect (JSON parsing / normalization), and _summarise_branch
with a scripted fake provider. _offload's file-write path is tested with tmp_path.
"""
from __future__ import annotations
import json
import pytest
from pathlib import Path
from unittest.mock import MagicMock, patch

from echelon_engine.agent.loop_helpers import (
    _OFFLOAD_THRESHOLD,
    _PEEK_LINES,
    _SYNTH_HARDGATE,
    _STRUCTURED_TOOLS,
    _STRUCTURED_CAP,
    _STRUCTURED_PEEK_LINES,
    _offload,
    _reflect,
    _summarise_branch,
)


# ── Constants ─────────────────────────────────────────────────────────────────

class TestConstants:
    def test_offload_threshold_is_4000(self):
        assert _OFFLOAD_THRESHOLD == 4000

    def test_synth_hardgate_is_3(self):
        assert _SYNTH_HARDGATE == 3

    def test_structured_tools_contains_map_repo(self):
        assert "map_repo" in _STRUCTURED_TOOLS

    def test_structured_cap_less_than_offload_threshold(self):
        """Structured cap (2500) < offload threshold (4000) — maps offload at a lower size."""
        assert _STRUCTURED_CAP < _OFFLOAD_THRESHOLD

    def test_peek_lines_positive(self):
        assert _PEEK_LINES > 0

    def test_structured_peek_lines_positive(self):
        assert _STRUCTURED_PEEK_LINES > 0


# ── _offload: small result pass-through ──────────────────────────────────────

class TestOffloadSmall:
    def test_small_result_returned_unchanged(self, tmp_path):
        short = "x" * (_OFFLOAD_THRESHOLD - 1)
        out = _offload(short, "read_file", {"path": "/foo"}, "cid1", 1, tmp_path / "outputs")
        assert out == short

    def test_exactly_threshold_returned_unchanged(self, tmp_path):
        exact = "x" * _OFFLOAD_THRESHOLD
        out = _offload(exact, "read_file", {}, "cid2", 1, tmp_path / "outputs")
        assert out == exact


class TestOffloadReoffloadGuard:
    def test_re_offload_of_outputs_handle_returns_raw(self, tmp_path):
        """Reading a path inside outputs/ must return the raw, never create another handle."""
        od = tmp_path / "outputs"
        od.mkdir()
        big = "A" * (_OFFLOAD_THRESHOLD + 500)
        # args contain a reference to the outputs dir name — the guard fires
        out = _offload(big, "read_file", {"path": str(od / "step001_foo.txt")}, "cid3", 1, od)
        assert out == big   # raw returned, no replacement


class TestOffloadStructured:
    def test_structured_tool_below_cap_inline(self, tmp_path):
        """map_repo output under _STRUCTURED_CAP stays inline (no file written)."""
        od = tmp_path / "outputs"
        small_map = "x" * (_STRUCTURED_CAP - 10)
        out = _offload(small_map, "map_repo", {}, "cid4", 1, od)
        assert out == small_map
        assert not od.exists() or not any(od.iterdir())

    def test_structured_tool_above_cap_writes_file(self, tmp_path):
        """map_repo output above both _STRUCTURED_CAP and _OFFLOAD_THRESHOLD writes a file."""
        od = tmp_path / "outputs"
        # Must exceed _OFFLOAD_THRESHOLD (4000) to pass the first guard,
        # AND exceed _STRUCTURED_CAP (2500) to trigger the structured offload path.
        big_map = ("line\n" * 200) + "x" * (_OFFLOAD_THRESHOLD + 100)
        assert len(big_map) > _OFFLOAD_THRESHOLD   # sanity
        out = _offload(big_map, "map_repo", {}, "cid5", 2, od)
        assert "read_file(" in out or str(od) in out
        assert od.exists()
        files = list(od.glob("*.txt"))
        assert len(files) == 1
        assert files[0].read_text(encoding="utf-8") == big_map

    def test_structured_tool_never_calls_summariser(self, tmp_path):
        """Even with a provider wired, structured tools bypass the summariser branch."""
        od = tmp_path / "outputs"
        big_map = "map_line\n" * 300
        mock_provider = MagicMock()
        out = _offload(big_map, "map_repo", {}, "cid6", 3, od,
                       provider=mock_provider, model="deepseek-chat", goal="g", history=[])
        mock_provider.send.assert_not_called()


class TestOffloadNormal:
    def test_large_result_writes_file(self, tmp_path):
        od = tmp_path / "outputs"
        big = "line of text\n" * 500   # ~6500 chars
        out = _offload(big, "read_file", {"path": "/some/file"}, "cid7", 5, od)
        assert od.exists()
        files = list(od.glob("*.txt"))
        assert len(files) == 1
        assert files[0].read_text(encoding="utf-8") == big

    def test_fallback_head_peek_in_output(self, tmp_path):
        od = tmp_path / "outputs"
        lines = [f"line {i}" for i in range(100)]
        big = "\n".join(lines)
        out = _offload(big, "some_tool", {}, "cid8", 1, od)
        # Without a provider, should fall back to head-peek
        assert "line 0" in out   # first line must be in the peek

    def test_summariser_branch_called_with_provider(self, tmp_path):
        od = tmp_path / "outputs"
        big = "A " * 3000   # > _OFFLOAD_THRESHOLD
        mock_provider = MagicMock()
        mock_resp = MagicMock()
        mock_resp.status = "success"
        mock_resp.content = "This output shows X which matters for goal G."
        mock_provider.send.return_value = mock_resp

        out = _offload(big, "run_bash", {"cmd": "ls"}, "cid9", 2, od,
                       goal="Find X", action="run_bash(ls)", history=[],
                       provider=mock_provider, model="deepseek-chat")
        # The summariser should have been called
        mock_provider.send.assert_called_once()
        assert "This output shows X" in out


# ── _reflect: JSON parsing and normalization ──────────────────────────────────

def _make_provider(content: str, status: str = "success"):
    p = MagicMock()
    resp = MagicMock()
    resp.status = status
    resp.content = content
    p.send.return_value = resp
    return p


class TestReflect:
    def test_empty_on_failed_provider(self):
        p = _make_provider("", status="error")
        result = _reflect("goal", "answer", [], p, "model")
        assert result == {}

    def test_multi_lesson_shape(self):
        payload = json.dumps({
            "lessons": [
                {"lesson": "Use ROT13 for all-letter ciphers.", "coordinate": "crypto:rot13"},
                {"lesson": "Windows shell uses backslash.", "coordinate": "env:windows:shell"},
            ],
            "valence": 0.5,
            "arousal": 0.3,
        })
        p = _make_provider(payload)
        r = _reflect("goal", "answer", [], p, "model")
        assert len(r["lessons"]) == 2
        assert r["lessons"][0]["lesson"].startswith("Use ROT13")
        assert r["valence"] == pytest.approx(0.5)
        assert r["arousal"] == pytest.approx(0.3)

    def test_single_lesson_backcompat(self):
        """Old single {lesson} shape must still work."""
        payload = json.dumps({
            "lesson": "Always check file encoding.",
            "coordinate": "env:encoding",
            "valence": 0.4,
            "arousal": 0.2,
        })
        p = _make_provider(payload)
        r = _reflect("goal", "answer", [], p, "model")
        assert len(r["lessons"]) == 1
        assert "encoding" in r["lessons"][0]["lesson"]

    def test_json_embedded_in_prose(self):
        """Provider may wrap JSON in prose text — extract the {…} block."""
        prose = 'Here is my analysis: {"lessons": [{"lesson": "Trim output.", "coordinate": "tooling:output"}], "valence": 0.6, "arousal": 0.4} Hope that helps!'
        p = _make_provider(prose)
        r = _reflect("goal", "answer", [], p, "model")
        assert len(r["lessons"]) == 1

    def test_empty_lesson_text_filtered(self):
        payload = json.dumps({
            "lessons": [
                {"lesson": "", "coordinate": "x"},
                {"lesson": "Valid lesson.", "coordinate": "y"},
            ],
            "valence": 0.4, "arousal": 0.3,
        })
        p = _make_provider(payload)
        r = _reflect("goal", "answer", [], p, "model")
        assert len(r["lessons"]) == 1
        assert r["lessons"][0]["lesson"] == "Valid lesson."

    def test_no_json_braces_returns_empty(self):
        p = _make_provider("No JSON here at all.")
        r = _reflect("goal", "answer", [], p, "model")
        assert r == {}

    def test_invalid_json_returns_empty(self):
        p = _make_provider("{not valid json}")
        r = _reflect("goal", "answer", [], p, "model")
        assert r == {}


# ── _summarise_branch ─────────────────────────────────────────────────────────

class TestSummariseBranch:
    def test_returns_content_on_success(self):
        p = _make_provider("The output shows file X exists at /tmp/x.")
        r = _summarise_branch("raw output text" * 100, "find X", "list_files(/)", [], p, "model")
        assert r == "The output shows file X exists at /tmp/x."

    def test_returns_empty_on_provider_failure(self):
        p = _make_provider("", status="error")
        r = _summarise_branch("raw", "goal", "action", [], p, "model")
        assert r == ""

    def test_sends_goal_and_action_in_message(self):
        p = _make_provider("summary")
        _summarise_branch("raw content here", "Find the bug", "read_file(foo.py)", [], p, "m")
        call_args = p.send.call_args
        msgs = call_args[0][0]   # positional first arg is the messages list
        last_user = next(m for m in reversed(msgs) if m["role"] == "user")
        assert "Find the bug" in last_user["content"]
        assert "read_file(foo.py)" in last_user["content"]

    def test_raw_truncated_to_14000(self):
        """Very large raw must be truncated before sending (to avoid huge context)."""
        p = _make_provider("ok")
        big_raw = "X" * 20000
        _summarise_branch(big_raw, "g", "a", [], p, "m")
        sent_content = p.send.call_args[0][0][-1]["content"]
        # The RAW OUTPUT section should not exceed ~14000+overhead chars
        raw_section = sent_content.split("RAW OUTPUT:\n", 1)[-1]
        assert len(raw_section) <= 14000 + 200   # small overhead for surrounding text
