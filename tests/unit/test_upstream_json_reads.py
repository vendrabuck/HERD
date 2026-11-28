"""Reservations reads an upstream JSON body only through the shared helper (issue #1577).

Every body the reservations service reads from another service goes through
`herd_common.upstream_body.read_json_body`, which turns a body that does not parse
(not JSON, not UTF-8, an integer past the digit limit, or nested past the decoder's
recursion limit) or is of the wrong top-level type into ONE exception,
`UpstreamBodyUnusable`, so each site maps one type to its documented failure. A
bare `resp.json()` catches only what its author thought of: before #1577, sites
caught nothing, `ValueError` only (which misses `RecursionError`), or everything.

Pure AST, no imports of the scanned code: the scan reads every `*.py` file under
`services/reservations/app/` and fails on

- any attribute named `json`, called or not (`resp.json()`, `getter = resp.json`);
- a call of `loads` or `load` on the `json` module imported under any name
  (`import json as j` then `j.loads(...)`), or of either imported from it bare or
  under an alias (`from json import loads as parse`).

Limits. The scan reads the source; it does not run it:

- another JSON library (`orjson`, `ujson`, `simplejson`), `json.JSONDecoder`,
  `json.decoder.scanstring`, and pydantic's `model_validate_json` or
  `TypeAdapter.validate_json` are not looked for;
- an attribute reached through `getattr(resp, "json")` or a string built at run
  time is not seen;
- a star import (`from json import *`) binds no name the scan follows;
- only reservations is scanned: the other services' sites move to the helper later
  (the recommended option on #1577), and `herd_common` itself is not scanned.
"""

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCANNED = REPO_ROOT / "services" / "reservations" / "app"
PARSE_FUNCTIONS = {"loads", "load"}


def _violations(source: str) -> list[tuple[int, str]]:
    tree = ast.parse(source)
    module_names: set[str] = set()
    function_names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "json":
                    module_names.add(alias.asname or "json")
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module == "json":
            for alias in node.names:
                if alias.name in PARSE_FUNCTIONS:
                    function_names.add(alias.asname or alias.name)
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            if node.attr == "json":
                found.append((node.lineno, "an attribute named json"))
            elif (
                node.attr in PARSE_FUNCTIONS
                and isinstance(node.value, ast.Name)
                and node.value.id in module_names
            ):
                found.append((node.lineno, f"json.{node.attr}"))
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id in function_names
        ):
            found.append((node.lineno, f"{node.func.id} imported from json"))
    return found


def test_reservations_reads_upstream_json_only_through_the_shared_helper():
    files = sorted(SCANNED.rglob("*.py"))
    assert files, f"nothing to scan under {SCANNED}"
    problems = [
        f"{path.relative_to(REPO_ROOT)}:{line}: {what}"
        for path in files
        for line, what in _violations(path.read_text())
    ]
    assert not problems, (
        "read an upstream answer's body with "
        "herd_common.upstream_body.read_json_body(resp, expect=...), and map "
        "UpstreamBodyUnusable to the site's documented failure (issue #1577):\n"
        + "\n".join(problems)
    )


def test_the_scan_finds_each_form_and_passes_the_helper_call():
    """Each flagged form, under each spelling, and the forms that are not flagged."""
    flagged = {
        "data = resp.json()": 1,
        "getter = resp.json": 1,
        "import json\njson.loads(resp.content)": 1,
        "import json as j\nj.load(fh)": 1,
        "from json import loads\nloads(b)": 1,
        "from json import load as parse\nparse(fh)": 1,
        "x = client.get(u).json()['items']": 1,
    }
    for source, count in flagged.items():
        assert len(_violations(source)) == count, source
    clean = [
        "body = read_json_body(resp, expect=dict)",
        "import json\njson.dumps({})",
        "from json import dumps\ndumps({})",
        "loads(b)",
        "other.loads(b)",
        "from .json import loads\nloads(b)",
        "model.model_dump_json()",
    ]
    for source in clean:
        assert _violations(source) == [], source
