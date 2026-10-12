"""The driver package contract, checked by parsing driver.py and never by importing it.

One implementation shared by the load path (`driver_loader.validate_driver`, called by
`load_driver`) and the package validator (`package_validator._structural_errors`), so
the two can never disagree about what a valid package is (issue #1114). Driver package
code runs only inside the driver sandbox; nothing here executes it.

The rule, as checked:

- `driver.py` exists at the package root and parses. Every way the parse can fail
  (`PARSE_FAILURES`: a syntax error, a null byte or bad encoding, an unreadable file,
  and the parser's own `RecursionError` or `MemoryError` on a deeply nested or very long
  expression) is a `DriverSourceError`, so a package the parser cannot read is a
  permanent load failure and never a transient one (issue #1178).
- It holds a plain top-level `class Driver` statement. A `Driver` bound any other way (an
  assignment, an import, a class statement nested in an `if`, `try`, or function) is
  refused, because a parser cannot see what it would evaluate to.
- Every method `REQUIRED_METHODS` lists for the connection type is provided by a binding
  that could be callable (issue #1179): in the class body a `def`, an `async def`, a
  nested class, an import, or an assignment whose value is not a literal (a lambda, a
  name, an attribute, a call); at module level an assignment of such a value onto
  `Driver` (`Driver.status = ...`); or inheritance from a base class that is itself a
  top-level class statement in `driver.py` and provides it the same way (followed
  recursively). An annotation with no value binds nothing, and an assignment of a
  literal (`None`, a string, a number, a container, an f-string, a comprehension) binds
  something that is never callable, so neither provides the method and a literal
  hides an inherited one. The class body and the module-level statements after the
  class run unconditionally in order, so there a later `del` or literal assignment
  removes an earlier binding.
- When a method could come from somewhere a parser cannot read (a base class imported
  from another module or written as an expression, a class decorator, a metaclass or
  other class keyword, a `setattr(Driver, ...)` call anywhere), the method check cannot
  prove a method absent and passes; a missing method then fails the sandboxed call that
  needs it.

Limits (what the parse cannot decide, passed by decision):

- A binding inside a conditional block (`if`, `try`, `while`, `with`, `match`), or a
  `for` or `with` target, counts as provided whether or not that code runs, and a `del`
  or literal assignment there removes nothing. So `if False: def status...` and
  `for status in []: pass` pass.
- `Driver.<name> = ...` anywhere other than the module-level statements after the
  class (inside a function, even one never called, or before the class) counts when its
  value could be callable and removes nothing.
- A `setattr(Driver, ...)` call anywhere, even in dead code, and any class decorator or
  class keyword turn the whole method check off, in the load path and the package
  validator alike; the validator's sandboxed dry run still calls the five Hypervisor
  methods, so a missing one still fails the report.
- A class with more than one followed base gets a method when any base provides it;
  the method resolution order is not computed, so a literal in an earlier base does
  not hide a later base's method.

How it differs from the import-based check it replaced (`callable(getattr(Driver,
name, None))` after running driver.py): it refuses two shapes that check accepted, a
required method assigned onto a followed base after its class statement
(`_Base.status = f`; only `Driver.<name>` assignments are read) and a method added by
rebinding the class (`Driver = add(Driver)`; the rebinding is not read, so the method
is reported missing). It accepts shapes that check refused: a method assigned only
inside a function that is never called, one bound only under a condition that is
false, a class with a decorator, a class keyword, or a `setattr` call whose methods are
really missing, and a driver.py that parses but does not compile (an assignment
expression in a comprehension in a class body is refused by the compiler, not the
parser). Each of those then fails its first sandboxed call. No checked-in driver uses
any of these shapes.

Pure standard library on purpose: tests/unit loads this file by path to check every
checked-in driver under drivers/ without importing the execution service.
"""

import ast
from pathlib import Path

# Required methods per connection type. driver_loader re-exports this name, which is
# where the rest of the service and its tests import it from.
REQUIRED_METHODS = {
    "Layer 1 Switch": ["login", "logout", "connect_ports", "disconnect_ports", "status"],
    "Layer 2 Switch": [
        "login",
        "logout",
        "create_vlan",
        "add_to_vlan",
        "remove_from_vlan",
        "delete_vlan",
        "status",
    ],
    "Layer 3 Switch": ["login", "logout", "configure_route", "remove_route", "status"],
    "Management": ["login", "logout", "configure", "backup", "status"],
    # A dynamic-resource recipe (ADR 0004, issue #32): an ordinary driver
    # package whose connection_type is Hypervisor. create_instance materializes
    # an instance and destroy_instance idempotently removes it.
    "Hypervisor": ["login", "logout", "create_instance", "destroy_instance", "status"],
}

