"""Contract test for the frozen `echelon-bank-core` 1.0.0 package (R-0171 B).

FREEZE has teeth only if a change to the frozen set or its re-exported
interface turns this test RED. Three independent checks, each mirroring one
part of the freeze contract recorded in
`packages/echelon-bank-core/interface.json` and
`packages/echelon-bank-core/frozen.json`:

(a) every name recorded in interface.json is reachable as
    `getattr(echelon_bank_core.<module>, name)`, and for functions/classes
    `str(inspect.signature(obj))` equals the recorded signature string —
    catches a renamed/removed export or a changed call signature.

(b) every frozen source file recorded in frozen.json still hashes (sha256) to
    its recorded value — catches an edit to a frozen file that was not
    accompanied by a version bump + regenerated manifests ("a change reopens
    the row").

(c) for each module, the *recomputed* set of public top-level names (same
    ast-based rule gen_interface.py's design assumes: top-level def/class not
    starting with `_`, plus UPPER_CASE module-level assigned constants)
    exactly equals interface.json's recorded key set for that module —
    catches silent widening (a new public name appears but isn't frozen) or
    narrowing (a frozen name quietly disappears) without touching (a).

This requires `echelon_bank_core` to be importable — installed editable into
this venv via `uv pip install -e packages/echelon-bank-core` (or
`pip install -e packages/echelon-bank-core`).
"""

from __future__ import annotations

import ast
import hashlib
import inspect
import json
import pathlib

import pytest

echelon_bank_core = pytest.importorskip("echelon_bank_core")

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
PACKAGE_ROOT = REPO_ROOT / "packages" / "echelon-bank-core"
INTERFACE_PATH = PACKAGE_ROOT / "interface.json"
FROZEN_PATH = PACKAGE_ROOT / "frozen.json"


def _load_interface() -> dict:
    with open(INTERFACE_PATH, encoding="utf-8") as fh:
        return json.load(fh)


def _load_frozen() -> dict:
    with open(FROZEN_PATH, encoding="utf-8") as fh:
        return json.load(fh)


def _public_names(source: str) -> set:
    """Recompute a module's public top-level names via the same ast rule
    gen_interface.py's design assumes: top-level def/class not starting with
    `_`, plus UPPER_CASE module-level assigned constants.
    """
    tree = ast.parse(source)
    names = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if not node.name.startswith("_"):
                names.add(node.name)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if (
                    isinstance(target, ast.Name)
                    and target.id.isupper()
                    and not target.id.startswith("_")
                ):
                    names.add(target.id)
        elif isinstance(node, ast.AnnAssign):
            if (
                isinstance(node.target, ast.Name)
                and node.target.id.isupper()
                and not node.target.id.startswith("_")
            ):
                names.add(node.target.id)
    return names


INTERFACE = _load_interface()
FROZEN = _load_frozen()
FROZEN_MODULE_PATHS = sorted(k for k in FROZEN if k.endswith(".py"))


class TestInterfaceReachableAndSignaturesMatch:
    """Check (a): every recorded name is reachable with the recorded
    kind/signature."""

    @pytest.mark.parametrize("module_name", sorted(INTERFACE))
    def test_module_names_reachable_with_matching_signature(self, module_name):
        submodule = getattr(echelon_bank_core, module_name)
        for name, meta in INTERFACE[module_name].items():
            assert hasattr(submodule, name), (
                f"{module_name}.{name} is recorded in interface.json but not "
                f"reachable on echelon_bank_core.{module_name}"
            )
            obj = getattr(submodule, name)
            if meta["kind"] in ("function", "class"):
                actual_sig = str(inspect.signature(obj))
                assert actual_sig == meta["signature"], (
                    f"{module_name}.{name} signature changed: "
                    f"recorded={meta['signature']!r} actual={actual_sig!r}"
                )


class TestFrozenSourceHashesUnchanged:
    """Check (b): every frozen source file still hashes to its recorded
    sha256 — a change without a version bump + regen is RED."""

    @pytest.mark.parametrize("rel_path", FROZEN_MODULE_PATHS)
    def test_frozen_file_hash_unchanged(self, rel_path):
        recorded = FROZEN[rel_path]
        # LF-normalised, same as gen_interface.frozen_hash: the tree mixes CRLF/LF
        # per checkout; a raw-byte hash would lock the checkout, not the content.
        raw = (REPO_ROOT / rel_path).read_bytes().replace(b"\r\n", b"\n")
        actual = hashlib.sha256(raw).hexdigest()
        assert actual == recorded, (
            f"{rel_path} source hash changed (recorded={recorded}, actual={actual}) "
            "without a version bump + regenerated interface.json/frozen.json — "
            "a change to a frozen file must reopen the row."
        )

    def test_frozen_manifest_has_exactly_twenty_modules(self):
        assert len(FROZEN_MODULE_PATHS) == 20


class TestNoSilentWideningOrNarrowing:
    """Check (c): the module's real public name set equals interface.json's
    recorded key set — exactly, in both directions."""

    @pytest.mark.parametrize("rel_path", FROZEN_MODULE_PATHS)
    def test_public_names_match_interface_exactly(self, rel_path):
        module_name = pathlib.Path(rel_path).stem
        source = (REPO_ROOT / rel_path).read_text(encoding="utf-8")
        actual_names = _public_names(source)
        recorded_names = set(INTERFACE[module_name])
        assert actual_names == recorded_names, (
            f"{module_name} public name set drifted from interface.json: "
            f"added={actual_names - recorded_names or None} "
            f"removed={recorded_names - actual_names or None}"
        )
