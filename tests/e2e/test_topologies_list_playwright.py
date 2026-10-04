"""Playwright e2e tests for the Topologies page list controls (issue #958).

TopologyPage.tsx gained a search box and an Owner filter in the shared left
filter panel, sortable Name, Owner, Created, and Updated headings, and a
checkbox per row with a bulk Delete. GET /api/cabling/topologies gained
`search`, `owner`, `sort_by`, and `sort_dir`. Every assertion that matters is
read back through the API after the UI action, not taken from the UI's own
acknowledgment.

Each test seeds its own topologies through the API with a per-run name prefix
and deletes whatever is left in a fixture finally, so a reused stack is fine:
the search term is the prefix, so foreign rows never enter an assertion.

The page PERSISTS the search and Owner filter (savedFilters.topologies) and
the sort (extras["sort:topologies"]) per user. The preferences fixture
resets both to the defaults before the test, the tests wait for the debounced
preference PATCH carrying their final choice, and the fixture restores the
baseline afterward, even on failure (the issue #951 rule).
"""

import uuid

import httpx
import pytest
from playwright.sync_api import expect

from .conftest import HOST_BASE_URL, log_cleanup_failure, pw_api, pw_login

WAIT_MS = 15000
FILTER_KEY = "topologies"
SORT_KEY = "sort:topologies"
USER1 = {"email": "user1@herd.dev", "password": "user1user1xx"}


def _park_pointer(page) -> None:
    # react-hot-toast pauses a toast's timer while the pointer is over the
    # toaster; keep the pointer well away from the top-right stack.
    page.mouse.move(5, 400)


def _prefs_api(page_or_token, method: str, **kwargs):
    """Preferences call as the browser's user, or as a raw bearer token."""
    if isinstance(page_or_token, str):
        with httpx.Client(verify=False, timeout=30.0) as client:
            return client.request(
                method,
                f"{HOST_BASE_URL}/api/user-profile/preferences",
                headers={"Authorization": f"Bearer {page_or_token}"},
                **kwargs,
            )
    return pw_api(page_or_token, method, "/user-profile/preferences", allow_errors=True, **kwargs)


class _SavedPrefs:
    """Snapshot one user's topology list preferences, reset, and restore them."""

    def __init__(self, who):
        self.who = who
        prefs = _prefs_api(who, "GET").json()
        self.filters = (prefs.get("saved_filters") or {}).get(FILTER_KEY)
        self.sort = (prefs.get("extras") or {}).get(SORT_KEY)

    def _write(self, filters, sort) -> None:
        resp = _prefs_api(
            self.who,
            "PATCH",
            json={"saved_filters": {FILTER_KEY: filters}, "extras": {SORT_KEY: sort}},
        )
        log_cleanup_failure("topology list preferences", FILTER_KEY, resp)

    def reset(self) -> None:
        self._write(None, None)

    def restore(self) -> None:
        self._write(self.filters, self.sort)
        after = _prefs_api(self.who, "GET")
        if after.status_code == 200:
            body = after.json()
            assert (body.get("saved_filters") or {}).get(FILTER_KEY) == self.filters
            assert (body.get("extras") or {}).get(SORT_KEY) == self.sort


@pytest.fixture
def seeded_topologies(pw_page):
    """Three admin-owned topologies under one per-run prefix, deleted at teardown.

    Names differ in case on purpose ("Alpha" between "bravo" and "charlie"
    only under a case-insensitive sort). Yields (prefix, {suffix: topology}).
    """
    pw_login(pw_page)
    prefix = f"pw958-{uuid.uuid4().hex[:8]}"
    created: dict[str, dict] = {}
    try:
        for suffix in ("charlie", "Alpha", "bravo"):
            resp = pw_api(
                pw_page, "POST", "/cabling/topologies", json={"name": f"{prefix}-{suffix}"}
            )
            created[suffix] = resp.json()
        yield prefix, created
    finally:
        for topo in created.values():
            resp = pw_api(pw_page, "DELETE", f"/cabling/topologies/{topo['id']}", allow_errors=True)
            if resp.status_code != 404:
                log_cleanup_failure("topology", topo["id"], resp)


@pytest.fixture
def admin_prefs(pw_page, seeded_topologies):
    saved = _SavedPrefs(pw_page)
    saved.reset()
    try:
        yield saved
    finally:
        saved.restore()


def _is_prefs_patch(predicate):
    def check(response) -> bool:
        request = response.request
        if request.method != "PATCH" or "/user-profile/preferences" not in response.url:
            return False
        return predicate(request.post_data_json or {})

    return check


def _search_saved(term: str):
    return _is_prefs_patch(
        lambda body: ((body.get("saved_filters") or {}).get(FILTER_KEY) or {}).get("search") == term
    )


def _sort_saved(field: str, direction: str):
    return _is_prefs_patch(
        lambda body: (
            (body.get("extras") or {}).get(SORT_KEY) == {"sortBy": field, "sortDir": direction}
        )
    )


def _open_page(page) -> None:
    page.goto(f"{HOST_BASE_URL}/topology")
    expect(page.get_by_role("button", name="New Topology", exact=True)).to_be_visible(
        timeout=WAIT_MS
    )


def _search(page, term: str) -> None:
    """Type a search and wait for the debounced preference PATCH that saves it."""
    with page.expect_response(_search_saved(term), timeout=WAIT_MS):
        page.get_by_label("Search topologies", exact=True).fill(term)


def _rendered_names(page, prefix: str) -> list[str]:
    cells = page.locator("table tbody tr td:nth-child(2)")
    names = [t.split("\n")[0].strip() for t in cells.all_inner_texts()]
    return [n for n in names if n.startswith(prefix)]


