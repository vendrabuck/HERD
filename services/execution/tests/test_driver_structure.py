"""The one structural check of a driver package, by parsing and never importing (issue #1114).

driver_structure_errors is shared by the load path (validate_driver) and the package
validator. These tests pin the rule it states: a plain top-level `class Driver`
statement is required; a required method counts when the class body binds it, when it
is assigned onto Driver at module level, or when a top-level base class in driver.py
provides it; and where a method could come from code a parser cannot read, the check
passes rather than refuse a package the old import-based check accepted.
"""

from pathlib import Path

import pytest
from app.services.driver_structure import (
    DRIVER_NOT_A_TOP_LEVEL_CLASS,
    MISSING_DRIVER_CLASS,
    MISSING_DRIVER_PY,
    DriverSourceError,
    driver_structure_errors,
)

L1 = "Layer 1 Switch"

L1_METHODS = """
    def login(self):
        return {"success": True}

    def logout(self):
        return {"success": True}

    def connect_ports(self, port_a, port_b):
        return {"success": True}

    def disconnect_ports(self, port_a, port_b):
        return {"success": True}

    def status(self):
        return {"reachable": True}
"""


def _check(tmp_path: Path, source: str, connection_type: str = L1) -> list[str]:
    (tmp_path / "driver.py").write_text(source, encoding="utf-8")
    return driver_structure_errors(tmp_path, connection_type)


def _missing(*methods: str) -> list[str]:
    return [f"Driver class is missing required method: {m}" for m in methods]


def test_plain_top_level_class_passes(tmp_path):
    assert _check(tmp_path, "class Driver:\n" + L1_METHODS) == []


def test_async_methods_count(tmp_path):
    source = "class Driver:\n" + L1_METHODS.replace("    def status", "    async def status")
    assert _check(tmp_path, source) == []


def test_missing_methods_are_listed_in_required_order(tmp_path):
    source = "class Driver:\n    def login(self):\n        pass\n"
    assert _check(tmp_path, source) == _missing(
        "logout", "connect_ports", "disconnect_ports", "status"
    )


def test_method_defined_only_inside_another_method_does_not_count(tmp_path):
    source = "class Driver:\n" + L1_METHODS.replace(
        '    def status(self):\n        return {"reachable": True}\n',
        "    def helper(self):\n        def status():\n            return {}\n",
    )
    assert _check(tmp_path, source) == _missing("status")


def test_missing_driver_py(tmp_path):
    assert driver_structure_errors(tmp_path, L1) == [MISSING_DRIVER_PY]


def test_unknown_connection_type_is_reported_first(tmp_path):
    assert driver_structure_errors(tmp_path, "Unknown Type") == [
        "Unknown connection type: Unknown Type"
    ]


def test_no_driver_at_all(tmp_path):
    assert _check(tmp_path, "class NotADriver:\n    pass\n") == [MISSING_DRIVER_CLASS]
    assert MISSING_DRIVER_CLASS == "driver.py must define a class named Driver"


def test_parse_failure_raises_with_the_cause_chained(tmp_path):
    with pytest.raises(DriverSourceError) as exc:
        _check(tmp_path, "class Driver(:\n    pass\n")
    assert isinstance(exc.value.__cause__, SyntaxError)
    # The bare file name, never the extraction directory.
    assert str(tmp_path) not in str(exc.value.__cause__)


def test_null_bytes_are_a_parse_failure(tmp_path):
    (tmp_path / "driver.py").write_bytes(b"class Driver:\n    pass\x00\n")
    with pytest.raises(DriverSourceError):
        driver_structure_errors(tmp_path, L1)


