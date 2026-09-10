import json

import pytest

from echelon_engine.atoms import harness_sync


def _contract(tmp_path):
    names = ("claude_memory", "codex_memory", "claude_banner", "codex_banner", "atom_list", "memory_index", "reflex_list")
    path = tmp_path / "contract.json"
    path.write_text(json.dumps({"version": 1, "scope": "echelon", "atom_limit": 5,
                                "reflex_path": str(tmp_path / "reflexes.json"),
                                "targets": {name: str(tmp_path / f"{name}.md") for name in names}}), encoding="utf-8")
    return path


def test_projection_is_contract_driven_and_preserves_owner_text(tmp_path, monkeypatch):
    contract = _contract(tmp_path)
    (tmp_path / "reflexes.json").write_text(json.dumps({"rules": [{"scope": "echelon", "name": "protect-bank", "action": "block", "tool": "Bash"}]}), encoding="utf-8")
    monkeypatch.setattr(harness_sync, "_snapshot", lambda scope, reflex_path, limit: {
        "scope": scope, "atom_count": 1, "reflexes": harness_sync._reflexes(reflex_path, scope),
        "atoms": [type("A", (), {"coordinate": "law:one", "id": "abc", "score": 105.0, "ts": 1, "content": "One durable fact", "kind": "feedback"})()]})
    memory = tmp_path / "claude_memory.md"
    memory.write_text("# Owner notes\n", encoding="utf-8")
    receipt = harness_sync.project(contract)
    assert receipt["changed"]
    first = memory.read_text(encoding="utf-8")
    assert "# Owner notes" in first and "protect-bank" in (tmp_path / "reflex_list.md").read_text(encoding="utf-8")
    assert harness_sync.project(contract)["changed"] == []
    assert harness_sync.project(contract, check=True)["drift"] == []


# ── spec S8 V2 (INC-0002): the room comes from the CONTRACT, not cwd ─────────

_NAMES = ("claude_memory", "codex_memory", "claude_banner", "codex_banner",
          "atom_list", "memory_index", "reflex_list")


def _v2_contract(tmp_path, **extra):
    path = tmp_path / "contract.json"
    path.write_text(json.dumps({"version": 2, "scope": "echelon", "atom_limit": 5,
                                "reflex_path": str(tmp_path / "reflexes.json"),
                                "targets": {n: str(tmp_path / f"{n}.md") for n in _NAMES},
                                **extra}), encoding="utf-8")
    (tmp_path / "reflexes.json").write_text(json.dumps({"rules": []}), encoding="utf-8")
    return path


def test_contract_room_wins_over_cwd(tmp_path, monkeypatch):
    """Contract `room` X + cwd Y -> the ROOM slice is X's, reported via contract."""
    from echelon_engine import workcycle as wc
    monkeypatch.setenv("ECHELON_HOME", str(tmp_path / "home"))
    root_x = tmp_path / "x"
    root_x.mkdir()
    (root_x / ".git").mkdir()
    wc.init(root_x, estate="x-estate", scope="x-scope")
    elsewhere = tmp_path / "y"
    elsewhere.mkdir()
    contract = _v2_contract(tmp_path, room=str(root_x))
    monkeypatch.setattr(harness_sync, "_snapshot", lambda scope, reflex_path, limit: {
        "scope": scope, "atom_count": 1, "reflexes": [], "atoms": [],
        "generated_at": "2026-08-27T00:00:00+00:00"})
    receipt = harness_sync.project(contract, cwd=str(elsewhere))
    assert receipt["room_via"] == "contract"
    assert receipt["room"] == str((root_x / ".echelon").resolve())
    mem = (tmp_path / "claude_memory.md").read_text(encoding="utf-8")
    assert "ROOM x-estate" in mem  # X's ROOM line, never Y's


def test_contract_scope_resolves_via_registry(tmp_path, monkeypatch):
    """No `room` key: the registry room whose scope == contract.scope wins."""
    from echelon_engine import workcycle as wc
    monkeypatch.setenv("ECHELON_HOME", str(tmp_path / "home"))
    root_x = tmp_path / "x"
    root_x.mkdir()
    wc.init(root_x, estate="x-estate", scope="x-scope")
    wc.register_room(root_x)  # the explicit verb is the registry door
    contract = _v2_contract(tmp_path, scope="x-scope")  # no room key
    monkeypatch.setattr(harness_sync, "_snapshot", lambda scope, reflex_path, limit: {
        "scope": scope, "atom_count": 1, "reflexes": [], "atoms": [],
        "generated_at": "2026-08-27T00:00:00+00:00"})
    receipt = harness_sync.project(contract, cwd=str(tmp_path / "nowhere"))
    assert receipt["room_via"] == "registry"
    assert "ROOM x-estate" in (tmp_path / "claude_memory.md").read_text(encoding="utf-8")


