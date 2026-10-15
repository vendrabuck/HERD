"""Playwright e2e tests for applying device configuration from the browser (issue #1100).

The four journeys the device page and the dry-run review offer, each driven through
the UI and then proved by reading the backend back through the API:

- Apply now from the Apply dialog (CFG-UI-4, CFG-APPLY-1 to CFG-APPLY-4).
- Schedule from the same dialog, watched in the Scheduled applies panel until the
  apply scheduler fires it (CFG-UI-4, CFG-UI-5, CFG-JOB-7, CFG-SCHED-8, CFG-SCHED-9).
- Cancel a pending job from the panel (CFG-UI-5, CFG-JOB-10, CFG-STATE-3).
- Review a fired dry run and confirm it (CFG-UI-6, CFG-JOB-11, CFG-JOB-12).

Every test seeds its own world through the API and removes it in a `finally`: the
checked-in `drivers/frr_mgmt` package uploaded unchanged under a per-run name, a
Management template on it, and one device. Nothing seeded on the stack is touched,
so the tests never skip on a seeded stack and need no `seeded_skip_ok` marker.

Why nothing here can reach a router. The device page offers no dry-run control:
Apply now sends no `dry_run` (CFG-APPLY-1) and Schedule sends only `scheduled_for`,
so both are REAL applies. The device these tests create carries no `HERD_ip` and no
`HERD_login`, and the execution sandbox calls the driver's `configure` directly
(`services/execution/app/services/_runner.py`), whose `_connect` builds netmiko's
`ConnectHandler(host=None, ...)`; netmiko refuses that in its constructor with
`ValueError: Either ip or host must be set` before it opens any socket. The run is
therefore FAILED with `driver raised ValueError` and an empty transcript, which is
what these tests pin. The dry-run review's job is a real dry run scheduled through the
API (the device page cannot schedule one; the reservation assistant does), and a dry
run of frr_mgmt opens no SSH session by the driver's own contract. The real job a
confirm queues is cancelled at once and the 204 proves it never fired.

The dry-run review opens only from the reservation assistant. Its two network calls
are answered in the page (`page.route`): `/api/ai/status` reports the assistant
enabled, and the assistant turn returns a `pending_apply` naming the real dry-run job.
Nothing reaches a model. Everything the review dialog shows after that (the job's
status, the run's transcript) and the confirm it sends are real backend reads and
writes. Both assistant transports are answered, so the test runs whether the
frontend was built with `VITE_AI_CHAT_ENABLED` (the streaming chat) or without it
(the single-shot form, the default build).
"""

from __future__ import annotations

import io
import json
import os
import re
import tarfile
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from playwright.sync_api import expect

from .conftest import (
    PW_OFF_TOASTER,
    log_cleanup_failure,
    pw_api,
    pw_click_for_response,
    pw_login,
)

HOST_BASE_URL = os.environ.get("E2E_HOST_BASE_URL", "https://localhost")
FRR_MGMT_DIR = Path(__file__).resolve().parents[2] / "drivers" / "frr_mgmt"
COMMANDS = ["ip route 192.0.2.0/24 blackhole", "ip route 198.51.100.0/24 blackhole"]
TERMINAL = {"success", "failed", "skipped", "cancelled"}
WAIT_MS = 15_000

# The run error of a real apply to a device with no HERD_ip (module docstring):
# execution stores the class name only (CFG-RUNSTATE-4, issue #840).
HOSTLESS_RUN_ERROR = "driver raised ValueError"

# Texts from DeviceConfigSection.tsx, ApplyJobsPanel.tsx, and AIApplyConfirmModal.tsx.
APPLY_DIALOG = "Apply config to device"
REVIEW_DIALOG = "Review AI-proposed change"
SCHEDULED_TOAST = "Scheduled"
CANCELLED_TOAST = "Cancelled"
CONFIRMED_TOAST = "Real apply scheduled. Watch the device's apply jobs for the result."
DRY_RUN_WAITING = "Waiting for dry-run to complete..."
DRY_RUN_SUCCEEDED = "Dry-run succeeded."

