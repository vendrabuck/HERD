"""Inventory page column filters (issue #842), driven in a real browser.

Self-seeding: the test uploads its own Management driver, creates two device
templates (one with a vendor and model) and three devices, all named with one
per-run token, so it runs on an unseeded stack and never skips. The search box
is set to that token first, which scopes every list to the test's own rows
whatever else lives on the stack; the Status and Template filters then compose
with it exactly as a user would.

After every filter action the test asserts BOTH the rendered rows and an API
read-back made with the IDENTICAL query parameters (same ids, same total), so a
filter that renders correctly but sends the wrong request, or the reverse, fails.

The admin's `saved_filters.inventory` preference is per-user server state shared
with every other session on the same account. The test reads the baseline first,
waits for the debounced preference PATCH that carries its final choice (a late
PATCH landing after the restore would poison the next run), and restores the
baseline in a finally block with a read-back.
"""

import uuid
from urllib.parse import parse_qs, urlparse

import pytest
from playwright.sync_api import expect

from .conftest import (
    HOST_BASE_URL,
    driver_tarball,
    log_cleanup_failure,
    pw_api,
    pw_login,
    pw_prefs_patch_carries,
)


def _is_list_request(response, expected: dict[str, str]) -> bool:
    """True for the page's own paginated list GET carrying exactly `expected` filters."""
    if response.request.method != "GET":
        return False
    parsed = urlparse(response.url)
    if not parsed.path.endswith("/api/inventory/devices"):
        return False
    params = {k: v[0] for k, v in parse_qs(parsed.query).items()}
    if params.pop("limit", None) != "50" or params.pop("skip", None) != "0":
        return False
    return params == expected


def _read_inventory_pref(page) -> dict:
    resp = pw_api(page, "GET", "/user-profile/preferences")
    return resp.json().get("saved_filters", {}).get("inventory") or {}


