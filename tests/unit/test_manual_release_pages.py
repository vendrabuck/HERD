"""Guard: the manual's release pages state no fact that expires when the next release ships.

The manual under `docs/manual/` is hand-written HTML: ordinary pages plus one
`release-X-Y-Z.html` page per release, each page carrying its own copy of the header
nav. Until 0.7.0 each old release page was left calling its successor "the current
release", which became false one release later. The rule now: a release page that is no
longer current states only facts that stay true, and the current release is named in one
place, the header link, which every manual page carries.

Terms: a release page is a file directly in `docs/manual/` whose name matches
`release-<digits>-<digits>-<digits>.html`. Release pages are ordered by the integer
triple, never by string, so 0.10.0 is newer than 0.9.0. The newest is CURRENT; every
other release page is DEMOTED. A release link is an `<a>` element with an `href` whose
last path segment, with any query or fragment removed, is a release page name (the file
need not exist); the link's target is that name.

What it requires (each check is a function returning a list of problem strings, run
against the real tree and against fixture trees):

1. Header link. In every `*.html` file directly in `docs/manual/`, the release links
   inside `<header>` elements number exactly one, that link targets the current release
   page, and its text, whitespace collapsed, is exactly `Release X.Y.Z` for the current
   version.
2. Sidebar list. Every release page has an `<aside>` whose class includes `side`; the
   release links inside such elements, in document order, are exactly every release
   page, newest first, each once; exactly one of them has the class `active`, and it
   targets the page itself.
3. Expiring phrasings. No demoted release page's text contains "The current release is",
   "now describes", "now documents", or "The version this manual documents". The text is
   every text node outside `<script>` and `<style>`, concatenated in document order, with
   each whitespace run (line breaks and `&nbsp;` included) collapsed to one space,
   compared case-insensitively, so a phrase split by a line break or an inline tag with
   whitespace beside it is still found. The current release page is exempt.
4. Successor link. Every demoted release page has a `<p class="dek">` (class includes
   `dek`), and some dek on the page contains a release link targeting the release page
   immediately after it in version order.

Limits:

- Check 3 knows only the four phrasings above. A new way of saying the same thing, such
  as "the latest release is 0.3.0", passes. Text in HTML comments and in attribute values
  (`alt`, `title`, a meta description) is not read, and words that meet only across a tag
  with no whitespace on either side (`now<b>describes</b>`) do not match.
- The guard judges no other prose on any page. An ordinary page that names a version in
  its text goes stale unnoticed.
- The previous and next links at the foot of release pages, and every release link
  outside the header, the sidebar, and the dek, are not checked. Neither is the header
  link's class, nor any sidebar entry's text.
- Only files directly in `docs/manual/` are read. A release page misnamed so that it does
  not match the pattern (`release-0-8.html`, `Release-0-8-0.html`) is an ordinary page to
  this guard and gets only the header check.
- Links are compared by the last path segment of the href, so `../release-0-7-0.html`
  and `release-0-7-0.html#what` both target `release-0-7-0.html`.
- The dek runs from a `<p>` start tag whose class includes `dek` to the next `</p>`,
  `<p>` start tag, or block-level start tag (`div`, `ul`, `ol`, `table`, `section`,
  `main`, `aside`, `header`, `footer`, `nav`, `h1` to `h6`, `pre`, `blockquote`,
  `figure`, `hr`). Check 4 does not judge what the dek says around the link, or what
  else it links.
- HTML is read with the standard library's forgiving parser: an unclosed `<header>` or
  `<aside>` runs to the end of the page, and an unclosed `<a>` runs to the next `<a>` that
  has an `href`, or to the next `</a>`.
"""

import re
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
MANUAL_DIR = REPO_ROOT / "docs" / "manual"

