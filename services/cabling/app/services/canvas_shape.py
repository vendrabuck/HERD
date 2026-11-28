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
- on a node: ``id``, ``data.label``, ``data.topologyType``, ``data.device.id``,
  ``data.device.name``, ``data.device.template_name``, ``data.device.role``,
  ``data.device.status``, ``data.device.topology_type``, ``data.device.template_icon``,
  ``data.device.connection_type``, ``data.element.id``, ``data.element.label``, and
  ``data.element.element_type`` are strings; ``data``, ``data.device``, and
  ``data.element`` are objects;
- on an edge: ``id``, ``source``, ``target``, ``data.layer``,
  ``data.source_port_name``, ``data.target_port_name``, ``data.sourcePort``, and
  ``data.targetPort`` are strings; ``data`` is an object; and ``id`` holds no NUL
  character, and, on the write side only, is at most ``MAX_EDGE_ID_LENGTH``
  characters (a fork writes it into each of the edge's wiring rows,
  ``fork_connections.edge_key``, ``String(255)``);
- JSON ``null`` and an absent key pass everywhere a part is named above, and a
  wrong type is refused even when it is empty, zero, or false;
- no object or list is nested deeper than ``MAX_CANVAS_DEPTH`` levels, the canvas
  itself being level 1;
- no number is ``NaN`` or infinite, and no string or key holds a lone surrogate.

Every other key passes as it is: ``data.l3`` keeps its own parser and its own
``l3_malformed`` answer (``l3_intent.py``), and keys only the editor reads
(``position``, ``type``, handles, ``selectedEdgeLayer``) are not cabling's to judge.
A structural problem anywhere is named before a content problem (the depth, the
numbers, the text) anywhere: nodes then edges, items in order, an item's parts in
the order above; then the content problems in canvas order of the top-level keys.
One pass over each list judges an item's named parts and walks its content, so the
check costs one visit of every value; the walk uses a stack, so a canvas nested past
the interpreter's recursion limit is judged, not raised on.

