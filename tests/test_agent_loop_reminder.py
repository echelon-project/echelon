"""Tests for echelon_engine.agent.loop_reminder — _StepReminder.

Pure logic: slot management, render output, the empty() guard.
No model call or I/O needed.
"""
from __future__ import annotations
import pytest

from echelon_engine.agent.loop_reminder import _StepReminder


class TestStepReminderEmpty:
    def test_fresh_is_empty(self):
        r = _StepReminder()
        assert r.empty() is True

    def test_todo_makes_not_empty(self):
        r = _StepReminder()
        r.todo = "do this"
        assert r.empty() is False

    def test_warmth_makes_not_empty(self):
        r = _StepReminder()
        r.warmth = "0.8 [warm]"
        assert r.empty() is False

    def test_category_makes_not_empty(self):
        r = _StepReminder()
        r.category = "thrash"
        assert r.empty() is False

    def test_guard_makes_not_empty(self):
        r = _StepReminder()
        r.add_guard("watch out")
        assert r.empty() is False

    def test_empty_guard_string_does_not_make_not_empty(self):
        r = _StepReminder()
        r.add_guard("")   # empty string — must NOT register
        assert r.empty() is True

    def test_whitespace_only_guard_registers_as_empty_string(self):
        """add_guard strips the text but only skips if the RAW string is falsy.
        '   ' is truthy (non-empty string), so it is appended as '' after strip.
        The guards list is non-empty, so empty() returns False.
        This matches the source: `if text: self.guards.append(text.strip())`."""
        r = _StepReminder()
        r.add_guard("   ")   # raw "   " is truthy -> appended as ""
        # guards = [""] — non-empty list, so empty() is False
        assert r.empty() is False
        assert r.guards == [""]


class TestStepReminderRender:
    def test_render_has_open_and_close_tags(self):
        r = _StepReminder()
        r.category = "advancing"
        out = r.render()
        assert out.startswith("<step-context>")
        assert out.endswith("</step-context>")

    def test_render_category_appears(self):
        r = _StepReminder()
        r.category = "synthesizing"
        out = r.render()
        assert "category: synthesizing" in out

    def test_render_todo_appears(self):
        r = _StepReminder()
        r.todo = "1. do A\n2. do B"
        out = r.render()
        assert "todo:" in out
        assert "1. do A" in out

    def test_render_warmth_appears(self):
        r = _StepReminder()
        r.warmth = "0.75 [warm] — you've been here before"
        out = r.render()
        assert "warmth: 0.75 [warm]" in out

    def test_render_guard_appears(self):
        r = _StepReminder()
        r.add_guard("Stop circling — pick a direction.")
        out = r.render()
        assert "guard: Stop circling" in out

    def test_render_multiple_guards(self):
        r = _StepReminder()
        r.add_guard("Guard A")
        r.add_guard("Guard B")
        out = r.render()
        assert "guard: Guard A" in out
        assert "guard: Guard B" in out

    def test_empty_slots_not_shown(self):
        r = _StepReminder()
        r.category = "advancing"
        # todo, warmth, guards all empty — they should not appear
        out = r.render()
        assert "todo:" not in out
        assert "warmth:" not in out
        assert "guard:" not in out

    def test_category_first_in_render(self):
        """Category should appear before todo/warmth in the render order."""
        r = _StepReminder()
        r.category = "circling"
        r.todo = "some plan"
        out = r.render()
        assert out.index("category:") < out.index("todo:")

    def test_guard_stripped(self):
        r = _StepReminder()
        r.add_guard("  trailing spaces  ")
        out = r.render()
        assert "guard: trailing spaces" in out
        assert "  trailing spaces  " not in out


class TestStepReminderSlots:
    def test_slots_defined(self):
        """_StepReminder uses __slots__ — verify no __dict__."""
        r = _StepReminder()
        assert not hasattr(r, "__dict__")

    def test_guards_list_default_empty(self):
        r = _StepReminder()
        assert r.guards == []

    def test_multiple_add_guard_accumulates(self):
        r = _StepReminder()
        r.add_guard("A")
        r.add_guard("B")
        r.add_guard("C")
        assert len(r.guards) == 3
