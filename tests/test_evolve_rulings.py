"""Tests for `echelon_engine evolve rulings` -- OPEN-0113 win 2, the rulings miner.

All fixtures are synthetic tmp_path jsonl/json files; no network, no real
~/.echelon, no real D:/repos/estate estate. Proposals only -- this module
must never open CLAUDE.md or MEMORY.md for writing.
"""
from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from echelon_engine.atoms import evolve


# ---------------------------------------------------------------------------
# fixtures: 6 rulings, 2 owner notes
# ---------------------------------------------------------------------------

def _ev(**kw):
    return kw


def _write_rulings(path: Path):
    events = [
        # R-0001: echelon room, VERBATIM owner quote -- the private-repo law
        _ev(evt="create", id="R-0001", room="echelon", title="privacy default",
            summary="s", options=[], ts="2026-08-01 10:00:00"),
        _ev(evt="rule", id="R-0001", ts="2026-08-01 10:05:00",
            text='Owner ruled: "repos stay private under the org by default."'),

        # R-0002: echelon room, paraphrase of the same law + a machine-origin
        # say event that must never leak into evidence or clustering.
        _ev(evt="create", id="R-0002", room="echelon", title="privacy default again",
            summary="s", options=[], ts="2026-08-02 10:00:00"),
        _ev(evt="say", id="R-0002", ts="2026-08-02 10:01:00", from_="room-pulse",
            text="repos stay private under the org by default (machine noise, ignore)"),
        _ev(evt="rule", id="R-0002", ts="2026-08-02 10:05:00",
            text="repos stay private under the org by default"),

        # R-0003: a DIFFERENT room, same law paraphrased with a synonym (repo/repository)
        _ev(evt="create", id="R-0003", room="alpha-app", title="privacy default third",
            summary="s", options=[], ts="2026-08-03 10:00:00"),
        _ev(evt="rule", id="R-0003", ts="2026-08-03 10:05:00",
            text="repositories stay private under the org by default"),

        # R-0004: unrelated ruling -- must end up a singleton, never a proposal
        _ev(evt="create", id="R-0004", room="echelon", title="unrelated",
            summary="s", options=[], ts="2026-08-04 10:00:00"),
        _ev(evt="rule", id="R-0004", ts="2026-08-04 10:05:00",
            text="always push before moving repos"),

        # R-0005: unruled -- no rule event ever lands, must never appear anywhere
        _ev(evt="create", id="R-0005", room="echelon", title="unruled, still open",
            summary="s", options=[], ts="2026-08-05 10:00:00"),
    ]
    lines = []
    for e in events:
        e = dict(e)
        if "from_" in e:
            e["from"] = e.pop("from_")
        lines.append(json.dumps(e))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_notes(inbox: Path):
    inbox.mkdir(parents=True, exist_ok=True)
    (inbox / "NOTE-0001.json").write_text(json.dumps({
        "id": "NOTE-0001", "from": "owner", "board_n": 42,
        "ts": "2026-08-06 10:00:00",
        "text": "repos stay private under the org by default",
        "ruling": "R-0002",
    }), encoding="utf-8")
    # not from the owner -- must never be mined
    (inbox / "NOTE-0002.json").write_text(json.dumps({
        "id": "NOTE-0002", "from": "someone-else", "board_n": 43,
        "ts": "2026-08-06 11:00:00",
        "text": "repos stay private under the org by default (not the owner)",
        "ruling": "R-0002",
    }), encoding="utf-8")


@pytest.fixture
def rulings_path(tmp_path):
    p = tmp_path / "rulings.jsonl"
    _write_rulings(p)
    return p


@pytest.fixture
def inbox_dir(tmp_path):
    d = tmp_path / "inbox"
    _write_notes(d)
    return d


# ---------------------------------------------------------------------------
# step 1: fold
# ---------------------------------------------------------------------------

def test_fold_rulings_counts_and_skips_unruled(rulings_path):
    rulings = evolve._fold_rulings(rulings_path)
    ids = {r["id"] for r in rulings}
    assert ids == {"R-0001", "R-0002", "R-0003", "R-0004"}
    assert "R-0005" not in ids  # unruled, never appears


