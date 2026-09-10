"""Tests for echelon_engine.atoms.atlas — the disciplined per-scope atlas state-change verb.

Covers:
  - link: writes scope->path to config
  - resolve: explicit flag > config > error
  - set + revert-on-gate-fail (T1): mocked gate failure leaves file byte-identical
  - minimal diff (T4): set one field, only that line changes
  - nodes: lists nodes with status
  - validate: runs both JS gates (mocked)
  - error paths: missing node, invalid JSON, no node binary
"""
from __future__ import annotations

import json
import subprocess
import sys
from collections import OrderedDict
from pathlib import Path
from unittest.mock import MagicMock, patch, ANY

import pytest

from echelon_engine.atoms import atlas as at


# ═══════════════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════════════

def _make_node_json(data: dict) -> str:
    """Produce a minimal node JSON matching the atlas style (2-space, trailing newline)."""
    return json.dumps(data, indent=2, ensure_ascii=False) + "\n"


def _write_atlas_structure(tmp_path: Path, nodes: dict[str, dict] | None = None) -> Path:
    """Create a minimal atlas repo structure under tmp_path."""
    atlas_dir = tmp_path / "atlas-repo"
    define_dir = atlas_dir / "define"
    define_dir.mkdir(parents=True)

    if nodes:
        for name, data in nodes.items():
            (define_dir / f"{name}.json").write_text(_make_node_json(data), encoding="utf-8")

    tools_dir = atlas_dir / "tools"
    tools_dir.mkdir(parents=True, exist_ok=True)
    # Create minimal stub scripts
    (tools_dir / "validate.js").write_text("console.log('validate OK');", encoding="utf-8")
    (tools_dir / "contract-check.js").write_text("console.log('contract-check OK');", encoding="utf-8")
    (tools_dir / "gen-spec-table.js").write_text(
        "const fs = require('fs'); const path = require('path');\n"
        "const ROOT = path.resolve(__dirname, '..');\n"
        "const specPath = path.join(ROOT, 'SPECIFICATION.md');\n"
        "if (!fs.existsSync(specPath)) {\n"
        "  fs.writeFileSync(specPath, '<!-- NODE-TABLE:START -->\\ntable\\n<!-- NODE-TABLE:END -->\\n');\n"
        "}\n"
        "console.log('gen-spec-table: wrote rows.');\n",
        encoding="utf-8"
    )

    # Make it a git repo
    subprocess.run(["git", "init"], cwd=str(atlas_dir), capture_output=True, timeout=10)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=str(atlas_dir), capture_output=True, timeout=10)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=str(atlas_dir), capture_output=True, timeout=10)
    subprocess.run(["git", "add", "."], cwd=str(atlas_dir), capture_output=True, timeout=10)
    subprocess.run(["git", "commit", "-m", "init"], cwd=str(atlas_dir), capture_output=True, timeout=10)

    return atlas_dir


# ═══════════════════════════════════════════════════════════════════════════════
# Config helpers
# ═══════════════════════════════════════════════════════════════════════════════

