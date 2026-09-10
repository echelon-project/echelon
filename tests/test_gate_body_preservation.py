"""Body-preservation tests for echelon_engine.atoms.gate — the write_into() function
must replace the banner while preserving the body below the `---` byte-for-byte.

Covers:
  - write_into with full gate (gate_text)
  - write_into with lean gate (gate_text_lean)
  - Body preservation after repeated rewrites (idempotency)
  - New file creation
  - File with no banner (no `---` rule)
"""
import pytest
from pathlib import Path

from echelon_engine.atoms.gate import (
    write_into,
    split_banner,
    gate_text,
    gate_text_lean,
    banner_version,
)


# ── Fixtures ──────────────────────────────────────────────────────────────────

FIXTURE_BODY = """- [test-atom-1](test-atom-1.md) — first test atom
- [test-atom-2](test-atom-2.md) — second test atom with some body text
- [test-atom-3](test-atom-3.md) — third test atom

Some prose below the index.
"""

FIXTURE_BODY_BYTES = FIXTURE_BODY.encode("utf-8")


@pytest.fixture
def mem_with_banner(tmp_path):
    """Create a MEMORY.md with a full gate banner + known body."""
    mem = tmp_path / "memory" / "MEMORY.md"
    mem.parent.mkdir(parents=True, exist_ok=True)
    banner = gate_text("test-scope", write_target=str(mem))
    mem.write_text(banner + FIXTURE_BODY, encoding="utf-8")
    return mem


@pytest.fixture
def mem_no_banner(tmp_path):
    """Create a MEMORY.md with NO banner (no `---` rule)."""
    mem = tmp_path / "memory" / "MEMORY.md"
    mem.parent.mkdir(parents=True, exist_ok=True)
    mem.write_text(FIXTURE_BODY, encoding="utf-8")
    return mem


# ── split_banner unit tests ───────────────────────────────────────────────────

def test_split_banner_normal():
    """split_banner separates banner from body at the first `---`."""
    content = "some banner\n---\nbody here"
    banner, body = split_banner(content)
    assert banner == "some banner\n---"
    assert body == "\nbody here"


def test_split_banner_no_rule():
    """split_banner returns empty banner when there is no `---`."""
    content = "just body, no rule"
    banner, body = split_banner(content)
    assert banner == ""
    assert body == "just body, no rule"


def test_split_banner_multiple_rules():
    """split_banner splits at the FIRST `---` only."""
    content = "banner\n---\nbody\n---\nmore body"
    banner, body = split_banner(content)
    assert banner == "banner\n---"
    assert body == "\nbody\n---\nmore body"


# ── write_into body preservation ──────────────────────────────────────────────

def test_write_into_preserves_body_full(mem_with_banner):
    """Full gate: body below `---` is preserved byte-for-byte."""
    original = mem_with_banner.read_text(encoding="utf-8")
    _, orig_body = split_banner(original)

    rep = write_into(mem_with_banner, "test-scope", lean=False)
    rewritten = mem_with_banner.read_text(encoding="utf-8")
    _, new_body = split_banner(rewritten)

    assert rep["preserved_body_chars"] == len(orig_body)
    assert new_body == orig_body, "Body must be preserved byte-for-byte"


def test_write_into_preserves_body_lean(mem_with_banner):
    """Lean gate: body below `---` is preserved byte-for-byte."""
    original = mem_with_banner.read_text(encoding="utf-8")
    _, orig_body = split_banner(original)

    rep = write_into(mem_with_banner, "test-scope", lean=True)
    rewritten = mem_with_banner.read_text(encoding="utf-8")
    _, new_body = split_banner(rewritten)

    assert rep["preserved_body_chars"] == len(orig_body)
    assert new_body == orig_body, "Body must be preserved byte-for-byte (lean)"


