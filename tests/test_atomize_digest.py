"""Focused unit tests for echelon_engine.atoms.atomize_digest.

MINIMAL-SEAM MODULE: atomize_digest is a T2 pipeline script whose real work is a
network call to a local LM Studio floor (call/digest_one/main). The only pure,
deterministic, side-effect-free surface worth pinning is `parse()` — the tolerant
JSON extractor that absorbs gemma's known malformations (think-tags, code fences,
the "omitted final }" array-only case). So these are SMOKE tests over parse() plus
a sanity import check; the LLM/subprocess paths are intentionally NOT exercised
(no network in CI). The module-level `load_lmstudio_key()` side effect is covered
by the bare import succeeding.
"""
from echelon_engine.atoms import atomize_digest as ad


def test_parse_clean_object():
    raw = '{"lessons": [{"slug": "a", "gist": "g", "facts": ["f"], "links": []}]}'
    o = ad.parse(raw)
    assert o is not None
    assert o["lessons"][0]["slug"] == "a"


def test_parse_strips_think_and_fence():
    raw = ('<think>reasoning here</think>\n```json\n'
           '{"lessons": [{"slug": "b", "gist": "g", "facts": [], "links": []}]}\n```')
    o = ad.parse(raw)
    assert o is not None and o["lessons"][0]["slug"] == "b"


def test_parse_recovers_unclosed_wrapper():
    # gemma's common defect: the array `]` is present but the wrapper's final `}` is dropped.
    raw = '{"lessons": [{"slug": "c", "gist": "g", "facts": ["x"], "links": []}]'
    o = ad.parse(raw)
    assert o is not None
    assert o["lessons"][0]["slug"] == "c"


def test_parse_returns_none_on_garbage():
    assert ad.parse("no json at all here") is None
    assert ad.parse("") is None


def test_repair_and_digest_prompts_present():
    # the prompts are load-bearing string templates with %s slots
    assert "%s" in ad.DIGEST_PROMPT and "%s" in ad.REPAIR_PROMPT
    assert ad.MODEL and ad.LMS.startswith("http")
