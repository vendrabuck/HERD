"""Repo guard: integration tests delete devices, ports, and topologies only through
the checked teardown helpers (issue #1158, the TEARDOWN CONTRACT).

Inventory refuses a device delete while a live reservation or a cable depends on the
device (issues #900, #940), a port delete while a cable names the port (#1023), and
cabling refuses a topology delete while a live reservation references it (#977). A
test that sends the DELETE itself and never reads the answer leaks the row silently,
so `tests/integration/` routes those deletes through `delete_device_checked`,
`delete_devices_checked`, `delete_port_checked`, and `delete_topology_checked`, which
fail the test on a refusal.

This scan keeps it that way. It detects the BEHAVIOR, not one spelling (the issue
#1145 rule): an HTTP DELETE whose URL resolves to one device, port, or topology,
whether the call is `client.delete(...)`, a bound `delete` under another name,
`getattr(client, "delete")(...)`, or any call that names the method "DELETE"
(`client.request`, `build_request`, `httpx.Request`), and whether the URL is an
f-string, a concatenation, `str.format`, `%` formatting, a `"".join`, a name assigned
one of those (in the function or at module level), or a parameter of a local wrapper
that forwards it to a delete. The spellings are fixture cases below that must be
flagged.

The exemptions are explicit, per function and per kind, with a count and a reason:
the helpers themselves, and the tests whose SUBJECT is the delete's answer. A new
delete in an exempt function changes its count and fails; an exemption nothing
matches any more fails as stale.

Known limits, stated plainly: a URL that reaches the call only through a function
parameter is followed one level (a local wrapper called by name), not through
several; a URL built at run time from data the scan cannot see (a dict lookup, an
attribute, a value returned by a call) is not resolved.
"""

import ast
import itertools
import re
from pathlib import Path

import pytest

INTEGRATION_DIR = Path(__file__).resolve().parents[1] / "integration"

# What a delete can target that a guard on the service side may refuse.
GUARDED_PATHS = {
    "/inventory/devices": "device",
    "/inventory/ports": "port",
    "/cabling/topologies": "topology",
}

# A piece of a URL the scan cannot resolve statically (one path segment at run time).
_HOLE = "\x00"
# A function parameter, kept by name so a wrapper's call sites can fill it in.
_PARAM = "\x01{}\x01"
_PARAM_RE = re.compile("\x01([A-Za-z_][A-Za-z0-9_]*)\x01")
_MAX_TEMPLATES = 32
_MAX_DEPTH = 6

_MEMBER_RE = re.compile("(" + "|".join(re.escape(p) for p in GUARDED_PATHS) + r")/[^/?#]+/?$")


def _kind_of(template: str) -> str | None:
    """The resource kind a URL template deletes ONE member of, or None."""
    match = _MEMBER_RE.search(_PARAM_RE.sub(_HOLE, template))
    return GUARDED_PATHS[match.group(1)] if match else None


class _Scope:
    """Assignments and parameters of one function body (or of the module)."""

    def __init__(self, node: ast.AST):
        self.node = node
        self.params: set[str] = set()
        self.assigns: dict[str, list[ast.expr]] = {}
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda):
            args = node.args
            for arg in (*args.posonlyargs, *args.args, *args.kwonlyargs):
                self.params.add(arg.arg)
        for child in self._own_nodes(node):
            if isinstance(child, ast.Assign):
                for target in child.targets:
                    if isinstance(target, ast.Name):
                        self.assigns.setdefault(target.id, []).append(child.value)
            elif (
                isinstance(child, ast.AnnAssign | ast.NamedExpr)
                and child.value is not None
                and isinstance(child.target, ast.Name)
            ):
                self.assigns.setdefault(child.target.id, []).append(child.value)

    @staticmethod
    def _own_nodes(node: ast.AST):
        """Every node of this body, not descending into nested function bodies."""
        stack = list(ast.iter_child_nodes(node))
        while stack:
            child = stack.pop()
            yield child
            if not isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda):
                stack.extend(ast.iter_child_nodes(child))


def _cap(templates) -> set[str]:
    return set(itertools.islice(templates, _MAX_TEMPLATES))


def _concat(parts: list[set[str]]) -> set[str]:
    return _cap("".join(combo) for combo in itertools.product(*parts))


