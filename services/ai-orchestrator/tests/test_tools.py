"""Tests for the ROADMAP #16 iter 2 ToolDispatcher."""

import json
import uuid

import httpx
import pytest
from app.services.tools import TOOL_DEFINITIONS, ToolDispatcher

RESERVATION_ID = uuid.UUID("11111111-1111-1111-1111-111111111111")
DEVICE_A = uuid.UUID("33333333-3333-3333-3333-333333333333")
DEVICE_B = uuid.UUID("44444444-4444-4444-4444-444444444444")
TEMPLATE_FW = "tpl-fw"
TEMPLATE_PORT = "tpl-port"
VERSION_ID = "cccccccc-cccc-cccc-cccc-cccccccccccc"

TOKEN = "test-token"


def _make_handler(routes):
    """Construct an httpx.MockTransport handler from a list of (predicate, response) pairs."""

    def handler(request: httpx.Request) -> httpx.Response:
        for predicate, response in routes:
            if predicate(request):
                if callable(response):
                    return response(request)
                return response
        return httpx.Response(404, json={"detail": f"unmocked: {request.method} {request.url}"})

    return handler


def _dispatcher_with(routes, *, char_cap: int = 8000) -> ToolDispatcher:
    transport = httpx.MockTransport(_make_handler(routes))
    client = httpx.AsyncClient(transport=transport)
    return ToolDispatcher(
        token=TOKEN,
        reservation_id=RESERVATION_ID,
        http_client=client,
        char_cap=char_cap,
    )


def _device_payload(device_id, name, template_id=TEMPLATE_FW, with_secret=True):
    field_data = {"management_ip": "10.0.0.1", "vlan": 100}
    if with_secret:
        field_data["admin_password"] = "DO-NOT-LEAK"
    return {
        "id": str(device_id),
        "name": name,
        "template_id": template_id,
        "template_name": "firewall",
        "topology_type": "PHYSICAL",
        "status": "RESERVED",
        "field_data": field_data,
        "exclusive": True,
        "connection_type": "Management",
    }


def _template_payload(template_id, password_keys=("admin_password",)):
    return {
        "id": template_id,
        "name": "firewall",
        "template_type": "device",
        "sections": [
            {
                "name": "Access",
                "fields": [
                    {"key": "management_ip", "label": "Mgmt IP", "type": "string"},
                    {"key": "vlan", "label": "VLAN", "type": "number"},
                    *({"key": k, "label": k, "type": "password"} for k in password_keys),
                ],
            },
        ],
    }


def _port_payload(port_id, name, template_id=TEMPLATE_PORT):
    return {
        "id": port_id,
        "name": name,
        "device_id": str(DEVICE_A),
        "template_id": template_id,
        "template_name": "ethernet-port",
        "exclusive": True,
        "field_data": {"speed_mbps": 1000, "snmp_community": "secret-string"},
    }


# --- Static checks ---


def test_tool_definitions_have_required_fields():
    names = {t["name"] for t in TOOL_DEFINITIONS}
    assert names == {
        "get_device",
        "get_device_ports",
        "get_device_current_config",
        "list_device_config_history",
        "get_device_config_schema",
        "find_path",
        "list_executions_for_reservation",
    }
    for t in TOOL_DEFINITIONS:
        assert "description" in t and t["description"]
        assert t["input_schema"]["type"] == "object"
        assert t["input_schema"].get("additionalProperties") is False


def test_list_executions_schema_has_no_reservation_id_property():
    """Critical safety invariant: model cannot supply a reservation_id."""
    [t] = [t for t in TOOL_DEFINITIONS if t["name"] == "list_executions_for_reservation"]
    assert "reservation_id" not in t["input_schema"]["properties"]


# --- Dispatcher behaviour ---


@pytest.mark.asyncio
async def test_dispatch_unknown_tool_returns_is_error():
    async with _dispatcher_with([]) as d:
        result = await d.dispatch("not_a_tool", {})
    assert result["is_error"] is True
    body = json.loads(result["content"])
    assert "unknown tool" in body["message"]
    assert d.call_log[0].name == "not_a_tool"
    assert d.call_log[0].error and "unknown tool" in d.call_log[0].error


