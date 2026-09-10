"""The DOORS (BRIEF W3): gate ROOM block, harness-sync v2 room slice, codex_nerve
resume-first, staged stop/warmup hooks, install-hooks --room.

Every test injects a FAKE `echelon_engine.workcycle` (the sibling worker's module,
not present in this worktree) via `monkeypatch.setitem(sys.modules, ...)` — nothing
here depends on the real module existing.
"""
import importlib.util
import json
import subprocess
import sys
import types
import time
from pathlib import Path

import pytest

from echelon_engine.atoms import gate, harness_sync, hooks_cmd
from echelon_engine import codex_nerve


BRIEF = "ROOM W3-doors\nWHERE build the four doors\nTASK stage 2: harness slice\nNEXT wire tests"


def _fake_workcycle(monkeypatch, room="room", brief=BRIEF, checkpoint=None, raise_import=False):
    fake = types.ModuleType("echelon_engine.workcycle")
    calls = []

    def _room_path(start=None):
        if not room:
            return None
        return Path(start or ".") / room

    fake.room_path = _room_path
    fake.resume_brief = lambda r: brief
    fake.checkpoint = checkpoint or (lambda r, **kw: calls.append((r, kw)))
    if raise_import:
        monkeypatch.setitem(sys.modules, "echelon_engine.workcycle",
                            {"__wants__": "import-error"})
        import echelon_engine as _pkg; monkeypatch.delattr(_pkg, "workcycle", raising=False)
        return calls
    monkeypatch.setitem(sys.modules, "echelon_engine.workcycle", fake)
    import echelon_engine as _pkg; monkeypatch.setattr(_pkg, "workcycle", fake, raising=False)  # the package attr wins over sys.modules once the real module was collected
    return calls


# ── gate.py: the lean banner's ROOM block ─────────────────────────────────────

def test_gate_lean_shows_room_block_iff_room_exists(tmp_path, monkeypatch):
    _fake_workcycle(monkeypatch)
    target = tmp_path / "memory" / "MEMORY.md"
    text = gate.gate_text_lean("echelon", write_target=str(target))
    # 3 lines max: goal · task/stage · next action
    assert "> **ROOM** — goal: build the four doors" in text
    assert ">   task/stage: stage 2: harness slice" in text
    assert ">   next: wire tests" in text
    room_lines = [l for l in text.splitlines()
                  if l.startswith(("> **ROOM**", ">   task/stage", ">   next"))]
    assert len(room_lines) == 3


def test_gate_lean_has_no_room_block_when_no_room(monkeypatch, tmp_path):
    _fake_workcycle(monkeypatch, room=None)
    text = gate.gate_text_lean("echelon", write_target=str(tmp_path / "memory" / "MEMORY.md"))
    assert "ROOM" not in text


def test_gate_lean_has_no_room_block_when_workcycle_missing(monkeypatch, tmp_path):
    monkeypatch.setitem(sys.modules, "echelon_engine.workcycle", object())  # not a module
    import echelon_engine as _pkg; monkeypatch.delattr(_pkg, "workcycle", raising=False)
    monkeypatch.setitem(sys.modules, "echelon_engine", types.SimpleNamespace())
    text = gate.gate_text_lean("echelon", write_target=str(tmp_path / "memory" / "MEMORY.md"))
    assert "ROOM" not in text


def test_gate_write_into_bakes_room_block_into_banner(tmp_path, monkeypatch):
    _fake_workcycle(monkeypatch)
    target = tmp_path / "memory" / "MEMORY.md"
    gate.write_into(target, "echelon", lean=True)
    content = target.read_text(encoding="utf-8")
    assert "> **ROOM** — goal: build the four doors" in content


# ── harness_sync.py: contract version 2 room slice ────────────────────────────