def _render(expr: ast.expr | None, scopes: list[_Scope], depth: int = 0) -> set[str]:
    """Every string `expr` can statically be, with holes where it cannot be known.

    `scopes` is innermost first, the module last. A name resolves to what it was
    assigned in the nearest scope that assigns it; a parameter stays a named hole.
    """
    if expr is None or depth > _MAX_DEPTH:
        return {_HOLE}
    if isinstance(expr, ast.Constant):
        return {expr.value if isinstance(expr.value, str) else str(expr.value)}
    if isinstance(expr, ast.JoinedStr):
        return _concat([_render(part, scopes, depth + 1) for part in expr.values])
    if isinstance(expr, ast.FormattedValue):
        return _render(expr.value, scopes, depth + 1)
    if isinstance(expr, ast.NamedExpr):
        return _render(expr.value, scopes, depth + 1)
    if isinstance(expr, ast.IfExp):
        return _cap(_render(expr.body, scopes, depth + 1) | _render(expr.orelse, scopes, depth + 1))
    if isinstance(expr, ast.BoolOp):
        return _cap(set().union(*(_render(v, scopes, depth + 1) for v in expr.values)))
    if isinstance(expr, ast.BinOp) and isinstance(expr.op, ast.Add):
        return _concat(
            [_render(expr.left, scopes, depth + 1), _render(expr.right, scopes, depth + 1)]
        )
    if isinstance(expr, ast.BinOp) and isinstance(expr.op, ast.Mod):
        return {
            re.sub(r"%(\([^)]*\))?[#0\- +]*\d*(\.\d+)?[sdrif]", _HOLE, t)
            for t in _render(expr.left, scopes, depth + 1)
        }
    if isinstance(expr, ast.Call):
        func = expr.func
        if isinstance(func, ast.Attribute) and func.attr == "format":
            return {re.sub(r"\{[^{}]*\}", _HOLE, t) for t in _render(func.value, scopes, depth + 1)}
        if (
            isinstance(func, ast.Attribute)
            and func.attr == "join"
            and len(expr.args) == 1
            and isinstance(expr.args[0], ast.List | ast.Tuple)
        ):
            seps = _render(func.value, scopes, depth + 1)
            items = [_render(item, scopes, depth + 1) for item in expr.args[0].elts]
            woven: list[set[str]] = []
            for index, item in enumerate(items):
                if index:
                    woven.append(seps)
                woven.append(item)
            return _concat(woven) if woven else {""}
        if isinstance(func, ast.Name) and func.id == "str" and len(expr.args) == 1:
            return _render(expr.args[0], scopes, depth + 1)
        return {_HOLE}
    if isinstance(expr, ast.Name):
        for scope in scopes:
            if expr.id in scope.assigns:
                rendered: set[str] = set()
                for value in scope.assigns[expr.id]:
                    rendered |= _render(value, scopes, depth + 1)
                return _cap(rendered)
            if expr.id in scope.params:
                return {_PARAM.format(expr.id)}
        return {_HOLE}
    return {_HOLE}


def _is_delete_attr(expr: ast.expr) -> bool:
    return isinstance(expr, ast.Attribute) and expr.attr == "delete"


def _names_bound_to_delete(scopes: list[_Scope]) -> set[str]:
    """Names assigned a bound `.delete` method (`rm = client.delete`)."""
    return {
        name
        for scope in scopes
        for name, values in scope.assigns.items()
        if any(_is_delete_attr(value) for value in values)
    }


def _keyword(call: ast.Call, name: str) -> ast.expr | None:
    for kw in call.keywords:
        if kw.arg == name:
            return kw.value
    return None


def _delete_url(call: ast.Call, scopes: list[_Scope]) -> ast.expr | None:
    """The URL expression when `call` sends an HTTP DELETE, else None."""
    func = call.func
    url_kw = _keyword(call, "url")
    first = call.args[0] if call.args else None

    # client.delete(url), rm(url) with rm = client.delete, getattr(client, "delete")(url)
    is_delete_call = (
        _is_delete_attr(func)
        or (isinstance(func, ast.Name) and func.id in _names_bound_to_delete(scopes))
        or (
            isinstance(func, ast.Call)
            and isinstance(func.func, ast.Name)
            and func.func.id == "getattr"
            and len(func.args) >= 2
            and "delete" in _render(func.args[1], scopes)
        )
    )
    if is_delete_call:
        return first if first is not None else url_kw

    # Any call that names the method: client.request("DELETE", url),
    # client.build_request(method="delete", url=url), httpx.Request("DELETE", url).
    method = _keyword(call, "method")
    positional_url = None
    if method is None and first is not None:
        method = first
        positional_url = call.args[1] if len(call.args) > 1 else None
    elif method is not None:
        positional_url = first
    if method is not None and any(m.upper() == "DELETE" for m in _render(method, scopes)):
        return positional_url if positional_url is not None else url_kw
    return None


