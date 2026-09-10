"""status command — the human-output path must survive an immune-scan failure.

Regression for the 2026-07-06 review finding: the `warn_list` referenced by the human-output
loop was assigned only inside the immune-scan try block, so a scan exception left it unbound
and `status` crashed with NameError instead of degrading to "immune: unknown".
"""
from __future__ import annotations

import echelon_engine.atoms.immune as immune
from echelon_engine.atoms import status_cmd


def test_status_survives_immune_scan_failure(monkeypatch, capsys):
    def _boom(*args, **kwargs):
        raise RuntimeError("scan blew up")

    monkeypatch.setattr(immune, "scan", _boom)
    rc = status_cmd._main([])           # must NOT raise NameError on the unbound warn_list
    assert rc == 0
    out = capsys.readouterr().out
    assert "immune: unknown" in out     # degraded gracefully, not crashed


def test_status_total_accounts_for_unscoped_atoms(capsys, tmp_path, monkeypatch):
    """The headline total is FILTERED (`scope!=''`), so it must never be presented as the bank size.

    2026-08-18: `status` reported 8462 while the bank held 8637 — 175 pre-scope atoms (namespace
    inside the coordinate) were excluded by the scope survey and by every scope-grouped door. A
    2026-07-08 mining pass had disclaimed 12 of them and never finished the rest, and the filtered
    total is what kept that half-done migration invisible for six weeks.

    Asserts the RELATIONSHIP, not a live count, so it holds as the bank grows and still passes
    once the unscoped set is eventually rescued or retired to zero.
    """
    import json

    # HERMETIC: point ECHELON_HOME at a temp dir so this reads a bank the test owns.
    # Without it the test opened the caller's real ~/.echelon/echelon.db, which made the
    # raw-count cross-check race any concurrent write to that bank (it read 57359 against
    # a table holding 57361) and made a pass depend on the machine it ran on.
    monkeypatch.setenv("ECHELON_HOME", str(tmp_path))

    from echelon_engine.atoms.cards import CardStore
    seed = CardStore()
    seed.bank_atom("fixture:scoped", "a scoped fixture atom", scope="fixture")
    seed.conn.commit()
    seed.conn.close()

    status_cmd._main(["--json"])
    d = json.loads(capsys.readouterr().out)

    assert d["bank_atoms"] == d["total_atoms"] + d["unscoped_atoms"]
    assert d["unscoped_atoms"] >= 0
    assert d["bank_atoms"] >= d["total_atoms"]

    # Cross-check the filtered total against the raw table: any drift between them must be
    # fully explained by the unscoped census, never silently absorbed into the headline.
    raw = CardStore().conn.execute("SELECT COUNT(*) FROM atoms").fetchone()[0]
    assert d["bank_atoms"] == raw

    if d["unscoped_atoms"]:
        status_cmd._main([])            # human path must SURFACE it, not drop it
        assert "unscoped:" in capsys.readouterr().out
