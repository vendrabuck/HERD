"""The ONE check of a canvas's shape and bounds, on write and on read.

A canvas (a topology's, a topology version's, a template's, a fork's draft, or a
fork version's ``canvas_data``) is read by cabling's validator, the fork resolver,
import and export, templates, version diff, the response models, and, over HTTP,
by reservations, execution, ai-orchestrator, and the frontend. Every one of them
assumes the shape below. ``canvas_problem`` answers whether a canvas has it, and
names the first part that does not, as a ``CanvasProblem``: a path made only of
the field names listed here and list indexes (never a key or a value the caller
chose) and one fixed message.

The shape:

- the canvas is an object; ``nodes`` and ``edges`` are lists, and every item of
  either is an object;
- on a node: ``id``, ``data.label``, ``data.device.id``, ``data.device.name``,
  ``data.device.template_name``, and ``data.device.role`` are strings; ``data``,
  ``data.device``, and ``data.element`` are objects;
- on an edge: ``id``, ``source``, ``target``, ``data.layer``,
  ``data.source_port_name``, ``data.target_port_name``, ``data.sourcePort``, and
  ``data.targetPort`` are strings; ``data`` is an object;
- JSON ``null`` and an absent key pass everywhere a part is named above, and a
  wrong type is refused even when it is empty, zero, or false;
- no object or list is nested deeper than ``MAX_CANVAS_DEPTH`` levels, the canvas
  itself being level 1;
- no number is ``NaN`` or infinite, and no string or key holds a lone surrogate.

Every other key passes as it is: ``data.l3`` keeps its own parser and its own
``l3_malformed`` answer (``l3_intent.py``), and keys only the editor reads
(``position``, ``type``, handles, ``selectedEdgeLayer``) are not cabling's to judge.
The structural checks run first, nodes then edges in canvas order, and then one
walk of each top-level entry for the depth, the numbers, and the text. The walk
uses a stack, so a canvas nested past the interpreter's recursion limit is judged,
not raised on.

Write side: the request schemas that carry a canvas take ``CanvasData``, whose
validator answers a refused canvas with one 422 item
``{"type": "canvas_shape", "loc": ["body", "canvas_data"], "msg": "<path>: <message>"}``;
the topology import judges each record's canvas with the same function (its own
reason, ``canvas.<path>: <message>``). Read side: a stored canvas written before
this check can still fail it. ``require_readable_canvas`` answers 409
``STORED_CANVAS_UNREADABLE_DETAIL`` for a route that needs the canvas, and
``readable_canvas_or_none`` gives a route that also answers other data (the fork
read) a null canvas instead; both log one warning naming the row's kind, its id,
and the problem, never any of the canvas.
"""

from __future__ import annotations

import logging
import math
import re
import uuid
from dataclasses import dataclass
from typing import Annotated, Any

from fastapi import HTTPException
from pydantic import AfterValidator
from pydantic_core import PydanticCustomError

logger = logging.getLogger(__name__)

# The deepest a stored canvas may nest, the canvas object being level 1. What the
# editor, the AI commit, the seed data, and the importers write nests about seven
# levels (canvas, nodes, a node, data, l3, routes, a route). The bound is far above
# that and far below the response models' serialization limit (about 255 levels,
# counted from the response itself, which holds the canvas a few levels down) and
# the interpreter's recursion limit, so every canvas it admits can be rendered
# again by every reader.
MAX_CANVAS_DEPTH = 64

MUST_BE_AN_OBJECT = "must be an object"
MUST_BE_A_LIST = "must be a list"
MUST_BE_A_STRING = "must be a string"
TOO_DEEP = f"is nested deeper than {MAX_CANVAS_DEPTH} levels"
NOT_FINITE = "holds a number that is not finite"
LONE_SURROGATE = "holds a lone surrogate escape, which is not valid text"

# The answer to a request that needs a stored canvas which fails the check.
STORED_CANVAS_UNREADABLE_DETAIL = (
    "Stored canvas cannot be read; save a new canvas to replace it, or delete it"
)

# The parts of a node and of an edge that a reader dereferences, in the order they
# are checked, each with the type it must have when present and not null.
_NODE_FIELDS: tuple[tuple[tuple[str, ...], type], ...] = (
    (("id",), str),
    (("data",), dict),
    (("data", "device"), dict),
    (("data", "device", "name"), str),
    (("data", "device", "id"), str),
    (("data", "device", "template_name"), str),
    (("data", "device", "role"), str),
    (("data", "label"), str),
    (("data", "element"), dict),
)
_EDGE_FIELDS: tuple[tuple[tuple[str, ...], type], ...] = (
    (("id",), str),
    (("source",), str),
    (("target",), str),
    (("data",), dict),
    (("data", "layer"), str),
    (("data", "source_port_name"), str),
    (("data", "target_port_name"), str),
    (("data", "sourcePort"), str),
    (("data", "targetPort"), str),
)
_TYPE_MESSAGE = {dict: MUST_BE_AN_OBJECT, list: MUST_BE_A_LIST, str: MUST_BE_A_STRING}

# A surrogate code unit in a Python str is always a lone one: a pair written as two
# JSON escapes decodes to one code point.
_SURROGATE = re.compile("[\ud800-\udfff]")


@dataclass(frozen=True)
class CanvasProblem:
    """The first part of a canvas that does not have the shape, and why.

    ``path`` holds only the field names of the module docstring and list indexes;
    ``message`` is one of the fixed messages above.
    """

    path: tuple[str | int, ...]
    message: str

    def text(self, root: str) -> str:
        """``<root>.<path>: <message>``, for example
        ``canvas_data.nodes.2.data: must be an object``."""
        return ".".join([root, *(str(part) for part in self.path)]) + ": " + self.message