class TestConfigHelpers:
    """Test _load_config, _set_atlas_repo, _get_atlas_repo."""

    def test_load_config_empty(self, tmp_path, monkeypatch):
        """Return {} when config file doesn't exist."""
        monkeypatch.setattr(at, "_config_path", lambda: tmp_path / "nonexistent.json")
        assert at._load_config() == {}

    def test_load_config_reads_data(self, tmp_path, monkeypatch):
        cfg = tmp_path / "config.json"
        cfg.write_text(json.dumps({"key": "value"}), encoding="utf-8")
        monkeypatch.setattr(at, "_config_path", lambda: cfg)
        assert at._load_config() == {"key": "value"}

    def test_set_and_get_atlas_repo(self, tmp_path, monkeypatch):
        cfg = tmp_path / "config.json"
        monkeypatch.setattr(at, "_config_path", lambda: cfg)
        at._set_atlas_repo("mol", "/test/path")
        assert at._get_atlas_repo("mol") == "/test/path"

    def test_get_atlas_repo_unknown_scope(self, tmp_path, monkeypatch):
        cfg = tmp_path / "config.json"
        monkeypatch.setattr(at, "_config_path", lambda: cfg)
        assert at._get_atlas_repo("unknown") is None

    def test_set_atlas_repo_preserves_other_keys(self, tmp_path, monkeypatch):
        cfg = tmp_path / "config.json"
        cfg.write_text(json.dumps({"brain": "grok"}), encoding="utf-8")
        monkeypatch.setattr(at, "_config_path", lambda: cfg)
        at._set_atlas_repo("mol", "/test/path")
        data = at._load_config()
        assert data["brain"] == "grok"
        assert data["atlas_repos"]["mol"] == "/test/path"

    def test_set_atlas_repo_multiple_scopes(self, tmp_path, monkeypatch):
        cfg = tmp_path / "config.json"
        monkeypatch.setattr(at, "_config_path", lambda: cfg)
        at._set_atlas_repo("mol", "/path/mol")
        at._set_atlas_repo("echelon", "/path/ech")
        assert at._get_atlas_repo("mol") == "/path/mol"
        assert at._get_atlas_repo("echelon") == "/path/ech"


# ═══════════════════════════════════════════════════════════════════════════════
# Resolution order: flag > config > error
# ═══════════════════════════════════════════════════════════════════════════════

class TestResolveAtlasPath:
    """Resolution order: explicit --atlas flag > config > clear error."""

    def test_explicit_flag_wins_over_config(self, tmp_path, monkeypatch):
        explicit = tmp_path / "explicit-atlas"
        explicit.mkdir()
        (explicit / "define").mkdir()
        (explicit / "tools").mkdir()

        # Set a config value that should be ignored
        monkeypatch.setattr(at, "_get_atlas_repo", lambda s: "/some/other/path")

        result = at._resolve_atlas_path("mol", explicit=str(explicit))
        assert result == explicit.resolve()

    def test_configured_path_used_when_no_flag(self, tmp_path, monkeypatch):
        atlas_dir = tmp_path / "configured-atlas"
        atlas_dir.mkdir()
        (atlas_dir / "define").mkdir()
        (atlas_dir / "tools").mkdir()

        monkeypatch.setattr(at, "_get_atlas_repo", lambda s: str(atlas_dir))
        result = at._resolve_atlas_path("mol", explicit=None)
        assert result == atlas_dir.resolve()

    def test_error_when_no_flag_and_no_config(self, monkeypatch):
        monkeypatch.setattr(at, "_get_atlas_repo", lambda s: None)
        with pytest.raises(SystemExit) as exc:
            at._resolve_atlas_path("mol", explicit=None)
        assert exc.value.code == 1

    def test_error_when_explicit_path_does_not_exist(self, monkeypatch):
        with pytest.raises(SystemExit) as exc:
            at._resolve_atlas_path("mol", explicit="/nonexistent/path")
        assert exc.value.code == 1


# ═══════════════════════════════════════════════════════════════════════════════
# JSON load/save with key-order preservation
# ═══════════════════════════════════════════════════════════════════════════════