PLAN_TEXT = "Add two blackhole routes for the documentation prefixes."

# A local wall-clock time in the browser's own zone, in the datetime-local input's
# format, plus the ISO instant `new Date(local).toISOString()` gives for it (what
# DeviceConfigSection sends). Computed in the page so the browser's time zone, not
# the host's, decides the conversion. Whole seconds only, and no seconds part at all
# when they are zero: Chromium keeps a datetime-local value in its shortest form, so
# it stores `T20:49:00` as `T20:49`, and Playwright's fill then refuses the value it
# typed as malformed (a one-in-sixty chance per fill before this rule).
_LOCAL_AHEAD_JS = """function (ms) {
  const d = new Date(Date.now() + ms);
  const p = function (n) { return String(n).padStart(2, "0"); };
  const seconds = d.getSeconds() ? `:${p(d.getSeconds())}` : "";
  const local = `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}` +
    `T${p(d.getHours())}:${p(d.getMinutes())}${seconds}`;
  return { local, iso: new Date(local).toISOString() };
}"""


def _frr_mgmt_tarball() -> bytes:
    """The checked-in drivers/frr_mgmt package, unchanged, as an upload."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name in ("driver.py", "driver_metadata.json"):
            tf.add(FRR_MGMT_DIR / name, arcname=name)
    return buf.getvalue()


def _seed(page, suffix: str) -> dict:
    """Upload frr_mgmt, a Management template on it, and one device with no host.

    Returns the ids created so far even when a later step fails, so the caller's
    `finally` can remove what exists.
    """
    seed: dict = {}
    driver = pw_api(
        page,
        "POST",
        "/inventory/drivers",
        files={"file": ("frr_mgmt.tar.gz", _frr_mgmt_tarball(), "application/gzip")},
        data={
            "name": f"pw-cfg-apply-frr-{suffix}",
            "connection_type": "Management",
            "description": "e2e: frr_mgmt for the config apply journeys",
        },
    ).json()
    seed["driver_id"] = driver["id"]
    assert driver["supports_dry_run"] is True, driver
    template = pw_api(
        page,
        "POST",
        "/inventory/templates",
        json={
            "name": f"pw-cfg-apply-tpl-{suffix}",
            "template_type": "device",
            "driver_id": driver["id"],
            "vendor": "FRRouting",
            "model": "E2ERouter",
            "sections": [
                {
                    "name": "General",
                    "fields": [{"key": "model", "label": "Model", "type": "string"}],
                }
            ],
        },
    ).json()
    seed["template_id"] = template["id"]
    device = pw_api(
        page,
        "POST",
        "/inventory/devices",
        json={
            "name": f"pw-cfg-apply-dev-{suffix}",
            "template_id": template["id"],
            "topology_type": "PHYSICAL",
            "status": "AVAILABLE",
            # No HERD_ip and no HERD_login: a real apply cannot open a session.
            "field_data": {"model": "e2e"},
        },
    ).json()
    seed["device_id"] = device["id"]
    return seed


def _teardown(page, seed: dict, reservation: dict | None) -> None:
    """Cancel the reservation, then delete device, template, and driver.

    The reservation goes first: a live reservation makes the device delete answer
    409 device_in_use. Deleting the device removes its versions and apply jobs
    (CFG-VER-15). Each refusal is printed, never hidden.
    """
    if reservation is not None:
        resp = pw_api(page, "DELETE", f"/reservations/{reservation['id']}", allow_errors=True)
        log_cleanup_failure("reservation", reservation["id"], resp)
    for kind, key, path in (
        ("device", "device_id", "/inventory/devices/"),
        ("template", "template_id", "/inventory/templates/"),
        ("driver", "driver_id", "/inventory/drivers/"),
    ):
        if key in seed:
            resp = pw_api(page, "DELETE", f"{path}{seed[key]}", allow_errors=True)
            log_cleanup_failure(kind, seed[key], resp)


def _create_version(page, device_id: str) -> dict:
    resp = pw_api(
        page,
        "POST",
        f"/inventory/devices/{device_id}/config-versions",
        json={"config": {"commands": COMMANDS}, "description": "e2e apply"},
    )
    assert resp.status_code == 201, resp.text
    version = resp.json()
    assert version["version_number"] == 1, version
    return version


def _book_active(page, device_id: str, suffix: str) -> dict:
    """An ACTIVE reservation of the device held by the signed-in admin.

    The scheduler fires a job only while its creator still has standing on the
    device (CFG-SCHED-6), and an admin has none of its own: holding an active
    reservation of the device is the standing.
    """
    now = datetime.now(timezone.utc)
    resp = pw_api(
        page,
        "POST",
        "/reservations/",
        json={
            "device_ids": [device_id],
            "purpose": f"e2e config apply {suffix}",
            "start_time": now.isoformat(),
            "end_time": (now + timedelta(hours=1)).isoformat(),
        },
        allow_errors=True,
    )
    assert resp.status_code == 201, resp.text
    reservation = resp.json()
    assert reservation["status"] == "ACTIVE", reservation
    return reservation


def _wait_for_terminal(page, job_id: str, budget_seconds: float) -> dict:
    deadline = time.monotonic() + budget_seconds
    while True:
        job = pw_api(page, "GET", f"/inventory/apply-jobs/{job_id}").json()
        if job["status"] in TERMINAL or time.monotonic() >= deadline:
            return job
        time.sleep(0.5)


def _open_device(page, device_id: str) -> None:
    page.goto(f"{HOST_BASE_URL}/inventory/{device_id}")
    expect(page.get_by_text("Configuration history", exact=True)).to_be_visible(timeout=WAIT_MS)


def _open_apply_dialog(page):
    """Click Apply on the v1 row; return the open Apply dialog."""
    page.locator("tr", has_text="v1").get_by_role("button", name="Apply", exact=True).click()
    expect(page.get_by_role("heading", name=APPLY_DIALOG, exact=True)).to_be_visible()
    return page.locator("dialog[open]", has=page.get_by_role("heading", name=APPLY_DIALOG))


def _panel_rows(page):
    """The body rows of the Scheduled applies panel's table."""
    return page.get_by_text("Scheduled applies", exact=True).locator(
        "xpath=following-sibling::table[1]/tbody/tr"
    )


