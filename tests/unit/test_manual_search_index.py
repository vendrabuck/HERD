"""Guard: the manual's search index is current, and every manual page can search (issue #1298).

The published manual (`docs/manual/`, hand-written HTML) searches in the browser from a
prebuilt index: `scripts/build_manual_search_index.py` writes
`docs/manual/assets/search-index.js`, which assigns the global
`window.HERD_MANUAL_SEARCH_INDEX`. No page loads that file itself: every page loads
`docs/manual/assets/manual.js`, which injects the search box into the page's header and
adds a script element for the index the first time the box is focused or typed in. The
generator's module docstring defines what a record is (section boundaries, anchors,
excluded chrome, text normalization).

What it requires (each check is a function returning a list of problem strings, run
against the real tree and against fixture trees):

1. Drift. The committed index's bytes equal the UTF-8 bytes the generator produces from
   the committed pages (so a CRLF or re-encoded copy fails too). The failure names the
   command that regenerates it.
2. Wiring. Every `*.html` file directly in `docs/manual/` has a
   `<header class="manualbar">` (the element manual.js puts the search box into), loads
   `assets/manual.js` through exactly one `<script src>`, and has no `<script src>` for
   `assets/search-index.js` (manual.js loads it on demand).
3. Anchors. Every page the index names exists; every section anchor in the index is the
   `id` of an element on that page (the intro record's empty anchor links the page
   itself); every page in the directory is in the index; and on every page, the target
   of every `<a href="#...">` inside an `<aside class="side">` (the "On this page"
   sidebar) is one of that page's index anchors, so a result never lands above or below
   the place the page's own sidebar would take the reader.
4. Unique ids. No `id` value occurs twice on a manual page (a duplicate makes the
   browser land on the first one, which a result link cannot control).
5. Size. The index stays under the generator's `SIZE_BUDGET_BYTES`.

Plus a cross-check of the generator against an independent scan
(`test_real_index_covers_every_section_boundary`, which classifies boundaries with its own
parser and names any boundary element the index lacks), the generator's own behavior
(section splitting, anchors and the error for a section without one, entity and
whitespace normalization, chrome exclusion, word breaks, page names, encodings, release
kinds, determinism, the CLI), and a scan of manual.js for markup-parsing sinks.

Limits:

- Check 2 reads `<script src>` values literally (`assets/manual.js`,
  `assets/search-index.js`, with no query string or `./`). It does not prove the browser
  ran manual.js or that manual.js fetched the index; the Playwright test
  `tests/e2e/test_manual_search_playwright.py` does that.
- Check 3 accepts any element's `id`, including one inside an excluded region or a
  `<template>`; it proves the fragment resolves in the markup, not that it is visible.
  It reads sidebar links only inside `<aside>` elements whose class includes `side`.
- The sink scan is a substring search of manual.js for `innerHTML`, `outerHTML`,
  `insertAdjacentHTML`, and `document.write`; it does not see a sink reached under
  another spelling (`el["inner" + "HTML"]`).
- Only files directly in `docs/manual/` are pages, as in the generator and in
  `tests/unit/test_manual_release_pages.py`.
"""

import importlib.util
import json
import shutil
import sys
from collections import Counter
from html.parser import HTMLParser
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
MANUAL_DIR = REPO_ROOT / "docs" / "manual"
SCRIPT_PATH = REPO_ROOT / "scripts" / "build_manual_search_index.py"
INDEX_SRC = "assets/search-index.js"
MANUAL_JS_SRC = "assets/manual.js"
MARKUP_SINKS = ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write")


