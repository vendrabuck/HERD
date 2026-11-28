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

import gc
import uuid
import warnings

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
# The default plus the import file bound `IMPORT_UPLOAD_BYTES`, which the route now
# enforces itself (#1417). It is read at each request, so it is shrunk here to keep
# the bodies small.

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


# -- However an import body is sent (issue #1417) -------------------------------------
#
# The limit counts what the app reads, so an import body over the route's limit is the
# 413 whether it declares its length (refused before one byte is read), sends it
# chunked with no length, or declares less than it sends; and the app has read at most
# the limit plus the one chunk that crossed it. An urlencoded body is read as a form,
# through the limit; any other body that is not a form is not read at all (the
# surrogate check reads a JSON body only when the app does). `_Pulled` counts the bytes
# the app asked for.

_IMPORT_PATH = "/topologies/import"
_CHUNK = 4096
_UNCLOSED_SPOOL = "Unclosed file <tempfile.SpooledTemporaryFile"


class _Pulled:
    """A request body sent in `_CHUNK` pieces, counting the bytes the app pulled."""

    def __init__(self, body: bytes) -> None:
        self.body = body
        self.pulled = 0

    async def __aiter__(self):
        for start in range(0, len(self.body), _CHUNK):
            piece = self.body[start : start + _CHUNK]
            self.pulled += len(piece)
            yield piece


def _multipart(file_bytes: bytes) -> tuple[bytes, dict[str, str]]:
    import httpx

    request = httpx.Request(
        "POST", "http://test/", files={"file": ("t.json", file_bytes, "application/json")}
    )
    return request.read(), {"content-type": request.headers["content-type"]}


@pytest.fixture
def small_import_bound(monkeypatch):
    from app.routes import bulk

    monkeypatch.setattr(bulk, "IMPORT_UPLOAD_BYTES", _UPLOAD_BYTES)


@pytest.mark.usefixtures("small_import_bound")
@pytest.mark.parametrize("sent", ["declared", "chunked", "understated"])
async def test_an_import_body_over_its_limit_is_413_however_it_is_sent(sent):
    route_limit = LIMIT + _UPLOAD_BYTES
    body, headers = _multipart(b"z" * (route_limit + 8 * _CHUNK))
    if sent == "declared":
        headers["content-length"] = str(len(body))
    elif sent == "understated":
        headers["content-length"] = "100"
    stream = _Pulled(body)
    # Starlette's multipart parser closes the spooled file it made only when the parse
    # fails with its own error, not when the read under it is refused, so a body refused
    # mid-upload leaves its file to the garbage collector, which warns. That warning is
    # expected here and collected here, never in a later test.
    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", _UNCLOSED_SPOOL, ResourceWarning)
        async with _client() as client:
            resp = await client.post(_IMPORT_PATH, content=stream, headers=headers)
        gc.collect()
    assert resp.status_code == 413, resp.text[:300]
    assert resp.json() == {"detail": [body_too_large_error(route_limit)]}
    if sent == "declared":
        assert stream.pulled == 0
    else:
        assert route_limit < stream.pulled <= route_limit + _CHUNK


@pytest.mark.usefixtures("small_import_bound")
async def test_a_raw_form_body_over_the_import_limit_is_413_and_read_no_further():
    route_limit = LIMIT + _UPLOAD_BYTES
    stream = _Pulled(b"file=" + b"z" * (route_limit + 8 * _CHUNK))
    async with _client() as client:
        resp = await client.post(
            _IMPORT_PATH,
            content=stream,
            headers={"content-type": "application/x-www-form-urlencoded"},
        )
    assert resp.status_code == 413, resp.text[:300]
    assert resp.json() == {"detail": [body_too_large_error(route_limit)]}
    assert route_limit < stream.pulled <= route_limit + _CHUNK