def _assert_hostless_failed_run(page, run_id: str, version: dict) -> None:
    """The run is a real configure of the version that failed before any session."""
    run = pw_api(page, "GET", f"/execution/runs/{run_id}").json()
    assert run["status"] == "FAILED", run
    assert run["action"] == "configure"
    assert run["error"] == HOSTLESS_RUN_ERROR
    assert run["input_params"]["dry_run"] is False
    assert run["input_params"]["config_version_id"] == version["id"]
    assert run["input_params"]["method_kwargs"] == {"commands": COMMANDS}
    # No command was recorded: the driver never reached a device.
    commands = pw_api(page, "GET", f"/execution/runs/{run_id}/commands").json()
    assert commands == []


def test_apply_now_answers_the_failed_run_and_records_it_on_the_version(pw_page):
    """Apply now pushes for real, answers the failed run, and records it on the version.

    CFG-UI-4: the failed answer reaches the user as `Apply failed: <error>`.
    CFG-APPLY-1 to CFG-APPLY-4: execution ran a `configure` of the version's config
    (no dry run), the answer carries its run and error, and the version's
    `last_apply_run_id` names that run.
    """
    pw_login(pw_page)
    suffix = uuid.uuid4().hex[:8]
    seed: dict = {}
    try:
        seed = _seed(pw_page, suffix)
        device_id = seed["device_id"]
        version = _create_version(pw_page, device_id)

        _open_device(pw_page, device_id)
        dialog = _open_apply_dialog(pw_page)
        response = pw_click_for_response(
            pw_page,
            dialog.get_by_role("button", name="Apply now", exact=True),
            "POST",
            f"/inventory/devices/{device_id}/config-versions/{version['id']}/apply",
        )
        body = response.json()
        assert body["version_id"] == version["id"]
        assert body["status"] == "failed", body
        assert body["error"] == HOSTLESS_RUN_ERROR
        run_id = body["run_id"]
        assert run_id
        expect(
            pw_page.get_by_text(f"Apply failed: {HOSTLESS_RUN_ERROR}", exact=True)
        ).to_be_visible()
        # The dialog closes after an answer, failed or not.
        expect(pw_page.get_by_role("heading", name=APPLY_DIALOG, exact=True)).to_have_count(0)

        # Read-back: the version records the run (CFG-APPLY-4). The device's current
        # config pointer is returned by no route (CFG-APPLY-6), so it is not read here.
        stored = pw_api(
            pw_page, "GET", f"/inventory/devices/{device_id}/config-versions/{version['id']}"
        ).json()
        assert stored["last_apply_run_id"] == run_id
        _assert_hostless_failed_run(pw_page, run_id, version)
        # An immediate apply is not a job: the panel stays hidden.
        jobs = pw_api(pw_page, "GET", f"/inventory/devices/{device_id}/apply-jobs").json()
        assert jobs["total"] == 0
        expect(pw_page.get_by_text("Scheduled applies", exact=True)).to_have_count(0)
    finally:
        _teardown(pw_page, seed, None)


