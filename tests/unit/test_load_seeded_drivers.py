"""The load test downloads only driver packages the default seed uploads (#1416).

`tests/load/locustfile.py`'s `InventoryBrowser.download_driver` picks a driver from
`SEEDED_DRIVER_NAMES`, because a package another suite left listed may have no
stored archive, and its download answers 500, which fails the run. This guard reads
both files as syntax trees (nothing is imported, so locust need not be installed)
and checks that every name in that set is one `seedtools/cli.py` passes, as a string
literal, as the name argument of a `get_or_create_driver` call. Limits: a name built
at run time, or passed by keyword, is not seen, and the guard does not check that
the seed run uploaded anything.
"""

import ast
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
LOCUSTFILE = REPO / "tests" / "load" / "locustfile.py"
SEED_CLI = REPO / "seedtools" / "cli.py"


def _seeded_driver_names_in_locustfile() -> set[str]:
    tree = ast.parse(LOCUSTFILE.read_text())
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "SEEDED_DRIVER_NAMES" for t in node.targets
        ):
            return {
                elt.value
                for elt in ast.walk(node.value)
                if isinstance(elt, ast.Constant) and isinstance(elt.value, str)
            }
    raise AssertionError("tests/load/locustfile.py defines no SEEDED_DRIVER_NAMES")


def _names_the_seed_uploads() -> set[str]:
    names = set()
    for node in ast.walk(ast.parse(SEED_CLI.read_text())):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "get_or_create_driver"
            and len(node.args) >= 2
            and isinstance(node.args[1], ast.Constant)
            and isinstance(node.args[1].value, str)
        ):
            names.add(node.args[1].value)
    return names


def test_the_load_test_downloads_only_drivers_the_default_seed_uploads():
    picked = _seeded_driver_names_in_locustfile()
    seeded = _names_the_seed_uploads()
    assert picked, "SEEDED_DRIVER_NAMES is empty"
    assert seeded, "seedtools/cli.py uploads no driver this guard can read"
    assert picked <= seeded, f"not uploaded by the default seed: {sorted(picked - seeded)}"