def test_inventory_status_and_template_filters(pw_page):
    pw_login(pw_page)
    token = uuid.uuid4().hex[:10]
    vendor = f"E2EVendor{token}"
    model = f"M{token}"

    driver_id: str | None = None
    template_ids: list[str] = []
    device_ids: list[str] = []
    baseline: dict = {}
    baseline_read = False

    try:
        driver = pw_api(
            pw_page,
            "POST",
            "/inventory/drivers",
            files={"file": ("e2e-filters.tar.gz", driver_tarball(), "application/gzip")},
            data={
                "name": f"e2e-inv-filters-drv-{token}",
                "connection_type": "Management",
                "description": "inventory filters e2e driver",
            },
        ).json()
        driver_id = driver["id"]

        def make_template(name: str, **extra) -> dict:
            created = pw_api(
                pw_page,
                "POST",
                "/inventory/templates",
                json={
                    "name": name,
                    "template_type": "device",
                    "driver_id": driver_id,
                    "sections": [
                        {
                            "name": "General",
                            "fields": [{"key": "model", "label": "Model", "type": "string"}],
                        }
                    ],
                    **extra,
                },
            ).json()
            template_ids.append(created["id"])
            return created

        tmpl_a = make_template(f"e2e-filters-tmpl-a-{token}", vendor=vendor, model=model)
        tmpl_b = make_template(
            f"e2e-filters-tmpl-b-{token}", vendor=f"{vendor}B", model=f"{model}B"
        )
        tmpl_a_label = f"{tmpl_a['name']} ({vendor} {model})"
        tmpl_b_label = f"{tmpl_b['name']} ({vendor}B {model}B)"

        def make_device(suffix: str, template: dict, status: str) -> dict:
            created = pw_api(
                pw_page,
                "POST",
                "/inventory/devices",
                json={
                    "name": f"e2e-filters-{token}-{suffix}",
                    "template_id": template["id"],
                    "topology_type": "PHYSICAL",
                    "status": status,
                    "field_data": {"model": "x"},
                },
            ).json()
            device_ids.append(created["id"])
            return created

        # Never AVAILABLE: another session on the shared stack may book any
        # available device, and a booked device cannot be deleted at cleanup.
        d1 = make_device("1", tmpl_a, "MAINTENANCE")
        d2 = make_device("2", tmpl_a, "OFFLINE")
        d3 = make_device("3", tmpl_b, "MAINTENANCE")
        by_id = {d["id"]: d for d in (d1, d2, d3)}

        baseline = _read_inventory_pref(pw_page)
        baseline_read = True

        pw_page.goto(f"{HOST_BASE_URL}/inventory")
        search = pw_page.get_by_placeholder("Search devices by name...")
        status_select = pw_page.get_by_label("Status", exact=True)
        template_select = pw_page.get_by_label("Template", exact=True)

        # A baseline preference left by another session must not leak in: start
        # from the cleared state (the finally block restores the baseline). Use
        # the Filters region's control: a saved filter that matches nothing also
        # renders a second "Clear filters" in the table's empty state (issue #1070).
        pw_page.wait_for_load_state("networkidle")
        panel_clear = pw_page.get_by_role("region", name="Filters").get_by_role(
            "button", name="Clear filters"
        )
        if panel_clear.count():
            panel_clear.click()

        with pw_page.expect_response(lambda r: _is_list_request(r, {"search": token})):
            search.fill(token)

        def assert_view(expected_devices: list[dict], params: dict[str, str]) -> None:
            """Rendered rows and an API read-back with the same params must agree."""
            readback = pw_api(
                pw_page,
                "GET",
                "/inventory/devices",
                params={**params, "skip": 0, "limit": 50},
            ).json()
            assert {d["id"] for d in readback["items"]} == {d["id"] for d in expected_devices}
            assert readback["total"] == len(expected_devices)
            for device in by_id.values():
                row = pw_page.locator("tbody tr", has_text=device["name"])
                expect(row).to_have_count(1 if device in expected_devices else 0)
            expect(pw_page.get_by_role("heading", name="All Devices")).to_contain_text(
                f"({readback['total']})"
            )

        # Baseline: the search alone scopes the list to the three test devices.
        expect(pw_page.locator("tbody tr", has_text=f"e2e-filters-{token}-")).to_have_count(3)
        assert_view([d1, d2, d3], {"search": token})

        # Status filter alone.
        with pw_page.expect_response(
            lambda r: _is_list_request(r, {"search": token, "status": "OFFLINE"})
        ):
            status_select.select_option("OFFLINE")
        expect(pw_page.locator("tbody tr", has_text=d2["name"])).to_have_count(1)
        assert_view([d2], {"search": token, "status": "OFFLINE"})

        with pw_page.expect_response(
            lambda r: _is_list_request(r, {"search": token, "status": "MAINTENANCE"})
        ):
            status_select.select_option("MAINTENANCE")
        expect(pw_page.locator("tbody tr", has_text=d2["name"])).to_have_count(0)
        assert_view([d1, d3], {"search": token, "status": "MAINTENANCE"})

        # Back to All sends no status parameter.
        # A revisited filter set is served from the page's query cache with no
        # new request, so this step syncs on the rendered rows instead.
        status_select.select_option("")
        expect(pw_page.locator("tbody tr", has_text=d3["name"])).to_have_count(1)
        assert_view([d1, d2, d3], {"search": token})

        # Template filter alone; the option label carries vendor and model.
        with pw_page.expect_response(
            lambda r: _is_list_request(r, {"search": token, "template_id": tmpl_a["id"]})
        ):
            template_select.select_option(label=tmpl_a_label)
        expect(pw_page.locator("tbody tr", has_text=d3["name"])).to_have_count(0)
        assert_view([d1, d2], {"search": token, "template_id": tmpl_a["id"]})

        # Both filters compose with the search.
        with pw_page.expect_response(
            lambda r: _is_list_request(
                r, {"search": token, "template_id": tmpl_a["id"], "status": "OFFLINE"}
            )
        ):
            status_select.select_option("OFFLINE")
        expect(pw_page.locator("tbody tr", has_text=d2["name"])).to_have_count(1)
        assert_view([d2], {"search": token, "template_id": tmpl_a["id"], "status": "OFFLINE"})

        # A combination that matches nothing shows the filtered-empty state.
        with pw_page.expect_response(
            lambda r: _is_list_request(
                r, {"search": token, "template_id": tmpl_b["id"], "status": "OFFLINE"}
            )
        ):
            template_select.select_option(label=tmpl_b_label)
        empty = pw_page.get_by_text("No devices match the current filters")
        expect(empty).to_be_visible()
        expect(pw_page.get_by_text("No devices found", exact=True)).to_have_count(0)
        assert_view([], {"search": token, "template_id": tmpl_b["id"], "status": "OFFLINE"})

        # The Clear control inside the empty state resets every control.
        # The unfiltered list was fetched on first load, so it may come from cache.
        pw_page.locator("tbody").get_by_role("button", name="Clear filters").click()
        expect(search).to_have_value("")
        expect(status_select).to_have_value("")
        expect(template_select).to_have_value("")
        expect(pw_page.get_by_role("button", name="Clear filters")).to_have_count(0)

        # Final choice: persist a filter, wait for the PATCH carrying it, then
        # prove the saved object reads back and survives a reload.
        final_pref = {"search": "", "status": "AVAILABLE"}
        with pw_page.expect_response(lambda r: pw_prefs_patch_carries(r, "inventory", final_pref)):
            status_select.select_option("AVAILABLE")
        assert _read_inventory_pref(pw_page) == final_pref

        pw_page.reload()
        expect(pw_page.get_by_label("Status", exact=True)).to_have_value("AVAILABLE")
        expect(pw_page.get_by_label("Template", exact=True)).to_have_value("")
    finally:
        # Dependency order: devices, then templates, then the driver.
        for device_id in device_ids:
            log_cleanup_failure(
                "device",
                device_id,
                pw_api(pw_page, "DELETE", f"/inventory/devices/{device_id}", allow_errors=True),
            )
        for template_id in template_ids:
            log_cleanup_failure(
                "template",
                template_id,
                pw_api(pw_page, "DELETE", f"/inventory/templates/{template_id}", allow_errors=True),
            )
        if driver_id is not None:
            log_cleanup_failure(
                "driver",
                driver_id,
                pw_api(pw_page, "DELETE", f"/inventory/drivers/{driver_id}", allow_errors=True),
            )
        if baseline_read:
            pw_api(
                pw_page,
                "PATCH",
                "/user-profile/preferences",
                json={"saved_filters": {"inventory": baseline}},
                allow_errors=True,
            )
            restored = _read_inventory_pref(pw_page)
            if restored != baseline:
                pytest.fail(f"inventory preference not restored: {restored!r} != {baseline!r}")