DRIVER_CLASS_NAME = "Driver"

MISSING_DRIVER_PY = "Missing driver.py at package root"
MISSING_DRIVER_CLASS = "driver.py must define a class named Driver"
DRIVER_NOT_A_TOP_LEVEL_CLASS = "driver.py must define Driver with a plain top-level class statement"

# Every way ast.parse can fail on a file a package author controls. SyntaxError covers
# bad syntax and a bad coding cookie, ValueError a null byte and a decode error
# (UnicodeDecodeError), OSError an unreadable file. RecursionError and MemoryError are
# raised by the parser itself on a deeply nested or very long expression (issue
# #1178); they are listed by name rather than caught as Exception so an unrelated
# defect in this module is never relabelled as a broken package.
PARSE_FAILURES = (SyntaxError, ValueError, OSError, RecursionError, MemoryError)

# Nodes that open a scope of their own: a name bound inside one is not bound in the
# class body that contains it.
_OWN_SCOPE_NODES = (
    ast.FunctionDef,
    ast.AsyncFunctionDef,
    ast.ClassDef,
    ast.Lambda,
    ast.ListComp,
    ast.SetComp,
    ast.DictComp,
    ast.GeneratorExp,
)

# Values that are never callable: a literal of any kind. Anything else (a lambda, a
# name, an attribute, a call, a subscript, an operator) could evaluate to a callable,
# and the parser cannot tell, so it counts.
_NEVER_CALLABLE_VALUES = (
    ast.Constant,
    ast.JoinedStr,
    ast.List,
    ast.Tuple,
    ast.Set,
    ast.Dict,
    ast.ListComp,
    ast.SetComp,
    ast.DictComp,
    ast.GeneratorExp,
)


class DriverSourceError(Exception):
    """driver.py could not be read or parsed; the cause is chained as ``__cause__``.

    Each caller words this failure its own way: the load path keeps only the cause's
    class name (the text can carry a container path), while the package validator
    reports the parser's message as the recipe-authoring repair signal.
    """


def _parse_source(source: bytes, filename: str) -> ast.Module:
    """The one parser call; a module-level seam so tests can make the parse fail."""
    return ast.parse(source, filename=filename)


def parse_python_file(path: Path, filename: str) -> ast.Module:
    """Read a package file as bytes and parse it once.

    Bytes, so a PEP 263 coding cookie is honored the same way wherever the file is
    judged. ``filename`` is the package-relative name, so a parser message never
    carries the extraction directory. Raises ``DriverSourceError`` for every failure
    in ``PARSE_FAILURES``.
    """
    try:
        return _parse_source(path.read_bytes(), filename)
    except PARSE_FAILURES as exc:
        raise DriverSourceError(f"{filename} could not be parsed") from exc


def driver_structure_errors(
    driver_dir: Path,
    connection_type: str,
    *,
    parsed: ast.Module | DriverSourceError | None = None,
) -> list[str]:
    """Return the package's structural errors for its connection type (empty if valid).

    ``parsed`` is the caller's own result of ``parse_python_file`` for driver.py, the
    tree or the ``DriverSourceError`` it raised, so a caller that parses every file
    (the package validator) parses driver.py once; when None, driver.py is read and
    parsed here. Raises ``DriverSourceError`` when driver.py cannot be read or parsed.
    """
    required = REQUIRED_METHODS.get(connection_type)
    if required is None:
        return [f"Unknown connection type: {connection_type}"]

    driver_py = driver_dir / "driver.py"
    if not driver_py.exists():
        return [MISSING_DRIVER_PY]

    if isinstance(parsed, DriverSourceError):
        raise parsed
    tree = parsed if parsed is not None else parse_python_file(driver_py, "driver.py")

    classes = _top_level_classes(tree)
    driver_cls = classes.get(DRIVER_CLASS_NAME)
    if driver_cls is None:
        if _binds_driver_elsewhere(tree):
            return [DRIVER_NOT_A_TOP_LEVEL_CLASS]
        return [MISSING_DRIVER_CLASS]

    available = _visible_methods(tree, driver_cls, classes)
    if available is None:
        return []
    return [
        f"Driver class is missing required method: {method_name}"
        for method_name in required
        if method_name not in available
    ]


