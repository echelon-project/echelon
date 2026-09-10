"""tests/test_bench_suite.py -- the PAPER-0029 bench scaffold is RUNNABLE today.

The EOS-era failure mode under test: a protocol designed and never run. So the
suite proves the plumbing END TO END: both schemas are valid JSON Schema, every
arc manifest validates (parametrized over bench/arcs/*/), a malformed manifest
is rejected loudly, a --dry-run arc pass emits a schema-valid ledger for every
arm, and the planted trap atoms really exist in the bank (skipped gracefully
when no bank is around).
"""
from __future__ import annotations

import copy
import importlib.util
import json
import os
import subprocess
import sys

import pytest

jsonschema = pytest.importorskip("jsonschema")

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BENCH = os.path.join(REPO, "bench")

# bench/ is deliberately not a package; load the runner by path.
_spec = importlib.util.spec_from_file_location("bench_runner",
                                               os.path.join(BENCH, "runner.py"))
runner = importlib.util.module_from_spec(_spec)
sys.modules["bench_runner"] = runner   # dataclasses resolve types via sys.modules
_spec.loader.exec_module(runner)


def _arc_manifests():
    """Every arc manifest under bench/arcs/ — the suite covers all of them."""
    arcs_dir = os.path.join(BENCH, "arcs")
    return sorted(
        os.path.join(arcs_dir, d, "manifest.json")
        for d in os.listdir(arcs_dir)
        if os.path.isdir(os.path.join(arcs_dir, d))
    )


MANIFESTS = _arc_manifests()
PILOT_MANIFEST = next(p for p in MANIFESTS if "pilot-01" in p)


def _arc_id(path):
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)["arc_id"]


def _load(path):
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


# -- schemas ---------------------------------------------------------------

def test_schemas_are_valid_jsonschema():
    for path in (runner.MANIFEST_SCHEMA, runner.LEDGER_SCHEMA):
        jsonschema.Draft202012Validator.check_schema(_load(path))


@pytest.mark.parametrize("manifest_path", MANIFESTS, ids=_arc_id)
def test_manifest_validates(manifest_path):
    manifest = runner.load_manifest(manifest_path)
    assert len(manifest["traps"]) >= 3
    assert manifest["turn_budget"] >= 20
    for task in manifest["tasks"]:
        assert len(task["paraphrases"]) == 2      # P4 needs both variants


@pytest.mark.parametrize("manifest_path", MANIFESTS, ids=_arc_id)
def test_arc_fixture_files_exist(manifest_path):
    """Every path the manifest points at is real on disk -- no stub arcs."""
    manifest = runner.load_manifest(manifest_path)
    fixture = os.path.join(manifest["_dir"], manifest["fixture_repo"])
    assert os.path.isdir(fixture)
    for trap in manifest["traps"]:
        for f in trap["evidence_files"]:
            assert os.path.isfile(os.path.join(fixture, f)), f"missing evidence: {f}"


def test_malformed_manifest_rejected(tmp_path):
    manifest = _load(PILOT_MANIFEST)
    for mutilation in (
        lambda m: m.pop("traps"),                              # required key gone
        lambda m: m.__setitem__("turn_budget", 5),             # under the 20-turn law
        lambda m: m["traps"].__delitem__(2),                   # fewer than 3 traps
        lambda m: m["tasks"][0]["paraphrases"].pop(),          # P4 variant missing
        lambda m: m["traps"][0].pop("atom_slug"),              # unmapped trap
    ):
        bad = copy.deepcopy(manifest)
        mutilation(bad)
        path = tmp_path / "bad.json"
        path.write_text(json.dumps(bad), encoding="utf-8")
        with pytest.raises(runner.ManifestError):
            runner.load_manifest(str(path))


# -- dry run ---------------------------------------------------------------

@pytest.mark.parametrize("manifest_path", MANIFESTS, ids=_arc_id)
@pytest.mark.parametrize("arm", ["A", "B", "Bprime"])
def test_dry_run_emits_schema_valid_ledger(arm, manifest_path, tmp_path):
    manifest = runner.load_manifest(manifest_path)
    path = runner.run_arc(manifest, arm=arm, muscle=runner.MockMuscle(),
                          governor=runner.MockGovernor(), out_dir=str(tmp_path),
                          model="mock", dry_run=True)
    ledger = _load(path)
    runner.validate_ledger(ledger)     # belt: run_arc validated pre-write too
    assert ledger["arm"] == arm
    assert ledger["dry_run"] is True
    assert len(ledger["rows"]) == len(manifest["tasks"])
    for row in ledger["rows"]:
        src = row["context_source"]
        if arm == "A":
            assert src["kind"] == "recall"
        else:
            assert src["kind"] == "governor"
            assert os.path.isfile(src["governor_ledger"])


@pytest.mark.parametrize("manifest_path", MANIFESTS, ids=_arc_id)
def test_dry_run_paraphrase_variant_recorded(manifest_path, tmp_path):
    manifest = runner.load_manifest(manifest_path)
    path = runner.run_arc(manifest, arm="A", muscle=runner.MockMuscle(),
                          governor=None, out_dir=str(tmp_path), model="mock",
                          paraphrase=1, dry_run=True)
    assert _load(path)["paraphrase_variant"] == 1


def test_b_arm_without_governor_rejected(tmp_path):
    manifest = runner.load_manifest(PILOT_MANIFEST)
    with pytest.raises(ValueError):
        runner.run_arc(manifest, arm="B", muscle=runner.MockMuscle(),
                       governor=None, out_dir=str(tmp_path), model="mock")


# -- the traps map to REAL banked atoms ------------------------------------

def _bank_path():
    return os.path.expanduser(os.path.join("~", ".echelon", "echelon.db"))


@pytest.mark.skipif(not os.path.exists(_bank_path()),
                    reason="no local bank at ~/.echelon/echelon.db")
@pytest.mark.parametrize("manifest_path", MANIFESTS, ids=_arc_id)
def test_trap_atom_slugs_exist_in_bank(manifest_path):
    """Each planted trap maps to an atom ALREADY banked in the scope (the A-arm
    can only win by thinking to query it). Verified through the recall door,
    read-only: first by name-warmth (recall --warm surfaces the slug), falling
    back to the scope-wide seed listing (recall --list) — some lesson atoms
    banked under a date suffix do not surface on their own name query, but are
    always present in the scope listing. Skips rather than fails when the
    engine/bank is unreachable."""
    manifest = runner.load_manifest(manifest_path)
    scope = manifest.get("scope", "echelon")
    for trap in manifest["traps"]:
        slug = trap["atom_slug"]
        proc = subprocess.run(
            [sys.executable, "-X", "utf8", "-m", "echelon_engine", "recall",
             "--scope", scope, "--warm", slug.replace("-", " ")],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            cwd=REPO, timeout=180)
        if proc.returncode != 0:
            pytest.skip(f"recall door unavailable (exit {proc.returncode})")
        if slug in proc.stdout:
            continue
        # fallback: the scope-wide listing must still contain the slug
        listing = subprocess.run(
            [sys.executable, "-X", "utf8", "-m", "echelon_engine", "recall",
             "--scope", scope, "--list"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            cwd=REPO, timeout=180)
        if listing.returncode != 0:
            pytest.skip(f"recall listing unavailable (exit {listing.returncode})")
        assert slug in listing.stdout, f"banked atom not surfaced: {slug}"
