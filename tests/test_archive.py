"""test_archive — `echelon archive push|list|verify` (spec S8 V6).

The fake S3 is a dict-backed stub (no moto, no network): paginated
list_objects_v2, upload_file, get_object. boto3 is never imported.
"""
from __future__ import annotations

import io
import json
import sys
from datetime import date
from pathlib import Path

import pytest

from echelon_engine import archive, workcycle


class _FakeS3:
    """Dict-backed object store: {key: bytes}. get_paginator returns self so
    paginate() can be called directly (boto3's paginator protocol)."""

    def __init__(self, objects=None):
        self.objects = dict(objects or {})

    def get_paginator(self, _name):
        return self

    def paginate(self, Bucket=None, Prefix=""):
        keys = sorted(k for k in self.objects if k.startswith(Prefix))
        for i in range(0, len(keys), 2):  # 2-object pages exercise pagination
            yield {"Contents": [{"Key": k, "Size": len(self.objects[k])} for k in keys[i:i + 2]]}

    def upload_file(self, path, _bucket, key):
        self.objects[key] = Path(path).read_bytes()

    def get_object(self, Bucket=None, Key=None):
        return {"Body": io.BytesIO(self.objects[Key])}


def _make_src(tmp_path, files: dict) -> Path:
    src = tmp_path / "src"
    src.mkdir()
    for name, data in files.items():
        (src / name).write_bytes(data)
    return src


def test_push_writes_manifest_with_sha256_and_size(tmp_path):
    src = _make_src(tmp_path, {"a.bin": b"a" * 100, "b.bin": b"b" * 50})
    res = archive.cmd_push(src, "p", min_mb=0, client=_FakeS3())
    assert res["uploaded"] == 2 and res["missing"] == 0
    manifest = json.loads((src / "MANIFEST.sha256.json").read_text(encoding="utf-8"))
    assert manifest["prefix"] == "p/" and manifest["bucket"] == "echelon"
    assert manifest["objects"]["p/a.bin"] == {"sha256": archive._sha256(src / "a.bin"), "size": 100}


def test_push_skips_subthreshold_unless_all(tmp_path):
    src = _make_src(tmp_path, {"big.bin": b"x" * 300, "small.bin": b"y" * 10})
    fake = _FakeS3()
    # 0.0003 MB == 300 bytes: big.bin (300B) passes, small.bin (10B) is skipped
    res = archive.cmd_push(src, "p", min_mb=0.0003, client=fake)
    assert res["uploaded"] == 1 and res["skipped_under_threshold"] == 1
    assert "p/big.bin" in fake.objects and "p/small.bin" not in fake.objects
    res = archive.cmd_push(src, "q", min_mb=0.0003, all_=True, client=fake)
    assert res["uploaded"] == 2 and res["skipped_under_threshold"] == 0
    assert "q/small.bin" in fake.objects


def test_push_second_run_uploads_zero(tmp_path):
    src = _make_src(tmp_path, {"a.bin": b"a" * 100})
    fake = _FakeS3()
    first = archive.cmd_push(src, "p", min_mb=0, client=fake)
    second = archive.cmd_push(src, "p", min_mb=0, client=fake)
    assert first["uploaded"] == 1 and second["uploaded"] == 0 and second["missing"] == 0


def test_verify_catches_size_mismatch(tmp_path, monkeypatch):
    fake = _FakeS3({"p/f.txt": b"x" * 10,
                    "p/MANIFEST.sha256.json": json.dumps(
                        {"objects": {"p/f.txt": {"sha256": "deadbeef", "size": 999}}}).encode()})
    res = archive.cmd_verify("p", client=fake)
    assert res["ok"] == 0 and res["missing"] == 0 and res["mismatch"] == 1
    monkeypatch.setattr(archive, "_client_factory", lambda: fake)  # main() has no client kwarg
    assert archive.main(["verify", "p"]) == 1  # exit 1 on any mismatch


def test_dry_run_uploads_nothing_and_writes_no_manifest(tmp_path):
    src = _make_src(tmp_path, {"a.bin": b"a" * 100})
    fake = _FakeS3()
    res = archive.cmd_push(src, "p", min_mb=0, dry_run=True, client=fake)
    assert res["dry_run"] and res["uploaded"] == 0
    assert fake.objects == {} and not (src / "MANIFEST.sha256.json").exists()


