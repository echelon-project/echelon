"""Regression tests for the LOUD FLOOR (smart-recall slice 0) — a 401/refusal from the local embedding
floor must NEVER pass silently. The silent-401 month happened because `except Exception: return None`
swallowed the auth failure; the semantic tier degraded to lexical with no signal. These tests pin the
banner so that bug cannot recur.

Scope discipline (slice 0): this touches ONLY the degrade path. warmth.py / verdict logic are untouched,
so nothing here imports or exercises them.
"""
import io
import urllib.error

import pytest

from echelon_sdk import floor_degrade as fd


# ── the classifier splits auth-refusal from unreachable (the diagnostic core) ──
def test_401_classifies_as_auth_refused():
    exc = urllib.error.HTTPError(url="http://f/v1/embeddings", code=401, msg="Unauthorized",
                                 hdrs=None, fp=None)
    cause, detail = fd.classify_floor_error(exc)
    assert cause == "auth-refused"
    assert "401" in detail


def test_403_also_auth_refused():
    exc = urllib.error.HTTPError(url="http://f", code=403, msg="Forbidden", hdrs=None, fp=None)
    assert fd.classify_floor_error(exc)[0] == "auth-refused"


def test_400_is_http_error_not_auth():
    exc = urllib.error.HTTPError(url="http://f", code=400, msg="no model", hdrs=None, fp=None)
    assert fd.classify_floor_error(exc)[0] == "http-error"


def test_connection_refused_is_unreachable():
    exc = urllib.error.URLError(ConnectionRefusedError("refused"))
    assert fd.classify_floor_error(exc)[0] == "unreachable"


def test_bad_shape_when_response_wrong():
    # reached the floor, got JSON, but data[0].embedding missing -> KeyError/IndexError -> bad-shape
    assert fd.classify_floor_error(KeyError("data"))[0] == "bad-shape"
    assert fd.classify_floor_error(IndexError())[0] == "bad-shape"


# ── the banner FIRES, names the organ, and is not silent ───────────────────────
def test_401_prints_a_banner_naming_the_organ():
    fd.reset_announced()
    buf = io.StringIO()
    exc = urllib.error.HTTPError(url="http://floor/v1/embeddings", code=401, msg="Unauthorized",
                                 hdrs=None, fp=None)
    cause, detail = fd.announce_degrade("floor-embedder (http://floor/v1/embeddings)", exc, stream=buf)
    out = buf.getvalue()
    assert out != ""                              # NOT silent — the whole point of slice 0
    assert "DEGRADED" in out
    assert "floor-embedder" in out                # names the failing organ
    assert "auth-refused" in out                  # names the cause
    assert cause == "auth-refused"


def test_banner_announces_once_per_organ_cause():
    fd.reset_announced()
    buf = io.StringIO()
    exc = urllib.error.HTTPError(url="http://f", code=401, msg="x", hdrs=None, fp=None)
    fd.announce_degrade("floor-embedder (http://f)", exc, stream=buf)
    first = buf.getvalue()
    fd.announce_degrade("floor-embedder (http://f)", exc, stream=buf)   # suppressed
    assert buf.getvalue() == first                # no second banner for the same organ+cause


def test_different_cause_still_announces():
    fd.reset_announced()
    buf = io.StringIO()
    auth = urllib.error.HTTPError(url="http://f", code=401, msg="x", hdrs=None, fp=None)
    down = urllib.error.URLError(ConnectionRefusedError("refused"))
    fd.announce_degrade("floor-embedder (http://f)", auth, stream=buf)
    fd.announce_degrade("floor-embedder (http://f)", down, stream=buf)
    txt = buf.getvalue()
    assert "auth-refused" in txt and "unreachable" in txt   # both distinct causes surfaced


# ── integration: embed_text must announce, not swallow, a 401 ──────────────────
def test_embed_text_announces_401(monkeypatch, capsys):
    """With the floor key revoked (or the floor 401ing), embed_text still returns None (degrade) BUT
    prints the banner first. This is the exact scenario of the silent-401 month, now made loud."""
    fd.reset_announced()
    from echelon_engine.atoms import bank_embed as be

    def fake_urlopen(req, timeout=None):
        raise urllib.error.HTTPError(url=req.full_url, code=401, msg="Unauthorized", hdrs=None, fp=None)

    monkeypatch.setattr(be.urllib.request, "urlopen", fake_urlopen)
    out = be.embed_text("anything", url="http://floor-under-test/v1/embeddings")
    assert out is None                             # still degrades (fallback preserved)
    err = capsys.readouterr().err
    assert "DEGRADED" in err and "auth-refused" in err   # but LOUDLY — the regression guard
    assert "floor-under-test" in err               # the organ URL is named


def test_floor_embed_announces_401(monkeypatch, capsys):
    """The SDK-layer floor call (minilm_embed._floor_embed) shares the same loud-floor guard.
    _floor_embed does a function-local `import urllib.request`, which resolves to the SAME stdlib
    module object we patch here — so patching urllib.request.urlopen at the source covers it."""
    fd.reset_announced()
    import urllib.request
    from echelon_sdk import minilm_embed as me

    def fake_urlopen(req, timeout=None):
        raise urllib.error.HTTPError(url=req.full_url, code=401, msg="Unauthorized", hdrs=None, fp=None)

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    out = me._floor_embed("anything")
    assert out is None
    err = capsys.readouterr().err
    assert "DEGRADED" in err and "auth-refused" in err