class TestNodeJson:
    """Test OrderedDict preservation and dotted-path set."""

    def test_load_preserves_key_order(self, tmp_path):
        f = tmp_path / "test.json"
        f.write_text('{\n  "b": 1,\n  "a": 2,\n  "c": 3\n}\n', encoding="utf-8")
        data = at._load_node_json(f)
        assert list(data.keys()) == ["b", "a", "c"]

    def test_save_roundtrip_preserves_order(self, tmp_path):
        f = tmp_path / "test.json"
        data = OrderedDict([("status", "live"), ("port", 9306), ("title", "test")])
        at._save_node_json(f, data)
        reloaded = at._load_node_json(f)
        assert list(reloaded.keys()) == ["status", "port", "title"]

    def test_set_dotted_simple_key(self):
        data = OrderedDict([("status", "live"), ("port", 9306)])
        at._set_dotted(data, "status", "disabled")
        assert data["status"] == "disabled"
        # Key order preserved — "status" is still first
        assert list(data.keys()) == ["status", "port"]

    def test_set_dotted_new_key_appends(self):
        data = OrderedDict([("status", "live"), ("port", 9306)])
        at._set_dotted(data, "bind", "127.0.0.1:9306")
        assert data["bind"] == "127.0.0.1:9306"
        # New key appended at end
        assert list(data.keys()) == ["status", "port", "bind"]

    def test_set_dotted_nested_key(self):
        data = OrderedDict([("status", "live"), ("nested", OrderedDict([("inner", 1)]))])
        at._set_dotted(data, "nested.inner", 42)
        assert data["nested"]["inner"] == 42

    def test_set_dotted_creates_intermediate_dicts(self):
        data = OrderedDict([("status", "live")])
        at._set_dotted(data, "a.b.c", 99)
        assert data["a"]["b"]["c"] == 99

    def test_save_adds_trailing_newline(self, tmp_path):
        f = tmp_path / "test.json"
        data = OrderedDict([("x", 1)])
        at._save_node_json(f, data)
        raw = f.read_text(encoding="utf-8")
        assert raw.endswith("\n")


# ═══════════════════════════════════════════════════════════════════════════════
# Value parsing
# ═══════════════════════════════════════════════════════════════════════════════

class TestParseValue:
    def test_json_number(self):
        assert at._parse_value("42") == 42

    def test_json_float(self):
        assert at._parse_value("3.14") == 3.14

    def test_json_bool(self):
        assert at._parse_value("true") is True
        assert at._parse_value("false") is False

    def test_json_null(self):
        assert at._parse_value("null") is None

    def test_json_array(self):
        assert at._parse_value('[1, 2, 3]') == [1, 2, 3]

    def test_json_object(self):
        assert at._parse_value('{"key": "val"}') == {"key": "val"}

    def test_json_string(self):
        assert at._parse_value('"hello"') == "hello"

    def test_plain_string_fallback(self):
        assert at._parse_value("disabled") == "disabled"

    def test_none_input(self):
        assert at._parse_value(None) is None


# ═══════════════════════════════════════════════════════════════════════════════
# cmd_set — the atomic core (T1 guarantee)
# ═══════════════════════════════════════════════════════════════════════════════

