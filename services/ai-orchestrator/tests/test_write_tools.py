"""Tests for the ROADMAP #16 iter 3 AI write tools.

Covers:
- propose_config_change happy path, password-field rejection, 403/422 from inventory
- schedule_config_apply default dry_run, delay_seconds stamping, side_effects tracking
- The feature flag (get_active_tool_definitions) gating the tools on/off
"""

import json
import uuid
from unittest.mock import patch

import httpx
import pytest
from app.config import settings
from app.services.tools import (
    TOOL_DEFINITIONS,
    WRITE_TOOL_DEFINITIONS,
    ToolDispatcher,
    _flatten_password_keys_present,
    get_active_tool_definitions,
)

RESERVATION_ID = uuid.UUID("11111111-1111-1111-1111-111111111111")
DEVICE_ID = uuid.UUID("33333333-3333-3333-3333-333333333333")
TEMPLATE_ID = "tpl-fw"
VERSION_ID = "cccccccc-cccc-cccc-cccc-cccccccccccc"
JOB_ID = "dddddddd-dddd-dddd-dddd-dddddddddddd"


def _make_handler(routes):
    def handler(request: httpx.Request) -> httpx.Response:
        for predicate, response in routes:
            if predicate(request):
                if callable(response):
                    return response(request)
                return response
        return httpx.Response(404, json={"detail": f"unmocked: {request.method} {request.url}"})

    return handler


def _dispatcher_with(routes) -> ToolDispatcher:
    transport = httpx.MockTransport(_make_handler(routes))
    client = httpx.AsyncClient(transport=transport)
    return ToolDispatcher(
        token="test-token",
        reservation_id=RESERVATION_ID,
        http_client=client,
    )


def _device(template_id=TEMPLATE_ID):
    return {
        "id": str(DEVICE_ID),
        "name": "fw-1",
        "template_id": template_id,
        "template_name": "firewall",
        "topology_type": "PHYSICAL",
        "status": "RESERVED",
        "field_data": {},
        "exclusive": True,
        "connection_type": "Management",
    }


def _template_with_password_fields(*keys):
    return {
        "id": TEMPLATE_ID,
        "name": "firewall",
        "template_type": "device",
        "sections": [
            {
                "name": "Access",
                "fields": [
                    {"key": "vlan", "label": "VLAN", "type": "number"},
                    *({"key": k, "label": k, "type": "password"} for k in keys),
                ],
            }
        ],
    }


# --- Feature flag ---


def test_get_active_tool_definitions_flag_off_omits_write_tools():
    """Default flag-off state must not expose write tools to the model."""
    with patch.object(settings, "ai_write_tools_enabled", False):
        active = get_active_tool_definitions()
    names = {t["name"] for t in active}
    assert "propose_config_change" not in names
    assert "schedule_config_apply" not in names
    assert "get_device" in names  # read-only baseline still present


def test_get_active_tool_definitions_flag_on_includes_write_tools():
    with patch.object(settings, "ai_write_tools_enabled", True):
        active = get_active_tool_definitions()
    names = {t["name"] for t in active}
    assert "propose_config_change" in names
    assert "schedule_config_apply" in names


def test_write_tool_definitions_shape():
    """Schemas must look right to Anthropic + closed-additional-properties."""
    for t in WRITE_TOOL_DEFINITIONS:
        assert "description" in t and t["description"]
        assert t["input_schema"]["type"] == "object"
        assert t["input_schema"].get("additionalProperties") is False
    propose = next(t for t in WRITE_TOOL_DEFINITIONS if t["name"] == "propose_config_change")
    assert propose["input_schema"]["required"] == [
        "device_id",
        "config_payload",
        "description",
    ]
    schedule = next(t for t in WRITE_TOOL_DEFINITIONS if t["name"] == "schedule_config_apply")
    # delay_seconds has a default, so it is NOT required.
    assert schedule["input_schema"]["required"] == ["device_id", "version_id"]
    # Critically: dry_run is NOT exposed to the model. The AI can never schedule a
    # real apply, so there is no parameter to promote a dry-run.
    assert "dry_run" not in schedule["input_schema"]["properties"]


# --- _flatten_password_keys_present helper ---


