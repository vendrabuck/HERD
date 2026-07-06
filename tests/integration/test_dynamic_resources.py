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
it fail to create. A driver-reported create failure NAKs through the consumer
backoff schedule ([1, 5, 15, 60, 120]s, max_deliver 5), so the FAILED path
takes roughly 90 seconds of wall clock before the DLQ exhaustion posts the
failure callback; the failure test's timeouts are sized for that.

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
    """Failure path: a driver-reported create_instance failure NAKs through the
    backoff schedule, exhausts max_deliver (roughly 90s), dead-letters the
    event, and the failure callback lands the reservation in FAILED. No
    materialized device may remain and the physical exclusive device returns
    to AVAILABLE. When the host can reach NATS, also asserts the exhausted
    provision_requested event was retained on the execution DLQ subject."""
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
    # Run-row status is sandbox-level across all consumer flows (the method
    # executed), so a driver-reported failure is a SUCCESS row whose recorded
    # output carries success: false; that output is the audit contract here.
    creates = await _runs(admin_client, reservation["id"], "create_instance")
    assert creates, "no create_instance runs were recorded"
    for run in creates:
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


@pytest.mark.timeout(120)
async def test_broken_recipe_package_dead_letters_on_first_delivery(
    admin_client, broken_dynamic_template, fresh_device
):
    """Permanent-package path (issue #279): a structurally broken recipe (no
    Driver class) can never load, so the consumer dead-letters the
    provision_requested event on the FIRST delivery rather than NAK'ing through
    the backoff ladder. The reservation lands in FAILED fast, no recipe method
    ever runs (no ExecutionRun rows), no device is materialized, and the
    physical DUT is released, matching the existing permanent-failure outcome.

    First-delivery proof is twofold: (1) NO create_instance/login runs exist,
    since load_driver fails before any sandbox step (contrast
    test_create_failure_lands_failed_with_no_orphans, which records five
    create_instance attempts across the ladder); and (2) FAILED arrives inside a
    60s window, which is reachable for the immediate DLQ but impossible for the
    retry ladder ([1, 5, 15, 60, 120]s needs >= 81s of backoff to exhaust)."""
    nats_error = await probe_nats()

    reservation = await _reserve_dynamic(
        admin_client, fresh_device["id"], broken_dynamic_template["id"]
    )
    assert reservation["status"] == "PENDING_PROVISION", reservation

    # 60s is below the >= 81s the ladder needs to exhaust, so reaching FAILED
    # here can only be the first-delivery DLQ, not a retried transient failure.
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
    """Redelivery idempotency: re-publishing the provision_requested event, both
    verbatim (same event_id, new sequence: the relay-republish case) and with a
    fresh event_id (forcing full reprocessing past any event-level dedupe),
    must not create a second instance. The ledger keys the create on the
    request id, so a redelivered create short-circuits before any sandbox step;
    the observable invariants are a single create_instance SUCCESS run, a
    single materialized device, and an unchanged ACTIVE reservation."""
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
        # Replay 2: fresh event_id, so the consumer fully reprocesses and the
        # dynamic_instances ledger guard is what must hold.
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
