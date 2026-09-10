"""fw — the framework door: pure pass-through to `echelon-fw`.

OPEN-0060 phase A, deliverable 4: `python -m echelon_engine fw <args...>` must forward argv
VERBATIM to the real `echelon-fw` console entry and mirror its exit code — no logic of its
own. These tests mock subprocess.run so they never actually invoke the framework CLI (which
may not even be installed in every test environment); they assert the forwarding contract:
argv passed through unchanged, exit code mirrored, and the PATH-missing fallback shape.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from echelon_engine.atoms import fw_cmd
from echelon_engine.__main__ import main


class TestFwPassThroughViaWhich:
    """When `echelon-fw` is found on PATH (the common case), fw_cmd shells out to it directly."""

    def test_argv_forwarded_verbatim(self):
        with patch("echelon_engine.atoms.fw_cmd.shutil.which", return_value="/fake/bin/echelon-fw"), \
             patch("echelon_engine.atoms.fw_cmd.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            rc = fw_cmd._main(["design", "scan", "--root", "."])

        assert rc == 0
        called_cmd = mock_run.call_args.args[0]
        assert called_cmd == ["/fake/bin/echelon-fw", "design", "scan", "--root", "."]

    def test_exit_code_mirrored_nonzero(self):
        with patch("echelon_engine.atoms.fw_cmd.shutil.which", return_value="/fake/bin/echelon-fw"), \
             patch("echelon_engine.atoms.fw_cmd.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=3)
            rc = fw_cmd._main(["compile"])

        assert rc == 3

    def test_help_flag_forwarded_not_intercepted(self):
        """--help must NOT be swallowed by an argparse layer in this door — it belongs to
        the framework's own parser."""
        with patch("echelon_engine.atoms.fw_cmd.shutil.which", return_value="/fake/bin/echelon-fw"), \
             patch("echelon_engine.atoms.fw_cmd.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            rc = fw_cmd._main(["--help"])

        assert rc == 0
        called_cmd = mock_run.call_args.args[0]
        assert called_cmd == ["/fake/bin/echelon-fw", "--help"]

    def test_empty_argv_forwarded(self):
        with patch("echelon_engine.atoms.fw_cmd.shutil.which", return_value="/fake/bin/echelon-fw"), \
             patch("echelon_engine.atoms.fw_cmd.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            rc = fw_cmd._main([])

        assert rc == 0
        called_cmd = mock_run.call_args.args[0]
        assert called_cmd == ["/fake/bin/echelon-fw"]

    def test_utf8_env_set(self):
        with patch("echelon_engine.atoms.fw_cmd.shutil.which", return_value="/fake/bin/echelon-fw"), \
             patch("echelon_engine.atoms.fw_cmd.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            fw_cmd._main(["list"])

        env = mock_run.call_args.kwargs.get("env")
        assert env is not None
        assert env.get("PYTHONUTF8") == "1"


class TestFwFallbackWithoutPath:
    """When `echelon-fw` is NOT on PATH, fall back to `python -X utf8 -m cli.main` in the
    framework path resolved from the harness contract."""

    def test_falls_back_to_module_form(self):
        with patch("echelon_engine.atoms.fw_cmd.shutil.which", return_value=None), \
             patch("echelon_engine.atoms.fw_cmd._contract_framework_path",
                   return_value="D:/repos/framework"), \
             patch("echelon_engine.atoms.fw_cmd.os.path.isdir", return_value=True), \
             patch("echelon_engine.atoms.fw_cmd.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            rc = fw_cmd._main(["grammar"])

        assert rc == 0
        called_cmd = mock_run.call_args.args[0]
        assert called_cmd[1:4] == ["-X", "utf8", "-m"]
        assert called_cmd[4] == "cli.main"
        assert called_cmd[5:] == ["grammar"]
        assert mock_run.call_args.kwargs.get("cwd") == "D:/repos/framework"

    def test_no_path_no_contract_reports_error(self, capsys):
        with patch("echelon_engine.atoms.fw_cmd.shutil.which", return_value=None), \
             patch("echelon_engine.atoms.fw_cmd._contract_framework_path", return_value=""):
            rc = fw_cmd._main(["compile"])

        assert rc == 1
        captured = capsys.readouterr()
        assert "echelon-fw" in captured.err


class TestFwRoutedFromMainDispatch:
    """The engine's top-level dispatcher (__main__.main) must route the 'fw' verb here."""

    def test_main_routes_fw_to_fw_cmd(self):
        with patch("echelon_engine.atoms.fw_cmd._main") as mock_fw_main:
            mock_fw_main.return_value = 0
            rc = main(["fw", "design", "scan"])

        mock_fw_main.assert_called_once_with(["design", "scan"])
        assert rc == 0

    def test_fw_listed_in_verbs_output(self, capsys):
        main(["verbs"])
        captured = capsys.readouterr()
        assert "\n  fw " in captured.out or "  fw  " in captured.out
        assert "framework" in captured.out.lower()
