"""Drift guard for the specifications under docs/specs/.

No stack, no database, no network: this only reads markdown and source files. A
specification names repository paths, the symbols that enforce each rule, and the tests
that pin it; a rename, a deleted test, or a moved file would otherwise leave it pointing
at nothing without anyone noticing. Every specification (every docs/specs/*.md except
README.md and TEMPLATE.md) must pass every check below; README.md gets the path and link
checks only, since it is the index, not a specification.

What counts as what (the rules docs/specs/README.md promises):

- Repository path: an inline code span whose whole content starts with one of
  REPO_DIRS followed by a slash and contains only path characters (letters, digits,
  `_`, `.`, `/`, `-`), or equals one of ROOT_FILES. It must exist on disk. Spans with
  wildcards, placeholders, or spaces (`services/*/tests`, `<svc>`) are not paths, and
  bare names such as `inventory.md` (a specification not written yet) are not either.
- Reference line: a line indented exactly two spaces, with no bullet, reading
  `Enforced in: ...` or `Pinned by: ...`. Its value is one or more
  `` `path` (`symbol`, `symbol`) `` groups separated by `; `, all on that one line.
  `Pinned by` may instead start with `none`. A reference written as a nested bullet
  (`  - Enforced in:`) is the retired format and is flagged.
- Enforced in symbol: it must appear literally in that file, not as part of a longer
  identifier, and for a Python, TypeScript, or JavaScript file not only inside a
  comment (a symbol may legitimately be a string such as an error detail, so string
  contents count).
- Pinned by test: it must be a test that EXISTS in that file, not a name that survives
  in a comment, a string, or a longer title (issue #1158). In a Python file it is a
  function or class pytest can collect: defined at module level, or a method of a
  module-level class. In a TypeScript or JavaScript file it is the complete title
  (the first string argument) of an `it(...)`, `test(...)`, or `describe(...)` call
  that is neither commented out nor `.skip` or `.todo`. In any other file type it is
  a literal match, as for Enforced in.
- Rule: a list item that starts `- **<ID>.**` where <ID> is hyphen-separated words.
  The identifier must match `<AREA>-<TOPIC>-<N>` (RULE_ID), be unique within the
  document, and the item must carry exactly one Enforced in line and one Pinned by line,
  unless its text says it is withdrawn.
- Table citation: every rule identifier written in a table row of the State model, API
  surface, Events, Internal API, or Errors section (CITING_SECTIONS) must be defined by
  a rule in the same document.
- Gaps: the Known limits and gaps section holds the GAP_LISTS subsections. Every `#`
  reference in it is an issue number (`#` and digits, no leading zero). Every bullet
  under Open defects cites at least one issue. The rule identifiers under Rules with no
  test are exactly the rules marked `Pinned by: none`.
- Link: a markdown link `[text](target)` that is not http, https, mailto, or a bare
  anchor. Its target, without any `#anchor`, must exist relative to the document.
- Section: every `## ` heading of TEMPLATE.md appears verbatim in the specification.

Every regular expression here is linear: no quantified group whose body can match the
same text in more than one way. test_patterns_are_linear_on_adversarial_input holds
them to that.
"""

from __future__ import annotations

import ast
import io
import re
import time
import tokenize
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SPECS_DIR = REPO_ROOT / "docs" / "specs"
NOT_SPECIFICATIONS = {"README.md", "TEMPLATE.md"}

REPO_DIRS = (
    "services",
    "frontend",
    "tests",
    "docs",
    "drivers",
    "seedtools",
    "infra",
    "scripts",
    ".github",
)
ROOT_FILES = {
    "README.md",
    "FEATURES.md",
    "PLANNED_FEATURES.md",
    "CHANGELOG.md",
    "FRESH_SETUP.md",
    "SECURITY.md",
    "Makefile",
    "pyproject.toml",
    "uv.lock",
    "docker-compose.yml",
    "docker-compose.override.yml",
    ".env.example",
}

# Section-name fragments whose tables cite rules; matched against `## ` headings.
CITING_SECTIONS = ("State model", "API surface", "Events", "Internal API", "Errors")
GAPS_SECTION = "Known limits and gaps"
GAP_LISTS = ("### Open defects", "### Limits by decision", "### Rules with no test")
OPEN_DEFECTS, _, NO_TEST = GAP_LISTS

