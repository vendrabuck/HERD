"""cabling bounds every request body (issue #1466, OPS-VALID-9).

`app/main.py` passes `settings.max_request_body_bytes` (`MAX_REQUEST_BODY_BYTES`) to
`install_validation_errors`, which installs `LimitRequestBody` just outside the
lone-surrogate check. These requests carry no credentials. A body that is not JSON
answers FastAPI's `json_invalid` at the limit, so it was handed on whole and parsed,
and 413 one byte over it, so it was never parsed; an honest body is refused by the
route's authentication while one over the limit is the 413, so the limit comes
first. The middleware itself is pinned in `services/common/tests/test_request_body_limit.py`.
"""

from __future__ import annotations

import uuid

import pytest
from app.config import settings
from app.main import app
from herd_common.validation_errors import (
    LimitRequestBody,
    RefuseLoneSurrogates,
    body_too_large_error,
)
from httpx import ASGITransport, AsyncClient

LIMIT = settings.max_request_body_bytes
_JSON = {"content-type": "application/json"}
_BODY_ROUTES = [("POST", "/topologies")]
_AUTHENTICATED_ROUTE = ("POST", "/topologies")


def _client() -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


def _refused(resp) -> bool:
    return resp.status_code == 413 and resp.json() == {"detail": [body_too_large_error(LIMIT)]}


def test_the_body_limit_is_installed_just_outside_the_body_check():
    assert [m.cls for m in app.user_middleware[-2:]] == [LimitRequestBody, RefuseLoneSurrogates]
    assert app.user_middleware[-2].kwargs == {"max_body_bytes": LIMIT}


@pytest.mark.parametrize(("method", "path"), _BODY_ROUTES)
async def test_a_body_at_the_limit_is_parsed_and_one_over_it_is_413_unparsed(method, path):
    async with _client() as client:
        at = await client.request(method, path, content=b"x" * LIMIT, headers=_JSON)
        over = await client.request(method, path, content=b"x" * (LIMIT + 1), headers=_JSON)
    assert at.status_code == 422, at.text[:300]
    assert at.json()["detail"][0]["type"] == "json_invalid"
    assert _refused(over), over.text[:300]
    assert "connection" not in over.headers  # the connection stays usable


