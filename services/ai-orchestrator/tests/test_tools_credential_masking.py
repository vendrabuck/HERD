"""Issue #1464: no credential value from a stored device config, or from a run row
derived from one, reaches an assistant tool result.

Every case puts CANARY somewhere a credential can sit (under a credential-named
key, after a keyword in command text, nested in lists and dicts, in each in-repo
driver's configuration shape) and asserts the canary is nowhere in what dispatch()
returns, which is exactly the text the model provider receives and the route stores
in assistant_messages.content_blocks. The fail-closed cases assert that a payload the
mask is not applied to is withheld, never passed through.
"""

import importlib.util
import json
import uuid
from pathlib import Path

import httpx
import pytest
from app.config import settings
from app.services import tools
from app.services.tools import (
    CONFIG_WITHHELD_NOTE,
    ITEMS_WITHHELD_NOTE,
    REDACTED_MARKER_REFUSAL,
    REDACTED_NOTE,
    ToolDispatcher,
)
from herd_common.config_redaction import REDACTED_VALUE
from herd_common.device_config import validate_device_config_with_schema

RESERVATION_ID = uuid.UUID("11111111-1111-1111-1111-111111111111")
DEVICE_A = uuid.UUID("33333333-3333-3333-3333-333333333333")
VERSION_ID = "cccccccc-cccc-cccc-cccc-cccccccccccc"
TOKEN = "test-token"
CANARY = "Canary-S3cr3t-1464"

_REPO_ROOT = Path(__file__).resolve().parents[3]