def test_flatten_password_keys_finds_top_level():
    found = _flatten_password_keys_present(
        {"vlan": 100, "admin_password": "x"},
        {"admin_password"},
    )
    assert found == {"admin_password"}


def test_flatten_password_keys_finds_nested():
    payload = {"settings": {"auth": {"snmp_community": "secret"}}}
    found = _flatten_password_keys_present(payload, {"snmp_community"})
    assert found == {"snmp_community"}


def test_flatten_password_keys_finds_inside_list():
    payload = {"users": [{"name": "alice"}, {"password": "p"}]}
    found = _flatten_password_keys_present(payload, {"password"})
    assert found == {"password"}


def test_flatten_password_keys_empty_when_no_match():
    found = _flatten_password_keys_present({"vlan": 100}, {"admin_password"})
    assert found == set()


def test_flatten_password_keys_empty_set_short_circuits():
    """No password keys to check -> immediately empty (no walk overhead)."""
    found = _flatten_password_keys_present({"deep": {"a": [{"b": 1}]}}, set())
    assert found == set()


# --- propose_config_change happy path ---


@pytest.mark.asyncio
async def test_propose_config_change_success():
    captured = {}

    def post_handler(req: httpx.Request) -> httpx.Response:
        captured["url"] = str(req.url)
        captured["body"] = json.loads(req.content)
        captured["auth"] = req.headers.get("authorization")
        return httpx.Response(
            201,
            json={
                "id": VERSION_ID,
                "version_number": 7,
                "config": captured["body"]["config"],
                "description": captured["body"]["description"],
                "author_name": "ai",
            },
        )

    routes = [
        (
            lambda r: r.method == "GET" and r.url.path.endswith(f"/devices/{DEVICE_ID}"),
            httpx.Response(200, json=_device()),
        ),
        (
            lambda r: r.method == "GET" and r.url.path.endswith(f"/templates/{TEMPLATE_ID}"),
            httpx.Response(200, json=_template_with_password_fields("admin_password")),
        ),
        (
            lambda r: (
                r.method == "POST" and r.url.path.endswith(f"/devices/{DEVICE_ID}/config-versions")
            ),
            post_handler,
        ),
    ]
    async with _dispatcher_with(routes) as dispatcher:
        result = await dispatcher.dispatch(
            "propose_config_change",
            {
                "device_id": str(DEVICE_ID),
                "config_payload": {"vlan": 200},
                "description": "set mgmt vlan",
            },
        )
    assert result["is_error"] is False
    body = json.loads(result["content"])
    assert body["validation"] == "ok"
    assert body["version_id"] == VERSION_ID
    assert body["version_number"] == 7
    assert captured["body"]["config"] == {"vlan": 200}
    assert captured["body"]["description"] == "set mgmt vlan"
    assert captured["auth"] == "Bearer test-token"


@pytest.mark.asyncio
async def test_propose_config_change_password_field_rejected():
    """AI must not be able to set password-typed fields."""
    routes = [
        (
            lambda r: r.method == "GET" and r.url.path.endswith(f"/devices/{DEVICE_ID}"),
            httpx.Response(200, json=_device()),
        ),
        (
            lambda r: r.method == "GET" and r.url.path.endswith(f"/templates/{TEMPLATE_ID}"),
            httpx.Response(200, json=_template_with_password_fields("admin_password")),
        ),
    ]
    async with _dispatcher_with(routes) as dispatcher:
        result = await dispatcher.dispatch(
            "propose_config_change",
            {
                "device_id": str(DEVICE_ID),
                "config_payload": {"vlan": 200, "admin_password": "leaked"},
                "description": "oops",
            },
        )
    assert result["is_error"] is True
    body = json.loads(result["content"])
    assert "admin_password" in body["message"]


