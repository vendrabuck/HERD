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


def test_zipped_driver_publishes_commands_config_schema(seed):
    # The live-config demo depends on the driver publishing a config_schema()
    # that accepts {commands: [...]}. Load the packaged driver and assert the
    # schema is present and accepts raw vtysh lines, so a regression that drops
    # the schema (which would re-break the configure boundary) fails here.
    import jsonschema

    data = seed._make_frr_driver_zip()
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        src = zf.read("driver.py").decode("utf-8")
    ns: dict = {}
    exec(compile(src, "frr_driver.py", "exec"), ns)
    schema = ns["Driver"].config_schema()
    jsonschema.Draft202012Validator.check_schema(schema)
    jsonschema.validate({"commands": ["ip route 192.0.2.0/24 blackhole"]}, schema)


class _FakeResp:
    def __init__(self, status_code, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = text

    def json(self):
        return self._payload


def test_get_or_create_driver_replaces_file_when_exists(seed):
    # When the driver already exists (409) and replace_if_exists is set, the seed
    # must PUT the new package file so an updated driver.py reaches inventory.
    existing_id = "11111111-1111-1111-1111-111111111111"
    put_calls = []

    class _FakeClient:
        def post(self, url, data=None, files=None):
            # Simulate "already exists".
            return _FakeResp(409, text="exists")

        def get(self, url, params=None):
            return _FakeResp(
                200, {"items": [{"name": "FRRouting Management Driver", "id": existing_id}]}
            )

        def put(self, url, files=None):
            put_calls.append(url)
            return _FakeResp(200, {"id": existing_id})

    did = seed.get_or_create_driver(
        _FakeClient(),
        "FRRouting Management Driver",
        "Management",
        zip_bytes=b"PK-fake-zip",
        replace_if_exists=True,
    )
    assert did == existing_id
    assert put_calls == [f"{seed.BASE}/inventory/drivers/{existing_id}/file"]


def test_get_or_create_driver_no_replace_by_default(seed):
    # Without replace_if_exists, an existing driver is returned untouched (no PUT),
    # so the heavy default seed never churns stable packages.
    existing_id = "22222222-2222-2222-2222-222222222222"
    put_calls = []

    class _FakeClient:
        def post(self, url, data=None, files=None):
            return _FakeResp(409, text="exists")

        def get(self, url, params=None):
            return _FakeResp(200, {"items": [{"name": "Some Driver", "id": existing_id}]})

        def put(self, url, files=None):
            put_calls.append(url)
            return _FakeResp(200, {"id": existing_id})

    did = seed.get_or_create_driver(
        _FakeClient(), "Some Driver", "Management", zip_bytes=b"PK-fake-zip"
    )
    assert did == existing_id
    assert put_calls == []


# --- FRR topology + reservation seeding -------------------------------------


def test_seed_frr_topology_builds_two_node_chain(seed):
    # The demo topology must be the two routers wired as a single L3 link, with
    # full device data on each node (so it renders like a hand-built canvas).
    r1, r2 = "aaaa1111-0000-0000-0000-000000000001", "aaaa1111-0000-0000-0000-000000000002"
    created = {}

    class _FakeClient:
        def get(self, url, params=None):
            if url.endswith(f"/inventory/devices/{r1}"):
                return _FakeResp(200, {"id": r1, "name": "frr-r1", "status": "RESERVED"})
            if url.endswith(f"/inventory/devices/{r2}"):
                return _FakeResp(200, {"id": r2, "name": "frr-r2", "status": "RESERVED"})
            if url.endswith("/cabling/topologies"):
                return _FakeResp(200, {"items": [], "total": 0, "skip": 0})
            return _FakeResp(404)

        def post(self, url, json=None):
            created["name"] = json["name"]
            return _FakeResp(201, {"id": "topo-id"})

        def put(self, url, json=None):
            created["canvas"] = json["canvas_data"]
            return _FakeResp(200, {"id": "topo-id"})

    tid = seed.seed_frr_topology(_FakeClient(), [r1, r2])
    assert tid == "topo-id"
    assert created["name"] == "FRR Live-Config Demo"
    canvas = created["canvas"]
    assert len(canvas["nodes"]) == 2
    assert len(canvas["edges"]) == 1  # single chain link r1 -- r2
    assert canvas["edges"][0]["data"]["layer"] == "L3"
    # node data carries the real device name, not a bare id
    assert canvas["nodes"][0]["data"]["label"] == "frr-r1"


def test_wait_for_active_returns_when_active(seed, monkeypatch):
    # Poll loop: returns as soon as the reservation reports ACTIVE.
    monkeypatch.setattr(seed.time, "sleep", lambda *_: None)
    states = iter(["PENDING_PROVISION", "PENDING_PROVISION", "ACTIVE"])

    class _FakeClient:
        def get(self, url, params=None):
            return _FakeResp(200, {"status": next(states)})

    assert seed._wait_for_active(_FakeClient(), "res-1", timeout_s=10) == "res-1"


def test_wait_for_active_stops_on_terminal_state(seed, monkeypatch):
    # A FAILED/CANCELLED reservation stops the wait early (does not spin to timeout).
    monkeypatch.setattr(seed.time, "sleep", lambda *_: None)
    calls = {"n": 0}

    class _FakeClient:
        def get(self, url, params=None):
            calls["n"] += 1
            return _FakeResp(200, {"status": "FAILED"})

    res = seed._wait_for_active(_FakeClient(), "res-2", timeout_s=10)
    assert res == "res-2"
    assert calls["n"] == 1  # returned on the first terminal read, no spinning


def test_ensure_frr_reservation_reuses_bound_one(seed, monkeypatch):
    # Idempotency: an existing ACTIVE reservation that already BINDS our topology
    # is reused, not duplicated, so a re-seed does not stack reservations.
    monkeypatch.setattr(seed.time, "sleep", lambda *_: None)
    r1 = "bbbb2222-0000-0000-0000-000000000001"
    posted = {"n": 0}

    class _FakeClient:
        def get(self, url, params=None):
            if url.endswith("/reservations/"):
                return _FakeResp(
                    200,
                    {
                        "items": [
                            {
                                "id": "res-existing",
                                "status": "ACTIVE",
                                "device_ids": [r1],
                                "topology_id": "t1",
                            }
                        ]
                    },
                )
            return _FakeResp(200, {"status": "ACTIVE"})  # _wait_for_active fetch

        def post(self, url, json=None):
            posted["n"] += 1
            return _FakeResp(201, {"id": "res-new", "status": "PENDING_PROVISION"})

    res = seed.ensure_frr_reservation(_FakeClient(), [r1], topology_id="t1")
    assert res == "res-existing"
    assert posted["n"] == 0  # reused, never created a second reservation


def test_ensure_frr_reservation_cancels_unbound_and_recreates_bound(seed, monkeypatch):
    # A live reservation over r1 that is NOT bound to our topology gets cancelled
    # and replaced with a topology-bound one, so the demo always ends up with a
    # bound reservation (the editable fork needs the binding).
    monkeypatch.setattr(seed.time, "sleep", lambda *_: None)
    r1, r2 = "bbbb2222-0000-0000-0000-000000000001", "bbbb2222-0000-0000-0000-000000000002"
    deleted, posted = [], []

    class _FakeClient:
        def get(self, url, params=None):
            if url.endswith("/reservations/"):
                # unbound live reservation (topology_id is None)
                return _FakeResp(
                    200,
                    {
                        "items": [
                            {
                                "id": "res-stale",
                                "status": "ACTIVE",
                                "device_ids": [r1, r2],
                                "topology_id": None,
                            }
                        ]
                    },
                )
            return _FakeResp(200, {"status": "ACTIVE"})

        def delete(self, url):
            deleted.append(url)
            return _FakeResp(204)

        def post(self, url, json=None):
            posted.append(json)
            return _FakeResp(201, {"id": "res-bound", "status": "ACTIVE"})

    res = seed.ensure_frr_reservation(_FakeClient(), [r1, r2], topology_id="t1")
    assert res == "res-bound"
    assert any("res-stale" in u for u in deleted)  # cancelled the stale one
    assert posted and posted[0]["topology_id"] == "t1"  # recreated bound