def _frr_published_schema() -> dict:
    """The schema drivers/frr_mgmt publishes, read from the driver as shipped."""
    path = _REPO_ROOT / "drivers" / "frr_mgmt" / "driver.py"
    spec = importlib.util.spec_from_file_location("frr_mgmt_masking_source", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.Driver.config_schema()


FRR_SCHEMA = _frr_published_schema()

# One stored configuration per shape an in-repo driver's config versions can take:
# frr_mgmt publishes {commands, command}; Management without a published schema,
# Layer 2 (srl_l2, mock_l2), and Layer 3 (frr_l3, mock_l3) use the registry. The
# Layer 1 and Hypervisor connection types accept no configuration. Each value is
# (connection_type, published schema or None, config, text that must survive).
DRIVER_SHAPES = {
    "frr_mgmt_commands": (
        "Management",
        FRR_SCHEMA,
        {
            "commands": [
                "router bgp 65000",
                " neighbor 192.0.2.1 remote-as 65001",
                f" neighbor 192.0.2.1 password {CANARY}",
                "interface eth0",
                f" ip ospf message-digest-key 1 md5 {CANARY}",
                f" ip ospf authentication-key {CANARY}",
                f" ip rip authentication string {CANARY}",
                f" ip nhrp authentication {CANARY}",
                f"snmp-server community {CANARY} RO",
                f"enable secret 5 {CANARY}",
                "key chain KC",
                f" key-string {CANARY}",
            ]
        },
        "router bgp 65000",
    ),
    "frr_mgmt_command": (
        "Management",
        FRR_SCHEMA,
        {"command": f"username admin password 7 {CANARY}"},
        "username admin password",
    ),
    "management_registry": (
        "Management",
        None,
        {"hostname": "r1", "ip": "10.0.0.1/24", "vlan": 10, "description": f"secret {CANARY}"},
        "10.0.0.1/24",
    ),
    "layer2_registry": (
        "Layer 2 Switch",
        None,
        {"vlan_assignments": {"ethernet-1/1": 10}, "description": f"psk key {CANARY}"},
        "ethernet-1/1",
    ),
    "layer3_registry": (
        "Layer 3 Switch",
        None,
        {
            "interfaces": [{"name": "eth1", "ip": "10.0.0.1/24", "zone": "trust"}],
            "routes": [{"destination": "0.0.0.0/0", "interface": "eth1"}],
            "description": f"bgp password {CANARY}",
        },
        "0.0.0.0/0",
    ),
}

# Credential-named keys, nested containers, and multi-line command text.
GENERIC_CONFIG = {
    "snmp": {"community": CANARY, "location": "lab-3"},
    "users": [{"name": "admin", "password": CANARY}, {"name": "ops", "secret": CANARY}],
    "api_key": CANARY,
    "auth_token": CANARY,
    "blocks": [[{"lines": [f"key-string {CANARY}", "no shutdown"]}]],
    "script": f"interface eth1\n neighbor 10.0.0.1 password {CANARY}\n exit",
}


@pytest.fixture(autouse=True)
def _reservation_holds_the_test_device(monkeypatch):
    async def _devices(self):
        return frozenset({DEVICE_A})

    monkeypatch.setattr(ToolDispatcher, "_fetch_reservation_devices", _devices)


def _metadata(**overrides):
    item = {
        "id": VERSION_ID,
        "device_id": str(DEVICE_A),
        "version_number": 7,
        "connection_type": "Management",
        "description": "nightly baseline",
        "created_by": "u1",
        "author_name": "Jordan",
        "created_at": "2026-11-18T10:00:00Z",
        "restored_from_id": None,
        "last_apply_run_id": None,
    }
    item.update(overrides)
    return item


def _config_dispatcher(detail_response: httpx.Response) -> ToolDispatcher:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith(f"/devices/{DEVICE_A}/config-versions"):
            return httpx.Response(
                200, json={"items": [_metadata()], "total": 1, "skip": 0, "limit": 1}
            )
        if path.endswith(f"/devices/{DEVICE_A}/config-versions/{VERSION_ID}"):
            return detail_response
        return httpx.Response(404)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return ToolDispatcher(token=TOKEN, reservation_id=RESERVATION_ID, http_client=client)


def _listing_dispatcher(path_suffix: str, body) -> ToolDispatcher:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith(path_suffix):
            return httpx.Response(200, json=body)
        return httpx.Response(404)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return ToolDispatcher(token=TOKEN, reservation_id=RESERVATION_ID, http_client=client)


async def _current_config(detail_response: httpx.Response) -> tuple[dict, str]:
    async with _config_dispatcher(detail_response) as d:
        result = await d.dispatch("get_device_current_config", {"device_id": str(DEVICE_A)})
    assert result["is_error"] is False, result
    return json.loads(result["content"]), result["content"]


# --- get_device_current_config ---


@pytest.mark.parametrize("shape", sorted(DRIVER_SHAPES))
async def test_current_config_masks_every_in_repo_driver_shape(shape):
    connection_type, schema, config, kept = DRIVER_SHAPES[shape]
    # The shape is one the driver's validator accepts, so it is a config that
    # can really be stored.
    validate_device_config_with_schema(connection_type, config, schema=schema)

    body, raw = await _current_config(httpx.Response(200, json=_metadata(config=config)))

    assert CANARY not in raw
    assert body["has_config"] is True
    assert body["redacted"] is True
    assert body["note"] == REDACTED_NOTE
    assert kept in raw
    assert REDACTED_VALUE in raw
    assert body["version_number"] == 7


async def test_current_config_masks_keyed_nested_and_multiline_values():
    body, raw = await _current_config(httpx.Response(200, json=_metadata(config=GENERIC_CONFIG)))
    assert CANARY not in raw
    config = body["config"]
    assert config["snmp"] == {"community": REDACTED_VALUE, "location": "lab-3"}
    assert config["users"][0] == {"name": "admin", "password": REDACTED_VALUE}
    assert config["api_key"] == REDACTED_VALUE
    assert config["blocks"] == [[{"lines": [f"key-string {REDACTED_VALUE}", "no shutdown"]}]]
    assert config["script"] == (
        f"interface eth1\n neighbor 10.0.0.1 password {REDACTED_VALUE}\n exit"
    )
    assert body["redacted"] is True


async def test_current_config_masks_every_field_of_the_version_not_only_config():
    # The version's own free text and any field inventory may add that is
    # derived from the config (a rendered text, a diff) are masked too.
    detail = _metadata(
        description=f"rotate: neighbor 192.0.2.1 password {CANARY}",
        config={"hostname": "r1"},
        rendered=f"hostname r1\nenable secret {CANARY}",
    )
    body, raw = await _current_config(httpx.Response(200, json=detail))
    assert CANARY not in raw
    assert body["config"] == {"hostname": "r1"}
    assert body["redacted"] is True


async def test_current_config_without_credentials_is_returned_whole_and_marked_false():
    config = {"hostname": "r1", "commands": ["interface eth1", " no shutdown"]}
    body, _ = await _current_config(httpx.Response(200, json=_metadata(config=config)))
    assert body["config"] == config
    assert body["redacted"] is False
    assert "note" not in body
    assert body["has_config"] is True


async def test_current_config_marker_cannot_be_overwritten_by_the_stored_record():
    detail = _metadata(config={"password": CANARY}, redacted=False, has_config=False)
    body, raw = await _current_config(httpx.Response(200, json=detail))
    assert CANARY not in raw
    assert body["redacted"] is True
    assert body["has_config"] is True


@pytest.mark.parametrize(
    "config",
    [
        [f"neighbor 192.0.2.1 password {CANARY}"],
        f"neighbor 192.0.2.1 password {CANARY}",
        None,
        42,
    ],
    ids=["list", "string", "null", "number"],
)
async def test_current_config_withholds_a_payload_that_is_not_an_object(config):
    body, raw = await _current_config(httpx.Response(200, json=_metadata(config=config)))
    assert CANARY not in raw
    assert "config" not in body
    assert body["config_withheld"] is True
    assert body["redacted"] is True
    assert body["note"] == CONFIG_WITHHELD_NOTE
    # The version's metadata is still answered.
    assert body["version_number"] == 7
    assert body["id"] == VERSION_ID


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, json=[{"config": {"password": CANARY}}]),
        httpx.Response(200, text=f"password {CANARY}"),
    ],
    ids=["array-body", "not-json"],
)
async def test_current_config_withholds_a_detail_that_is_not_an_object(response):
    body, raw = await _current_config(response)
    assert CANARY not in raw
    assert body["config_withheld"] is True
    # The metadata comes from the listing item instead.
    assert body["id"] == VERSION_ID
    assert body["version_number"] == 7