@pytest.mark.asyncio
async def test_propose_config_change_403_becomes_tool_error():
    routes = [
        (
            lambda r: r.method == "GET" and r.url.path.endswith(f"/devices/{DEVICE_ID}"),
            httpx.Response(200, json=_device()),
        ),
        (
            lambda r: r.method == "GET" and r.url.path.endswith(f"/templates/{TEMPLATE_ID}"),
            httpx.Response(200, json=_template_with_password_fields()),
        ),
        (
            lambda r: (
                r.method == "POST" and r.url.path.endswith(f"/devices/{DEVICE_ID}/config-versions")
            ),
            httpx.Response(403, json={"detail": "no manage"}),
        ),
    ]
    async with _dispatcher_with(routes) as dispatcher:
        result = await dispatcher.dispatch(
            "propose_config_change",
            {
                "device_id": str(DEVICE_ID),
                "config_payload": {"vlan": 200},
                "description": "x",
            },
        )
    assert result["is_error"] is True
    body = json.loads(result["content"])
    assert "manage permission" in body["message"]


@pytest.mark.asyncio
async def test_propose_config_change_422_returns_soft_validation_errors():
    """422 should let the model retry with a corrected payload, not bail."""
    routes = [
        (
            lambda r: r.method == "GET" and r.url.path.endswith(f"/devices/{DEVICE_ID}"),
            httpx.Response(200, json=_device()),
        ),
        (
            lambda r: r.method == "GET" and r.url.path.endswith(f"/templates/{TEMPLATE_ID}"),
            httpx.Response(200, json=_template_with_password_fields()),
        ),
        (
            lambda r: (
                r.method == "POST" and r.url.path.endswith(f"/devices/{DEVICE_ID}/config-versions")
            ),
            httpx.Response(422, json={"detail": "extra_key not allowed"}),
        ),
    ]
    async with _dispatcher_with(routes) as dispatcher:
        result = await dispatcher.dispatch(
            "propose_config_change",
            {
                "device_id": str(DEVICE_ID),
                "config_payload": {"vlan": 200},
                "description": "x",
            },
        )
    # Not an error path; the body carries the validation failure for the model to read.
    assert result["is_error"] is False
    body = json.loads(result["content"])
    assert body["validation"] == "errors"
    assert "extra_key" in body["detail"]


# --- schedule_config_apply ---