@pytest.fixture
def sync_contract(tmp_path, monkeypatch):
    names = ("claude_memory", "codex_memory", "claude_banner", "codex_banner",
             "atom_list", "memory_index", "reflex_list")
    path = tmp_path / "contract.json"
    path.write_text(json.dumps({"version": 2, "scope": "echelon", "atom_limit": 5,
                                "gate_memory_targets": ["claude_memory"],
                                "reflex_path": str(tmp_path / "reflexes.json"),
                                "targets": {n: str(tmp_path / f"{n}.md") for n in names}}),
                    encoding="utf-8")
    (tmp_path / "reflexes.json").write_text(json.dumps({"rules": []}), encoding="utf-8")
    monkeypatch.setattr(harness_sync, "_snapshot", lambda scope, reflex_path, limit: {
        "scope": scope, "atom_count": 1, "reflexes": [], "atoms": [],
        "generated_at": "2026-08-27T00:00:00+00:00"})
    _fake_workcycle(monkeypatch, room="room")
    return path


def test_harness_sync_v2_writes_room_markers_after_gate_banner(sync_contract, tmp_path):
    harness_sync.project(sync_contract)
    mem = (tmp_path / "claude_memory.md").read_text(encoding="utf-8")
    assert "<!-- ECHELON-ROOM:BEGIN -->" in mem
    assert "WHERE build the four doors" in mem
    banner = mem.index("ECHELON-GATE-BANNER")
    rule = mem.index("---")
    room = mem.index("ECHELON-ROOM:BEGIN")
    harness = mem.index("ECHELON-HARNESS-SYNC:BEGIN")
    assert banner < rule < room < harness  # directly AFTER the gate banner region


def test_harness_sync_v2_truncates_at_room_max_bytes(sync_contract, tmp_path, monkeypatch):
    fake = sys.modules["echelon_engine.workcycle"]
    raw = "ROOM big\n" + "x" * 5000
    fake.resume_brief = lambda r: raw
    harness_sync.project(sync_contract)
    mem = (tmp_path / "claude_memory.md").read_text(encoding="utf-8")
    seg = mem[mem.index("<!-- ECHELON-ROOM:BEGIN -->"):mem.index("<!-- ECHELON-ROOM:END -->")]
    brief = seg.split("\n", 1)[1].strip("\n")
    n = len(raw) - 1200
    assert brief.endswith(f"…(+{n} bytes)")
    assert len(brief) == 1200 + 1 + len(f"…(+{n} bytes)")


def test_harness_sync_v2_default_targets_and_ceiling(sync_contract, tmp_path):
    harness_sync.project(sync_contract)
    for name in ("claude_memory", "codex_memory", "codex_banner"):
        content = (tmp_path / f"{name}.md").read_text(encoding="utf-8")
        assert "<!-- ECHELON-ROOM:BEGIN -->" in content
    # MEMORY.md read ceiling holds: the room slice is bounded, the file stays small
    assert len((tmp_path / "claude_memory.md").read_text(encoding="utf-8")) < 4000


def test_harness_sync_v1_writes_no_room_markers(tmp_path, monkeypatch):
    names = ("claude_memory", "codex_memory", "claude_banner", "codex_banner",
             "atom_list", "memory_index", "reflex_list")
    path = tmp_path / "v1.json"
    path.write_text(json.dumps({"version": 1, "scope": "echelon", "atom_limit": 5,
                                "targets": {n: str(tmp_path / f"{n}.md") for n in names}}),
                    encoding="utf-8")
    monkeypatch.setattr(harness_sync, "_snapshot", lambda scope, reflex_path, limit: {
        "scope": scope, "atom_count": 1, "reflexes": [], "atoms": [],
        "generated_at": "2026-08-27T00:00:00+00:00"})
    _fake_workcycle(monkeypatch, room="room")
    harness_sync.project(path)
    assert "ECHELON-ROOM" not in (tmp_path / "claude_memory.md").read_text(encoding="utf-8")


def test_harness_sync_v2_no_room_means_no_markers(sync_contract, tmp_path, monkeypatch):
    sys.modules["echelon_engine.workcycle"].room_path = lambda start=None: None
    harness_sync.project(sync_contract)
    assert "ECHELON-ROOM" not in (tmp_path / "claude_memory.md").read_text(encoding="utf-8")