_FENCE = re.compile(r"^```.*?^```", re.MULTILINE | re.DOTALL)
_CODE_SPAN = re.compile(r"`([^`\n]+)`")
_PATH_SHAPE = re.compile(r"^(?:" + "|".join(re.escape(d) for d in REPO_DIRS) + r")/[\w./-]*$")
# A rule's text and its "Enforced in" line each end in a backslash, the markdown hard line
# break, so the rule, its enforcement, and its tests render on three separate lines.
_REFERENCE_LINE = re.compile(r"^  (Enforced in|Pinned by): (.*?)(?: \\)?$")
_HARD_BREAK = " \\"
_BULLETED_REFERENCE = re.compile(r"^\s*- (?:Enforced in|Pinned by):")
# One group: a code span, one space, then parenthesized code spans each followed by an
# optional ", ". Every repetition starts at a backtick and its body excludes backticks,
# so no input can be split two ways.
_REFERENCE_GROUP = re.compile(r"`([^`]+)` \(((?:`[^`]+`(?:, )?)+)\)")
_RULE_START = re.compile(r"^- \*\*([A-Za-z0-9]+(?:-[A-Za-z0-9]+)+)\.\*\*")
RULE_ID = re.compile(r"^[A-Z]{2,6}-[A-Z][A-Z0-9]*-[1-9][0-9]*$")
_RULE_ID_TOKEN = re.compile(r"\b[A-Z]{2,6}-[A-Z][A-Z0-9]*-[0-9]+\b")
_ISSUE_TOKEN = re.compile(r"(?<![\w&/#])#(?!#)(\w*)")
_ISSUE_NUMBER = re.compile(r"^[1-9][0-9]*$")
# Link text excludes "[" so a run of "[" cannot make every start scan to the end.
_LINK = re.compile(r"\[[^\[\]]*\]\(([^)\s]+)\)")
_HEADING2 = re.compile(r"^## .+$", re.MULTILINE)
# The opening of a vitest call up to its title: `it(`, `test.each([...])(`,
# `describe.skip(`. Anchored at the start of a line so a commented-out call does not
# match; group 1 holds the modifiers. The optional middle group is the argument list
# of a modifier such as `.each(...)`, with one level of nested parentheses.
_JS_TEST_CALL = re.compile(
    r"^[ \t]*(?:it|test|describe)((?:\.\w+)*)(?:\((?:[^()]|\([^()]*\))*\))?\(\s*(?=[\"'`])",
    re.MULTILINE,
)
_JS_SUFFIXES = {".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"}
_JS_NOT_RUN = {"skip", "todo"}

PATTERNS = (
    _FENCE,
    _CODE_SPAN,
    _PATH_SHAPE,
    _REFERENCE_LINE,
    _BULLETED_REFERENCE,
    _REFERENCE_GROUP,
    _RULE_START,
    RULE_ID,
    _RULE_ID_TOKEN,
    _ISSUE_TOKEN,
    _ISSUE_NUMBER,
    _LINK,
    _HEADING2,
    _JS_TEST_CALL,
)


def _strip_fences(text: str) -> str:
    return _FENCE.sub("", text)


def is_repo_path(token: str) -> bool:
    """Whether a code span's content is a repository path under the rule above."""
    return token in ROOT_FILES or bool(_PATH_SHAPE.match(token))


def _symbol_pattern(symbol: str) -> re.Pattern[str]:
    left = r"(?<!\w)" if re.match(r"\w", symbol[0]) else ""
    right = r"(?!\w)" if re.match(r"\w", symbol[-1]) else ""
    return re.compile(left + re.escape(symbol) + right)


def python_without_comments(source: str) -> str:
    """Python source with every comment removed and all other text in place."""
    tokens = tokenize.generate_tokens(io.StringIO(source).readline)
    return tokenize.untokenize(t for t in tokens if t.type != tokenize.COMMENT)


def python_collectable_names(source: str) -> set[str]:
    """Functions and classes pytest can collect from a module: those defined at
    module level, and the methods of a module-level class. A name in a comment, a
    string, a call, or a function nested inside another is not one."""
    names: set[str] = set()
    for node in ast.parse(source).body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        if isinstance(node, ast.ClassDef):
            names |= {
                member.name
                for member in node.body
                if isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef))
            }
    return names


def _js_string_end(source: str, start: int) -> int:
    """Index just past the string literal that opens at `start`."""
    quote, i = source[start], start + 1
    while i < len(source) and source[i] != quote:
        i += 2 if source[i] == "\\" else 1
    return min(i + 1, len(source))


def js_without_comments(source: str) -> str:
    """TypeScript or JavaScript source with `//` and `/* */` comments blanked out,
    line structure kept. String and template literals are skipped whole, so a `//`
    in a URL or a `/*` in a glob is not a comment."""
    out: list[str] = []
    i = 0
    while i < len(source):
        ch, pair = source[i], source[i : i + 2]
        if ch in "\"'`":
            end = _js_string_end(source, i)
            out.append(source[i:end])
            i = end
        elif pair == "//":
            end = source.find("\n", i)
            i = len(source) if end == -1 else end
        elif pair == "/*":
            end = source.find("*/", i + 2)
            end = len(source) if end == -1 else end + 2
            out.append("".join(c if c == "\n" else " " for c in source[i:end]))
            i = end
        else:
            out.append(ch)
            i += 1
    return "".join(out)


