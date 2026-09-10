"""Role devices and ingest config must honor a subprocess's selected home."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


def _probe(code: str, env: dict) -> dict:
    result = subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=env, capture_output=True,
                            text=True, encoding="utf-8", check=True)
    return json.loads(result.stdout)


def _canaries(tmp_path):
    ambient = tmp_path / "ambient" / ".echelon" / "role_devices" / "reviewer"
    packaged = tmp_path / "packaged" / "reviewer"
    ambient.mkdir(parents=True); packaged.mkdir(parents=True)
    (ambient / "core.db").write_bytes(b"ambient")
    (packaged / "core.db").write_bytes(b"packaged")
    # Patch only in the child: test canaries never touch the installed SDK or owner home.
    setup = ("import echelon_sdk.roles as r; from pathlib import Path; "
             f"r._PKG_DEVICES=Path({str(packaged.parent)!r}); r.Path.home=classmethod(lambda cls: Path({str(tmp_path / 'ambient')!r})); ")
    return setup


def test_explicit_home_and_device_roots_refuse_ambient_and_packaged_canaries(tmp_path):
    setup = _canaries(tmp_path)
    query = "import json; print(json.dumps({'device': str(r.role_device_dir('reviewer')) if r.role_device_dir('reviewer') else None}))"
    home_env = {**os.environ, "ECHELON_HOME": str(tmp_path / "isolated")}
    home_env.pop("ECHELON_ROLE_DEVICES", None)
    assert _probe(setup + query, home_env) == {"device": None}
    device_env = {**home_env, "ECHELON_ROLE_DEVICES": str(tmp_path / "devices")}
    assert _probe(setup + query, device_env) == {"device": None}
    assert not (tmp_path / "isolated").exists() and not (tmp_path / "devices").exists()


def test_default_unconfigured_mode_retains_packaged_legacy_fallback(tmp_path):
    setup = _canaries(tmp_path)
    (tmp_path / "ambient" / ".echelon" / "role_devices" / "reviewer" / "core.db").unlink()
    env = dict(os.environ); env.pop("ECHELON_HOME", None); env.pop("ECHELON_ROLE_DEVICES", None)
    data = _probe(setup + "import json; print(json.dumps({'device': str(r.role_device_dir('reviewer'))}))", env)
    assert Path(data["device"]) == tmp_path / "packaged" / "reviewer"


def test_home_selects_its_own_role_device_and_ingest_config(tmp_path):
    home = tmp_path / "home"; device = home / "role_devices" / "dev"; device.mkdir(parents=True)
    (device / "core.db").write_bytes(b"")
    env = {**os.environ, "ECHELON_HOME": str(home)}
    code = "import json; from echelon_sdk.roles import role_device_dir; from echelon_engine.atoms.ingest import mem_dirs_config; print(json.dumps({'device':str(role_device_dir('dev')), 'config':mem_dirs_config()}))"
    data = _probe(code, env)
    assert Path(data["device"]) == device
    assert Path(data["config"]) == home / "mem_dirs.json"
