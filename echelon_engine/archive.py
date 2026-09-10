"""archive — `echelon archive push|list|verify` (spec S8 V6, charter R5/R6).

The bucket mirrors what is too big for the git ARCHIVE (R5): `push` uploads
files >= --min-mb under a prefix, writes MANIFEST.sha256.json locally AND to
the bucket, then size-verifies every manifest key against a fresh listing
BEFORE the receipt is journaled. Credentials live in <home>/bucket.txt (R6)
and are never printed, logged, or journaled. boto3 is imported LAZILY inside
the client factory — this module and its tests import with no boto3.
"""
from __future__ import annotations

import hashlib
import json
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from echelon_engine import workcycle
from echelon_engine.atoms.echelon_home import echelon_home

BUCKET = "echelon"


def _client_factory():
    """Lazy boto3 client for the `echelon` bucket — creds from <home>/bucket.txt
    (line 1 `...:<host>`, line 2 key, line 3 secret). The import lives here so
    the module and every test import with no boto3 installed."""
    cred = echelon_home() / "bucket.txt"
    try:
        lines = [l.strip() for l in cred.read_text(encoding="utf-8").splitlines() if l.strip()]
    except OSError as exc:
        raise SystemExit(f"archive: no credentials at {cred} ({exc.__class__.__name__}) — "
                         f"expected 3 lines: `...:<host>`, key, secret (charter R6)") from None
    if len(lines) < 3:
        raise SystemExit(f"archive: {cred} is malformed ({len(lines)} non-empty lines; "
                         f"expected 3: `...:<host>`, key, secret) — content never printed")
    try:
        import boto3
        import botocore
    except ImportError:
        raise SystemExit("archive: optional archive dependencies unavailable; install echelon[archive] in the selected runtime") from None
    host = lines[0].split(":")[-1].strip()
    cfg = botocore.config.Config(signature_version="s3v4", connect_timeout=10,
                                 read_timeout=60, retries={"max_attempts": 3},
                                 max_pool_connections=16)
    return boto3.client("s3", endpoint_url="https://" + host,
                        aws_access_key_id=lines[1], aws_secret_access_key=lines[2],
                        region_name="ap-south-1", config=cfg)


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _list_sizes(client, prefix: str) -> dict:
    """{key: size} for every object under prefix (paginated)."""
    sizes = {}
    for page in client.get_paginator("list_objects_v2").paginate(Bucket=BUCKET, Prefix=prefix):
        for o in page.get("Contents", []):
            sizes[o["Key"]] = o["Size"]
    return sizes


def cmd_push(local_dir, prefix, min_mb=100, all_=False, dry_run=False, client=None):
    """Push local files >= min_mb under prefix (idempotent: a key whose remote
    size already matches is skipped); write the manifest locally + remotely,
    then size-verify every manifest key BEFORE the receipt. Returns a summary
    dict — `missing > 0` means the CLI exits 1 with MISSING lines."""
    local = Path(local_dir).resolve()
    prefix = prefix.strip("/") + "/"
    s3 = client or _client_factory()
    remote = _list_sizes(s3, prefix)
    min_b = float(min_mb) * 1_000_000
    mpath = local / "MANIFEST.sha256.json"
    # the manifest never lists itself (a stale self-entry on re-runs is the
    # seed's defect); it is written + uploaded after the scan either way
    files = sorted(p for p in local.rglob("*") if p.is_file() and p != mpath)
    todo, skipped, manifest = [], 0, {}
    for p in files:
        size = p.stat().st_size
        key = prefix + p.relative_to(local).as_posix()
        if not all_ and size < min_b:
            skipped += 1  # counted, never listed: the manifest is what the bucket holds (gate r1 V6 M-1)
            continue
        manifest[key] = {"sha256": _sha256(p), "size": size}
        if remote.get(key) != size:
            todo.append((p, key))
    print(f"local files {len(files)} · to upload {len(todo)} · "
          f"skipped_under_threshold {skipped} · {sum(p.stat().st_size for p, _ in todo) / 1e6:.1f} MB")
    if dry_run:
        return {"uploaded": 0, "verified": 0, "missing": 0, "bytes": 0,
                "skipped_under_threshold": skipped, "dry_run": True}

    def up(item):
        p, key = item
        s3.upload_file(str(p), BUCKET, key)
        return key

    done = 0
    with ThreadPoolExecutor(max_workers=12) as ex:
        for _ in ex.map(up, todo):
            done += 1
            if done % 200 == 0:
                print(f"  {done}/{len(todo)} …", flush=True)
    total_bytes = sum(p.stat().st_size for p, _ in todo)
    mpath.write_text(json.dumps({"prefix": prefix, "bucket": BUCKET,
                                 "pushed_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                                 "objects": manifest,
                                 "skipped_under_threshold": skipped}, indent=1),
                     encoding="utf-8")
    s3.upload_file(str(mpath), BUCKET, prefix + "MANIFEST.sha256.json")
    remote = _list_sizes(s3, prefix)  # fresh listing — verify BEFORE the receipt
    missing = [k for k, v in manifest.items() if remote.get(k) != v["size"]]
    for k in missing:
        print("MISSING", k)
    _journal_push_receipt(prefix, uploaded=done, verified=len(manifest) - len(missing),
                          missing=len(missing), bytes_=total_bytes)
    print(f"uploaded {done} · verified {len(manifest) - len(missing)}/{len(manifest)}"
          f" · missing {len(missing)} · manifest {mpath.name}")
    return {"uploaded": done, "verified": len(manifest) - len(missing),
            "missing": len(missing), "bytes": total_bytes,
            "skipped_under_threshold": skipped}


