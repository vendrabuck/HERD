"""Playwright e2e tests for the admin "Classify now" action (issue #822).

The reservation detail modal gained an admin-only Classify now button that
calls POST /api/reservations/admin/purpose-review/{id}/classify (issue #808).
The button renders only on a terminal reservation with no suggestion, so each
test books a reservation through the API and cancels it through the API, then
drives the UI.

What the real-stack test can prove depends on the stack: the gate and nightly
stacks have the classifier off (the endpoint answers 503), a stack with an AI
provider and AI_PURPOSE_CLASSIFICATION_ENABLED may answer 200 with any outcome.
So the test captures the REAL response with expect_response and asserts the UI
and an API read-back against whatever the backend actually said. It passes on
either kind of stack and never skips on a seeded one.

The intercepted-response tests below fulfill the classify call in the browser
with page.route, so the stack cannot be asked to produce a 200 ok. They assert
UI behavior only (toast wording, the button leaving); the backend is not
involved and the reservation is read back only to show it was left alone.

What is left behind: the cancelled reservation itself (a terminal row cannot
be deleted). If a feature-on stack stored a suggestion for it, teardown
dismisses it so it does not linger in the Purpose Review queue.
"""

import re
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from playwright.sync_api import expect

from .conftest import HOST_BASE_URL, log_cleanup_failure, pw_api, pw_login

WAIT_MS = 15000

# Hand-mirrored from frontend/src/lib/purposeClassify.ts. A wording change
# there must change these in the same PR; that is the point of asserting them.
DISABLED_MESSAGE = "Purpose classification is disabled."
OUTCOME_MESSAGES = {
    "timeout": (
        "Classification timed out. Check that the AI orchestrator is responding, then try again."
    ),
    "transient": (
        "The AI orchestrator could not take the request right now. Check its status, "
        "then try again."
    ),
    "failed": "Classification failed. Check the AI orchestrator logs, then try again.",
    "forbidden": (
        "The AI orchestrator refused the request. Check that its internal token "
        "matches this service."
    ),
}

# Hand-mirrored from frontend/src/lib/purposeCategories.ts (the seven defaults;
# anything else humanizes snake_case).
CATEGORY_LABELS = {
    "qa_regression": "QA and regression",
    "support_case_replication": "Support case replication",
    "feature_development": "Feature development",
    "customer_demo_poc": "Customer demo or POC",
    "training": "Training",
    "performance_benchmark": "Performance benchmark",
    "other": "Other",
}


def _category_label(category: str) -> str:
    return CATEGORY_LABELS.get(category) or " ".join(w.capitalize() for w in category.split("_"))


@pytest.fixture
def pw_cancelled_reservation(pw_page):
    """A reservation booked then cancelled through the real API, as admin.

    Cancelling stamps purpose_classify_requested_at in the same transaction,
    so the row is eligible for the on-demand trigger. Yields the reservation
    dict after the read-back confirms CANCELLED with no suggestion.
    """
    pw_login(pw_page)

    devices_resp = pw_api(
        pw_page, "GET", "/inventory/devices?limit=100&dut_only=true", allow_errors=True
    )
    if devices_resp.status_code != 200:
        pytest.fail(f"cannot list devices: {devices_resp.status_code}")
    payload = devices_resp.json()
    items = payload.get("items", payload) if isinstance(payload, dict) else payload
    available = [d for d in items if d.get("status") == "AVAILABLE" and d.get("exclusive", True)]
    assert available, "the seeded stack should have an available exclusive device"

    now = datetime.now(timezone.utc)
    body = {
        "device_ids": [available[0]["id"]],
        "purpose": f"e2e classify now {uuid.uuid4().hex[:8]}",
        "start_time": now.isoformat(),
        "end_time": (now + timedelta(minutes=30)).isoformat(),
    }
    create = pw_api(pw_page, "POST", "/reservations/", json=body, allow_errors=True)
    assert create.status_code == 201, (
        f"reservation create failed: {create.status_code} {create.text}"
    )
    reservation = create.json()
    rid = reservation["id"]
    try:
        cancel = pw_api(pw_page, "DELETE", f"/reservations/{rid}", allow_errors=True)
        assert cancel.status_code == 204, f"cancel failed: {cancel.status_code} {cancel.text}"
        row = pw_api(pw_page, "GET", f"/reservations/{rid}").json()
        assert row["status"] == "CANCELLED"
        assert row.get("purpose_suggestion") is None
        yield row
    finally:
        # Idempotent on a terminal row; covers a failure before the cancel above.
        log_cleanup_failure(
            "reservation",
            rid,
            pw_api(pw_page, "DELETE", f"/reservations/{rid}", allow_errors=True),
        )
        after = pw_api(pw_page, "GET", f"/reservations/{rid}", allow_errors=True)
        if after.status_code == 200 and after.json().get("purpose_suggestion"):
            log_cleanup_failure(
                "purpose suggestion",
                rid,
                pw_api(
                    pw_page,
                    "POST",
                    f"/reservations/admin/purpose-review/{rid}/dismiss",
                    allow_errors=True,
                ),
            )