async def test_current_config_withholds_when_masking_the_payload_fails(monkeypatch):
    real = tools.redact_config

    def failing_on_the_payload(value):
        if isinstance(value, dict) and "config" in value:
            raise RuntimeError("mask failed")
        return real(value)

    monkeypatch.setattr(tools, "redact_config", failing_on_the_payload)
    body, raw = await _current_config(
        httpx.Response(200, json=_metadata(config={"password": CANARY}))
    )
    assert CANARY not in raw
    assert "config" not in body
    assert body["config_withheld"] is True
    assert body["version_number"] == 7


async def test_current_config_keeps_only_the_id_when_nothing_can_be_masked(monkeypatch):
    def failing(value):
        raise RuntimeError("mask failed")

    monkeypatch.setattr(tools, "redact_config", failing)
    body, raw = await _current_config(
        httpx.Response(
            200, json=_metadata(config={"password": CANARY}, description=f"secret {CANARY}")
        )
    )
    assert CANARY not in raw
    assert body == {
        "has_config": True,
        "id": VERSION_ID,
        "config_withheld": True,
        "redacted": True,
        "note": CONFIG_WITHHELD_NOTE,
    }


# --- list_device_config_history ---


async def _history(body) -> dict:
    async with _listing_dispatcher(f"/devices/{DEVICE_A}/config-versions", body) as d:
        result = await d.dispatch(
            "list_device_config_history", {"device_id": str(DEVICE_A), "limit": 10}
        )
    return result


async def test_history_masks_every_item():
    items = [
        _metadata(description=f"bgp: neighbor 192.0.2.1 password {CANARY}"),
        _metadata(version_number=6, description="baseline"),
        # Inventory's listing carries metadata only today; an item that ever
        # carries the payload is masked the same way.
        _metadata(version_number=5, config={"snmp": {"community": CANARY}}),
    ]
    result = await _history({"items": items, "total": 3, "skip": 0, "limit": 10})
    assert CANARY not in result["content"]
    body = json.loads(result["content"])
    assert [i["version_number"] for i in body["items"]] == [7, 6, 5]
    assert body["items"][1]["description"] == "baseline"
    assert body["redacted"] is True
    assert body["note"] == REDACTED_NOTE


async def test_history_without_credentials_is_returned_whole_and_marked_false():
    items = [_metadata(), _metadata(version_number=6)]
    result = await _history({"items": items, "total": 2, "skip": 0, "limit": 10})
    body = json.loads(result["content"])
    assert body == {"items": items, "redacted": False}


async def test_history_leaves_out_an_item_it_cannot_mask():
    items = [_metadata(), f"neighbor 192.0.2.1 password {CANARY}", [CANARY]]
    result = await _history({"items": items, "total": 3, "skip": 0, "limit": 10})
    assert CANARY not in result["content"]
    body = json.loads(result["content"])
    assert body["items"] == [_metadata()]
    assert body["withheld"] == 2
    assert body["redacted"] is True
    assert body["note"] == ITEMS_WITHHELD_NOTE