# ── OPEN-0060 phase A deliverable 3: framework projection ────────────────────

def test_framework_region_lands_inside_generated_region_only(tmp_path, monkeypatch):
    """contract.framework block -> a `## ECHELON framework door` line inside CLAUDE.md's
    generated (BEGIN/END-marked) region; owner-authored text outside stays byte-identical."""
    contract = _contract(tmp_path)
    data = json.loads(contract.read_text(encoding="utf-8"))
    data["framework"] = {"path": "D:/repos/framework", "entry": "echelon-fw", "version": "0.1.0"}
    contract.write_text(json.dumps(data), encoding="utf-8")
    monkeypatch.setattr(harness_sync, "_framework_probe",
                        lambda entry: (True, ["init", "design", "compile", "grammar"]))
    monkeypatch.setattr(harness_sync, "_snapshot", lambda scope, reflex_path, limit: {
        "scope": scope, "atom_count": 1, "reflexes": [],
        "atoms": [type("A", (), {"coordinate": "law:one", "id": "abc", "score": 105.0, "ts": 1,
                                  "content": "One durable fact", "kind": "feedback"})()]})

    owner_text = "# Owner-authored heading\nSome instructions the harness must never touch.\n"
    claude_md = tmp_path / "claude_banner.md"
    claude_md.write_text(owner_text, encoding="utf-8")

    receipt = harness_sync.project(contract)
    assert receipt["changed"]

    text = claude_md.read_text(encoding="utf-8")
    assert owner_text in text  # owner text preserved byte-for-byte, outside the region

    begin = text.find(harness_sync.BEGIN)
    end = text.find(harness_sync.END)
    assert begin >= 0 and end > begin
    region = text[begin:end]
    assert "## ECHELON framework door" in region
    assert "Installed: **yes**" in region
    assert "entry `echelon-fw`" in region
    assert "version `0.1.0`" in region
    # verbs are PARSED from `--help`, never hand-picked (gate round 2, 2026-09-02)
    assert "`init|design|compile|grammar`" in region
    # the line must not leak outside the generated region
    assert "## ECHELON framework door" not in text[:begin]
    assert "## ECHELON framework door" not in text[end + len(harness_sync.END):]


def test_framework_region_absent_without_contract_block(tmp_path, monkeypatch):
    """No `framework` key in the contract -> no framework region at all (nothing to project)."""
    contract = _contract(tmp_path)
    monkeypatch.setattr(harness_sync, "_snapshot", lambda scope, reflex_path, limit: {
        "scope": scope, "atom_count": 1, "reflexes": [],
        "atoms": [type("A", (), {"coordinate": "law:one", "id": "abc", "score": 105.0, "ts": 1,
                                  "content": "One durable fact", "kind": "feedback"})()]})
    receipt = harness_sync.project(contract)
    assert receipt["changed"]
    text = (tmp_path / "claude_banner.md").read_text(encoding="utf-8")
    assert "## ECHELON framework door" not in text


def test_framework_region_reports_not_installed(tmp_path, monkeypatch):
    contract = _contract(tmp_path)
    data = json.loads(contract.read_text(encoding="utf-8"))
    data["framework"] = {"path": "D:/repos/framework", "entry": "echelon-fw", "version": "0.1.0"}
    contract.write_text(json.dumps(data), encoding="utf-8")
    monkeypatch.setattr(harness_sync, "_framework_probe", lambda entry: (False, []))
    monkeypatch.setattr(harness_sync, "_snapshot", lambda scope, reflex_path, limit: {
        "scope": scope, "atom_count": 1, "reflexes": [],
        "atoms": [type("A", (), {"coordinate": "law:one", "id": "abc", "score": 105.0, "ts": 1,
                                  "content": "One durable fact", "kind": "feedback"})()]})
    receipt = harness_sync.project(contract)
    assert receipt["changed"]
    text = (tmp_path / "claude_banner.md").read_text(encoding="utf-8")
    assert "Installed: **no**" in text
    assert "Verbs: unknown" in text