def test_harness_sync_v2_removes_stale_room_region(sync_contract, tmp_path, monkeypatch):
    harness_sync.project(sync_contract)
    assert "ECHELON-ROOM" in (tmp_path / "claude_memory.md").read_text(encoding="utf-8")
    sys.modules["echelon_engine.workcycle"].room_path = lambda start=None: None
    harness_sync.project(sync_contract)
    assert "ECHELON-ROOM" not in (tmp_path / "claude_memory.md").read_text(encoding="utf-8")


def test_harness_sync_v2_room_projection_is_idempotent(sync_contract):
    harness_sync.project(sync_contract)
    second = harness_sync.project(sync_contract)
    assert second["changed"] == [] and second["drift"] == []


# ── codex_nerve.py: resume_brief BEFORE the bank warmth block ─────────────────

@pytest.fixture
def fake_warmth(monkeypatch):
    import echelon_engine.atoms.warmth as warmth_mod

    class Reading:
        verdict, score, guidance = "cold", 0.0, "think fresh"
        warmest = []

    monkeypatch.setattr(warmth_mod, "warmth", lambda *a, **k: Reading())
    return warmth_mod


def test_codex_nerve_room_before_warmth(fake_warmth, tmp_path, monkeypatch):
    _fake_workcycle(monkeypatch, room="room")
    text, _ = codex_nerve.primer("build the doors now please", str(tmp_path))
    assert "ROOM W3-doors" in text
    assert text.index("ROOM W3-doors") < text.index("[ECHELON NERVE]")


def test_codex_nerve_room_brief_byte_equal_to_direct_call(fake_warmth, tmp_path, monkeypatch):
    _fake_workcycle(monkeypatch, room="room")
    text, _ = codex_nerve.primer("build the doors now please", str(tmp_path))
    brief = BRIEF.rstrip("\n")
    assert text.startswith(brief)  # verbatim — row 11 byte-equality
    assert text[len(brief)] == "\n"


def test_codex_nerve_no_room_emits_nothing_extra(fake_warmth, tmp_path, monkeypatch):
    _fake_workcycle(monkeypatch, room=None)
    text, _ = codex_nerve.primer("build the doors now please", str(tmp_path))
    assert "ROOM" not in text
    assert text.startswith("[ECHELON NERVE]")


def test_codex_nerve_broken_room_is_severed(fake_warmth, tmp_path, monkeypatch):
    _fake_workcycle(monkeypatch, room="room")
    sys.modules["echelon_engine.workcycle"].resume_brief = lambda r: (_ for _ in ()).throw(RuntimeError("boom"))
    text, _ = codex_nerve.primer("build the doors now please", str(tmp_path))
    assert "ROOM" not in text
    assert text.startswith("[ECHELON NERVE]")


# ── staged hooks: echelon_stop.py ─────────────────────────────────────────────

@pytest.fixture
def fake_engine_package(tmp_path):
    """A lightweight fake echelon_engine package so the staged stop hook runs as a
    REAL subprocess in <2s (the real engine's import-time scanner takes ~3s cold)."""
    pkg = tmp_path / "fake_engine" / "echelon_engine"
    (pkg / "atoms").mkdir(parents=True)
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "atoms" / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "atoms" / "resolve_scope.py").write_text(
        "def resolve_scope(cwd=None, *, live_scopes=None):\n    return 'testscope'\n",
        encoding="utf-8")
    return tmp_path / "fake_engine"


def _write_fake_workcycle(fake_engine, room=True, checkpoint_body="pass"):
    (fake_engine / "echelon_engine" / "workcycle.py").write_text(
        f"""import pathlib
def room_path(start=None):
    if not {room}:
        return None
    return pathlib.Path(start or ".") / "room"
def checkpoint(room, **kw):
    {checkpoint_body}
""", encoding="utf-8")


