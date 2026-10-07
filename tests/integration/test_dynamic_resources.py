"""Integration tests for dynamic resources (ADR 0004, issue #32) end to end.

A dynamic reservation books through PENDING_PROVISION; the execution consumer
handles reservation.provision_requested by running the Hypervisor recipe
(login, create_instance, logout) in the sandbox, materializing the result as a
RESERVED / CLOUD inventory device via the internal create endpoint, and posting
the provision-result callback that activates the reservation. Teardown drives
from the dynamic_instances ledger on the lifecycle events.

The suite self-seeds everything through the public APIs, per the integration
convention: a secret (secrets service), a hypervisor referencing it
(inventory), the checked-in drivers/mock_hypervisor recipe package, dynamic
templates bound to hypervisor + driver, and the physical DUT the booking needs
(the conftest fresh_device). Names are unique per run, so the suite is
re-runnable against the same stack.

Failure injection rides the template field DEFAULTS: a dynamic instance has no
device row when the recipe runs, so nats_consumer._build_recipe_context feeds
the recipe the template defaults as HERD_<field> keys; a template whose
mock_fail_actions field defaults to "create_instance" makes every instance of
it fail to create. A driver-reported create failure NAKs through max_deliver=5
redeliveries, each delayed by the consumer's NATS_NAK_BACKOFF_SECONDS schedule
(issue #895): the PRODUCTION default is [1, 5, 15, 60, 120]s, needing roughly
90 seconds of wall clock before the DLQ exhaustion posts the failure callback,
but docker-compose.override.yml (the dev/test stack this suite actually runs
against) pins a short override schedule so this finishes in a few seconds; the
failure test's timeouts are sized generously enough to cover either.

The redelivery test reaches NATS directly from the host (NATS_URL_HOST) and
skips when unreachable, mirroring test_dlq_and_idempotency.py.
"""

import asyncio
import io
import json
import tarfile
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest
from _nats_helpers import (
    fetch_reservation_event,
    find_in_execution_dlq,
    probe_nats,
    publish_raw,
)

from .conftest import _psql

pytestmark = pytest.mark.asyncio

_MOCK_HV_DIR = Path(__file__).resolve().parents[2] / "drivers" / "mock_hypervisor"
_PROVISION_SUBJECT = "herd.reservations.provision_requested"