async def test_history_leaves_out_an_item_whose_masking_fails(monkeypatch):
    def failing(value):
        raise RuntimeError("mask failed")

    monkeypatch.setattr(tools, "redact_config", failing)
    result = await _history({"items": [_metadata(description=f"secret {CANARY}")]})
    assert CANARY not in result["content"]
    body = json.loads(result["content"])
    assert body["items"] == []
    assert body["withheld"] == 1


async def test_history_refuses_a_listing_of_an_unexpected_shape():
    result = await _history({"items": f"password {CANARY}"})
    assert result["is_error"] is True
    assert CANARY not in result["content"]


# --- list_executions_for_reservation ---


def _run_row(**overrides):
    row = {
        "id": "dddddddd-dddd-dddd-dddd-dddddddddddd",
        "device_id": str(DEVICE_A),
        "driver_id": "eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee",
        "driver_sha256": "ab" * 32,
        "action": "configure",
        "status": "SUCCESS",
        "reservation_id": str(RESERVATION_ID),
        "user_id": "ffffffff-ffff-ffff-ffff-ffffffffffff",
        "input_params": {"HERD_ip": "10.0.0.1", "dry_run": True},
        "output": None,
        "error": None,
        "port_a": None,
        "port_b": None,
        "started_at": None,
        "completed_at": None,
        "duration_ms": 12,
        "created_at": "2026-11-18T10:00:00Z",
    }
    row.update(overrides)
    return row


async def _runs(body) -> dict:
    async with _listing_dispatcher("/runs", body) as d:
        result = await d.dispatch("list_executions_for_reservation", {})
    return result


async def test_run_list_masks_arguments_output_and_error():
    commands = ["router bgp 65000", f" neighbor 192.0.2.1 password {CANARY}", " exit"]
    # A dry run: frr_mgmt returns the commands it would apply, and execution
    # stores that return value on the row as JSON text, all on one line.
    dry_run = _run_row(
        input_params={
            "HERD_ip": "10.0.0.1",
            "HERD_password": "***REDACTED***",
            "method_kwargs": {"commands": ["router bgp 65000", " neighbor 1 password [redacted]"]},
            "method_kwargs_redacted": True,
            "dry_run": True,
        },
        output=json.dumps({"success": True, "simulated": True, "applied": commands}),
    )
    # A row stored before execution masked arguments, whose real apply failed:
    # unmasked arguments, the device's echo in the output, a driver error.
    legacy = _run_row(
        status="FAILED",
        input_params={"method_kwargs": {"commands": commands}, "dry_run": False},
        output=json.dumps(
            {
                "success": False,
                "error": "% Invalid input detected",
                "output": f"r1(config-router)# neighbor 192.0.2.1 password {CANARY}\n% Invalid",
                "attempted": commands,
            }
        ),
        error=f"driver said: neighbor 192.0.2.1 password {CANARY}",
    )
    result = await _runs({"items": [dry_run, legacy], "total": 2, "skip": 0, "limit": 20})
    assert result["is_error"] is False
    assert CANARY not in result["content"]
    body = json.loads(result["content"])
    assert (body["total"], body["skip"], body["limit"]) == (2, 0, 20)
    assert body["redacted"] is True
    assert body["note"] == REDACTED_NOTE
    first = json.loads(body["items"][0]["output"])
    # Parsed and masked as a structure: the commands after the secret survive.
    assert first["applied"] == [
        "router bgp 65000",
        f" neighbor 192.0.2.1 password {REDACTED_VALUE}",
        " exit",
    ]
    second = body["items"][1]
    assert second["input_params"]["method_kwargs"]["commands"][2] == " exit"
    assert second["status"] == "FAILED"
    assert second["error"] == f"driver said: neighbor 192.0.2.1 password {REDACTED_VALUE}"


async def test_run_output_that_is_not_json_is_masked_line_by_line():
    row = _run_row(output=f"raw text\n neighbor 192.0.2.1 password {CANARY}\nend")
    result = await _runs({"items": [row], "total": 1, "skip": 0, "limit": 20})
    assert CANARY not in result["content"]
    body = json.loads(result["content"])
    assert body["items"][0]["output"] == (
        f"raw text\n neighbor 192.0.2.1 password {REDACTED_VALUE}\nend"
    )


async def test_run_list_without_credentials_is_returned_whole_and_marked_false():
    rows = [_run_row(output=json.dumps({"success": True, "applied": ["ip route 0.0.0.0/0 eth1"]}))]
    result = await _runs({"items": rows, "total": 1, "skip": 0, "limit": 20})
    body = json.loads(result["content"])
    assert body == {"total": 1, "skip": 0, "limit": 20, "items": rows, "redacted": False}