def js_test_titles(source: str) -> set[str]:
    """Complete titles of the `it`, `test`, and `describe` calls a vitest file runs:
    not commented out, and not `.skip` or `.todo`."""
    code = js_without_comments(source)
    titles: set[str] = set()
    for match in _JS_TEST_CALL.finditer(code):
        if _JS_NOT_RUN & set(match.group(1).split(".")):
            continue
        end = _js_string_end(code, match.end())
        titles.add(re.sub(r"\\(.)", r"\1", code[match.end() + 1 : end - 1]))
    return titles


class _Source:
    """One referenced file, read once per document: what an Enforced in symbol is
    searched in, and which tests it holds."""

    def __init__(self, path: Path):
        content = path.read_text(encoding="utf-8")
        self.tests: set[str] | None = None
        self.searchable = content
        if path.suffix == ".py":
            self.searchable = python_without_comments(content)
            self.tests = python_collectable_names(content)
        elif path.suffix in _JS_SUFFIXES:
            self.searchable = js_without_comments(content)
            self.tests = js_test_titles(content)

    def problem(self, kind: str, symbol: str, path_token: str) -> str | None:
        if kind == "Pinned by" and self.tests is not None:
            if symbol in self.tests:
                return None
            return f"Pinned by: `{symbol}` is not a test in {path_token}"
        if _symbol_pattern(symbol).search(self.searchable):
            return None
        return f"{kind}: `{symbol}` does not appear in {path_token}"


def _sections(text: str, level: str) -> dict[str, str]:
    """Heading line to body, for every heading of exactly this level ("##" or "###")."""
    out: dict[str, str] = {}
    heading, body = None, []
    for line in text.splitlines():
        hashes = len(line) - len(line.lstrip("#"))
        is_any_heading = 0 < hashes and line[hashes : hashes + 1] == " "
        is_heading = is_any_heading and hashes == len(level)
        closes = is_any_heading and hashes <= len(level)
        if is_heading or (heading is not None and closes):
            if heading is not None:
                out[heading] = "\n".join(body)
            heading, body = (line, []) if is_heading else (None, [])
        elif heading is not None:
            body.append(line)
    if heading is not None:
        out[heading] = "\n".join(body)
    return out


def check_paths(text: str, repo_root: Path) -> list[str]:
    problems = []
    for token in sorted(set(_CODE_SPAN.findall(_strip_fences(text)))):
        if is_repo_path(token) and not (repo_root / token).exists():
            problems.append(f"path does not exist: {token}")
    return problems


def check_links(text: str, doc_path: Path) -> list[str]:
    problems = []
    for target in _LINK.findall(_strip_fences(text)):
        if target.startswith(("http://", "https://", "mailto:", "#")):
            continue
        relative = target.split("#", 1)[0]
        if not relative:
            continue
        if not (doc_path.parent / relative).exists():
            problems.append(f"link does not resolve: {target}")
    return problems


def check_references(text: str, repo_root: Path) -> list[str]:
    problems = []
    sources: dict[Path, _Source] = {}
    previous = ""
    for line in _strip_fences(text).splitlines():
        before, previous = previous, line
        if line.startswith("  Enforced in: ") and not (
            line.endswith(_HARD_BREAK) and before.endswith(_HARD_BREAK)
        ):
            problems.append(
                "the rule text and its Enforced in line must each end with a backslash "
                f"line break: {line.strip()}"
            )
        if _BULLETED_REFERENCE.match(line):
            problems.append(f"reference line must not be a bullet: {line.strip()}")
            continue
        match = _REFERENCE_LINE.match(line)
        if not match:
            continue
        kind, value = match.groups()
        if kind == "Pinned by" and value.startswith("none"):
            continue
        groups = list(_REFERENCE_GROUP.finditer(value))
        leftover = _REFERENCE_GROUP.sub("", value).replace(";", "").strip()
        if not groups or leftover:
            problems.append(f"{kind} line is not `path` (`symbol`) groups: {value}")
            continue
        for group in groups:
            path_token, symbols = group.group(1), _CODE_SPAN.findall(group.group(2))
            source = repo_root / path_token
            if not is_repo_path(path_token) or not source.is_file():
                problems.append(f"{kind} names a file that does not exist: {path_token}")
                continue
            if source not in sources:
                sources[source] = _Source(source)
            for symbol in symbols:
                problem = sources[source].problem(kind, symbol, path_token)
                if problem:
                    problems.append(problem)
    return problems


