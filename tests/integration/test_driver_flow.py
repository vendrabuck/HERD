"""Integration tests for the driver package lifecycle against a running HERD stack.

Covers the full cross-service flow: upload -> template references it ->
replace file -> delete blocked while referenced -> delete after template removed.
"""

import io
import tarfile
import uuid

import pytest

pytestmark = pytest.mark.asyncio


def _tarball(driver_py: bytes = b"class Driver:\n    pass\n") -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        info = tarfile.TarInfo("driver.py")
        info.size = len(driver_py)
        tf.addfile(info, io.BytesIO(driver_py))
    return buf.getvalue()


async def _upload_driver(client, name: str, connection_type: str = "Management") -> dict:
    files = {"file": ("driver.tar.gz", _tarball(), "application/gzip")}
    data = {"name": name, "connection_type": connection_type, "description": "integration"}
    resp = await client.post("/inventory/drivers", files=files, data=data)
    resp.raise_for_status()
    return resp.json()


async def _delete_driver(client, driver_id: str) -> int:
    resp = await client.delete(f"/inventory/drivers/{driver_id}")
    return resp.status_code


async def _create_template(client, driver_id: str, name: str) -> dict:
    body = {
        "name": name,
        "template_type": "device",
        "driver_id": driver_id,
        "vendor": "IntegrationVendor",
        "model": "IntegrationModel",
        "sections": [
            {
                "name": "General",
                "fields": [{"key": "model", "label": "Model", "type": "string"}],
            }
        ],
    }
    resp = await client.post("/inventory/templates", json=body)
    resp.raise_for_status()
    return resp.json()


async def _delete_template(client, template_id: str) -> int:
    resp = await client.delete(f"/inventory/templates/{template_id}")
    return resp.status_code


async def test_upload_driver_and_create_template_references_it(admin_client):
    """Uploaded driver can be referenced by a device template; the template
    exposes driver_name + connection_type."""
    suffix = uuid.uuid4().hex[:8]
    driver = await _upload_driver(admin_client, name=f"int-driver-{suffix}")
    template_id = None
    try:
        template = await _create_template(admin_client, driver["id"], f"int-tmpl-{suffix}")
        template_id = template["id"]
        assert template["driver_id"] == driver["id"]
        assert template["driver_name"] == driver["name"]
        assert template["connection_type"] == "Management"
    finally:
        if template_id:
            await _delete_template(admin_client, template_id)
        await _delete_driver(admin_client, driver["id"])


async def test_driver_delete_blocked_while_template_references_it(admin_client):
    """Deleting a driver with a referencing template returns 409; delete
    succeeds after the template is removed."""
    suffix = uuid.uuid4().hex[:8]
    driver = await _upload_driver(admin_client, name=f"int-driver-{suffix}")
    template = await _create_template(admin_client, driver["id"], f"int-tmpl-{suffix}")

    # While template exists, driver delete is blocked.
    blocked = await _delete_driver(admin_client, driver["id"])
    assert blocked == 409, f"expected 409, got {blocked}"

    # Remove template, then driver delete should succeed.
    assert await _delete_template(admin_client, template["id"]) == 204
    assert await _delete_driver(admin_client, driver["id"]) == 204


async def test_replace_driver_file_updates_sha256(admin_client):
    """PUT /drivers/{id}/file swaps the stored package and updates the sha256."""
    suffix = uuid.uuid4().hex[:8]
    driver = await _upload_driver(admin_client, name=f"int-driver-{suffix}")
    try:
        original_sha = driver["sha256"]
        # Upload a different package body so sha changes.
        new_body = _tarball(b"class Driver:\n    version = 2\n")
        files = {"file": ("driver.tar.gz", new_body, "application/gzip")}
        resp = await admin_client.put(f"/inventory/drivers/{driver['id']}/file", files=files)
        resp.raise_for_status()
        updated = resp.json()
        assert updated["sha256"] != original_sha
        assert updated["id"] == driver["id"]
    finally:
        await _delete_driver(admin_client, driver["id"])


async def test_driver_upload_rejects_bad_extension(admin_client):
    """Uploading a .txt file is rejected (allowed: .zip, .tar.gz)."""
    files = {"file": ("driver.txt", b"not a real driver", "text/plain")}
    data = {"name": f"bad-ext-{uuid.uuid4().hex[:8]}", "connection_type": "Management"}
    resp = await admin_client.post("/inventory/drivers", files=files, data=data)
    assert resp.status_code in (400, 422)


async def test_driver_creation_requires_connection_type(admin_client):
    """POST /drivers without connection_type returns 422 (field is required)."""
    files = {"file": ("driver.tar.gz", _tarball(), "application/gzip")}
    data = {"name": f"no-ct-{uuid.uuid4().hex[:8]}"}
    resp = await admin_client.post("/inventory/drivers", files=files, data=data)
    assert resp.status_code == 422