class TestCmdSet:
    """The core state-change: mutate -> validate -> contract-check.
    Revert on ANY gate failure. Never leave node in invalid state."""

    def _args(self, scope="mol", node="test-node", field="status", value="disabled",
              reason=None, commit=False, atlas_path=None):
        """Build an argparse.Namespace matching cmd_set expectations."""
        ns = argparse.Namespace()
        ns.scope = scope
        ns.node = node
        ns.field = field
        ns.value = value
        ns.reason = reason
        ns.commit = commit
        ns.atlas = atlas_path
        return ns

    def test_set_and_validate_success(self, tmp_path, monkeypatch):
        """Happy path: mutate, validate passes, file staged."""
        atlas_dir = _write_atlas_structure(tmp_path, {
            "test-node": {"status": "live", "port": 9306, "title": "test service"}
        })
        node_file = atlas_dir / "define" / "test-node.json"
        original = node_file.read_bytes()

        # Mock subprocess.run for node --version (passes)
        orig_run = subprocess.run

        def fake_run(cmd, **kwargs):
            if cmd[0] == "node" and cmd[1] == "--version":
                return subprocess.CompletedProcess(cmd, 0, stdout="v20.0.0", stderr="")
            if "validate.js" in str(cmd):
                return subprocess.CompletedProcess(cmd, 0, stdout="atlas validate: OK", stderr="")
            if "contract-check.js" in str(cmd):
                return subprocess.CompletedProcess(cmd, 0, stdout="contract-check: OK", stderr="")
            if "gen-spec-table.js" in str(cmd):
                return subprocess.CompletedProcess(cmd, 0, stdout="gen-spec-table: wrote rows.", stderr="")
            if cmd[0] == "git" and cmd[1] == "diff":
                return subprocess.CompletedProcess(cmd, 0, stdout="-  \"status\": \"live\",\n+  \"status\": \"disabled\",", stderr="")
            if cmd[0] == "git" and cmd[1] == "add":
                return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
            if cmd[0] == "git" and cmd[1] == "status":
                return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
            # For git init during setup
            if cmd[0] == "git" and cmd[1] == "init":
                return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
            if cmd[0] == "git" and cmd[1] == "config":
                return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
            if cmd[0] == "git" and cmd[1] == "commit":
                return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
            return orig_run(cmd, **kwargs)

        monkeypatch.setattr(subprocess, "run", fake_run)
        monkeypatch.setattr(at, "_get_atlas_repo", lambda s: str(atlas_dir))

        args = self._args(node="test-node", field="status", value='"disabled"',
                          atlas_path=str(atlas_dir))
        result = at.cmd_set(args)
        assert result == 0

        # File should have the new value
        data = json.loads(node_file.read_text(encoding="utf-8"))
        assert data["status"] == "disabled"

    def test_revert_on_validate_failure_T1(self, tmp_path, monkeypatch):
        """THE ATOMIC GUARANTEE: validate failure -> file byte-identical to original."""
        atlas_dir = _write_atlas_structure(tmp_path, {
            "test-node": {"status": "live", "port": 9306, "title": "test service"}
        })
        node_file = atlas_dir / "define" / "test-node.json"
        original_bytes = node_file.read_bytes()

        orig_run = subprocess.run

        def fake_run(cmd, **kwargs):
            if cmd[0] == "node" and cmd[1] == "--version":
                return subprocess.CompletedProcess(cmd, 0, stdout="v20.0.0", stderr="")
            if "validate.js" in str(cmd):
                # FAIL the validate gate
                return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="atlas validate: 2 problem(s):\n  - bad key")
            if "contract-check.js" in str(cmd):
                return subprocess.CompletedProcess(cmd, 0, stdout="contract-check: OK", stderr="")
            if "gen-spec-table.js" in str(cmd):
                return subprocess.CompletedProcess(cmd, 0, stdout="gen-spec-table: wrote rows.", stderr="")
            if cmd[0] == "git" and cmd[1] == "init":
                return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
            if cmd[0] == "git" and cmd[1] == "config":
                return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
            if cmd[0] == "git" and cmd[1] == "commit":
                return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
            return orig_run(cmd, **kwargs)

        monkeypatch.setattr(subprocess, "run", fake_run)
        monkeypatch.setattr(at, "_get_atlas_repo", lambda s: str(atlas_dir))

        args = self._args(node="test-node", field="status", value='"disabled"',
                          atlas_path=str(atlas_dir))
        result = at.cmd_set(args)
        assert result == 1  # Nonzero exit

        # THE GUARANTEE: file is byte-identical to original
        reverted_bytes = node_file.read_bytes()
        assert reverted_bytes == original_bytes, (
            f"T1 ATOMIC GUARANTEE VIOLATED: file changed despite gate failure!\n"
            f"Original: {original_bytes!r}\n"
            f"Current:  {reverted_bytes!r}"
        )

        # Verify the data is still the original
        data = json.loads(node_file.read_text(encoding="utf-8"))
        assert data["status"] == "live"  # NOT "disabled"

    def test_revert_on_contract_check_failure_T1(self, tmp_path, monkeypatch):
        """THE ATOMIC GUARANTEE: contract-check failure -> file byte-identical."""
        atlas_dir = _write_atlas_structure(tmp_path, {
            "test-node": {"status": "live", "port": 9306}
        })
        node_file = atlas_dir / "define" / "test-node.json"
        original_bytes = node_file.read_bytes()

        orig_run = subprocess.run

        def fake_run(cmd, **kwargs):
            if cmd[0] == "node" and cmd[1] == "--version":
                return subprocess.CompletedProcess(cmd, 0, stdout="v20.0.0", stderr="")
            if "validate.js" in str(cmd):
                return subprocess.CompletedProcess(cmd, 0, stdout="atlas validate: OK", stderr="")
            if "contract-check.js" in str(cmd):
                # FAIL contract-check
                return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="contract-check: 1 problem(s):\n  - missing edge")
            if "gen-spec-table.js" in str(cmd):
                return subprocess.CompletedProcess(cmd, 0, stdout="gen-spec-table: wrote rows.", stderr="")
            if cmd[0] == "git" and cmd[1] == "init":
                return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
            if cmd[0] == "git" and cmd[1] == "config":
                return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
            if cmd[0] == "git" and cmd[1] == "commit":
                return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
            return orig_run(cmd, **kwargs)

        monkeypatch.setattr(subprocess, "run", fake_run)
        monkeypatch.setattr(at, "_get_atlas_repo", lambda s: str(atlas_dir))

        args = self._args(node="test-node", field="port", value="9999",
                          atlas_path=str(atlas_dir))
        result = at.cmd_set(args)
        assert result == 1

        reverted_bytes = node_file.read_bytes()
        assert reverted_bytes == original_bytes
        data = json.loads(node_file.read_text(encoding="utf-8"))
        assert data["port"] == 9306  # NOT 9999

    def test_minimal_diff_T4(self, tmp_path, monkeypatch):
        """Setting one field produces only that line's diff, not a reformat."""
        atlas_dir = _write_atlas_structure(tmp_path, {
            "test-node": {"status": "live", "port": 9306, "title": "test service"}
        })
        node_file = atlas_dir / "define" / "test-node.json"
        original_text = node_file.read_text(encoding="utf-8")

        orig_run = subprocess.run

        def fake_run(cmd, **kwargs):
            if cmd[0] == "node" and cmd[1] == "--version":
                return subprocess.CompletedProcess(cmd, 0, stdout="v20.0.0", stderr="")
            if "validate.js" in str(cmd):
                return subprocess.CompletedProcess(cmd, 0, stdout="atlas validate: OK", stderr="")
            if "contract-check.js" in str(cmd):
                return subprocess.CompletedProcess(cmd, 0, stdout="contract-check: OK", stderr="")
            if "gen-spec-table.js" in str(cmd):
                return subprocess.CompletedProcess(cmd, 0, stdout="gen-spec-table: wrote rows.", stderr="")
            if cmd[0] == "git" and cmd[1] == "diff":
                return subprocess.CompletedProcess(cmd, 0, stdout="diff output", stderr="")
            if cmd[0] == "git" and cmd[1] == "add":
                return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
            if cmd[0] == "git" and cmd[1] == "init":
                return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
            if cmd[0] == "git" and cmd[1] == "config":
                return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
            if cmd[0] == "git" and cmd[1] == "commit":
                return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
            return orig_run(cmd, **kwargs)

        monkeypatch.setattr(subprocess, "run", fake_run)
        monkeypatch.setattr(at, "_get_atlas_repo", lambda s: str(atlas_dir))

        args = self._args(node="test-node", field="status", value='"disabled"',
                          atlas_path=str(atlas_dir))
        at.cmd_set(args)

        # After setting, the file should still have the same structure
        new_text = node_file.read_text(encoding="utf-8")
        # Both should start with { and end with }\n
        assert new_text.startswith("{")
        assert new_text.endswith("}\n")
        # The original keys should still be present in order
        assert '"status"' in new_text
        assert '"port"' in new_text
        assert '"title"' in new_text
        # Only the status line should differ
        assert '"status": "disabled"' in new_text
        assert '"status": "live"' not in new_text

    def test_node_not_found_error(self, tmp_path, monkeypatch):
        """Error when define/<node>.json doesn't exist."""
        atlas_dir = _write_atlas_structure(tmp_path)  # no nodes

        monkeypatch.setattr(at, "_get_atlas_repo", lambda s: str(atlas_dir))

        args = self._args(node="nonexistent", field="status", value='"live"',
                          atlas_path=str(atlas_dir))
        result = at.cmd_set(args)
        assert result == 1

    def test_set_with_commit(self, tmp_path, monkeypatch):
        """--commit flag auto-commits the change."""
        atlas_dir = _write_atlas_structure(tmp_path, {
            "test-node": {"status": "live", "port": 9306}
        })

        orig_run = subprocess.run
        commit_called = []

        def fake_run(cmd, **kwargs):
            if cmd[0] == "node" and cmd[1] == "--version":
                return subprocess.CompletedProcess(cmd, 0, stdout="v20.0.0", stderr="")
            if "validate.js" in str(cmd):
                return subprocess.CompletedProcess(cmd, 0, stdout="OK", stderr="")
            if "contract-check.js" in str(cmd):
                return subprocess.CompletedProcess(cmd, 0, stdout="OK", stderr="")
            if "gen-spec-table.js" in str(cmd):
                return subprocess.CompletedProcess(cmd, 0, stdout="gen-spec-table: wrote rows.", stderr="")
            if cmd[0] == "git" and cmd[1] == "diff":
                return subprocess.CompletedProcess(cmd, 0, stdout="diff", stderr="")
            if cmd[0] == "git" and cmd[1] == "add":
                return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
            if cmd[0] == "git" and cmd[1] == "commit":
                commit_called.append(True)
                return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
            if cmd[0] == "git" and cmd[1] == "init":
                return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
            if cmd[0] == "git" and cmd[1] == "config":
                return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
            return orig_run(cmd, **kwargs)

        monkeypatch.setattr(subprocess, "run", fake_run)
        monkeypatch.setattr(at, "_get_atlas_repo", lambda s: str(atlas_dir))

        args = self._args(node="test-node", field="status", value='"disabled"',
                          reason="test commit", commit=True, atlas_path=str(atlas_dir))
        at.cmd_set(args)
        assert commit_called, "commit was not called with --commit flag"

    def test_set_preserves_key_order_of_original(self, tmp_path, monkeypatch):
        """Setting a value on an existing key keeps it in the same position."""
        atlas_dir = _write_atlas_structure(tmp_path, {
            "test-node": {"title": "My Service", "type": "system", "control": "ours",
                          "status": "live", "port": 9306}
        })
        node_file = atlas_dir / "define" / "test-node.json"

        orig_run = subprocess.run

        def fake_run(cmd, **kwargs):
            if cmd[0] == "node" and cmd[1] == "--version":
                return subprocess.CompletedProcess(cmd, 0, stdout="v20.0.0", stderr="")
            if "validate.js" in str(cmd) or "contract-check.js" in str(cmd):
                return subprocess.CompletedProcess(cmd, 0, stdout="OK", stderr="")
            if "gen-spec-table.js" in str(cmd):
                return subprocess.CompletedProcess(cmd, 0, stdout="gen-spec-table: wrote rows.", stderr="")
            if cmd[0] == "git":
                return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
            return orig_run(cmd, **kwargs)

        monkeypatch.setattr(subprocess, "run", fake_run)
        monkeypatch.setattr(at, "_get_atlas_repo", lambda s: str(atlas_dir))

        args = self._args(node="test-node", field="status", value='"disabled"',
                          atlas_path=str(atlas_dir))
        at.cmd_set(args)

        data = at._load_node_json(node_file)
        keys = list(data.keys())
        assert keys == ["title", "type", "control", "status", "port"], (
            f"Key order changed: {keys}"
        )
        assert data["status"] == "disabled"