def _mock_hv_tarball() -> bytes:
    """Package the checked-in drivers/mock_hypervisor package into a .tar.gz."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name in ("driver.py", "driver_metadata.json"):
            tf.add(_MOCK_HV_DIR / name, arcname=name)
    return buf.getvalue()


# A structurally broken recipe: valid archive and a driver.py that imports
# cleanly, but with NO class named Driver. Inventory does not validate package
# structure on upload (only filename/type/size), so this seeds fine; the
# execution service's load_driver rejects it at validation time, which the
# consumer classifies as a permanent, first-delivery DLQ (issue #279).
_BROKEN_RECIPE_PY = "class NotADriver:\n    pass\n"


def _broken_recipe_tarball() -> bytes:
    """Build a .tar.gz recipe package whose driver.py defines no Driver class."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name, content in (
            ("driver.py", _BROKEN_RECIPE_PY),
            ("driver_metadata.json", json.dumps({"supports_dry_run": False})),
        ):
            data = content.encode()
            info = tarfile.TarInfo(name=name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def _admin_session_client(base_url, admin_token):
    return httpx.AsyncClient(
        base_url=base_url,
        verify=False,
        timeout=30.0,
        headers={"Authorization": f"Bearer {admin_token}"},
    )


# --- self-seeding session fixtures -------------------------------------------


@pytest.fixture(scope="session")
async def hv_secret(base_url, admin_token):
    """A secrets-service credential for the mock hypervisor."""
    async with _admin_session_client(base_url, admin_token) as client:
        resp = await client.post(
            "/secrets/secrets",
            json={
                "name": f"int-hv-secret-{uuid.uuid4().hex[:8]}",
                "type": "password",
                "description": "dynamic-resources integration hypervisor credential",
                "data": {"username": "svc", "password": "integration-hv-password"},
            },
        )
        resp.raise_for_status()
        secret = resp.json()
        yield secret
        await client.delete(f"/secrets/secrets/{secret['id']}")


@pytest.fixture(scope="session")
async def hv_driver(base_url, admin_token):
    """Upload the mock Hypervisor recipe package once per session."""
    async with _admin_session_client(base_url, admin_token) as client:
        files = {"file": ("mock_hypervisor.tar.gz", _mock_hv_tarball(), "application/gzip")}
        data = {
            "name": f"mock-hv-{uuid.uuid4().hex[:8]}",
            "connection_type": "Hypervisor",
            "description": "integration mock hypervisor recipe driver",
        }
        resp = await client.post("/inventory/drivers", files=files, data=data)
        resp.raise_for_status()
        driver = resp.json()
        assert driver["connection_type"] == "Hypervisor"
        yield driver
        await client.delete(f"/inventory/drivers/{driver['id']}")


@pytest.fixture(scope="session")
async def hypervisor(base_url, admin_token, hv_secret):
    """A registered hypervisor referencing the secret."""
    async with _admin_session_client(base_url, admin_token) as client:
        resp = await client.post(
            "/inventory/hypervisors",
            json={
                "name": f"int-mock-hv-{uuid.uuid4().hex[:8]}",
                "description": "dynamic-resources integration mock hypervisor",
                "endpoint": "https://mock-hv.example:8006",
                "hypervisor_type": "mock",
                "secret_id": hv_secret["id"],
            },
        )
        resp.raise_for_status()
        hv = resp.json()
        yield hv
        await client.delete(f"/inventory/hypervisors/{hv['id']}")


def _dynamic_template_payload(driver_id: str, hypervisor_id: str, extra_fields: list) -> dict:
    return {
        "name": f"int-dyn-tmpl-{uuid.uuid4().hex[:8]}",
        "template_type": "dynamic",
        "driver_id": driver_id,
        "hypervisor_id": hypervisor_id,
        "vendor": "IntegrationVendor",
        "model": "MockInstance",
        "sections": [
            {
                "name": "Instance",
                "fields": [
                    {
                        "key": "image",
                        "label": "Image",
                        "type": "string",
                        "default": "ubuntu-22.04",
                    },
                ]
                + extra_fields,
            }
        ],
    }


@pytest.fixture(scope="session")
async def dynamic_template(base_url, admin_token, hv_driver, hypervisor):
    """A dynamic template bound to the mock hypervisor + recipe (happy path)."""
    async with _admin_session_client(base_url, admin_token) as client:
        payload = _dynamic_template_payload(hv_driver["id"], hypervisor["id"], [])
        resp = await client.post("/inventory/templates", json=payload)
        resp.raise_for_status()
        template = resp.json()
        yield template
        await client.delete(f"/inventory/templates/{template['id']}")


@pytest.fixture(scope="session")
async def failing_dynamic_template(base_url, admin_token, hv_driver, hypervisor):
    """A dynamic template whose field DEFAULT injects a create_instance failure.

    There is no device row when the recipe runs, so the knob must ride the
    template default to reach the driver context (HERD_mock_fail_actions).
    """
    async with _admin_session_client(base_url, admin_token) as client:
        payload = _dynamic_template_payload(
            hv_driver["id"],
            hypervisor["id"],
            [
                {
                    "key": "mock_fail_actions",
                    "label": "Mock fail actions",
                    "type": "string",
                    "default": "create_instance",
                },
            ],
        )
        resp = await client.post("/inventory/templates", json=payload)
        resp.raise_for_status()
        template = resp.json()
        yield template
        await client.delete(f"/inventory/templates/{template['id']}")


@pytest.fixture(scope="session")
async def failing_destroy_dynamic_template(base_url, admin_token, hv_driver, hypervisor):
    """A dynamic template whose create AND destroy fail (issue #937).

    The create failure leaves the ledger row with no instance_ref, so teardown
    runs the keyed destroy, which this template also fails: the stand-in for a
    recipe written before the keyed-destroy contract.
    """
    async with _admin_session_client(base_url, admin_token) as client:
        payload = _dynamic_template_payload(
            hv_driver["id"],
            hypervisor["id"],
            [
                {
                    "key": "mock_fail_actions",
                    "label": "Mock fail actions",
                    "type": "string",
                    "default": "create_instance,destroy_instance",
                },
            ],
        )
        resp = await client.post("/inventory/templates", json=payload)
        resp.raise_for_status()
        template = resp.json()
        yield template
        await client.delete(f"/inventory/templates/{template['id']}")


@pytest.fixture(scope="session")
async def failing_login_dynamic_template(base_url, admin_token, hv_driver, hypervisor):
    """A dynamic template whose recipe login answers {"success": false} without
    raising (issue #1027): the mock's HERD_mock_fail_actions=login knob, which
    the dynamic flows used to ignore."""
    async with _admin_session_client(base_url, admin_token) as client:
        payload = _dynamic_template_payload(
            hv_driver["id"],
            hypervisor["id"],
            [
                {
                    "key": "mock_fail_actions",
                    "label": "Mock fail actions",
                    "type": "string",
                    "default": "login",
                },
            ],
        )
        resp = await client.post("/inventory/templates", json=payload)
        resp.raise_for_status()
        template = resp.json()
        yield template
        await client.delete(f"/inventory/templates/{template['id']}")


@pytest.fixture(scope="session")
async def broken_recipe_driver(base_url, admin_token):
    """Upload a structurally broken Hypervisor recipe (no Driver class).

    Passes inventory's upload checks (a valid Hypervisor-type archive) but can
    never load in the execution sandbox, exercising the issue #279 first-delivery
    DLQ classification.
    """
    async with _admin_session_client(base_url, admin_token) as client:
        files = {"file": ("broken_recipe.tar.gz", _broken_recipe_tarball(), "application/gzip")}
        data = {
            "name": f"broken-hv-{uuid.uuid4().hex[:8]}",
            "connection_type": "Hypervisor",
            "description": "integration broken hypervisor recipe (no Driver class)",
        }
        resp = await client.post("/inventory/drivers", files=files, data=data)
        resp.raise_for_status()
        driver = resp.json()
        yield driver
        await client.delete(f"/inventory/drivers/{driver['id']}")


@pytest.fixture(scope="session")
async def broken_dynamic_template(base_url, admin_token, broken_recipe_driver, hypervisor):
    """A dynamic template bound to the structurally broken recipe package."""
    async with _admin_session_client(base_url, admin_token) as client:
        payload = _dynamic_template_payload(broken_recipe_driver["id"], hypervisor["id"], [])
        resp = await client.post("/inventory/templates", json=payload)
        resp.raise_for_status()
        template = resp.json()
        yield template
        await client.delete(f"/inventory/templates/{template['id']}")


# --- helpers ------------------------------------------------------------------


async def _reserve_dynamic(client, device_id: str, template_id: str) -> dict:
    now = datetime.now(timezone.utc)
    resp = await client.post(
        "/reservations/",
        json={
            "device_ids": [device_id],
            "purpose": "dynamic resources integration test",
            "start_time": now.isoformat(),
            "end_time": (now + timedelta(hours=1)).isoformat(),
            "dynamic_requests": [{"template_id": template_id}],
        },
    )
    resp.raise_for_status()
    return resp.json()


async def _poll_reservation_status(
    client, reservation_id: str, wanted: str, *, timeout: float = 60.0, interval: float = 1.0
) -> dict | None:
    """Poll until the reservation reaches `wanted`; return its body, else None."""
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        resp = await client.get(f"/reservations/{reservation_id}")
        if resp.status_code == 200 and resp.json().get("status") == wanted:
            return resp.json()
        await asyncio.sleep(interval)
    return None


async def _poll_device_gone(
    client, device_id: str, *, timeout: float = 60.0, interval: float = 1.0
) -> bool:
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        resp = await client.get(f"/inventory/devices/{device_id}")
        if resp.status_code == 404:
            return True
        await asyncio.sleep(interval)
    return False


async def _poll_device_status(
    client, device_id: str, wanted: str, *, timeout: float = 30.0, interval: float = 1.0
) -> str | None:
    """Poll until the device reports `wanted`; return the last seen status."""
    seen = None
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        resp = await client.get(f"/inventory/devices/{device_id}")
        if resp.status_code == 200:
            seen = resp.json().get("status")
            if seen == wanted:
                return seen
        await asyncio.sleep(interval)
    return seen


async def _runs(client, reservation_id: str, action: str, status: str | None = None) -> list[dict]:
    params = {"reservation_id": reservation_id, "limit": 200}
    if status:
        params["status"] = status
    resp = await client.get("/execution/runs", params=params)
    resp.raise_for_status()
    return [r for r in resp.json().get("items", []) if r["action"] == action]


async def _devices_with_prefix(client, prefix: str) -> list[dict]:
    resp = await client.get("/inventory/devices", params={"search": prefix, "limit": 200})
    resp.raise_for_status()
    return [d for d in resp.json().get("items", []) if d["name"].startswith(prefix)]


def _ledger_row(request_id: str) -> tuple[str, str] | None:
    """(status, instance_ref) of execution's dynamic_instances row, read in Postgres.

    The ledger has no API; this is the same `docker compose exec postgres psql`
    helper the LDAP suites use, so COMPOSE_PROJECT_NAME picks the stack.
    """
    uuid.UUID(request_id)  # only ever interpolate a well-formed uuid
    result = _psql(
        "SELECT status, coalesce(instance_ref, '') FROM execution.dynamic_instances "
        f"WHERE request_id = '{request_id}'",
        tuples_only=True,
    )
    assert result.returncode == 0, result.stderr
    line = result.stdout.strip()
    if not line:
        return None
    status, _, ref = line.partition("|")
    return status, ref


# Final execution_run statuses. A recipe step's row is created PENDING before
# the sandbox call (RUNNING on the run_driver_action path) and gets its status
# and `output` only when the call returns, so a row in any other status is
# still in flight and its `output` is not written yet (issue #1014).
_TERMINAL_RUN_STATUSES = frozenset({"SUCCESS", "FAILED", "TIMEOUT"})


async def _poll_keyed_destroys(client, reservation_id: str, *, timeout: float = 60.0) -> list:
    """Poll until a keyed destroy_instance run (method_kwargs instance_ref None)
    exists and every keyed run has finished, so each one's `output` is final."""
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        runs = await _runs(client, reservation_id, "destroy_instance")
        keyed = [
            r
            for r in runs
            if (r.get("input_params") or {}).get("method_kwargs") == {"instance_ref": None}
        ]
        if keyed and all(r["status"] in _TERMINAL_RUN_STATUSES for r in keyed):
            return keyed
        await asyncio.sleep(1.0)
    return []


def _dynamic_device_id(reservation: dict, physical_id: str) -> str:
    extra = set(reservation["device_ids"]) - {physical_id}
    assert len(extra) == 1, f"expected exactly one materialized device, got {extra}"
    return extra.pop()


async def _cancel_and_drain(client, reservation_id: str, device_id: str | None = None) -> None:
    """Best-effort cleanup: cancel, then wait for the async instance teardown.

    Waiting matters: the session-scoped template fixture deletes the dynamic
    template at session end, and a materialized device still referencing it
    would make that teardown flaky.
    """
    try:
        await client.delete(f"/reservations/{reservation_id}")
        if device_id is not None:
            await _poll_device_gone(client, device_id, timeout=30.0)
    except Exception:
        pass


# --- tests ----------------------------------------------------------------------


@pytest.mark.timeout(180)
async def test_dynamic_reservation_materializes_device_and_activates(
    admin_client, dynamic_template, hypervisor, fresh_device
):
    """Happy path: a booking with a dynamic request books through
    PENDING_PROVISION, the recipe creates the instance, and the reservation
    activates with a RESERVED / CLOUD device attached whose attributes carry
    the recipe's field_data (deterministic mgmt address, template image echo)."""
    reservation = await _reserve_dynamic(admin_client, fresh_device["id"], dynamic_template["id"])
    device_id = None
    try:
        # Gated activation: the booking must not be ACTIVE synchronously.
        assert reservation["status"] == "PENDING_PROVISION", reservation
        assert len(reservation["dynamic_requests"]) == 1
        assert reservation["dynamic_requests"][0]["template_id"] == dynamic_template["id"]

        active = await _poll_reservation_status(admin_client, reservation["id"], "ACTIVE")
        assert active is not None, "reservation never became ACTIVE"

        # The materialized device is attached alongside the physical DUT.
        device_id = _dynamic_device_id(active, fresh_device["id"])
        resp = await admin_client.get(f"/inventory/devices/{device_id}")
        assert resp.status_code == 200, resp.text
        device = resp.json()
        assert device["status"] == "RESERVED"
        assert device["topology_type"] == "CLOUD"
        prefix = f"{dynamic_template['name']}-{reservation['id'][:8]}-"
        assert device["name"].startswith(prefix), device["name"]
        # Recipe field_data flowed into the device: deterministic address plus
        # the template's image default, echoed through the driver context.
        assert device["field_data"]["mgmt_address"].startswith("10.66.")
        assert device["field_data"]["image"] == "ubuntu-22.04"

        # The create ran as an auditable ExecutionRun keyed on the hypervisor.
        creates = await _runs(admin_client, reservation["id"], "create_instance", "SUCCESS")
        assert len(creates) == 1
        assert creates[0]["device_id"] == hypervisor["id"]
    finally:
        await _cancel_and_drain(admin_client, reservation["id"], device_id)


@pytest.mark.timeout(180)
async def test_cancel_tears_down_the_dynamic_instance(admin_client, dynamic_template, fresh_device):
    """Teardown: cancelling an ACTIVE dynamic reservation drives
    destroy_instance from the ledger (with the deterministic instance_ref) and
    deletes the materialized device from inventory; the physical DUT returns
    to AVAILABLE."""
    reservation = await _reserve_dynamic(admin_client, fresh_device["id"], dynamic_template["id"])
    active = await _poll_reservation_status(admin_client, reservation["id"], "ACTIVE")
    assert active is not None, "reservation never became ACTIVE"
    device_id = _dynamic_device_id(active, fresh_device["id"])
    request_id = active["dynamic_requests"][0]["id"]

    resp = await admin_client.delete(f"/reservations/{reservation['id']}")
    assert resp.status_code == 204, resp.text

    assert await _poll_device_gone(admin_client, device_id), (
        "the materialized device was not deleted from inventory after cancel"
    )

    # destroy_instance ran against the ledger's instance_ref, which the mock
    # derives deterministically from the request id.
    destroys = await _runs(admin_client, reservation["id"], "destroy_instance", "SUCCESS")
    assert destroys, "no SUCCESS destroy_instance run was recorded after cancel"
    refs = {r["input_params"]["method_kwargs"]["instance_ref"] for r in destroys}
    assert refs == {f"mock-vm-{request_id}"}

    # The physical exclusive device is released by the cancel path.
    status = await _poll_device_status(admin_client, fresh_device["id"], "AVAILABLE")
    assert status == "AVAILABLE", f"physical device stuck in {status} after cancel"


@pytest.mark.timeout(300)
async def test_create_failure_lands_failed_with_no_orphans(
    admin_client, failing_dynamic_template, fresh_device
):
    """Failure path: a driver-reported create_instance failure NAKs through
    max_deliver=5 redeliveries (production: roughly 90s of NATS_NAK_BACKOFF_SECONDS
    delay; the dev/test stack's short override schedule finishes far faster),
    dead-letters the event, and the failure callback lands the reservation in
    FAILED. No materialized device may remain and the physical exclusive
    device returns to AVAILABLE. When the host can reach NATS, also asserts
    the exhausted provision_requested event was retained on the execution DLQ
    subject."""
    nats_error = await probe_nats()

    reservation = await _reserve_dynamic(
        admin_client, fresh_device["id"], failing_dynamic_template["id"]
    )
    assert reservation["status"] == "PENDING_PROVISION", reservation

    failed = await _poll_reservation_status(
        admin_client, reservation["id"], "FAILED", timeout=240.0, interval=2.0
    )
    assert failed is not None, "reservation never landed in FAILED after create failures"

    # No orphaned dynamic device: create never succeeded, so nothing with the
    # generated name prefix may exist in inventory.
    prefix = f"{failing_dynamic_template['name']}-{reservation['id'][:8]}"
    orphans = await _devices_with_prefix(admin_client, prefix)
    assert orphans == [], f"orphaned dynamic devices left in inventory: {orphans}"

    # The failure callback releases the exclusive physical device.
    status = await _poll_device_status(admin_client, fresh_device["id"], "AVAILABLE")
    assert status == "AVAILABLE", f"physical device stuck in {status} after FAILED"

    # Every create attempt is auditable and none actually created an instance.
    # A recipe step that completed but returned success: false is a FAILED run
    # row (issue #1027, the physical runs' rule), and its recorded output still
    # carries the recipe's verdict.
    creates = await _runs(admin_client, reservation["id"], "create_instance")
    assert creates, "no create_instance runs were recorded"
    for run in creates:
        assert run["status"] == "FAILED", f"create_instance run {run['id']}: {run['status']}"
        output = json.loads(run["output"]) if run.get("output") else {}
        assert output.get("success") is False, (
            f"create_instance run {run['id']} did not report the injected failure: {output}"
        )
        assert not output.get("instance_ref"), (
            f"create_instance run {run['id']} reported an instance_ref despite failing"
        )

    # DLQ retention (skipped silently when the host cannot reach NATS; under
    # make master / the compose stack the 4222 port is published, so it runs).
    if nats_error is None:
        retained = await find_in_execution_dlq(reservation["id"].encode())
        assert retained is not None, (
            "exhausted provision_requested was not retained on herd.reservations.dlq.execution"
        )
        assert json.loads(retained)["event"] == "reservation.provision_requested"


@pytest.mark.timeout(300)
async def test_failed_create_is_destroyed_by_keyed_teardown(
    admin_client, failing_dynamic_template, fresh_device
):
    """Issue #937, live: create_instance fails on every delivery, so the ledger
    row never learns an instance_ref. When the reservation lands in FAILED,
    teardown must still drive the recipe: a keyed destroy_instance with
    instance_ref None, which the mock resolves to the name its create derives
    from the request id, and only then is the row DESTROYED. Before #937 the
    row was retired with no driver call at all."""
    reservation = await _reserve_dynamic(
        admin_client, fresh_device["id"], failing_dynamic_template["id"]
    )
    request_id = reservation["dynamic_requests"][0]["id"]
    failed = await _poll_reservation_status(
        admin_client, reservation["id"], "FAILED", timeout=240.0, interval=2.0
    )
    assert failed is not None, "reservation never landed in FAILED after create failures"

    keyed = await _poll_keyed_destroys(admin_client, reservation["id"])
    assert len(keyed) == 1, f"expected one keyed destroy_instance run, got {keyed}"
    output = json.loads(keyed[0]["output"])
    assert output == {
        "success": True,
        "instance_ref": f"mock-vm-{request_id}",
        "keyed": True,
    }

    deadline = asyncio.get_event_loop().time() + 30.0
    row = _ledger_row(request_id)
    while row != ("DESTROYED", "") and asyncio.get_event_loop().time() < deadline:
        await asyncio.sleep(1.0)
        row = _ledger_row(request_id)
    assert row == ("DESTROYED", ""), f"ledger row after keyed destroy: {row}"


@pytest.mark.timeout(300)
async def test_failed_keyed_destroy_leaves_ledger_row_creating(
    admin_client, failing_destroy_dynamic_template, fresh_device
):
    """Issue #937 failure policy, live: the keyed destroy itself fails (the
    stand-in for a recipe written before the contract), so the row must NOT be
    retired: it stays CREATING with no instance_ref as a may-still-exist
    record, and the reservation still ends FAILED (the event was ACKed, not
    looped)."""
    reservation = await _reserve_dynamic(
        admin_client, fresh_device["id"], failing_destroy_dynamic_template["id"]
    )
    request_id = reservation["dynamic_requests"][0]["id"]
    try:
        failed = await _poll_reservation_status(
            admin_client, reservation["id"], "FAILED", timeout=240.0, interval=2.0
        )
        assert failed is not None, "reservation never landed in FAILED after create failures"

        keyed = await _poll_keyed_destroys(admin_client, reservation["id"])
        assert len(keyed) == 1, f"expected one keyed destroy_instance run, got {keyed}"
        output = json.loads(keyed[0]["output"])
        assert output["success"] is False

        # Teardown has finished with this row (the keyed run is recorded before
        # the ledger decision); give it a moment, then the row must still be live.
        await asyncio.sleep(3.0)
        assert _ledger_row(request_id) == ("CREATING", "")
        # The physical device is released regardless.
        status = await _poll_device_status(admin_client, fresh_device["id"], "AVAILABLE")
        assert status == "AVAILABLE", f"physical device stuck in {status} after FAILED"
    finally:
        # Test garbage on a shared stack: the mock created nothing, so the
        # may-still-exist row is known to be empty here and can go.
        _psql(f"DELETE FROM execution.dynamic_instances WHERE request_id = '{request_id}'")


@pytest.mark.timeout(300)
async def test_failed_recipe_login_never_creates_and_lands_failed(
    admin_client, failing_login_dynamic_template, fresh_device
):
    """Issue #1027, live: the recipe login returns {"success": false}. No
    create_instance may run on any delivery (it used to run right after the
    failed login), every login run row is FAILED, the event exhausts its
    deliveries and the reservation lands in FAILED. Teardown's own login fails
    the same way, so no destroy_instance runs either and the ledger row stays
    CREATING with no instance_ref as a may-still-exist record."""
    reservation = await _reserve_dynamic(
        admin_client, fresh_device["id"], failing_login_dynamic_template["id"]
    )
    request_id = reservation["dynamic_requests"][0]["id"]
    try:
        failed = await _poll_reservation_status(
            admin_client, reservation["id"], "FAILED", timeout=240.0, interval=2.0
        )
        assert failed is not None, "reservation never landed in FAILED after login failures"
        # Let teardown's login run land before reading the runs.
        await asyncio.sleep(3.0)

        assert await _runs(admin_client, reservation["id"], "create_instance") == []
        assert await _runs(admin_client, reservation["id"], "destroy_instance") == []
        logins = await _runs(admin_client, reservation["id"], "login")
        assert logins, "no login runs were recorded"
        assert {r["status"] for r in logins} == {"FAILED"}, logins
        assert _ledger_row(request_id) == ("CREATING", "")

        prefix = f"{failing_login_dynamic_template['name']}-{reservation['id'][:8]}"
        assert await _devices_with_prefix(admin_client, prefix) == []
        status = await _poll_device_status(admin_client, fresh_device["id"], "AVAILABLE")
        assert status == "AVAILABLE", f"physical device stuck in {status} after FAILED"
    finally:
        # Test garbage on a shared stack: no create ever ran, so the
        # may-still-exist row is known to be empty here and can go.
        _psql(f"DELETE FROM execution.dynamic_instances WHERE request_id = '{request_id}'")


@pytest.mark.timeout(120)
async def test_broken_recipe_package_dead_letters_on_first_delivery(
    admin_client, broken_dynamic_template, fresh_device
):
    """Permanent-package path (issue #279): a structurally broken recipe (no
    Driver class) can never load, so the consumer dead-letters the
    provision_requested event on the FIRST delivery rather than NAK'ing
    through the redelivery ladder. The reservation lands in FAILED fast, no
    recipe method ever runs (no ExecutionRun rows), no device is
    materialized, and the physical DUT is released, matching the existing
    permanent-failure outcome.

    First-delivery proof: NO create_instance/login runs exist, since
    load_driver fails before any sandbox step (contrast
    test_create_failure_lands_failed_with_no_orphans, which records five
    create_instance attempts across the ladder). The 60s window this test
    polls under is generous, not discriminating (issue #895): under the
    PRODUCTION NATS_NAK_BACKOFF_SECONDS default ([1, 5, 15, 60, 120]s, needing
    >= 81s to exhaust) a transient path could not finish that fast either, but
    this suite actually runs against docker-compose.override.yml's short
    dev/test schedule, under which a transient exhaustion would ALSO land
    FAILED well inside 60s, so the absence of create_instance/login runs is
    the real, timing-independent proof of the first-delivery path, not the
    elapsed time."""
    nats_error = await probe_nats()

    reservation = await _reserve_dynamic(
        admin_client, fresh_device["id"], broken_dynamic_template["id"]
    )
    assert reservation["status"] == "PENDING_PROVISION", reservation

    # 60s is generous, not a discriminator against a transient retry under the
    # dev/test stack's short override schedule (see the docstring above); the
    # real proof that this is the first-delivery DLQ, not a retried transient
    # failure, is the absence of create_instance/login runs asserted below.
    failed = await _poll_reservation_status(
        admin_client, reservation["id"], "FAILED", timeout=60.0, interval=1.0
    )
    assert failed is not None, (
        "reservation never landed in FAILED within 60s; a broken package must "
        "dead-letter on first delivery, not ride the retry ladder"
    )

    # The recipe never executed: load_driver failed before login/create_instance.
    creates = await _runs(admin_client, reservation["id"], "create_instance")
    assert creates == [], f"a broken package must not run create_instance: {creates}"
    logins = await _runs(admin_client, reservation["id"], "login")
    assert logins == [], f"a broken package must not reach recipe login: {logins}"

    # No orphaned dynamic device: create never ran, so nothing with the generated
    # name prefix may exist in inventory.
    prefix = f"{broken_dynamic_template['name']}-{reservation['id'][:8]}"
    orphans = await _devices_with_prefix(admin_client, prefix)
    assert orphans == [], f"orphaned dynamic devices left in inventory: {orphans}"

    # The failure callback releases the exclusive physical device.
    status = await _poll_device_status(admin_client, fresh_device["id"], "AVAILABLE")
    assert status == "AVAILABLE", f"physical device stuck in {status} after FAILED"

    # DLQ retention (skipped silently when the host cannot reach NATS; under make
    # master / the compose stack the 4222 port is published, so it runs).
    if nats_error is None:
        retained = await find_in_execution_dlq(reservation["id"].encode())
        assert retained is not None, (
            "broken-package provision_requested was not retained on herd.reservations.dlq.execution"
        )
        assert json.loads(retained)["event"] == "reservation.provision_requested"


@pytest.mark.timeout(180)
async def test_provision_requested_redelivery_is_idempotent(
    admin_client, dynamic_template, fresh_device
):
    """Replay safety: re-publishing the provision_requested event, both
    verbatim (same event_id, new sequence: the relay-republish case) and with a
    fresh event_id (past any event-level dedupe), must not create a second
    instance. The observable invariants are a single create_instance SUCCESS
    run, a single materialized device, and an unchanged ACTIVE reservation.

    What this proves is the corroboration gate, not the ledger (issue #1032):
    both replays arrive after the reservation is ACTIVE, and execution's gate
    requires PENDING_PROVISION for this event, so each replay is acked as
    unverified before any ledger read. The ledger's own redelivery guard (an
    ACTIVE row with a device skips the create) is pinned at unit level by
    services/execution/tests/test_nats_consumer_dynamic.py
    (test_redelivery_skips_active_row_and_still_reports_success)."""
    nats_error = await probe_nats()
    if nats_error is not None:
        pytest.skip(f"NATS unreachable from test host: {nats_error}")

    reservation = await _reserve_dynamic(admin_client, fresh_device["id"], dynamic_template["id"])
    device_id = None
    try:
        active = await _poll_reservation_status(admin_client, reservation["id"], "ACTIVE")
        assert active is not None, "reservation never became ACTIVE"
        device_id = _dynamic_device_id(active, fresh_device["id"])
        prefix = f"{dynamic_template['name']}-{reservation['id'][:8]}"
        assert len(await _devices_with_prefix(admin_client, prefix)) == 1

        raw = await fetch_reservation_event(reservation["id"], "reservation.provision_requested")
        assert raw is not None, "could not capture the provision_requested event"

        # Replay 1: verbatim (same payload event_id, new JetStream sequence).
        await publish_raw(_PROVISION_SUBJECT, raw)
        # Replay 2: fresh event_id, so no event-level dedupe applies; the
        # corroboration gate refuses it (the reservation is ACTIVE).
        mutated = json.loads(raw)
        mutated["event_id"] = str(uuid.uuid4())
        await publish_raw(_PROVISION_SUBJECT, json.dumps(mutated).encode())

        # Settle: a redelivered create short-circuits on the ledger row, so
        # there is no positive signal to poll for; give the consumer time to
        # process both replays, then assert nothing was duplicated.
        await asyncio.sleep(6.0)

        creates = await _runs(admin_client, reservation["id"], "create_instance", "SUCCESS")
        assert len(creates) == 1, (
            f"create_instance succeeded {len(creates)} times across replays; "
            "the ledger idempotency guard failed"
        )
        devices = await _devices_with_prefix(admin_client, prefix)
        assert [d["id"] for d in devices] == [device_id], (
            f"replays materialized extra devices: {[d['name'] for d in devices]}"
        )
        resp = await admin_client.get(f"/reservations/{reservation['id']}")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "ACTIVE"
        assert sorted(body["device_ids"]) == sorted(active["device_ids"])
    finally:
        await _cancel_and_drain(admin_client, reservation["id"], device_id)


# --- secret delete reference guard (issue #456) ------------------------------


async def test_secret_delete_refused_while_hypervisor_references_it(base_url, admin_token):
    """A referenced secret cannot be deleted; an unreferenced one can.

    Issue #456: secrets' delete calls inventory's by-secret reverse lookup and
    409s with the referencing hypervisor ids/names. Deleting the hypervisor
    first (the correct order) unblocks the delete. Test-local resources, not
    the shared session fixtures, since this test must delete them.
    """
    async with _admin_session_client(base_url, admin_token) as client:
        resp = await client.post(
            "/secrets/secrets",
            json={
                "name": f"int-guard-secret-{uuid.uuid4().hex[:8]}",
                "type": "password",
                "description": "issue #456 delete-guard credential",
                "data": {"username": "svc", "password": "integration-guard-password"},
            },
        )
        resp.raise_for_status()
        secret = resp.json()
        hv = None
        try:
            resp = await client.post(
                "/inventory/hypervisors",
                json={
                    "name": f"int-guard-hv-{uuid.uuid4().hex[:8]}",
                    "description": "issue #456 delete-guard hypervisor",
                    "endpoint": "https://guard-hv.example:8006",
                    "hypervisor_type": "mock",
                    "secret_id": secret["id"],
                },
            )
            resp.raise_for_status()
            hv = resp.json()

            resp = await client.delete(f"/secrets/secrets/{secret['id']}")
            assert resp.status_code == 409, resp.text
            detail = resp.json()["detail"]
            assert detail["error"] == "secret_in_use"
            assert hv["id"] in detail["hypervisor_ids"]
            assert hv["name"] in detail["hypervisor_names"]

            # The refused delete left the secret intact and revealable.
            resp = await client.get(f"/secrets/secrets/{secret['id']}")
            assert resp.status_code == 200
        finally:
            if hv is not None:
                resp = await client.delete(f"/inventory/hypervisors/{hv['id']}")
                assert resp.status_code in (204, 404), resp.text

        # With the reference gone, the same delete succeeds.
        resp = await client.delete(f"/secrets/secrets/{secret['id']}")
        assert resp.status_code == 204, resp.text
        resp = await client.get(f"/secrets/secrets/{secret['id']}")
        assert resp.status_code == 404


# --- issues #1053, #1033, #1030: who may book, a disabled hypervisor, mixed edits ---


@pytest.fixture
async def gated_dynamic(base_url, admin_client, user_token, hv_secret, hv_driver):
    """A private hypervisor plus dynamic template, and visibility scaffolding.

    The hypervisor starts with no device group, so its template is admin-only
    (issue #1053). `device_group_id` names a device group the intuser's own,
    isolated user group holds a permission on; a test opens the gate by setting
    it on the hypervisor and adds physical devices to it to make them visible.
    Private objects, so no other test's shared hypervisor or template changes.
    """
    suffix = uuid.uuid4().hex[:8]
    created: dict = {}
    try:
        async with httpx.AsyncClient(
            base_url=base_url,
            verify=False,
            timeout=30.0,
            headers={"Authorization": f"Bearer {user_token}"},
        ) as uclient:
            me = await uclient.get("/auth/me")
            me.raise_for_status()
        ug = await admin_client.post(
            "/auth/groups", json={"name": f"int-dyn-ug-{suffix}", "description": "#1053"}
        )
        ug.raise_for_status()
        created["user_group_id"] = ug.json()["id"]
        (
            await admin_client.post(
                f"/auth/groups/{created['user_group_id']}/members/bulk",
                json={"user_ids": [me.json()["id"]]},
            )
        ).raise_for_status()
        dg = await admin_client.post(
            "/inventory/device-groups",
            json={"name": f"int-dyn-dg-{suffix}", "description": "#1053"},
        )
        dg.raise_for_status()
        created["device_group_id"] = dg.json()["id"]
        (
            await admin_client.post(
                f"/inventory/device-groups/{created['device_group_id']}/permissions/bulk",
                json={"user_group_ids": [created["user_group_id"]]},
            )
        ).raise_for_status()
        hv = await admin_client.post(
            "/inventory/hypervisors",
            json={
                "name": f"int-gated-hv-{suffix}",
                "endpoint": "https://gated-hv.example:8006",
                "hypervisor_type": "mock",
                "secret_id": hv_secret["id"],
            },
        )
        hv.raise_for_status()
        created["hypervisor"] = hv.json()
        assert created["hypervisor"]["device_group_id"] is None
        tmpl = await admin_client.post(
            "/inventory/templates",
            json=_dynamic_template_payload(hv_driver["id"], created["hypervisor"]["id"], []),
        )
        tmpl.raise_for_status()
        created["template"] = tmpl.json()
        yield created
    finally:
        if "template" in created:
            await admin_client.delete(f"/inventory/templates/{created['template']['id']}")
        if "hypervisor" in created:
            await admin_client.delete(f"/inventory/hypervisors/{created['hypervisor']['id']}")
        if "device_group_id" in created:
            await admin_client.delete(f"/inventory/device-groups/{created['device_group_id']}")
        if "user_group_id" in created:
            await admin_client.delete(f"/auth/groups/{created['user_group_id']}")


def _dynamic_only_body(template_id: str) -> dict:
    now = datetime.now(timezone.utc)
    return {
        "device_ids": [],
        "purpose": "dynamic gating integration test",
        "start_time": now.isoformat(),
        "end_time": (now + timedelta(hours=1)).isoformat(),
        "dynamic_requests": [{"template_id": template_id}],
    }


async def _cancel_dynamic_only(client, reservation_id: str) -> None:
    """Cancel a dynamic-only booking and wait for its instance device to go.

    The private template is deleted at fixture teardown, which a lingering
    instance device would refuse.
    """
    active = await _poll_reservation_status(client, reservation_id, "ACTIVE", timeout=60.0)
    await client.delete(f"/reservations/{reservation_id}")
    if active is not None:
        for device_id in active["device_ids"]:
            await _poll_device_gone(client, device_id, timeout=30.0)


@pytest.mark.timeout(240)
async def test_dynamic_template_is_bookable_only_through_its_hypervisors_device_group(
    admin_client, user_client, gated_dynamic
):
    """Issue #1053: a non-admin cannot see or book a dynamic template until its
    hypervisor names a device group one of their user groups holds a permission
    on; the refusal is the unknown-template refusal. An admin books it either way."""
    template_id = gated_dynamic["template"]["id"]
    hv_id = gated_dynamic["hypervisor"]["id"]

    hidden = await user_client.get(f"/inventory/templates/{template_id}")
    assert hidden.status_code == 404, hidden.text
    listing = await user_client.get("/inventory/templates", params={"template_type": "dynamic"})
    assert template_id not in {t["id"] for t in listing.json()["items"]}
    refused = await user_client.post("/reservations/", json=_dynamic_only_body(template_id))
    assert refused.status_code == 422, refused.text
    assert refused.json()["detail"] == f"Template {template_id} not found in inventory"

    admin_booking = await admin_client.post("/reservations/", json=_dynamic_only_body(template_id))
    assert admin_booking.status_code == 201, admin_booking.text
    await _cancel_dynamic_only(admin_client, admin_booking.json()["id"])

    opened = await admin_client.put(
        f"/inventory/hypervisors/{hv_id}",
        json={"device_group_id": gated_dynamic["device_group_id"]},
    )
    assert opened.status_code == 200, opened.text
    assert (await user_client.get(f"/inventory/templates/{template_id}")).status_code == 200
    booked = await user_client.post("/reservations/", json=_dynamic_only_body(template_id))
    assert booked.status_code == 201, booked.text
    await _cancel_dynamic_only(user_client, booked.json()["id"])


@pytest.mark.timeout(120)
async def test_disabled_hypervisor_refuses_the_booking(admin_client, gated_dynamic):
    """Issue #1033: a template whose hypervisor is disabled is refused at booking
    with a 422 naming the hypervisor, and books again once it is re-enabled."""
    hv = gated_dynamic["hypervisor"]
    template = gated_dynamic["template"]
    off = await admin_client.put(f"/inventory/hypervisors/{hv['id']}", json={"enabled": False})
    assert off.status_code == 200, off.text
    refused = await admin_client.post("/reservations/", json=_dynamic_only_body(template["id"]))
    assert refused.status_code == 422, refused.text
    assert refused.json()["detail"] == (
        f"Hypervisor '{hv['name']}' is disabled; these dynamic templates cannot be booked "
        f"until an admin enables it: {template['name']}"
    )

    on = await admin_client.put(f"/inventory/hypervisors/{hv['id']}", json={"enabled": True})
    assert on.status_code == 200, on.text
    booked = await admin_client.post("/reservations/", json=_dynamic_only_body(template["id"]))
    assert booked.status_code == 201, booked.text
    await _cancel_dynamic_only(admin_client, booked.json()["id"])


@pytest.mark.timeout(240)
async def test_non_admin_owner_sees_and_keeps_their_instance_in_a_mixed_edit(
    admin_client, user_client, gated_dynamic, fresh_devices
):
    """Issue #1030: a non-admin books one physical device plus one instance; once
    ACTIVE they can read the instance device, and a device-set edit that adds a
    second physical device while listing the instance succeeds and keeps it."""
    first, second = await fresh_devices(2)
    group = gated_dynamic["device_group_id"]
    (
        await admin_client.post(
            f"/inventory/device-groups/{group}/devices/bulk",
            json={"device_ids": [first["id"], second["id"]]},
        )
    ).raise_for_status()
    (
        await admin_client.put(
            f"/inventory/hypervisors/{gated_dynamic['hypervisor']['id']}",
            json={"device_group_id": group},
        )
    ).raise_for_status()

    reservation = await _reserve_dynamic(user_client, first["id"], gated_dynamic["template"]["id"])
    instance_id = None
    try:
        active = await _poll_reservation_status(user_client, reservation["id"], "ACTIVE")
        assert active is not None, "reservation never became ACTIVE"
        instance_id = _dynamic_device_id(active, first["id"])

        seen = await user_client.get(f"/inventory/devices/{instance_id}")
        assert seen.status_code == 200, seen.text
        assert seen.json()["topology_type"] == "CLOUD"

        edit = await user_client.patch(
            f"/reservations/{reservation['id']}",
            json={"device_ids": [first["id"], second["id"], instance_id]},
        )
        assert edit.status_code == 200, edit.text
        assert set(edit.json()["device_ids"]) == {first["id"], second["id"], instance_id}
        # The instance was kept, so nothing tore it down.
        assert (await user_client.get(f"/inventory/devices/{instance_id}")).status_code == 200
    finally:
        await _cancel_and_drain(admin_client, reservation["id"], instance_id)