def _top_level_classes(tree: ast.Module) -> dict[str, ast.ClassDef]:
    """Top-level class statements by name; a later statement wins, as at runtime."""
    return {node.name: node for node in tree.body if isinstance(node, ast.ClassDef)}


def _binds_driver_elsewhere(tree: ast.Module) -> bool:
    """True when driver.py binds the name Driver some way other than a top-level class."""
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == DRIVER_CLASS_NAME:
            return True
        if (
            isinstance(node, ast.Name)
            and node.id == DRIVER_CLASS_NAME
            and isinstance(node.ctx, ast.Store)
        ):
            return True
        if isinstance(node, ast.alias) and (node.asname or node.name) == DRIVER_CLASS_NAME:
            return True
    return False


def _could_be_callable(value: ast.expr | None) -> bool:
    """False only for a literal; None means a value the parser cannot see (unpacking)."""
    return value is None or not isinstance(value, _NEVER_CALLABLE_VALUES)


def _assigned_targets(target: ast.expr, value: ast.expr | None):
    """Yield (bound target, could be callable) for each Name or Attribute an
    assignment binds, pairing a tuple target with a tuple literal element by element."""
    if isinstance(target, ast.Starred):
        # A starred target always binds a list.
        if isinstance(target.value, (ast.Name, ast.Attribute)):
            yield target.value, False
        else:
            yield from _assigned_targets(target.value, None)
        return
    if isinstance(target, (ast.Tuple, ast.List)):
        values: list[ast.expr] | None = None
        if (
            isinstance(value, (ast.Tuple, ast.List))
            and len(value.elts) == len(target.elts)
            and not any(isinstance(e, ast.Starred) for e in (*value.elts, *target.elts))
        ):
            values = value.elts
        for index, element in enumerate(target.elts):
            yield from _assigned_targets(element, values[index] if values is not None else None)
        return
    if isinstance(target, (ast.Name, ast.Attribute)):
        yield target, _could_be_callable(value)


def _assignment_bindings(stmt: ast.stmt):
    """(target, could be callable) for an Assign or an AnnAssign that has a value."""
    if isinstance(stmt, ast.Assign):
        for target in stmt.targets:
            yield from _assigned_targets(target, stmt.value)
    elif isinstance(stmt, ast.AnnAssign) and stmt.value is not None:
        yield from _assigned_targets(stmt.target, stmt.value)


def _deleted_targets(stmt: ast.Delete):
    """Each Name or Attribute a del statement unbinds."""
    pending: list[ast.expr] = list(stmt.targets)
    while pending:
        target = pending.pop()
        if isinstance(target, (ast.Tuple, ast.List)):
            pending.extend(target.elts)
        elif isinstance(target, (ast.Name, ast.Attribute)):
            yield target


def _names_in_target(target: ast.expr) -> set[str]:
    return {
        node.id
        for node in ast.walk(target)
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store)
    }


def _possible_class_bindings(root: ast.AST) -> set[str]:
    """Names a class-body statement might bind to something callable.

    Used for code that may or may not run (a conditional block, a loop) and for the
    walrus expressions inside an unconditional statement: a binding that could be
    callable counts, and nothing here removes one. Scopes of their own are not
    entered; a def or class contributes its own name.
    """
    names: set[str] = set()
    stack: list[ast.AST] = [root]
    while stack:
        node = stack.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
            continue
        if isinstance(node, _OWN_SCOPE_NODES):
            continue
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            for target, could_call in _assignment_bindings(node):
                if could_call and isinstance(target, ast.Name):
                    names.add(target.id)
        elif isinstance(node, ast.NamedExpr):
            if _could_be_callable(node.value):
                names.add(node.target.id)
        elif isinstance(node, (ast.For, ast.AsyncFor)):
            # Undecidable: what a loop target ends up holding (pass by decision).
            names |= _names_in_target(node.target)
        elif isinstance(node, ast.withitem) and node.optional_vars is not None:
            names |= _names_in_target(node.optional_vars)
        elif isinstance(node, ast.alias):
            names.add(node.asname or node.name.split(".")[0])
        elif isinstance(node, (ast.MatchAs, ast.MatchStar)) and node.name:
            names.add(node.name)
        elif isinstance(node, ast.MatchMapping) and node.rest:
            names.add(node.rest)
        stack.extend(ast.iter_child_nodes(node))
    return names