def _load_generator():
    spec = importlib.util.spec_from_file_location("build_manual_search_index", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["build_manual_search_index"] = module
    spec.loader.exec_module(module)
    return module


gen = _load_generator()

_VOID = frozenset("area base br col embed hr img input link meta param source track wbr".split())


class _MarkupScan(HTMLParser):
    """Ids (with counts), script srcs, the manualbar header, and sidebar hash targets."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.ids: Counter[str] = Counter()
        self.scripts: list[str] = []
        self.manualbar = False
        self.side_targets: list[str] = []
        self._side_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        classes = (values.get("class") or "").split()
        if values.get("id"):
            self.ids[values["id"] or ""] += 1
        if tag == "script" and values.get("src") is not None:
            self.scripts.append(values["src"] or "")
        if tag == "header" and "manualbar" in classes:
            self.manualbar = True
        if tag == "aside":
            self._side_depth += 1 if "side" in classes or self._side_depth else 0
        href = values.get("href") or ""
        if tag == "a" and self._side_depth and href.startswith("#") and len(href) > 1:
            self.side_targets.append(href[1:])

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if values.get("id"):
            self.ids[values["id"] or ""] += 1

    def handle_endtag(self, tag: str) -> None:
        if tag == "aside" and self._side_depth:
            self._side_depth -= 1


def _scan(path: Path) -> _MarkupScan:
    scan = _MarkupScan()
    scan.feed(path.read_text(encoding="utf-8-sig"))
    scan.close()
    return scan


class _BoundaryScan(HTMLParser):
    """Section boundaries, classified the way the generator's docstring defines them.

    Written separately from the generator's parser on purpose: an `<h2>` whose class
    includes `sec`; a `<p>` whose class includes `sectitle` that carries an id and is not
    the first start tag after a section `<h2>` closes (with no text between); a `<div>`
    directly inside a `<dl>` whose class includes `gloss`. Excluded regions are skipped.
    Each boundary is recorded as (line, description, id).
    """

    _EXCLUDED_TAGS = frozenset("script style template noscript header nav aside".split())
    _EXCLUDED_CLASSES = frozenset({"foot", "pagenav", "shot-stage"})

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.boundaries: list[tuple[int, str, str]] = []
        self._stack: list[tuple[str, list[str], bool]] = []
        self._after_h2 = False

    def _excluded(self) -> bool:
        return any(excluded for _, _, excluded in self._stack)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        classes = (values.get("class") or "").split()
        excluded = (
            tag in self._EXCLUDED_TAGS
            or bool(self._EXCLUDED_CLASSES.intersection(classes))
            or values.get("id") == "totop"
        )
        after_h2, self._after_h2 = self._after_h2, False
        if not excluded and not self._excluded():
            element = f'<{tag} class="{" ".join(classes)}" id="{values.get("id") or ""}">'
            line = self.getpos()[0]
            parent = self._stack[-1] if self._stack else None
            if tag == "h2" and "sec" in classes:
                self.boundaries.append((line, element, values.get("id") or ""))
            elif tag == "p" and "sectitle" in classes and values.get("id") and not after_h2:
                self.boundaries.append((line, element, values.get("id") or ""))
            elif tag == "div" and parent and parent[0] == "dl" and "gloss" in parent[1]:
                self.boundaries.append((line, element, values.get("id") or ""))
        if tag not in _VOID:
            self._stack.append((tag, classes, excluded))

    def handle_endtag(self, tag: str) -> None:
        for i in range(len(self._stack) - 1, -1, -1):
            if self._stack[i][0] == tag:
                closed = self._stack[i]
                del self._stack[i:]
                if tag == "h2" and "sec" in closed[1] and not closed[2] and not self._excluded():
                    self._after_h2 = True
                return

    def handle_data(self, data: str) -> None:
        if data.strip():
            self._after_h2 = False


def _boundaries(path: Path) -> list[tuple[int, str, str]]:
    scan = _BoundaryScan()
    scan.feed(path.read_text(encoding="utf-8-sig"))
    scan.close()
    return scan.boundaries


def _pages(manual_dir: Path) -> list[Path]:
    return sorted(p for p in manual_dir.glob("*.html") if p.is_file())


def _read_index(index_path: Path) -> dict:
    text = index_path.read_text(encoding="utf-8")
    prefix = f"window.{gen.INDEX_GLOBAL} = "
    start = text.index(prefix) + len(prefix)
    return json.loads(text[start:].rstrip().rstrip(";"))


def _anchors_by_page(index: dict) -> dict[str, set[str]]:
    files = [page["file"] for page in index["pages"]]
    anchors: dict[str, set[str]] = {name: set() for name in files}
    for section in index["sections"]:
        anchors[files[section["page"]]].add(section["anchor"])
    return anchors


# Checks


def check_index_current(manual_dir: Path, index_path: Path) -> list[str]:
    try:
        fresh = gen.generate(manual_dir).encode("utf-8")
    except gen.ManualIndexError as exc:
        return [f"the generator refused the pages: {exc}"]
    if not index_path.is_file():
        return [f"{index_path.name} is missing; run `{gen.REGENERATE_COMMAND}` and commit it"]
    if index_path.read_bytes() != fresh:
        return [
            f"{index_path.name} is not what the generator produces from the pages in "
            f"{manual_dir.name}/ (a page changed without a regeneration, or the index was "
            f"edited by hand); run `{gen.REGENERATE_COMMAND}` and commit the result"
        ]
    return []


def check_page_wiring(manual_dir: Path) -> list[str]:
    problems = []
    for path in _pages(manual_dir):
        scan = _scan(path)
        if not scan.manualbar:
            problems.append(f'{path.name}: has no <header class="manualbar"> for the search box')
        count = scan.scripts.count(MANUAL_JS_SRC)
        if count != 1:
            problems.append(
                f'{path.name}: loads <script src="{MANUAL_JS_SRC}"> {count} times, not once'
            )
        if INDEX_SRC in scan.scripts:
            problems.append(
                f'{path.name}: loads <script src="{INDEX_SRC}"> itself; manual.js loads the '
                "index on the first use of the search box, so remove the tag"
            )
    return problems


def check_anchors(manual_dir: Path, index_path: Path) -> list[str]:
    index = _read_index(index_path)
    problems = []
    anchors = _anchors_by_page(index)
    for path in _pages(manual_dir):
        if path.name not in anchors:
            problems.append(f"{path.name}: is not in the index")
    for name in anchors:
        path = manual_dir / name
        if not path.is_file():
            problems.append(f"{name}: is in the index but is not a page")
            continue
        scan = _scan(path)
        for anchor in sorted(anchors[name]):
            if anchor and anchor not in scan.ids:
                problems.append(
                    f'{name}: the index links #{anchor}, but no element has id="{anchor}"'
                )
        for target in scan.side_targets:
            if target not in anchors[name]:
                problems.append(
                    f"{name}: its sidebar links #{target}, which is not a section anchor in the "
                    "index, so search would land elsewhere; make that element a section boundary"
                )
    return problems


def check_unique_ids(manual_dir: Path) -> list[str]:
    problems = []
    for path in _pages(manual_dir):
        for value, count in sorted(_scan(path).ids.items()):
            if count > 1:
                problems.append(f'{path.name}: id="{value}" occurs {count} times')
    return problems


def check_size(index_path: Path) -> list[str]:
    size = index_path.stat().st_size
    if size > gen.SIZE_BUDGET_BYTES:
        return [
            f"{index_path.name} is {size} bytes, over the {gen.SIZE_BUDGET_BYTES} budget; cap "
            "per-section text in the generator (and say so in its docstring)"
        ]
    return []


def check_no_markup_sinks(text: str) -> list[str]:
    return [f"manual.js uses {sink}" for sink in MARKUP_SINKS if sink in text]


# Real tree


def test_real_index_is_current():
    problems = check_index_current(MANUAL_DIR, gen.INDEX_PATH)
    assert problems == [], "\n".join(problems)


def test_real_pages_load_manual_js_and_not_the_index():
    problems = check_page_wiring(MANUAL_DIR)
    assert problems == [], "\n".join(problems)
    assert len(_pages(MANUAL_DIR)) >= 20


def test_real_index_anchors_exist_and_cover_every_sidebar_target():
    problems = check_anchors(MANUAL_DIR, gen.INDEX_PATH)
    assert problems == [], "\n".join(problems)


def test_real_pages_have_unique_ids():
    problems = check_unique_ids(MANUAL_DIR)
    assert problems == [], "\n".join(problems)


def test_real_index_is_under_its_size_budget():
    assert check_size(gen.INDEX_PATH) == []


def test_real_index_covers_every_section_boundary():
    anchors = _anchors_by_page(_read_index(gen.INDEX_PATH))
    problems = []
    for path in _pages(MANUAL_DIR):
        found = _boundaries(path)
        assert found or path.name == "index.html", f"{path.name}: no boundaries found"
        for line, element, anchor in found:
            if anchor not in anchors.get(path.name, set()):
                problems.append(f"{path.name}:{line}: {element} is a boundary the index lacks")
        indexed = anchors.get(path.name, set()) - {""}
        extra = indexed - {anchor for _, _, anchor in found}
        if extra:
            problems.append(
                f"{path.name}: the index has anchors no boundary carries: {sorted(extra)}"
            )
    assert problems == [], "\n".join(problems)


def test_manual_js_reads_the_global_and_uses_no_markup_sink():
    text = (MANUAL_DIR / MANUAL_JS_SRC).read_text(encoding="utf-8")
    assert f"window.{gen.INDEX_GLOBAL}" in text
    assert "search-index.js" in text
    assert check_no_markup_sinks(text) == []


@pytest.mark.parametrize("sink", MARKUP_SINKS)
def test_the_markup_sink_scan_refuses_each_sink(sink: str):
    assert check_no_markup_sinks(f"el.{sink}(x);") == [f"manual.js uses {sink}"]


# Fixture trees


def _page(
    title: str,
    main: str,
    *,
    header: bool = True,
    scripts=(MANUAL_JS_SRC,),
    side: tuple[str, ...] = (),
) -> str:
    head = (
        '<header class="manualbar"><a class="wm" href="index.html">HERD</a>'
        '<nav><a href="guide.html">Guide nav link</a></nav></header>\n'
        if header
        else ""
    )
    tags = "".join(f'<script src="{src}"></script>\n' for src in scripts)
    side_links = "".join(f'<a href="#{target}">Side {target}</a>' for target in side)
    return (
        f"<!doctype html>\n<html><head><title>HERD Manual: {title}</title>\n"
        "<style>.x{color:red}</style></head><body>\n"
        f"{head}"
        '<div class="shell"><aside class="side"><a href="guide.html">Sidebar entry</a>'
        f"{side_links}</aside>\n"
        f'<main class="main">\n{main}\n'
        '<div class="foot"><p>Footer words</p><div class="pagenav"><a href="x.html">Next</a>'
        "</div></div>\n</main></div>\n"
        '<button id="totop" aria-label="Back to top">Top</button>\n'
        f"{tags}</body></html>\n"
    )


GUIDE_MAIN = (
    '<h1 class="title">Guide</h1>\n'
    '<p class="dek">Intro words.</p>\n'
    '<h2 class="sec" id="first">Eyebrow one</h2>\n'
    '<p class="sectitle">Title one</p>\n'
    "<p>Body one.</p>\n"
    '<h2 class="sec" id="second">Plain heading</h2>\n'
    "<p>Body two.</p>\n"
)


def _write(manual: Path, name: str, html: str) -> Path:
    path = manual / name
    path.write_text(html, encoding="utf-8")
    return path


@pytest.fixture
def manual(tmp_path: Path) -> Path:
    root = tmp_path / "manual"
    (root / "assets").mkdir(parents=True)
    _write(root, "guide.html", _page("Guide", GUIDE_MAIN, side=("first", "second")))
    _write(
        root,
        "index.html",
        _page("Start here", '<section class="hero"><h1>Hub</h1><p>Hub words.</p></section>'),
    )
    _write(root, "release-0-1-0.html", _page("Release 0.1.0", "<p>Old notes.</p>"))
    _write(root, "release-0-2-0.html", _page("Release 0.2.0", "<p>New notes.</p>"))
    (root / "assets" / "search-index.js").write_bytes(gen.generate(root).encode("utf-8"))
    return root


def _index_path(manual: Path) -> Path:
    return manual / "assets" / "search-index.js"


def _records(manual: Path, name: str) -> list[dict]:
    index = gen.build_index(manual)
    number = [p["file"] for p in index["pages"]].index(name)
    return [
        {k: v for k, v in s.items() if k != "page"}
        for s in index["sections"]
        if s["page"] == number
    ]


def _edit(path: Path, old: str, new: str) -> None:
    text = path.read_text(encoding="utf-8")
    assert text.count(old) == 1, f"{old!r} occurs {text.count(old)} times in {path.name}"
    path.write_text(text.replace(old, new), encoding="utf-8")


def test_clean_fixture_passes_every_check(manual: Path):
    assert check_index_current(manual, _index_path(manual)) == []
    assert check_page_wiring(manual) == []
    assert check_anchors(manual, _index_path(manual)) == []
    assert check_size(_index_path(manual)) == []


# Check 1: drift, both directions


def test_drift_a_page_edit_without_regeneration_fails(manual: Path):
    _edit(manual / "guide.html", "<p>Body two.</p>", "<p>Body two, edited.</p>")
    assert check_index_current(manual, _index_path(manual)) == [
        "search-index.js is not what the generator produces from the pages in manual/ (a page "
        "changed without a regeneration, or the index was edited by hand); run "
        "`make manual-index` and commit the result"
    ]


def test_drift_a_hand_edited_index_fails(manual: Path):
    _edit(_index_path(manual), "Body two.", "Body 2.")
    assert len(check_index_current(manual, _index_path(manual))) == 1


def test_drift_a_new_page_without_regeneration_fails(manual: Path):
    _write(manual, "extra.html", _page("Extra", "<p>Extra words.</p>"))
    assert len(check_index_current(manual, _index_path(manual))) == 1
    assert check_anchors(manual, _index_path(manual)) == ["extra.html: is not in the index"]


def test_drift_compares_bytes_so_a_crlf_copy_fails(manual: Path):
    data = _index_path(manual).read_bytes()
    _index_path(manual).write_bytes(data.replace(b"\n", b"\r\n"))
    assert len(check_index_current(manual, _index_path(manual))) == 1


def test_drift_a_missing_index_fails(manual: Path):
    _index_path(manual).unlink()
    assert check_index_current(manual, _index_path(manual)) == [
        "search-index.js is missing; run `make manual-index` and commit it"
    ]


def test_drift_reports_a_page_the_generator_refuses(manual: Path):
    _edit(manual / "guide.html", 'id="second"', "")
    (problem,) = check_index_current(manual, _index_path(manual))
    assert problem.startswith("the generator refused the pages: guide.html:")


# Check 2: wiring


@pytest.mark.parametrize(
    ("scripts", "problem"),
    [
        ((), f'loads <script src="{MANUAL_JS_SRC}"> 0 times, not once'),
        ((MANUAL_JS_SRC, MANUAL_JS_SRC), f'loads <script src="{MANUAL_JS_SRC}"> 2 times'),
        ((INDEX_SRC, MANUAL_JS_SRC), f'loads <script src="{INDEX_SRC}"> itself'),
        ((MANUAL_JS_SRC, INDEX_SRC), f'loads <script src="{INDEX_SRC}"> itself'),
    ],
    ids=["no-manual-js", "manual-js-twice", "index-before", "index-after"],
)
def test_wiring_problems(manual: Path, scripts, problem: str):
    _write(manual, "guide.html", _page("Guide", GUIDE_MAIN, scripts=scripts, side=("first",)))
    problems = check_page_wiring(manual)
    assert len(problems) == 1 and problems[0].startswith(f"guide.html: {problem}"), problems


def test_wiring_requires_the_manualbar_header(manual: Path):
    _write(manual, "guide.html", _page("Guide", GUIDE_MAIN, header=False, side=("first",)))
    assert check_page_wiring(manual) == [
        'guide.html: has no <header class="manualbar"> for the search box'
    ]


# Check 3: anchors


def test_anchor_missing_from_its_page(manual: Path):
    _edit(_index_path(manual), '"anchor": "second"', '"anchor": "secnd"')
    assert check_anchors(manual, _index_path(manual)) == [
        'guide.html: the index links #secnd, but no element has id="secnd"',
        "guide.html: its sidebar links #second, which is not a section anchor in the index, "
        "so search would land elsewhere; make that element a section boundary",
    ]


def test_sidebar_target_that_is_not_a_section_boundary(manual: Path):
    # The target exists on the page (an h3 id), so a sidebar click works, but search
    # would fold its text into the section above and land there.
    _edit(manual / "guide.html", "<p>Body two.</p>", '<p>Body two.</p><h3 id="later">Later</h3>')
    _write(
        manual,
        "guide.html",
        (manual / "guide.html")
        .read_text(encoding="utf-8")
        .replace('<a href="#second">Side second</a>', '<a href="#later">Side later</a>'),
    )
    _index_path(manual).write_bytes(gen.generate(manual).encode("utf-8"))
    assert check_anchors(manual, _index_path(manual)) == [
        "guide.html: its sidebar links #later, which is not a section anchor in the index, "
        "so search would land elsewhere; make that element a section boundary"
    ]


def test_hash_links_outside_the_sidebar_are_not_sidebar_targets(manual: Path):
    _edit(
        manual / "guide.html", "<p>Body two.</p>", '<p>Body two, see <a href="#nowhere">x</a>.</p>'
    )
    _index_path(manual).write_bytes(gen.generate(manual).encode("utf-8"))
    assert check_anchors(manual, _index_path(manual)) == []


def test_index_naming_a_page_that_does_not_exist(manual: Path):
    (manual / "release-0-1-0.html").unlink()
    assert check_anchors(manual, _index_path(manual)) == [
        "release-0-1-0.html: is in the index but is not a page"
    ]


# Unique ids


def test_duplicate_ids_on_a_page(manual: Path):
    _edit(
        manual / "guide.html",
        "<p>Body one.</p>",
        '<p id="second">Body one.</p><br id="x"/><i id="x"></i>',
    )
    assert check_unique_ids(manual) == [
        'guide.html: id="second" occurs 2 times',
        'guide.html: id="x" occurs 2 times',
    ]


def test_unique_ids_on_the_clean_fixture(manual: Path):
    assert check_unique_ids(manual) == []


# Check 4: size


def test_size_over_budget(manual: Path, monkeypatch):
    monkeypatch.setattr(gen, "SIZE_BUDGET_BYTES", 10)
    (problem,) = check_size(_index_path(manual))
    assert "over the 10 budget" in problem


# Generator: sections and anchors


def test_sections_split_at_h2_sec_with_sectitle_and_eyebrow(manual: Path):
    assert _records(manual, "guide.html") == [
        {"anchor": "", "heading": "Guide", "eyebrow": "", "text": "Guide Intro words."},
        {"anchor": "first", "heading": "Title one", "eyebrow": "Eyebrow one", "text": "Body one."},
        {"anchor": "second", "heading": "Plain heading", "eyebrow": "", "text": "Body two."},
    ]


def test_a_page_without_boundaries_is_one_intro_record(manual: Path):
    assert _records(manual, "index.html") == [
        {"anchor": "", "heading": "Start here", "eyebrow": "", "text": "Hub Hub words."}
    ]


def test_an_empty_intro_is_dropped(manual: Path):
    _write(manual, "guide.html", _page("Guide", '<h2 class="sec" id="only">Only</h2><p>W.</p>'))
    assert [r["anchor"] for r in _records(manual, "guide.html")] == ["only"]


def test_glossary_entries_are_sections_headed_by_their_dt(manual: Path):
    _write(
        manual,
        "glossary.html",
        _page(
            "Glossary",
            '<h1 class="title">Glossary</h1>\n<dl class="gloss">\n'
            '<div id="dut"><dt>DUT<span class="mono">Device Under Test</span></dt>'
            "<dd>A device you test.</dd></div>\n"
            '<div id="fork"><dt>Fork</dt><dd>A private <b>copy</b>.</dd></div>\n</dl>',
        ),
    )
    assert _records(manual, "glossary.html") == [
        {"anchor": "", "heading": "Glossary", "eyebrow": "", "text": "Glossary"},
        {
            "anchor": "dut",
            "heading": "DUT Device Under Test",
            "eyebrow": "",
            "text": "A device you test.",
        },
        {"anchor": "fork", "heading": "Fork", "eyebrow": "", "text": "A private copy."},
    ]


def test_a_heading_outside_the_three_boundary_kinds_is_body_text(manual: Path):
    _edit(
        manual / "guide.html",
        "<p>Body one.</p>",
        '<p>Body one.</p><figure class="shot"><h2>Mockup dialog</h2></figure>'
        '<h3 class="step">A step</h3><h2>Plain h2</h2>',
    )
    first = _records(manual, "guide.html")[1]
    assert first["text"] == "Body one. Mockup dialog A step Plain h2"


@pytest.mark.parametrize(
    ("boundary", "suggestion"),
    [
        ('<h2 class="sec">Shared &amp; exclusive gear</h2>', 'id="shared-exclusive-gear"'),
        (
            '<dl class="gloss"><div><dt>Dry run</dt><dd>Words.</dd></div></dl>',
            'id="dry-run"',
        ),
    ],
    ids=["h2-sec", "glossary-entry"],
)
def test_a_section_without_an_id_stops_the_build_with_a_suggestion(
    manual: Path, boundary: str, suggestion: str
):
    _edit(manual / "guide.html", "<p>Body two.</p>", f"<p>Body two.</p>\n{boundary}")
    with pytest.raises(gen.ManualIndexError) as raised:
        gen.build_index(manual)
    message = str(raised.value)
    assert message.startswith("guide.html:")
    assert "has no id, so a search result cannot link to it" in message
    assert suggestion in message
    assert "make manual-index" in message


def test_every_unanchored_section_is_reported_at_once(manual: Path):
    _edit(manual / "guide.html", ' id="first"', "")
    _edit(manual / "guide.html", ' id="second"', "")
    with pytest.raises(gen.ManualIndexError) as raised:
        gen.build_index(manual)
    assert len(str(raised.value).splitlines()) == 2


def test_a_duplicate_section_id_stops_the_build(manual: Path):
    _edit(manual / "guide.html", 'id="second"', 'id="first"')
    with pytest.raises(gen.ManualIndexError, match="id 'first' is already used"):
        gen.build_index(manual)


def test_a_section_with_no_heading_text_stops_the_build(manual: Path):
    _edit(manual / "guide.html", ">Plain heading<", "><")
    with pytest.raises(gen.ManualIndexError, match="section 'second' has no heading text"):
        gen.build_index(manual)


def test_a_page_without_a_title_stops_the_build(manual: Path):
    _write(manual, "guide.html", "<html><body><p>No title.</p></body></html>")
    with pytest.raises(gen.ManualIndexError, match="guide.html: has no <title>"):
        gen.build_index(manual)


def test_an_empty_directory_stops_the_build(tmp_path: Path):
    with pytest.raises(gen.ManualIndexError, match="no \\*.html pages"):
        gen.build_index(tmp_path)


def test_a_sectitle_with_an_id_and_no_h2_before_it_is_its_own_section(manual: Path):
    _edit(
        manual / "guide.html",
        "<p>Body two.</p>",
        '<p>Body two.</p>\n<p class="sectitle" id="step2">Step 2 &middot; Templates</p>\n'
        '<p>Step two body.</p>\n<p class="sectitle">No id, so body</p>',
    )
    records = _records(manual, "guide.html")
    assert records[2] == {
        "anchor": "second",
        "heading": "Plain heading",
        "eyebrow": "",
        "text": "Body two.",
    }
    assert records[3] == {
        "anchor": "step2",
        "heading": "Step 2 \u00b7 Templates",
        "eyebrow": "",
        "text": "Step two body. No id, so body",
    }


def test_a_sectitle_with_an_id_right_after_a_section_h2_is_that_section_heading(manual: Path):
    _edit(
        manual / "guide.html",
        '<p class="sectitle">Title one</p>',
        '<p class="sectitle" id="t1">Title one</p>',
    )
    assert [(r["anchor"], r["heading"]) for r in _records(manual, "guide.html")] == [
        ("", "Guide"),
        ("first", "Title one"),
        ("second", "Plain heading"),
    ]


@pytest.mark.parametrize(
    "wrapper",
    [
        '<aside class="note">{}</aside>',
        '<div class="shot-stage">{}</div>',
        '<div class="foot">{}</div>',
    ],
    ids=["aside", "shot-stage", "foot"],
)
def test_a_boundary_inside_an_excluded_region_is_not_a_boundary(manual: Path, wrapper: str):
    inner = '<p class="sectitle" id="hidden">Hidden</p><h2 class="sec" id="hidden2">Hidden 2</h2>'
    _edit(manual / "guide.html", "<p>Body two.</p>", "<p>Body two.</p>" + wrapper.format(inner))
    assert [r["anchor"] for r in _records(manual, "guide.html")] == ["", "first", "second"]
    assert _records(manual, "guide.html")[2]["text"] == "Body two."


def test_an_excluded_sectitle_right_after_a_section_h2_is_not_its_heading(manual: Path):
    _edit(
        manual / "guide.html",
        '<p class="sectitle">Title one</p>',
        '<p class="sectitle foot">Hidden title</p>',
    )
    assert _records(manual, "guide.html")[1] == {
        "anchor": "first",
        "heading": "Eyebrow one",
        "eyebrow": "",
        "text": "Body one.",
    }


def test_text_between_a_section_h2_and_a_sectitle_cancels_the_sectitle(manual: Path):
    _edit(
        manual / "guide.html",
        '<h2 class="sec" id="first">Eyebrow one</h2>\n<p class="sectitle">Title one</p>',
        '<h2 class="sec" id="first">Eyebrow one</h2>\nLoose text\n'
        '<p class="sectitle">Title one</p>',
    )
    assert _records(manual, "guide.html")[1] == {
        "anchor": "first",
        "heading": "Eyebrow one",
        "eyebrow": "",
        "text": "Loose text Title one Body one.",
    }


def test_another_start_tag_between_a_section_h2_and_a_sectitle_cancels_it(manual: Path):
    _edit(
        manual / "guide.html",
        '<h2 class="sec" id="first">Eyebrow one</h2>\n<p class="sectitle">Title one</p>',
        '<h2 class="sec" id="first">Eyebrow one</h2><hr><p class="sectitle">Title one</p>',
    )
    assert _records(manual, "guide.html")[1]["heading"] == "Eyebrow one"


def test_a_dt_outside_a_glossary_list_is_body_text(manual: Path):
    _edit(
        manual / "guide.html",
        "<p>Body two.</p>",
        '<p>Body two.</p><dl class="hd-dl"><div><dt>Status</dt><dd>ACTIVE</dd></div></dl>',
    )
    assert _records(manual, "guide.html")[2] == {
        "anchor": "second",
        "heading": "Plain heading",
        "eyebrow": "",
        "text": "Body two. Status ACTIVE",
    }


def test_a_title_outside_head_is_not_the_page_title(manual: Path):
    _edit(
        manual / "guide.html",
        "<p>Body two.</p>",
        "<p>Body two.</p><svg><title>Icon name</title></svg>",
    )
    index = gen.build_index(manual)
    assert [p["title"] for p in index["pages"] if p["file"] == "guide.html"] == ["Guide"]


def test_a_page_whose_only_title_is_outside_head_has_no_title(manual: Path):
    _write(
        manual,
        "guide.html",
        "<html><head></head><body><svg><title>Icon</title></svg></body></html>",
    )
    with pytest.raises(gen.ManualIndexError, match="guide.html: has no <title>"):
        gen.build_index(manual)


def test_shot_stage_mockups_are_excluded_but_their_captions_are_not(manual: Path):
    _edit(
        manual / "guide.html",
        "<p>Body two.</p>",
        '<p>Body two.</p><figure class="shot"><div class="shot-frame"><div class="shot-stage">'
        "<span>Inventory</span><span>Topology</span><h2>Dialog</h2></div></div>"
        '<p class="shotcap"><b>The caption.</b></p></figure>',
    )
    assert _records(manual, "guide.html")[2]["text"] == "Body two. The caption."


@pytest.mark.parametrize(
    "name", ["Guide.html", "guide two.html", "guide_two.html", "gu\u00efde.html"]
)
def test_a_page_name_outside_the_slug_pattern_stops_the_build(manual: Path, name: str):
    _write(manual, name, _page("Odd", "<p>Odd.</p>"))
    with pytest.raises(gen.ManualIndexError, match="page file names must match"):
        gen.build_index(manual)


def test_a_byte_order_mark_is_ignored(manual: Path):
    before = _records(manual, "guide.html")
    path = manual / "guide.html"
    path.write_bytes(b"\xef\xbb\xbf" + path.read_bytes())
    assert _records(manual, "guide.html") == before
    assert [p["title"] for p in gen.build_index(manual)["pages"] if p["file"] == "guide.html"] == [
        "Guide"
    ]


# Generator: text


def test_entities_decode_and_whitespace_collapses(manual: Path):
    _edit(
        manual / "guide.html",
        "<p>Body two.</p>",
        "<p>Device\n   health &amp; config&nbsp;&nbsp;versions &mdash; it&#39;s &lt;fine&gt;\t"
        "&rarr; done.</p>",
    )
    assert _records(manual, "guide.html")[2]["text"] == (
        "Device health & config versions \u2014 it's <fine> \u2192 done."
    )


def test_markup_looking_text_stays_literal_text(manual: Path):
    _edit(
        manual / "guide.html",
        "<p>Body two.</p>",
        "<p>Type &lt;img src=x onerror=alert(1)&gt; here.</p>",
    )
    assert _records(manual, "guide.html")[2]["text"] == "Type <img src=x onerror=alert(1)> here."


def test_chrome_is_excluded(manual: Path):
    _edit(
        manual / "guide.html",
        "<p>Body one.</p>",
        "<p>Body one.</p><script>var hidden = 1;</script><style>.y{}</style>"
        "<template><p>Template words</p></template><noscript>Noscript words</noscript>"
        "<nav>Inner nav</nav><aside>Inner aside</aside>",
    )
    text = " ".join(r["text"] for r in _records(manual, "guide.html"))
    for absent in (
        "hidden",
        ".x{",
        ".y{",
        "Template words",
        "Noscript words",
        "Guide nav link",
        "Inner nav",
        "Sidebar entry",
        "Inner aside",
        "Footer words",
        "Next",
        "Top",
        "HERD",
    ):
        assert absent not in text, absent


def test_block_tags_break_words_and_inline_tags_do_not(manual: Path):
    _edit(
        manual / "guide.html",
        "<p>Body two.</p>",
        "<div>one</div><div>two</div><span>three</span>four<br>five<br/>six"
        "<td>seven</td>eight eth1/<b>1</b> <code>ge-0</code><em>/0</em> <a href='x'>li</a>nk",
    )
    assert _records(manual, "guide.html")[2]["text"] == (
        "one two three four five six seven eight eth1/1 ge-0/0 link"
    )


def test_heading_text_is_not_repeated_in_the_body(manual: Path):
    for record in _records(manual, "guide.html")[1:]:
        assert record["heading"] not in record["text"]
        if record["eyebrow"]:
            assert record["eyebrow"] not in record["text"]


# Generator: kinds, ordering, determinism, output


def test_release_kinds_order_by_integer_version(manual: Path):
    _write(manual, "release-0-10-0.html", _page("Release 0.10.0", "<p>Newest.</p>"))
    _write(manual, "release-0-9-0.html", _page("Release 0.9.0", "<p>Older.</p>"))
    kinds = {p["file"]: p["kind"] for p in gen.build_index(manual)["pages"]}
    assert kinds == {
        "guide.html": "page",
        "index.html": "page",
        "release-0-1-0.html": "earlier-release",
        "release-0-2-0.html": "earlier-release",
        "release-0-9-0.html": "earlier-release",
        "release-0-10-0.html": "release",
    }


def test_pages_are_in_file_name_order_and_titles_lose_the_prefix(manual: Path):
    pages = gen.build_index(manual)["pages"]
    assert [p["file"] for p in pages] == sorted(p["file"] for p in pages)
    assert {p["title"] for p in pages} == {
        "Guide",
        "Start here",
        "Release 0.1.0",
        "Release 0.2.0",
    }


def test_output_is_deterministic_and_independent_of_file_creation_order(
    manual: Path, tmp_path: Path
):
    first = gen.generate(manual)
    assert gen.generate(manual) == first
    copy = tmp_path / "copy"
    (copy / "assets").mkdir(parents=True)
    for path in reversed(_pages(manual)):
        shutil.copy(path, copy / path.name)
    assert gen.generate(copy) == first


def test_output_is_ascii_with_one_record_per_line(manual: Path):
    _edit(
        manual / "guide.html", "<p>Body two.</p>", "<p>Dash &mdash; arrow &rarr; dot &middot;</p>"
    )
    text = gen.generate(manual)
    assert text.isascii()
    assert "\\u2014" in text and "\\u2192" in text
    lines = text.splitlines()
    assert lines[0].startswith("// Generated by scripts/build_manual_search_index.py")
    assert "make manual-index" in lines[1]
    assert lines[2] == "window.HERD_MANUAL_SEARCH_INDEX = {"
    assert lines[-1] == "};"
    records = [line for line in lines if line.startswith('    {"')]
    index = gen.build_index(manual)
    assert len(records) == len(index["pages"]) + len(index["sections"])
    assert "generated at" not in text.lower()


# CLI


def test_cli_writes_then_check_passes_then_detects_drift(manual: Path, capsys):
    out = _index_path(manual)
    out.unlink()
    assert gen.main(["--manual-dir", str(manual)]) == 0
    assert out.read_text(encoding="utf-8") == gen.generate(manual)
    assert "4 pages, 6 sections" in capsys.readouterr().out
    assert gen.main(["--manual-dir", str(manual), "--check"]) == 0
    _edit(manual / "guide.html", "<p>Body two.</p>", "<p>Body three.</p>")
    assert gen.main(["--manual-dir", str(manual), "--check"]) == 1
    assert "is stale: run `make manual-index`" in capsys.readouterr().err


def test_cli_reports_a_refused_page_with_exit_code_2(manual: Path, capsys):
    _edit(manual / "guide.html", ' id="second"', "")
    assert gen.main(["--manual-dir", str(manual)]) == 2
    assert capsys.readouterr().err.startswith("error: guide.html:")


def test_cli_builds_the_index_once(manual: Path, monkeypatch, capsys):
    calls = []
    real = gen.build_index

    def counting(manual_dir):
        calls.append(manual_dir)
        return real(manual_dir)

    monkeypatch.setattr(gen, "build_index", counting)
    assert gen.main(["--manual-dir", str(manual)]) == 0
    assert len(calls) == 1
    assert gen.main(["--manual-dir", str(manual), "--check"]) == 0
    assert len(calls) == 2