def test_fold_rulings_ignores_machine_say_events(rulings_path):
    rulings = evolve._fold_rulings(rulings_path)
    r2 = next(r for r in rulings if r["id"] == "R-0002")
    assert "machine" not in r2["ruled_text"]
    assert r2["ruled_text"] == "repos stay private under the org by default"


# ---------------------------------------------------------------------------
# step 2: owner notes
# ---------------------------------------------------------------------------

def test_owner_notes_only_from_owner(inbox_dir):
    notes = evolve._owner_notes(inbox_dir)
    assert len(notes) == 1
    assert notes[0]["id"] == "NOTE-0001"
    assert notes[0]["board_n"] == 42
    assert notes[0]["ruling"] == "R-0002"


def test_owner_notes_empty_dir(tmp_path):
    assert evolve._owner_notes(tmp_path / "absent") == []


# ---------------------------------------------------------------------------
# step 3: law sentences + verbatim
# ---------------------------------------------------------------------------

def test_law_sentences_keeps_declarative_drops_narration():
    sents = evolve._law_sentences("repos stay private under the org by default")
    assert sents == ["repos stay private under the org by default"]
    assert evolve._law_sentences("just chatting about the weather today") == []


def test_law_sentences_verbatim_quote_detected():
    text = 'Owner ruled: "repos stay private under the org by default."'
    sents = evolve._law_sentences(text)
    assert len(sents) == 1
    assert evolve._is_verbatim(sents[0]) is True


def test_law_sentences_paraphrase_not_verbatim():
    sents = evolve._law_sentences("repos stay private under the org by default")
    assert evolve._is_verbatim(sents[0]) is False


# ---------------------------------------------------------------------------
# step 4/5: cluster + propose, end to end via the fold+notes pipeline
# ---------------------------------------------------------------------------

def _clusters_for(rulings_path, inbox_dir):
    rulings = evolve._fold_rulings(rulings_path)
    notes = evolve._owner_notes(inbox_dir)
    sentences = evolve._sentence_records(rulings, notes)
    return evolve._cluster(sentences)


def test_cluster_private_repo_law_n4_two_rooms(rulings_path, inbox_dir):
    clusters = _clusters_for(rulings_path, inbox_dir)
    main = max(clusters, key=lambda c: c["n"])
    assert main["n"] == 4
    # _cluster returns sorted(rooms), so this is alphabetical, not insertion order.
    assert main["rooms"] == ["alpha-app", "echelon"]


def test_propose_declared_with_verbatim_law_line(rulings_path, inbox_dir):
    clusters = _clusters_for(rulings_path, inbox_dir)
    main = max(clusters, key=lambda c: c["n"])
    proposal = evolve._propose(main, db_path=str(Path("nonexistent-dir-xyz") / "none.db"))
    assert proposal["confidence"] == "declared"
    assert proposal["law"] == 'Owner ruled: "repos stay private under the org by default."'
    assert proposal["n"] == 4


def test_machine_say_event_never_in_evidence(rulings_path, inbox_dir):
    clusters = _clusters_for(rulings_path, inbox_dir)
    main = max(clusters, key=lambda c: c["n"])
    proposal = evolve._propose(main, db_path=None)
    for ev in proposal["evidence"]:
        assert "machine noise" not in ev["quote"]


def test_singleton_listed_not_proposed(rulings_path, inbox_dir):
    clusters = _clusters_for(rulings_path, inbox_dir)
    singleton = min(clusters, key=lambda c: c["n"])
    assert singleton["n"] == 1
    assert "push" in singleton["key"]