@pytest.mark.asyncio
async def test_dispatch_records_duration_and_args_summary():
    routes = [
        (
            lambda r: r.url.path.endswith(f"/devices/{DEVICE_A}"),
            httpx.Response(200, json=_device_payload(DEVICE_A, "fw-a")),
        ),
        (
            lambda r: r.url.path.endswith(f"/templates/{TEMPLATE_FW}"),
            httpx.Response(200, json=_template_payload(TEMPLATE_FW)),
        ),
    ]
    async with _dispatcher_with(routes) as d:
        await d.dispatch("get_device", {"device_id": str(DEVICE_A)})
    record = d.call_log[0]
    assert record.name == "get_device"
    assert "device_id=" in record.arguments_summary
    assert record.duration_ms >= 0
    assert record.error is None


# --- get_device + password stripping ---


@pytest.mark.asyncio
async def test_get_device_strips_password_field_data():
    routes = [
        (
            lambda r: r.url.path.endswith(f"/devices/{DEVICE_A}"),
            httpx.Response(200, json=_device_payload(DEVICE_A, "fw-a")),
        ),
        (
            lambda r: r.url.path.endswith(f"/templates/{TEMPLATE_FW}"),
            httpx.Response(200, json=_template_payload(TEMPLATE_FW)),
        ),
    ]
    async with _dispatcher_with(routes) as d:
        result = await d.dispatch("get_device", {"device_id": str(DEVICE_A)})
    body = json.loads(result["content"])
    assert body["name"] == "fw-a"
    assert body["field_data"]["management_ip"] == "10.0.0.1"
    assert body["field_data"]["vlan"] == 100
    assert "admin_password" not in body["field_data"]
    assert "DO-NOT-LEAK" not in result["content"]


@pytest.mark.asyncio
async def test_get_device_closed_by_default_when_template_unfetchable():
    """If the template fetch fails, drop field_data wholesale rather than leak."""
    routes = [
        (
            lambda r: r.url.path.endswith(f"/devices/{DEVICE_A}"),
            httpx.Response(200, json=_device_payload(DEVICE_A, "fw-a")),
        ),
        (
            lambda r: r.url.path.endswith(f"/templates/{TEMPLATE_FW}"),
            httpx.Response(500, json={"detail": "boom"}),
        ),
    ]
    async with _dispatcher_with(routes) as d:
        result = await d.dispatch("get_device", {"device_id": str(DEVICE_A)})
    body = json.loads(result["content"])
    assert body["field_data"] == {}
    assert body.get("_field_data_stripped")
    assert "DO-NOT-LEAK" not in result["content"]


@pytest.mark.asyncio
async def test_get_device_missing_device_returns_tool_error():
    routes = [
        (
            lambda r: r.url.path.endswith(f"/devices/{DEVICE_A}"),
            httpx.Response(404),
        ),
    ]
    async with _dispatcher_with(routes) as d:
        result = await d.dispatch("get_device", {"device_id": str(DEVICE_A)})
    assert result["is_error"] is True
    assert "not found" in json.loads(result["content"])["message"]


@pytest.mark.asyncio
async def test_get_device_rejects_non_uuid_arg():
    async with _dispatcher_with([]) as d:
        result = await d.dispatch("get_device", {"device_id": "not-a-uuid"})
    assert result["is_error"] is True
    assert "UUID" in json.loads(result["content"])["message"]


# --- get_device_ports + per-port template cache ---


@pytest.mark.asyncio
async def test_get_device_ports_strips_passwords_using_port_template_cache():
    template_calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith(f"/devices/{DEVICE_A}/ports"):
            return httpx.Response(
                200,
                json=[
                    _port_payload("p1", "Gig0/0"),
                    _port_payload("p2", "Gig0/1"),
                ],
            )
        if path.endswith(f"/templates/{TEMPLATE_PORT}"):
            template_calls.append(path)
            return httpx.Response(
                200,
                json={
                    "sections": [
                        {
                            "fields": [
                                {"key": "speed_mbps", "type": "number"},
                                {"key": "snmp_community", "type": "password"},
                            ]
                        }
                    ]
                },
            )
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport)
    async with ToolDispatcher(token=TOKEN, reservation_id=RESERVATION_ID, http_client=client) as d:
        result = await d.dispatch("get_device_ports", {"device_id": str(DEVICE_A)})

    body = json.loads(result["content"])
    assert isinstance(body, list) and len(body) == 2
    for port in body:
        assert "snmp_community" not in port["field_data"]
        assert port["field_data"]["speed_mbps"] == 1000
    # Template fetched once across the two ports.
    assert len(template_calls) == 1


# --- get_device_current_config (two-hop) ---