@pytest.mark.usefixtures("small_import_bound")
@pytest.mark.parametrize("content_type", ["application/json", "application/octet-stream"])
async def test_a_raw_body_that_is_not_a_form_is_never_read(content_type):
    """Nothing of it is buffered, and the route's authentication answers (no
    credentials, so 401 or 403)."""
    stream = _Pulled(b"[" + b" " * (LIMIT + _UPLOAD_BYTES + 8 * _CHUNK) + b"]")
    async with _client() as client:
        resp = await client.post(
            _IMPORT_PATH, content=stream, headers={"content-type": content_type}
        )
    assert resp.status_code in (401, 403), resp.text[:300]
    assert stream.pulled == 0


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
    """A canvas in the shape the topology editor's Save sends (`persistableCanvas` in
    `frontend/src/pages/TopologyEditorPage.tsx`), field by field, with realistic values.
    The body is `{"canvas_data": canvas}` as JSON.stringify writes it (compact)."""

    def node(i: int) -> dict:
        return {
            # genId() in frontend/src/lib/id.ts: crypto.randomUUID().
            "id": str(uuid.UUID(int=i + 1)),
            # TopologyEditorPage.tsx onDrop: type "deviceNode", the drop position (a
            # dragged node's coordinates are doubles with up to 17 digits).
            "type": "deviceNode",
            "position": {"x": 1234.5678901234567, "y": 987.6543210987654},
            "data": {
                # persistableDevice in frontend/src/lib/canvasNodes.ts: the seven
                # PERSISTABLE_DEVICE_KEYS of the device record.
                "device": {
                    "id": str(uuid.UUID(int=10**6 + i)),
                    "name": f"lab-rack{i // 40:02d}-leaf-switch-{i:04d}",
                    "topology_type": "PHYSICAL",
                    "connection_type": "Layer 2 Switch",
                    "status": "AVAILABLE",
                    "template_name": "Arista 7050SX3-48YC8 Leaf Switch",
                    "template_icon": "switch",
                },
                # TopologyEditorPage.tsx onDrop: label and topologyType beside device.
                "label": f"lab-rack{i // 40:02d}-leaf-switch-{i:04d}",
                "topologyType": "PHYSICAL",
            },
            # @xyflow/react 12 keeps these on every node in the store, and
            # persistableCanvasNodes passes the node through whole.
            "measured": {"width": 196, "height": 72},
            "selected": False,
            "dragging": False,
        }

    def edge(j: int) -> dict:
        return {
            # buildEnrichedEdge in frontend/src/stores/topologyStore.ts: genId(), the
            # two node ids, the DeviceNode handle ids, type "layerEdge".
            "id": str(uuid.UUID(int=2 * 10**6 + j)),
            "source": str(uuid.UUID(int=j % devices + 1)),
            "target": str(uuid.UUID(int=(j * 7 + 1) % devices + 1)),
            "sourceHandle": "bottom",
            "targetHandle": "top",
            "type": "layerEdge",
            # handleWiringConfirm in TopologyEditorPage.tsx (the wiring dialog's
            # per-line data, the same keys QuickConnectPopover.tsx sends), then
            # pathValid and pathHopCount from the store's path updates
            # (topologyStore.ts); stripTransientEdgeFields drops only selected,
            # animated, style, zIndex, and serverInvalidReason.
            "data": {
                "layer": "L1",
                "source_port_id": str(uuid.UUID(int=3 * 10**6 + j)),
                "source_port_name": f"Ethernet{j % 48 + 1}/1",
                "target_port_id": str(uuid.UUID(int=4 * 10**6 + j)),
                "target_port_name": f"Ethernet{j % 52 + 1}/1",
                "portsCabled": True,
                "pathValid": True,
                "pathHopCount": 1,
            },
        }

    return {
        "nodes": [node(i) for i in range(devices)],
        "edges": [edge(j) for j in range(links)],
        "selectedEdgeLayer": "L1",
    }


# The measures, pinned: 1,000 devices and 4,000 links; a 250-device reservation's canvas
# with 1,000 links; and the most devices, with four links each, the canvas routes' limit
# at the defaults (4456448 bytes) holds (1,776 within the 4 MiB allowance alone).
CANVAS_1000_4000_BYTES = 2_361_613
CANVAS_250_1000_BYTES = 590_443
CANVAS_CAPACITY = 1_887


def _canvas_put_bytes(devices: int, links: int) -> int:
    import json

    body = json.dumps({"canvas_data": _editor_canvas(devices, links)}, separators=(",", ":"))
    return len(body.encode("utf-8"))


def test_a_generated_large_canvas_is_over_the_default_and_under_the_allowance():
    """The measure behind the canvas routes' mark, in the editor's save shape: 1,000
    devices and 4,000 links need more than the default and fit the large-JSON limit;
    a 250-device reservation's canvas with 1,000 links is already over the default;
    and the limit holds up to 1,887 devices with four links each."""
    from herd_common.validation_errors import (
        DEFAULT_MAX_REQUEST_BODY_BYTES,
        LARGE_JSON_BODY_BYTES,
    )

    large_limit = DEFAULT_MAX_REQUEST_BODY_BYTES + LARGE_JSON_BODY_BYTES
    size = _canvas_put_bytes(1000, 4000)
    assert DEFAULT_MAX_REQUEST_BODY_BYTES < size < large_limit
    assert size == CANVAS_1000_4000_BYTES
    assert _canvas_put_bytes(250, 1000) == CANVAS_250_1000_BYTES > DEFAULT_MAX_REQUEST_BODY_BYTES
    assert _canvas_put_bytes(CANVAS_CAPACITY, 4 * CANVAS_CAPACITY) <= large_limit
    assert _canvas_put_bytes(CANVAS_CAPACITY + 1, 4 * CANVAS_CAPACITY + 4) > large_limit


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