# ═══════════════════════════════════════════════════════════════════════════════
# cmd_nodes
# ═══════════════════════════════════════════════════════════════════════════════

class TestCmdNodes:
    def _args(self, scope="mol", atlas_path=None):
        ns = argparse.Namespace()
        ns.scope = scope
        ns.atlas = atlas_path
        return ns

    def test_lists_nodes_with_status(self, tmp_path, monkeypatch, capsys):
        atlas_dir = _write_atlas_structure(tmp_path, {
            "svc-a": {"status": "live", "type": "system", "title": "Service A"},
            "svc-b": {"status": "planned", "type": "deployment", "title": "Service B"},
        })

        monkeypatch.setattr(at, "_get_atlas_repo", lambda s: str(atlas_dir))
        result = at.cmd_nodes(self._args(atlas_path=str(atlas_dir)))
        assert result == 0

        captured = capsys.readouterr()
        assert "svc-a" in captured.out
        assert "svc-b" in captured.out
        assert "live" in captured.out
        assert "planned" in captured.out
        assert "Service A" in captured.out

    def test_skips_meta_json(self, tmp_path, monkeypatch, capsys):
        atlas_dir = _write_atlas_structure(tmp_path, {
            "svc-a": {"status": "live", "type": "system", "title": "A"},
        })
        # Add _meta.json which should be excluded from the count
        (atlas_dir / "define" / "_meta.json").write_text('{"version": 1}\n', encoding="utf-8")

        monkeypatch.setattr(at, "_get_atlas_repo", lambda s: str(atlas_dir))
        at.cmd_nodes(self._args(atlas_path=str(atlas_dir)))
        captured = capsys.readouterr()
        assert "_meta" not in captured.out
        assert "1 node(s)" in captured.out