def _journal_push_receipt(prefix, uploaded, verified, missing, bytes_):
    """Journal the push receipt in the current room; a room-less cwd prints a
    warning and continues — NEVER blocks the turn."""
    room = workcycle.room_path()
    if room is None:
        print("warning: no room in this cwd — push receipt not journaled")
        return
    workcycle.journal(room, "receipt", {"archive": "push", "prefix": prefix,
                                        "uploaded": uploaded, "verified": verified,
                                        "missing": missing, "bytes": bytes_})


def cmd_list(prefix, client=None):
    """Keys + sizes under prefix (paginated), total at the end."""
    prefix = prefix.strip("/") + "/"
    s3 = client or _client_factory()
    sizes = _list_sizes(s3, prefix)
    for key in sorted(sizes):
        print(f"{sizes[key]:>12}  {key}")
    print(f"total {len(sizes)} objects · {sum(sizes.values())} bytes")
    return {"objects": len(sizes), "bytes": sum(sizes.values())}


def cmd_verify(prefix, client=None):
    """Download the remote MANIFEST.sha256.json and compare every entry's size
    to the live listing — ok/missing/mismatch; exit 1 on any. This is a
    PRESENCE + SIZE check, not a content check: the manifest's sha256 is
    recorded for a future content audit but never compared remotely (gate r1
    V6 S-3) — a same-length corruption passes `verify`."""
    prefix = prefix.strip("/") + "/"
    s3 = client or _client_factory()
    try:
        body = s3.get_object(Bucket=BUCKET, Key=prefix + "MANIFEST.sha256.json")["Body"]
    except Exception as exc:
        print(f"archive verify: cannot read remote manifest: {exc}")
        return {"ok": 0, "missing": 0, "mismatch": 0, "error": str(exc)}
    manifest = json.loads(body.read().decode("utf-8"))
    objects = manifest.get("objects", {})
    remote = _list_sizes(s3, prefix)
    missing = [k for k in objects if k not in remote]
    mismatch = [k for k, v in objects.items() if k in remote and remote[k] != v["size"]]
    ok = len(objects) - len(missing) - len(mismatch)
    for k in missing:
        print("MISSING", k)
    for k in mismatch:
        print("MISMATCH", k)
    print(f"verified {ok} ok · {len(missing)} missing · {len(mismatch)} mismatch")
    return {"ok": ok, "missing": len(missing), "mismatch": len(mismatch)}


def main(argv):
    if not argv or argv[0] not in ("push", "list", "verify"):
        print("usage: echelon archive push|list|verify …")
        return 2
    cmd, rest = argv[0], argv[1:]
    if cmd == "push":
        if len(rest) < 2:
            print("usage: echelon archive push <local_dir> <prefix> [--min-mb N] [--all] [--dry-run]")
            return 2
        min_mb, all_, dry = 100, False, False
        i = 2
        while i < len(rest):
            if rest[i] == "--min-mb" and i + 1 < len(rest):
                min_mb = float(rest[i + 1])
                i += 2
            elif rest[i] == "--all":
                all_ = True
                i += 1
            elif rest[i] == "--dry-run":
                dry = True
                i += 1
            else:
                print(f"archive: unknown option {rest[i]}")
                return 2
        res = cmd_push(rest[0], rest[1], min_mb=min_mb, all_=all_, dry_run=dry)
        return 1 if res.get("missing") else 0
    if not rest:
        print(f"usage: echelon archive {cmd} <prefix>")
        return 2
    if cmd == "list":
        cmd_list(rest[0])
        return 0
    res = cmd_verify(rest[0])
    return 1 if (res.get("missing") or res.get("mismatch")) else 0
