"""Two-way sync round-trip over two REAL banks — the end-to-end proof.

Not a unit test: this drives two independent CardStore banks the way the live flow will
(local <-> cloud), through the real witnessed doors, and checks the properties that decide
whether the design is safe to point at box4:

  1. an atom authored on A reaches B, and vice versa (the whole reason for two-way)
  2. earned weight from BOTH banks survives on BOTH banks (the crown jewels)
  3. sync is CONVERGENT — after a round trip both banks agree
  4. sync is IDEMPOTENT — running it again changes nothing (no drift, no double-count)
  5. no echo — a second cycle with no new work ships zero ops
  6. concurrent edits to the SAME atom on both sides merge without loss
  7. scope filtering keeps a second estate out of the exchange
  8. a restore mid-life is detected by generation mismatch (no silent cursor replay)
"""
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

os.environ["ECHELON_SYNC"] = "1"

from echelon_engine.atoms.cards import CardStore
from echelon_engine.atoms import sync_journal as sj

OK, FAIL = "PASS", "FAIL"
results = []


def check(name, cond, detail=""):
    results.append((OK if cond else FAIL, name, detail))
    print(f"  [{OK if cond else FAIL}] {name}" + (f"  — {detail}" if detail else ""))
    return cond


def sync_once(a: CardStore, b: CardStore, a_name: str, b_name: str, scope: str = "") -> dict:
    """One full bidirectional cycle: pull B->A, then push A->B, advancing cursors only
    after each batch fully applies (ack-after-apply)."""
    moved = {}
    # --- pull: B's ops since A last saw them ---
    st = sj.peer_state(a.conn, b_name)
    b_gen = sj.generation(b.conn)
    if st["peer_generation"] and st["peer_generation"] != b_gen:
        moved["rebaseline"] = True
        st = {"pulled_through": 0}          # generation changed -> full re-baseline
    ops = sj.ops_since(b.conn, st["pulled_through"], scope=scope)
    if ops:
        sj.apply_ops(a.conn, ops, origin=b_gen)
        sj.set_peer_state(a.conn, b_name, peer_generation=b_gen,
                          pulled_through=ops[-1]["seq"])
    moved["pulled"] = len(ops)

    # --- push: A's ops since B last saw them ---
    st2 = sj.peer_state(b.conn, a_name)
    a_gen = sj.generation(a.conn)
    if st2["peer_generation"] and st2["peer_generation"] != a_gen:
        moved["rebaseline"] = True
        st2 = {"pulled_through": 0}
    ops2 = sj.ops_since(a.conn, st2["pulled_through"], scope=scope)
    if ops2:
        sj.apply_ops(b.conn, ops2, origin=a_gen)
        sj.set_peer_state(b.conn, a_name, peer_generation=a_gen,
                          pulled_through=ops2[-1]["seq"])
    moved["pushed"] = len(ops2)
    return moved


def fetch_events(store, atom_id):
    r = store.conn.execute(
        "SELECT score_history FROM atom_earned WHERE atom_id=?", (atom_id,)).fetchone()
    if not r:
        return []
    return [e for e in json.loads(r[0] or "[]") if len(e) > 2 and str(e[2]) == "fetch"]