class _Finder(ast.NodeVisitor):
    """Collects, per module, the guarded deletes and the wrappers that forward a URL."""

    def __init__(self, tree: ast.Module):
        self.module_scope = _Scope(tree)
        self.stack: list[tuple[str, _Scope]] = []
        # (function name, kind, line)
        self.hits: list[tuple[str, str, int]] = []
        # wrapper name to a list of (parameter names in order, url templates with params)
        self.wrappers: dict[str, list[tuple[list[str], set[str]]]] = {}
        # (enclosing function, callee name, call node, scopes at the call)
        self.calls: list[tuple[str, str, ast.Call, list[_Scope]]] = []
        self.visit(tree)

    def _scopes(self) -> list[_Scope]:
        return [scope for _name, scope in reversed(self.stack)] + [self.module_scope]

    def _where(self) -> str:
        return self.stack[-1][0] if self.stack else "<module>"

    def _visit_function(self, node) -> None:
        name = getattr(node, "name", "<lambda>")
        self.stack.append((name, _Scope(node)))
        self.generic_visit(node)
        self.stack.pop()

    visit_FunctionDef = _visit_function
    visit_AsyncFunctionDef = _visit_function
    visit_Lambda = _visit_function

    def visit_Call(self, node: ast.Call) -> None:
        scopes = self._scopes()
        url = _delete_url(node, scopes)
        if url is not None:
            templates = _render(url, scopes)
            kinds = {kind for kind in map(_kind_of, templates) if kind}
            for kind in sorted(kinds):
                self.hits.append((self._where(), kind, node.lineno))
            if not kinds and self.stack and any(_PARAM_RE.search(t) for t in templates):
                func_node = self.stack[-1][1].node
                if isinstance(func_node, ast.FunctionDef | ast.AsyncFunctionDef):
                    params = [a.arg for a in (*func_node.args.posonlyargs, *func_node.args.args)]
                    self.wrappers.setdefault(func_node.name, []).append((params, templates))
        if isinstance(node.func, ast.Name):
            self.calls.append((self._where(), node.func.id, node, scopes))
        self.generic_visit(node)

    def resolve_wrapper_calls(self) -> None:
        """Flag a call that hands a local delete wrapper a guarded URL."""
        for where, callee, call, scopes in self.calls:
            for params, templates in self.wrappers.get(callee, []):
                bound: dict[str, set[str]] = {}
                for index, arg in enumerate(call.args):
                    if index < len(params):
                        bound[params[index]] = _render(arg, scopes)
                for kw in call.keywords:
                    if kw.arg:
                        bound[kw.arg] = _render(kw.value, scopes)
                kinds: set[str] = set()
                for template in templates:
                    names = _PARAM_RE.findall(template)
                    options = [sorted(bound.get(name, {_HOLE})) for name in names]
                    for combo in itertools.islice(itertools.product(*options), _MAX_TEMPLATES):
                        filled = template
                        for name, value in zip(names, combo, strict=True):
                            filled = filled.replace(_PARAM.format(name), value, 1)
                        kind = _kind_of(filled)
                        if kind:
                            kinds.add(kind)
                for kind in sorted(kinds):
                    self.hits.append((where, kind, call.lineno))


def find_guarded_deletes(source: str) -> list[tuple[str, str, int]]:
    """(enclosing function, kind, line) for every device, port, or topology delete.

    `kind` is "device", "port", or "topology"; the function is "<module>" for a call
    at module level. A call to one of the checked helpers is not a delete here: the
    DELETE is sent inside the helper module, which the allowlist exempts by name.
    """
    finder = _Finder(ast.parse(source))
    finder.resolve_wrapper_calls()
    return sorted(set(finder.hits), key=lambda hit: (hit[2], hit[0], hit[1]))


# --- the detector flags every spelling ---------------------------------------------

