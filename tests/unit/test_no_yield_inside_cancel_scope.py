"""Structural guard for issue #946: no yield inside a timeout or task-group scope.

A timeout scope cancels the task that entered it. In an async generator a
`yield` hands control back to the CALLER in the same task, so a deadline that
fires while the generator is suspended at a yield cancels the consumer, never
raises inside the generator, and its own `except` handlers do not run (PEP 789).
The assistant stream lost its terminal `done`/`error` frame that way.

Pure AST, no service imports: it scans every `services/*/app/**/*.py` for a
`yield` or `yield from` lexically inside a `with` or `async with` whose context
expression is `asyncio.timeout`, `asyncio.timeout_at`, or `asyncio.TaskGroup`.
A yield inside a nested function definition belongs to that function and is not
counted against the enclosing scope.
"""

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCOPE_NAMES = {"timeout", "timeout_at", "TaskGroup"}


def _is_cancel_scope(expr: ast.expr) -> bool:
    target = expr.func if isinstance(expr, ast.Call) else expr
    if isinstance(target, ast.Attribute):
        return (
            isinstance(target.value, ast.Name)
            and target.value.id == "asyncio"
            and target.attr in SCOPE_NAMES
        )
    return isinstance(target, ast.Name) and target.id in SCOPE_NAMES


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
    hits: set[int] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.With | ast.AsyncWith) and any(
            _is_cancel_scope(item.context_expr) for item in node.items
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
