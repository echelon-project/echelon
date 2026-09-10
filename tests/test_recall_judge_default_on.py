"""Judge default-on resolution (owner OPEN, 2026-08-31): a plain `recall --warm` with no
--judge used to mean 'skip the judge entirely', silently running on the lexical floor.
These tests pin the new resolution law:

    ECHELON_RECALL_JUDGE=off  -> lexical floor, honest label, no attempt
    ECHELON_RECALL_JUDGE=<x>  -> explicit override candidate
    no env, deepseek key present -> "deepseek" is the first candidate
    no env, no deepseek key       -> _DEFAULT_JUDGE (minimax) is the first candidate
    explicit --judge (a value != None) is UNCHANGED — bypasses default resolution entirely

Build failures (missing key, import error, throttle) must fall through to the next
candidate and ultimately to the lexical floor — default-on must NEVER make recall crash
where it used to work. No test spends a real token: provider builds are stubbed.
"""
import io
import contextlib

import pytest

from echelon_engine.atoms import recall as R
from echelon_engine.atoms.store import SeedStore


@pytest.fixture
def store(tmp_path):
    s = SeedStore(tmp_path / "core.db", v2_db=tmp_path / "core_v2.db")
    s.remember("probe-scope", "the money actually moved on this deal", coordinate="deal:moved")
    return s


# ── _resolve_default_judge: pure candidate-selection logic ────────────────

def test_env_off_returns_none(monkeypatch):
    monkeypatch.setenv("ECHELON_RECALL_JUDGE", "off")
    assert R._resolve_default_judge() is None


def test_env_explicit_override(monkeypatch):
    monkeypatch.setenv("ECHELON_RECALL_JUDGE", "grok")
    assert R._resolve_default_judge() == "grok"


def test_no_env_deepseek_keyed_wins(monkeypatch):
    monkeypatch.delenv("ECHELON_RECALL_JUDGE", raising=False)
    monkeypatch.setattr(R, "_deepseek_key_configured", lambda: True)
    assert R._resolve_default_judge() == "deepseek"


def test_no_env_no_deepseek_key_falls_to_default_judge(monkeypatch):
    monkeypatch.delenv("ECHELON_RECALL_JUDGE", raising=False)
    monkeypatch.setattr(R, "_deepseek_key_configured", lambda: False)
    assert R._resolve_default_judge() == R._DEFAULT_JUDGE


def test_deepseek_key_probe_never_raises(monkeypatch):
    """A missing/malformed key raises ValueError inside load_deepseek_key — the probe
    must swallow it, not propagate."""
    def _boom():
        raise ValueError("DeepSeek key not found")
    monkeypatch.setattr(
        "echelon_sdk.keys.load_deepseek_key", _boom, raising=False)
    assert R._deepseek_key_configured() is False


# ── warm_probe: end-to-end default-on behavior, no keys/providers stubbed ─

def test_default_on_no_providers_available_falls_to_lexical_no_crash(store, monkeypatch):
    """No keys configured anywhere, every provider build fails -> must still print the
    honest lexical tier and must NOT crash. This is the 'nothing is wired' floor case
    default-on must degrade to gracefully."""
    monkeypatch.delenv("ECHELON_RECALL_JUDGE", raising=False)
    monkeypatch.setattr(R, "_deepseek_key_configured", lambda: False)

    def _build_fail(spec):
        raise ValueError(f"no key for {spec}")
    monkeypatch.setattr(R, "_build_judge", _build_fail)

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        R.warm_probe("probe-scope", "the money actually moved on this deal",
                      db_path=None, judge=None, bridge=False, earn=False)
    out = buf.getvalue()
    assert "tier lexical" in out
    assert "judge     :" not in out  # no default-judge line when nothing could build


def test_env_off_produces_lexical_floor_honest_label(store, monkeypatch):
    monkeypatch.setenv("ECHELON_RECALL_JUDGE", "off")
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        R.warm_probe("probe-scope", "the money actually moved on this deal",
                      db_path=None, judge=None, bridge=False, earn=False)
    out = buf.getvalue()
    assert "tier lexical" in out
    assert "judge     :" not in out


def test_explicit_judge_off_string_is_lexical_floor(store, monkeypatch):
    """judge='off' passed straight through (as the CLI does for `--judge off`) must be
    treated the same as the env opt-out — no build attempt at all."""
    buf = io.StringIO()
    calls = []
    monkeypatch.setattr(R, "_build_judge", lambda spec: calls.append(spec) or (None, "x"))
    with contextlib.redirect_stdout(buf):
        R.warm_probe("probe-scope", "the money actually moved on this deal",
                      db_path=None, judge="off", bridge=False, earn=False)
    assert calls == []
    assert "tier lexical" in buf.getvalue()


def test_default_resolution_picks_deepseek_when_key_present(store, monkeypatch):
    """Stub the provider build so no real token is spent — pins that the DEEPSEEK
    candidate is attempted first and its name is reported in the honest judge line."""
    monkeypatch.delenv("ECHELON_RECALL_JUDGE", raising=False)
    monkeypatch.setattr(R, "_deepseek_key_configured", lambda: True)

    built = []

    class _StubProvider:
        def judge(self, *a, **kw):
            return None

    def _stub_build(spec):
        built.append(spec)
        if spec == "deepseek":
            return _StubProvider(), "deepseek-v4-flash"
        raise ValueError("should not reach this candidate")

    monkeypatch.setattr(R, "_build_judge", _stub_build)

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        R.warm_probe("probe-scope", "the money actually moved on this deal",
                      db_path=None, judge=None, bridge=False, earn=False)
    out = buf.getvalue()
    assert built[0] == "deepseek"
    assert "judge     : deepseek (default-on; ECHELON_RECALL_JUDGE=off to disable)" in out


def test_deepseek_build_failure_falls_through_to_default_judge_seat(store, monkeypatch):
    """Key LOOKS configured but the build still fails (bad key/import error) -> must fall
    through to the free _DEFAULT_JUDGE seat, never crash."""
    monkeypatch.delenv("ECHELON_RECALL_JUDGE", raising=False)
    monkeypatch.setattr(R, "_deepseek_key_configured", lambda: True)

    built = []

    class _StubProvider:
        def judge(self, *a, **kw):
            return None

    def _stub_build(spec):
        built.append(spec)
        if spec == "deepseek":
            raise RuntimeError("build blew up")
        return _StubProvider(), "stub-model"

    monkeypatch.setattr(R, "_build_judge", _stub_build)

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        R.warm_probe("probe-scope", "the money actually moved on this deal",
                      db_path=None, judge=None, bridge=False, earn=False)
    assert built == ["deepseek", R._DEFAULT_JUDGE]
    assert f"judge     : {R._DEFAULT_JUDGE} (default-on;" in buf.getvalue()


def test_explicit_judge_arg_bypasses_default_resolution_entirely(store, monkeypatch):
    """--judge <x> is UNCHANGED behavior: _resolve_default_judge must not even be called."""
    calls = {"n": 0}
    orig = R._resolve_default_judge

    def _spy():
        calls["n"] += 1
        return orig()
    monkeypatch.setattr(R, "_resolve_default_judge", _spy)

    class _StubProvider:
        def judge(self, *a, **kw):
            return None
    monkeypatch.setattr(R, "_build_judge", lambda spec: (_StubProvider(), "explicit-model"))

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        R.warm_probe("probe-scope", "the money actually moved on this deal",
                      db_path=None, judge="grok", bridge=False, earn=False)
    assert calls["n"] == 0
    # explicit path prints no "default-on" judge line (that's default-resolution-only)
    assert "default-on" not in buf.getvalue()