_MUST_FLAG = {
    "f-string": (
        "async def t(admin_client, switch):\n"
        "    await admin_client.delete(f\"/inventory/devices/{switch['id']}\")\n",
        [("t", "device", 2)],
    ),
    "f-string with the base url in front": (
        "async def t(client, base_url, port_id):\n"
        '    await client.delete(f"{base_url}/inventory/ports/{port_id}")\n',
        [("t", "port", 2)],
    ),
    "answer assigned but still a bare delete": (
        "async def t(client, topology_id):\n"
        '    resp = await client.delete(f"/cabling/topologies/{topology_id}")\n'
        "    assert resp.status_code == 204\n",
        [("t", "topology", 2)],
    ),
    "concatenation": (
        "async def t(client, device_id):\n"
        '    await client.delete("/inventory/devices/" + device_id)\n',
        [("t", "device", 2)],
    ),
    "str.format": (
        "async def t(client, port_id):\n"
        '    await client.delete("/inventory/ports/{}".format(port_id))\n',
        [("t", "port", 2)],
    ),
    "percent formatting": (
        "async def t(client, topology_id):\n"
        '    await client.delete("/cabling/topologies/%s" % topology_id)\n',
        [("t", "topology", 2)],
    ),
    "join": (
        "async def t(client, device_id):\n"
        '    await client.delete("/".join(["/inventory", "devices", device_id]))\n',
        [("t", "device", 2)],
    ),
    "url in a variable": (
        "async def t(client, device_id):\n"
        '    url = f"/inventory/devices/{device_id}"\n'
        "    await client.delete(url)\n",
        [("t", "device", 3)],
    ),
    "url through two variables": (
        "async def t(client, device_id):\n"
        '    base = "/inventory/devices"\n'
        '    url = base + "/" + str(device_id)\n'
        "    await client.delete(url)\n",
        [("t", "device", 4)],
    ),
    "module-level path constant": (
        'DEVICES = "/inventory/devices"\n'
        "async def t(client, device_id):\n"
        '    await client.delete(f"{DEVICES}/{device_id}")\n',
        [("t", "device", 3)],
    ),
    "url keyword": (
        "async def t(client, port_id):\n"
        '    await client.delete(url=f"/inventory/ports/{port_id}")\n',
        [("t", "port", 2)],
    ),
    "aliased client": (
        "async def t(admin_client, device_id):\n"
        "    c = admin_client\n"
        '    await c.delete(f"/inventory/devices/{device_id}")\n',
        [("t", "device", 3)],
    ),
    "bound delete under another name": (
        "async def t(admin_client, device_id):\n"
        "    rm = admin_client.delete\n"
        '    await rm(f"/inventory/devices/{device_id}")\n',
        [("t", "device", 3)],
    ),
    "getattr": (
        "async def t(client, device_id):\n"
        '    await getattr(client, "delete")(f"/inventory/devices/{device_id}")\n',
        [("t", "device", 2)],
    ),
    "request with the method named": (
        "async def t(client, device_id):\n"
        '    await client.request("DELETE", f"/inventory/devices/{device_id}")\n',
        [("t", "device", 2)],
    ),
    "request with keywords and a lower-case method": (
        "async def t(client, topology_id):\n"
        '    await client.request(method="delete", url=f"/cabling/topologies/{topology_id}")\n',
        [("t", "topology", 2)],
    ),
    "method in a variable": (
        'VERB = "DELETE"\n'
        "async def t(client, port_id):\n"
        '    await client.request(VERB, f"/inventory/ports/{port_id}")\n',
        [("t", "port", 3)],
    ),
    "built request": (
        "async def t(client, device_id):\n"
        '    req = client.build_request("DELETE", f"/inventory/devices/{device_id}")\n'
        "    await client.send(req)\n",
        [("t", "device", 2)],
    ),
    "wrapper that forwards the path": (
        "async def _rm(client, path):\n"
        "    return await client.delete(path)\n"
        "async def t(client, port_id):\n"
        '    await _rm(client, f"/inventory/ports/{port_id}")\n',
        [("t", "port", 4)],
    ),
    "wrapper that forwards the path by keyword": (
        "async def _rm(client, path):\n"
        "    return await client.delete(path)\n"
        "async def t(client, device_id):\n"
        '    await _rm(client, path="/inventory/devices/" + device_id)\n',
        [("t", "device", 4)],
    ),
    "wrapper that builds the url from an id": (
        "async def _rm(client, kind, ident):\n"
        '    return await client.delete(f"/inventory/{kind}/{ident}")\n'
        "async def t(client, device_id):\n"
        '    await _rm(client, "devices", device_id)\n',
        [("t", "device", 4)],
    ),
    "helper with the path written inside it": (
        "async def _delete_when_released(client, device_id):\n"
        '    resp = await client.delete(f"/inventory/devices/{device_id}")\n'
        "    return resp\n",
        [("_delete_when_released", "device", 2)],
    ),
    "fixture teardown after the yield": (
        "async def gating_device(client):\n"
        "    device = {}\n"
        "    yield device\n"
        "    await client.delete(f\"/inventory/devices/{device['id']}\")\n",
        [("gating_device", "device", 4)],
    ),
    "module level": (
        'import httpx\nhttpx.delete("https://localhost/api/inventory/devices/" + "abc")\n',
        [("<module>", "device", 2)],
    ),
    "trailing slash": (
        "async def t(client, device_id):\n"
        '    await client.delete(f"/inventory/devices/{device_id}/")\n',
        [("t", "device", 2)],
    ),
}

