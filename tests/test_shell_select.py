import os

import pytest

from echelon_engine.atoms import shell_select


@pytest.mark.skipif(os.name != "nt", reason="native Windows shell selection")
def test_wsl_launcher_is_not_an_automatic_native_shell(monkeypatch):
    monkeypatch.setenv("SystemRoot", r"C:\Windows")
    monkeypatch.setenv("COMSPEC", r"C:\Windows\System32\cmd.exe")
    monkeypatch.setattr(shell_select.shutil, "which", lambda name: r"C:\Windows\System32\bash.exe")
    assert shell_select.pick_shell(None) == (r"C:\Windows\System32\cmd.exe", "cmd")
    # Explicit intent remains an explicit contract, with no silent substitution.
    assert shell_select.pick_shell(r"C:\Windows\System32\bash.exe") == (r"C:\Windows\System32\bash.exe", "posix")


@pytest.mark.skipif(os.name != "nt", reason="native Windows shell selection")
def test_native_git_bash_remains_available(monkeypatch):
    executable = r"C:\Program Files\Git\bin\bash.exe"
    monkeypatch.setattr(shell_select.shutil, "which", lambda name: executable)
    assert shell_select.pick_shell(None) == (executable, "posix")


@pytest.mark.skipif(os.name != "nt", reason="Windows App Execution Alias")
def test_windowsapps_bash_alias_is_not_a_native_shell(monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", r"C:\Users\test\AppData\Local")
    monkeypatch.setenv("COMSPEC", r"C:\Windows\System32\cmd.exe")
    monkeypatch.setattr(shell_select.shutil, "which", lambda name: r"C:\Users\test\AppData\Local\Microsoft\WindowsApps\bash.EXE")
    assert shell_select.pick_shell(None) == (r"C:\Windows\System32\cmd.exe", "cmd")


def test_selected_shell_executes_in_requested_directory(tmp_path):
    from echelon_engine.atoms.tools import ToolRegistry
    registry = ToolRegistry(tmp_path)
    result = registry._run_bash("echo echelon-shell-ready")
    assert "echelon-shell-ready" in result
    assert "no installed distributions" not in result
