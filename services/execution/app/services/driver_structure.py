"""The driver package contract, checked by parsing driver.py and never by importing it.

One implementation shared by the load path (`driver_loader.validate_driver`, called by
`load_driver`) and the package validator (`package_validator._structural_errors`), so
the two can never disagree about what a valid package is (issue #1114). Driver package
code runs only inside the driver sandbox; nothing here executes it.

The rule, as checked:

- `driver.py` exists at the package root and parses.
- It holds a plain top-level `class Driver` statement. A `Driver` bound any other way (an
  assignment, an import, a class statement nested in an `if`, `try`, or function) is
  refused, because a parser cannot see what it would evaluate to.
- Every method `REQUIRED_METHODS` lists for the connection type is bound in the class
  body (`def`, `async def`, an assignment, or an import), assigned onto `Driver` at module
  level (`Driver.status = ...`), or inherited from a base class that is itself a top-level
  class statement in `driver.py` (followed recursively).
- When a method could come from somewhere a parser cannot read (a base class imported
  from another module or written as an expression, a class decorator, a metaclass or
  other class keyword, a `setattr(Driver, ...)` call), the method check cannot prove a
  method absent and passes; a missing method then fails the sandboxed call that needs
  it. This keeps every package the old import-based check accepted loading.

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


class DriverSourceError(Exception):
    """driver.py could not be read or parsed; the cause is chained as ``__cause__``.

    Each caller words this failure its own way: the load path keeps only the cause's
    class name (the text can carry a container path), while the package validator
    reports the parser's message as the recipe-authoring repair signal.
    """


def driver_structure_errors(driver_dir: Path, connection_type: str) -> list[str]:
    """Return the package's structural errors for its connection type (empty if valid).

    Raises ``DriverSourceError`` when driver.py cannot be read or parsed.
    """
    required = REQUIRED_METHODS.get(connection_type)
    if required is None:
        return [f"Unknown connection type: {connection_type}"]

    driver_py = driver_dir / "driver.py"
    if not driver_py.exists():
        return [MISSING_DRIVER_PY]

    try:
        # filename is the bare name so a parser message never carries the
        # extraction directory.
        tree = ast.parse(driver_py.read_bytes(), filename="driver.py")
    except (SyntaxError, ValueError, OSError) as exc:
        raise DriverSourceError("driver.py could not be parsed") from exc

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


def _class_body_names(cls: ast.ClassDef) -> set[str]:
    """Every name the class body binds, including inside if, try, with, and for blocks."""
    names: set[str] = set()
    stack: list[ast.AST] = list(cls.body)
    while stack:
        node = stack.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
            continue
        if isinstance(node, _OWN_SCOPE_NODES):
            continue
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            names.add(node.id)
        elif isinstance(node, ast.alias):
            names.add(node.asname or node.name.split(".")[0])
        stack.extend(ast.iter_child_nodes(node))
    return names


def _is_driver_name(node: ast.AST) -> bool:
    return isinstance(node, ast.Name) and node.id == DRIVER_CLASS_NAME


def _module_level_driver_attributes(tree: ast.Module) -> tuple[set[str], bool]:
    """Names assigned as ``Driver.<name> = ...``, and whether ``setattr(Driver, ...)`` occurs."""
    names: set[str] = set()
    has_setattr = False
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Attribute)
            and isinstance(node.ctx, ast.Store)
            and _is_driver_name(node.value)
        ):
            names.add(node.attr)
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "setattr"
            and node.args
            and _is_driver_name(node.args[0])
        ):
            has_setattr = True
    return names, has_setattr


def _visible_methods(
    tree: ast.Module, driver_cls: ast.ClassDef, classes: dict[str, ast.ClassDef]
) -> set[str] | None:
    """The names Driver provides, or None when some could come from code a parser cannot read."""
    attribute_names, has_setattr = _module_level_driver_attributes(tree)
    if has_setattr:
        return None

    names: set[str] = set(attribute_names)
    seen: set[str] = set()
    pending: list[ast.ClassDef] = [driver_cls]
    while pending:
        cls = pending.pop()
        if cls.name in seen:
            continue
        seen.add(cls.name)
        if cls.decorator_list or cls.keywords:
            return None
        names |= _class_body_names(cls)
        for base in cls.bases:
            if isinstance(base, ast.Name) and base.id == "object":
                continue
            if isinstance(base, ast.Name) and base.id in classes and base.id != cls.name:
                pending.append(classes[base.id])
                continue
            return None
    return names