def _rule_items(text: str) -> list[tuple[str, str]]:
    """(identifier, full item text) for every rule list item."""
    items: list[tuple[str, str]] = []
    current: list[str] | None = None
    current_id = ""
    for line in _strip_fences(text).splitlines():
        start = _RULE_START.match(line)
        if start:
            if current is not None:
                items.append((current_id, "\n".join(current)))
            current_id, current = start.group(1), [line]
        elif current is not None and (not line.strip() or line.startswith((" ", "\t"))):
            current.append(line)
        elif current is not None:
            items.append((current_id, "\n".join(current)))
            current = None
    if current is not None:
        items.append((current_id, "\n".join(current)))
    return items


def _reference_kinds(item: str) -> list[tuple[str, str]]:
    return [m.groups() for m in map(_REFERENCE_LINE.match, item.splitlines()) if m]


def check_rules(text: str) -> list[str]:
    problems = []
    seen: set[str] = set()
    for rule_id, item in _rule_items(text):
        if not RULE_ID.match(rule_id):
            problems.append(f"rule identifier is not <AREA>-<TOPIC>-<N>: {rule_id}")
        if rule_id in seen:
            problems.append(f"duplicate rule identifier: {rule_id}")
        seen.add(rule_id)
        if "withdrawn" in item.lower():
            continue
        kinds = [kind for kind, _ in _reference_kinds(item)]
        for kind in ("Enforced in", "Pinned by"):
            if kinds.count(kind) != 1:
                problems.append(f"{rule_id} needs exactly one '{kind}' line")
    return problems


def check_table_citations(text: str) -> list[str]:
    stripped = _strip_fences(text)
    defined = {rule_id for rule_id, _ in _rule_items(stripped)}
    problems = []
    for heading, body in _sections(stripped, "##").items():
        if not any(name in heading for name in CITING_SECTIONS):
            continue
        for line in body.splitlines():
            if not line.startswith("|"):
                continue
            for rule_id in _RULE_ID_TOKEN.findall(line):
                if rule_id not in defined:
                    problems.append(f"{heading[3:]} table cites an undefined rule: {rule_id}")
    return sorted(set(problems))


def check_gaps(text: str) -> list[str]:
    stripped = _strip_fences(text)
    gaps = next((b for h, b in _sections(stripped, "##").items() if GAPS_SECTION in h), None)
    if gaps is None:
        return []  # check_sections reports the missing section
    problems = []
    for token in _ISSUE_TOKEN.findall(gaps):
        if not _ISSUE_NUMBER.match(token):
            problems.append(f"gaps section has a malformed issue reference: #{token}")
    lists = _sections(gaps, "###")
    for name in GAP_LISTS:
        if name not in lists:
            problems.append(f"gaps section is missing the list: {name}")
    for line in lists.get(OPEN_DEFECTS, "").splitlines():
        if line.startswith("- ") and not any(map(_ISSUE_NUMBER.match, _ISSUE_TOKEN.findall(line))):
            problems.append(f"open defect without an issue: {line[2:60]}")
    if NO_TEST in lists:
        listed = set(_RULE_ID_TOKEN.findall(lists[NO_TEST]))
        unpinned = {
            rule_id
            for rule_id, item in _rule_items(stripped)
            if any(k == "Pinned by" and v.startswith("none") for k, v in _reference_kinds(item))
        }
        for rule_id in sorted(unpinned - listed):
            problems.append(f"{rule_id} is Pinned by: none but not under {NO_TEST[4:]}")
        for rule_id in sorted(listed - unpinned):
            problems.append(f"{rule_id} is under {NO_TEST[4:]} but has a test")
    return problems


def template_headings(template_path: Path) -> list[str]:
    return _HEADING2.findall(_strip_fences(template_path.read_text(encoding="utf-8")))


def check_sections(text: str, headings: list[str]) -> list[str]:
    present = set(_HEADING2.findall(_strip_fences(text)))
    return [f"missing template section: {h}" for h in headings if h not in present]


def check_document(
    doc_path: Path, repo_root: Path, headings: list[str], *, is_spec: bool = True
) -> list[str]:
    text = doc_path.read_text(encoding="utf-8")
    problems = check_paths(text, repo_root) + check_links(text, doc_path)
    if is_spec:
        problems += check_references(text, repo_root)
        problems += check_rules(text)
        problems += check_table_citations(text)
        problems += check_gaps(text)
        problems += check_sections(text, headings)
    return problems


def _specifications() -> list[Path]:
    return sorted(p for p in SPECS_DIR.glob("*.md") if p.name not in NOT_SPECIFICATIONS)


# The real documents.


def test_there_is_at_least_one_specification():
    assert _specifications(), "docs/specs/ holds no specification to check"


@pytest.mark.parametrize("doc", _specifications(), ids=lambda p: p.name)
def test_specification_references_are_current(doc: Path):
    headings = template_headings(SPECS_DIR / "TEMPLATE.md")
    problems = check_document(doc, REPO_ROOT, headings)
    assert not problems, f"{doc.name}:\n" + "\n".join(problems)


