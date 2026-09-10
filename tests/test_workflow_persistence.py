"""tests/test_workflow_persistence.py -- unit tests for workflow-run persistence (tail/wfpersist).

Tests: WorkflowRunJournal (start, record, finish, list_runs, load_run), crash-safety
(truncated last line doesn't poison reload), default in-memory (journal_dir=None),
run_workflow with journal (incremental persistence survives simulated restart).
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

import pytest

pytest.importorskip("echelon_engine.agent.workflow",
                    reason="echelon_engine.agent.workflow not available")

from echelon_engine.agent.workflow import (
    WorkflowRunJournal,
    _workflow_runs_dir,
    run_workflow,
    available_agents,
)


# -- helpers ----------------------------------------------------------------

def _tmp_dir():
    """A temporary directory that cleans up after the test."""
    return tempfile.TemporaryDirectory(prefix="echelon_test_wf_")


def _known_agent():
    try:
        return available_agents()[0]["agent"]
    except Exception:
        return "dev"


# -- WorkflowRunJournal basic I/O -------------------------------------------

class TestWorkflowRunJournal:
    def test_start_creates_journal_file(self):
        d = _tmp_dir()
        try:
            jd = Path(d.name)
            j = WorkflowRunJournal("run-1", jd)
            j.start({"goal": "test", "steps": 3, "waves": [["s1"]]})
            f = jd / "run-1.jsonl"
            assert f.exists(), "journal file must be created by start()"
            lines = f.read_text(encoding="utf-8").splitlines()
            assert len(lines) >= 1
            rec = json.loads(lines[0])
            assert rec["kind"] == "run_start"
            assert rec["goal"] == "test"
        finally:
            d.cleanup()

    def test_record_appends_lines(self):
        d = _tmp_dir()
        try:
            jd = Path(d.name)
            j = WorkflowRunJournal("run-2", jd)
            j.start({"steps": 2})
            j.record("step_start", {"id": "s1", "agent": "dev"})
            j.record("step_done", {"id": "s1", "status": "completed"})
            f = jd / "run-2.jsonl"
            lines = [json.loads(ln) for ln in f.read_text(encoding="utf-8").splitlines() if ln.strip()]
            assert len(lines) == 3  # start + 2 records
            assert lines[1]["kind"] == "step_start"
            assert lines[2]["kind"] == "step_done"
        finally:
            d.cleanup()

    def test_finish_writes_terminal_record(self):
        d = _tmp_dir()
        try:
            jd = Path(d.name)
            j = WorkflowRunJournal("run-3", jd)
            j.start({"steps": 1})
            j.finish({"ok": True, "elapsed": 2.5, "steps_done": 1})
            f = jd / "run-3.jsonl"
            lines = [json.loads(ln) for ln in f.read_text(encoding="utf-8").splitlines() if ln.strip()]
            assert lines[-1]["kind"] == "run_finish"
            assert lines[-1]["ok"] is True
        finally:
            d.cleanup()

    def test_null_journal_dir_is_in_memory(self):
        """journal_dir=None -> no file I/O (pure in-memory), the unchanged default."""
        j = WorkflowRunJournal("run-mem")
        j.start({"steps": 1})
        j.record("step_start", {"id": "s1"})
        j.finish({"ok": True})
        # No exception, no file created — the journal is a silent no-op
        assert j._path is None

    def test_default_in_memory_mode_unchanged(self):
        """Default construction (no journal_dir) must not touch the filesystem."""
        # ensure the default dir path resolver doesn't accidentally create dirs
        j = WorkflowRunJournal("run-default")
        assert j._path is None
        # start/record/finish should be silent no-ops
        j.start({"steps": 1})
        j.record("wave_start", {"wave": 0})
        j.finish({"ok": True})
        # No file should exist in the default dir for this run_id
        default_dir = _workflow_runs_dir()
        f = default_dir / "run-default.jsonl"
        assert not f.exists(), f"default in-memory mode must not create files, but found {f}"


# -- list_runs / load_run ---------------------------------------------------

class TestListAndLoadRuns:
    def test_list_runs_returns_summaries_newest_first(self):
        d = _tmp_dir()
        try:
            jd = Path(d.name)
            # Create two runs
            j1 = WorkflowRunJournal("aaa", jd)
            j1.start({"goal": "first", "steps": 2})
            j1.record("step_done", {"id": "s1", "status": "completed"})
            j1.finish({"ok": True, "elapsed": 1.0, "steps_done": 2})

            j2 = WorkflowRunJournal("bbb", jd)
            j2.start({"goal": "second", "steps": 1})
            j2.finish({"ok": False, "elapsed": 0.5, "steps_done": 0})

            runs = WorkflowRunJournal.list_runs(jd)
            assert len(runs) == 2
            # newest first (by mtime — bbb was written after aaa)
            assert runs[0]["run_id"] == "bbb"
            assert runs[0]["status"] == "failed"
            assert runs[1]["run_id"] == "aaa"
            assert runs[1]["status"] == "done"
            assert runs[1]["goal"] == "first"
        finally:
            d.cleanup()

    def test_list_runs_empty_dir(self):
        d = _tmp_dir()
        try:
            runs = WorkflowRunJournal.list_runs(Path(d.name))
            assert runs == []
        finally:
            d.cleanup()

    def test_load_run_returns_full_journal(self):
        d = _tmp_dir()
        try:
            jd = Path(d.name)
            j = WorkflowRunJournal("load-test", jd)
            j.start({"goal": "load me", "steps": 2})
            j.record("step_start", {"id": "s1"})
            j.record("step_done", {"id": "s1", "status": "completed"})
            j.finish({"ok": True, "elapsed": 3.0, "steps_done": 2})

            loaded = WorkflowRunJournal.load_run("load-test", jd)
            assert loaded is not None
            assert loaded["run_id"] == "load-test"
            assert loaded["status"] == "done"
            assert len(loaded["events"]) == 4  # start + 2 steps + finish
            assert loaded["result"]["ok"] is True
            assert loaded["result"]["elapsed"] == 3.0
        finally:
            d.cleanup()

    def test_load_run_not_found(self):
        d = _tmp_dir()
        try:
            loaded = WorkflowRunJournal.load_run("nonexistent", Path(d.name))
            assert loaded is None
        finally:
            d.cleanup()

    def test_load_run_still_running(self):
        """A run with no finish record is reported as 'running'."""
        d = _tmp_dir()
        try:
            jd = Path(d.name)
            j = WorkflowRunJournal("in-flight", jd)
            j.start({"steps": 3})
            j.record("step_start", {"id": "s1"})
            # no finish() — simulates a run that hasn't completed yet

            loaded = WorkflowRunJournal.load_run("in-flight", jd)
            assert loaded is not None
            assert loaded["status"] == "running"
            assert loaded["result"] is None
            assert loaded["finished_at"] is None
        finally:
            d.cleanup()


# -- crash-safety ----------------------------------------------------------

class TestCrashSafety:
    def test_truncated_last_line_skipped(self):
        """A partial/trailing line (kill -9 mid-write) must not poison the reload."""
        d = _tmp_dir()
        try:
            jd = Path(d.name)
            j = WorkflowRunJournal("crash-test", jd)
            j.start({"steps": 3})
            j.record("step_start", {"id": "s1"})
            j.record("step_done", {"id": "s1", "status": "completed"})
            j.record("step_start", {"id": "s2"})

            # Simulate a kill -9: append a partial line (truncated JSON)
            f = jd / "crash-test.jsonl"
            with open(f, "a", encoding="utf-8") as fh:
                fh.write('{"kind": "step_done", "id": "s2", "statu')  # truncated!
                fh.flush()

            # Reload — must see the intact lines, skip the partial one
            loaded = WorkflowRunJournal.load_run("crash-test", jd)
            assert loaded is not None
            assert loaded["status"] == "running"  # no finish record
            events = loaded["events"]
            # We should have: run_start + step_start s1 + step_done s1 + step_start s2 = 4
            assert len(events) == 4, f"expected 4 intact events, got {len(events)}"
            kinds = [e["kind"] for e in events]
            assert kinds == ["run_start", "step_start", "step_done", "step_start"]
        finally:
            d.cleanup()

    def test_simulated_restart_sees_run(self):
        """New WorkflowRunJournal object, same dir, same run_id -> can read the prior events."""
        d = _tmp_dir()
        try:
            jd = Path(d.name)
            # First "process": write a partial run
            j1 = WorkflowRunJournal("restart-test", jd)
            j1.start({"goal": "survive restart", "steps": 3})
            j1.record("step_start", {"id": "s1"})
            j1.record("step_done", {"id": "s1", "status": "completed"})
            # Simulate process restart — j1 is gone, create a new j2
            del j1

            # Second "process": load the same run from disk
            loaded = WorkflowRunJournal.load_run("restart-test", jd)
            assert loaded is not None
            assert loaded["status"] == "running"
            assert len(loaded["events"]) == 3  # start + step_start + step_done
            assert loaded["events"][0]["goal"] == "survive restart"
        finally:
            d.cleanup()

    def test_empty_file_handled(self):
        """An empty journal file (created but never written to) returns None gracefully."""
        d = _tmp_dir()
        try:
            jd = Path(d.name)
            jd.mkdir(parents=True, exist_ok=True)
            (jd / "empty.jsonl").write_text("", encoding="utf-8")
            loaded = WorkflowRunJournal.load_run("empty", jd)
            assert loaded is not None  # file exists, has 0 lines -> empty events list
            assert loaded["events"] == []
            assert loaded["status"] == "running"
        finally:
            d.cleanup()


# -- run_workflow with journal ----------------------------------------------

class TestRunWorkflowWithJournal:
    def test_journal_persists_events_incrementally(self):
        """run_workflow with journal: every event is written to the JSONL immediately."""
        ag = _known_agent()
        d = _tmp_dir()
        try:
            jd = Path(d.name)
            j = WorkflowRunJournal("wf-journal-test", jd)
            j.start({"goal": "test run", "steps": 2, "waves": [["s1"], ["s2"]]})

            wf = {"steps": [
                {"id": "s1", "agent": ag, "task": "t1", "depends_on": []},
                {"id": "s2", "agent": ag, "task": "t2", "depends_on": ["s1"]},
            ]}
            result = run_workflow(
                wf,
                run_step=lambda s, d: {"status": "completed", "answer": s["id"]},
                journal=j,
            )
            j.finish({"ok": result["ok"], "elapsed": result["elapsed"],
                      "steps_done": len(result["steps"])})

            # Verify journal file has the right events
            f = jd / "wf-journal-test.jsonl"
            assert f.exists()
            lines = [json.loads(ln) for ln in f.read_text(encoding="utf-8").splitlines() if ln.strip()]
            kinds = [ln["kind"] for ln in lines]
            assert "run_start" in kinds
            assert "workflow_start" in kinds
            assert "step_start" in kinds
            assert "step_done" in kinds
            assert "workflow_done" in kinds
            assert "run_finish" in kinds
            # start + finish are bookends
            assert kinds[0] == "run_start"
            assert kinds[-1] == "run_finish"
        finally:
            d.cleanup()

    def test_run_without_journal_stays_in_memory(self):
        """Default: no journal -> run_workflow works exactly as before (unchanged contract)."""
        ag = _known_agent()
        wf = {"steps": [
            {"id": "s1", "agent": ag, "task": "t", "depends_on": []},
        ]}
        # No journal param — must not crash, must return the usual shape
        result = run_workflow(wf, run_step=lambda s, d: {"status": "completed"})
        assert result["ok"] is True
        assert len(result["steps"]) == 1
        assert "waves" in result
        assert "elapsed" in result

    def test_simulated_restart_reconstructs_run(self):
        """Full round-trip: run with journal in one 'process', load in another."""
        ag = _known_agent()
        d = _tmp_dir()
        try:
            jd = Path(d.name)
            run_id = "roundtrip-test"

            # Process 1: create journal, run workflow
            j1 = WorkflowRunJournal(run_id, jd)
            j1.start({"goal": "roundtrip", "steps": 2, "waves": [["s1"], ["s2"]]})
            wf = {"steps": [
                {"id": "s1", "agent": ag, "task": "ta", "depends_on": []},
                {"id": "s2", "agent": ag, "task": "tb", "depends_on": ["s1"]},
            ]}
            result = run_workflow(
                wf,
                run_step=lambda s, d: {"status": "completed", "answer": s["id"]},
                journal=j1,
            )
            j1.finish({"ok": result["ok"], "elapsed": result["elapsed"],
                       "steps_done": len(result["steps"])})
            del j1, result

            # Process 2 (simulated restart): load the same run from the journal
            loaded = WorkflowRunJournal.load_run(run_id, jd)
            assert loaded is not None
            assert loaded["status"] == "done"
            assert loaded["result"]["ok"] is True
            assert loaded["result"]["steps_done"] == 2

            # Also check that list_runs finds it
            runs = WorkflowRunJournal.list_runs(jd)
            assert len(runs) == 1
            assert runs[0]["run_id"] == run_id
            assert runs[0]["status"] == "done"
        finally:
            d.cleanup()


# -- env var precedence -----------------------------------------------------

class TestEnvVarPrecedence:
    def test_echelon_workflow_runs_dir_overrides(self):
        d = _tmp_dir()
        try:
            old = os.environ.get("ECHELON_WORKFLOW_RUNS_DIR")
            os.environ["ECHELON_WORKFLOW_RUNS_DIR"] = d.name
            resolved = _workflow_runs_dir()
            assert resolved == Path(d.name).expanduser()
        finally:
            if old is not None:
                os.environ["ECHELON_WORKFLOW_RUNS_DIR"] = old
            else:
                os.environ.pop("ECHELON_WORKFLOW_RUNS_DIR", None)
            d.cleanup()

    def test_echelon_home_fallback(self):
        d = _tmp_dir()
        try:
            old_wf = os.environ.get("ECHELON_WORKFLOW_RUNS_DIR")
            old_home = os.environ.get("ECHELON_HOME")
            os.environ.pop("ECHELON_WORKFLOW_RUNS_DIR", None)
            os.environ["ECHELON_HOME"] = d.name
            resolved = _workflow_runs_dir()
            assert resolved == Path(d.name).expanduser() / "workflow_runs"
        finally:
            if old_wf is not None:
                os.environ["ECHELON_WORKFLOW_RUNS_DIR"] = old_wf
            else:
                os.environ.pop("ECHELON_WORKFLOW_RUNS_DIR", None)
            if old_home is not None:
                os.environ["ECHELON_HOME"] = old_home
            else:
                os.environ.pop("ECHELON_HOME", None)
            d.cleanup()