def _run_stop_hook(fake_engine, payload):
    hook = Path(__file__).resolve().parent.parent / "echelon_engine" / "hooks_staged" / "echelon_stop.py"
    t0 = time.monotonic()
    proc = subprocess.run(
        [sys.executable, str(hook)],
        input=payload, capture_output=True, text=True,
        env={**__import__("os").environ, "PYTHONPATH": str(fake_engine)},
        cwd=str(fake_engine), timeout=10)
    return proc, time.monotonic() - t0


def test_staged_stop_hook_exits_0_fast_no_room(fake_engine_package):
    _write_fake_workcycle(fake_engine_package, room=False)
    proc, elapsed = _run_stop_hook(fake_engine_package, json.dumps({"cwd": str(fake_engine_package)}))
    assert proc.returncode == 0
    assert elapsed < 2.0
    assert proc.stdout == ""


def test_staged_stop_hook_exits_0_fast_broken_checkpoint(fake_engine_package):
    _write_fake_workcycle(fake_engine_package, room=True,
                          checkpoint_body="raise RuntimeError('boom')")
    proc, elapsed = _run_stop_hook(fake_engine_package, json.dumps({"cwd": str(fake_engine_package)}))
    assert proc.returncode == 0
    assert elapsed < 2.0
    assert proc.stdout == ""


def test_staged_stop_hook_exits_0_fast_empty_stdin(fake_engine_package):
    _write_fake_workcycle(fake_engine_package, room=False)
    proc, elapsed = _run_stop_hook(fake_engine_package, "")
    assert proc.returncode == 0
    assert elapsed < 2.0


def test_staged_stop_hook_leaves_scope_resolution_to_room(tmp_path, monkeypatch):
    """The checkpoint door owns room scope; bank resolution must not block it."""
    hook = Path(__file__).resolve().parent.parent / "echelon_engine" / "hooks_staged" / "echelon_stop.py"
    spec = importlib.util.spec_from_file_location("staged_stop", hook)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    calls = []
    _fake_workcycle(monkeypatch, room="room",
                    checkpoint=lambda r, **kw: calls.append((r, kw)))
    import echelon_engine.atoms.resolve_scope as rs
    monkeypatch.setattr(rs, "resolve_scope", lambda *a, **k: (_ for _ in ()).throw(AssertionError("bank scope lookup")))
    monkeypatch.setattr(sys, "stdin", type("S", (), {"read": lambda self: json.dumps({"cwd": str(tmp_path)})})())
    mod._safe_main()
    assert len(calls) == 1
    room, kw = calls[0]
    assert kw.get("auto") is True and "scope" not in kw
    assert "room" in str(room)


# ── staged hooks: echelon_warmup.py ───────────────────────────────────────────

def test_staged_warmup_preserves_pre_room_hook():
    """Room integration preserves the pre-room hook; behavior is tested below."""
    ref = Path(__file__).resolve().parent / "fixtures" / "doors" / "echelon_warmup.ref.py"  # committed reference copy of the pre-room live hook
    staged = Path(__file__).resolve().parent.parent / "echelon_engine" / "hooks_staged" / "echelon_warmup.py"
    import difflib
    diff = list(difflib.unified_diff(ref.read_text(encoding="utf-8").splitlines(),
                                     staged.read_text(encoding="utf-8").splitlines(), n=0))
    added = [l for l in diff if l.startswith("+") and not l.startswith("+++")]
    removed = [l for l in diff if l.startswith("-") and not l.startswith("---")]
    assert removed == [] and added