async def test_run_list_leaves_out_a_row_it_cannot_mask():
    rows = [_run_row(), f"password {CANARY}"]
    result = await _runs({"items": rows, "total": 2, "skip": 0, "limit": 20})
    assert CANARY not in result["content"]
    body = json.loads(result["content"])
    assert body["items"] == [_run_row()]
    assert body["withheld"] == 1
    assert body["note"] == ITEMS_WITHHELD_NOTE


async def test_run_list_leaves_out_a_row_whose_masking_fails(monkeypatch):
    def failing(value):
        raise RuntimeError("mask failed")

    monkeypatch.setattr(tools, "redact_config", failing)
    row = _run_row(error=f"secret {CANARY}")
    result = await _runs({"items": [row], "total": 1, "skip": 0, "limit": 20})
    assert CANARY not in result["content"]
    body = json.loads(result["content"])
    assert body["items"] == []
    assert body["withheld"] == 1


@pytest.mark.parametrize(
    "body",
    [[_run_row(error=f"password {CANARY}")], {"items": f"password {CANARY}"}],
    ids=["bare-array", "items-not-a-list"],
)
async def test_run_list_refuses_an_answer_of_an_unexpected_shape(body):
    result = await _runs(body)
    assert result["is_error"] is True
    assert CANARY not in result["content"]


# --- propose_config_change: the marker is never written back ---


async def test_propose_config_change_refuses_a_payload_holding_the_redacted_marker(monkeypatch):
    monkeypatch.setattr(settings, "ai_write_tools_enabled", True)
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(f"{request.method} {request.url.path}")
        return httpx.Response(500)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    async with ToolDispatcher(token=TOKEN, reservation_id=RESERVATION_ID, http_client=client) as d:
        result = await d.dispatch(
            "propose_config_change",
            {
                "device_id": str(DEVICE_A),
                "config_payload": {
                    "commands": ["router bgp 65000", f" neighbor 1 password {REDACTED_VALUE}"]
                },
                "description": "add a route",
            },
        )
    assert result["is_error"] is True
    assert json.loads(result["content"])["message"] == REDACTED_MARKER_REFUSAL
    # The guidance names both units of a payload: a command line and a key.
    assert REDACTED_MARKER_REFUSAL == (
        "config_payload contains [redacted], a masked value from a tool result, not "
        "the device's real text; leave out every line or key that holds it"
    )
    # Refused before any call to inventory.
    assert seen == []


# --- the marker keys are the tool's alone ---


async def test_current_config_markers_are_always_the_tools_own():
    # Nothing to mask, but the stored record carries every marker key: none of
    # its values survives.
    detail = _metadata(
        config={"hostname": "r1"},
        redacted=True,
        note="stored note",
        withheld=9,
        config_withheld=True,
        has_config=False,
    )
    body, _ = await _current_config(httpx.Response(200, json=detail))
    assert body["has_config"] is True
    assert body["redacted"] is False
    assert "note" not in body
    assert "withheld" not in body
    assert "config_withheld" not in body
    assert body["config"] == {"hostname": "r1"}


async def test_run_list_envelope_cannot_set_a_marker():
    body = {
        "items": [_run_row()],
        "total": 1,
        "skip": 0,
        "limit": 20,
        "note": "upstream note",
        "withheld": 4,
        "redacted": True,
        "config_withheld": True,
        "has_config": True,
    }
    result = await _runs(body)
    answer = json.loads(result["content"])
    assert answer == {"total": 1, "skip": 0, "limit": 20, "items": [_run_row()], "redacted": False}


async def test_withheld_version_markers_replace_the_stored_ones():
    detail = _metadata(config=["not", "an", "object"], note="stored note", withheld=3)
    body, _ = await _current_config(httpx.Response(200, json=detail))
    assert body["note"] == CONFIG_WITHHELD_NOTE
    assert "withheld" not in body
    assert body["config_withheld"] is True