def _structure_problem(
    item: Any, path: tuple[str | int, ...], fields: tuple[tuple[tuple[str, ...], type], ...]
) -> CanvasProblem | None:
    """The first named part of one node or edge with the wrong type, else None."""
    if not isinstance(item, dict):
        return CanvasProblem(path, MUST_BE_AN_OBJECT)
    for keys, expected in fields:
        parent: Any = item
        for key in keys[:-1]:
            parent = parent.get(key) if isinstance(parent, dict) else None
        if not isinstance(parent, dict):
            # An absent or null parent; a parent of the wrong type was refused
            # when its own row of the table was checked.
            continue
        value = parent.get(keys[-1])
        if value is not None and not isinstance(value, expected):
            return CanvasProblem((*path, *keys), _TYPE_MESSAGE[expected])
    return None


def _content_problem(value: Any, depth: int, path: tuple[str | int, ...]) -> CanvasProblem | None:
    """Walk ``value`` (at nesting level ``depth``) for a container nested past the
    bound, a number that is not finite, or text holding a lone surrogate. A stack,
    never recursion; only containers are pushed."""
    stack: list[tuple[Any, int]] = [(value, depth)]
    while stack:
        current, level = stack.pop()
        if isinstance(current, dict):
            children: Any = current.values()
            for key in current:
                if isinstance(key, str) and _SURROGATE.search(key):
                    return CanvasProblem(path, LONE_SURROGATE)
        elif isinstance(current, list):
            children = current
        else:
            children = (current,)
            level -= 1
        if level > MAX_CANVAS_DEPTH:
            return CanvasProblem(path, TOO_DEEP)
        for child in children:
            if isinstance(child, (dict, list)):
                stack.append((child, level + 1))
            elif isinstance(child, float):
                if not math.isfinite(child):
                    return CanvasProblem(path, NOT_FINITE)
            elif isinstance(child, str):
                if _SURROGATE.search(child):
                    return CanvasProblem(path, LONE_SURROGATE)
    return None


def canvas_problem(canvas: Any) -> CanvasProblem | None:
    """The first part of ``canvas`` that does not have the shape of the module
    docstring, or None when it has it. A null canvas (no canvas at all) has it."""
    if canvas is None:
        return None
    if not isinstance(canvas, dict):
        return CanvasProblem((), MUST_BE_AN_OBJECT)
    for collection, fields in (("nodes", _NODE_FIELDS), ("edges", _EDGE_FIELDS)):
        items = canvas.get(collection)
        if items is None:
            continue
        if not isinstance(items, list):
            return CanvasProblem((collection,), MUST_BE_A_LIST)
        for index, item in enumerate(items):
            problem = _structure_problem(item, (collection, index), fields)
            if problem is not None:
                return problem
    for key, value in canvas.items():
        if isinstance(key, str) and _SURROGATE.search(key):
            return CanvasProblem((), LONE_SURROGATE)
        if key in ("nodes", "edges") and isinstance(value, list):
            # Each item is judged on its own so the path names it: the canvas is
            # level 1, the list level 2, the item level 3.
            for index, item in enumerate(value):
                problem = _content_problem(item, 3, (key, index))
                if problem is not None:
                    return problem
        else:
            # Any other top-level key's name is the caller's, so the path stops
            # at the canvas.
            problem = _content_problem(value, 2, ())
            if problem is not None:
                return problem
    return None


def _check_canvas_data(value: dict[str, Any]) -> dict[str, Any]:
    problem = canvas_problem(value)
    if problem is not None:
        raise PydanticCustomError("canvas_shape", problem.text("canvas_data"))
    return value


# The type of every request field that carries a canvas to be stored.
CanvasData = Annotated[dict[str, Any], AfterValidator(_check_canvas_data)]


def stored_canvas_readable(canvas: Any, *, kind: str, row_id: uuid.UUID | None) -> bool:
    """Whether a stored canvas has the shape. One that does not is logged at
    warning with the row's kind and id and the problem's path and message, and
    nothing of the canvas."""
    problem = canvas_problem(canvas)
    if problem is None:
        return True
    logger.warning(
        "stored_canvas_unreadable kind=%s id=%s problem=%s",
        kind,
        row_id,
        problem.text("canvas_data"),
    )
    return False


def require_readable_canvas(canvas: Any, *, kind: str, row_id: uuid.UUID | None) -> Any:
    """``canvas`` itself when it has the shape; otherwise log it and answer 409
    ``STORED_CANVAS_UNREADABLE_DETAIL``. For a route that needs the stored canvas:
    its single read, a copy of it, a judgment of it."""
    if not stored_canvas_readable(canvas, kind=kind, row_id=row_id):
        raise HTTPException(status_code=409, detail=STORED_CANVAS_UNREADABLE_DETAIL)
    return canvas


def readable_canvas_or_none(canvas: Any, *, kind: str, row_id: uuid.UUID | None) -> Any:
    """``canvas`` itself when it has the shape; otherwise log it and return None.
    For a reader whose answer carries other data that must still be served (the
    fork read's wiring), or that can proceed without the canvas (a prune)."""
    return canvas if stored_canvas_readable(canvas, kind=kind, row_id=row_id) else None


__all__ = [
    "CanvasData",
    "CanvasProblem",
    "MAX_CANVAS_DEPTH",
    "STORED_CANVAS_UNREADABLE_DETAIL",
    "canvas_problem",
    "readable_canvas_or_none",
    "require_readable_canvas",
    "stored_canvas_readable",
]
