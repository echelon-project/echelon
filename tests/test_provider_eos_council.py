"""Focused unit tests for echelon_engine.atoms.providers.eos_council (EosCouncilProvider).

This provider is almost ENTIRELY network I/O over the eos reverse tunnel (every real
method — detect_model, send, is_online, _collect — POSTs/GETs the eos_host). There is no
pure parsing/shaping helper factored out, so this is a deliberately MINIMAL construct +
config + inherited-seam smoke test (no fake network coverage). Reported as such.
"""
from __future__ import annotations

from echelon_engine.atoms.providers.eos_council import (
    EosCouncilProvider,
    DEFAULT_HOST,
    DEFAULT_VLLM,
    COUNCIL_MODEL,
)


def test_name():
    assert EosCouncilProvider().name == "eos_council"


def test_defaults_wired():
    p = EosCouncilProvider()
    assert p.host == DEFAULT_HOST
    assert p.vllm_url == DEFAULT_VLLM
    assert p.model is None                  # discover-on-first-use
    assert p._detected_model is None
    assert p._wait_supported is None        # lazily probed


def test_host_trailing_slash_stripped():
    p = EosCouncilProvider(host="http://127.0.0.1:8888/")
    assert p.host == "http://127.0.0.1:8888"


def test_explicit_model_pins_detection():
    # an explicit model pre-seeds _detected_model so detect_model() returns it without a probe.
    p = EosCouncilProvider(model="qwen3-32b-burst")
    assert p.model == "qwen3-32b-burst"
    assert p._detected_model == "qwen3-32b-burst"
    assert p.detect_model() == "qwen3-32b-burst"   # no network: cached value returned


def test_inherited_token_counting():
    # the shared ProviderBase pre-flight seam works on this provider too (no network).
    p = EosCouncilProvider()
    assert p.count_tokens("some council text") > 0
    assert p.count_messages([{"role": "user", "content": "hi"}]) > 0


def test_council_model_constant():
    assert isinstance(COUNCIL_MODEL, str) and COUNCIL_MODEL