_MUST_PASS = {
    "the checked helpers": (
        "async def t(client, device, port_id, topology_id):\n"
        '    await delete_device_checked(client, device["id"])\n'
        '    await delete_devices_checked(client, [device["id"]])\n'
        "    await delete_port_checked(client, port_id)\n"
        "    await delete_topology_checked(client, topology_id)\n"
    ),
    "other verbs on the same paths": (
        "async def t(client, device_id):\n"
        '    await client.get(f"/inventory/devices/{device_id}")\n'
        '    await client.put(f"/inventory/ports/{device_id}", json={})\n'
        '    await client.post("/inventory/devices", json={})\n'
        '    await client.request("GET", f"/cabling/topologies/{device_id}")\n'
        '    await client.request("PUT", f"/inventory/devices/{device_id}", json={})\n'
    ),
    "deletes the contract does not cover": (
        "async def t(client, x):\n"
        '    await client.delete(f"/reservations/{x}")\n'
        '    await client.delete(f"/cabling/connections/{x}")\n'
        '    await client.delete(f"/inventory/templates/{x}")\n'
        '    await client.delete(f"/inventory/drivers/{x}")\n'
        '    await client.delete(f"/inventory/device-groups/{x}")\n'
        '    await client.delete(f"/auth/groups/{x}")\n'
        '    await client.delete(f"/v1/webhooks/{x}")\n'
    ),
    "a sub-resource of a device is not the device": (
        "async def t(client, device_id, job_id):\n"
        '    await client.delete(f"/inventory/devices/{device_id}/apply-jobs/{job_id}")\n'
    ),
    "a wrapper used for something else": (
        "async def _rm(client, path):\n"
        "    return await client.delete(path)\n"
        "async def t(client, rid):\n"
        '    await _rm(client, f"/reservations/{rid}")\n'
    ),
    "a url the scan cannot see is not guessed at": (
        "async def t(client, urls):\n    await client.delete(urls[0])\n"
    ),
    "the path only mentioned in a string or a comment": (
        "async def t(client):\n"
        '    note = "never client.delete(/inventory/devices/x) by hand"\n'
        '    # await client.delete(f"/inventory/devices/{x}")\n'
        "    return note\n"
    ),
    "not a delete: a set method that happens to share the name is given no url": (
        "def t(cache):\n    cache.delete()\n"
    ),
}


@pytest.mark.parametrize("name", sorted(_MUST_FLAG))
def test_detector_flags_every_spelling_of_a_bare_delete(name):
    source, expected = _MUST_FLAG[name]
    assert find_guarded_deletes(source) == expected


@pytest.mark.parametrize("name", sorted(_MUST_PASS))
def test_detector_passes_what_the_contract_allows(name):
    assert find_guarded_deletes(_MUST_PASS[name]) == []


def test_detector_reports_each_kind_and_each_call_once():
    source = (
        "async def t(client, d, p, topo):\n"
        '    await client.delete(f"/inventory/devices/{d}")\n'
        '    await client.delete(f"/inventory/devices/{d}")\n'
        '    await client.delete(f"/inventory/ports/{p}")\n'
        '    await client.delete(f"/cabling/topologies/{topo}")\n'
    )
    assert find_guarded_deletes(source) == [
        ("t", "device", 2),
        ("t", "device", 3),
        ("t", "port", 4),
        ("t", "topology", 5),
    ]


