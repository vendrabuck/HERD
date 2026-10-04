"""Drift guard for the specifications under docs/specs/.

No stack, no database: this only reads markdown and source files. A specification
names repository paths, the symbols that enforce each rule, and the tests that pin
it; a rename, a deleted test, or a moved file would otherwise leave it pointing at
nothing without anyone noticing. Every specification (every docs/specs/*.md except
README.md and TEMPLATE.md) must pass all five checks below; README.md gets the path
and link checks only, since it is the index, not a specification.

What counts as what (the rules docs/specs/README.md promises):

- Repository path: an inline code span whose whole content starts with one of
  REPO_DIRS followed by a slash and contains only path characters (letters, digits,
  `_`, `.`, `/`, `-`), or equals one of ROOT_FILES. It must exist on disk. Spans with
  wildcards, placeholders, or spaces (`services/*/tests`, `<svc>`) are not paths, and
  bare names such as `inventory.md` (a specification not written yet) are not either.
- Reference line: a line `- Enforced in: ...` or `- Pinned by: ...`. Its value is one
  or more `` `path` (`symbol`, `symbol`) `` groups separated by `;`. Each symbol must
  appear literally in that file, not as part of a longer identifier. `Pinned by` may
  instead start with `none`.
- Rule: a list item that starts `- **<AREA>-<TOPIC>-<N>.**`. Identifiers are unique
  within a document, and each rule carries one Enforced in line and one Pinned by line
  in its own item, unless its text says it is withdrawn.
- Link: a markdown link `[text](target)` that is not http, https, mailto, or a bare
  anchor. Its target, without any `#anchor`, must exist relative to the document.
- Section: every `## ` heading of TEMPLATE.md appears verbatim in the specification.
"""

from __future__ import annotations

import re
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

_FENCE = re.compile(r"^```.*?^```", re.MULTILINE | re.DOTALL)
_CODE_SPAN = re.compile(r"`([^`\n]+)`")
_PATH_SHAPE = re.compile(r"^(?:" + "|".join(re.escape(d) for d in REPO_DIRS) + r")/[\w./-]*$")
_REFERENCE_LINE = re.compile(r"^\s*- (Enforced in|Pinned by):\s*(.*)$")
_REFERENCE_GROUP = re.compile(r"`([^`]+)`\s*\(((?:\s*`[^`]+`\s*,?)+)\)")
_RULE_START = re.compile(r"^- \*\*([A-Z][A-Z0-9]*-[A-Z][A-Z0-9]*-\d+)\.\*\*")
_LINK = re.compile(r"\[[^\]]*\]\(([^)\s]+)\)")
_HEADING2 = re.compile(r"^## .+$", re.MULTILINE)


def _strip_fences(text: str) -> str:
    return _FENCE.sub("", text)


def is_repo_path(token: str) -> bool:
    """Whether a code span's content is a repository path under the rule above."""
    return token in ROOT_FILES or bool(_PATH_SHAPE.match(token))


def _symbol_pattern(symbol: str) -> re.Pattern[str]:
    left = r"(?<!\w)" if re.match(r"\w", symbol[0]) else ""
    right = r"(?!\w)" if re.match(r"\w", symbol[-1]) else ""
    return re.compile(left + re.escape(symbol) + right)


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
    for line in _strip_fences(text).splitlines():
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
            content = source.read_text(encoding="utf-8")
            for symbol in symbols:
                if not _symbol_pattern(symbol).search(content):
                    problems.append(f"{kind}: `{symbol}` does not appear in {path_token}")
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


def check_rules(text: str) -> list[str]:
    problems = []
    seen: set[str] = set()
    for rule_id, item in _rule_items(text):
        if rule_id in seen:
            problems.append(f"duplicate rule identifier: {rule_id}")
        seen.add(rule_id)
        if "withdrawn" in item.lower():
            continue
        kinds = [m.group(1) for m in map(_REFERENCE_LINE.match, item.splitlines()) if m]
        for kind in ("Enforced in", "Pinned by"):
            if kinds.count(kind) != 1:
                problems.append(f"{rule_id} needs exactly one '{kind}' line")
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


# The checker itself, on a throwaway repository, one failure mode per test.

TEMPLATE = "# X\n\n## 1. Purpose\n\ntext\n\n## 2. Rules\n\ntext\n"
GOOD_RULE = (
    "- **XXX-A-1.** A rule.\n"
    "  - Enforced in: `services/svc/app.py` (`real_symbol`)\n"
    "  - Pinned by: `tests/test_svc.py` (`test_real`)\n"
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


def _spec(rules: str = GOOD_RULE, extra: str = "") -> str:
    return f"# Area\n\n## 1. Purpose\n\n{extra}\n\n## 2. Rules\n\n{rules}"


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
        "Pinned by: `test_real` does not appear in tests/test_svc.py"
    ]


def test_checker_flags_a_reference_to_a_missing_file(fake_repo: Path):
    rule = GOOD_RULE.replace("tests/test_svc.py", "tests/test_gone.py")
    problems = _check(fake_repo, _spec(rules=rule))
    assert "Pinned by names a file that does not exist: tests/test_gone.py" in problems


def test_checker_flags_an_unparseable_reference_line(fake_repo: Path):
    rule = GOOD_RULE.replace("`tests/test_svc.py` (`test_real`)", "see the tests")
    problems = _check(fake_repo, _spec(rules=rule))
    assert problems == ["Pinned by line is not `path` (`symbol`) groups: see the tests"]


def test_checker_accepts_pinned_by_none(fake_repo: Path):
    rule = GOOD_RULE.replace("`tests/test_svc.py` (`test_real`)", "none (listed in section 9)")
    assert _check(fake_repo, _spec(rules=rule)) == []


def test_checker_flags_a_duplicate_rule_identifier(fake_repo: Path):
    assert _check(fake_repo, _spec(rules=GOOD_RULE + GOOD_RULE)) == [
        "duplicate rule identifier: XXX-A-1"
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
    rule = "- **XXX-A-2.** Bare rule.\n\n**Errors.** None.\n\n" + GOOD_RULE
    problems = _check(fake_repo, _spec(rules=rule))
    assert "XXX-A-2 needs exactly one 'Enforced in' line" in problems


def test_checker_exempts_a_withdrawn_rule(fake_repo: Path):
    rule = GOOD_RULE + "- **XXX-A-2.** Withdrawn: replaced by XXX-A-1.\n"
    assert _check(fake_repo, _spec(rules=rule)) == []


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
