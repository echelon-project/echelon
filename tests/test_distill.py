"""Focused unit tests for echelon_engine.atoms.distill — raw captures -> COS atoms.

Isolation: a tmp-redirected SeedStore (both v1 + v2 in tmp_path) and a FAKE provider
(no network) whose canned reply drives the keep/drop branches. Covers the pure
helpers (_looks_worthless, _parse) and the end-to-end distill_raw flow: a worthless
capture is dropped pre-model, a substantive one becomes a coordinate-bearing atom and
the raw is marked consumed.
"""
import pytest

from echelon_engine.atoms.store import SeedStore
from echelon_engine.atoms import distill


@pytest.fixture
def store(tmp_path):
    return SeedStore(tmp_path / "core.db", v2_db=tmp_path / "core_v2.db")


class _Resp:
    def __init__(self, content):
        self.content = content


class _FakeProvider:
    """Returns the same canned JSON for every send() — enough to drive distill_raw."""
    def __init__(self, content):
        self._content = content
        self.calls = 0

    def send(self, messages, model_id=None, tools=None):
        self.calls += 1
        return _Resp(self._content)


# ── pure helpers ──────────────────────────────────────────────────────────
def test_looks_worthless_short_and_acks():
    assert distill._looks_worthless("[ECHELON] Edit: edited ok")
    assert distill._looks_worthless("[x] Bash: (no output)")
    assert distill._looks_worthless("tiny")  # under 8 chars of body


def test_looks_worthless_keeps_substantive():
    assert not distill._looks_worthless(
        "[ECHELON] Bash: always pass -X utf8 to avoid the cp1252 arrow crash")


def test_parse_extracts_object_from_prose():
    assert distill._parse('sure: {"keep": true, "atom": "a"}') == {"keep": True, "atom": "a"}
    assert distill._parse("no object") is None
    assert distill._parse("") is None


# ── end-to-end distill_raw ────────────────────────────────────────────────
def test_distill_keeps_substantive_atom(store):
    store.remember("proj-raw", "[ECHELON] Bash: on windows use $env:VAR not export VAR",
                   kind="raw", tier="working")
    prov = _FakeProvider('{"keep": true, "atom": "use $env:VAR on windows", '
                         '"coordinate": "tooling:shell:windows:env"}')
    rep = distill.distill_raw(store, "proj-raw", "proj-atoms", prov, "fake-model")
    assert rep["atoms"] == 1
    assert rep["made"][0]["coordinate"] == "tooling:shell:windows:env"
    # the atom landed in the atom scope
    atoms = [s for s in store.seeds(scope="proj-atoms") if s.kind == "atom"]
    assert len(atoms) == 1
    # the carried charge: the atom inherits the raw's valence/arousal (both 0.0 here)
    assert atoms[0].valence == 0.0


def test_distill_mark_consumed_retires_a_v2born_raw(store):
    """REGRESSION for a CODE BUG found + FIXED in migration: store.mark_kind used to update
    ONLY the v1 UAME table via self._row. Under frozen-v1 a new 'raw' is born v2-ONLY, so
    _row returned None -> mark_kind no-op'd -> distill._mark_consumed could never retire a
    consumed raw -> distill_raw re-distilled the SAME raw every pass (lost idempotency,
    repeated model spend). The fix makes mark_kind ALSO flip the v2 atom (matched by
    born_from=seed_id). This test now asserts the FIXED behavior: a consumed raw is retired.
    """
    store.remember("proj-raw", "[ECHELON] Bash: on windows use $env:VAR not export VAR",
                   kind="raw", tier="working")
    prov = _FakeProvider('{"keep": true, "atom": "use $env:VAR", "coordinate": "a:b:c"}')
    distill.distill_raw(store, "proj-raw", "proj-atoms2", prov, "fake-model")
    # FIXED: mark_kind reached the v2 row, so the raw was retired (kind flipped off 'raw')
    raws = [s for s in store.seeds(scope="proj-raw") if s.kind == "raw"]
    assert len(raws) == 0


def test_distill_drops_worthless_without_calling_model(store):
    store.remember("proj-raw", "[ECHELON] Edit: edited ok", kind="raw", tier="working")
    prov = _FakeProvider('{"keep": true, "atom": "x", "coordinate": "a:b"}')
    rep = distill.distill_raw(store, "proj-raw", "proj-atoms", prov, "fake-model")
    assert rep["dropped"] == 1 and rep["atoms"] == 0
    assert prov.calls == 0  # dropped pre-model


def test_distill_drops_on_keep_false(store):
    store.remember("proj-raw", "[ECHELON] Bash: ran the full suite once, all green here",
                   kind="raw", tier="working")
    prov = _FakeProvider('{"keep": false}')
    rep = distill.distill_raw(store, "proj-raw", "proj-atoms", prov, "fake-model")
    assert rep["dropped"] == 1 and rep["atoms"] == 0
    assert prov.calls == 1