def test_specs_index_paths_and_links_resolve():
    readme = SPECS_DIR / "README.md"
    problems = check_document(readme, REPO_ROOT, [], is_spec=False)
    assert not problems, "README.md:\n" + "\n".join(problems)


def test_template_names_every_gap_list_and_citing_section():
    template = (SPECS_DIR / "TEMPLATE.md").read_text(encoding="utf-8")
    headings = template_headings(SPECS_DIR / "TEMPLATE.md")
    for name in GAP_LISTS:
        assert name in template.splitlines(), name
    for name in (*CITING_SECTIONS, GAPS_SECTION):
        assert any(name in h for h in headings), name


# The checker itself, on a throwaway repository, one failure mode per test.

TEMPLATE = "# X\n\n## 1. Purpose\n\ntext\n\n## 2. Rules\n\ntext\n"
GOOD_RULE = (
    "- **XXX-A-1.** A rule. \\\n"
    "  Enforced in: `services/svc/app.py` (`real_symbol`) \\\n"
    "  Pinned by: `tests/test_svc.py` (`test_real`)\n"
)


@pytest.fixture
def fake_repo(tmp_path: Path) -> Path:
    (tmp_path / "services" / "svc").mkdir(parents=True)
    (tmp_path / "services" / "svc" / "app.py").write_text("def real_symbol():\n    pass\n")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_svc.py").write_text("def test_real():\n    pass\n")
    (tmp_path / "docs" / "specs").mkdir(parents=True)
    (tmp_path / "docs" / "specs" / "TEMPLATE.md").write_text(TEMPLATE)
    (tmp_path / "docs" / "OTHER.md").write_text("other\n")
    return tmp_path


def _check(repo: Path, body: str) -> list[str]:
    doc = repo / "docs" / "specs" / "area.md"
    doc.write_text(body)
    headings = template_headings(repo / "docs" / "specs" / "TEMPLATE.md")
    return check_document(doc, repo, headings)


def _spec(rules: str = GOOD_RULE, extra: str = "", tail: str = "") -> str:
    return f"# Area\n\n## 1. Purpose\n\n{extra}\n\n## 2. Rules\n\n{rules}\n{tail}"


def _gaps(defects: str = "None.", no_test: str = "None.") -> str:
    return (
        "## 13. Known limits and gaps\n\n"
        f"### Open defects\n\n{defects}\n\n"
        "### Limits by decision\n\nNone.\n\n"
        f"### Rules with no test\n\n{no_test}\n"
    )


def test_checker_passes_a_clean_document(fake_repo: Path):
    assert _check(fake_repo, _spec(extra="See [other](../OTHER.md) and `inventory.md`.")) == []


def test_checker_flags_a_missing_path(fake_repo: Path):
    problems = _check(fake_repo, _spec(extra="Code lives in `services/svc/gone.py`."))
    assert problems == ["path does not exist: services/svc/gone.py"]


def test_checker_ignores_wildcards_and_unwritten_spec_names(fake_repo: Path):
    extra = "`services/*/tests/*_live_pg.py`, `services/<svc>/app`, `topology.md`"
    assert _check(fake_repo, _spec(extra=extra)) == []


def test_checker_flags_a_symbol_missing_from_its_file(fake_repo: Path):
    rule = GOOD_RULE.replace("`real_symbol`", "`renamed_symbol`")
    assert _check(fake_repo, _spec(rules=rule)) == [
        "Enforced in: `renamed_symbol` does not appear in services/svc/app.py"
    ]


def test_checker_flags_a_test_name_that_is_only_a_prefix(fake_repo: Path):
    (fake_repo / "tests" / "test_svc.py").write_text("def test_real_but_longer():\n    pass\n")
    assert _check(fake_repo, _spec()) == [
        "Pinned by: `test_real` is not a test in tests/test_svc.py"
    ]


def test_checker_flags_a_reference_to_a_missing_file(fake_repo: Path):
    rule = GOOD_RULE.replace("tests/test_svc.py", "tests/test_gone.py")
    problems = _check(fake_repo, _spec(rules=rule))
    assert "Pinned by names a file that does not exist: tests/test_gone.py" in problems


def test_checker_flags_an_unparseable_reference_line(fake_repo: Path):
    rule = GOOD_RULE.replace("`tests/test_svc.py` (`test_real`)", "see the tests")
    problems = _check(fake_repo, _spec(rules=rule))
    assert problems == ["Pinned by line is not `path` (`symbol`) groups: see the tests"]


def test_checker_accepts_several_groups_on_one_line(fake_repo: Path):
    rule = GOOD_RULE.replace(
        "(`real_symbol`)", "(`real_symbol`); `tests/test_svc.py` (`test_real`, `test_real`)"
    )
    assert _check(fake_repo, _spec(rules=rule)) == []


