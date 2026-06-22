"""Branch coverage for ToolDispatcher error-parsing paths.

These are NOT the write-tool gate (that security invariant, issue #113, is
already covered in test_write_tools.py at the dispatch boundary). They are the
remaining uncovered branches:

- get_device_config_schema on a device with no template_id (lines 464-475)
- propose_config_change 422 whose body is not JSON, so detail falls back to
  resp.text (lines 645-646)
- schedule_config_apply 403/422 whose body is not JSON, so detail falls back to
  the status code string (lines 688-689)

All downstream HTTP is stubbed via httpx.MockTransport; no real service runs.
"""

import json
import uuid

import httpx
import pytest
from app.config import settings
from app.services.tools import ToolDispatcher

RESERVATION_ID = uuid.UUID("11111111-1111-1111-1111-111111111111")
DEVICE_ID = uuid.UUID("33333333-3333-3333-3333-333333333333")
TEMPLATE_ID = "tpl-fw"
VERSION_ID = "cccccccc-cccc-cccc-cccc-cccccccccccc"


@pytest.fixture(autouse=True)
def _write_tools_enabled(monkeypatch):
    monkeypatch.setattr(settings, "ai_write_tools_enabled", True)


def _make_handler(routes):
    def handler(request: httpx.Request) -> httpx.Response:
        for predicate, response in routes:
            if predicate(request):
                return response
        return httpx.Response(404, json={"detail": f"unmocked: {request.method} {request.url}"})

    return handler


def _dispatcher_with(routes) -> ToolDispatcher:
    transport = httpx.MockTransport(_make_handler(routes))
    client = httpx.AsyncClient(transport=transport)
    return ToolDispatcher(token="test-token", reservation_id=RESERVATION_ID, http_client=client)


def _device(template_id):
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


def _template():
    return {
        "id": TEMPLATE_ID,
        "name": "firewall",
        "template_type": "device",
        "sections": [
            {"name": "Access", "fields": [{"key": "vlan", "label": "VLAN", "type": "number"}]}
        ],
    }


# --- get_device_config_schema: device has no template_id (464-475) ---


async def test_config_schema_no_template_id_reports_none_source():
    routes = [
        (
            lambda r: r.method == "GET" and r.url.path.endswith(f"/devices/{DEVICE_ID}"),
            httpx.Response(200, json=_device(template_id=None)),
        ),
    ]
    async with _dispatcher_with(routes) as dispatcher:
        result = await dispatcher.dispatch(
            "get_device_config_schema", {"device_id": str(DEVICE_ID)}
        )
    assert result["is_error"] is False
    body = json.loads(result["content"])
    assert body["connection_type"] is None
    assert body["schema"] is None
    assert body["allowed_keys"] == []
    assert body["source"] == "none"
    assert "no template_id" in body["note"]


async def test_config_schema_no_template_id_is_cached():
    """The no-template response is memoized: a second dispatch must not refetch
    the device (the route would 404 on a second GET if it were not cached)."""
    calls = {"device": 0}

    def device_handler(_r):
        calls["device"] += 1
        return httpx.Response(200, json=_device(template_id=None))

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path.endswith(f"/devices/{DEVICE_ID}"):
            return device_handler(request)
        return httpx.Response(404, json={"detail": "unmocked"})

    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport)
    async with ToolDispatcher(
        token="t", reservation_id=RESERVATION_ID, http_client=client
    ) as dispatcher:
        await dispatcher.dispatch("get_device_config_schema", {"device_id": str(DEVICE_ID)})
        await dispatcher.dispatch("get_device_config_schema", {"device_id": str(DEVICE_ID)})
    assert calls["device"] == 1


# --- propose_config_change: 422 with a non-JSON body falls back to text (645-646) ---


async def test_propose_config_change_422_non_json_body_uses_text():
    routes = [
        (
            lambda r: r.method == "GET" and r.url.path.endswith(f"/devices/{DEVICE_ID}"),
            httpx.Response(200, json=_device(template_id=TEMPLATE_ID)),
        ),
        (
            lambda r: r.method == "GET" and r.url.path.endswith(f"/templates/{TEMPLATE_ID}"),
            httpx.Response(200, json=_template()),
        ),
        (
            lambda r: (
                r.method == "POST" and r.url.path.endswith(f"/devices/{DEVICE_ID}/config-versions")
            ),
            httpx.Response(422, text="plain text validation failure"),
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
    # 422 is a soft result so the model can retry, not a hard tool error.
    assert result["is_error"] is False
    body = json.loads(result["content"])
    assert body["validation"] == "errors"
    assert body["detail"] == "plain text validation failure"


# --- schedule_config_apply: 403/422 with a non-JSON body uses status code (688-689) ---


async def test_schedule_config_apply_403_non_json_body_uses_status_code():
    routes = [
        (
            lambda r: (
                r.method == "POST"
                and r.url.path.endswith(f"/config-versions/{VERSION_ID}/schedule")
            ),
            httpx.Response(403, text="forbidden, no json here"),
        ),
    ]
    async with _dispatcher_with(routes) as dispatcher:
        result = await dispatcher.dispatch(
            "schedule_config_apply",
            {
                "device_id": str(DEVICE_ID),
                "version_id": VERSION_ID,
                "delay_seconds": 30,
            },
        )
    assert result["is_error"] is True
    body = json.loads(result["content"])
    # Non-JSON 403 body: detail falls back to the bare status code string.
    assert body["message"] == "403"