def test_schedule_from_the_dialog_queues_a_job_the_scheduler_fires(pw_page):
    """Schedule stores a pending real job at the chosen time; the scheduler fires it.

    CFG-UI-4: a set time schedules instead of applying, sent as the ISO instant of the
    chosen local time and nothing else. CFG-JOB-7: the job is stored pending with
    that time, not a dry run, tied to no reservation. CFG-UI-5: the panel lists it as
    pending with Cancel, then shows the fired outcome with its error and run.
    CFG-SCHED-8 and CFG-SCHED-9: the scheduler fired a real `configure` of the version
    through execution and recorded the run and its error on the job.
    """
    pw_login(pw_page)
    suffix = uuid.uuid4().hex[:8]
    seed: dict = {}
    reservation = None
    try:
        seed = _seed(pw_page, suffix)
        device_id = seed["device_id"]
        version = _create_version(pw_page, device_id)
        reservation = _book_active(pw_page, device_id, suffix)

        _open_device(pw_page, device_id)
        expect(pw_page.get_by_text("Scheduled applies", exact=True)).to_have_count(0)
        dialog = _open_apply_dialog(pw_page)
        when = pw_page.evaluate(_LOCAL_AHEAD_JS, 8_000)
        dialog.locator("#schedule-at").fill(when["local"])
        response = pw_click_for_response(
            pw_page,
            dialog.get_by_role("button", name="Schedule", exact=True),
            "POST",
            f"/inventory/devices/{device_id}/config-versions/{version['id']}/schedule",
            status=201,
        )
        assert response.request.post_data_json == {"scheduled_for": when["iso"]}
        job = response.json()
        assert job["status"] == "pending", job
        assert job["dry_run"] is False
        expect(pw_page.get_by_text(SCHEDULED_TOAST, exact=True)).to_be_visible()

        # Read-back: the stored job (CFG-JOB-7).
        stored = pw_api(pw_page, "GET", f"/inventory/apply-jobs/{job['id']}").json()
        assert stored["device_id"] == device_id
        assert stored["version_id"] == version["id"]
        assert stored["dry_run"] is False
        assert stored["reservation_id"] is None
        assert datetime.fromisoformat(stored["scheduled_for"].replace("Z", "+00:00")) == (
            datetime.fromisoformat(when["iso"].replace("Z", "+00:00"))
        )
        assert stored["status"] in {"pending", "running", "failed"}, stored

        # Wait for the scheduler (every 2 s on the dev and gate stacks).
        fired = _wait_for_terminal(pw_page, job["id"], budget_seconds=40)
        assert fired["status"] == "failed", fired
        assert fired["error"] == HOSTLESS_RUN_ERROR
        assert fired["fired_at"] is not None
        run_id = fired["run_id"]
        assert run_id
        _assert_hostless_failed_run(pw_page, run_id, version)

        # The panel catches up on its 10 second refresh (CFG-UI-5).
        rows = _panel_rows(pw_page)
        expect(rows).to_have_count(1)
        row = rows.first
        status_cell = row.locator("td").nth(1)
        expect(status_cell).to_have_text(f"failed({HOSTLESS_RUN_ERROR})", timeout=WAIT_MS)
        expect(row.locator("td").nth(3)).to_have_text(run_id[:8])
        expect(row.get_by_role("button", name="Cancel", exact=True)).to_have_count(0)
    finally:
        _teardown(pw_page, seed, reservation)