def test_write_into_idempotent_full(mem_with_banner):
    """Repeated full gate writes preserve the body identically."""
    # First rewrite
    write_into(mem_with_banner, "test-scope", lean=False)
    after_first = mem_with_banner.read_text(encoding="utf-8")
    _, body1 = split_banner(after_first)

    # Second rewrite (same scope, same lean)
    write_into(mem_with_banner, "test-scope", lean=False)
    after_second = mem_with_banner.read_text(encoding="utf-8")
    _, body2 = split_banner(after_second)

    # The banner may change (engine root could differ), but body must be identical
    assert body1 == body2, "Body must survive repeated rewrites unchanged"


def test_write_into_idempotent_lean(mem_with_banner):
    """Repeated lean gate writes preserve the body identically."""
    write_into(mem_with_banner, "test-scope", lean=True)
    after_first = mem_with_banner.read_text(encoding="utf-8")
    _, body1 = split_banner(after_first)

    write_into(mem_with_banner, "test-scope", lean=True)
    after_second = mem_with_banner.read_text(encoding="utf-8")
    _, body2 = split_banner(after_second)

    assert body1 == body2, "Body must survive repeated lean rewrites unchanged"


def test_write_into_lean_to_full_switching_preserves_body(mem_with_banner):
    """Switching from lean to full (or vice versa) must preserve the body."""
    original = mem_with_banner.read_text(encoding="utf-8")
    _, orig_body = split_banner(original)

    # Write lean
    write_into(mem_with_banner, "test-scope", lean=True)
    _, body_after_lean = split_banner(mem_with_banner.read_text(encoding="utf-8"))
    assert body_after_lean == orig_body, "Lean write must preserve body"

    # Rewrite as full
    write_into(mem_with_banner, "test-scope", lean=False)
    _, body_after_full = split_banner(mem_with_banner.read_text(encoding="utf-8"))
    assert body_after_full == orig_body, "Full rewrite must preserve body"

    # Back to lean
    write_into(mem_with_banner, "test-scope", lean=True)
    _, body_after_lean2 = split_banner(mem_with_banner.read_text(encoding="utf-8"))
    assert body_after_lean2 == orig_body, "Lean rewrite must preserve body"


def test_write_into_no_banner_file(mem_no_banner):
    """When a file has no `---`, the whole file is treated as body and preserved."""
    original = mem_no_banner.read_text(encoding="utf-8")

    rep = write_into(mem_no_banner, "test-scope", lean=False)
    rewritten = mem_no_banner.read_text(encoding="utf-8")

    # The original content should appear somewhere in the rewritten file
    # (the banner is prepended, the original becomes body)
    assert original.strip() in rewritten, "Original content must survive in rewritten file"


def test_write_into_new_file(tmp_path):
    """Creating a new MEMORY.md from scratch."""
    mem = tmp_path / "new_memory" / "MEMORY.md"
    rep = write_into(mem, "test-scope", lean=False)
    assert rep["created"] is True
    assert rep["preserved_body_chars"] == 0
    assert mem.exists()
    content = mem.read_text(encoding="utf-8")
    assert "---" in content


def test_write_into_new_file_lean(tmp_path):
    """Creating a new MEMORY.md from scratch with lean gate."""
    mem = tmp_path / "new_memory" / "MEMORY.md"
    rep = write_into(mem, "test-scope", lean=True)
    assert rep["created"] is True
    assert rep["lean"] is True
    assert rep["preserved_body_chars"] == 0
    assert mem.exists()
    content = mem.read_text(encoding="utf-8")
    assert "---" in content


# ── Banner size assertions ────────────────────────────────────────────────────

def test_lean_banner_is_smaller_than_full():
    """The lean banner must be ≤60% of the full banner (target: ≤40%)."""
    full = gate_text("echelon")
    lean = gate_text_lean("echelon")
    ratio = len(lean) / len(full)
    # Draft target is ≤40%; gate at ≤60% so the build stays honest.
    assert ratio <= 0.60, f"Lean banner is {ratio:.1%} of full (must be ≤60%)"
    # Log the actual ratio for the report
    print(f"Lean/full ratio: {ratio:.1%} ({len(lean)} / {len(full)} bytes)")


