---
name: reflex-copy2-a-live-wal-bank
description: "REFLEX warn: copying a .db file with cp/copy2 — in WAL mode that saves a 4KB stub with NO TABLES; use the online-backup API"
metadata:
  node_type: memory
  type: feedback
  reflex: true
  reflex-tier: portable
  reflex-event: PreToolUse
  reflex-tool: Bash
  reflex-match: "(cp|copy|shutil[.]copy2?)[^|;&]*[.]db"
  reflex-action: warn
---

STOP — copying a `.db` file directly? The ECHELON bank runs in **WAL mode**, so while any
process holds it open the `.db` can be a **4KB stub with every byte of real data in the
sibling `-wal`**. A plain `cp` / `shutil.copy2` then yields a backup that raises
**`no such table: atoms`** — not stale, EMPTY. It looks fine only when the source happens to
be checkpointed (a clean close), i.e. NOT when another process holds the bank open — exactly
when you need the backup.

Use the online-backup API instead:
`sqlite3.connect(f"file:{src}?mode=ro", uri=True).backup(sqlite3.connect(dst))` — present on
every Python (`serialize()` is 3.11+ and absent on older installs). Or just run `echelon backup`,
which does the safe thing for you.

And after restoring an image over a bank file, **delete the stale `-wal`/`-shm` sidecars** or
SQLite replays them over the restored image and silently resurrects pre-restore state.

Copying a backup FILE that nobody holds open is fine. Measured on a live bank: 4,096-byte main
file vs 659,232-byte WAL.