def test_cancel_a_pending_scheduled_apply_from_the_panel(pw_page):
    """A pending job scheduled an hour out is cancelled from the panel, for good.

    CFG-UI-5: the panel offers Cancel on the pending row and drops it once the job is
    cancelled. CFG-JOB-10 and CFG-STATE-3: the creator's cancel answers 204 and the
    job reads back `cancelled`, never fired and with no run.
    """
    pw_login(pw_page)
    suffix = uuid.uuid4().hex[:8]
    seed: dict = {}
    try:
        seed = _seed(pw_page, suffix)
        device_id = seed["device_id"]
        version = _create_version(pw_page, device_id)

        _open_device(pw_page, device_id)
        dialog = _open_apply_dialog(pw_page)
        when = pw_page.evaluate(_LOCAL_AHEAD_JS, 60 * 60 * 1000)
        dialog.locator("#schedule-at").fill(when["local"])
        response = pw_click_for_response(
            pw_page,
            dialog.get_by_role("button", name="Schedule", exact=True),
            "POST",
            f"/inventory/devices/{device_id}/config-versions/{version['id']}/schedule",
            status=201,
        )
        job = response.json()
        assert job["status"] == "pending", job

        rows = _panel_rows(pw_page)
        expect(rows).to_have_count(1)
        expect(rows.first.locator("td").nth(1)).to_have_text("pending")
        cancel = rows.first.get_by_role("button", name="Cancel", exact=True)
        pw_click_for_response(
            pw_page, cancel, "DELETE", f"/inventory/apply-jobs/{job['id']}", status=204
        )
        expect(pw_page.get_by_text(CANCELLED_TOAST, exact=True)).to_be_visible()

        # Read-back: cancelled, never fired, no run (CFG-STATE-3).
        stored = pw_api(pw_page, "GET", f"/inventory/apply-jobs/{job['id']}").json()
        assert stored["status"] == "cancelled", stored
        assert stored["fired_at"] is None
        assert stored["run_id"] is None
        assert stored["error"] is None

        # The panel shows the new status and no longer offers Cancel.
        expect(rows.first.locator("td").nth(1)).to_have_text("cancelled")
        expect(rows.first.get_by_role("button", name="Cancel", exact=True)).to_have_count(0)
    finally:
        _teardown(pw_page, seed, None)