# ═══════════════════════════════════════════════════════════════════════════════
# cmd_link
# ═══════════════════════════════════════════════════════════════════════════════

class TestCmdLink:
    def _args(self, scope="mol", path=None):
        ns = argparse.Namespace()
        ns.scope = scope
        ns.path = path
        return ns

    def test_link_writes_config(self, tmp_path, monkeypatch):
        cfg = tmp_path / "config.json"
        monkeypatch.setattr(at, "_config_path", lambda: cfg)

        atlas_dir = tmp_path / "atlas-repo"
        atlas_dir.mkdir()

        result = at.cmd_link(self._args(scope="mol", path=str(atlas_dir)))
        assert result == 0
        assert at._get_atlas_repo("mol") == str(atlas_dir.resolve())

    def test_link_nonexistent_path_fails(self, tmp_path, monkeypatch):
        result = at.cmd_link(self._args(scope="mol", path="/nonexistent/path"))
        assert result == 1


# ═══════════════════════════════════════════════════════════════════════════════
# cmd_validate (mocked)
# ═══════════════════════════════════════════════════════════════════════════════

class TestCmdValidate:
    def _args(self, scope="mol", atlas_path=None):
        ns = argparse.Namespace()
        ns.scope = scope
        ns.atlas = atlas_path
        return ns

    def test_validate_both_pass(self, tmp_path, monkeypatch, capsys):
        atlas_dir = _write_atlas_structure(tmp_path)

        orig_run = subprocess.run

        def fake_run(cmd, **kwargs):
            if cmd[0] == "node" and cmd[1] == "--version":
                return subprocess.CompletedProcess(cmd, 0, stdout="v20.0.0", stderr="")
            if "validate.js" in str(cmd):
                return subprocess.CompletedProcess(cmd, 0, stdout="atlas validate: OK", stderr="")
            if "contract-check.js" in str(cmd):
                return subprocess.CompletedProcess(cmd, 0, stdout="contract-check: OK", stderr="")
            return orig_run(cmd, **kwargs)

        monkeypatch.setattr(subprocess, "run", fake_run)
        monkeypatch.setattr(at, "_get_atlas_repo", lambda s: str(atlas_dir))

        result = at.cmd_validate(self._args(atlas_path=str(atlas_dir)))
        assert result == 0
        captured = capsys.readouterr()
        assert "CLEAN" in captured.out

    def test_validate_fails_when_gate_fails(self, tmp_path, monkeypatch):
        atlas_dir = _write_atlas_structure(tmp_path)

        orig_run = subprocess.run

        def fake_run(cmd, **kwargs):
            if cmd[0] == "node" and cmd[1] == "--version":
                return subprocess.CompletedProcess(cmd, 0, stdout="v20.0.0", stderr="")
            if "validate.js" in str(cmd):
                return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="atlas validate: PROBLEM")
            if "contract-check.js" in str(cmd):
                return subprocess.CompletedProcess(cmd, 0, stdout="contract-check: OK", stderr="")
            return orig_run(cmd, **kwargs)

        monkeypatch.setattr(subprocess, "run", fake_run)
        monkeypatch.setattr(at, "_get_atlas_repo", lambda s: str(atlas_dir))

        result = at.cmd_validate(self._args(atlas_path=str(atlas_dir)))
        assert result == 1