Write side: the request schemas that carry a canvas take ``CanvasData``, whose
validator answers a refused canvas with one 422 item
``{"type": "canvas_shape", "loc": ["body", "canvas_data"], "msg": "<path>: <message>"}``;
the topology import judges each record's canvas with the same function (its own
reason, ``canvas.<path>: <message>``); and fork create judges the parent canvas it
copies with it (``as_written``). The body check runs where pydantic validates the
request body, on the event loop. Read side: a stored canvas need not have the shape,
and is judged by ``stored_shape_problem``, the same check without the edge id's
length bound. ``require_readable_canvas`` answers 409
``STORED_CANVAS_UNREADABLE_DETAIL`` for a route that needs the canvas, and
``readable_canvas_or_none`` gives a reader that can do without the canvas None
instead; both judge through ``stored_canvas_problem``, which keeps a canvas that
costs more than ``INLINE_CHECK_VALUES`` (as the constant's comment counts) off the
event loop, and both log one warning naming the row's kind, its id, and the problem,
never any of the canvas.
"""

from __future__ import annotations

import logging
import math
import re
import sys
import uuid
from dataclasses import dataclass
from typing import Annotated, Any

from fastapi import HTTPException
from herd_common.bulk_work import run_bulk_work
from pydantic import AfterValidator
from pydantic_core import PydanticCustomError

logger = logging.getLogger(__name__)

# The deepest a stored canvas may nest, the canvas object being level 1. What the
# editor, the AI commit, the seed data, and the importers write nests about seven
# levels (canvas, nodes, a node, data, l3, routes, a route). The bound is far above
# that and far below any reader's nesting limit.
MAX_CANVAS_DEPTH = 64

MUST_BE_AN_OBJECT = "must be an object"
MUST_BE_A_LIST = "must be a list"
MUST_BE_A_STRING = "must be a string"
# The width of the column an edge id is written to (``fork_connections.edge_key``).
MAX_EDGE_ID_LENGTH = 255
TOO_LONG_FOR_ITS_COLUMN = f"is longer than {MAX_EDGE_ID_LENGTH} characters"
HOLDS_A_NUL = "holds a NUL character, which no database column takes"
TOO_DEEP = f"is nested deeper than {MAX_CANVAS_DEPTH} levels"
NOT_FINITE = "holds a number that is not finite"
LONE_SURROGATE = "holds a lone surrogate escape, which is not valid text"

# The most values a stored canvas read by a request is judged for on the event loop;
# a canvas that costs more is judged on a worker thread instead
# (``stored_canvas_problem``). ``_judge`` charges each value before it touches it:
# each entry of the canvas itself, each list item, and each container with one for
# each of its entries; and each key or string it searches character by character (one
# that is not ASCII, searched for a lone surrogate; an edge id, searched for a NUL)
# one more for every ``TEXT_CHARS_PER_VALUE`` characters it holds. Beyond that, each
# node or edge costs a fixed number of lookups of its named parts. Measured on one
# core, a canvas at this budget is judged in about 1 ms to about 10 ms, more the more
# nodes and edges it holds (10,000 edges of one key each took the longest); an editor
# canvas of 100 devices and 200 links costs about 5,400.
INLINE_CHECK_VALUES = 20_000
# Searching 16 characters for a lone surrogate takes about as long as judging one
# small value; a string of fewer costs only its place in its container.
TEXT_CHARS_PER_VALUE = 16

# The answer to a request that needs a stored canvas which fails the check.
STORED_CANVAS_UNREADABLE_DETAIL = (
    "Stored canvas cannot be read; save a new canvas to replace it, or delete it"
)

# A surrogate code unit in a Python str is always a lone one: a pair written as two
# JSON escapes decodes to one code point. Only a string that is not ASCII can hold
# one, and `str.isascii` answers in constant time, so the search runs only there.
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


def _not_text(value: str) -> bool:
    """Whether the string ``value`` holds a lone surrogate."""
    return not value.isascii() and _SURROGATE.search(value) is not None


def _node_structure(node: dict, path: tuple[str | int, ...]) -> CanvasProblem | None:
    """The first named part of one node with the wrong type, in the order of the
    module docstring, else None."""
    value = node.get("id")
    if value is not None and type(value) is not str and not isinstance(value, str):
        return CanvasProblem((*path, "id"), MUST_BE_A_STRING)
    data = node.get("data")
    if data is None:
        return None
    if type(data) is not dict and not isinstance(data, dict):
        return CanvasProblem((*path, "data"), MUST_BE_AN_OBJECT)
    if not data:
        return None
    device = data.get("device")
    if device is not None:
        if type(device) is not dict and not isinstance(device, dict):
            return CanvasProblem((*path, "data", "device"), MUST_BE_AN_OBJECT)
        if device:
            for key in _DEVICE_TEXT:
                value = device.get(key)
                if value is not None and type(value) is not str and not isinstance(value, str):
                    return CanvasProblem((*path, "data", "device", key), MUST_BE_A_STRING)
    for key in _NODE_DATA_TEXT:
        value = data.get(key)
        if value is not None and type(value) is not str and not isinstance(value, str):
            return CanvasProblem((*path, "data", key), MUST_BE_A_STRING)
    element = data.get("element")
    if element is None:
        return None
    if type(element) is not dict and not isinstance(element, dict):
        return CanvasProblem((*path, "data", "element"), MUST_BE_AN_OBJECT)
    for key in _ELEMENT_TEXT:
        value = element.get(key)
        if value is not None and type(value) is not str and not isinstance(value, str):
            return CanvasProblem((*path, "data", "element", key), MUST_BE_A_STRING)
    return None


def _written_edge_structure(edge: dict, path: tuple[str | int, ...]) -> CanvasProblem | None:
    """``_edge_structure`` with the write side's bound on the edge id's length,
    judged right after its type, so in the order of the module docstring."""
    edge_id = edge.get("id")
    if isinstance(edge_id, str) and len(edge_id) > MAX_EDGE_ID_LENGTH:
        return CanvasProblem((*path, "id"), TOO_LONG_FOR_ITS_COLUMN)
    return _edge_structure(edge, path)


def _edge_structure(edge: dict, path: tuple[str | int, ...]) -> CanvasProblem | None:
    """The first named part of one edge with the wrong type, or an id holding a
    NUL, in the order of the module docstring, else None."""
    get = edge.get
    edge_id = get("id")
    if edge_id is not None:
        if type(edge_id) is not str and not isinstance(edge_id, str):
            return CanvasProblem((*path, "id"), MUST_BE_A_STRING)
        if "\x00" in edge_id:
            return CanvasProblem((*path, "id"), HOLDS_A_NUL)
    for key in _EDGE_ENDS:
        value = get(key)
        if value is not None and type(value) is not str and not isinstance(value, str):
            return CanvasProblem((*path, key), MUST_BE_A_STRING)
    data = get("data")
    if data is None:
        return None
    if type(data) is not dict and not isinstance(data, dict):
        return CanvasProblem((*path, "data"), MUST_BE_AN_OBJECT)
    if not data:
        return None
    get = data.get
    for key in _EDGE_DATA_TEXT:
        value = get(key)
        if value is not None and type(value) is not str and not isinstance(value, str):
            return CanvasProblem((*path, "data", key), MUST_BE_A_STRING)
    return None


# The string parts checked on a node's `data.device`, its `data`, and its
# `data.element`, on an edge after its `id`, and on an edge's `data`, each in the
# order of the module docstring.
_DEVICE_TEXT = (
    "name",
    "id",
    "template_name",
    "role",
    "status",
    "topology_type",
    "template_icon",
    "connection_type",
)
_NODE_DATA_TEXT = ("label", "topologyType")
_ELEMENT_TEXT = ("id", "label", "element_type")
_EDGE_ENDS = ("source", "target")
_EDGE_DATA_TEXT = ("layer", "source_port_name", "target_port_name", "sourcePort", "targetPort")


class _Unfinished(Exception):
    """The check used up its budget before it reached an answer."""


def _drain(stack: list[tuple[Any, int]], left: int) -> tuple[str | None, int]:
    """Judge every container on ``stack`` (each with its nesting level) and those
    nested in it, the last pushed first: the message of the first problem, else
    None, and what is left of the budget ``left``. Each container costs one plus
    its length, and each key or string that is not ASCII one more for every
    ``TEXT_CHARS_PER_VALUE`` characters, charged before it is searched;
    ``_Unfinished`` is raised when the budget goes below zero.

    For each container: its keys (a lone surrogate), then its depth, then its
    values (a lone surrogate, a number that is not finite), its child containers
    pushed to be judged after it. An empty child container is pushed only when it
    would sit past the bound, since one within it holds nothing to judge. A stack,
    never recursion, so a canvas nested past the interpreter's recursion limit is
    judged, not raised on. On a problem the stack is left as it is."""
    push = stack.append
    pop = stack.pop
    while stack:
        current, level = pop()
        left -= 1 + len(current)
        if left < 0:
            raise _Unfinished
        if type(current) is dict or isinstance(current, dict):
            for key in current:
                if (type(key) is str or isinstance(key, str)) and not key.isascii():
                    left -= len(key) // TEXT_CHARS_PER_VALUE
                    if left < 0:
                        raise _Unfinished
                    if _SURROGATE.search(key) is not None:
                        return LONE_SURROGATE, left
            children: Any = current.values()
        else:
            children = current
        if level > MAX_CANVAS_DEPTH:
            return TOO_DEEP, left
        deeper = level + 1
        at_bound = level >= MAX_CANVAS_DEPTH
        for child in children:
            kind = type(child)
            if kind is str:
                if not child.isascii():
                    left -= len(child) // TEXT_CHARS_PER_VALUE
                    if left < 0:
                        raise _Unfinished
                    if _SURROGATE.search(child) is not None:
                        return LONE_SURROGATE, left
            elif kind is dict or kind is list:
                if child or at_bound:
                    push((child, deeper))
            elif kind is int or kind is bool or child is None:
                continue
            elif kind is float:
                if not math.isfinite(child):
                    return NOT_FINITE, left
            else:
                left -= _text_cost(child)
                if left < 0:
                    raise _Unfinished
                message = _other_message(child)
                if message is not None:
                    return message, left
                if isinstance(child, (dict, list)) and (child or at_bound):
                    push((child, deeper))
    return None, left


def _text_cost(value: Any) -> int:
    """What searching ``value`` character by character costs beyond the value
    itself: one for every ``TEXT_CHARS_PER_VALUE`` characters of a string that is
    not ASCII, else nothing."""
    if isinstance(value, str) and not value.isascii():
        return len(value) // TEXT_CHARS_PER_VALUE
    return 0


def _other_message(value: Any) -> str | None:
    """The message for a scalar of a subclass of ``str`` or ``float`` (never built
    from JSON, only by a Python caller), else None."""
    if isinstance(value, str) and _not_text(value):
        return LONE_SURROGATE
    if isinstance(value, float) and not math.isfinite(value):
        return NOT_FINITE
    return None


def _judge_items(
    items: list, collection: str, structure: Any, left: int
) -> tuple[CanvasProblem | None, CanvasProblem | None, int]:
    """One pass over the items of ``nodes`` or ``edges``: the first structural
    problem (which ends the pass), the first content problem, kept while the pass
    goes on, and what is left of the budget ``left`` (each item costs one plus its
    length; an edge's ``id``, which ``_edge_structure`` searches for a NUL, and each
    key or string of the item that is not ASCII, one more for every
    ``TEXT_CHARS_PER_VALUE`` characters, charged before the search; and its walk
    what ``_drain`` charges). Each item is level 3 (the canvas is level 1, the list
    level 2). Its own keys and values are judged here, as ``_drain`` judges a
    container, and only its non-empty child containers are pushed for ``_drain``,
    so an item with no nested container costs no walk."""
    found: CanvasProblem | None = None
    stack: list[tuple[Any, int]] = []
    push = stack.append
    searched_id = collection == "edges"
    left -= len(items)
    if left < 0:
        raise _Unfinished
    for index, item in enumerate(items):
        if type(item) is not dict and not isinstance(item, dict):
            return CanvasProblem((collection, index), MUST_BE_AN_OBJECT), None, left
        if not item:
            continue
        left -= len(item)
        if searched_id:
            edge_id = item.get("id")
            if isinstance(edge_id, str):
                left -= len(edge_id) // TEXT_CHARS_PER_VALUE
        if left < 0:
            raise _Unfinished
        problem = structure(item, (collection, index))
        if problem is not None:
            return problem, None, left
        if found is not None:
            continue
        message: str | None = None
        for key in item:
            if (type(key) is str or isinstance(key, str)) and not key.isascii():
                left -= len(key) // TEXT_CHARS_PER_VALUE
                if left < 0:
                    raise _Unfinished
                if _SURROGATE.search(key) is not None:
                    message = LONE_SURROGATE
                    break
        if message is None:
            for child in item.values():
                kind = type(child)
                if kind is str:
                    if not child.isascii():
                        left -= len(child) // TEXT_CHARS_PER_VALUE
                        if left < 0:
                            raise _Unfinished
                        if _SURROGATE.search(child) is not None:
                            message = LONE_SURROGATE
                            break
                elif kind is dict or kind is list:
                    if child:
                        push((child, 4))
                elif kind is int or kind is bool or child is None:
                    continue
                elif kind is float:
                    if not math.isfinite(child):
                        message = NOT_FINITE
                        break
                else:
                    left -= _text_cost(child)
                    if left < 0:
                        raise _Unfinished
                    message = _other_message(child)
                    if message is not None:
                        break
                    if isinstance(child, (dict, list)) and child:
                        push((child, 4))
        if message is None and stack:
            message, left = _drain(stack, left)
        if message is not None:
            found = CanvasProblem((collection, index), message)
            stack.clear()
    return None, found, left


def _judge(canvas: Any, left: int, edge_structure: Any) -> CanvasProblem | None:
    """The check, judging each edge's named parts with ``edge_structure`` (the write
    side's ``_written_edge_structure`` or the read side's ``_edge_structure``),
    within a budget of ``left`` values; ``_Unfinished`` when
    the canvas costs more than that. Each entry of the canvas itself costs one,
    charged before any is judged, and a top-level key or scalar string that is not
    ASCII one more for every ``TEXT_CHARS_PER_VALUE`` characters, charged before it
    is searched."""
    if canvas is None:
        return None
    if not isinstance(canvas, dict):
        return CanvasProblem((), MUST_BE_AN_OBJECT)
    left -= len(canvas)
    if left < 0:
        raise _Unfinished
    content: dict[str, CanvasProblem] = {}
    for collection, structure in (("nodes", _node_structure), ("edges", edge_structure)):
        items = canvas.get(collection)
        if items is None:
            continue
        if not isinstance(items, list):
            return CanvasProblem((collection,), MUST_BE_A_LIST)
        problem, found, left = _judge_items(items, collection, structure, left)
        if problem is not None:
            return problem
        if found is not None:
            content[collection] = found
    for key, value in canvas.items():
        if type(key) is not str or not key.isascii():
            left -= _text_cost(key)
            if left < 0:
                raise _Unfinished
            if isinstance(key, str) and _not_text(key):
                return CanvasProblem((), LONE_SURROGATE)
        kind = type(value)
        if (key == "nodes" or key == "edges") and kind is list:
            # Each item was judged on its own above, so the path names it.
            if key in content:
                return content[key]
            continue
        message: str | None = None
        if kind is dict or kind is list:
            # Any other top-level key's name is the caller's, so the path stops
            # at the canvas.
            message, left = _drain([(value, 2)], left)
        elif kind is str:
            if not value.isascii():
                left -= len(value) // TEXT_CHARS_PER_VALUE
                if left < 0:
                    raise _Unfinished
                if _SURROGATE.search(value) is not None:
                    message = LONE_SURROGATE
        elif kind is int or kind is bool or value is None:
            continue
        elif kind is float:
            if not math.isfinite(value):
                message = NOT_FINITE
        elif (key == "nodes" or key == "edges") and isinstance(value, list):
            if key in content:
                return content[key]
            continue
        elif isinstance(value, (dict, list)):
            message, left = _drain([(value, 2)], left)
        else:
            left -= _text_cost(value)
            if left < 0:
                raise _Unfinished
            message = _other_message(value)
        if message is not None:
            return CanvasProblem((), message)
    return None


def canvas_problem(canvas: Any) -> CanvasProblem | None:
    """The first part of ``canvas`` that does not have the shape of the module
    docstring, or None when it has it. A null canvas (no canvas at all) has it.

    One pass over each list's items, nodes then edges, judges each item's named
    parts and walks it for the depth, the numbers, and the text; a structural
    problem anywhere is answered before a content problem anywhere, so the first
    content problem of each list is kept while the pass goes on. Then the top-level
    entries are judged in canvas order. An empty item holds nothing to judge.

    The write side's check: it holds an edge id to ``MAX_EDGE_ID_LENGTH``.
    ``stored_shape_problem`` is the same check without that bound."""
    return _judge(canvas, sys.maxsize, _written_edge_structure)


def stored_shape_problem(canvas: Any) -> CanvasProblem | None:
    """The read side's check: ``canvas_problem`` without the bound on an edge id's
    length, which exists for the column a fork writes the id to, so a stored
    canvas with a longer edge id stays readable, and repairable in the editor."""
    return _judge(canvas, sys.maxsize, _edge_structure)


async def stored_canvas_problem(canvas: Any, *, as_written: bool = False) -> CanvasProblem | None:
    """``stored_shape_problem`` for a stored canvas read by a request, or, with
    ``as_written``, ``canvas_problem`` for a reader that writes the canvas's edge
    ids to a column (fork create). The check starts on the event loop and stops
    there once the canvas has cost more than ``INLINE_CHECK_VALUES`` (as the
    constant's comment counts); such a canvas is then judged whole on a bulk worker
    thread (``herd_common.bulk_work.run_bulk_work``), so the loop is never held for
    the whole check."""
    edge_structure = _written_edge_structure if as_written else _edge_structure
    try:
        return _judge(canvas, INLINE_CHECK_VALUES, edge_structure)
    except _Unfinished:
        return await run_bulk_work(canvas_problem if as_written else stored_shape_problem, canvas)


def _check_canvas_data(value: dict[str, Any]) -> dict[str, Any]:
    problem = canvas_problem(value)
    if problem is not None:
        raise PydanticCustomError("canvas_shape", problem.text("canvas_data"))
    return value


# The type of every request field that carries a canvas to be stored.
CanvasData = Annotated[dict[str, Any], AfterValidator(_check_canvas_data)]


def _log_unreadable(problem: CanvasProblem, *, kind: str, row_id: uuid.UUID | None) -> None:
    logger.warning(
        "stored_canvas_unreadable kind=%s id=%s problem=%s",
        kind,
        row_id,
        problem.text("canvas_data"),
    )


def stored_canvas_readable(canvas: Any, *, kind: str, row_id: uuid.UUID | None) -> bool:
    """Whether a stored canvas has the shape, judged where it is called (the export
    calls it on its bulk worker thread). One that does not is logged at warning
    with the row's kind and id and the problem's path and message, and nothing of
    the canvas."""
    problem = stored_shape_problem(canvas)
    if problem is None:
        return True
    _log_unreadable(problem, kind=kind, row_id=row_id)
    return False


async def require_readable_canvas(
    canvas: Any, *, kind: str, row_id: uuid.UUID | None, as_written: bool = False
) -> Any:
    """``canvas`` itself when it has the shape (``stored_canvas_problem``, with
    ``as_written`` as given); otherwise log it as ``stored_canvas_readable`` does and
    answer 409 ``STORED_CANVAS_UNREADABLE_DETAIL``. For a route that needs the
    stored canvas: its single read, a copy of it, a judgment of it."""
    problem = await stored_canvas_problem(canvas, as_written=as_written)
    if problem is not None:
        _log_unreadable(problem, kind=kind, row_id=row_id)
        raise HTTPException(status_code=409, detail=STORED_CANVAS_UNREADABLE_DETAIL)
    return canvas


async def readable_canvas_or_none(canvas: Any, *, kind: str, row_id: uuid.UUID | None) -> Any:
    """``canvas`` itself when it has the shape (``stored_canvas_problem``);
    otherwise log it as ``stored_canvas_readable`` does and return None. For a
    reader that can proceed without the canvas (a prune)."""
    problem = await stored_canvas_problem(canvas)
    if problem is None:
        return canvas
    _log_unreadable(problem, kind=kind, row_id=row_id)
    return None


__all__ = [
    "CanvasData",
    "CanvasProblem",
    "MAX_CANVAS_DEPTH",
    "MAX_EDGE_ID_LENGTH",
    "STORED_CANVAS_UNREADABLE_DETAIL",
    "INLINE_CHECK_VALUES",
    "canvas_problem",
    "readable_canvas_or_none",
    "require_readable_canvas",
    "stored_canvas_problem",
    "stored_shape_problem",
    "stored_canvas_readable",
]