def test_checker_flags_a_rule_without_hard_line_breaks(fake_repo: Path):
    # Without the two backslashes GitHub renders the rule, its enforcement, and its tests
    # as one run-on paragraph.
    for rule in (
        GOOD_RULE.replace("A rule. \\\n", "A rule.\n"),
        GOOD_RULE.replace("(`real_symbol`) \\\n", "(`real_symbol`)\n"),
    ):
        assert rule != GOOD_RULE
        problems = _check(fake_repo, _spec(rules=rule))
        assert any("backslash line break" in p for p in problems), problems


def test_checker_flags_the_retired_bulleted_reference_format(fake_repo: Path):
    rule = GOOD_RULE.replace("  Enforced in:", "  - Enforced in:")
    problems = _check(fake_repo, _spec(rules=rule))
    assert "XXX-A-1 needs exactly one 'Enforced in' line" in problems
    assert any(p.startswith("reference line must not be a bullet") for p in problems)


def test_checker_accepts_pinned_by_none(fake_repo: Path):
    rule = GOOD_RULE.replace("`tests/test_svc.py` (`test_real`)", "none (issue #12)")
    assert _check(fake_repo, _spec(rules=rule)) == []


def test_checker_flags_a_duplicate_rule_identifier(fake_repo: Path):
    assert _check(fake_repo, _spec(rules=GOOD_RULE + GOOD_RULE)) == [
        "duplicate rule identifier: XXX-A-1"
    ]


@pytest.mark.parametrize("bad_id", ["XXX-a-1", "XXX-A-0", "XXX-A-01", "X-A-1", "XXX-1-1"])
def test_checker_flags_a_malformed_rule_identifier(fake_repo: Path, bad_id: str):
    rule = GOOD_RULE.replace("XXX-A-1", bad_id)
    assert _check(fake_repo, _spec(rules=rule)) == [
        f"rule identifier is not <AREA>-<TOPIC>-<N>: {bad_id}"
    ]


def test_checker_flags_a_rule_without_enforced_in(fake_repo: Path):
    rule = "\n".join(line for line in GOOD_RULE.splitlines() if "Enforced" not in line)
    assert _check(fake_repo, _spec(rules=rule + "\n")) == [
        "XXX-A-1 needs exactly one 'Enforced in' line"
    ]


def test_checker_flags_a_rule_without_pinned_by(fake_repo: Path):
    rule = "\n".join(line for line in GOOD_RULE.splitlines() if "Pinned" not in line)
    assert _check(fake_repo, _spec(rules=rule + "\n")) == [
        "XXX-A-1 needs exactly one 'Pinned by' line"
    ]


def test_checker_does_not_borrow_references_from_the_next_item(fake_repo: Path):
    rule = "- **XXX-A-2.** Bare rule.\n\n**Out of scope.** None.\n\n" + GOOD_RULE
    problems = _check(fake_repo, _spec(rules=rule))
    assert "XXX-A-2 needs exactly one 'Enforced in' line" in problems


def test_checker_exempts_a_withdrawn_rule(fake_repo: Path):
    rule = GOOD_RULE + "- **XXX-A-2.** Withdrawn: replaced by XXX-A-1.\n"
    assert _check(fake_repo, _spec(rules=rule)) == []


@pytest.mark.parametrize("section", ["4. State model", "5. API surface", "9. Errors"])
def test_checker_flags_a_table_citing_an_undefined_rule(fake_repo: Path, section: str):
    table = f"## {section}\n\n| A | Rule |\n|---|---|\n| x | XXX-A-1, XXX-A-9 |\n"
    assert _check(fake_repo, _spec(tail=table)) == [
        f"{section} table cites an undefined rule: XXX-A-9"
    ]


def test_checker_ignores_rule_ids_outside_citing_tables(fake_repo: Path):
    prose = "## 8. Features\n\n| A | Rule |\n|---|---|\n| x | XXX-A-9 |\n\nSee XXX-B-3.\n"
    assert _check(fake_repo, _spec(tail=prose)) == []


def test_checker_accepts_well_formed_gaps(fake_repo: Path):
    rule = GOOD_RULE.replace("`tests/test_svc.py` (`test_real`)", "none (issue #12)")
    tail = _gaps(defects="- #34 (XXX-A-1): wrong.", no_test="- XXX-A-1: no test (#12).")
    assert _check(fake_repo, _spec(rules=rule, tail=tail)) == []


@pytest.mark.parametrize("ref", ["#0", "#012", "#12a", "#abc"])
def test_checker_flags_a_malformed_issue_reference_in_gaps(fake_repo: Path, ref: str):
    tail = _gaps(defects=f"- {ref} and #5 (XXX-A-1): wrong.")
    assert _check(fake_repo, _spec(tail=tail)) == [
        f"gaps section has a malformed issue reference: {ref}"
    ]