def _open_detail(page, reservation) -> None:
    page.goto(f"{HOST_BASE_URL}/reservations")
    row = page.locator("table tbody tr", has_text=reservation["purpose"])
    expect(row).to_be_visible(timeout=WAIT_MS)
    row.click()
    expect(page.get_by_role("button", name="Classify now")).to_be_visible(timeout=WAIT_MS)


def _park_pointer(page) -> None:
    # react-hot-toast pauses a toast's timer while the pointer is over the
    # toaster; keep the pointer well away from the top-right stack.
    page.mouse.move(5, 400)


def test_classify_now_matches_the_real_backend_answer(pw_page, pw_cancelled_reservation):
    rid = pw_cancelled_reservation["id"]
    _open_detail(pw_page, pw_cancelled_reservation)

    with pw_page.expect_response(
        lambda r: r.request.method == "POST" and r.url.endswith(f"/purpose-review/{rid}/classify"),
        timeout=WAIT_MS,
    ) as response_info:
        pw_page.get_by_role("button", name="Classify now").click()
    _park_pointer(pw_page)
    response = response_info.value

    readback = pw_api(pw_page, "GET", f"/reservations/{rid}").json()

    if response.status == 503:
        assert response.json()["detail"] == {"error": "purpose_classification_disabled"}
        expect(pw_page.get_by_text(DISABLED_MESSAGE, exact=True)).to_be_visible(timeout=WAIT_MS)
        assert readback.get("purpose_suggestion") is None
        # The refusal leaves the button in place so the admin can retry later.
        expect(pw_page.get_by_role("button", name="Classify now")).to_be_enabled()
        return

    assert response.status == 200, (
        f"unexpected classify answer: {response.status} {response.text()}"
    )
    body = response.json()
    assert body["reservation_id"] == rid
    if body["outcome"] == "ok":
        suggestion = body["purpose_suggestion"]
        assert suggestion is not None
        label = _category_label(suggestion["top_category"])
        expect(pw_page.get_by_text(f"Suggested category: {label}.", exact=True)).to_be_visible(
            timeout=WAIT_MS
        )
        assert readback["purpose_suggestion"]["top_category"] == suggestion["top_category"]
        expect(pw_page.get_by_role("button", name="Classify now")).to_have_count(0)
    else:
        assert body["outcome"] in OUTCOME_MESSAGES, body["outcome"]
        expect(pw_page.get_by_text(OUTCOME_MESSAGES[body["outcome"]], exact=True)).to_be_visible(
            timeout=WAIT_MS
        )
        assert body["purpose_suggestion"] is None
        assert readback.get("purpose_suggestion") is None
        expect(pw_page.get_by_role("button", name="Classify now")).to_be_enabled()


