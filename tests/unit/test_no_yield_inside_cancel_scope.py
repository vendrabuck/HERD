"""Structural guard for issue #946: no yield inside a timeout or task-group scope.

A timeout scope cancels the task that entered it. In an async generator a
`yield` hands control back to the CALLER in the same task, so a deadline that
fires while the generator is suspended at a yield cancels the consumer, never
raises inside the generator, and its own `except` handlers do not run (PEP 789).
The assistant stream lost its terminal `done`/`error` frame that way.

Pure AST, no service imports: it scans every `services/*/app/**/*.py` for a
`yield` or `yield from` lexically inside a `with` or `async with` whose context
expression is `asyncio.timeout`, `asyncio.timeout_at`, or `asyncio.TaskGroup`.
The scope is recognized under every spelling that names it (issue #1145): the
module under an alias (`import asyncio as aio`), the function imported bare or
under an alias (`from asyncio import timeout as t`), a walrus inside the `with`,
and a variable bound to a scope call in the same function
(`scope = asyncio.timeout_at(d)` then `async with scope:`).
A yield inside a nested function definition belongs to that function and is not
counted against the enclosing scope.
"""

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCOPE_NAMES = {"timeout", "timeout_at", "TaskGroup"}


_FUNCTION_NODES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)


def _asyncio_spellings(tree: ast.Module) -> tuple[set[str], set[str]]:
    """Names bound to the asyncio module, and names bound to a scope function."""
    modules = {"asyncio"}
    scopes = set(SCOPE_NAMES)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "asyncio":
                    modules.add(alias.asname or alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module == "asyncio":
            for alias in node.names:
                if alias.name in SCOPE_NAMES:
                    scopes.add(alias.asname or alias.name)
    return modules, scopes


def _is_scope_call(expr: ast.expr, modules: set[str], scopes: set[str]) -> bool:
    if isinstance(expr, ast.NamedExpr):
        expr = expr.value
    target = expr.func if isinstance(expr, ast.Call) else expr
    if isinstance(target, ast.Attribute):
        return (
            isinstance(target.value, ast.Name)
            and target.value.id in modules
            and target.attr in SCOPE_NAMES
        )
    return isinstance(target, ast.Name) and target.id in scopes


def _own_nodes(scope: ast.AST) -> list[ast.AST]:
    """Every node of a function (or the module) outside its nested functions."""
    found: list[ast.AST] = []
    stack = list(ast.iter_child_nodes(scope))
    while stack:
        node = stack.pop()
        if isinstance(node, _FUNCTION_NODES):
            continue
        found.append(node)
        stack.extend(ast.iter_child_nodes(node))
    return found


def _scope_variables(nodes: list[ast.AST], modules: set[str], scopes: set[str]) -> set[str]:
    """Names assigned from a scope call among one function's own nodes."""
    names: set[str] = set()
    for node in nodes:
        if isinstance(node, ast.Assign) and _is_scope_call(node.value, modules, scopes):
            names.update(t.id for t in node.targets if isinstance(t, ast.Name))
        elif (
            isinstance(node, ast.AnnAssign)
            and node.value is not None
            and isinstance(node.target, ast.Name)
            and _is_scope_call(node.value, modules, scopes)
        ):
            names.add(node.target.id)
    return names


def _yields_in(nodes: list[ast.stmt]) -> list[int]:
    found: list[int] = []
    stack: list[ast.AST] = list(nodes)
    while stack:
        node = stack.pop()
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda | ast.ClassDef):
            continue
        if isinstance(node, ast.Yield | ast.YieldFrom):
            found.append(node.lineno)
        stack.extend(ast.iter_child_nodes(node))
    return found