def _route_assistant(page, reservation_id: str, pending_apply: dict) -> list:
    """Answer the AI status and both assistant transports in the page.

    Returns the list of assistant requests answered, so the test can prove the turn
    went to this stub and nowhere else.
    """
    calls: list = []
    status = {
        "enabled": True,
        "provider": "anthropic",
        "model": "e2e-stub",
        "recipe_authoring": False,
        "purpose_classification": False,
        "degraded": False,
        "reason": None,
    }
    result = {
        "answer": PLAN_TEXT,
        "model": "e2e-stub",
        "input_tokens": 0,
        "output_tokens": 0,
        "stop_reason": "end_turn",
        "tool_calls": [
            {
                "name": "schedule_config_apply",
                "arguments_summary": "dry run",
                "duration_ms": 1,
                "error": None,
            }
        ],
        "tool_iterations": 1,
        "conversation_id": None,
        "pending_apply": pending_apply,
    }

    def answer_status(route):
        route.fulfill(status=200, content_type="application/json", body=json.dumps(status))

    def answer_buffered(route):
        calls.append(route.request.url)
        route.fulfill(status=200, content_type="application/json", body=json.dumps(result))

    def answer_stream(route):
        calls.append(route.request.url)
        route.fulfill(
            status=200,
            content_type="text/event-stream",
            body=f"event: done\ndata: {json.dumps(result)}\n\n",
        )

    base = re.escape(f"/api/ai/reservations/{reservation_id}/assistant")
    page.route(re.compile(r".*/api/ai/status$"), answer_status)
    page.route(re.compile(f".*{base}$"), answer_buffered)
    page.route(re.compile(f".*{base}/stream$"), answer_stream)
    return calls


def _ask_the_assistant(page) -> str:
    """Send one question through whichever assistant form this build renders.

    Returns the path the turn must have gone to: the streaming route for the chat
    (`VITE_AI_CHAT_ENABLED`), the buffered route for the single-shot form.
    """
    chat = page.get_by_test_id("assistant-input")
    single = page.get_by_placeholder(
        "What devices are in my reservation and how are they connected?", exact=True
    )
    expect(chat.or_(single)).to_be_visible(timeout=WAIT_MS)
    if chat.count():
        chat.fill("Add the two documentation routes.")
        page.get_by_test_id("assistant-send").click()
        return "/assistant/stream"
    single.fill("Add the two documentation routes.")
    page.get_by_role("button", name="Ask", exact=True).click()
    return "/assistant"