def _api_names(page, **params) -> tuple[list[str], int]:
    body = pw_api(page, "GET", "/cabling/topologies", params={"limit": 500, **params}).json()
    return [t["name"] for t in body["items"]], body["total"]


def test_search_narrows_to_the_api_answer_and_name_sort_matches_api_order(
    pw_page, seeded_topologies, admin_prefs
):
    prefix, _created = seeded_topologies
    _open_page(pw_page)

    _search(pw_page, prefix)
    api_names, api_total = _api_names(pw_page, search=prefix)
    assert api_total == 3
    # The rendered list is exactly the API's answer for the same search: same
    # rows, same (default, updated_at desc) order, and nothing else.
    expect(pw_page.locator("table tbody tr")).to_have_count(3, timeout=WAIT_MS)
    assert _rendered_names(pw_page, prefix) == api_names
    expect(pw_page.get_by_role("heading", name="Topologies(3)", exact=True)).to_be_visible()

    # Sort by Name ascending through the heading.
    with pw_page.expect_response(_sort_saved("name", "asc"), timeout=WAIT_MS):
        pw_page.get_by_role("button", name="Name", exact=True).click()
    expect(pw_page.get_by_role("columnheader", name="Name")).to_have_attribute(
        "aria-sort", "ascending", timeout=WAIT_MS
    )
    api_sorted, _ = _api_names(pw_page, search=prefix, sort_by="name", sort_dir="asc")
    assert api_sorted == [f"{prefix}-Alpha", f"{prefix}-bravo", f"{prefix}-charlie"]
    expect(pw_page.locator("table tbody tr").first).to_contain_text(
        f"{prefix}-Alpha", timeout=WAIT_MS
    )
    assert _rendered_names(pw_page, prefix) == api_sorted

    # Both choices really were saved, which is why the fixture restores them.
    saved = _prefs_api(pw_page, "GET").json()
    assert saved["saved_filters"][FILTER_KEY] == {"search": prefix}
    assert saved["extras"][SORT_KEY] == {"sortBy": "name", "sortDir": "asc"}


def test_bulk_delete_removes_exactly_the_selected_topologies(
    pw_page, seeded_topologies, admin_prefs
):
    prefix, created = seeded_topologies
    _open_page(pw_page)
    _search(pw_page, prefix)
    expect(pw_page.locator("table tbody tr")).to_have_count(3, timeout=WAIT_MS)

    for suffix in ("Alpha", "charlie"):
        pw_page.get_by_role(
            "checkbox", name=f"Select topology {prefix}-{suffix}", exact=True
        ).check()
    expect(pw_page.get_by_role("status", name="Selection")).to_contain_text("2 selected")

    pw_page.get_by_role("button", name="Delete selected", exact=True).click()
    dialog = pw_page.locator("dialog[open]")
    expect(dialog).to_contain_text("Delete 2 topologies?")
    expect(dialog).to_contain_text("cannot be undone")
    expect(dialog).not_to_contain_text("skipped")
    dialog.get_by_role("button", name="Delete 2 topologies", exact=True).click()
    _park_pointer(pw_page)
    expect(pw_page.locator("table tbody tr")).to_have_count(1, timeout=WAIT_MS)

    # Effect: exactly the two selected rows are gone, the third is untouched.
    for suffix, expected in (("Alpha", 404), ("charlie", 404), ("bravo", 200)):
        resp = pw_api(
            pw_page, "GET", f"/cabling/topologies/{created[suffix]['id']}", allow_errors=True
        )
        assert resp.status_code == expected, (suffix, resp.status_code)
    names, total = _api_names(pw_page, search=prefix)
    assert names == [f"{prefix}-bravo"]
    assert total == 1
    assert _rendered_names(pw_page, prefix) == names


def test_non_owner_non_admin_gets_no_delete_action_on_someone_elses_topology(
    pw_contexts, pw_page, seeded_topologies
):
    """The seeded non-admin user1 sees the admin's topology but cannot delete it.

    user1 comes from `make seed`; the unseeded first e2e pass has no such user
    and skips, the seeded pass must not. Only user1's preferences are touched,
    reset and restored through the API.
    """
    prefix, created = seeded_topologies
    login = httpx.post(f"{HOST_BASE_URL}/api/auth/login", json=USER1, verify=False, timeout=30.0)
    if login.status_code != 200:
        pytest.skip(f"seeded user1 cannot log in: {login.status_code}")
    token = login.json()["access_token"]
    target = created["bravo"]

    user_prefs = _SavedPrefs(token)
    user_prefs.reset()
    try:
        page = pw_contexts.new(ignore_https_errors=True).new_page()
        pw_login(page, USER1["email"], USER1["password"])
        me = pw_api(page, "GET", "/auth/me").json()
        assert me["role"] == "user"
        assert me["id"] != target["created_by"]
        _open_page(page)

        name = f"{prefix}-bravo"
        box = page.get_by_role("checkbox", name=f"Select topology {name}", exact=True)
        expect(box).to_be_visible(timeout=WAIT_MS)
        expect(
            page.get_by_role("button", name=f"Delete topology {name}", exact=True)
        ).to_have_count(0)

        box.check()
        delete_selected = page.get_by_role("button", name="Delete selected", exact=True)
        expect(delete_selected).to_be_disabled()
        expect(page.get_by_role("status", name="Selection")).to_contain_text(
            "None of the selected topologies can be deleted: 1 not yours."
        )

        # The backend agrees: user1's DELETE is refused and the row survives.
        refused = pw_api(page, "DELETE", f"/cabling/topologies/{target['id']}", allow_errors=True)
        assert refused.status_code == 403
        still = pw_api(pw_page, "GET", f"/cabling/topologies/{target['id']}", allow_errors=True)
        assert still.status_code == 200
    finally:
        user_prefs.restore()