def test_push_journals_receipt_into_room(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    workcycle.init(root, estate="t", type_="engine")
    monkeypatch.chdir(root)
    src = _make_src(tmp_path, {"a.bin": b"a" * 200})
    archive.cmd_push(src, "p", min_mb=0, client=_FakeS3())
    lines = (root / ".echelon" / "journal" / f"{date.today().isoformat()}.jsonl").read_text(
        encoding="utf-8").splitlines()
    receipt = json.loads(lines[-1])
    assert receipt["kind"] == "receipt" and receipt["archive"] == "push"
    assert receipt["prefix"] == "p/" and receipt["uploaded"] == 1
    assert receipt["verified"] == 1 and receipt["missing"] == 0 and receipt["bytes"] == 200


def test_module_imports_without_boto3(monkeypatch):
    monkeypatch.setitem(sys.modules, "boto3", None)
    monkeypatch.delitem(sys.modules, "echelon_engine.archive", raising=False)
    import importlib
    mod = importlib.import_module("echelon_engine.archive")
    assert mod.BUCKET == "echelon"


def test_credentials_never_appear_in_stdout(tmp_path, monkeypatch, capsys):
    home = tmp_path / "home"
    home.mkdir()
    (home / "bucket.txt").write_text("https://s3.example.com:443\nAKID-USER\nSECRET-ABC123\n",
                                     encoding="utf-8")
    monkeypatch.setenv("ECHELON_HOME", str(home))
    seen = {}

    class _FakeBoto3:
        def client(self, *a, **kw):
            seen.update(kw)
            return _FakeS3()

    monkeypatch.setitem(sys.modules, "boto3", _FakeBoto3())
    monkeypatch.setitem(sys.modules, "botocore",
                        type("botocore", (), {"config": type("cfg", (), {"Config": lambda **k: k})})())
    src = _make_src(tmp_path, {"a.bin": b"a" * 100})
    archive.cmd_push(src, "p", min_mb=0, client=None)  # None -> real _client_factory path
    out = capsys.readouterr().out
    assert "SECRET-ABC123" not in out and "AKID-USER" not in out
    assert seen.get("aws_secret_access_key") == "SECRET-ABC123"  # creds DID reach the client


def test_list_prints_keys_sizes_and_total(tmp_path, capsys):
    fake = _FakeS3({"p/a.bin": b"x" * 3, "p/b.bin": b"y" * 7, "z.bin": b"z"})
    res = archive.cmd_list("p", client=fake)
    assert res == {"objects": 2, "bytes": 10}
    out = capsys.readouterr().out
    assert "p/a.bin" in out and "p/b.bin" in out and "z.bin" not in out
    assert "total 2 objects · 10 bytes" in out


def test_usage_returns_2(capsys):
    assert archive.main([]) == 2 and archive.main(["bogus"]) == 2
    assert archive.main(["push", "only"]) == 2


def test_default_threshold_push_does_not_report_skips_as_missing(tmp_path):
    """Gate r1 V6 M-1: at the real default (100 MB) every small file is skipped — skipped files
    are COUNTED, never listed in the manifest, so verify cannot mark them MISSING."""
    src = _make_src(tmp_path, {"small.bin": b"y" * 10, "tiny.bin": b"z"})
    res = archive.cmd_push(src, "p", client=_FakeS3())
    assert res["skipped_under_threshold"] == 2 and res["missing"] == 0 and res["uploaded"] == 0
    manifest = json.loads((src / "MANIFEST.sha256.json").read_text(encoding="utf-8"))
    assert manifest["objects"] == {} and manifest["skipped_under_threshold"] == 2


def test_missing_or_malformed_bucket_txt_is_a_clear_error(tmp_path, monkeypatch, capsys):
    """Gate r1 V6 S-1/S-2: no traceback, no content — the path and the expected shape only."""
    import pytest
    monkeypatch.setenv("ECHELON_HOME", str(tmp_path))
    with pytest.raises(SystemExit) as e:
        archive._client_factory()
    assert "no credentials" in str(e.value) and "3 lines" in str(e.value)
    (tmp_path / "bucket.txt").write_text("host:only\nSECRETKEY\n", encoding="utf-8")
    with pytest.raises(SystemExit) as e:
        archive._client_factory()
    assert "malformed" in str(e.value) and "SECRETKEY" not in str(e.value)
