"""Playwright e2e tests for the published manual's search (issue #1298).

The manual (`docs/manual/`) is static HTML, so these tests need no HERD stack and no
login: a module fixture serves `docs/manual/` from a ThreadingHTTPServer it starts itself
on a free port bound to 127.0.0.1, and one test opens the pages from `file://`, where the
index must still load (manual.js adds it as a script element, not a fetch). They use the
shared `pw_page`/`pw_contexts` fixtures from conftest.py for the browser and the failure
artifacts. The conftest's session-scoped autouse `_ensure_config_seeded` (which GETs
`/api/config/status` at `E2E_HOST_BASE_URL` and may PUT settings) is overridden in this
module by a fixture of the same name that does nothing, so a run of this file alone
sends nothing to any stack. In a run that also collects other e2e modules, the
conftest's fixture still runs once for them, as it always has.

What they prove in a real Chromium: every page shows the search box and loads no index
until the box is used; typing a query lists results and a click lands on the right page
and anchor, below the sticky header; a query typed before the index arrives is answered
when it does, and a failed load says so; the keyboard-only path (`/`, arrows, Enter,
Escape); the visible no-results state; release pages labeled and ranked after current
pages, in the same order the pure function returns; markup-looking text in the index
renders inert (served through a fixture index with `page.route`); the header at every
width from 721 to 1100 px on three page kinds (no nav link outside the bar, no
horizontal scroll); and the phone breakpoint, where the search sits beside the nav
toggle.

The pure matching and ranking function is tested directly in
`frontend/src/test/manual/manualSearch.test.ts`; the index generator and its drift guard
in `tests/unit/test_manual_search_index.py`. Nothing here writes to a file or a service:
the only server is the module's own, and it only reads `docs/manual/`.
"""

import functools
import http.server
import json
import re
import threading
from pathlib import Path

import pytest
from playwright.sync_api import expect

MANUAL_DIR = Path(__file__).resolve().parents[2] / "docs" / "manual"
PAGES = sorted(p.name for p in MANUAL_DIR.glob("*.html"))
INDEX_ROUTE = "**/assets/search-index.js"


@pytest.fixture(scope="session", autouse=True)
def _ensure_config_seeded():
    """Shadow tests/e2e/conftest.py's fixture of this name for this module only.

    That fixture seeds a running stack's config service; these tests use no stack, so
    for them it does nothing. pytest resolves a fixture name from the requesting test's
    module first, so the conftest's version never runs for a test in this file.
    """
    yield


class _QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, format, *args):  # noqa: A002 - the base class's signature
        pass


@pytest.fixture(scope="module")
def manual_url():
    handler = functools.partial(_QuietHandler, directory=str(MANUAL_DIR))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _errors(page) -> list[str]:
    errors: list[str] = []
    page.on("pageerror", lambda exc: errors.append(str(exc)))
    return errors


def _search(page):
    return page.locator("#msearch-input")


def _options(page):
    return page.locator("#msearch-results [role='option']")


def test_every_page_shows_the_search_box_and_loads_the_index_only_on_use(pw_page, manual_url):
    assert len(PAGES) >= 20
    errors = _errors(pw_page)
    requested: list[str] = []
    pw_page.on("request", lambda r: requested.append(r.url))
    for name in PAGES:
        requested.clear()
        pw_page.goto(f"{manual_url}/{name}")
        expect(pw_page.locator("header.manualbar #msearch-input")).to_be_visible()
        assert pw_page.evaluate("typeof window.HERD_MANUAL_SEARCH_INDEX") == "undefined", name
        assert not [u for u in requested if u.endswith("/assets/search-index.js")], name
        _search(pw_page).focus()
        pw_page.wait_for_function("() => window.HERD_MANUAL_SEARCH_INDEX !== undefined")
        sections = pw_page.evaluate("window.HERD_MANUAL_SEARCH_INDEX.sections.length")
        assert sections > len(PAGES), name
        assert [u for u in requested if u.endswith("/assets/search-index.js")] == [
            f"{manual_url}/assets/search-index.js"
        ], name
    assert errors == []


def test_a_query_typed_before_the_index_arrives_is_answered_when_it_does(pw_page, manual_url):
    held = []
    pw_page.route(INDEX_ROUTE, lambda route: held.append(route))
    pw_page.goto(f"{manual_url}/user-ai.html")
    _search(pw_page).fill("dry run")
    expect(pw_page.locator(".msearch-count")).to_have_text("Loading the search index\u2026")
    expect(pw_page.locator("#msearch-status")).to_have_text("Loading the search index\u2026")
    expect(_options(pw_page)).to_have_count(0)
    for _ in range(100):
        if held:
            break
        pw_page.wait_for_timeout(50)
    assert len(held) == 1
    held[0].continue_()
    expect(_options(pw_page).first).to_have_attribute("href", "glossary.html#dry-run")
    expect(pw_page.locator(".msearch-count")).not_to_have_text("Loading the search index\u2026")