@pytest.mark.asyncio
async def test_get_device_current_config_two_hop_returns_payload():
    listing_payload = {
        "items": [
            {
                "id": VERSION_ID,
                "device_id": str(DEVICE_A),
                "version_number": 3,
                "connection_type": "ssh",
                "description": "live",
                "created_by": "u1",
                "author_name": "Jordan",
                "created_at": "2026-05-19T10:00:00Z",
            }
        ],
        "total": 1,
        "skip": 0,
        "limit": 1,
    }
    detail_payload = {
        "id": VERSION_ID,
        "device_id": str(DEVICE_A),
        "version_number": 3,
        "connection_type": "ssh",
        "description": "live",
        "created_by": "u1",
        "author_name": "Jordan",
        "created_at": "2026-05-19T10:00:00Z",
        "config": {"hostname": "fw-a", "vlans": [10, 20]},
    }
    routes = [
        (
            lambda r: (
                r.url.path.endswith(f"/devices/{DEVICE_A}/config-versions")
                and r.url.params.get("limit") == "1"
            ),
            httpx.Response(200, json=listing_payload),
        ),
        (
            lambda r: r.url.path.endswith(f"/devices/{DEVICE_A}/config-versions/{VERSION_ID}"),
            httpx.Response(200, json=detail_payload),
        ),
    ]
    async with _dispatcher_with(routes) as d:
        result = await d.dispatch("get_device_current_config", {"device_id": str(DEVICE_A)})
    body = json.loads(result["content"])
    assert body["has_config"] is True
    assert body["config"]["hostname"] == "fw-a"
    assert body["version_number"] == 3


@pytest.mark.asyncio
async def test_get_device_current_config_no_versions_returns_has_config_false():
    routes = [
        (
            lambda r: r.url.path.endswith(f"/devices/{DEVICE_A}/config-versions"),
            httpx.Response(200, json={"items": [], "total": 0, "skip": 0, "limit": 1}),
        ),
    ]
    async with _dispatcher_with(routes) as d:
        result = await d.dispatch("get_device_current_config", {"device_id": str(DEVICE_A)})
    body = json.loads(result["content"])
    assert body == {"has_config": False}


@pytest.mark.asyncio
async def test_get_device_current_config_missing_version_id_returns_tool_error():
    listing_payload = {
        "items": [
            {
                "device_id": str(DEVICE_A),
                "version_number": 3,
                "connection_type": "ssh",
            }
        ],
        "total": 1,
        "skip": 0,
        "limit": 1,
    }
    routes = [
        (
            lambda r: r.url.path.endswith(f"/devices/{DEVICE_A}/config-versions"),
            httpx.Response(200, json=listing_payload),
        ),
    ]
    async with _dispatcher_with(routes) as d:
        result = await d.dispatch("get_device_current_config", {"device_id": str(DEVICE_A)})
    assert result["is_error"] is True
    assert "missing an id" in json.loads(result["content"])["message"]


# --- list_device_config_history ---


@pytest.mark.asyncio
async def test_list_device_config_history_passes_limit():
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith(f"/devices/{DEVICE_A}/config-versions"):
            captured["limit"] = request.url.params.get("limit", "")
            return httpx.Response(
                200, json={"items": [{"id": "v1"}], "total": 1, "skip": 0, "limit": 5}
            )
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport)
    async with ToolDispatcher(token=TOKEN, reservation_id=RESERVATION_ID, http_client=client) as d:
        await d.dispatch(
            "list_device_config_history",
            {"device_id": str(DEVICE_A), "limit": 5},
        )
    assert captured["limit"] == "5"


@pytest.mark.asyncio
async def test_list_device_config_history_rejects_bad_limit():
    async with _dispatcher_with([]) as d:
        result = await d.dispatch(
            "list_device_config_history",
            {"device_id": str(DEVICE_A), "limit": 999},
        )
    assert result["is_error"] is True
    assert "1 and 50" in json.loads(result["content"])["message"]


# --- get_device_config_schema (Branch 2: schema discovery) ---


def _schema_routes(template_id=TEMPLATE_FW, connection_type="Management"):
    """Build routes that resolve device -> template (with connection_type) so
    get_device_config_schema can do its full lookup chain. Returns the list
    plus a mutable counter so tests can assert call counts.
    """
    counters = {"device": 0, "template": 0}

    def device_handler(request: httpx.Request) -> httpx.Response:
        counters["device"] += 1
        return httpx.Response(200, json=_device_payload(DEVICE_A, "fw-a", template_id=template_id))

    def template_handler(request: httpx.Request) -> httpx.Response:
        counters["template"] += 1
        body = _template_payload(template_id)
        body["connection_type"] = connection_type
        return httpx.Response(200, json=body)

    routes = [
        (lambda r: r.url.path.endswith(f"/devices/{DEVICE_A}"), device_handler),
        (lambda r: r.url.path.endswith(f"/templates/{template_id}"), template_handler),
    ]
    return routes, counters


