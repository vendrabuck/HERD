"""Route-table helpers for the reservations tests that enumerate a family of routes.

Two families are read from the app rather than from a literal list, so a route added
to either one is tested without editing its tests: the internal routes (RES-INTERNAL-1,
test_reservations.py, #1529) and the fork and wiring routes (RES-FORK-4,
test_fork_and_wiring_routes.py, #1532). Each test file owns its selection rule and
states that rule's limits beside it; this module only reads the table and builds a
request that passes validation.
"""

import re
import uuid
from collections.abc import Callable

from app.main import app
from fastapi.routing import RouteContext, iter_route_contexts


def path_segments(path: str) -> list[str]:
    """The segments of a route path, without its leading and trailing slash."""
    return path.strip("/").split("/")


def routes_where(select: Callable[[str], bool], routes=None) -> list[tuple[str, RouteContext]]:
    """(method, route) for every route of `routes` (the app's by default) whose path
    `select` accepts, for each method it declares except HEAD and OPTIONS.

    The table is read through FastAPI's iter_route_contexts, which flattens included
    routers and does not descend into a Mount; an entry without methods is skipped.
    """
    return sorted(
        (
            (method, route)
            for route in iter_route_contexts(app.routes if routes is None else routes)
            if route.methods is not None and select(route.path)
            for method in route.methods - {"HEAD", "OPTIONS"}
        ),
        key=lambda pair: (pair[1].path, pair[0]),
    )


def otherwise_valid_request(
    route: RouteContext,
    *,
    bodies: dict,
    bodies_name: str,
    where: str,
    path_values: dict[str, str] | None = None,
) -> dict:
    """The URL, query, and body that pass validation for `route`.

    A path parameter named in `path_values` takes that value; every other path or
    query parameter must be a required UUID and gets a fresh one, else the assertion
    names `where` as the place to supply a value. A body model must be a key of
    `bodies` (named `bodies_name` in the assertion), whose value is sent as JSON.
    """
    path_values = path_values or {}
    values = {}
    for kind, params in (
        ("path", route.dependant.path_params),
        ("query", route.dependant.query_params),
    ):
        for param in params:
            if kind == "path" and param.alias in path_values:
                values[(kind, param.alias)] = path_values[param.alias]
                continue
            annotation = param.field_info.annotation
            assert annotation is uuid.UUID and param.field_info.is_required(), (
                f"{route.path}: {kind} parameter {param.alias!r} is {annotation!r}, "
                f"not a required UUID; add a valid value for it in {where}"
            )
            values[(kind, param.alias)] = str(uuid.uuid4())
    url = re.sub(r"\{([^}:]+)[^}]*\}", lambda m: values[("path", m.group(1))], route.path)
    query = {name: value for (kind, name), value in values.items() if kind == "query"}
    request: dict = {"url": url, "params": query}
    for body in route.dependant.body_params:
        model = body.field_info.annotation
        assert model in bodies, (
            f"{route.path}: add a minimal valid {model.__name__} body to {bodies_name}"
        )
        request["json"] = bodies[model]
    return request
