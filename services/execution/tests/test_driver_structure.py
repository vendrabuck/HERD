"""The one structural check of a driver package, by parsing and never importing (issue #1114).

driver_structure_errors is shared by the load path (validate_driver) and the package
validator. These tests pin the rule it states: every parse failure, the parser's own
RecursionError and MemoryError included, is a DriverSourceError (issue #1178); a plain
top-level `class Driver` statement is required; a required method counts when a binding
that could be callable provides it, in the class body, assigned onto Driver at module
level, or in a top-level base class in driver.py (issue #1179); and where a method could
come from code a parser cannot read, the check passes. The shapes where the answer
differs from the import-based check this replaced are pinned with that check's answer
quoted.
"""

from pathlib import Path
from unittest.mock import patch

import pytest
from app.services import driver_structure
from app.services.driver_structure import (
    DRIVER_NOT_A_TOP_LEVEL_CLASS,
    MISSING_DRIVER_CLASS,
    MISSING_DRIVER_PY,
    PARSE_FAILURES,
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


# --- every parse failure is a DriverSourceError (issue #1178) ---

# Shapes the parser itself cannot get through. Before #1178 these escaped as a bare
# RecursionError or MemoryError, which the load path classed as transient and which
# left the extraction directory behind on every retry.
PARSER_CRASH_SOURCES = {
    "plus-chain": "x = " + "+".join(["1"] * 500_000) + "\nclass Driver:\n    pass\n",
    "unary-chain": "x = " + "-" * 200_000 + "1\nclass Driver:\n    pass\n",
}


@pytest.mark.parametrize("source", PARSER_CRASH_SOURCES.values(), ids=PARSER_CRASH_SOURCES)
def test_a_parser_crash_is_a_parse_failure(tmp_path, source):
    with pytest.raises(DriverSourceError) as exc:
        _check(tmp_path, source)
    assert isinstance(exc.value.__cause__, (RecursionError, MemoryError))


def test_parse_failures_name_every_class_the_parser_can_raise():
    assert PARSE_FAILURES == (SyntaxError, ValueError, OSError, RecursionError, MemoryError)


@pytest.mark.parametrize(
    "error",
    [
        SyntaxError("bad"),
        ValueError("null byte"),
        UnicodeDecodeError("utf-8", b"\xff", 0, 1, "bad"),
        OSError("unreadable"),
        RecursionError("too deep"),
        MemoryError(),
    ],
    ids=lambda e: type(e).__name__,
)
def test_each_parse_failure_class_becomes_a_driver_source_error(tmp_path, error):
    """Each class, raised from the module's own parse seam, is wrapped with the
    cause chained, so a platform where the parser raises another of them for
    the crash shapes above is covered too."""
    (tmp_path / "driver.py").write_text("class Driver:\n    pass\n", encoding="utf-8")
    with patch.object(driver_structure, "_parse_source", side_effect=error):
        with pytest.raises(DriverSourceError) as exc:
            driver_structure_errors(tmp_path, L1)
    assert exc.value.__cause__ is error


def test_an_unrelated_error_is_not_relabelled_as_a_parse_failure(tmp_path):
    """Only the listed classes become a broken-package answer; anything else
    propagates unchanged."""
    (tmp_path / "driver.py").write_text("class Driver:\n    pass\n", encoding="utf-8")
    with patch.object(driver_structure, "_parse_source", side_effect=KeyError("bug")):
        with pytest.raises(KeyError):
            driver_structure_errors(tmp_path, L1)


# --- a required method needs a binding that could be callable (issue #1179) ---

FOUR_L1 = L1_METHODS.replace('    def status(self):\n        return {"reachable": True}\n', "")


@pytest.mark.parametrize(
    "body",
    [
        "    status = None\n",
        "    status = 'not callable'\n",
        "    status = 5\n",
        "    status = []\n",
        "    status = {}\n",
        "    status = ()\n",
        "    status = f'{1}'\n",
        "    status = [x for x in ()]\n",
        "    status: object\n",
        "    status, other = None, len\n",
        "    other, *status = len, len\n",
        "    def status(self):\n        return {}\n    del status\n",
        "    def status(self):\n        return {}\n    status = None\n",
    ],
    ids=[
        "none",
        "string",
        "number",
        "list",
        "dict",
        "tuple",
        "f-string",
        "comprehension",
        "annotation-only",
        "tuple-unpacked-none",
        "starred-binds-a-list",
        "del-after-def",
        "literal-after-def",
    ],
)
def test_a_class_body_binding_that_is_never_callable_does_not_count(tmp_path, body):
    """The import-based check (callable(getattr(Driver, name, None))) refused
    each of these too; #1131's parse-based check accepted them until #1179."""
    assert _check(tmp_path, "class Driver:\n" + FOUR_L1 + body) == _missing("status")


@pytest.mark.parametrize(
    "body",
    [
        "    status = lambda self: {}\n",
        "    status = login\n",
        "    status = staticmethod(len)\n",
        "    status = login.__call__\n",
        "    status = None\n    status = login\n",
        "    status, other = login, None\n",
        "    status, other = make()\n",
        "    status: object = login\n",
        "    class status:\n        pass\n",
        "    from os import getcwd as status\n",
    ],
    ids=[
        "lambda",
        "name",
        "call",
        "attribute",
        "rebound-after-literal",
        "tuple-paired",
        "tuple-from-call",
        "annotated-with-value",
        "nested-class",
        "import",
    ],
)
def test_a_class_body_binding_that_could_be_callable_counts(tmp_path, body):
    source = "class Driver:\n" + FOUR_L1 + body
    assert _check(tmp_path, source) == []


def test_a_literal_in_the_class_body_hides_an_inherited_method(tmp_path):
    source = (
        "class _Base:\n    def status(self):\n        return {}\n\n\n"
        "class Driver(_Base):\n" + FOUR_L1 + "    status = None\n"
    )
    assert _check(tmp_path, source) == _missing("status")


def test_a_del_in_the_class_body_exposes_the_inherited_method(tmp_path):
    source = (
        "class _Base:\n    def status(self):\n        return {}\n\n\n"
        "class Driver(_Base):\n" + FOUR_L1 + "    def status(self):\n        return {}\n"
        "    del status\n"
    )
    assert _check(tmp_path, source) == []


@pytest.mark.parametrize(
    "tail",
    ["Driver.status = None\n", "del Driver.status\n", "Driver.status: object = 'x'\n"],
    ids=["literal", "del", "annotated-literal"],
)
def test_a_module_level_statement_after_the_class_can_remove_a_method(tmp_path, tail):
    """The import-based check refused these too."""
    source = "class Driver:\n" + L1_METHODS + "\n\n" + tail
    assert _check(tmp_path, source) == _missing("status")


def test_a_module_level_literal_then_callable_counts(tmp_path):
    source = (
        "class Driver:\n" + FOUR_L1 + "\n\nDriver.status = None\nDriver.status = lambda self: {}\n"
    )
    assert _check(tmp_path, source) == []


@pytest.mark.parametrize(
    "body",
    [
        "    if False:\n        def status(self):\n            return {}\n",
        "    for status in []:\n        pass\n",
        "    with open('x') as status:\n        pass\n",
        "    def status(self):\n        return {}\n    if False:\n        del status\n",
        "    def status(self):\n        return {}\n    if False:\n        status = None\n",
    ],
    ids=[
        "def-under-if-false",
        "for-target",
        "with-target",
        "conditional-del",
        "conditional-literal",
    ],
)
def test_bindings_in_code_that_may_not_run_pass_by_decision(tmp_path, body):
    """Undecidable for a parser, so the check passes (module docstring, Limits).
    The import-based check refused the first three, where the method really is
    absent at runtime, and accepted the last two."""
    assert _check(tmp_path, "class Driver:\n" + FOUR_L1 + body) == []


# --- where the parse-based check and the import-based check differ (issue #1179) ---


def test_a_method_assigned_onto_a_followed_base_is_reported_missing(tmp_path):
    """The import-based check ACCEPTED this (the method exists at runtime): only
    Driver.<name> assignments are read, never assignments onto a base."""
    source = (
        "class _Base:\n    pass\n\n\ndef _status(self):\n    return {}\n\n\n"
        "_Base.status = _status\n\n\nclass Driver(_Base):\n" + FOUR_L1
    )
    assert _check(tmp_path, source) == _missing("status")


def test_a_method_added_by_rebinding_the_class_is_reported_missing(tmp_path):
    """The import-based check ACCEPTED this: the rebinding `Driver = add(Driver)`
    is not read, so the class statement alone is judged."""
    source = (
        "def add(cls):\n    cls.status = lambda self: {}\n    return cls\n\n\n"
        "class Driver:\n" + FOUR_L1 + "\n\nDriver = add(Driver)\n"
    )
    assert _check(tmp_path, source) == _missing("status")


def test_a_method_assigned_only_inside_an_uncalled_function_counts(tmp_path):
    """The import-based check REFUSED this (the function never runs, so the
    method is absent): an assignment inside a function may run, so it counts."""
    source = (
        "class Driver:\n" + FOUR_L1 + "\n\ndef _never():\n    Driver.status = lambda self: {}\n"
    )
    assert _check(tmp_path, source) == []


def test_a_driver_py_that_parses_but_does_not_compile_passes(tmp_path):
    """The import-based check REFUSED this (`Failed to load driver.py:
    SyntaxError`): an assignment expression in a comprehension in a class body is
    refused by the compiler, not the parser, so the load passes and the first
    sandboxed call fails."""
    source = "class Driver:\n" + L1_METHODS + "    _x = [y := 1 for _ in ()]\n"
    assert _check(tmp_path, source) == []
