"""selftest — the earning-loop end-to-end ALARM (B4, from the ruflo study 2026-07-06).

WHY THIS EXISTS (ruflo-lesson-recorded-everything-distilled-nothing): ruflo's most humbling
defect was a scheduled `consolidate` worker that existed, ran every 30 minutes, and stayed GREEN
for 6,000+ commits — while writing hardcoded zeros and touching no database. Recording is not
learning; a job COMPLETING is not the job doing its work. The only honest proof is a MOVED ROW.

`echelon selftest` is the alarm ECHELON lacked: it proves the whole earning loop end-to-end on a
throwaway scope (`_selftest`), reading the actual DB value before and after — not trusting an exit
code, an "enabled: true", or any worker's say-so. It exercises the REAL organs (ingest_folder,
warmth, CardStore.remember_fetch through the witnessed door), not a mock, so a stub anywhere in the
chain fails a stage loudly.

THE FIVE STAGES:
  1. INGEST   — write a synthetic atom .md to a temp dir; ingest --scope _selftest. Prove it landed.
  2. COMPILE  — prove the atom has a compiled spine + a born-neutral earned row (score 100, use 0).
  3. RECALL   — warmth on its exact description must SURFACE it (recording that can't be recalled is
                the ruflo failure exactly).
  4. EARN     — remember_fetch through the witnessed door; read atom_earned BEFORE and AFTER and
                prove the weight/use_count actually MOVED. This is the moved-row proof.
  5. CLEANUP  — remove the temp dir. `_selftest` scope rows are LEFT in place (no scope-purge path
                exists in the bank, and we do NOT build a deletion mechanism just for this — a
                throwaway scope's rows never surface in `echelon`, so they are harmless).

Exit 0 iff every stage PASSED; non-zero on any FAIL. Safe: only ever writes to scope `_selftest`.
"""
from __future__ import annotations

import argparse
import os
import tempfile
import time
from pathlib import Path

_SCOPE = "_selftest"


def _line(ok: bool, stage: str, detail: str) -> None:
    print(f"  [{'PASS' if ok else 'FAIL'}] {stage:<9} {detail}")