def test_anti_no_op_paraphrase_only_cluster_is_never_declared():
    """A cluster with zero verbatim members must be tagged inferred, never
    declared -- habit is not law, and a paraphrase is not the owner's word."""
    sentences = [
        {"text": "repos stay private under the org by default", "source_id": "R-A",
         "ts": "2026-08-01 00:00:00", "room": "echelon", "verbatim": False},
        {"text": "repositories stay private under the org by default", "source_id": "R-B",
         "ts": "2026-08-02 00:00:00", "room": "alpha-app", "verbatim": False},
    ]
    clusters = evolve._cluster(sentences)
    assert len(clusters) == 1
    proposal = evolve._propose(clusters[0], db_path=str(Path("nonexistent-dir-xyz") / "none.db"))
    assert proposal["confidence"] == "inferred"


# ---------------------------------------------------------------------------
# existing_atoms degrade
# ---------------------------------------------------------------------------

def test_existing_atoms_degrades_to_empty_on_absent_db(tmp_path):
    absent = tmp_path / "no" / "such" / "bank.db"
    out = evolve._existing_atoms("a law nobody has ever written down", str(absent))
    assert out == []


# ---------------------------------------------------------------------------
# CLI: --json, --write-proposals, report shape
# ---------------------------------------------------------------------------

def test_cli_json_parses(rulings_path, inbox_dir, tmp_path, capsys):
    out_file = tmp_path / "out.json"
    rc = evolve._main(["rulings", "--rulings", str(rulings_path), "--inbox", str(inbox_dir),
                        "--json", "--out", str(out_file)])
    assert rc == 0
    payload = json.loads(out_file.read_text(encoding="utf-8"))
    assert "proposals" in payload and "singletons" in payload
    assert len(payload["proposals"]) == 1
    assert payload["proposals"][0]["n"] == 4
    err = capsys.readouterr().err
    assert "evolve rulings:" in err


def test_cli_write_proposals_writes_exactly_one_file(rulings_path, inbox_dir, tmp_path):
    out_dir = tmp_path / "proposals"
    rc = evolve._main(["rulings", "--rulings", str(rulings_path), "--inbox", str(inbox_dir),
                        "--write-proposals", str(out_dir)])
    assert rc == 0
    files = list(out_dir.glob("*"))
    assert len(files) == 1
    assert files[0].name.startswith("LAW-PROPOSALS-")
    doc = json.loads(files[0].read_text(encoding="utf-8"))
    assert doc["version"] == 1
    assert "as_of" in doc and "sources" in doc
    assert len(doc["proposals"]) == 1
    assert len(doc["singletons"]) == 1


def test_cli_text_report_shows_singletons_and_proposals(rulings_path, inbox_dir, tmp_path):
    out_file = tmp_path / "out.md"
    rc = evolve._main(["rulings", "--rulings", str(rulings_path), "--inbox", str(inbox_dir),
                        "--out", str(out_file)])
    assert rc == 0
    text = out_file.read_text(encoding="utf-8")
    assert "== LAW PROPOSALS (1) ==" in text
    assert "== SINGLETONS (1) ==" in text
    assert "confidence=declared" in text


def test_min_cluster_pushes_pair_to_singletons(rulings_path, inbox_dir, tmp_path):
    out_file = tmp_path / "out.md"
    rc = evolve._main(["rulings", "--rulings", str(rulings_path), "--inbox", str(inbox_dir),
                        "--min-cluster", "10", "--out", str(out_file)])
    assert rc == 0
    text = out_file.read_text(encoding="utf-8")
    assert "== LAW PROPOSALS (0) ==" in text
    assert "== SINGLETONS (2) ==" in text


# ---------------------------------------------------------------------------
# the never-write law: no code path opens CLAUDE.md / MEMORY.md
# ---------------------------------------------------------------------------

def test_never_writes_constitution_files():
    src_path = Path(evolve.__file__)
    src = src_path.read_text(encoding="utf-8")
    tree = ast.parse(src)
    docstring = ast.get_docstring(tree) or ""
    for needle in ("CLAUDE.md", "MEMORY.md"):
        total = src.count(needle)
        in_doc = docstring.count(needle)
        assert total == in_doc, (
            f"{needle} appears {total} time(s) in evolve.py but only {in_doc} "
            "inside the module docstring -- the miner must never open it for writing"
        )
