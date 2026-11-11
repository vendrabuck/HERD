"""Build the published manual's search index (issue #1298).

The manual under `docs/manual/` is hand-written static HTML served by GitHub Pages and
opened from `file://` as well, so its search runs entirely in the browser from a prebuilt
index: this script walks `docs/manual/*.html` and writes
`docs/manual/assets/search-index.js`, which assigns the index to the global
`window.HERD_MANUAL_SEARCH_INDEX` (a script element works from `file://`, where `fetch`
is refused). No page loads it directly: `docs/manual/assets/manual.js` adds a script
element for it the first time a reader focuses or types in the search box, then reads
that global and does the matching. The standard library only; no third-party dependency.

Usage, from the repo root (or `make manual-index`):

    python3 scripts/build_manual_search_index.py          # rewrite the index
    python3 scripts/build_manual_search_index.py --check  # exit 1 if it is stale

`tests/unit/test_manual_search_index.py` regenerates the index in memory and fails when the
committed file differs, so a page edit must be followed by a regeneration.

What a record is:

- Pages are the `*.html` files directly in the manual directory, in file-name order.
  Every page name must match `[a-z0-9-]+.html` (it becomes part of a result link), or
  the build stops. Pages are read as UTF-8, a leading byte order mark ignored. A page's
  title is the text of the `<title>` inside its `<head>` (an SVG `<title>` in the body
  does not count) with the `HERD Manual: ` prefix removed; a page with no such
  `<title>` is an error.
- A page is cut into sections at its section boundaries, in document order:
  1. an `<h2>` whose class includes `sec` (the manual's section headings, the targets
     of each page's "On this page" sidebar). Its heading is the text of the
     `<p class="sectitle">` that immediately follows it, when one does, and its eyebrow
     is the `<h2>` text; otherwise the heading is the `<h2>` text and the eyebrow is
     empty.
     Any text, or any other start tag, between the `<h2>` and the `<p>` means no
     sectitle follows.
  2. a `<p>` whose class includes `sectitle`, that carries an `id`, and that is not the
     sectitle of a section `<h2>` as in 1 (a step heading that a sidebar links on its
     own, as on `admin-setup.html`). Its heading is its own text; its eyebrow is empty.
     A `<p class="sectitle">` with no `id` that follows no section `<h2>` is body text.
  3. a `<div>` whose parent is a `<dl>` whose class includes `gloss` (one glossary
     entry). Its heading is the text of its `<dt>` (a `<dt>` anywhere else is body
     text); its eyebrow is empty.
  A boundary inside an excluded region (below) is not a boundary.
  Every boundary must carry an `id`, which becomes the record's anchor; a boundary
  without one stops the build with an error naming the page, the line, and a suggested
  id, so the fix is made in the page and every result link lands on a real element.
  Ids must be unique within a page.
- The text before a page's first boundary is its intro record: heading = the page
  title, anchor = "" (the link is the page itself, which opens at its top). An intro
  with no text is dropped.
- Indexed text is every text node outside the excluded chrome: `<script>`, `<style>`,
  `<template>`, `<noscript>`, `<header>`, `<nav>`, `<aside>`, and any element whose
  class includes `foot`, `pagenav`, or `shot-stage` (the drawn UI mockups, whose labels
  would otherwise glue into tokens such as `InventoryTopologyReservations`) or whose id
  is `totop`. Heading, eyebrow, and dt
  text is not repeated in the body. Character references are decoded; every start or
  end tag except the inline phrasing tags (`a`, `abbr`, `b`, `cite`, `code`, `em`, `i`,
  `kbd`, `mark`, `q`, `s`, `small`, `strong`, `sub`, `sup`, `u`, `var`) counts as a word
  break; each whitespace run (`&nbsp;` included) collapses to one space.
- Body text is not truncated: the whole manual indexes to about 240 KB. The guard holds
  the file under `SIZE_BUDGET_BYTES`; past it, cap per-section text here and say so.
- A page's kind is `release` for the newest `release-X-Y-Z.html` (ordered by the integer
  triple, as in `tests/unit/test_manual_release_pages.py`), `earlier-release` for every
  other release page, and `page` otherwise. The search ranks earlier releases last and
  labels both release kinds as release notes.

Output is deterministic: no timestamp, keys sorted, one page or section per line,
non-ASCII characters escaped (so the file itself carries no em-dash or arrow glyph that
the pages encode as entities).

Limits: the parser is the standard library's forgiving `HTMLParser`, so an unclosed
excluded element (say an `<aside>` with no end tag) hides the rest of the page from the
index; a heading that is not one of the three boundary kinds above (an `<h3>`, an `<h2>`
without the `sec` class) is body text of the section it sits in; text in attribute
values (`alt`, `title`, `aria-label`) and HTML comments is not indexed.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
MANUAL_DIR = REPO_ROOT / "docs" / "manual"
INDEX_PATH = MANUAL_DIR / "assets" / "search-index.js"
INDEX_GLOBAL = "HERD_MANUAL_SEARCH_INDEX"
INDEX_FORMAT = 1
REGENERATE_COMMAND = "make manual-index"
TITLE_PREFIX = "HERD Manual: "
SIZE_BUDGET_BYTES = 400_000

RELEASE_NAME = re.compile(r"^release-(\d+)-(\d+)-(\d+)\.html$")
PAGE_NAME = re.compile(r"^[a-z0-9-]+\.html$")
EXCLUDED_TAGS = frozenset("script style template noscript header nav aside".split())
EXCLUDED_CLASSES = frozenset({"foot", "pagenav", "shot-stage"})
EXCLUDED_IDS = frozenset({"totop"})
INLINE_TAGS = frozenset("a abbr b cite code em i kbd mark q s small strong sub sup u var".split())
VOID_TAGS = frozenset(
    "area base br col embed hr img input link meta param source track wbr".split()
)

HEADER = (
    "// Generated by scripts/build_manual_search_index.py from docs/manual/*.html (issue #1298).\n"
    f"// Do not edit by hand: run `{REGENERATE_COMMAND}` after changing any manual page.\n"
)


class ManualIndexError(Exception):
    """A page the index cannot represent faithfully; the message names the fix."""


def collapse(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def suggest_id(heading: str) -> str:
    """A stable id for a heading: lower case, runs of other characters to one hyphen."""
    slug = re.sub(r"[^a-z0-9]+", "-", heading.lower()).strip("-")
    return slug or "section"


@dataclass
class _Section:
    anchor: str | None  # "" for the intro, None for a boundary with no id
    line: int
    heading: list[str] = field(default_factory=list)
    eyebrow: list[str] = field(default_factory=list)
    body: list[str] = field(default_factory=list)


@dataclass
class _Open:
    tag: str
    classes: tuple[str, ...]
    excluded: bool
    capture: bool = False  # text inside goes to the section heading


class _PageParser(HTMLParser):
    def __init__(self, page: str) -> None:
        super().__init__(convert_charrefs=True)
        self.page = page
        self.title_parts: list[str] = []
        self.has_title = False
        self.sections: list[_Section] = [_Section(anchor="", line=1)]
        self.anchors: dict[str, int] = {}
        self._stack: list[_Open] = []
        self._in_title = False
        # After a section <h2> closes: the next start tag decides whether a
        # <p class="sectitle"> supplies the heading.
        self._await_sectitle = False

    # -- helpers --

    @property
    def _excluded(self) -> bool:
        return any(entry.excluded for entry in self._stack)

    @property
    def _capture(self) -> bool:
        return any(entry.capture for entry in self._stack)

    def _emit(self, text: str) -> None:
        if self._excluded:
            return
        section = self.sections[-1]
        (section.heading if self._capture else section.body).append(text)

    def _start_section(self, attrs: dict[str, str | None]) -> None:
        line = self.getpos()[0]
        anchor = (attrs.get("id") or "").strip() or None
        if anchor is not None:
            if anchor in self.anchors:
                raise ManualIndexError(
                    f"{self.page}:{line}: id {anchor!r} is already used by the section on "
                    f"line {self.anchors[anchor]}; section ids must be unique within a page"
                )
            self.anchors[anchor] = line
        self.sections.append(_Section(anchor=anchor, line=line))

    # -- HTMLParser callbacks --

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        classes = tuple((values.get("class") or "").split())
        if tag == "title" and not self._excluded and any(e.tag == "head" for e in self._stack):
            self._in_title = True
            self.has_title = True
        if tag not in INLINE_TAGS:
            self._emit(" ")

        excluded = (
            tag in EXCLUDED_TAGS
            or bool(EXCLUDED_CLASSES.intersection(classes))
            or (values.get("id") or "") in EXCLUDED_IDS
        )
        capture = False
        sectitle = tag == "p" and "sectitle" in classes and not excluded and not self._excluded
        followed = False
        if self._await_sectitle:
            self._await_sectitle = False
            if sectitle:
                section = self.sections[-1]
                section.eyebrow, section.heading = section.heading, []
                capture = followed = True
        if not excluded and not self._excluded:
            parent = self._stack[-1] if self._stack else None
            if sectitle and not followed and (values.get("id") or "").strip():
                self._start_section(values)
                capture = True
            elif tag == "h2" and "sec" in classes:
                self._start_section(values)
                capture = True
            elif tag == "div" and parent and parent.tag == "dl" and "gloss" in parent.classes:
                self._start_section(values)
            elif tag == "dt" and parent and parent.tag == "div":
                grand = self._stack[-2] if len(self._stack) > 1 else None
                if grand and grand.tag == "dl" and "gloss" in grand.classes:
                    capture = True
        if tag not in VOID_TAGS:
            self._stack.append(_Open(tag, classes, excluded, capture))

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        # <br/>, <img/>, <path/>: a word break, never an open element.
        if tag not in INLINE_TAGS:
            self._emit(" ")

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._in_title = False
        if tag not in INLINE_TAGS:
            self._emit(" ")
        for index in range(len(self._stack) - 1, -1, -1):
            if self._stack[index].tag == tag:
                closed = self._stack[index]
                del self._stack[index:]
                if closed.tag == "h2" and closed.capture:
                    self._await_sectitle = True
                break

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title_parts.append(data)
            return
        if self._await_sectitle and data.strip() and not self._excluded:
            self._await_sectitle = False
        self._emit(data)


def _release_kinds(names: list[str]) -> dict[str, str]:
    releases = sorted(
        (tuple(int(n) for n in m.groups()), name)
        for name in names
        if (m := RELEASE_NAME.match(name))
    )
    kinds = {name: "earlier-release" for _, name in releases}
    if releases:
        kinds[releases[-1][1]] = "release"
    return kinds


def parse_page(path: Path) -> tuple[str, list[dict[str, str]]]:
    """Return a page's title and its section records (without the page index)."""
    parser = _PageParser(path.name)
    parser.feed(path.read_text(encoding="utf-8-sig"))
    parser.close()
    if not parser.has_title:
        raise ManualIndexError(f"{path.name}: has no <title>")
    title = collapse("".join(parser.title_parts))
    if title.startswith(TITLE_PREFIX):
        title = title[len(TITLE_PREFIX) :].strip()
    if not title:
        raise ManualIndexError(f"{path.name}: <title> is empty")
    records, unanchored = [], []
    for section in parser.sections:
        body = collapse("".join(section.body))
        if section.anchor == "":
            if not body:
                continue
            heading, eyebrow = title, ""
        else:
            heading = collapse("".join(section.heading))
            eyebrow = collapse("".join(section.eyebrow))
            if section.anchor is None:
                unanchored.append(
                    f"{path.name}:{section.line}: section {heading!r} has no id, so a search "
                    f'result cannot link to it; add a stable id to it, for example id="'
                    f'{suggest_id(heading)}", then run `{REGENERATE_COMMAND}`'
                )
                continue
            if not heading:
                raise ManualIndexError(
                    f"{path.name}:{section.line}: section {section.anchor!r} has no heading text"
                )
        records.append(
            {"anchor": section.anchor, "heading": heading, "eyebrow": eyebrow, "text": body}
        )
    if unanchored:
        raise ManualIndexError("\n".join(unanchored))
    return title, records


