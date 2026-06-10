"""Unit tests for the FRR live-config demo seed builder.

These are pure, stack-free tests: they load the seed module by path and exercise
_make_frr_driver_zip directly. The builder zips the REAL drivers/frr_mgmt/ package
from disk (not an inline stub), so these tests also guard that the on-disk driver
stays packageable and keeps advertising dry-run support, which the live-config demo
relies on.
"""

import importlib.util
import io
import json
import sys
import zipfile
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SEED_PATH = _REPO_ROOT / "seed_devices_public.py"


@pytest.fixture(scope="module")
def seed():
    spec = importlib.util.spec_from_file_location("seed_devices_public", _SEED_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["seed_devices_public"] = module
    spec.loader.exec_module(module)
    return module


def test_frr_driver_dir_points_at_the_real_package(seed):
    # The builder must target the checked-in driver package, not a temp/inline copy.
    frr_dir = Path(seed.FRR_DRIVER_DIR)
    assert frr_dir == _REPO_ROOT / "drivers" / "frr_mgmt"
    assert (frr_dir / "driver.py").is_file()
    assert (frr_dir / "driver_metadata.json").is_file()


def test_make_frr_driver_zip_packages_exactly_the_two_files(seed):
    data = seed._make_frr_driver_zip()
    assert data is not None, "the real driver package must exist in the repo"
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        names = set(zf.namelist())
    # Exactly the driver and its metadata: no __pycache__, no README cruft, so the
    # SHA256 cache key the execution service computes is stable across seed runs.
    assert names == {"driver.py", "driver_metadata.json"}


def test_zipped_driver_matches_source_on_disk(seed):
    data = seed._make_frr_driver_zip()
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        zipped = zf.read("driver.py").decode("utf-8")
    on_disk = (_REPO_ROOT / "drivers" / "frr_mgmt" / "driver.py").read_text(encoding="utf-8")
    # Zipped-from-disk means the seeded package can never drift from the source.
    assert zipped == on_disk


def test_zipped_metadata_advertises_dry_run(seed):
    data = seed._make_frr_driver_zip()
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        meta = json.loads(zf.read("driver_metadata.json"))
    # supports_dry_run is binding (docs/DRIVERS.md): the dry-run live-config demo
    # relies on it, so a regression that flips it off must fail here.
    assert meta["supports_dry_run"] is True


def test_make_frr_driver_zip_returns_none_when_package_missing(seed, monkeypatch, tmp_path):
    # A checkout without the driver package must degrade gracefully (warn + skip),
    # not raise, so the rest of the seed still runs.
    monkeypatch.setattr(seed, "FRR_DRIVER_DIR", str(tmp_path / "nonexistent"))
    assert seed._make_frr_driver_zip() is None
