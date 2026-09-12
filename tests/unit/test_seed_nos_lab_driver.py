"""Unit tests for the NOS test lab seed builder (seedtools.nos_lab, ADR 0010 phase
3a).

Pure, stack-free tests: they import the seed package and exercise
_make_driver_zip_from_dir directly against the two real, checked-in driver
packages (drivers/srl_l2, drivers/frr_l3), mirroring
test_seed_frr_driver.py's approach for drivers/frr_mgmt. Guards that both
on-disk packages stay packageable, so a future edit to either driver cannot
silently break the seed without a red test here.
"""

import io
import zipfile
from pathlib import Path

import pytest

from seedtools import drivers, nos_lab

_REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("dir_attr", ["SRL_L2_DRIVER_DIR", "FRR_L3_DRIVER_DIR"])
def test_driver_dir_points_at_the_real_package(dir_attr):
    driver_dir = Path(getattr(nos_lab, dir_attr))
    assert (driver_dir / "driver.py").is_file()
    assert (driver_dir / "driver_metadata.json").is_file()


@pytest.mark.parametrize("dir_attr", ["SRL_L2_DRIVER_DIR", "FRR_L3_DRIVER_DIR"])
def test_make_driver_zip_from_dir_packages_exactly_the_two_files(dir_attr):
    data = drivers._make_driver_zip_from_dir(getattr(nos_lab, dir_attr))
    assert data is not None, "the real driver package must exist in the repo"
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        names = set(zf.namelist())
    # Exactly the driver and its metadata: no __pycache__, no cruft, so the
    # SHA256 cache key the execution service computes is stable across runs.
    assert names == {"driver.py", "driver_metadata.json"}


@pytest.mark.parametrize(
    ("dir_attr", "subdir"),
    [("SRL_L2_DRIVER_DIR", "srl_l2"), ("FRR_L3_DRIVER_DIR", "frr_l3")],
)
def test_zipped_driver_matches_source_on_disk(dir_attr, subdir):
    data = drivers._make_driver_zip_from_dir(getattr(nos_lab, dir_attr))
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        zipped = zf.read("driver.py").decode("utf-8")
    on_disk = (_REPO_ROOT / "drivers" / subdir / "driver.py").read_text(encoding="utf-8")
    # Zipped-from-disk means the seeded package can never drift from the source.
    assert zipped == on_disk


def test_make_driver_zip_from_dir_returns_none_when_package_missing(tmp_path):
    # A checkout without a driver package must degrade gracefully (warn +
    # skip), not raise, so the rest of the seed still runs.
    assert drivers._make_driver_zip_from_dir(str(tmp_path / "nonexistent")) is None


def test_seed_nos_lab_skips_cleanly_when_a_package_is_missing(monkeypatch, tmp_path):
    # seed_nos_lab must bail out (with a warning already printed by the zip
    # builder) rather than crash when either driver package is absent, the
    # same "warn and skip" contract seed_frr_demo follows.
    monkeypatch.setattr(nos_lab, "FRR_L3_DRIVER_DIR", str(tmp_path / "nonexistent"))
    calls = {"n": 0}

    class _FakeClient:
        def get(self, url, params=None):
            calls["n"] += 1
            raise AssertionError("seed_nos_lab must return before making any HTTP call")

        def post(self, url, **kwargs):
            calls["n"] += 1
            raise AssertionError("seed_nos_lab must return before making any HTTP call")

    nos_lab.seed_nos_lab(_FakeClient())
    assert calls["n"] == 0