def test_checker_flags_an_open_defect_without_an_issue(fake_repo: Path):
    tail = _gaps(defects="- XXX-A-1 is wrong.")
    assert _check(fake_repo, _spec(tail=tail)) == [
        "open defect without an issue: XXX-A-1 is wrong."
    ]


def test_checker_flags_a_missing_gap_list(fake_repo: Path):
    tail = _gaps().replace("### Limits by decision", "### Limits")
    assert _check(fake_repo, _spec(tail=tail)) == [
        "gaps section is missing the list: ### Limits by decision"
    ]


def test_checker_flags_an_unpinned_rule_missing_from_the_no_test_list(fake_repo: Path):
    rule = GOOD_RULE.replace("`tests/test_svc.py` (`test_real`)", "none")
    assert _check(fake_repo, _spec(rules=rule, tail=_gaps())) == [
        "XXX-A-1 is Pinned by: none but not under Rules with no test"
    ]


def test_checker_flags_a_pinned_rule_listed_as_untested(fake_repo: Path):
    tail = _gaps(no_test="- XXX-A-1: no test.")
    assert _check(fake_repo, _spec(tail=tail)) == [
        "XXX-A-1 is under Rules with no test but has a test"
    ]


def test_checker_flags_a_broken_relative_link(fake_repo: Path):
    problems = _check(fake_repo, _spec(extra="See [gone](../GONE.md#anchor)."))
    assert problems == ["link does not resolve: ../GONE.md#anchor"]


def test_checker_ignores_external_and_anchor_links(fake_repo: Path):
    extra = "[a](https://example.com/x) [b](#section) [c](mailto:x@example.com)"
    assert _check(fake_repo, _spec(extra=extra)) == []


def test_checker_flags_a_missing_template_section(fake_repo: Path):
    body = "# Area\n\n## 2. Rules\n\n" + GOOD_RULE
    assert _check(fake_repo, body) == ["missing template section: ## 1. Purpose"]


def test_checker_ignores_code_inside_fences(fake_repo: Path):
    extra = "```\n`services/svc/gone.py` [x](../GONE.md)\n```"
    assert _check(fake_repo, _spec(extra=extra)) == []


# A cited test must exist as a test, and an enforcing symbol must be code (issue
# #1158): one way of leaving the name in the file without the thing it names per case.

NOT_A_PYTHON_TEST = {
    "only in a comment": "# test_real was removed\ndef test_other():\n    pass\n",
    "commented out": "# def test_real():\n#     pass\ndef test_other():\n    pass\n",
    "only in a string": 'REMOVED = "test_real"\n\n\ndef test_other():\n    pass\n',
    "only in a docstring": 'def test_other():\n    """Replaces test_real."""\n',
    "only called": "def test_other():\n    test_real()\n",
    "only imported": "from elsewhere import test_real\n\n\ndef test_other():\n    pass\n",
    "nested in another function": "def helper():\n    def test_real():\n        pass\n",
    "a longer name": "def test_real_but_longer():\n    pass\n",
    "a private twin": "def _test_real():\n    pass\n",
}


@pytest.mark.parametrize("spelling", sorted(NOT_A_PYTHON_TEST))
def test_checker_flags_a_python_test_name_that_is_not_a_test(fake_repo: Path, spelling: str):
    (fake_repo / "tests" / "test_svc.py").write_text(NOT_A_PYTHON_TEST[spelling])
    assert _check(fake_repo, _spec()) == [
        "Pinned by: `test_real` is not a test in tests/test_svc.py"
    ]


@pytest.mark.parametrize(
    "source",
    [
        "def test_real():\n    pass\n",
        "async def test_real():\n    pass\n",
        "class TestSvc:\n    def test_real(self):\n        pass\n",
        "import pytest\n\n\n@pytest.mark.parametrize('x', [1])\ndef test_real(x):\n    pass\n",
        "class test_real:\n    pass\n",
    ],
)
def test_checker_accepts_every_collectable_python_test(fake_repo: Path, source: str):
    (fake_repo / "tests" / "test_svc.py").write_text(source)
    assert _check(fake_repo, _spec()) == []


def test_checker_flags_an_enforcing_symbol_that_is_only_in_a_comment(fake_repo: Path):
    (fake_repo / "services" / "svc" / "app.py").write_text(
        "# real_symbol used to live here\ndef other():\n    pass  # real_symbol\n"
    )
    assert _check(fake_repo, _spec()) == [
        "Enforced in: `real_symbol` does not appear in services/svc/app.py"
    ]


def test_checker_accepts_an_enforcing_symbol_that_is_a_string(fake_repo: Path):
    (fake_repo / "services" / "svc" / "app.py").write_text('DETAIL = "real_symbol"  # the detail\n')
    assert _check(fake_repo, _spec()) == []


_TS_RULE = GOOD_RULE.replace(
    "`tests/test_svc.py` (`test_real`)", "`frontend/src/x.test.tsx` (`shows the row`)"
)