@pytest.mark.parametrize(("method", "path"), _BODY_ROUTES)
async def test_a_chunked_body_over_the_limit_is_413(method, path):
    async def parts():
        for _ in range(5):
            yield b" " * (LIMIT // 4)

    async with _client() as client:
        resp = await client.request(method, path, content=parts(), headers=_JSON)
    assert _refused(resp), resp.text[:300]


@pytest.mark.parametrize(("method", "path"), _BODY_ROUTES)
@pytest.mark.parametrize("chunked", [False, True], ids=["declared", "chunked"])
async def test_a_lone_surrogate_over_the_limit_is_the_413_not_the_surrogate_422(
    method, path, chunked
):
    """Chunked, there is no Content-Length to refuse early: only a limit outside the
    surrogate check stops it from reading the whole body and answering its 422."""
    head = b'["\\ud800"'

    async def parts():
        yield head
        for _ in range(5):
            yield b" " * (LIMIT // 4)

    body = parts() if chunked else head + b" " * LIMIT + b"]"
    async with _client() as client:
        resp = await client.request(method, path, content=body, headers=_JSON)
    assert _refused(resp), resp.text[:300]


async def test_without_credentials_the_limit_answers_before_authentication():
    method, path = _AUTHENTICATED_ROUTE
    async with _client() as client:
        honest = await client.request(method, path, content=b"{}", headers=_JSON)
        over = await client.request(method, path, content=b"{}" + b" " * LIMIT, headers=_JSON)
    assert honest.status_code in (401, 403), honest.text[:300]
    assert _refused(over), over.text[:300]


# -- The topology import takes its own limit -------------------------------------------
#
# The default plus the import file bound `IMPORT_UPLOAD_BYTES` (the route's own read is
# still unbounded, as inventory's are in issue #1417). It is read at each request, so
# it is shrunk here to keep the bodies small.

_UPLOAD_BYTES = 2000


async def test_the_import_route_takes_its_own_limit(monkeypatch):
    """Over the default but within the route's limit, the body reaches the route's
    authentication (no credentials, so 401 or 403); over the route's limit it is the
    413 naming that limit."""
    from app.routes import bulk

    monkeypatch.setattr(bulk, "IMPORT_UPLOAD_BYTES", _UPLOAD_BYTES)
    within = {"file": ("t.json", b"z" * (LIMIT + 100))}
    over = {"file": ("t.json", b"z" * (LIMIT + _UPLOAD_BYTES + 1))}
    async with _client() as client:
        reached = await client.post("/topologies/import", files=within)
        refused = await client.post("/topologies/import", files=over)
    assert reached.status_code in (401, 403), reached.text[:300]
    assert refused.status_code == 413, refused.text[:300]
    assert refused.json() == {"detail": [body_too_large_error(LIMIT + _UPLOAD_BYTES)]}


def test_the_larger_routes_are_exactly_the_import_the_large_json_routes_and_the_fork_forwards():
    from app.routes import bulk
    from fastapi.routing import iter_route_contexts
    from herd_common.validation_errors import (
        FORWARD_ENVELOPE_BYTES,
        LARGE_JSON_BODY_BYTES,
        route_body_limit,
    )

    limits = {
        (method, route.path): route_body_limit(route, LIMIT)
        for route in iter_route_contexts(app.routes)
        for method in route.methods or ()
    }
    assert bulk.IMPORT_UPLOAD_BYTES == 10 * 1024 * 1024
    large = LIMIT + LARGE_JSON_BODY_BYTES
    assert {k: v for k, v in limits.items() if v != LIMIT} == {
        ("POST", "/topologies/import"): LIMIT + bulk.IMPORT_UPLOAD_BYTES,
        ("PUT", "/topologies/{topology_id}"): large,
        ("POST", "/templates"): large,
        ("PUT", "/templates/{template_id}"): large,
        ("POST", "/connections/bulk"): large,
        ("PUT", "/internal/forks/{reservation_id}/canvas"): large + FORWARD_ENVELOPE_BYTES,
        ("POST", "/internal/forks/{reservation_id}/save"): large + FORWARD_ENVELOPE_BYTES,
    }


# -- The large-JSON routes take the allowance (OPS-VALID-10, issue #1484) ----------------
#
# A canvas save, a template's canvas, and a bulk connection create: the default limit
# plus LARGE_JSON_BODY_BYTES. A body that is not JSON at that limit is handed on whole
# and parsed (json_invalid); one byte more is the 413 naming the route's limit.

_LARGE_JSON_ROUTES = [
    ("PUT", f"/topologies/{uuid.uuid4()}"),
    ("POST", "/templates"),
    ("PUT", f"/templates/{uuid.uuid4()}"),
    ("POST", "/connections/bulk"),
]


@pytest.mark.parametrize(("method", "path"), _LARGE_JSON_ROUTES)
async def test_a_large_json_route_takes_the_allowance(method, path):
    from herd_common.validation_errors import LARGE_JSON_BODY_BYTES

    route_limit = LIMIT + LARGE_JSON_BODY_BYTES
    async with _client() as client:
        over_default = await client.request(
            method, path, content=b"{}" + b" " * LIMIT, headers=_JSON
        )
        at = await client.request(method, path, content=b"x" * route_limit, headers=_JSON)
        over = await client.request(method, path, content=b"x" * (route_limit + 1), headers=_JSON)
    assert over_default.status_code in (401, 403), over_default.text[:300]
    assert at.status_code == 422, at.text[:300]
    assert at.json()["detail"][0]["type"] == "json_invalid"
    assert over.status_code == 413, over.text[:300]
    assert over.json() == {"detail": [body_too_large_error(route_limit)]}
    assert "connection" not in over.headers


def _editor_canvas(devices: int, links: int) -> dict:
    """A canvas in the shape the topology editor saves (persistableCanvasNodes: a device
    node carries the seven allowlisted device keys; a link carries its layer and port
    names), with realistic names."""
    nodes = [
        {
            "id": f"node-{i}",
            "type": "deviceNode",
            "position": {"x": 1234.5678 + i, "y": 987.6543 + i},
            "data": {
                "device": {
                    "id": str(uuid.UUID(int=i)),
                    "name": f"lab-rack{i // 40:02d}-leaf-switch-{i:04d}",
                    "topology_type": "PHYSICAL",
                    "connection_type": "Layer 2 Switch",
                    "status": "AVAILABLE",
                    "template_name": "Arista 7050SX3-48YC8 Leaf Switch",
                    "template_icon": "switch",
                }
            },
        }
        for i in range(devices)
    ]
    edges = [
        {
            "id": f"edge-{j}",
            "source": f"node-{j % devices}",
            "target": f"node-{(j * 7 + 1) % devices}",
            "sourceHandle": "bottom",
            "targetHandle": "top",
            "type": "layerEdge",
            "data": {
                "layer": "L1",
                "source_port_name": f"Ethernet{j % 48 + 1}/1",
                "target_port_name": f"Ethernet{j % 52 + 1}/1",
                "isProposal": False,
            },
        }
        for j in range(links)
    ]
    return {"nodes": nodes, "edges": edges, "selectedEdgeLayer": "L1"}


def test_a_generated_large_canvas_is_over_the_default_and_under_the_allowance():
    """The measure behind the canvas routes' mark: 1,000 devices and 4,000 links in the
    editor's save shape need more than the default and fit the large-JSON limit, with
    room for what #1466 measured with whole device records (about 3.1 MiB)."""
    import json

    from herd_common.validation_errors import (
        DEFAULT_MAX_REQUEST_BODY_BYTES,
        LARGE_JSON_BODY_BYTES,
    )

    body = json.dumps({"canvas_data": _editor_canvas(1000, 4000)}, separators=(",", ":"))
    size = len(body.encode("utf-8"))
    assert (
        DEFAULT_MAX_REQUEST_BODY_BYTES
        < size
        < DEFAULT_MAX_REQUEST_BODY_BYTES + LARGE_JSON_BODY_BYTES
    )
    assert size == 1_272_167
    # A reservation holds at most 250 devices; that canvas alone passes the default.
    reservation = json.dumps({"canvas_data": _editor_canvas(250, 1000)}, separators=(",", ":"))
    assert len(reservation) > DEFAULT_MAX_REQUEST_BODY_BYTES


def _largest_pathfind_batch() -> bytes:
    import json

    from app.schemas.pathfind import MAX_BATCH_PAIRS

    pair = {"source_device_id": str(uuid.uuid4()), "target_device_id": str(uuid.uuid4())}
    body = {"pairs": [pair] * MAX_BATCH_PAIRS}
    return json.dumps(body, separators=(",", ":")).encode()


async def test_the_largest_pathfind_batch_fits_the_default_and_is_not_marked():
    """2,000 pairs (MAX_BATCH_PAIRS) is 236011 bytes as compact JSON (the measure the
    wiring guard records), under the default: the route stays unmarked, and that body
    reaches its authentication."""
    body = _largest_pathfind_batch()
    assert len(body) == 236_011
    assert len(body) <= LIMIT
    async with _client() as client:
        resp = await client.post("/pathfind/batch", content=body, headers=_JSON)
    assert resp.status_code in (401, 403), resp.text[:300]


def test_the_largest_bulk_connection_create_is_over_the_default():
    """200 items at every bound of ConnectionCreate: the measure behind the mark on
    POST /connections/bulk."""
    import json

    from app.schemas.connection import ConnectionBulkCreate, ConnectionCreate

    item = ConnectionCreate(
        device_a_id=uuid.uuid4(),
        port_a="p" * 255,
        device_b_id=uuid.uuid4(),
        port_b="q" * 255,
        connection_type="t" * 50,
        notes="n" * 2000,
    )
    body = ConnectionBulkCreate(items=[item] * 200)
    size = len(json.dumps(body.model_dump(mode="json"), separators=(",", ":")))
    assert size == 544_811
    assert size > LIMIT


# -- A route another service forwards a body to takes the envelope (OPS-VALID-10) ---------
#
# reservations forwards its fork canvas PUT and fork save here, each admitted by a
# large-JSON route there, so these take the large-JSON allowance plus
# FORWARD_ENVELOPE_BYTES.

_FORWARD_ROUTES = [
    ("PUT", f"/internal/forks/{uuid.uuid4()}/canvas"),
    ("POST", f"/internal/forks/{uuid.uuid4()}/save"),
]


@pytest.mark.parametrize(("method", "path"), _FORWARD_ROUTES)
async def test_a_forwarded_body_up_to_the_envelope_is_parsed_and_one_byte_more_is_413(method, path):
    from herd_common.validation_errors import FORWARD_ENVELOPE_BYTES, LARGE_JSON_BODY_BYTES

    route_limit = LIMIT + LARGE_JSON_BODY_BYTES + FORWARD_ENVELOPE_BYTES
    async with _client() as client:
        at = await client.request(method, path, content=b"x" * route_limit, headers=_JSON)
        over = await client.request(method, path, content=b"x" * (route_limit + 1), headers=_JSON)
    assert at.status_code == 422, at.text[:300]
    assert at.json()["detail"][0]["type"] == "json_invalid"
    assert over.status_code == 413, over.text[:300]
    assert over.json() == {"detail": [body_too_large_error(route_limit)]}
