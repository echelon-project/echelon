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

    # HERMETIC: this test used to read the caller's REAL ~/.echelon/echelon.db and
    # cross-check a raw row count against it, so it raced any concurrent write to that
    # bank (observed: 57359 vs a table holding 57361) and its result depended on the
    # machine it ran on.
    #
    # Setting ECHELON_HOME is NOT sufficient: `cards.DEFAULT_V2_DB` is evaluated at
    # IMPORT time (cards.py line ~110), so by the time this test runs in a full suite
    # some earlier test has already imported the module and frozen the default to the
    # real home. Patching the resolved constant is what actually redirects the bare
    # `CardStore()` that status_cmd constructs internally.
    from echelon_engine.atoms import cards as _cards
    bank = tmp_path / "echelon.db"
    monkeypatch.setenv("ECHELON_HOME", str(tmp_path))
    CardStore = _cards.CardStore

    # `CardStore.__init__(self, db_path=DEFAULT_V2_DB)` binds its default ONCE, when the
    # `def` executes at import. Rebinding the module constant therefore does nothing to
    # a bare `CardStore()` — which is exactly what status_cmd constructs internally. The
    # default itself has to be replaced.
    monkeypatch.setattr(_cards, "DEFAULT_V2_DB", bank)
    monkeypatch.setattr(CardStore.__init__, "__defaults__", (bank,))

    seed = CardStore(bank)
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
    raw = CardStore(bank).conn.execute("SELECT COUNT(*) FROM atoms").fetchone()[0]
    assert d["bank_atoms"] == raw

    if d["unscoped_atoms"]:
        status_cmd._main([])            # human path must SURFACE it, not drop it
        assert "unscoped:" in capsys.readouterr().out