def find_yields_in_cancel_scopes(source: str) -> list[int]:
    """Line numbers of yields lexically inside a timeout or TaskGroup scope."""
    tree = ast.parse(source)
    modules, scopes = _asyncio_spellings(tree)
    hits: set[int] = set()
    owners = [tree] + [n for n in ast.walk(tree) if isinstance(n, _FUNCTION_NODES)]
    for owner in owners:
        nodes = _own_nodes(owner)
        variables = _scope_variables(nodes, modules, scopes)
        for node in nodes:
            if isinstance(node, ast.With | ast.AsyncWith) and any(
                _is_scope_call(item.context_expr, modules, scopes)
                or (isinstance(item.context_expr, ast.Name) and item.context_expr.id in variables)
                for item in node.items
            ):
                hits.update(_yields_in(node.body))
    return sorted(hits)


def test_detector_flags_yield_in_timeout_scopes():
    src = (
        "async def g():\n"
        "    async with asyncio.timeout(1):\n"
        "        yield 1\n"
        "async def h():\n"
        "    async with asyncio.timeout_at(5.0):\n"
        "        async for x in y:\n"
        "            yield x\n"
        "async def t():\n"
        "    async with asyncio.TaskGroup() as tg:\n"
        "        yield from z\n"
        "async def mixed():\n"
        "    async with lock, asyncio.timeout(1):\n"
        "        if a:\n"
        "            yield 2\n"
    )
    assert find_yields_in_cancel_scopes(src) == [3, 7, 10, 14]


def test_detector_flags_every_spelling_of_the_scope():
    """Issue #1145: the alias and variable-bound spellings escaped the first scan."""
    src = (
        "import asyncio as aio\n"
        "from asyncio import timeout as to, TaskGroup as TG\n"
        "async def module_alias():\n"
        "    async with aio.timeout(5):\n"
        "        yield 1\n"
        "async def bound_scope(d):\n"
        "    scope = asyncio.timeout_at(d)\n"
        "    async with scope:\n"
        "        yield 2\n"
        "async def bound_alias_scope(d):\n"
        "    scope: object = aio.timeout(d)\n"
        "    async with scope:\n"
        "        yield 3\n"
        "async def function_alias():\n"
        "    async with to(1):\n"
        "        yield 4\n"
        "async def task_group_alias():\n"
        "    async with TG() as tg:\n"
        "        yield 5\n"
        "async def walrus():\n"
        "    async with (s := aio.timeout(1)):\n"
        "        yield 6\n"
        "def plain_bare_import():\n"
        "    with timeout(1):\n"
        "        yield 7\n"
    )
    assert find_yields_in_cancel_scopes(src) == [5, 9, 13, 16, 19, 22, 25]


def test_detector_ignores_safe_shapes():
    src = (
        "async def after():\n"
        "    async with asyncio.timeout_at(d):\n"
        "        ev = await anext(a)\n"
        "    yield ev\n"
        "async def other_cm():\n"
        "    async with lock:\n"
        "        yield 1\n"
        "async def nested_def():\n"
        "    async with asyncio.timeout(1):\n"
        "        async def inner():\n"
        "            yield 1\n"
        "        f = lambda: (yield)\n"
        "        await inner().__anext__()\n"
        "async def binds_a_scope(d):\n"
        "    scope = asyncio.timeout_at(d)\n"
        "async def same_name_other_function():\n"
        "    async with scope:\n"
        "        yield 3\n"
        "async def aliased_other_module():\n"
        "    async with aio.Lock():\n"
        "        yield 4\n"
    )
    assert find_yields_in_cancel_scopes(src) == []


def test_no_service_app_yields_inside_a_cancel_scope():
    files = sorted((REPO_ROOT / "services").glob("*/app/**/*.py"))
    assert files, "scan found no service app files: the glob is wrong"
    offenders = []
    for path in files:
        for lineno in find_yields_in_cancel_scopes(path.read_text(encoding="utf-8")):
            offenders.append(f"{path.relative_to(REPO_ROOT)}:{lineno}")
    assert not offenders, (
        "yield inside asyncio.timeout/timeout_at/TaskGroup (issue #946): "
        "bound only the wait for the next item and yield after the scope closes: "
        + ", ".join(offenders)
    )