@pytest.mark.parametrize(
    "source",
    [
        "class _Impl:\n" + L1_METHODS + "\nDriver = _Impl\n",
        "from impl import Driver\n",
        "import impl as Driver\n",
        "if True:\n    class Driver:\n" + L1_METHODS.replace("\n    ", "\n        "),
        "try:\n    class Driver:\n"
        + L1_METHODS.replace("\n    ", "\n        ")
        + "except Exception:\n    pass\n",
        "def make():\n    class Driver:\n        pass\n    return Driver\n\nDriver = make()\n",
    ],
    ids=["assignment", "from-import", "import-as", "nested-in-if", "nested-in-try", "factory"],
)
def test_driver_not_bound_by_a_top_level_class_statement_is_refused(tmp_path, source):
    assert _check(tmp_path, source) == [DRIVER_NOT_A_TOP_LEVEL_CLASS]


def test_methods_inherited_from_a_top_level_base_count(tmp_path):
    source = (
        "class _Base:\n" + L1_METHODS + "\n\nclass _Middle(_Base):\n    pass\n"
        "\n\nclass Driver(_Middle, object):\n    pass\n"
    )
    assert _check(tmp_path, source) == []


def test_a_followed_base_that_lacks_a_method_still_reports_it(tmp_path):
    base_methods = L1_METHODS.replace(
        '    def status(self):\n        return {"reachable": True}\n', ""
    )
    source = "class _Base:\n" + base_methods + "\n\nclass Driver(_Base):\n    pass\n"
    assert _check(tmp_path, source) == _missing("status")


def test_class_body_bindings_other_than_def_count(tmp_path):
    source = (
        "class Driver:\n"
        "    def login(self):\n        return {}\n"
        "    logout = login\n"
        "    from os import getcwd as status\n"
        "    if True:\n"
        "        def connect_ports(self, a, b):\n            return {}\n"
        "    else:\n"
        "        pass\n"
        "    try:\n"
        "        disconnect_ports: object = login\n"
        "    except Exception:\n"
        "        pass\n"
    )
    assert _check(tmp_path, source) == []


def test_module_level_attribute_assignment_counts(tmp_path):
    source = (
        "class Driver:\n"
        "    def login(self):\n        return {}\n"
        "    def logout(self):\n        return {}\n"
        "    def connect_ports(self, a, b):\n        return {}\n"
        "    def disconnect_ports(self, a, b):\n        return {}\n"
        "\n\ndef _status(self):\n    return {}\n\n\nDriver.status = _status\n"
    )
    assert _check(tmp_path, source) == []


@pytest.mark.parametrize(
    "source",
    [
        "from base import Base\n\n\nclass Driver(Base):\n    pass\n",
        "import base\n\n\nclass Driver(base.Base):\n    pass\n",
        "def deco(cls):\n    return cls\n\n\n@deco\nclass Driver:\n    pass\n",
        "class Meta(type):\n    pass\n\n\nclass Driver(metaclass=Meta):\n    pass\n",
        "class Driver:\n    pass\n\n\nsetattr(Driver, 'status', lambda self: {})\n",
        "from base import Base\n\n\nclass _Mid(Base):\n    pass\n\n\n"
        "class Driver(_Mid):\n    pass\n",
    ],
    ids=[
        "imported-base",
        "attribute-base",
        "decorator",
        "metaclass",
        "setattr",
        "imported-base-two-levels-up",
    ],
)
def test_methods_from_code_a_parser_cannot_read_are_not_refused(tmp_path, source):
    """The old import-based check accepted these when the methods really
    existed; the parser cannot tell, so it does not refuse them. A missing
    method then fails the sandboxed call that needs it."""
    assert _check(tmp_path, source) == []


def test_a_later_top_level_class_statement_wins(tmp_path):
    source = "class Driver:\n" + L1_METHODS + "\n\nclass Driver:\n    pass\n"
    assert _check(tmp_path, source) == _missing(
        "login", "logout", "connect_ports", "disconnect_ports", "status"
    )


def test_driver_never_runs_top_level_code(tmp_path):
    flag = tmp_path / "ran.flag"
    source = f'open(r"{flag}", "w").write("ran")\n' + "class Driver:\n" + L1_METHODS
    assert _check(tmp_path, source) == []
    assert not flag.exists()