def test_a_failed_index_load_says_so(pw_page, manual_url):
    pw_page.route(INDEX_ROUTE, lambda route: route.abort())
    pw_page.goto(f"{manual_url}/index.html")
    _search(pw_page).fill("fork")
    expect(pw_page.locator(".msearch-count")).to_have_text("The search index did not load.")
    expect(pw_page.locator("#msearch-status")).to_have_text("The search index did not load.")


def test_query_lists_results_and_a_click_lands_on_the_anchor(pw_page, manual_url):
    pw_page.goto(f"{manual_url}/user-ai.html")
    _search(pw_page).fill("dry run")
    first = _options(pw_page).first
    expect(first).to_be_visible()
    expect(first.locator(".msearch-page")).to_have_text("Glossary")
    expect(first.locator(".msearch-heading")).to_have_text("Dry run")
    expect(first.locator(".msearch-heading mark")).to_have_text(["Dry", "run"])
    assert first.get_attribute("href") == "glossary.html#dry-run"
    first.click()
    expect(pw_page).to_have_url(re.compile(r"/glossary\.html#dry-run$"))
    target = pw_page.locator("#dry-run")
    expect(target).to_be_in_viewport()
    # Below the sticky 56 px header, not hidden under it.
    pw_page.wait_for_function(
        "() => { const r = document.getElementById('dry-run').getBoundingClientRect();"
        " return r.top >= 56 && r.top < window.innerHeight; }"
    )


def test_keyboard_only_path(pw_page, manual_url):
    pw_page.goto(f"{manual_url}/index.html")
    pw_page.locator("body").press("/")
    assert pw_page.evaluate("document.activeElement.id") == "msearch-input"
    search = _search(pw_page)
    focus_ring = search.evaluate("el => getComputedStyle(el).boxShadow")
    assert focus_ring != "none"
    pw_page.keyboard.type("device group")
    expect(search).to_have_value("device group")
    expect(search).to_have_attribute("aria-expanded", "true")
    expect(_options(pw_page).first).to_be_visible()

    pw_page.keyboard.press("ArrowDown")
    expect(search).to_have_attribute("aria-activedescendant", "msearch-opt-0")
    pw_page.keyboard.press("ArrowDown")
    expect(search).to_have_attribute("aria-activedescendant", "msearch-opt-1")
    expect(pw_page.locator("#msearch-opt-1")).to_have_attribute("aria-selected", "true")
    pw_page.keyboard.press("ArrowUp")
    expect(search).to_have_attribute("aria-activedescendant", "msearch-opt-0")

    pw_page.keyboard.press("Escape")
    expect(search).to_have_attribute("aria-expanded", "false")
    expect(pw_page.locator(".msearch-panel")).to_be_hidden()
    expect(search).to_have_value("device group")
    pw_page.keyboard.press("Escape")
    expect(search).to_have_value("")

    # "/" typed inside the search box is text, not the shortcut.
    pw_page.keyboard.type("eth1/1")
    expect(search).to_have_value("eth1/1")

    search.fill("")
    pw_page.keyboard.type("device group")
    pw_page.keyboard.press("ArrowDown")
    pw_page.keyboard.press("ArrowDown")
    href = pw_page.locator("#msearch-opt-1").get_attribute("href")
    assert href and "#" in href
    pw_page.keyboard.press("Enter")
    expect(pw_page).to_have_url(re.compile(re.escape(href) + "$"))
    anchor = href.split("#", 1)[1]
    expect(pw_page.locator(f"[id='{anchor}']")).to_be_in_viewport()


def test_no_results_state_is_visible(pw_page, manual_url):
    pw_page.goto(f"{manual_url}/glossary.html")
    _search(pw_page).fill("zzqqxxnothing")
    shown = pw_page.locator(".msearch-count")
    expect(shown).to_be_visible()
    expect(shown).to_have_text("No results for \u201czzqqxxnothing\u201d.")
    expect(pw_page.locator("#msearch-status")).to_have_text(
        "No results for \u201czzqqxxnothing\u201d."
    )
    expect(_options(pw_page)).to_have_count(0)


def test_release_pages_are_labeled_and_earlier_releases_rank_last(pw_page, manual_url):
    pw_page.goto(f"{manual_url}/index.html")
    query = "webhook"
    _search(pw_page).fill(query)
    expect(_options(pw_page).first).to_be_visible()
    expected = pw_page.evaluate(
        "q => HERDManualSearch.search(window.HERD_MANUAL_SEARCH_INDEX, q, 20).results"
        ".map(r => [r.href, r.kind])",
        query,
    )
    kinds = [kind for _, kind in expected]
    assert "earlier-release" in kinds and "page" in kinds, (
        f"pick another query: {query!r} no longer hits both kinds ({kinds})"
    )
    options = _options(pw_page)
    expect(options).to_have_count(len(expected))
    seen_earlier = False
    for i, (href, kind) in enumerate(expected):
        option = options.nth(i)
        assert option.get_attribute("href") == href
        tag = option.locator(".msearch-tag")
        if kind in ("release", "earlier-release"):
            expect(tag).to_have_text("Release notes")
        else:
            expect(tag).to_have_count(0)
        if kind == "earlier-release":
            seen_earlier = True
        else:
            assert not seen_earlier, f"{href} ({kind}) ranks below an earlier release page"


