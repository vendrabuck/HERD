"""Playwright e2e: every native modal <dialog> opens centred (issue #979).

Tailwind 4's preflight zeroes every element's margin, which overrides the
user-agent `margin: auto` that centres a modal dialog; before the fix each
dialog rendered at x=0, y=0. The frontend has three native dialogs, and this
test opens one representative of each on the live stack:

- ConfirmDialog: a topology row's Delete, then Cancel (never confirmed).
- Modal: the Topologies page's New Topology, then Cancel.
- BulkImportExport: the Topologies page's Import, then Close.

For each it asserts the dialog box centre is within 2 px of the viewport
centre on both axes and that the box lies fully inside the viewport, at two
viewport sizes (a desktop one and a narrow one where the dialogs fill the
width). The only data written is one transient topology for the Delete row,
created through the API and deleted in a finally block; the read-back after
Cancel proves the destructive action did not run. No seeded data is needed,
so the test runs in the unseeded e2e pass too.
"""

import time

import pytest
from playwright.sync_api import expect

from .conftest import HOST_BASE_URL, pw_api, pw_login

VIEWPORTS = [
    pytest.param({"width": 1280, "height": 720}, id="1280x720"),
    pytest.param({"width": 480, "height": 800}, id="480x800"),
]

TOLERANCE_PX = 2


def _assert_centred(page, viewport) -> None:
    dialog = page.locator("dialog[open]")
    expect(dialog).to_have_count(1)
    expect(dialog).to_be_visible()
    box = dialog.bounding_box()
    assert box, "open dialog has no bounding box"
    cx = box["x"] + box["width"] / 2
    cy = box["y"] + box["height"] / 2
    vx = viewport["width"] / 2
    vy = viewport["height"] / 2
    assert abs(cx - vx) <= TOLERANCE_PX, f"dialog centre x {cx} vs viewport {vx}; box {box}"
    assert abs(cy - vy) <= TOLERANCE_PX, f"dialog centre y {cy} vs viewport {vy}; box {box}"
    assert box["x"] >= 0 and box["y"] >= 0, f"dialog starts outside the viewport: {box}"
    assert box["x"] + box["width"] <= viewport["width"], f"dialog wider than viewport: {box}"
    assert box["y"] + box["height"] <= viewport["height"], f"dialog taller than viewport: {box}"


def _open_topologies(page, viewport) -> None:
    page.set_viewport_size(viewport)
    pw_login(page)
    page.goto(f"{HOST_BASE_URL}/topology")
    expect(page.get_by_role("button", name="New Topology", exact=True)).to_be_visible()


@pytest.mark.parametrize("viewport", VIEWPORTS)
def test_confirm_dialog_is_centred(pw_page, viewport):
    _open_topologies(pw_page, viewport)
    name = f"e2e-pw-979-centre-{int(time.time() * 1000)}"
    topology_id = pw_api(pw_page, "POST", "/cabling/topologies", json={"name": name}).json()["id"]
    try:
        # With no saved sort or search the list is ordered by updated_at
        # descending, so the new row is on page 1. A saved Topologies search or
        # sort for the admin user (#958) could hide it; the failure then names
        # the missing button rather than a centring problem.
        pw_page.reload()
        delete = pw_page.get_by_role("button", name=f"Delete topology {name}", exact=True)
        expect(delete).to_be_visible()
        delete.click()
        dialog = pw_page.locator("dialog[open]")
        expect(dialog.locator("#confirm-dialog-title")).to_be_visible()
        _assert_centred(pw_page, viewport)

        dialog.get_by_role("button", name="Cancel", exact=True).click()
        expect(pw_page.locator("dialog[open]")).to_have_count(0)
        # Cancel must not have deleted anything.
        resp = pw_api(pw_page, "GET", f"/cabling/topologies/{topology_id}", allow_errors=True)
        assert resp.status_code == 200, resp.text
    finally:
        resp = pw_api(pw_page, "DELETE", f"/cabling/topologies/{topology_id}", allow_errors=True)
        assert resp.status_code in (200, 204, 404), resp.text


@pytest.mark.parametrize("viewport", VIEWPORTS)
def test_modal_is_centred(pw_page, viewport):
    _open_topologies(pw_page, viewport)
    pw_page.get_by_role("button", name="New Topology", exact=True).click()
    dialog = pw_page.locator("dialog[open]")
    expect(dialog.locator("#modal-title")).to_have_text("New Topology")
    _assert_centred(pw_page, viewport)

    dialog.get_by_role("button", name="Cancel", exact=True).click()
    expect(pw_page.locator("dialog[open]")).to_have_count(0)


@pytest.mark.parametrize("viewport", VIEWPORTS)
def test_bulk_import_dialog_is_centred(pw_page, viewport):
    _open_topologies(pw_page, viewport)
    pw_page.get_by_role("button", name="Import", exact=True).click()
    dialog = pw_page.locator("dialog[open]")
    expect(dialog.locator("#bulk-import-title")).to_have_text("Import topologies")
    _assert_centred(pw_page, viewport)

    dialog.get_by_role("button", name="Close", exact=True).click()
    expect(pw_page.locator("dialog[open]")).to_have_count(0)