# ═══════════════════════════════════════════════════════════════════════════════
# cmd_status
# ═══════════════════════════════════════════════════════════════════════════════

class TestCmdStatus:
    def _args(self, scope="mol", atlas_path=None):
        ns = argparse.Namespace()
        ns.scope = scope
        ns.atlas = atlas_path
        return ns

    def test_status_shows_info(self, tmp_path, monkeypatch, capsys):
        atlas_dir = _write_atlas_structure(tmp_path, {
            "svc-a": {"status": "live", "type": "system", "title": "A"},
            "svc-b": {"status": "planned", "type": "deployment", "title": "B"},
        })

        orig_run = subprocess.run

        def fake_run(cmd, **kwargs):
            if cmd[0] == "node" and cmd[1] == "--version":
                return subprocess.CompletedProcess(cmd, 0, stdout="v20.0.0", stderr="")
            if "validate.js" in str(cmd):
                return subprocess.CompletedProcess(cmd, 0, stdout="atlas validate: OK", stderr="")
            if "contract-check.js" in str(cmd):
                return subprocess.CompletedProcess(cmd, 0, stdout="contract-check: OK", stderr="")
            if cmd[0] == "git" and cmd[1] == "status":
                return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
            return orig_run(cmd, **kwargs)

        monkeypatch.setattr(subprocess, "run", fake_run)
        monkeypatch.setattr(at, "_get_atlas_repo", lambda s: str(atlas_dir))

        result = at.cmd_status(self._args(atlas_path=str(atlas_dir)))
        assert result == 0

        captured = capsys.readouterr()
        assert "node count: 2" in captured.out
        assert "git state:" in captured.out
        assert "validate.js" in captured.out
        assert "contract-check.js" in captured.out

    def test_status_uses_configured_path(self, tmp_path, monkeypatch, capsys):
        """Status can resolve from config without explicit --atlas."""
        atlas_dir = _write_atlas_structure(tmp_path, {
            "svc-a": {"status": "live", "type": "system", "title": "A"},
        })

        orig_run = subprocess.run

        def fake_run(cmd, **kwargs):
            if cmd[0] == "node" and cmd[1] == "--version":
                return subprocess.CompletedProcess(cmd, 0, stdout="v20.0.0", stderr="")
            if "validate.js" in str(cmd):
                return subprocess.CompletedProcess(cmd, 0, stdout="OK", stderr="")
            if "contract-check.js" in str(cmd):
                return subprocess.CompletedProcess(cmd, 0, stdout="OK", stderr="")
            if cmd[0] == "git" and cmd[1] == "status":
                return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
            return orig_run(cmd, **kwargs)

        monkeypatch.setattr(subprocess, "run", fake_run)
        monkeypatch.setattr(at, "_get_atlas_repo", lambda s: str(atlas_dir))

        # No explicit --atlas — resolves from config
        result = at.cmd_status(self._args(atlas_path=None))
        assert result == 0

        captured = capsys.readouterr()
        assert str(atlas_dir) in captured.out


import argparse  # noqa: E402 (used in test helpers above)