def _check_ts(repo: Path, source: str) -> list[str]:
    (repo / "frontend" / "src").mkdir(parents=True, exist_ok=True)
    (repo / "frontend" / "src" / "x.test.tsx").write_text(source)
    return _check(repo, _spec(rules=_TS_RULE))


@pytest.mark.parametrize(
    "source",
    [
        'it("shows the row", fn);\n',
        "  it('shows the row', fn);\n",
        "test(`shows the row`, fn);\n",
        'describe("shows the row", fn);\n',
        'it(\n  "shows the row",\n  fn,\n);\n',
        'it.each([[1, (2)], [3, 4]])("shows the row", fn);\n',
        'it.only("shows the row", fn);\n',
        'const glob = "src/**/*.ts"; // a glob, not a comment\nit("shows the row", fn);\n',
        'const url = "https://example.com/*";\nit("shows the row", fn);\n/* tail */\n',
    ],
)
def test_checker_accepts_a_frontend_test_title_under_any_call_shape(fake_repo: Path, source: str):
    assert _check_ts(fake_repo, source) == []


def test_checker_matches_a_frontend_title_through_its_escapes(fake_repo: Path):
    rule = _TS_RULE.replace("shows the row", "shows the row's name")
    (fake_repo / "frontend" / "src").mkdir(parents=True)
    (fake_repo / "frontend" / "src" / "x.test.tsx").write_text(
        "it('shows the row\\'s name', fn);\n"
    )
    assert _check(fake_repo, _spec(rules=rule)) == []


NOT_A_FRONTEND_TEST = {
    "only in a line comment": '// shows the row\nit("other", fn);\n',
    "commented out": '// it("shows the row", fn);\nit("other", fn);\n',
    "commented out, indented": '  // it("shows the row", fn);\n',
    "inside a block comment": '/*\nit("shows the row", fn);\n*/\nit("other", fn);\n',
    "inside a one-line block comment": '/* it("shows the row", fn); */\n',
    "skipped": 'it.skip("shows the row", fn);\n',
    "a todo": 'it.todo("shows the row");\n',
    "a skipped block": 'describe.skip("shows the row", fn);\n',
    "a longer title": 'it("shows the row and its actions", fn);\n',
    "a longer title, the cited text at the end": 'it("never shows the row", fn);\n',
    "only a string": 'const title = "shows the row";\nit("other", fn);\n',
    "an expectation, not a title": (
        'it("other", function () {\n  expect(t).toBe("shows the row");\n});\n'
    ),
}


@pytest.mark.parametrize("spelling", sorted(NOT_A_FRONTEND_TEST))
def test_checker_flags_a_frontend_title_that_is_not_a_running_test(fake_repo: Path, spelling: str):
    assert _check_ts(fake_repo, NOT_A_FRONTEND_TEST[spelling]) == [
        "Pinned by: `shows the row` is not a test in frontend/src/x.test.tsx"
    ]


def test_checker_keeps_the_literal_match_for_other_file_types(fake_repo: Path):
    (fake_repo / "tests" / "cases.md").write_text("Case: shows the row\n")
    rule = GOOD_RULE.replace(
        "`tests/test_svc.py` (`test_real`)", "`tests/cases.md` (`shows the row`)"
    )
    assert _check(fake_repo, _spec(rules=rule)) == []


# Every pattern stays linear. Each input is built to make a backtracking pattern
# explode (long runs that almost match, then fail at the end); a linear pattern
# finishes each in milliseconds.

_N = 50_000
ADVERSARIAL = (
    "`a` (`b`" + " " * _N,
    "`a` (" + "`b`, " * _N,
    "`a` (" + "`b`" * _N,
    "`" * _N,
    " ".join(["`x`"] * _N) + " (",
    "- **" + "A-" * _N,
    "- **" + "A" * _N + "-",
    "A-" * _N + "1x",
    "#" * _N + "x",
    "[" * _N + "](",
    "[a](" + "b" * _N,
    "```\n" + "x\n" * _N,
    "  Enforced in: " + "`p` (`s`); " * _N + "?",
    "services/" + "a/" * _N + " ",
    "## " + " " * _N,
    "it(" * _N,
    "it.each(" + "(a)" * _N,
    "it(x\n" * _N,
    "it" + ".a" * _N + "(",
)


@pytest.mark.parametrize("pattern", PATTERNS, ids=lambda p: p.pattern[:40])
def test_patterns_are_linear_on_adversarial_input(pattern: re.Pattern[str]):
    for text in ADVERSARIAL:
        started = time.perf_counter()
        pattern.findall(text)
        pattern.sub("", text)
        elapsed = time.perf_counter() - started
        assert elapsed < 1.0, f"{pattern.pattern!r} took {elapsed:.2f}s on {text[:30]!r}"