RELEASE_NAME = re.compile(r"^release-(\d+)-(\d+)-(\d+)\.html$")
EXPIRING_PHRASES = (
    "The current release is",
    "now describes",
    "now documents",
    "The version this manual documents",
)
_DEK_ENDERS = frozenset(
    "div ul ol table section main aside header footer nav "
    "h1 h2 h3 h4 h5 h6 pre blockquote figure hr".split()
)


def _collapse(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _release_target(href: str) -> str | None:
    segment = href.split("#", 1)[0].split("?", 1)[0].rsplit("/", 1)[-1]
    return segment if RELEASE_NAME.match(segment) else None


@dataclass
class _Link:
    target: str | None
    classes: tuple[str, ...]
    in_header: bool
    in_sidebar: bool
    in_dek: bool
    text: str = ""


class _PageParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[_Link] = []
        self.dek_count = 0
        self.sidebar_count = 0
        self._text: list[str] = []
        self._header_depth = 0
        self._aside_stack: list[bool] = []
        self._in_dek = False
        self._skip_depth = 0
        self._link: _Link | None = None
        self._link_text: list[str] = []

    @property
    def text(self) -> str:
        return _collapse("".join(self._text))

    def _finish_link(self) -> None:
        if self._link is not None:
            self._link.text = _collapse("".join(self._link_text))
            self.links.append(self._link)
        self._link = None
        self._link_text = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        classes = tuple((values.get("class") or "").split())
        if tag in ("script", "style"):
            self._skip_depth += 1
        if tag == "p" or tag in _DEK_ENDERS:
            self._in_dek = False
        if tag == "p" and "dek" in classes:
            self._in_dek = True
            self.dek_count += 1
        if tag == "header":
            self._header_depth += 1
        elif tag == "aside":
            self._aside_stack.append("side" in classes)
            if "side" in classes:
                self.sidebar_count += 1
        elif tag == "a" and values.get("href") is not None:
            self._finish_link()
            self._link = _Link(
                target=_release_target(values["href"] or ""),
                classes=classes,
                in_header=self._header_depth > 0,
                in_sidebar=any(self._aside_stack),
                in_dek=self._in_dek,
            )

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style") and self._skip_depth:
            self._skip_depth -= 1
        elif tag == "p":
            self._in_dek = False
        elif tag == "header" and self._header_depth:
            self._header_depth -= 1
        elif tag == "aside" and self._aside_stack:
            self._aside_stack.pop()
        elif tag == "a":
            self._finish_link()

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        self._text.append(data)
        if self._link is not None:
            self._link_text.append(data)


def _parse(path: Path) -> _PageParser:
    parser = _PageParser()
    parser.feed(path.read_text(encoding="utf-8"))
    parser.close()
    parser._finish_link()
    return parser


def _release_pages(manual_dir: Path) -> list[str]:
    """Release page names, oldest first, ordered by the integer version triple."""
    found = []
    for path in manual_dir.glob("release-*.html"):
        match = RELEASE_NAME.match(path.name)
        if match and path.is_file():
            found.append((tuple(int(n) for n in match.groups()), path.name))
    return [name for _, name in sorted(found)]


def _version(name: str) -> str:
    match = RELEASE_NAME.match(name)
    assert match, name
    return ".".join(str(int(n)) for n in match.groups())


def _no_release_pages(manual_dir: Path) -> list[str]:
    return [f"{manual_dir}: no release-X-Y-Z.html pages"]


def check_header_links(manual_dir: Path) -> list[str]:
    releases = _release_pages(manual_dir)
    if not releases:
        return _no_release_pages(manual_dir)
    current = releases[-1]
    want_text = f"Release {_version(current)}"
    problems = []
    for path in sorted(p for p in manual_dir.glob("*.html") if p.is_file()):
        links = [link for link in _parse(path).links if link.in_header and link.target]
        if not links:
            problems.append(f"{path.name}: header has no release link; expected one to {current}")
            continue
        if len(links) > 1:
            targets = ", ".join(str(link.target) for link in links)
            problems.append(
                f"{path.name}: header has {len(links)} release links ({targets}); "
                f"expected one, to {current}"
            )
            continue
        link = links[0]
        if link.target != current:
            problems.append(
                f"{path.name}: header release link targets {link.target}, "
                f"not the current release page {current}"
            )
        if link.text != want_text:
            problems.append(
                f"{path.name}: header release link text is {link.text!r}, expected {want_text!r}"
            )
    return problems


def check_sidebars(manual_dir: Path) -> list[str]:
    releases = _release_pages(manual_dir)
    if not releases:
        return _no_release_pages(manual_dir)
    want = list(reversed(releases))
    problems = []
    for name in releases:
        page = _parse(manual_dir / name)
        if not page.sidebar_count:
            problems.append(f'{name}: has no <aside class="side">')
            continue
        links = [link for link in page.links if link.in_sidebar and link.target]
        got = [str(link.target) for link in links]
        if got != want:
            problems.append(
                f"{name}: sidebar release links are [{', '.join(got)}], "
                f"expected newest first [{', '.join(want)}]"
            )
        active = [str(link.target) for link in links if "active" in link.classes]
        if len(active) != 1:
            problems.append(
                f"{name}: sidebar marks {len(active)} release entries active; "
                f"expected exactly one, its own entry {name}"
            )
        elif active[0] != name:
            problems.append(f"{name}: sidebar marks {active[0]} active, not its own entry")
    return problems


def check_expiring_phrasings(manual_dir: Path) -> list[str]:
    releases = _release_pages(manual_dir)
    if not releases:
        return _no_release_pages(manual_dir)
    problems = []
    for name in releases[:-1]:
        text = _parse(manual_dir / name).text.casefold()
        for phrase in EXPIRING_PHRASES:
            if phrase.casefold() in text:
                problems.append(
                    f"{name}: demoted release page contains the expiring phrase {phrase!r}"
                )
    return problems


def check_successor_links(manual_dir: Path) -> list[str]:
    releases = _release_pages(manual_dir)
    if not releases:
        return _no_release_pages(manual_dir)
    problems = []
    for name, successor in zip(releases, releases[1:]):
        page = _parse(manual_dir / name)
        if not page.dek_count:
            problems.append(f'{name}: has no <p class="dek">')
            continue
        if successor not in {link.target for link in page.links if link.in_dek}:
            problems.append(f"{name}: dek does not link the next release page, {successor}")
    return problems


CHECKS = (check_header_links, check_sidebars, check_expiring_phrasings, check_successor_links)


# Real tree


def _real_manual() -> Path:
    releases = _release_pages(MANUAL_DIR)
    ordinary = [p for p in MANUAL_DIR.glob("*.html") if p.is_file() and p.name not in releases]
    assert len(releases) >= 2, (
        f"expected at least two release pages in {MANUAL_DIR}, found {releases}"
    )
    assert ordinary, f"expected at least one ordinary page in {MANUAL_DIR}"
    return MANUAL_DIR


def test_real_manual_has_release_and_ordinary_pages():
    _real_manual()


def test_real_header_links_name_the_current_release():
    problems = check_header_links(_real_manual())
    assert problems == [], "\n".join(problems)


def test_real_sidebars_list_every_release_newest_first():
    problems = check_sidebars(_real_manual())
    assert problems == [], "\n".join(problems)


def test_real_demoted_pages_carry_no_expiring_phrasing():
    problems = check_expiring_phrasings(_real_manual())
    assert problems == [], "\n".join(problems)


def test_real_demoted_pages_link_their_successor_in_the_dek():
    problems = check_successor_links(_real_manual())
    assert problems == [], "\n".join(problems)


# Fixture trees

ORDINARY = ("index.html", "guide.html")
VERSIONS = ((0, 1, 0), (0, 2, 0), (0, 3, 0))


def _file(version: tuple[int, int, int]) -> str:
    return "release-" + "-".join(str(n) for n in version) + ".html"


def _dotted(version: tuple[int, int, int]) -> str:
    return ".".join(str(n) for n in version)


def _header(current: tuple[int, int, int], link_class: str) -> str:
    return (
        '<header class="manualbar">\n'
        '  <a class="wm" href="index.html">HERD</a>\n'
        "  <nav>\n"
        '    <a href="guide.html">Guide</a>\n'
        f'    <a href="{_file(current)}"{link_class}>Release {_dotted(current)}</a>\n'
        "  </nav>\n"
        "</header>\n"
    )


def _write_manual(root: Path, versions=VERSIONS, ordinary=ORDINARY) -> Path:
    """Write a minimal valid manual: ordinary pages plus one release page per version."""
    manual = root / "manual"
    manual.mkdir()
    current = versions[-1]
    for name in ordinary:
        (manual / name).write_text(
            "<!doctype html>\n<html><head><title>Guide</title></head><body>\n"
            + _header(current, "")
            + f'<main><p>See <a href="{_file(current)}">the release notes</a>.</p></main>\n'
            "</body></html>\n",
            encoding="utf-8",
        )
    for index, version in enumerate(versions):
        sidebar = "".join(
            f'      <li><a href="{_file(v)}"{' class="active"' if v == version else ""}>'
            f"Release {_dotted(v)}</a></li>\n"
            for v in reversed(versions)
        )
        if version == current:
            dek = "The version this manual documents."
        else:
            successor = versions[index + 1]
            dek = (
                f"The tagged release {_dotted(version)}, kept as the record of what it shipped. "
                f'The release after it is <a href="{_file(successor)}">{_dotted(successor)}</a>.'
            )
        pager = ""
        if index > 0:
            pager += f'    <a href="{_file(versions[index - 1])}">Previous</a>\n'
        if version != current:
            pager += f'    <a href="{_file(versions[index + 1])}">Next</a>\n'
        (manual / _file(version)).write_text(
            "<!doctype html>\n<html><head><title>Release</title>\n"
            "<style>.dek{color:gray}</style></head><body>\n"
            + _header(current, ' class="active"')
            + '<div class="shell">\n'
            '  <aside class="side">\n'
            '    <ul><li><a href="#what">What it is</a></li></ul>\n'
            "    <ul>\n"
            '      <li><a href="guide.html">Guide</a></li>\n' + sidebar + "    </ul>\n"
            "  </aside>\n"
            '  <main class="main">\n'
            f'    <h1 class="title">Release {_dotted(version)}</h1>\n'
            f'    <p class="dek">{dek}</p>\n'
            "    <p>Body text.</p>\n"
            '    <div class="pager">\n' + pager + "    </div>\n"
            "  </main>\n"
            "</div>\n"
            "</body></html>\n",
            encoding="utf-8",
        )
    return manual


def _edit(path: Path, old: str, new: str) -> None:
    text = path.read_text(encoding="utf-8")
    assert text.count(old) == 1, f"{old!r} occurs {text.count(old)} times in {path.name}"
    path.write_text(text.replace(old, new), encoding="utf-8")


@pytest.fixture
def manual(tmp_path: Path) -> Path:
    return _write_manual(tmp_path)


@pytest.mark.parametrize("check", CHECKS, ids=lambda c: c.__name__)
def test_clean_fixture_has_no_problems(manual: Path, check):
    assert check(manual) == []


@pytest.mark.parametrize("check", CHECKS, ids=lambda c: c.__name__)
def test_a_directory_without_release_pages_is_a_problem(tmp_path: Path, check):
    assert check(tmp_path) == [f"{tmp_path}: no release-X-Y-Z.html pages"]


# Check 1: header link


def test_stale_header_link_on_an_ordinary_page(manual: Path):
    _edit(
        manual / "guide.html",
        '<a href="release-0-3-0.html">Release 0.3.0</a>',
        '<a href="release-0-2-0.html">Release 0.3.0</a>',
    )
    assert check_header_links(manual) == [
        "guide.html: header release link targets release-0-2-0.html, "
        "not the current release page release-0-3-0.html"
    ]


def test_stale_header_link_on_a_demoted_release_page(manual: Path):
    _edit(
        manual / "release-0-1-0.html",
        '<a href="release-0-3-0.html" class="active">Release 0.3.0</a>',
        '<a href="release-0-2-0.html" class="active">Release 0.3.0</a>',
    )
    assert check_header_links(manual) == [
        "release-0-1-0.html: header release link targets release-0-2-0.html, "
        "not the current release page release-0-3-0.html"
    ]


def test_page_with_no_header_release_link(manual: Path):
    # The body still links the current release; a link outside the header does not count.
    _edit(manual / "index.html", '    <a href="release-0-3-0.html">Release 0.3.0</a>\n', "")
    assert check_header_links(manual) == [
        "index.html: header has no release link; expected one to release-0-3-0.html"
    ]


def test_page_with_two_header_release_links(manual: Path):
    _edit(
        manual / "guide.html",
        '<a href="release-0-3-0.html">Release 0.3.0</a>',
        '<a href="release-0-3-0.html">Release 0.3.0</a>\n'
        '    <a href="release-0-2-0.html">Release 0.2.0</a>',
    )
    assert check_header_links(manual) == [
        "guide.html: header has 2 release links (release-0-3-0.html, release-0-2-0.html); "
        "expected one, to release-0-3-0.html"
    ]


def test_header_link_text_naming_the_wrong_version(manual: Path):
    _edit(
        manual / "guide.html",
        '<a href="release-0-3-0.html">Release 0.3.0</a>',
        '<a href="release-0-3-0.html">Release 0.2.0</a>',
    )
    assert check_header_links(manual) == [
        "guide.html: header release link text is 'Release 0.2.0', expected 'Release 0.3.0'"
    ]


def test_header_link_to_a_release_page_that_does_not_exist(manual: Path):
    _edit(
        manual / "index.html",
        '<a href="release-0-3-0.html">Release 0.3.0</a>',
        '<a href="release-0-4-0.html">Release 0.3.0</a>',
    )
    assert check_header_links(manual) == [
        "index.html: header release link targets release-0-4-0.html, "
        "not the current release page release-0-3-0.html"
    ]


# Check 2: sidebar list


def test_sidebar_missing_the_newest_release(manual: Path):
    _edit(
        manual / "release-0-1-0.html",
        '      <li><a href="release-0-3-0.html">Release 0.3.0</a></li>\n',
        "",
    )
    assert check_sidebars(manual) == [
        "release-0-1-0.html: sidebar release links are [release-0-2-0.html, release-0-1-0.html], "
        "expected newest first [release-0-3-0.html, release-0-2-0.html, release-0-1-0.html]"
    ]


def test_sidebar_in_the_wrong_order(manual: Path):
    _edit(
        manual / "release-0-2-0.html",
        '      <li><a href="release-0-3-0.html">Release 0.3.0</a></li>\n'
        '      <li><a href="release-0-2-0.html" class="active">Release 0.2.0</a></li>\n',
        '      <li><a href="release-0-2-0.html" class="active">Release 0.2.0</a></li>\n'
        '      <li><a href="release-0-3-0.html">Release 0.3.0</a></li>\n',
    )
    assert check_sidebars(manual) == [
        "release-0-2-0.html: sidebar release links are "
        "[release-0-2-0.html, release-0-3-0.html, release-0-1-0.html], "
        "expected newest first [release-0-3-0.html, release-0-2-0.html, release-0-1-0.html]"
    ]


def test_sidebar_with_the_active_class_on_the_wrong_entry(manual: Path):
    _edit(
        manual / "release-0-2-0.html",
        '<li><a href="release-0-3-0.html">Release 0.3.0</a></li>\n'
        '      <li><a href="release-0-2-0.html" class="active">',
        '<li><a href="release-0-3-0.html" class="active">Release 0.3.0</a></li>\n'
        '      <li><a href="release-0-2-0.html">',
    )
    assert check_sidebars(manual) == [
        "release-0-2-0.html: sidebar marks release-0-3-0.html active, not its own entry"
    ]


def test_sidebar_with_no_active_entry(manual: Path):
    _edit(
        manual / "release-0-2-0.html",
        '<li><a href="release-0-2-0.html" class="active">',
        '<li><a href="release-0-2-0.html">',
    )
    assert check_sidebars(manual) == [
        "release-0-2-0.html: sidebar marks 0 release entries active; "
        "expected exactly one, its own entry release-0-2-0.html"
    ]


def test_sidebar_with_two_active_entries(manual: Path):
    _edit(
        manual / "release-0-2-0.html",
        '<li><a href="release-0-1-0.html">',
        '<li><a href="release-0-1-0.html" class="active">',
    )
    assert check_sidebars(manual) == [
        "release-0-2-0.html: sidebar marks 2 release entries active; "
        "expected exactly one, its own entry release-0-2-0.html"
    ]


def test_release_page_without_a_side_aside(manual: Path):
    _edit(manual / "release-0-3-0.html", '<aside class="side">', '<aside class="toc">')
    assert check_sidebars(manual) == ['release-0-3-0.html: has no <aside class="side">']


# Check 3: expiring phrasings

DEMOTED_DEK = "kept as the record of what it shipped."


@pytest.mark.parametrize(
    ("sentence", "phrase"),
    [
        ("The current release is 0.3.0.", "The current release is"),
        ("This manual now describes 0.2.0.", "now describes"),
        ("This manual now documents 0.2.0.", "now documents"),
        ("The version this manual documents.", "The version this manual documents"),
    ],
)
def test_expiring_phrasing_on_a_demoted_page(manual: Path, sentence: str, phrase: str):
    _edit(manual / "release-0-2-0.html", DEMOTED_DEK, f"{DEMOTED_DEK} {sentence}")
    assert check_expiring_phrasings(manual) == [
        f"release-0-2-0.html: demoted release page contains the expiring phrase {phrase!r}"
    ]


@pytest.mark.parametrize(
    ("snippet", "phrase"),
    [
        ("This manual now\n      describes <b>0.3.0</b>.", "now describes"),
        ("This manual now <b>documents</b> 0.3.0.", "now documents"),
        (
            "The <em>current</em>\n  release is <a href='release-0-3-0.html'>0.3.0</a>.",
            "The current release is",
        ),
        ("THE CURRENT RELEASE&nbsp;IS 0.3.0.", "The current release is"),
    ],
    ids=["line-break-then-tag", "inline-tag", "tag-and-line-break", "upper-case-and-nbsp"],
)
def test_expiring_phrasing_split_across_markup_is_still_found(
    manual: Path, snippet: str, phrase: str
):
    _edit(manual / "release-0-1-0.html", DEMOTED_DEK, f"{DEMOTED_DEK} {snippet}")
    assert check_expiring_phrasings(manual) == [
        f"release-0-1-0.html: demoted release page contains the expiring phrase {phrase!r}"
    ]


def test_expiring_phrasing_on_the_current_page_is_not_reported(manual: Path):
    _edit(
        manual / "release-0-3-0.html",
        "<p>Body text.</p>",
        "<p>This manual now documents 0.3.0. The current release is 0.3.0; "
        "it now describes the list pages.</p>",
    )
    assert check_expiring_phrasings(manual) == []


def test_comments_attributes_and_style_text_are_not_read(manual: Path):
    # Pins the documented limit, so the docstring stays true of the scan.
    _edit(
        manual / "release-0-1-0.html",
        "<p>Body text.</p>",
        '<!-- The current release is 0.3.0 --><p title="now describes 0.3.0">Body text.</p>'
        "<style>/* now documents */</style>",
    )
    assert check_expiring_phrasings(manual) == []


# Check 4: successor link


def test_dek_without_the_successor_link(manual: Path):
    # The pager at the foot still links release-0-2-0.html; only the dek counts.
    _edit(
        manual / "release-0-1-0.html",
        'The release after it is <a href="release-0-2-0.html">0.2.0</a>.',
        "The release after it is 0.2.0.",
    )
    assert check_successor_links(manual) == [
        "release-0-1-0.html: dek does not link the next release page, release-0-2-0.html"
    ]


def test_dek_linking_a_later_release_that_is_not_the_immediate_successor(manual: Path):
    _edit(
        manual / "release-0-1-0.html",
        'The release after it is <a href="release-0-2-0.html">0.2.0</a>.',
        'The release after it is <a href="release-0-3-0.html">0.3.0</a>.',
    )
    assert check_successor_links(manual) == [
        "release-0-1-0.html: dek does not link the next release page, release-0-2-0.html"
    ]


def test_successor_link_in_the_paragraph_after_the_dek(manual: Path):
    _edit(
        manual / "release-0-1-0.html",
        'The release after it is <a href="release-0-2-0.html">0.2.0</a>.</p>',
        '</p>\n    <p>The release after it is <a href="release-0-2-0.html">0.2.0</a>.</p>',
    )
    assert check_successor_links(manual) == [
        "release-0-1-0.html: dek does not link the next release page, release-0-2-0.html"
    ]


def test_successor_link_after_an_unclosed_dek_and_a_block_tag(manual: Path):
    _edit(
        manual / "release-0-1-0.html",
        'The release after it is <a href="release-0-2-0.html">0.2.0</a>.</p>',
        '<div>The release after it is <a href="release-0-2-0.html">0.2.0</a>.</div>',
    )
    assert check_successor_links(manual) == [
        "release-0-1-0.html: dek does not link the next release page, release-0-2-0.html"
    ]


def test_demoted_page_without_a_dek(manual: Path):
    _edit(manual / "release-0-1-0.html", '<p class="dek">', "<p>")
    assert check_successor_links(manual) == ['release-0-1-0.html: has no <p class="dek">']


# Version ordering


def test_versions_order_by_integer_not_by_string(tmp_path: Path):
    manual = _write_manual(tmp_path, versions=((0, 9, 0), (0, 10, 0)))
    assert _release_pages(manual) == ["release-0-9-0.html", "release-0-10-0.html"]
    for check in CHECKS:
        assert check(manual) == [], check.__name__


def test_header_naming_the_string_largest_release_is_stale(tmp_path: Path):
    manual = _write_manual(tmp_path, versions=((0, 9, 0), (0, 10, 0)))
    _edit(
        manual / "index.html",
        '<a href="release-0-10-0.html">Release 0.10.0</a>',
        '<a href="release-0-9-0.html">Release 0.9.0</a>',
    )
    assert check_header_links(manual) == [
        "index.html: header release link targets release-0-9-0.html, "
        "not the current release page release-0-10-0.html",
        "index.html: header release link text is 'Release 0.9.0', expected 'Release 0.10.0'",
    ]


def test_misnamed_release_page_is_an_ordinary_page(manual: Path):
    # Pins the documented limit: a name off the pattern is never a release page.
    (manual / "release-0-4.html").write_text(
        (manual / "guide.html").read_text(encoding="utf-8"), encoding="utf-8"
    )
    assert _release_pages(manual) == [
        "release-0-1-0.html",
        "release-0-2-0.html",
        "release-0-3-0.html",
    ]
    for check in CHECKS:
        assert check(manual) == [], check.__name__