# --- the allowlist -----------------------------------------------------------------

# (file, function, kind) to (how many such deletes the function holds, why it may).
# Two groups only: the helper module functions that ARE the checked delete, and the
# tests whose subject is the delete's own answer (each one reads and asserts it).
ALLOWED = {
    ("_device_teardown.py", "delete_device_checked", "device"): (
        1,
        "the checked device delete itself",
    ),
    ("_device_teardown.py", "delete_port_checked", "port"): (
        1,
        "the checked port delete itself",
    ),
    ("_topology_teardown.py", "delete_topology_checked", "topology"): (
        1,
        "the checked topology delete itself",
    ),
    (
        "test_device_delete_guard.py",
        "test_delete_blocked_by_active_reservation_then_succeeds_after_cancel",
        "device",
    ): (2, "subject: the 409 device_in_use, then the 204 after the cancel; both asserted"),
    (
        "test_device_delete_guard.py",
        "test_delete_succeeds_for_unreserved_device",
        "device",
    ): (1, "subject: the plain 204; asserted"),
    ("test_device_delete_guard.py", "_delete_when_released", "device"): (
        2,
        "subject helper: retries while the guard refuses and returns the answer, "
        "which its one caller asserts is 204",
    ),
    (
        "test_device_delete_guard.py",
        "test_transit_switch_on_live_fork_blocks_delete_until_cancel",
        "device",
    ): (1, "subject: the 409 device_in_use for a transit-only switch; asserted"),
    (
        "test_device_delete_guard.py",
        "test_cabled_unreserved_device_blocks_delete_until_cables_removed",
        "device",
    ): (2, "subject: the 409 device_cabled per end, then the 204; both asserted"),
    (
        "test_port_cabling_guard.py",
        "test_cabled_port_refuses_delete_and_rename_until_the_cable_is_removed",
        "port",
    ): (
        2,
        "subject: the 409 port_cabled, then the 204 once the cable is gone; both asserted",
    ),
    ("test_topology_delete_guard.py", "_assert_refused", "topology"): (
        1,
        "subject helper: asserts the 409 topology_in_use and its body",
    ),
    ("test_topology_delete_guard.py", "_assert_deleted", "topology"): (
        1,
        "subject helper: asserts the 204 and the 404 that follows",
    ),
    (
        "test_topology_delete_guard.py",
        "test_non_owner_gets_403_not_the_reservation_ids",
        "topology",
    ): (1, "subject: the non-owner's 403; asserted"),
}


def _scan(directory: Path) -> dict[tuple[str, str, str], list[int]]:
    found: dict[tuple[str, str, str], list[int]] = {}
    for path in sorted(directory.glob("*.py")):
        for function, kind, line in find_guarded_deletes(path.read_text()):
            found.setdefault((path.name, function, kind), []).append(line)
    return found


def judge(found: dict[tuple[str, str, str], list[int]], allowed: dict) -> list[str]:
    """Every way `found` departs from `allowed`, as one line each."""
    problems: list[str] = []
    for key, lines in sorted(found.items()):
        file, function, kind = key
        where = f"{file}::{function} ({kind}, line {', '.join(map(str, lines))})"
        if key not in allowed:
            problems.append(f"bare delete, use the checked teardown helper: {where}")
        elif len(lines) != allowed[key][0]:
            problems.append(
                f"{len(lines)} bare deletes where {allowed[key][0]} are exempt, "
                f"convert the new one or correct the count: {where}"
            )
    for key in sorted(set(allowed) - set(found)):
        file, function, kind = key
        problems.append(f"stale exemption, nothing matches it: {file}::{function} ({kind})")
    return problems


def test_integration_tests_delete_devices_ports_and_topologies_through_the_helpers():
    assert INTEGRATION_DIR.is_dir()
    problems = judge(_scan(INTEGRATION_DIR), ALLOWED)
    assert problems == [], "\n".join(problems)


def test_every_exemption_names_an_existing_file_and_carries_a_reason():
    for (file, _function, kind), (count, reason) in ALLOWED.items():
        assert (INTEGRATION_DIR / file).is_file(), file
        assert kind in GUARDED_PATHS.values()
        assert count >= 1 and reason.strip()