@pytest.mark.asyncio
async def test_get_device_config_schema_returns_schema_for_known_connection_type():
    routes, _ = _schema_routes(connection_type="Management")
    async with _dispatcher_with(routes) as d:
        result = await d.dispatch("get_device_config_schema", {"device_id": str(DEVICE_A)})

    body = json.loads(result["content"])
    assert result["is_error"] is False
    assert body["connection_type"] == "Management"
    assert isinstance(body["schema"], dict)
    assert body["schema"]["type"] == "object"
    # The Management schema's allowed keys are the source of truth.
    assert set(body["allowed_keys"]) == {"vlan", "ip", "hostname", "description"}
    assert "note" not in body


@pytest.mark.asyncio
async def test_get_device_config_schema_null_schema_for_unregistered_connection_type():
    routes, _ = _schema_routes(connection_type="MadeUpProtocol")
    async with _dispatcher_with(routes) as d:
        result = await d.dispatch("get_device_config_schema", {"device_id": str(DEVICE_A)})

    body = json.loads(result["content"])
    assert result["is_error"] is False
    assert body["connection_type"] == "MadeUpProtocol"
    assert body["schema"] is None
    assert body["allowed_keys"] == []
    assert "no schema is registered" in body["note"]


@pytest.mark.asyncio
async def test_get_device_config_schema_caches_per_dispatcher():
    routes, counters = _schema_routes()
    async with _dispatcher_with(routes) as d:
        first = await d.dispatch("get_device_config_schema", {"device_id": str(DEVICE_A)})
        second = await d.dispatch("get_device_config_schema", {"device_id": str(DEVICE_A)})

    assert first["content"] == second["content"]
    # Two dispatches, one set of network round-trips.
    assert counters["device"] == 1
    assert counters["template"] == 1


@pytest.mark.asyncio
async def test_get_device_config_schema_returns_tool_error_when_device_missing():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith(f"/devices/{DEVICE_A}"):
            return httpx.Response(404, json={"detail": "Device not found"})
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport)
    async with ToolDispatcher(token=TOKEN, reservation_id=RESERVATION_ID, http_client=client) as d:
        result = await d.dispatch("get_device_config_schema", {"device_id": str(DEVICE_A)})

    body = json.loads(result["content"])
    assert result["is_error"] is True
    assert "not found" in body["message"].lower()