@pytest.mark.asyncio
async def test_schedule_config_apply_defaults_dry_run_true():
    captured = {}

    def post_handler(req: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(req.content)
        return httpx.Response(
            201,
            json={
                "id": JOB_ID,
                "device_id": str(DEVICE_ID),
                "version_id": VERSION_ID,
                "scheduled_for": captured["body"]["scheduled_for"],
                "reservation_id": str(RESERVATION_ID),
                "dry_run": captured["body"]["dry_run"],
                "status": "pending",
                "created_by": "00000000-0000-0000-0000-000000000001",
                "author_name": "ai",
                "created_at": "2026-01-01T00:00:00Z",
            },
        )

    routes = [
        (
            lambda r: (
                r.method == "POST"
                and r.url.path.endswith(f"/config-versions/{VERSION_ID}/schedule")
            ),
            post_handler,
        ),
    ]
    async with _dispatcher_with(routes) as dispatcher:
        result = await dispatcher.dispatch(
            "schedule_config_apply",
            {"device_id": str(DEVICE_ID), "version_id": VERSION_ID},
        )

    assert result["is_error"] is False
    body = json.loads(result["content"])
    assert body["dry_run"] is True
    assert captured["body"]["dry_run"] is True
    assert captured["body"]["reservation_id"] == str(RESERVATION_ID)


@pytest.mark.asyncio
async def test_schedule_config_apply_ignores_attacker_supplied_dry_run_false():
    """Security: a model (or prompt-injected) args dict carrying dry_run=false must
    NOT reach inventory as a real apply. The orchestrator forces dry_run=true
    regardless of what args contains, so the human-confirmation flow is never bypassed.
    """
    captured = {}

    def post_handler(req: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(req.content)
        return httpx.Response(
            201,
            json={
                "id": JOB_ID,
                "device_id": str(DEVICE_ID),
                "version_id": VERSION_ID,
                "scheduled_for": captured["body"]["scheduled_for"],
                "reservation_id": str(RESERVATION_ID),
                "dry_run": captured["body"]["dry_run"],
                "status": "pending",
                "created_by": "00000000-0000-0000-0000-000000000001",
                "author_name": "ai",
                "created_at": "2026-01-01T00:00:00Z",
            },
        )

    routes = [
        (
            lambda r: (
                r.method == "POST"
                and r.url.path.endswith(f"/config-versions/{VERSION_ID}/schedule")
            ),
            post_handler,
        ),
    ]
    async with _dispatcher_with(routes) as dispatcher:
        result = await dispatcher.dispatch(
            "schedule_config_apply",
            {
                "device_id": str(DEVICE_ID),
                "version_id": VERSION_ID,
                "dry_run": False,  # attacker / injected attempt to force a real apply
            },
        )

    assert result["is_error"] is False
    # The forwarded body to inventory must still be a dry-run.
    assert captured["body"]["dry_run"] is True


@pytest.mark.asyncio
async def test_schedule_config_apply_delay_seconds_clamped_to_range():
    """Out-of-range delay -> ToolError, no HTTP call."""
    routes: list = []
    async with _dispatcher_with(routes) as dispatcher:
        result = await dispatcher.dispatch(
            "schedule_config_apply",
            {
                "device_id": str(DEVICE_ID),
                "version_id": VERSION_ID,
                "delay_seconds": 1,  # below the floor of 5
            },
        )
    assert result["is_error"] is True
    body = json.loads(result["content"])
    assert "delay_seconds" in body["message"]


@pytest.mark.asyncio
async def test_schedule_config_apply_records_side_effect():
    """A successful schedule appends a scheduled_apply entry to side_effects so
    the route handler can surface it as pending_apply on the response."""

    def post_handler(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content)
        return httpx.Response(
            201,
            json={
                "id": JOB_ID,
                "device_id": str(DEVICE_ID),
                "version_id": VERSION_ID,
                "scheduled_for": body["scheduled_for"],
                "reservation_id": str(RESERVATION_ID),
                "dry_run": body["dry_run"],
                "status": "pending",
                "created_by": "00000000-0000-0000-0000-000000000001",
                "author_name": "ai",
                "created_at": "2026-01-01T00:00:00Z",
            },
        )

    routes = [
        (
            lambda r: (
                r.method == "POST"
                and r.url.path.endswith(f"/config-versions/{VERSION_ID}/schedule")
            ),
            post_handler,
        ),
    ]
    async with _dispatcher_with(routes) as dispatcher:
        await dispatcher.dispatch(
            "schedule_config_apply",
            {"device_id": str(DEVICE_ID), "version_id": VERSION_ID},
        )
        assert len(dispatcher.side_effects) == 1
        entry = dispatcher.side_effects[0]
        assert entry["kind"] == "scheduled_apply"
        assert entry["job_id"] == JOB_ID
        assert entry["dry_run"] is True


@pytest.mark.asyncio
async def test_schedule_config_apply_422_from_inventory_bubbles_up():
    """E.g. driver does not advertise supports_dry_run."""
    routes = [
        (
            lambda r: (
                r.method == "POST"
                and r.url.path.endswith(f"/config-versions/{VERSION_ID}/schedule")
            ),
            httpx.Response(
                422,
                json={"detail": "this driver does not advertise dry-run support"},
            ),
        ),
    ]
    async with _dispatcher_with(routes) as dispatcher:
        result = await dispatcher.dispatch(
            "schedule_config_apply",
            {"device_id": str(DEVICE_ID), "version_id": VERSION_ID},
        )
    assert result["is_error"] is True
    body = json.loads(result["content"])
    assert "dry-run" in body["message"]


@pytest.mark.asyncio
async def test_schedule_config_apply_no_side_effect_on_failure():
    routes = [
        (
            lambda r: (
                r.method == "POST"
                and r.url.path.endswith(f"/config-versions/{VERSION_ID}/schedule")
            ),
            httpx.Response(422, json={"detail": "bad"}),
        ),
    ]
    async with _dispatcher_with(routes) as dispatcher:
        await dispatcher.dispatch(
            "schedule_config_apply",
            {"device_id": str(DEVICE_ID), "version_id": VERSION_ID},
        )
        assert dispatcher.side_effects == []


# --- Sanity: TOOL_DEFINITIONS unchanged in count ---


def test_baseline_tool_count_unchanged():
    """Stage 3 (write tools) must not silently grow the read-tool list; that
    is a separate concern. Branch 2 added get_device_config_schema as a read
    tool, bringing the count to 7.
    """
    assert len(TOOL_DEFINITIONS) == 7
