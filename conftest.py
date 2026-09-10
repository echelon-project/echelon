"""Test-suite guards for the public export.

The ECHELON substrate has NO required third-party dependencies: sqlite3, urllib
and ast do the work, so `pip install echelon` gives you a working bank without
pulling a web stack. A handful of tests cover the OPTIONAL surfaces (the API
proxy, the MCP HTTP server, the Codex bridge, the embedders) and those genuinely
need their extra installed.

Rather than make every contributor install every extra, those modules skip
cleanly when their dependency is absent. Install the relevant extra to run them:

    pip install -e ".[server]"    # fastapi / uvicorn / starlette surfaces
    pip install -e ".[embed]"     # sentence-transformers / torch
    pip install -e ".[mine]"      # numpy
    pip install -e ".[gates]"     # playwright
    pip install -e ".[steering]"  # PyYAML
"""
import importlib.util

import pytest

#: test module (by filename stem) -> the third-party module it requires
_OPTIONAL = {
    "test_codex_bridge_translation": "fastapi",
    "test_web_status_card": "fastapi",
    "test_bank_embed": "sentence_transformers",
    "test_cross_scope_miner": "numpy",
    "test_add_steering": "yaml",
}


def _missing(mod: str) -> bool:
    try:
        return importlib.util.find_spec(mod) is None
    except (ImportError, ValueError):
        return True


def pytest_collection_modifyitems(config, items):
    for item in items:
        need = _OPTIONAL.get(item.path.stem if hasattr(item, "path") else "")
        if need and _missing(need):
            item.add_marker(pytest.mark.skip(
                reason=f"optional dependency {need!r} not installed "
                       f"(install the matching extra to run this module)"))


def pytest_ignore_collect(collection_path, config):
    """Import-time deps must be handled BEFORE collection, not by a marker."""
    need = _OPTIONAL.get(collection_path.stem)
    return bool(need and _missing(need))
