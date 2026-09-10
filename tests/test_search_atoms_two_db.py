"""search_atoms must open the bank the way the CLI does — with BOTH databases.

THE BUG (2026-08-15): `SeedStore(db_path)` passed the v2 bank as the CORE db, so the
web recall returned `cold` / 0 atoms for a bank that plainly held the answer, while
`recall --warm` on the SAME bank with the SAME query returned `lukewarm` with the atom
on top. Two code paths disagreeing about one bank.

The user-visible failure is the worst possible one for a memory product: a new user
writes their first memory, asks about it, and is told "I don't have any record of
that" — with the atom sitting right there.
"""
from __future__ import annotations

import os

from echelon_engine import services
from echelon_engine.services_memory import search_atoms


def test_web_recall_finds_an_atom_the_cli_would_find(tmp_path):
    home = str(tmp_path)
    planted = services.remember_lesson(
        "Two workers on the same port die silently - check the port first.",
        scope="ts", home=home, label="lesson")
    assert planted["ok"], planted

    r = search_atoms("workers dying silently", "ts", limit=5,
                     db_path=os.path.join(home, "echelon.db"))
    assert r["count"] >= 1, f"web recall found nothing: {r}"
    assert r["verdict"] != "cold"
    assert any("same port die silently" in a["content"] for a in r["atoms"])


def test_search_atoms_survives_a_bank_with_no_core_sibling(tmp_path):
    """A caller may pass a lone v2 path; that must degrade, never raise."""
    services.remember_lesson("a lesson", scope="ts", home=str(tmp_path), label="l")
    lone = tmp_path / "solo" / "echelon.db"
    lone.parent.mkdir()
    lone.write_bytes((tmp_path / "echelon.db").read_bytes())
    r = search_atoms("lesson", "ts", limit=5, db_path=str(lone))
    assert "verdict" in r and "atoms" in r