def test_framework_probe_parses_verbs_from_argparse_help(monkeypatch):
    """The verb list comes from the entry's own `--help` positional block: every advertised
    verb, no hand-picked subset, and the `VERB` placeholder is not a verb."""
    fake_help = (
        "usage: echelon-fw [-h] VERB ...\n\n"
        "ECHELON Framework — the verb surface.\n\n"
        "positional arguments:\n"
        "  VERB\n"
        "    init      initialize a project: write the .eos door + src/ + bin/ +\n"
        "              .echelon/\n"
        "    design    scan/plan/build the .des canvases\n"
        "    compile   walk the estate\n"
        "    test-set  a hyphenated verb\n\n"
        "options:\n"
        "  -h, --help  show this help message and exit\n"
    )

    class R:
        returncode = 0
        stdout = fake_help

    monkeypatch.setattr(harness_sync.shutil, "which", lambda e: "C:/fake/echelon-fw.exe")
    monkeypatch.setattr(harness_sync.subprocess, "run", lambda *a, **k: R())
    installed, verbs = harness_sync._framework_probe("echelon-fw")
    assert installed is True
    assert verbs == ["init", "design", "compile", "test-set"]


def test_sync_main_prints_room_line(tmp_path, monkeypatch, capsys):
    """The text door prints `room: <path> (via contract|registry|cwd)`."""
    from echelon_engine import workcycle as wc
    monkeypatch.setenv("ECHELON_HOME", str(tmp_path / "home"))
    root_x = tmp_path / "x"
    root_x.mkdir()
    (root_x / ".git").mkdir()
    wc.init(root_x, estate="x-estate", scope="x-scope")
    contract = _v2_contract(tmp_path, room=str(root_x))
    monkeypatch.setattr(harness_sync, "_snapshot", lambda scope, reflex_path, limit: {
        "scope": scope, "atom_count": 1, "reflexes": [], "atoms": [],
        "generated_at": "2026-08-27T00:00:00+00:00"})
    assert harness_sync._main(["--contract", str(contract)]) == 0
    out = capsys.readouterr().out
    assert f"room: {str((root_x / '.echelon').resolve())} (via contract)" in out

# appended by OPEN-0062 step 2 builder — child-contract (gate 6) tests

# ── SPEC-S9 step 2: child contracts inherit the parent scope (gate 6) ────────


def _promoted_child_estate(tmp_path, monkeypatch):
    """A registered beta-svc estate with a promoted mrp child; the ledger is
    stubbed so no bank write ever leaves this test."""
    from echelon_engine import session_state
    from echelon_engine import workcycle as wc
    monkeypatch.setattr(session_state, "open_ledger", lambda scope, **kw: None)
    root = tmp_path / "estate"
    root.mkdir()
    wc.init(root, estate="beta-svc", scope="mol")
    wc.register_room(root, scope="mol")
    proot = root / "mrp"
    proot.mkdir()
    wc.set_room_projects("beta-svc", {"mrp": {"name": "MRP", "root": "mrp",
                                             "repo": True, "deploy": True}})
    room = root / ".echelon"
    for i in (1, 2, 3):
        wc.open_item(room, f"mrp {i}", project="mrp")
    wc.promote("beta-svc", "mrp")
    return root, proot


def test_gate_6_child_contract_inherits_parent_scope_and_projects_child_state(tmp_path, monkeypatch):
    monkeypatch.setenv("ECHELON_HOME", str(tmp_path / "home"))
    root, proot = _promoted_child_estate(tmp_path, monkeypatch)
    contract = _v2_contract(tmp_path, room=str(proot), parent="beta-svc")
    data = json.loads(contract.read_text(encoding="utf-8"))
    del data["scope"]  # a child contract OMITS an independent scope — it inherits
    contract.write_text(json.dumps(data), encoding="utf-8")
    seen = {}

    def snap(scope, reflex_path, limit):
        seen["scope"] = scope
        return {"scope": scope, "atom_count": 1, "reflexes": [], "atoms": []}

    monkeypatch.setattr(harness_sync, "_snapshot", snap)
    receipt = harness_sync.project(contract)
    assert seen["scope"] == "mol"                       # the PARENT scope primed the projection
    assert receipt["scope"] == "mol" and receipt["child"] is True
    assert receipt["parent"] == "beta-svc" and receipt["room_via"] == "child"
    memory = (tmp_path / "claude_memory.md").read_text(encoding="utf-8")
    assert "beta-svc/mrp" in memory                      # the child's OWN state is projected


def test_gate_6_child_scope_mismatch_is_a_hard_error(tmp_path, monkeypatch):
    monkeypatch.setenv("ECHELON_HOME", str(tmp_path / "home"))
    root, proot = _promoted_child_estate(tmp_path, monkeypatch)
    rj_path = proot / ".echelon" / "room.json"
    rj = json.loads(rj_path.read_text(encoding="utf-8"))
    rj["scope"] = "other"                               # the child drifted off the parent scope
    rj_path.write_text(json.dumps(rj), encoding="utf-8")
    contract = _v2_contract(tmp_path, room=str(proot), parent="beta-svc")
    data = json.loads(contract.read_text(encoding="utf-8"))
    del data["scope"]
    contract.write_text(json.dumps(data), encoding="utf-8")
    monkeypatch.setattr(harness_sync, "_snapshot", lambda scope, reflex_path, limit: {
        "scope": scope, "atom_count": 1, "reflexes": [], "atoms": []})
    with pytest.raises(ValueError, match="does not equal parent"):
        harness_sync.project(contract)