@pytest.mark.parametrize("checkout", ["D:/repos/engine-checkout", "D:/" + "long-candidate-directory/" * 12 + "ECHELON-AGENT"])
def test_lean_banner_no_cartridge_scope_is_compact(monkeypatch, checkout):
    """For a non-cartridge scope, the lean banner should be very compact."""
    from echelon_engine.atoms import gate as _g
    monkeypatch.setattr(_g, "_engine_root", lambda: checkout)
    monkeypatch.setattr(_g, "_compact_handoff_notice", lambda *a, **k: "")  # base banner budget excludes per-session continuity evidence
    monkeypatch.setattr(_g, "_room_block", lambda cwd="": "")  # compactness is measured WITHOUT a cwd room (the ROOM block is per-project, ~180B)
    lean = gate_text_lean("some-random-scope")
    full = gate_text("some-random-scope")
    assert len(lean) <= len(full), "Lean must not be larger than full"
    # Non-cartridge banner should be under 2KB
    assert len(lean) < 2048, f"Non-cartridge lean banner is {len(lean)} bytes (target < 2048)"
    if len(checkout) > 48:
        assert "Engine path: `echelon doctor`" in lean
        assert checkout not in lean


# ── spec S8 V3 (INC-0001): one banner writer — the full form never clobbers lean ──

def test_gate_write_full_refuses_on_lean_banner(tmp_path, capsys):
    """`gate --write` (full form) onto a file carrying ECHELON-GATE-BANNER v9-lean
    exits 2 and leaves the file unchanged unless --full is passed (spec S8 V3)."""
    from echelon_engine.atoms import gate as gate_mod
    mem = tmp_path / "memory" / "MEMORY.md"
    mem.parent.mkdir(parents=True, exist_ok=True)
    write_into(mem, "test-scope", lean=True)
    before = mem.read_text(encoding="utf-8")
    rc = gate_mod._main(["--write", str(mem), "--scope", "test-scope"])
    assert rc == 2
    assert mem.read_text(encoding="utf-8") == before  # unchanged
    assert "v9-lean" in capsys.readouterr().err  # the reason is printed
    # --full forces the full form through
    rc = gate_mod._main(["--write", str(mem), "--scope", "test-scope", "--full"])
    assert rc == 0
    content = mem.read_text(encoding="utf-8")
    assert "v9-lean" not in content
    assert banner_version(content).startswith("v8")
    # a lean write onto a lean file stays allowed (idempotent)
    rc = gate_mod._main(["--write", str(mem), "--scope", "test-scope", "--lean"])
    assert rc == 0
    assert banner_version(mem.read_text(encoding="utf-8")).startswith("v9-lean")


def test_banner_version_extracts_token():
    assert banner_version("") == ""
    assert banner_version("plain body\n") == ""
    full = "<!-- ECHELON-GATE-BANNER v8 (2026-08-02) — +LAW 4 loop-shape & tool-wait\n---\n"
    assert banner_version(full) == "v8 (2026-08-02)"
    lean = "<!-- ECHELON-GATE-BANNER v9-lean (2026-08-02) — ceremony tiered by stakes\n---\n"
    assert banner_version(lean) == "v9-lean (2026-08-02)"


def test_write_into_preserves_lean_banner_for_every_caller(tmp_path):
    """Gate r1 S8a S-3: the library door itself keeps a v9-lean file lean (the SessionStart
    hook calls write_into with no lean=); only force=True puts the full form over it."""
    from echelon_engine.atoms.gate import write_into, banner_version
    p = tmp_path / "MEMORY.md"
    write_into(p, "s", lean=True); p.write_text(p.read_text(encoding="utf-8") + "body\n", encoding="utf-8")
    rep = write_into(p, "s")
    assert rep["lean"] is True and "v9-lean" in banner_version(p.read_text(encoding="utf-8"))
    assert p.read_text(encoding="utf-8").endswith("body\n")
    rep = write_into(p, "s", force=True)
    assert rep["lean"] is False and "v9-lean" not in banner_version(p.read_text(encoding="utf-8"))