def run_selftest(db_path: str | None = None, keep_dir: bool = False) -> int:
    """Prove the earning loop end-to-end on scope `_selftest`. Returns 0 (all PASS) or 1 (any FAIL)."""
    from .store import SeedStore
    from .ingest import ingest_folder
    from .warmth import warmth

    stamp = int(time.time())
    slug = f"selftest-moved-row-probe-{stamp}"
    # A description unique enough that warmth can only surface THIS atom (the exact-match recall test).
    desc = (f"selftest probe {stamp}: the moved-row alarm proving the earning loop runs end-to-end, "
            f"not merely that its parts exist and stay green")
    atom_md = (
        f"---\n"
        f"name: {slug}\n"
        f"description: {desc}\n"
        f"metadata:\n"
        f"  type: note\n"
        f"  witness: execution\n"
        f"---\n\n"
        f"Synthetic atom planted by `echelon selftest` at {stamp}. Its only job is to be ingested, "
        f"recalled, and fetched through the witnessed door so the earned weight can be read before and "
        f"after — the moved-row proof. See ruflo-lesson-recorded-everything-distilled-nothing.\n"
    )

    print(f"echelon selftest — proving the earning loop end-to-end on scope '{_SCOPE}'\n")
    tmp = Path(tempfile.mkdtemp(prefix="echelon_selftest_"))
    failed = False
    # The STORED coordinate is norm_coord'd (leading '_' stripped, hyphens -> '_'), so compare against
    # the normalized form — the same shape recall surfaces and atom_id_for_coordinate resolves.
    from .coord_norm import norm_coord as _nc
    coord = _nc(f"{_SCOPE}:{slug}")
    try:
        (tmp / f"{slug}.md").write_text(atom_md, encoding="utf-8")

        # STAGE 1 — INGEST (the real organ, not a mock).
        # The selftest plants a TEMP dir under the fixed scope '_selftest', so the ingest
        # canonicalization gate (which resolves scope from the folder name) necessarily
        # disagrees — it resolved 'echelon-selftest-<tmpsuffix>'. That is precisely the
        # deliberate-override case the gate documents, so declare it rather than weaken the
        # gate. Without this the selftest died at stage 1 and the earning loop went unproven
        # every session (found 2026-08-15 while wiring the wrap-optimise verbs).
        store = SeedStore(db_path) if db_path else SeedStore()
        _prev_override = os.environ.get("ECHELON_INGEST_SCOPE")
        os.environ["ECHELON_INGEST_SCOPE"] = "1"
        try:
            planted = ingest_folder(tmp, _SCOPE, db_path=db_path, verbose=False)
        finally:
            if _prev_override is None:
                os.environ.pop("ECHELON_INGEST_SCOPE", None)
            else:
                os.environ["ECHELON_INGEST_SCOPE"] = _prev_override
        aid = store.cards.atom_id_for_coordinate(coord)
        ok1 = bool(planted) and aid is not None
        _line(ok1, "INGEST", f"planted {len(planted)} atom(s); coordinate {coord} -> atom_id {aid}")
        failed |= not ok1

        # STAGE 2 — COMPILE: spine present + a born-neutral earned row.
        earned_before = store.cards.struct_earned(aid) if aid else None
        peek = store.cards.recall_peek(aid) if aid else None
        ok2 = peek is not None and earned_before is not None
        _line(ok2, "COMPILE", f"spine={'yes' if peek else 'NO'}  earned_before={earned_before}")
        failed |= not ok2

        # STAGE 3 — RECALL: warmth on the exact description must surface this atom.
        reading = warmth(desc, store, scope=_SCOPE)
        surfaced_coords = [getattr(getattr(sw, "seed", sw), "coordinate", "") for sw in (reading.warmest or [])]
        ok3 = coord in surfaced_coords
        top = surfaced_coords[0] if surfaced_coords else "(none)"
        _line(ok3, "RECALL", f"verdict={reading.verdict} score={round(reading.score,3)}; "
                             f"surfaced={'YES' if ok3 else 'NO'} (top: {top})")
        failed |= not ok3

        # STAGE 4 — EARN: fetch through the witnessed door; the earned row must MOVE (the moved-row proof).
        served = store.cards.remember_fetch(aid, depth="spine") if aid else None
        earned_after = store.cards.struct_earned(aid) if aid else None
        moved = (bool(earned_before) and bool(earned_after)
                 and (earned_after["score"] > earned_before["score"])
                 and (earned_after["use_count"] > earned_before["use_count"]))
        ok4 = served is not None and moved
        _line(ok4, "EARN", f"served={'yes' if served else 'NO'}  "
                           f"score {earned_before and earned_before['score']} -> {earned_after and earned_after['score']}  "
                           f"use_count {earned_before and earned_before['use_count']} -> {earned_after and earned_after['use_count']}  "
                           f"MOVED={'YES' if moved else 'NO'}")
        failed |= not ok4

    finally:
        # STAGE 5 — CLEANUP: remove the temp dir. `_selftest` scope rows are left (no purge path; we
        # do not build a deletion mechanism just for this — the throwaway scope never surfaces in echelon).
        if not keep_dir:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)
            _line(True, "CLEANUP", f"removed temp dir {tmp} (scope '{_SCOPE}' rows left — no purge path, harmless)")
        else:
            _line(True, "CLEANUP", f"kept temp dir {tmp} (--keep-dir)")

    print()
    if failed:
        print("SELFTEST: FAIL — a stage above did not prove the moved row. The earning loop is NOT "
              "trustworthy end-to-end (the ruflo stub-worker failure mode). Investigate the failing stage.")
        return 1
    print("SELFTEST: PASS — ingest -> compile -> recall -> witnessed-earn all proven, with the moved "
          "row read from the DB (not an exit code). The earning loop runs end-to-end.")
    return 0


def _main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="echelon selftest",
        description="Prove the earning loop end-to-end on a throwaway scope (_selftest): ingest a "
                    "synthetic atom, recall it, fetch it through the witnessed door, and verify the "
                    "earned weight actually MOVED in the DB. The moved-row alarm (ruflo study). "
                    "Exit 0 = all PASS; non-zero = any FAIL.")
    ap.add_argument("--db", default=None, help="override bank db path (default ~/.echelon/echelon.db)")
    ap.add_argument("--keep-dir", action="store_true", help="do not delete the temp dir (debugging)")
    a = ap.parse_args(argv)
    return run_selftest(db_path=a.db, keep_dir=a.keep_dir)


if __name__ == "__main__":
    import sys
    sys.exit(_main())