def build_index(manual_dir: Path = MANUAL_DIR) -> dict:
    paths = sorted(p for p in manual_dir.glob("*.html") if p.is_file())
    if not paths:
        raise ManualIndexError(f"{manual_dir}: no *.html pages")
    misnamed = [p.name for p in paths if not PAGE_NAME.match(p.name)]
    if misnamed:
        raise ManualIndexError(
            f"page file names must match [a-z0-9-]+.html (they become result links): "
            f"{', '.join(misnamed)}"
        )
    kinds = _release_kinds([p.name for p in paths])
    pages, sections = [], []
    for page_index, path in enumerate(paths):
        title, records = parse_page(path)
        pages.append({"file": path.name, "title": title, "kind": kinds.get(path.name, "page")})
        for record in records:
            sections.append({"page": page_index, **record})
    return {"format": INDEX_FORMAT, "pages": pages, "sections": sections}


def _line(value: dict) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(", ", ": "))


def render_index(index: dict) -> str:
    pages = ",\n".join("    " + _line(page) for page in index["pages"])
    sections = ",\n".join("    " + _line(section) for section in index["sections"])
    return (
        HEADER
        + f"window.{INDEX_GLOBAL} = {{\n"
        + f'  "format": {index["format"]},\n'
        + '  "pages": [\n'
        + pages
        + "\n  ],\n"
        + '  "sections": [\n'
        + sections
        + "\n  ]\n};\n"
    )


def generate(manual_dir: Path = MANUAL_DIR) -> str:
    return render_index(build_index(manual_dir))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--manual-dir", type=Path, default=MANUAL_DIR)
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument(
        "--check", action="store_true", help="exit 1 if the index on disk is not current"
    )
    args = parser.parse_args(argv)
    output = args.output or args.manual_dir / "assets" / "search-index.js"
    try:
        index = build_index(args.manual_dir)
    except ManualIndexError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    data = render_index(index).encode("utf-8")
    current = output.read_bytes() if output.is_file() else None
    if args.check:
        if current != data:
            print(
                f"{output} is stale: run `{REGENERATE_COMMAND}` and commit the result",
                file=sys.stderr,
            )
            return 1
        print(f"{output} is current")
        return 0
    if current != data:
        output.write_bytes(data)
    print(
        f"{output}: {len(index['pages'])} pages, {len(index['sections'])} sections, "
        f"{len(data)} bytes"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