def test_staged_warmup_prints_room_brief_above_offering(fake_engine_package, monkeypatch):
    """With a fake workcycle providing a room, the warmup's first output line is the
    room brief (it is printed ABOVE the bank offering — here the lean offering)."""
    (fake_engine_package / "echelon_engine" / "atoms" / "cards.py").write_text(
        "class CardStore:\n    pass\n", encoding="utf-8")
    (fake_engine_package / "echelon_engine" / "atoms" / "summoning_lean.py").write_text(
        "def summoning_lean_text(cwd, scope, kind):\n    return 'LEAN OFFERING\\n'\n",
        encoding="utf-8")
    (fake_engine_package / "echelon_engine" / "workcycle.py").write_text(
        f"""import pathlib
def room_path(start=None):
    return pathlib.Path(start or ".") / "room"
def resume_brief(room):
    return 'ROOM W3-doors\\nWHERE build the four doors\\nTASK stage 2\\nNEXT wire tests'
""", encoding="utf-8")
    import os
    env = {**os.environ, "PYTHONPATH": str(fake_engine_package),
           "HOME": str(fake_engine_package), "USERPROFILE": str(fake_engine_package)}
    hook = Path(__file__).resolve().parent.parent / "echelon_engine" / "hooks_staged" / "echelon_warmup.py"
    proc = subprocess.run(
        [sys.executable, str(hook)],
        input=json.dumps({"cwd": str(fake_engine_package)}), capture_output=True, text=True,
        env=env, cwd=str(fake_engine_package), timeout=15)
    assert proc.returncode == 0
    lines = proc.stdout.splitlines()
    assert lines[0] == "ROOM W3-doors"  # brief above the offering
    assert lines.index("LEAN OFFERING") == 4  # brief is 4 lines, then the offering


# ── hooks_cmd.py: install-hooks --room ────────────────────────────────────────

def test_install_hooks_room_copies_staged_files(tmp_path):
    results = hooks_cmd.cmd_install_hooks(hooks_dir=tmp_path, room=True)
    assert results["echelon_warmup.py"].startswith("installed")
    assert results["echelon_stop.py"].startswith("installed")
    warmup = (tmp_path / "echelon_warmup.py").read_text(encoding="utf-8")
    assert "ROOM DOOR" in warmup and "resume_brief" in warmup
    stop = (tmp_path / "echelon_stop.py").read_text(encoding="utf-8")
    assert "checkpoint" in stop and "ROOM DOOR" not in stop
    # byte-identical to the staged sources
    assert warmup == hooks_cmd._staged_dir().joinpath("echelon_warmup.py").read_text(encoding="utf-8")


def test_install_hooks_room_refuses_overwrite_without_force(tmp_path):
    hooks_cmd.cmd_install_hooks(hooks_dir=tmp_path, room=True)
    before = (tmp_path / "echelon_warmup.py").read_text(encoding="utf-8")
    results = hooks_cmd.cmd_install_hooks(hooks_dir=tmp_path, room=True)
    assert "exists (not overwritten" in results["echelon_warmup.py"]
    assert "exists (not overwritten" in results["echelon_stop.py"]
    assert (tmp_path / "echelon_warmup.py").read_text(encoding="utf-8") == before
    forced = hooks_cmd.cmd_install_hooks(hooks_dir=tmp_path, room=True, force=True)
    assert forced["echelon_warmup.py"].startswith("installed")
    assert forced["echelon_stop.py"].startswith("installed")


def test_install_hooks_installs_all_staged_without_room(tmp_path):
    """spec S8 V4: WITHOUT --room every staged hook installs — the staged dir is
    the ONE source of the hook canon; the old stub-only default is gone."""
    staged = hooks_cmd._staged_dir()
    results = hooks_cmd.cmd_install_hooks(hooks_dir=tmp_path)
    assert len(results) == len(list(staged.glob("*.py")))
    for name in ("echelon_gate.py", "echelon_warmup.py", "echelon_stop.py",
                 "echelon_reflex.py", "echelon_prime.py"):
        assert results[name].startswith("installed"), name
        assert (tmp_path / name).read_text(encoding="utf-8") ==             (staged / name).read_text(encoding="utf-8"), name
    stop = (tmp_path / "echelon_stop.py").read_text(encoding="utf-8")
    assert "checkpoint" in stop  # the STAGED stop, not the old stub