def _fixture_index() -> str:
    evil = "<img src=x onerror=\"window.__pwned='{}'\">"
    index = {
        "format": 1,
        "pages": [{"file": "glossary.html", "kind": "page", "title": "Fixture " + evil.format(1)}],
        "sections": [
            {
                "page": 0,
                "anchor": "fork",
                "heading": evil.format(2) + " onerror heading",
                "eyebrow": "",
                "text": "Body "
                + evil.format(3)
                + " and <script>window.__pwned='4'</script> onerror text.",
            }
        ],
    }
    return f"window.HERD_MANUAL_SEARCH_INDEX = {json.dumps(index)};\n"


def test_markup_in_index_text_renders_inert(pw_page, manual_url):
    errors = _errors(pw_page)
    pw_page.route(
        INDEX_ROUTE,
        lambda route: route.fulfill(body=_fixture_index(), content_type="text/javascript"),
    )
    pw_page.goto(f"{manual_url}/index.html")
    _search(pw_page).fill("onerror")
    option = _options(pw_page).first
    expect(option).to_be_visible()
    panel = pw_page.locator(".msearch-panel")
    expect(panel.locator("img, script")).to_have_count(0)
    expect(option.locator(".msearch-page")).to_have_text(
        "Fixture <img src=x onerror=\"window.__pwned='1'\">"
    )
    expect(option.locator(".msearch-snippet")).to_contain_text(
        "<img src=x onerror=\"window.__pwned='3'\">"
    )
    expect(option.locator("mark")).to_have_count(4)
    pw_page.wait_for_timeout(300)
    assert pw_page.evaluate("window.__pwned") is None
    assert errors == []


def test_search_works_from_file_urls(pw_page):
    pw_page.goto((MANUAL_DIR / "user-topology.html").as_uri())
    _search(pw_page).fill("fork")
    first = _options(pw_page).first
    expect(first).to_have_attribute("href", "glossary.html#fork")
    first.click()
    expect(pw_page).to_have_url(re.compile(r"^file://.*/glossary\.html#fork$"))
    expect(pw_page.locator("#fork")).to_be_in_viewport()


def test_mobile_breakpoint_beside_the_nav_toggle(pw_contexts, manual_url):
    context = pw_contexts.new(viewport={"width": 375, "height": 740})
    page = context.new_page()
    page.goto(f"{manual_url}/user-ai.html")
    toggle = page.locator(".navtoggle")
    nav = page.locator(".manualbar nav")
    search = _search(page)
    expect(toggle).to_be_visible()
    expect(search).to_be_visible()
    expect(nav).to_be_hidden()
    toggle.click()
    expect(nav).to_be_visible()
    search.click()
    expect(nav).to_be_hidden()
    page.keyboard.type("fork")
    panel = page.locator(".msearch-panel")
    expect(panel).to_be_visible()
    box = panel.bounding_box()
    assert box is not None
    assert box["x"] >= 0 and box["x"] + box["width"] <= 375
    no_hscroll = "document.documentElement.scrollWidth <= window.innerWidth"
    assert page.evaluate(no_hscroll)
    _options(page).first.click()
    expect(page).to_have_url(re.compile(r"/glossary\.html#fork$"))


SWEEP_WIDTHS = [*range(721, 1101, 20), 980, 981, 1100]
HEADER_FIT = """() => {
  const bar = document.querySelector('.manualbar').getBoundingClientRect();
  const out = [];
  for (const el of document.querySelectorAll('.manualbar nav a, .manualbar .msearch')) {
    const r = el.getBoundingClientRect();
    if (!r.width && !r.height) continue;
    if (r.top < bar.top - 0.5 || r.bottom > bar.bottom + 0.5 || r.left < -0.5
        || r.right > window.innerWidth + 0.5) {
      out.push(`${el.textContent.trim() || el.className} at ${Math.round(r.left)},`
        + `${Math.round(r.top)} to ${Math.round(r.right)},${Math.round(r.bottom)}`);
    }
  }
  return {outside: out, hscroll: document.documentElement.scrollWidth > window.innerWidth,
          navShown: getComputedStyle(document.querySelector('.manualbar nav')).display !== 'none'};
}"""


@pytest.mark.parametrize("name", ["index.html", "user-ai.html", "release-0-7-0.html"])
def test_header_fits_its_bar_at_every_width(pw_page, manual_url, name):
    pw_page.goto(f"{manual_url}/{name}")
    problems = []
    for width in SWEEP_WIDTHS:
        pw_page.set_viewport_size({"width": width, "height": 800})
        fit = pw_page.evaluate(HEADER_FIT)
        if fit["outside"]:
            problems.append(f"{width}px: outside the header bar: {fit['outside']}")
        if fit["hscroll"]:
            problems.append(f"{width}px: the page scrolls horizontally")
        # The nav collapses behind the toggle at 980 px and below.
        if fit["navShown"] != (width > 980):
            problems.append(f"{width}px: nav shown is {fit['navShown']}")
    assert problems == [], "\n".join(problems)