def test_gate_6_child_contract_is_explicit_not_name_based_and_requires_version_2(tmp_path, monkeypatch):
    monkeypatch.setenv("ECHELON_HOME", str(tmp_path / "home"))
    from echelon_engine import workcycle as wc
    root, proot = _promoted_child_estate(tmp_path, monkeypatch)
    # a room that declares NO parent is not a child — naming it under a parent is refused
    contract = _v2_contract(tmp_path, room=str(root), parent="beta-svc")
    data = json.loads(contract.read_text(encoding="utf-8"))
    del data["scope"]
    contract.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ValueError, match="declares parent None"):
        harness_sync.project(contract)
    # version-1 contracts cannot carry a parent key
    v1 = tmp_path / "v1.json"
    v1.write_text(json.dumps({"version": 1, "parent": "beta-svc", "scope": "mol",
                              "targets": {"claude_memory": str(tmp_path / "v1.md")}}), encoding="utf-8")
    with pytest.raises(ValueError, match="requires contract.version >= 2"):
        harness_sync.project(v1)


# ── room-tolerant verify: live receipt churn is not harness drift ─────────────

def _stub_snapshot(monkeypatch):
    monkeypatch.setattr(harness_sync, "_snapshot", lambda scope, reflex_path, limit: {
        "scope": scope, "atom_count": 0, "reflexes": [], "atoms": []})


def test_room_tolerant_check_separates_live_room_churn_from_harness_drift(tmp_path, monkeypatch):
    """A seat writing receipts between apply and verify changes ONLY the ROOM slice; a strict
    check reads that as drift forever (the moderator claim door could never pass while any
    other seat was alive). room_tolerant=True keeps the file's own slice for the verdict and
    reports the churn under `room_drift` instead."""
    _stub_snapshot(monkeypatch)
    root = tmp_path / "estate"; (root / ".echelon").mkdir(parents=True)
    contract = _v2_contract(tmp_path, room=str(root))
    briefs = iter(["pulse A", "pulse B", "pulse B", "pulse C"])
    monkeypatch.setattr(harness_sync, "_room_region_from",
                        lambda room, n: f"{harness_sync.ROOM_BEGIN}\n{next(briefs)}\n{harness_sync.ROOM_END}")
    assert harness_sync.project(contract)["changed"]                       # apply with pulse A
    strict = harness_sync.project(contract, check=True)                    # another seat -> pulse B
    assert set(strict["drift"]) == {"claude_memory", "codex_memory", "codex_banner"}
    assert strict["room_drift"] == []                                      # strict mode: no split
    tolerant = harness_sync.project(contract, check=True, room_tolerant=True)  # still pulse B
    assert tolerant["drift"] == []
    assert tolerant["room_drift"] == ["claude_memory", "codex_banner", "codex_memory"]
    # a REAL harness change still drifts under the tolerant verify
    (tmp_path / "claude_memory.md").write_text(
        (tmp_path / "claude_memory.md").read_text(encoding="utf-8").replace("live atoms: **0**", "live atoms: **9**"),
        encoding="utf-8")
    hurt = harness_sync.project(contract, check=True, room_tolerant=True)  # pulse C
    assert hurt["drift"] == ["claude_memory"]


def test_room_tolerant_check_still_repairs_a_missing_room_slice(tmp_path, monkeypatch):
    """No slice in the file = a repair owed, never tolerated churn."""
    _stub_snapshot(monkeypatch)
    root = tmp_path / "estate"; (root / ".echelon").mkdir(parents=True)
    contract = _v2_contract(tmp_path, room=str(root))
    monkeypatch.setattr(harness_sync, "_room_region_from",
                        lambda room, n: f"{harness_sync.ROOM_BEGIN}\nbrief\n{harness_sync.ROOM_END}")
    harness_sync.project(contract)
    m = tmp_path / "claude_memory.md"
    text = m.read_text(encoding="utf-8")
    s, e = text.find(harness_sync.ROOM_BEGIN), text.find(harness_sync.ROOM_END) + len(harness_sync.ROOM_END)
    m.write_text(text[:s] + text[e:], encoding="utf-8")
    r = harness_sync.project(contract, check=True, room_tolerant=True)
    assert r["drift"] == ["claude_memory"] and r["room_drift"] == []