def main():
    tmp = Path(tempfile.mkdtemp(prefix="twoway_"))
    print(f"\nTWO-WAY SYNC ROUND TRIP  ({tmp})")
    print("=" * 78)

    A = CardStore(tmp / "local.db")     # stands in for ~/.echelon/echelon.db
    B = CardStore(tmp / "cloud.db")     # stands in for the box4 member bank
    print(f"  bank A generation: {sj.generation(A.conn)[:12]}")
    print(f"  bank B generation: {sj.generation(B.conn)[:12]}")

    # ── 1. each side authors its own atom ────────────────────────────────────
    print("\n[1] each bank authors an atom of its own")
    a_id = A.add_atom("lesson:from-local", "# from-local\nauthored at the desk", scope="echelon")
    A.compile_atom_struct(a_id)
    b_id = B.add_atom("lesson:from-cloud", "# from-cloud\nauthored on claude.ai", scope="echelon")
    B.compile_atom_struct(b_id)
    # each earns on its own atom, through the real witnessed door
    A.remember_fetch(a_id, depth="body")
    B.remember_fetch(b_id, depth="body")

    moved = sync_once(A, B, "local", "cloud", scope="echelon")
    print(f"    cycle 1: pulled {moved['pulled']} ops, pushed {moved['pushed']} ops")

    check("cloud-authored atom reached local",
          A.conn.execute("SELECT COUNT(*) FROM atoms WHERE id=?", (b_id,)).fetchone()[0] == 1)
    check("local-authored atom reached cloud",
          B.conn.execute("SELECT COUNT(*) FROM atoms WHERE id=?", (a_id,)).fetchone()[0] == 1)
    # content must survive intact, not as a half-row
    row = A.conn.execute("SELECT content, scope, coordinate FROM atoms WHERE id=?", (b_id,)).fetchone()
    check("synced row carries full content", row and row[0] == "# from-cloud\nauthored on claude.ai",
          f"content={row[0][:30]!r}" if row else "row missing")
    check("synced row carries scope", row and row[1] == "echelon")

    # ── 2. earned weight from both sides ─────────────────────────────────────
    print("\n[2] the SAME atom earns on BOTH banks between syncs")
    shared = "# shared-lesson\nboth banks will read this one"
    s_local = A.add_atom("lesson:shared", shared, scope="echelon"); A.compile_atom_struct(s_local)
    s_cloud = B.add_atom("lesson:shared", shared, scope="echelon"); B.compile_atom_struct(s_cloud)
    check("content-addressed ids match across banks", s_local == s_cloud,
          f"{s_local[:12]} == {s_cloud[:12]}")

    for _ in range(3):
        A.remember_fetch(s_local, depth="spine")     # 3 witnessed reads at the desk
    for _ in range(2):
        B.remember_fetch(s_cloud, depth="spine")     # 2 witnessed reads on claude.ai

    sync_once(A, B, "local", "cloud", scope="echelon")

    ea, eb = fetch_events(A, s_local), fetch_events(B, s_cloud)
    check("bank A holds all 5 witnessed reads (3 local + 2 cloud)", len(ea) == 5, f"got {len(ea)}")
    check("bank B holds all 5 witnessed reads", len(eb) == 5, f"got {len(eb)}")
    uca = A.conn.execute("SELECT use_count FROM atom_earned WHERE atom_id=?", (s_local,)).fetchone()[0]
    ucb = B.conn.execute("SELECT use_count FROM atom_earned WHERE atom_id=?", (s_cloud,)).fetchone()[0]
    check("use_count derived consistently on both banks", uca == ucb == 5, f"A={uca} B={ucb}")

    # ── 3. convergence ───────────────────────────────────────────────────────
    print("\n[3] convergence — both banks agree after the round trip")
    ids_a = {r[0] for r in A.conn.execute("SELECT id FROM atoms WHERE scope='echelon'")}
    ids_b = {r[0] for r in B.conn.execute("SELECT id FROM atoms WHERE scope='echelon'")}
    check("both banks hold the same atom set", ids_a == ids_b,
          f"A={len(ids_a)} B={len(ids_b)} diff={len(ids_a ^ ids_b)}")
    ha = sorted(json.loads(A.conn.execute(
        "SELECT score_history FROM atom_earned WHERE atom_id=?", (s_local,)).fetchone()[0]))
    hb = sorted(json.loads(B.conn.execute(
        "SELECT score_history FROM atom_earned WHERE atom_id=?", (s_cloud,)).fetchone()[0]))
    check("earned history is identical on both banks", ha == hb)

    # ── 4/5. idempotence + no echo ───────────────────────────────────────────
    print("\n[4] idempotence + echo check — re-sync with no new work")
    head_a, head_b = sj.head(A.conn), sj.head(B.conn)
    moved2 = sync_once(A, B, "local", "cloud", scope="echelon")
    check("second cycle ships zero ops (nothing new to say)",
          moved2["pulled"] == 0 and moved2["pushed"] == 0,
          f"pulled={moved2['pulled']} pushed={moved2['pushed']}")
    check("applying produced no new journal ops on A (no echo)",
          sj.head(A.conn) == head_a, f"{head_a} -> {sj.head(A.conn)}")
    check("applying produced no new journal ops on B (no echo)",
          sj.head(B.conn) == head_b, f"{head_b} -> {sj.head(B.conn)}")
    ea2 = fetch_events(A, s_local)
    check("re-sync did not inflate earned weight", len(ea2) == 5, f"got {len(ea2)}")

    # ── 6. concurrent edits to the same atom ─────────────────────────────────
    print("\n[6] concurrent work on the same atom, then sync")
    for _ in range(4):
        A.remember_fetch(s_local, depth="spine")
    B.fire_lower(s_cloud, "misled me on the cloud side")
    for _ in range(2):
        B.remember_fetch(s_cloud, depth="spine")
    sync_once(A, B, "local", "cloud", scope="echelon")

    ea3, eb3 = fetch_events(A, s_local), fetch_events(B, s_cloud)
    check("all 11 witnessed reads survive on A (5 + 4 local + 2 cloud)", len(ea3) == 11, f"got {len(ea3)}")
    check("all 11 witnessed reads survive on B", len(eb3) == 11, f"got {len(eb3)}")
    fl_a = [e for e in json.loads(A.conn.execute(
        "SELECT score_history FROM atom_earned WHERE atom_id=?", (s_local,)).fetchone()[0])
        if len(e) > 2 and str(e[2]).startswith("fire_lower")]
    check("the cloud's fire_lower reached the local bank", len(fl_a) == 1, f"got {len(fl_a)}")

    # ── 7. scope isolation ───────────────────────────────────────────────────
    print("\n[7] a second estate must NOT cross the wire")
    secret = A.add_atom("lesson:client-secret", "# secret\nanother client's estate", scope="alpha-app")
    A.compile_atom_struct(secret)
    sync_once(A, B, "local", "cloud", scope="echelon")
    check("other-estate atom did NOT reach the cloud bank",
          B.conn.execute("SELECT COUNT(*) FROM atoms WHERE id=?", (secret,)).fetchone()[0] == 0)
    check("other-estate atom is still on the local bank",
          A.conn.execute("SELECT COUNT(*) FROM atoms WHERE id=?", (secret,)).fetchone()[0] == 1)

    # ── 8. restore detection ─────────────────────────────────────────────────
    print("\n[8] a whole-file restore must force a re-baseline, not a cursor replay")
    gen_before = sj.generation(B.conn)
    snap = tmp / "cloud_snapshot.db"
    # WAL-safe snapshot — a plain copy2 of a live WAL bank yields a table-less 4KB stub
    # (measured 2026-08-02; the real backup path uses this same serialize()).
    from echelon_engine.atoms.backup_cmd import _safe_file_copy
    _safe_file_copy(tmp / "cloud.db", snap)
    B.conn.close()
    shutil.copy2(snap, tmp / "cloud.db")
    for side in ("cloud.db-wal", "cloud.db-shm"):
        if (tmp / side).exists():
            (tmp / side).unlink()
    gen_after = sj.remint_generation(tmp / "cloud.db")
    B2 = CardStore(tmp / "cloud.db")
    check("restore re-minted the generation", gen_after != gen_before,
          f"{gen_before[:8]} -> {gen_after[:8]}")
    moved3 = sync_once(A, B2, "local", "cloud", scope="echelon")
    check("peer detected the rewind and re-baselined", moved3.get("rebaseline") is True)
    check("re-baseline replayed the full history without loss",
          len(fetch_events(B2, s_cloud)) == 11, f"got {len(fetch_events(B2, s_cloud))}")

    # ── receipt ──────────────────────────────────────────────────────────────
    print("\n" + "=" * 78)
    passed = sum(1 for r in results if r[0] == OK)
    failed = [r for r in results if r[0] == FAIL]
    print(f"  {passed}/{len(results)} checks passed")
    for _, name, detail in failed:
        print(f"    FAILED: {name}  {detail}")
    print(f"\n  journal sizes: A={sj.head(A.conn)} ops, B={sj.head(B2.conn)} ops")
    ja = A.conn.execute("SELECT SUM(LENGTH(payload)+LENGTH(pk)) FROM sync_journal").fetchone()[0] or 0
    print(f"  bank A journal payload: {ja/1024:.1f} KB for {sj.head(A.conn)} ops "
          f"(~{ja/max(1,sj.head(A.conn)):.0f} bytes/op)")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
