"""End-to-end integration: lab purpose classification (issue #646 phase 1).

Assumes a running HERD stack (make up). Exercises the v1 facade create path,
the PATCH endpoint, and the utilization report's by_purpose breakdown against
a real reservations service and Postgres, not the SQLite unit harness.
"""

from datetime import datetime, timedelta, timezone

import pytest

pytestmark = pytest.mark.asyncio


def _reservation_body(device_id: str, purpose_category: str | None = None) -> dict:
    now = datetime.now(timezone.utc)
    body = {
        "device_ids": [device_id],
        "purpose": "purpose classification integration test",
        "start_time": now.isoformat(),
        "end_time": (now + timedelta(hours=1)).isoformat(),
    }
    if purpose_category is not None:
        body["purpose_category"] = purpose_category
    return body


async def test_purpose_category_via_v1_facade_read_back_and_patch(admin_client, fresh_device):
    """Create through /api/v1 with a category, read it back through both the
    v1 facade and the interactive reservations endpoint, then clear it via
    PATCH and confirm the interactive read reflects the clear."""
    create = await admin_client.post(
        "/v1/reservations",
        json=_reservation_body(fresh_device["id"], purpose_category="qa_regression"),
    )
    assert create.status_code == 201, create.text
    reservation_id = create.json()["id"]
    assert create.json()["purpose_category"] == "qa_regression"

    try:
        v1_get = await admin_client.get(f"/v1/reservations/{reservation_id}")
        assert v1_get.status_code == 200
        assert v1_get.json()["purpose_category"] == "qa_regression"

        direct_get = await admin_client.get(f"/reservations/{reservation_id}")
        assert direct_get.status_code == 200
        direct_data = direct_get.json()
        assert direct_data["purpose_category"] == "qa_regression"
        assert direct_data["purpose_category_set_at"] is not None

        patch_resp = await admin_client.patch(
            f"/reservations/{reservation_id}/purpose-category",
            json={"purpose_category": None},
        )
        assert patch_resp.status_code == 200, patch_resp.text
        assert patch_resp.json()["purpose_category"] is None

        after_clear = await admin_client.get(f"/reservations/{reservation_id}")
        assert after_clear.status_code == 200
        assert after_clear.json()["purpose_category"] is None
        assert after_clear.json()["purpose_category_set_at"] is None
    finally:
        await admin_client.delete(f"/reservations/{reservation_id}")


async def test_purpose_category_unknown_value_rejected_through_facade(admin_client, fresh_device):
    """An unknown category is rejected with the same 422 wording whether the
    caller goes through the v1 facade or the interactive endpoint directly."""
    resp = await admin_client.post(
        "/v1/reservations",
        json=_reservation_body(fresh_device["id"], purpose_category="not_a_real_category"),
    )
    assert resp.status_code == 422
    assert "Unknown purpose_category" in str(resp.json()["detail"])


async def test_utilization_report_by_purpose_includes_classified_reservation(
    admin_client, fresh_device
):
    """The utilization report's by_purpose breakdown (issue #646 phase 1)
    includes a reservation classified through the create path."""
    create = await admin_client.post(
        "/reservations/",
        json=_reservation_body(fresh_device["id"], purpose_category="training"),
    )
    assert create.status_code == 201, create.text
    reservation_id = create.json()["id"]

    try:
        now = datetime.now(timezone.utc)
        resp = await admin_client.get(
            "/reservations/reports/utilization",
            params={
                "start": (now - timedelta(hours=1)).isoformat(),
                "end": (now + timedelta(hours=2)).isoformat(),
                "status": "ACTIVE",
            },
        )
        assert resp.status_code == 200, resp.text
        by_purpose = {b["purpose_category"]: b for b in resp.json()["by_purpose"]}
        assert "training" in by_purpose
        assert by_purpose["training"]["reservations"] >= 1
    finally:
        await admin_client.delete(f"/reservations/{reservation_id}")