def test_classify_now_is_absent_on_a_reservation_that_is_not_terminal(pw_page):
    pw_login(pw_page)
    devices = pw_api(pw_page, "GET", "/inventory/devices?limit=100&dut_only=true").json()
    items = devices.get("items", devices) if isinstance(devices, dict) else devices
    available = [d for d in items if d.get("status") == "AVAILABLE" and d.get("exclusive", True)]
    assert available, "the seeded stack should have an available exclusive device"
    now = datetime.now(timezone.utc)
    purpose = f"e2e classify live {uuid.uuid4().hex[:8]}"
    create = pw_api(
        pw_page,
        "POST",
        "/reservations/",
        json={
            "device_ids": [available[0]["id"]],
            "purpose": purpose,
            "start_time": now.isoformat(),
            "end_time": (now + timedelta(minutes=30)).isoformat(),
        },
        allow_errors=True,
    )
    assert create.status_code == 201, (
        f"reservation create failed: {create.status_code} {create.text}"
    )
    rid = create.json()["id"]
    try:
        assert pw_api(pw_page, "GET", f"/reservations/{rid}").json()["status"] in {
            "PENDING",
            "PENDING_PROVISION",
            "ACTIVE",
        }
        pw_page.goto(f"{HOST_BASE_URL}/reservations")
        row = pw_page.locator("table tbody tr", has_text=purpose)
        expect(row).to_be_visible(timeout=WAIT_MS)
        row.click()
        expect(pw_page.get_by_text("Purpose category", exact=True)).to_be_visible(timeout=WAIT_MS)
        expect(pw_page.get_by_role("button", name="Classify now")).to_have_count(0)
    finally:
        log_cleanup_failure(
            "reservation",
            rid,
            pw_api(pw_page, "DELETE", f"/reservations/{rid}", allow_errors=True),
        )


# Intercepted-response tests: UI behavior only, the backend is never asked.
INTERCEPTED_OK_BODY = {
    "outcome": "ok",
    "purpose_suggestion": {
        "distribution": [{"category": "qa_regression", "probability": 0.9}],
        "top_category": "qa_regression",
        "pass": "end",
        "model": "intercepted",
        "rationale": "intercepted in the browser",
        "generated_at": "2026-06-02T01:00:00Z",
        "signals_used": ["purpose_text"],
    },
}


@pytest.mark.parametrize("outcome", ["ok", "timeout", "transient", "failed", "forbidden"])
def test_classify_now_ui_with_intercepted_200_response(pw_page, pw_cancelled_reservation, outcome):
    """UI only: the browser is handed a fabricated 200, so nothing is stored."""
    rid = pw_cancelled_reservation["id"]

    def fulfill(route):
        body = (
            INTERCEPTED_OK_BODY
            if outcome == "ok"
            else {"outcome": outcome, "purpose_suggestion": None}
        )
        route.fulfill(status=200, json={"reservation_id": rid, **body})

    pw_page.route(re.compile(r".*/purpose-review/.+/classify$"), fulfill)
    _open_detail(pw_page, pw_cancelled_reservation)
    pw_page.get_by_role("button", name="Classify now").click()
    _park_pointer(pw_page)

    if outcome == "ok":
        expect(
            pw_page.get_by_text("Suggested category: QA and regression.", exact=True)
        ).to_be_visible(timeout=WAIT_MS)
        expect(pw_page.get_by_role("button", name="Classify now")).to_have_count(0)
    else:
        expect(pw_page.get_by_text(OUTCOME_MESSAGES[outcome], exact=True)).to_be_visible(
            timeout=WAIT_MS
        )
        expect(pw_page.get_by_role("button", name="Classify now")).to_be_enabled()
    # Nothing reached the backend, so the row has no suggestion.
    assert pw_api(pw_page, "GET", f"/reservations/{rid}").json().get("purpose_suggestion") is None