def _class_namespace(cls: ast.ClassDef) -> dict[str, bool]:
    """What the class body leaves bound: name to whether it could be callable.

    The body's own statements run unconditionally and in order, so an assignment
    rebinds, a literal makes the name non-callable (hiding an inherited method), and a
    del unbinds it (exposing an inherited one). A compound statement only adds
    (``_possible_class_bindings``).
    """
    own: dict[str, bool] = {}
    for stmt in cls.body:
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            own[stmt.name] = True
        elif isinstance(stmt, (ast.Import, ast.ImportFrom)):
            for alias in stmt.names:
                own[alias.asname or alias.name.split(".")[0]] = True
        elif isinstance(stmt, (ast.Assign, ast.AnnAssign)):
            if stmt.value is not None:
                # A walrus in the value binds first.
                for name in _possible_class_bindings(stmt.value):
                    own[name] = True
            for target, could_call in _assignment_bindings(stmt):
                if isinstance(target, ast.Name):
                    own[target.id] = could_call
        elif isinstance(stmt, ast.Delete):
            for target in _deleted_targets(stmt):
                if isinstance(target, ast.Name):
                    own.pop(target.id, None)
        else:
            for name in _possible_class_bindings(stmt):
                own[name] = True
    return own


def _is_driver_name(node: ast.AST) -> bool:
    return isinstance(node, ast.Name) and node.id == DRIVER_CLASS_NAME


def _driver_attribute(target: ast.expr) -> str | None:
    if isinstance(target, ast.Attribute) and _is_driver_name(target.value):
        return target.attr
    return None


def _has_driver_setattr(tree: ast.Module) -> bool:
    return any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "setattr"
        and node.args
        and _is_driver_name(node.args[0])
        for node in ast.walk(tree)
    )


def _possible_driver_attributes(root: ast.AST) -> set[str]:
    """``Driver.<name>`` assignments anywhere under root whose value could be callable."""
    names: set[str] = set()
    for node in ast.walk(root):
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            for target, could_call in _assignment_bindings(node):
                attr = _driver_attribute(target)
                if attr is not None and could_call:
                    names.add(attr)
    return names


def _apply_module_statements(
    tree: ast.Module, driver_cls: ast.ClassDef, own: dict[str, bool]
) -> None:
    """Apply what driver.py does to ``Driver.<name>`` outside the class body to ``own``.

    The top-level statements after the class statement run unconditionally and in
    order, so there an assignment rebinds the attribute and a del unbinds it. Anything
    else (code inside a function or a conditional block, or before the class
    statement) may or may not run, so it only adds a binding that could be callable.
    """
    after_class = False
    for stmt in tree.body:
        if stmt is driver_cls:
            after_class = True
            continue
        if after_class and isinstance(stmt, (ast.Assign, ast.AnnAssign)):
            for target, could_call in _assignment_bindings(stmt):
                attr = _driver_attribute(target)
                if attr is not None:
                    own[attr] = could_call
        elif after_class and isinstance(stmt, ast.Delete):
            for target in _deleted_targets(stmt):
                attr = _driver_attribute(target)
                if attr is not None:
                    own.pop(attr, None)
        else:
            for attr in _possible_driver_attributes(stmt):
                own[attr] = True


def _provided_names(
    cls: ast.ClassDef,
    classes: dict[str, ast.ClassDef],
    seen: frozenset[str],
    own: dict[str, bool] | None = None,
) -> set[str] | None:
    """Names the class provides as something that could be callable, or None when some
    could come from code a parser cannot read. ``own`` overrides the class body's own
    namespace (the caller passes Driver's with the module-level statements applied)."""
    if cls.decorator_list or cls.keywords:
        return None
    if own is None:
        own = _class_namespace(cls)
    inherited: set[str] = set()
    for base in cls.bases:
        if isinstance(base, ast.Name) and base.id == "object":
            continue
        if isinstance(base, ast.Name) and base.id in classes and base.id != cls.name:
            if base.id in seen:
                continue
            provided = _provided_names(classes[base.id], classes, seen | {base.id})
            if provided is None:
                return None
            inherited |= provided
            continue
        return None
    return {name for name, could_call in own.items() if could_call} | (inherited - own.keys())


def _visible_methods(
    tree: ast.Module, driver_cls: ast.ClassDef, classes: dict[str, ast.ClassDef]
) -> set[str] | None:
    """The names Driver provides, or None when some could come from code a parser cannot read."""
    if _has_driver_setattr(tree):
        return None
    own = _class_namespace(driver_cls)
    _apply_module_statements(tree, driver_cls, own)
    return _provided_names(driver_cls, classes, frozenset({driver_cls.name}), own)
