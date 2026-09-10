"""Per-leaf unit tests for the four extracted tool leaf atoms.

Each leaf is tested directly (not through ToolRegistry) to confirm its pure
contract independently of the orchestrator. This is the property test_tools.py
cannot provide — direct exercise of the classification/path/shell logic as
standalone functions.

Leaves tested:
  bg_job.py      — _BgJob lifecycle (alive/kill)
  shell_select.py — pick_shell() pure function
  tool_policy.py  — classify_destructive() + target_exists()
  sandbox_path.py — normalize_path() + resolve_safe()
"""
import os
import sys
from pathlib import Path

import pytest


# ── bg_job.py ─────────────────────────────────────────────────────────────────

def test_bgjob_starts_alive():
    from echelon_engine.atoms.bg_job import _BgJob
    job = _BgJob(1, "test_tool", {"arg": "val"})
    assert job.alive() is True
    assert job.result is None
    assert job.killed is False


def test_bgjob_killed_not_alive():
    from echelon_engine.atoms.bg_job import _BgJob
    job = _BgJob(2, "test_tool", {})
    job.killed = True
    assert job.alive() is False


def test_bgjob_result_set_not_alive():
    from echelon_engine.atoms.bg_job import _BgJob
    job = _BgJob(3, "test_tool", {})
    job.result = "done"
    assert job.alive() is False


def test_bgjob_kill_no_proc_returns_true():
    from echelon_engine.atoms.bg_job import _BgJob
    job = _BgJob(4, "test_tool", {})
    assert job.kill() is True   # no proc attached — always returns True
    assert job.killed is True


# ── shell_select.py ────────────────────────────────────────────────────────────

def test_pick_shell_explicit_bash():
    from echelon_engine.atoms.shell_select import pick_shell
    exe, kind = pick_shell("/usr/bin/bash")
    assert exe == "/usr/bin/bash"
    assert kind == "posix"


def test_pick_shell_explicit_sh():
    from echelon_engine.atoms.shell_select import pick_shell
    exe, kind = pick_shell("/bin/sh")
    assert kind == "posix"


def test_pick_shell_explicit_non_posix():
    from echelon_engine.atoms.shell_select import pick_shell
    exe, kind = pick_shell("powershell.exe")
    assert exe == "powershell.exe"
    assert kind == "cmd"


def test_pick_shell_auto_returns_tuple():
    from echelon_engine.atoms.shell_select import pick_shell
    exe, kind = pick_shell(None)
    # Must return a 2-tuple; kind must be one of the three valid values
    assert kind in ("posix", "cmd", "default")
    # On Windows exe is always a string; on POSIX it can be None (shell=True default)
    if os.name == "nt":
        assert isinstance(exe, str) and len(exe) > 0


# ── tool_policy.py ─────────────────────────────────────────────────────────────

@pytest.fixture
def root(tmp_path):
    return tmp_path


def test_classify_destructive_rm_rf_is_bulk(root):
    from echelon_engine.atoms.tool_policy import classify_destructive
    cat, desc = classify_destructive("run_bash", {"cmd": "rm -rf /tmp/foo"}, root)
    assert cat == "bulk"
    assert "run_bash" in desc


def test_classify_destructive_rmdir_s_is_delete(root):
    from echelon_engine.atoms.tool_policy import classify_destructive
    cat, desc = classify_destructive("run_bash", {"cmd": "rmdir /s /q C:\\junk"}, root)
    assert cat == "delete"


def test_classify_destructive_stop_process_is_process_kill(root):
    from echelon_engine.atoms.tool_policy import classify_destructive
    cat, desc = classify_destructive("run_bash", {"cmd": "Get-Process chrome | Stop-Process -Force"}, root)
    assert cat == "process_kill"


def test_classify_destructive_taskkill_is_process_kill(root):
    from echelon_engine.atoms.tool_policy import classify_destructive
    cat, desc = classify_destructive("run_bash", {"cmd": "taskkill /F /PID 1234"}, root)
    assert cat == "process_kill"


def test_classify_destructive_safe_cmd_returns_none(root):
    from echelon_engine.atoms.tool_policy import classify_destructive
    cat, desc = classify_destructive("run_bash", {"cmd": "echo hello"}, root)
    assert cat is None
    assert desc == ""


def test_classify_destructive_edit_file_is_overwrite(root):
    from echelon_engine.atoms.tool_policy import classify_destructive
    cat, desc = classify_destructive("edit_file", {"path": "foo.py"}, root)
    assert cat == "overwrite"
    assert "foo.py" in desc