async def test_current_config_masks_the_shapes_the_review_found_leaking():
    # Keys the line keywords name, keywords with punctuation attached, and
    # JSON held in a string (issue #1464 review).
    config = {
        "authentication-key": CANARY,
        "key-string": CANARY,
        "message-digest-key": CANARY,
        "passphrase": CANARY,
        "pre_shared_key": CANARY,
        "tacacsKey": CANARY,
        "wpa_passphrase": CANARY,
        "commands": [
            f"Password: {CANARY}",
            f"PASSWORD={CANARY}",
            f"login password={CANARY}",
            f"key:{CANARY}",
            f"set mgt-config users admin phash {CANARY}",
            f"wpa-psk ascii {CANARY}",
        ],
        "blob": json.dumps({"admin-user": {"hashed-password": CANARY}}),
    }
    body, raw = await _current_config(httpx.Response(200, json=_metadata(config=config)))
    assert CANARY not in raw
    assert body["redacted"] is True
    assert json.loads(body["config"]["blob"]) == {"admin-user": {"hashed-password": REDACTED_VALUE}}


# --- replaying a stored tool result (AI-TOOL-13) ---


def test_a_replayed_result_is_masked_as_a_structure_or_as_text():
    stored = json.dumps({"config": {"commands": [f"neighbor 1 password {CANARY}"]}})
    masked = tools.mask_replayed_tool_result("get_device_current_config", stored)
    assert json.loads(masked) == {"config": {"commands": [f"neighbor 1 password {REDACTED_VALUE}"]}}
    cut = stored[:60] + "\n... [truncated: 10 chars omitted]"
    assert CANARY not in tools.mask_replayed_tool_result(None, cut)


def test_a_replayed_documentation_result_is_resent_as_stored():
    stored = json.dumps({"text": f"Set the password {CANARY} in the UI."})
    assert tools.mask_replayed_tool_result("read_doc", stored) == stored
    assert tools.mask_replayed_tool_result("search_docs", stored) == stored


@pytest.mark.parametrize("content", [{"password": CANARY}, None, 7], ids=["dict", "none", "int"])
def test_a_replayed_result_that_is_not_text_is_withheld(content):
    replaced = tools.mask_replayed_tool_result("get_device", content)
    assert json.loads(replaced) == {"withheld": True, "note": tools.REPLAY_WITHHELD_NOTE}


def test_a_replayed_result_whose_masking_fails_is_withheld(monkeypatch):
    def failing(value):
        raise RuntimeError("mask failed")

    monkeypatch.setattr(tools, "redact_config", failing)
    replaced = tools.mask_replayed_tool_result("get_device", f"enable secret {CANARY}")
    assert CANARY not in replaced
    assert json.loads(replaced)["note"] == tools.REPLAY_WITHHELD_NOTE


# --- JSON-escaped text (round 3 of the review) ---

RUNNING_CONFIG = (
    "frr version 9\nhostname r1\npassword zebra\nenable password S3cret\n"
    "key chain K\n key 1\n  key-string S3cret2\n"
)
LEAKED_WORDS = ("zebra", "S3cret", "S3cret2")


@pytest.mark.parametrize(
    "output",
    [
        json.dumps("hostname r1\npassword zebra"),
        json.dumps(RUNNING_CONFIG),
        json.dumps({"success": True, "config": RUNNING_CONFIG + "!\n" * 140_000}),
    ],
    ids=["bare-string-result", "bare-running-config", "backup-over-256KiB"],
)
async def test_run_output_holding_escaped_text_is_masked(output):
    result = await _runs({"items": [_run_row(output=output)], "total": 1, "skip": 0, "limit": 20})
    assert result["is_error"] is False
    for word in LEAKED_WORDS:
        assert word not in result["content"], word
    # The markers lead the result, so the char cap never cuts them off.
    assert result["content"].startswith('{"redacted": true, "note": ')


@pytest.mark.parametrize(
    "content",
    [
        # A get_device_current_config result holding running-config text, cut
        # by the 8000-character cap.
        json.dumps({"has_config": True, "config": {"running": RUNNING_CONFIG}})[:120]
        + "\n... [truncated: 9000 chars omitted]",
        # A run list whose output is JSON inside JSON, cut.
        json.dumps({"items": [{"output": json.dumps({"password": "S3cret", "x": "y" * 50})}]})[:80]
        + "\n... [truncated: 10 chars omitted]",
        # An escaped line that starts with an escaped tab.
        json.dumps({"items": [{"output": "a\n\tsecret S3cret\nexit"}]})[:60]
        + "\n... [truncated: 10 chars omitted]",
    ],
    ids=["running-config", "escaped-embedded-json", "escaped-tab"],
)
def test_a_cut_replayed_result_is_masked_through_its_escapes(content):
    assert any(word in content for word in LEAKED_WORDS)
    masked = tools.mask_replayed_tool_result("list_executions_for_reservation", content)
    for word in LEAKED_WORDS:
        assert word not in masked, (word, masked)
