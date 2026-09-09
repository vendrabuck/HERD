"""Shared setup helpers for the L3 (Layer 3 Switch) integration test files
(ADR 0014, issue #34; R9 review fix on 2ade362c moved these out of
test_l3_route_provisioning.py rather than letting test_l3_intent_validate_and_fork.py
copy them a second time).

Kept out of conftest.py for the same reason as `_ai_helpers.py`: pytest treats
conftest as fixture-only, and module-style imports of conftest are ambiguous
across the repo's several conftest.py files. Plain Python module is unambiguous.

These are plain functions, not fixtures: each test module keeps its own
session-scoped `l3_driver`/`l3_template` fixtures (uploading its own
uniquely-named driver so running both files together is safe) that call
`create_l3_driver`/`create_l3_template` for the actual HTTP round trip, so the
POST/DELETE logic lives in exactly one place while fixture scope and teardown
stay declared locally where pytest expects to find them.
"""

import io
import tarfile
from pathlib import Path

import httpx

MOCK_L3_DIR = Path(__file__).resolve().parents[2] / "drivers" / "mock_l3"


def mock_l3_tarball() -> bytes:
    """Package the checked-in drivers/mock_l3 package into a .tar.gz for upload."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name in ("driver.py", "driver_metadata.json"):
            tf.add(MOCK_L3_DIR / name, arcname=name)
    return buf.getvalue()


async def create_l3_driver(client: httpx.AsyncClient, name: str) -> dict:
    """Upload the mock Layer 3 Switch driver under the given (unique) name."""
    files = {"file": ("mock_l3.tar.gz", mock_l3_tarball(), "application/gzip")}
    data = {
        "name": name,
        "connection_type": "Layer 3 Switch",
        "description": "integration mock L3 switch driver",
    }
    resp = await client.post("/inventory/drivers", files=files, data=data)
    resp.raise_for_status()
    driver = resp.json()
    assert driver["connection_type"] == "Layer 3 Switch"
    return driver


async def create_l3_template(client: httpx.AsyncClient, driver_id: str, name: str) -> dict:
    """A device template wired to the given (already-uploaded) L3 driver."""
    payload = {
        "name": name,
        "template_type": "device",
        "driver_id": driver_id,
        "vendor": "IntegrationVendor",
        "model": "MockL3Switch",
        "sections": [
            {
                "name": "General",
                "fields": [{"key": "model", "label": "Model", "type": "string"}],
            }
        ],
    }
    resp = await client.post("/inventory/templates", json=payload)
    resp.raise_for_status()
    return resp.json()


async def create_device(client: httpx.AsyncClient, template_id: str, name: str) -> dict:
    resp = await client.post(
        "/inventory/devices",
        json={
            "name": name,
            "template_id": template_id,
            "topology_type": "PHYSICAL",
            "status": "AVAILABLE",
            "field_data": {"model": "test"},
        },
    )
    resp.raise_for_status()
    return resp.json()


async def create_connection(
    client: httpx.AsyncClient, dut_id: str, switch_id: str, switch_port: str
) -> dict:
    # The cabling connection_type field is irrelevant to L3 adjacency: the L3
    # reconcile derives adjacency from the fork's recorded hops, keying on the
    # far-end device's driver connection_type ("Layer 3 Switch"), not this field.
    resp = await client.post(
        "/cabling/connections",
        json={
            "device_a_id": dut_id,
            "port_a": "eth0",
            "device_b_id": switch_id,
            "port_b": switch_port,
            "connection_type": "L1",
        },
    )
    resp.raise_for_status()
    return resp.json()