def test_classify_destructive_replace_in_file_is_overwrite(root):
    from echelon_engine.atoms.tool_policy import classify_destructive
    cat, desc = classify_destructive("replace_in_file", {"path": "bar.py"}, root)
    assert cat == "overwrite"


def test_classify_destructive_write_new_file_is_not_destructive(root):
    # write_file to a NON-EXISTING path is CREATE — not gated
    from echelon_engine.atoms.tool_policy import classify_destructive
    cat, _ = classify_destructive("write_file", {"path": "brand_new.txt"}, root)
    assert cat is None


def test_classify_destructive_write_existing_file_is_overwrite(root):
    # write_file to an EXISTING path is OVERWRITE — gated
    from echelon_engine.atoms.tool_policy import classify_destructive
    (root / "exists.txt").write_text("content", encoding="utf-8")
    cat, _ = classify_destructive("write_file", {"path": str(root / "exists.txt")}, root)
    assert cat == "overwrite"


def test_classify_destructive_git_force_push_is_bulk(root):
    from echelon_engine.atoms.tool_policy import classify_destructive
    cat, _ = classify_destructive("run_bash", {"cmd": "git push origin main --force"}, root)
    assert cat == "bulk"


def test_classify_destructive_unknown_tool_returns_none(root):
    from echelon_engine.atoms.tool_policy import classify_destructive
    cat, _ = classify_destructive("read_file", {"path": "x.txt"}, root)
    assert cat is None


def test_target_exists_missing_file(root):
    from echelon_engine.atoms.tool_policy import target_exists
    assert target_exists("nonexistent.txt", root) is False


def test_target_exists_real_file(root):
    from echelon_engine.atoms.tool_policy import target_exists
    (root / "real.txt").write_text("x", encoding="utf-8")
    assert target_exists(str(root / "real.txt"), root) is True


def test_target_exists_blank_path(root):
    from echelon_engine.atoms.tool_policy import target_exists
    assert target_exists("", root) is False
    assert target_exists("   ", root) is False


# ── sandbox_path.py ────────────────────────────────────────────────────────────

def test_normalize_path_posix_drive_form():
    from echelon_engine.atoms.sandbox_path import normalize_path
    if os.name == "nt":
        result = normalize_path("/f/WORK/foo.py")
        assert result.startswith("F:\\") or result.startswith("f:\\")
    else:
        # on POSIX, /f/WORK/foo.py is a real POSIX path — not translated
        result = normalize_path("/f/WORK/foo.py")
        assert result == "/f/WORK/foo.py"


def test_normalize_path_normal_path_unchanged():
    from echelon_engine.atoms.sandbox_path import normalize_path
    p = "some/relative/path.py"
    assert normalize_path(p) == p


def test_resolve_safe_relative_path_inside_root(root):
    from echelon_engine.atoms.sandbox_path import resolve_safe
    p = resolve_safe("subdir/file.txt", root, [])
    assert str(p).startswith(str(root))


def test_resolve_safe_absolute_path_inside_root(root):
    from echelon_engine.atoms.sandbox_path import resolve_safe
    inside = root / "a.txt"
    p = resolve_safe(str(inside), root, [])
    assert p == inside.resolve()


def test_resolve_safe_escape_raises(tmp_path):
    from echelon_engine.atoms.sandbox_path import resolve_safe
    from echelon_sdk.exceptions import ToolError
    # Two completely separate sibling dirs under tmp_path
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    with pytest.raises(ToolError, match="escapes"):
        resolve_safe(str(outside / "file.txt"), sandbox, [])


def test_resolve_safe_read_root_allowed_for_read(tmp_path):
    from echelon_engine.atoms.sandbox_path import resolve_safe
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    extra = tmp_path / "artifacts"
    extra.mkdir()
    p = resolve_safe(str(extra / "out.txt"), sandbox, [extra])
    assert p == (extra / "out.txt").resolve()


def test_resolve_safe_read_root_blocked_for_write(tmp_path):
    from echelon_engine.atoms.sandbox_path import resolve_safe
    from echelon_sdk.exceptions import ToolError
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    extra = tmp_path / "artifacts"
    extra.mkdir()
    # extra is allowed for READ but NOT for WRITE (for_write=True confines to sandbox only)
    with pytest.raises(ToolError, match="sandbox root"):
        resolve_safe(str(extra / "out.txt"), sandbox, [extra], for_write=True)