def test_install_hooks_diff_lists_drift(tmp_path):
    """--diff: N staged files installed -> N current; a modified installed copy is
    named as drift; a removed one as missing (spec S8 V4)."""
    hooks_cmd.cmd_install_hooks(hooks_dir=tmp_path)
    assert all(s == "current" for s in hooks_cmd.cmd_hooks_diff(hooks_dir=tmp_path).values())
    (tmp_path / "echelon_gate.py").write_text("changed" + chr(10), encoding="utf-8")
    diff = hooks_cmd.cmd_hooks_diff(hooks_dir=tmp_path)
    assert diff["echelon_gate.py"] == "drift"
    assert all(s == "current" for n, s in diff.items() if n != "echelon_gate.py")
    (tmp_path / "echelon_warmup.py").unlink()
    assert hooks_cmd.cmd_hooks_diff(hooks_dir=tmp_path)["echelon_warmup.py"] == "missing"


def test_install_hooks_diff_cli_exit_codes(tmp_path, capsys):
    """--diff exits 1 while any drift, 0 when installed == staged (spec S8 V4)."""
    assert hooks_cmd._main_install_hooks(["--diff", "--hooks-dir", str(tmp_path)]) == 1
    hooks_cmd.cmd_install_hooks(hooks_dir=tmp_path)
    assert hooks_cmd._main_install_hooks(["--diff", "--hooks-dir", str(tmp_path)]) == 0
    (tmp_path / "echelon_gate.py").write_text("x" + chr(10), encoding="utf-8")
    assert hooks_cmd._main_install_hooks(["--diff", "--hooks-dir", str(tmp_path)]) == 1

def test_room_region_caps_on_bytes_not_chars(monkeypatch):
    # gate counter-test C2 (2026-08-27): multi-byte briefs must respect the BYTE ceiling
    import sys, types
    from pathlib import Path
    fake = types.ModuleType("echelon_engine.workcycle")
    fake.room_path = lambda start=None: Path(".")
    fake.resume_brief = lambda room: "ROOM " + ("é" * 1500)
    monkeypatch.setitem(sys.modules, "echelon_engine.workcycle", fake)
    import echelon_engine as _pkg; monkeypatch.setattr(_pkg, "workcycle", fake, raising=False)  # the package attr wins over sys.modules once the real module was collected
    from echelon_engine.atoms.harness_sync import _room_region, ROOM_BEGIN, ROOM_END
    out = _room_region(None, 1200)
    body = out[len(ROOM_BEGIN):-len(ROOM_END)]
    assert len(body.split("(+")[0].encode("utf-8")) <= 1200 + 8
    assert "bytes)" in out


def test_staged_stop_hook_checkpoints_through_the_real_engine(tmp_path):
    """LIVE-SMOKE REGRESSION (2026-08-27): the real engine's import runs the scanner
    (~3s cold). With a 2s watchdog the hook exited 0 but NEVER wrote — the fake-engine
    tests above cannot see that. This one runs the real hook, real engine, real room.
    ECHELON_HOME is isolated so the hook's delta disarm() (spec S8 V7) writes to the
    tmp home — a test must never touch the real bank or the real archive."""
    from echelon_engine import workcycle
    workcycle.init(tmp_path, estate="t", type_="engine", scope="testscope")
    hook = Path(__file__).resolve().parent.parent / "echelon_engine" / "hooks_staged" / "echelon_stop.py"
    env = {**__import__("os").environ, "ECHELON_HOME": str(tmp_path / "home")}
    proc = subprocess.run([sys.executable, "-X", "utf8", str(hook)],
                          input=json.dumps({"cwd": str(tmp_path)}), capture_output=True, text=True,
                          env=env, timeout=30)
    assert proc.returncode == 0
    lines = list((tmp_path / ".echelon" / "journal").glob("*.jsonl"))
    assert lines, "stop hook wrote no journal line through the real engine"
    assert any('"auto": true' in l for l in lines[0].read_text(encoding="utf-8").splitlines())