@pytest.mark.asyncio
async def test_get_device_config_schema_null_when_template_has_no_connection_type():
    """Template without a bound driver returns null schema with a note."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith(f"/devices/{DEVICE_A}"):
            return httpx.Response(200, json=_device_payload(DEVICE_A, "fw-a"))
        if request.url.path.endswith(f"/templates/{TEMPLATE_FW}"):
            body = _template_payload(TEMPLATE_FW)
            body["connection_type"] = None  # no driver bound
            return httpx.Response(200, json=body)
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport)
    async with ToolDispatcher(token=TOKEN, reservation_id=RESERVATION_ID, http_client=client) as d:
        result = await d.dispatch("get_device_config_schema", {"device_id": str(DEVICE_A)})

    body = json.loads(result["content"])
    assert result["is_error"] is False
    assert body["connection_type"] is None
    assert body["schema"] is None
    assert "no driver bound" in body["note"]


def test_get_device_config_schema_in_active_tool_set_unconditionally():
    """Schema discovery must ship in the read set whether write tools are on
    or off; the model needs schema info even for read-only diagnostic work.
    """
    from app.services.tools import get_active_tool_definitions

    # Names in the read-only tool list (no write tools).
    read_names = {t["name"] for t in get_active_tool_definitions()}
    assert "get_device_config_schema" in read_names


# --- find_path ---


@pytest.mark.asyncio
async def test_find_path_posts_both_uuids():
    captured: dict[str, dict] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/pathfind"):
            captured["body"] = json.loads(request.content)
            return httpx.Response(
                200,
                json={"reachable": True, "hop_count": 1, "paths": [[]]},
            )
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport)
    async with ToolDispatcher(token=TOKEN, reservation_id=RESERVATION_ID, http_client=client) as d:
        result = await d.dispatch(
            "find_path",
            {"source_device_id": str(DEVICE_A), "target_device_id": str(DEVICE_B)},
        )
    assert json.loads(result["content"])["reachable"] is True
    assert captured["body"]["source_device_id"] == str(DEVICE_A)
    assert captured["body"]["target_device_id"] == str(DEVICE_B)


# --- list_executions_for_reservation: reservation_id injection ---


@pytest.mark.asyncio
async def test_list_executions_injects_reservation_id_ignoring_model_input():
    """Critical safety invariant: dispatcher injects the constructor-bound
    reservation_id no matter what the model supplies.
    """
    captured: dict[str, dict] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/runs"):
            captured["params"] = dict(request.url.params)
            return httpx.Response(200, json={"items": [], "total": 0, "skip": 0, "limit": 20})
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport)
    other_reservation = str(uuid.uuid4())
    async with ToolDispatcher(token=TOKEN, reservation_id=RESERVATION_ID, http_client=client) as d:
        # Model sneakily includes a reservation_id arg; dispatcher must ignore it.
        await d.dispatch(
            "list_executions_for_reservation",
            {"reservation_id": other_reservation, "limit": 5},
        )
    assert captured["params"]["reservation_id"] == str(RESERVATION_ID)
    assert captured["params"]["limit"] == "5"


@pytest.mark.asyncio
async def test_list_executions_forwards_device_id_and_status_filters():
    captured: dict[str, dict] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/runs"):
            captured["params"] = dict(request.url.params)
            return httpx.Response(200, json={"items": [], "total": 0, "skip": 0, "limit": 20})
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport)
    async with ToolDispatcher(token=TOKEN, reservation_id=RESERVATION_ID, http_client=client) as d:
        await d.dispatch(
            "list_executions_for_reservation",
            {"device_id": str(DEVICE_A), "status": "FAILED"},
        )
    assert captured["params"]["device_id"] == str(DEVICE_A)
    assert captured["params"]["status"] == "FAILED"


@pytest.mark.asyncio
async def test_list_executions_translates_403_to_friendly_tool_error():
    routes = [(lambda r: r.url.path.endswith("/runs"), httpx.Response(403))]
    async with _dispatcher_with(routes) as d:
        result = await d.dispatch("list_executions_for_reservation", {})
    assert result["is_error"] is True
    assert "ownership" in json.loads(result["content"])["message"]


# --- truncation + JWT forwarding ---


@pytest.mark.asyncio
async def test_tool_result_truncation():
    huge = "X" * 50_000
    routes = [
        (
            lambda r: (
                r.url.path.endswith(f"/devices/{DEVICE_A}/config-versions")
                and r.url.params.get("limit") == "1"
            ),
            httpx.Response(
                200,
                json={"items": [{"id": VERSION_ID}], "total": 1, "skip": 0, "limit": 1},
            ),
        ),
        (
            lambda r: r.url.path.endswith(f"/devices/{DEVICE_A}/config-versions/{VERSION_ID}"),
            httpx.Response(200, json={"id": VERSION_ID, "config": {"blob": huge}}),
        ),
    ]
    dispatcher = _dispatcher_with(routes, char_cap=500)
    async with dispatcher as d:
        result = await d.dispatch("get_device_current_config", {"device_id": str(DEVICE_A)})
    assert "truncated" in result["content"]
    assert len(result["content"]) <= 600  # cap + truncation marker


@pytest.mark.asyncio
async def test_dispatcher_forwards_bearer_token():
    captured_auth: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured_auth["header"] = request.headers.get("authorization", "")
        if request.url.path.endswith(f"/devices/{DEVICE_A}"):
            return httpx.Response(200, json=_device_payload(DEVICE_A, "fw-a"))
        if request.url.path.endswith(f"/templates/{TEMPLATE_FW}"):
            return httpx.Response(200, json=_template_payload(TEMPLATE_FW))
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport)
    async with ToolDispatcher(
        token="forward-me", reservation_id=RESERVATION_ID, http_client=client
    ) as d:
        await d.dispatch("get_device", {"device_id": str(DEVICE_A)})
    assert captured_auth["header"] == "Bearer forward-me"


@pytest.mark.asyncio
async def test_dispatcher_handles_httpx_failure():
    class _FailingTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            raise httpx.ConnectError("no network")

    client = httpx.AsyncClient(transport=_FailingTransport())
    async with ToolDispatcher(token=TOKEN, reservation_id=RESERVATION_ID, http_client=client) as d:
        result = await d.dispatch("get_device", {"device_id": str(DEVICE_A)})
    assert result["is_error"] is True
    assert "HTTP error" in json.loads(result["content"])["message"]
    assert d.call_log[0].error and "HTTP error" in d.call_log[0].error