def test_judge_fails_on_a_new_site_a_grown_count_and_a_stale_exemption(tmp_path):
    """The judge on a made-up directory: every departure from the allowlist is named."""
    (tmp_path / "test_subject.py").write_text(
        "async def test_refusal(client, d):\n"
        '    resp = await client.delete(f"/inventory/devices/{d}")\n'
        "    assert resp.status_code == 409\n"
    )
    allowed = {("test_subject.py", "test_refusal", "device"): (1, "subject")}
    assert judge(_scan(tmp_path), allowed) == []

    # A second bare delete added to the exempt function: the count no longer matches.
    (tmp_path / "test_subject.py").write_text(
        "async def test_refusal(client, d):\n"
        '    resp = await client.delete(f"/inventory/devices/{d}")\n'
        "    assert resp.status_code == 409\n"
        '    await client.delete(f"/inventory/devices/{d}")\n'
    )
    assert judge(_scan(tmp_path), allowed) == [
        "2 bare deletes where 1 are exempt, convert the new one or correct the count: "
        "test_subject.py::test_refusal (device, line 2, 4)"
    ]

    # A bare delete in another function of the same file, and in a new file.
    (tmp_path / "test_subject.py").write_text(
        "async def test_refusal(client, d):\n"
        '    resp = await client.delete(f"/inventory/devices/{d}")\n'
        "    assert resp.status_code == 409\n"
        "async def test_other(client, p):\n"
        '    await client.delete(f"/inventory/ports/{p}")\n'
    )
    (tmp_path / "test_new.py").write_text(
        "async def test_leak(client, t):\n"
        '    await client.request("DELETE", "/cabling/topologies/" + t)\n'
    )
    assert judge(_scan(tmp_path), allowed) == [
        "bare delete, use the checked teardown helper: test_new.py::test_leak (topology, line 2)",
        "bare delete, use the checked teardown helper: test_subject.py::test_other (port, line 5)",
    ]

    # The exempt delete is converted: its exemption is stale.
    (tmp_path / "test_new.py").unlink()
    (tmp_path / "test_subject.py").write_text(
        "async def test_refusal(client, d):\n    await delete_device_checked(client, d)\n"
    )
    assert judge(_scan(tmp_path), allowed) == [
        "stale exemption, nothing matches it: test_subject.py::test_refusal (device)"
    ]


# --- an exempt helper that hands the answer back -------------------------------------

# Exempt helpers that RETURN the delete's answer instead of asserting it themselves.
# Their exemption holds only while every caller reads that answer, so a call whose
# result is dropped is the bare ignored delete again, one call removed.
ANSWER_RETURNING_HELPERS = {"test_device_delete_guard.py": {"_delete_when_released"}}


def find_discarded_answers(source: str, helpers: set[str]) -> list[int]:
    """Lines where a call to one of `helpers` is a statement whose value is dropped."""
    lines = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Expr):
            continue
        value = node.value.value if isinstance(node.value, ast.Await) else node.value
        if (
            isinstance(value, ast.Call)
            and isinstance(value.func, ast.Name)
            and value.func.id in helpers
        ):
            lines.append(node.lineno)
    return sorted(lines)


def test_discarded_answer_detector_flags_the_dropped_call_only():
    helpers = {"_delete_when_released"}
    dropped = (
        "async def t(client, switch):\n"
        "    try:\n"
        "        pass\n"
        "    finally:\n"
        '        await _delete_when_released(client, switch["id"])\n'
    )
    assert find_discarded_answers(dropped, helpers) == [5]
    read = (
        "async def t(client, switch):\n"
        '    deleted = await _delete_when_released(client, switch["id"])\n'
        "    assert deleted.status_code == 204\n"
        '    assert (await _delete_when_released(client, switch["id"])).status_code == 204\n'
        '    return await _delete_when_released(client, switch["id"])\n'
    )
    assert find_discarded_answers(read, helpers) == []


def test_no_caller_drops_the_answer_of_an_exempt_helper():
    for file, helpers in ANSWER_RETURNING_HELPERS.items():
        for helper in helpers:
            assert any(key[:2] == (file, helper) for key in ALLOWED), (
                f"{file}::{helper} is not an exempt helper any more; drop it here too"
            )
        dropped = find_discarded_answers((INTEGRATION_DIR / file).read_text(), helpers)
        assert dropped == [], f"{file}: the delete's answer is dropped at line {dropped}"