def test_dry_run_review_shows_the_transcript_and_confirm_queues_the_real_apply(pw_page):
    """The review waits for the real dry run, shows its transcript, and confirms it.

    CFG-UI-6: the dialog polls the job, shows the waiting state with Confirm disabled,
    then the success line and the run's transcript rows marked simulated, and enables
    Confirm. CFG-JOB-11 and CFG-JOB-12: Confirm promotes the successful dry run into
    a new pending real job for the same device, version, and reservation, and the
    source job is left as it was. The promoted job is cancelled at once; its 204 is
    the proof it never fired.
    """
    pw_login(pw_page)
    suffix = uuid.uuid4().hex[:8]
    seed: dict = {}
    reservation = None
    promoted_id = None
    try:
        seed = _seed(pw_page, suffix)
        device_id = seed["device_id"]
        version = _create_version(pw_page, device_id)
        reservation = _book_active(pw_page, device_id, suffix)

        # The dry run the assistant would have scheduled (the device page cannot).
        # Due far enough out that the dialog opens before the scheduler fires it.
        scheduled = pw_api(
            pw_page,
            "POST",
            f"/inventory/devices/{device_id}/config-versions/{version['id']}/schedule",
            json={
                "scheduled_for": (datetime.now(timezone.utc) + timedelta(seconds=25)).isoformat(),
                "dry_run": True,
                "reservation_id": reservation["id"],
            },
            allow_errors=True,
        )
        assert scheduled.status_code == 201, scheduled.text
        source = scheduled.json()
        assert source["dry_run"] is True and source["status"] == "pending", source

        calls = _route_assistant(
            pw_page,
            reservation["id"],
            {
                "job_id": source["id"],
                "version_id": version["id"],
                "device_id": device_id,
                "dry_run": True,
                "scheduled_for": source["scheduled_for"],
            },
        )
        pw_page.goto(f"{HOST_BASE_URL}/reservations")
        row = pw_page.locator("table tbody tr", has_text=reservation["purpose"])
        expect(row).to_be_visible(timeout=WAIT_MS)
        row.get_by_text(reservation["purpose"], exact=True).click()
        pw_page.get_by_role("button", name="AI Assistant", exact=True).click()
        transport = _ask_the_assistant(pw_page)

        review = pw_page.locator(
            "dialog[open]", has=pw_page.get_by_role("heading", name=REVIEW_DIALOG)
        )
        expect(review).to_be_visible(timeout=WAIT_MS)
        # One turn, answered by the stub on the route this build's form uses.
        assert len(calls) == 1, calls
        assert calls[0].endswith(f"/reservations/{reservation['id']}{transport}"), calls
        expect(review.get_by_text(PLAN_TEXT, exact=True)).to_be_visible()
        confirm = review.get_by_role("button", name="Confirm and apply", exact=True)

        # Before the scheduler fires the job: waiting, Confirm disabled.
        expect(review.get_by_text(DRY_RUN_WAITING, exact=True)).to_be_visible()
        expect(confirm).to_be_disabled()

        # After: the real job reads success and the dialog follows on its own poll.
        fired = _wait_for_terminal(pw_page, source["id"], budget_seconds=60)
        assert fired["status"] == "success", fired
        expect(review.get_by_text(DRY_RUN_SUCCEEDED, exact=True)).to_be_visible(timeout=WAIT_MS)

        # The transcript the dialog shows is the run's own, row for row.
        transcript = pw_api(pw_page, "GET", f"/execution/runs/{fired['run_id']}/commands").json()
        assert [r["command"] for r in transcript] == COMMANDS
        assert {r["exit_status"] for r in transcript} == {"simulated"}
        rows = review.locator("tbody tr")
        expect(rows).to_have_count(len(COMMANDS))
        for index, command in enumerate(COMMANDS):
            cells = rows.nth(index).locator("td")
            expect(cells.nth(1)).to_have_text(command)
            expect(cells.nth(3)).to_have_text("simulated")

        pw_page.mouse.move(*PW_OFF_TOASTER)
        response = pw_click_for_response(
            pw_page, confirm, "POST", f"/inventory/apply-jobs/{source['id']}/confirm", status=201
        )
        promoted = response.json()
        promoted_id = promoted["id"]
        expect(pw_page.get_by_text(CONFIRMED_TOAST, exact=True)).to_be_visible()
        expect(pw_page.get_by_role("heading", name=REVIEW_DIALOG, exact=True)).to_have_count(0)

        # Read-back: a new pending real job for the same device, version, reservation.
        stored = pw_api(pw_page, "GET", f"/inventory/apply-jobs/{promoted_id}").json()
        assert stored["id"] != source["id"]
        assert stored["status"] == "pending", stored
        assert stored["dry_run"] is False
        assert stored["device_id"] == device_id
        assert stored["version_id"] == version["id"]
        assert stored["reservation_id"] == reservation["id"]
        # The source dry run is unchanged.
        after = pw_api(pw_page, "GET", f"/inventory/apply-jobs/{source['id']}").json()
        assert after == fired

        # Cancel the real job before it is due: a 204 means it never fires (CFG-STATE-4).
        cancelled = pw_api(
            pw_page, "DELETE", f"/inventory/apply-jobs/{promoted_id}", allow_errors=True
        )
        assert cancelled.status_code == 204, cancelled.text
        promoted_id = None
    finally:
        if promoted_id is not None:
            resp = pw_api(
                pw_page, "DELETE", f"/inventory/apply-jobs/{promoted_id}", allow_errors=True
            )
            log_cleanup_failure("apply job", promoted_id, resp)
        _teardown(pw_page, seed, reservation)
