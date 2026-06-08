"""Read-only tool dispatcher for the ROADMAP #16 iter 2 reservation assistant.

Exposes a curated set of read-only HERD endpoints as Anthropic tools the
assistant can call during a tool-use loop. The reservation_id is captured at
construction time and injected server-side into any tool that takes it, so
the model cannot peek across reservations no matter what arguments it
supplies.

JWT forwarding mirrors reservation_context.py: a shared httpx.AsyncClient
with the caller's Bearer token. Device fetches are bounded by a Semaphore.
Tool results are JSON-serialised and truncated to a configurable char cap to
prevent any single tool from blowing the model's context window.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

DEVICE_FETCH_CONCURRENCY = 8
HTTP_TIMEOUT_SECONDS = 15.0
DEFAULT_TOOL_RESULT_CHAR_CAP = 8000


class ToolError(Exception):
    """Recoverable downstream failure. The loop wraps this into a tool_result
    block with is_error=True so the model can apologise or retry instead of
    aborting the request.
    """


@dataclass
class ToolCallRecord:
    name: str
    arguments_summary: str
    duration_ms: int
    error: str | None = None


# --- Tool definitions in Anthropic tool-use format ---

TOOL_DEFINITIONS: list[dict[str, Any]] = [
    {
        "name": "get_device",
        "description": (
            "Fetch a single device's metadata and template-defined fields. "
            "Password-typed template fields are stripped server-side. Use the "
            "device IDs listed in <devices>."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "device_id": {"type": "string", "description": "Device UUID"},
            },
            "required": ["device_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "get_device_ports",
        "description": (
            "List ports configured on a device, each with its "
            "template-defined fields (passwords stripped). Use to inspect "
            "interface counts, names, and per-port metadata."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "device_id": {"type": "string", "description": "Device UUID"},
            },
            "required": ["device_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "get_device_current_config",
        "description": (
            "Fetch the device's latest applied configuration (version metadata "
            "plus the config payload). Returns {'has_config': false} if no "
            "config version has ever been recorded for this device."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "device_id": {"type": "string", "description": "Device UUID"},
            },
            "required": ["device_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "list_device_config_history",
        "description": (
            "List recent config versions for a device, newest first. Returns "
            "version metadata only (id, version_number, description, author, "
            "created_at, last_apply_run_id). Useful for reasoning about "
            "config drift over time."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "device_id": {"type": "string", "description": "Device UUID"},
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 50,
                    "default": 10,
                },
            },
            "required": ["device_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "find_path",
        "description": (
            "Find a cabling path between two devices in the topology. Returns "
            "{reachable, hop_count, paths}. Both device IDs must be in this "
            "reservation's <devices> list."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "source_device_id": {"type": "string"},
                "target_device_id": {"type": "string"},
            },
            "required": ["source_device_id", "target_device_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "get_device_config_schema",
        "description": (
            "Return the JSON schema that `config_payload` must satisfy when "
            "calling propose_config_change on this device. Returns "
            "{connection_type, schema, allowed_keys}. The schema is keyed by "
            "the device's driver connection_type; `schema` is null and "
            "`allowed_keys` is empty when no schema is registered for that "
            "connection_type (writes will be rejected). ALWAYS call this "
            "before propose_config_change so your payload matches what the "
            "validator accepts; guessing the shape will fail with a 422."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "device_id": {"type": "string", "description": "Device UUID"},
            },
            "required": ["device_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "list_executions_for_reservation",
        "description": (
            "List recent execution runs for the current reservation. The "
            "reservation is fixed; you do not need to supply it. Optional "
            "filters: device_id (limit to runs on one device), status "
            "(PENDING, RUNNING, SUCCESS, FAILED)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "device_id": {"type": "string"},
                "status": {
                    "type": "string",
                    "enum": ["PENDING", "RUNNING", "SUCCESS", "FAILED"],
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 100,
                    "default": 20,
                },
            },
            "additionalProperties": False,
        },
    },
]


# Iter 3 write tools. Gated by settings.ai_write_tools_enabled; consumers should
# call get_active_tool_definitions() rather than concatenating the two lists
# themselves so the gate stays in one place.
WRITE_TOOL_DEFINITIONS: list[dict[str, Any]] = [
    {
        "name": "propose_config_change",
        "description": (
            "Author a new device-config version (a draft; not yet applied). "
            "Returns {validation: 'ok'|'errors', version_id, version_number}. "
            "Does not push to the device. After authoring, call "
            "schedule_config_apply with dry_run=true so the user can review "
            "the captured command transcript before confirming the real "
            "apply via the UI. Password-typed template fields are rejected; "
            "never include credentials in config_payload."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "device_id": {"type": "string"},
                "config_payload": {"type": "object"},
                "description": {"type": "string", "maxLength": 500},
            },
            "required": ["device_id", "config_payload", "description"],
            "additionalProperties": False,
        },
    },
    {
        "name": "schedule_config_apply",
        "description": (
            "Schedule a config-version apply job. This ALWAYS runs as a dry-run: "
            "the scheduler runs the driver in simulation mode and persists the "
            "transcript for user review. You cannot run a real apply; the user "
            "must explicitly confirm via the UI before any real apply runs, and "
            "there is no parameter that lets you promote a dry-run. Pass "
            "delay_seconds (default 30, 5 to 600) to control when the dry-run "
            "fires; the server stamps the absolute time."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "device_id": {"type": "string"},
                "version_id": {"type": "string"},
                "delay_seconds": {
                    "type": "integer",
                    "minimum": 5,
                    "maximum": 600,
                    "default": 30,
                },
            },
            "required": ["device_id", "version_id"],
            "additionalProperties": False,
        },
    },
]


# Single source of truth for which tool names are write tools. Derived from
# WRITE_TOOL_DEFINITIONS so dispatch() never hardcodes the names; the gate stays
# correct if a write tool is added or renamed.
WRITE_TOOL_NAMES: frozenset[str] = frozenset(d["name"] for d in WRITE_TOOL_DEFINITIONS)


def get_active_tool_definitions() -> list[dict[str, Any]]:
    """Return the tool set the assistant should advertise, honoring the
    ai_write_tools_enabled flag. Read-only tools are always present; write
    tools are appended only when the flag is on.
    """
    if settings.ai_write_tools_enabled:
        return TOOL_DEFINITIONS + WRITE_TOOL_DEFINITIONS
    return list(TOOL_DEFINITIONS)


def _flatten_password_keys_present(payload: Any, password_keys: set[str]) -> set[str]:
    """Recursively walk `payload` and return any key in `password_keys` that
    appears anywhere. Used to reject AI writes targeting password-typed
    template fields before they reach the inventory service.

    Walks dicts and lists. Non-container values are leaves.
    """
    if not password_keys:
        return set()
    found: set[str] = set()

    def _walk(node: Any) -> None:
        if isinstance(node, dict):
            for k, v in node.items():
                if k in password_keys:
                    found.add(k)
                _walk(v)
        elif isinstance(node, list):
            for item in node:
                _walk(item)

    _walk(payload)
    return found


class ToolDispatcher:
    def __init__(
        self,
        *,
        token: str,
        reservation_id: uuid.UUID,
        http_client: httpx.AsyncClient | None = None,
        char_cap: int = DEFAULT_TOOL_RESULT_CHAR_CAP,
    ) -> None:
        self._token = token
        self._reservation_id = reservation_id
        self._owned_client = http_client is None
        self._http = http_client or httpx.AsyncClient(timeout=HTTP_TIMEOUT_SECONDS)
        self._char_cap = char_cap
        # template_id -> set of password-typed field keys, or None on fetch failure
        self._template_cache: dict[str, set[str] | None] = {}
        # device_id -> resolved schema response, or None on lookup failure
        self._schema_cache: dict[str, dict[str, Any] | None] = {}
        self._device_fetch_semaphore = asyncio.Semaphore(DEVICE_FETCH_CONCURRENCY)
        self.call_log: list[ToolCallRecord] = []
        # Side-effects produced by write tools (e.g. scheduled dry-run apply).
        # The route handler reads the most recent scheduled_apply entry and
        # surfaces it as `pending_apply` on the response so the frontend can
        # render the confirmation modal.
        self.side_effects: list[dict[str, Any]] = []

    async def aclose(self) -> None:
        if self._owned_client:
            await self._http.aclose()

    async def __aenter__(self) -> ToolDispatcher:
        return self

    async def __aexit__(self, *_exc: Any) -> None:
        await self.aclose()

    @property
    def _auth_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token}"}

    async def dispatch(self, tool_name: str, tool_input: dict[str, Any]) -> dict[str, Any]:
        """Execute one tool call. Returns {"content": <json string>, "is_error": bool}
        ready to be wrapped into an Anthropic tool_result block by the loop.

        Never raises for downstream failures; converts them to is_error=True
        so the model can recover or apologise within the same conversation.
        """
        started = time.monotonic()
        error: str | None = None
        content: Any
        try:
            # Enforce the write-tools gate at the execution boundary. Hiding a
            # write tool from get_active_tool_definitions() is not enough: a model
            # can still emit the call by name. Refuse to run it here so the gate
            # holds even when the flag is off. The ToolError flows through the
            # except branch below into an is_error result the model can recover
            # from, and the finally block still records the attempt in call_log.
            if tool_name in WRITE_TOOL_NAMES and not settings.ai_write_tools_enabled:
                raise ToolError("write tools are disabled")
            handler = getattr(self, f"_tool_{tool_name}", None)
            if handler is None:
                raise ToolError(f"unknown tool: {tool_name}")
            content = await handler(tool_input)
        except ToolError as exc:
            error = str(exc)
            content = {"is_error": True, "message": error}
        except httpx.HTTPError as exc:
            error = f"HTTP error: {exc}"
            content = {"is_error": True, "message": error}
        except Exception as exc:  # noqa: BLE001
            error = f"Unexpected error: {type(exc).__name__}: {exc}"
            content = {"is_error": True, "message": error}
        finally:
            duration_ms = int((time.monotonic() - started) * 1000)
            self.call_log.append(
                ToolCallRecord(
                    name=tool_name,
                    arguments_summary=_summarise_args(tool_input),
                    duration_ms=duration_ms,
                    error=error,
                )
            )

        body = json.dumps(content, default=str)
        if len(body) > self._char_cap:
            truncated = len(body) - self._char_cap
            body = body[: self._char_cap] + f"\n... [truncated: {truncated} chars omitted]"
        return {"content": body, "is_error": error is not None}

    # --- Tool handlers ---

    async def _tool_get_device(self, args: dict[str, Any]) -> dict[str, Any]:
        device_id = _require_uuid(args, "device_id")
        device = await self._fetch_device(device_id)
        return await self._strip_password_fields(device, device.get("template_id"))

    async def _tool_get_device_ports(self, args: dict[str, Any]) -> list[dict[str, Any]]:
        device_id = _require_uuid(args, "device_id")
        url = f"{settings.inventory_service_url.rstrip('/')}/devices/{device_id}/ports"
        resp = await self._http.get(url, headers=self._auth_headers)
        if resp.status_code == 404:
            raise ToolError(f"device {device_id} not found")
        resp.raise_for_status()
        payload = resp.json()
        ports = payload["items"] if isinstance(payload, dict) and "items" in payload else payload
        stripped: list[dict[str, Any]] = []
        for port in ports:
            stripped.append(await self._strip_password_fields(port, port.get("template_id")))
        return stripped

    async def _tool_get_device_current_config(self, args: dict[str, Any]) -> dict[str, Any]:
        device_id = _require_uuid(args, "device_id")
        # Step 1: list versions (limit=1) for latest metadata.
        list_url = (
            f"{settings.inventory_service_url.rstrip('/')}"
            f"/devices/{device_id}/config-versions?limit=1"
        )
        resp = await self._http.get(list_url, headers=self._auth_headers)
        if resp.status_code == 404:
            raise ToolError(f"device {device_id} not found")
        resp.raise_for_status()
        listing = resp.json()
        items = listing.get("items", []) if isinstance(listing, dict) else []
        if not items:
            return {"has_config": False}
        latest = items[0]
        version_id = latest.get("id")
        if not version_id:
            raise ToolError(f"device {device_id} latest config-version is missing an id")
        # Step 2: fetch the full version detail (includes the config payload).
        detail_url = (
            f"{settings.inventory_service_url.rstrip('/')}"
            f"/devices/{device_id}/config-versions/{version_id}"
        )
        detail_resp = await self._http.get(detail_url, headers=self._auth_headers)
        detail_resp.raise_for_status()
        return {"has_config": True, **detail_resp.json()}

    async def _tool_list_device_config_history(self, args: dict[str, Any]) -> list[dict[str, Any]]:
        device_id = _require_uuid(args, "device_id")
        limit = int(args.get("limit", 10))
        if not 1 <= limit <= 50:
            raise ToolError("limit must be between 1 and 50")
        url = (
            f"{settings.inventory_service_url.rstrip('/')}"
            f"/devices/{device_id}/config-versions?limit={limit}"
        )
        resp = await self._http.get(url, headers=self._auth_headers)
        if resp.status_code == 404:
            raise ToolError(f"device {device_id} not found")
        resp.raise_for_status()
        listing = resp.json()
        return listing.get("items", []) if isinstance(listing, dict) else listing

    async def _tool_get_device_config_schema(self, args: dict[str, Any]) -> dict[str, Any]:
        # Imported lazily so unit tests that do not exercise the schema-discovery
        # path do not need herd_common installed. CONFIG_SCHEMAS is keyed by
        # connection_type; absence means "no validator wired for this driver",
        # which the response reports explicitly so the model can decline rather
        # than guess a payload that will 422.
        from app.services.config_validator import CONFIG_SCHEMAS

        device_id = _require_uuid(args, "device_id")
        cache_key = str(device_id)
        cached = self._schema_cache.get(cache_key)
        if cached is not None:
            return cached

        device = await self._fetch_device(device_id)
        template_id = device.get("template_id")
        if not template_id:
            response: dict[str, Any] = {
                "connection_type": None,
                "schema": None,
                "allowed_keys": [],
                "note": (
                    "device has no template_id; cannot determine connection_type "
                    "and no schema can be validated"
                ),
            }
            self._schema_cache[cache_key] = response
            return response

        # Fetch the template to read connection_type (computed server-side from
        # the joined driver). A separate one-liner here rather than reusing
        # _password_keys_for_template's cache because that one stores only the
        # password-key set, not the full template payload.
        url = f"{settings.inventory_service_url.rstrip('/')}/templates/{template_id}"
        resp = await self._http.get(url, headers=self._auth_headers)
        if resp.status_code == 404:
            raise ToolError(f"template {template_id} not found")
        resp.raise_for_status()
        template = resp.json()
        connection_type = template.get("connection_type")

        if not connection_type:
            response = {
                "connection_type": None,
                "schema": None,
                "allowed_keys": [],
                "note": (
                    "template has no connection_type (no driver bound); device "
                    "cannot accept a config_payload"
                ),
            }
            self._schema_cache[cache_key] = response
            return response

        schema = CONFIG_SCHEMAS.get(connection_type)
        if schema is None:
            response = {
                "connection_type": connection_type,
                "schema": None,
                "allowed_keys": [],
                "note": (
                    f"no schema is registered for connection_type "
                    f"{connection_type!r}; propose_config_change will be "
                    f"rejected by the validator"
                ),
            }
        else:
            allowed = sorted(schema.get("properties", {}).keys())
            response = {
                "connection_type": connection_type,
                "schema": schema,
                "allowed_keys": allowed,
            }
        self._schema_cache[cache_key] = response
        return response

    async def _tool_find_path(self, args: dict[str, Any]) -> dict[str, Any]:
        src = _require_uuid(args, "source_device_id")
        dst = _require_uuid(args, "target_device_id")
        url = f"{settings.cabling_service_url.rstrip('/')}/pathfind"
        resp = await self._http.post(
            url,
            json={"source_device_id": str(src), "target_device_id": str(dst)},
            headers=self._auth_headers,
        )
        if resp.status_code == 404:
            raise ToolError("device(s) not found in cabling topology")
        resp.raise_for_status()
        return resp.json()

    async def _tool_list_executions_for_reservation(self, args: dict[str, Any]) -> dict[str, Any]:
        # reservation_id is injected by the dispatcher; the model never supplies it.
        params: dict[str, Any] = {"reservation_id": str(self._reservation_id)}
        if "device_id" in args:
            params["device_id"] = str(_require_uuid(args, "device_id"))
        if "status" in args:
            params["status"] = args["status"]
        params["limit"] = int(args.get("limit", 20))
        if not 1 <= params["limit"] <= 100:
            raise ToolError("limit must be between 1 and 100")
        url = f"{settings.execution_service_url.rstrip('/')}/runs"
        resp = await self._http.get(url, params=params, headers=self._auth_headers)
        if resp.status_code == 403:
            raise ToolError("execution runs require reservation ownership or admin role")
        resp.raise_for_status()
        return resp.json()

    # --- Write tools (iter 3, gated by settings.ai_write_tools_enabled) ---

    async def _tool_propose_config_change(self, args: dict[str, Any]) -> dict[str, Any]:
        device_id = _require_uuid(args, "device_id")
        payload = args.get("config_payload") or {}
        if not isinstance(payload, dict):
            raise ToolError("config_payload must be an object")
        description = (args.get("description") or "")[:500]

        # Password-field rejection BEFORE the write hits inventory. Inverts the
        # iter-2 strip helper: instead of dropping password keys on read, refuse
        # the whole write if any are present. The AI should never set
        # credentials; if it tries, fail loudly so the model can apologise.
        device = await self._fetch_device(device_id)
        password_keys = await self._password_keys_for_template(device.get("template_id"))
        if password_keys is None:
            raise ToolError("could not load device template; refusing to write")
        rejected = _flatten_password_keys_present(payload, password_keys)
        if rejected:
            raise ToolError(f"refusing to write password-typed fields: {sorted(rejected)}")

        url = f"{settings.inventory_service_url.rstrip('/')}/devices/{device_id}/config-versions"
        resp = await self._http.post(
            url,
            json={"config": payload, "description": description},
            headers=self._auth_headers,
        )
        if resp.status_code == 403:
            raise ToolError(
                "manage permission required on this device (or active reservation ownership)"
            )
        if resp.status_code == 422:
            # Validation errors return as a soft tool result so the model can
            # retry with a corrected payload in the same conversation.
            try:
                detail = resp.json().get("detail")
            except ValueError:
                detail = resp.text
            return {"validation": "errors", "detail": detail}
        resp.raise_for_status()
        body = resp.json()
        return {
            "validation": "ok",
            "version_id": body["id"],
            "version_number": body["version_number"],
        }

    async def _tool_schedule_config_apply(self, args: dict[str, Any]) -> dict[str, Any]:
        device_id = _require_uuid(args, "device_id")
        version_id = _require_uuid(args, "version_id")
        delay_seconds = int(args.get("delay_seconds", 30))
        if not 5 <= delay_seconds <= 600:
            raise ToolError("delay_seconds must be between 5 and 600")
        # The AI can only ever schedule a dry-run. This is forced server-side and
        # not read from args: the model must never be able to promote a dry-run to
        # a real apply (the human confirms in the UI via a separate endpoint).
        # The tool schema no longer exposes dry_run, but we hard-set it here too so
        # a prompt-injected or malformed args dict cannot smuggle dry_run=false
        # past the schema into a real device configuration apply.
        dry_run = True

        # Server stamps the absolute time so the model never has to reason about
        # wall-clock. The inventory schedule endpoint rejects past timestamps,
        # so a generous floor on delay_seconds keeps clock-skew races out.
        scheduled_for = (datetime.now(UTC) + timedelta(seconds=delay_seconds)).isoformat()

        url = (
            f"{settings.inventory_service_url.rstrip('/')}"
            f"/devices/{device_id}/config-versions/{version_id}/schedule"
        )
        body = {
            "scheduled_for": scheduled_for,
            "dry_run": dry_run,
            "reservation_id": str(self._reservation_id),
        }
        resp = await self._http.post(url, json=body, headers=self._auth_headers)
        if resp.status_code in (403, 422):
            try:
                detail = resp.json().get("detail", str(resp.status_code))
            except ValueError:
                detail = str(resp.status_code)
            raise ToolError(str(detail))
        resp.raise_for_status()
        data = resp.json()

        # Record the side effect for the route handler to surface as
        # `pending_apply` on the response. The frontend uses this to render
        # the confirm-or-cancel modal.
        self.side_effects.append(
            {
                "kind": "scheduled_apply",
                "job_id": data["id"],
                "version_id": str(version_id),
                "device_id": str(device_id),
                "dry_run": data["dry_run"],
                "scheduled_for": data["scheduled_for"],
            }
        )

        return {
            "job_id": data["id"],
            "dry_run": data["dry_run"],
            "scheduled_for": data["scheduled_for"],
            "status": data["status"],
        }

    # --- Helpers ---

    async def _fetch_device(self, device_id: uuid.UUID) -> dict[str, Any]:
        async with self._device_fetch_semaphore:
            url = f"{settings.inventory_service_url.rstrip('/')}/devices/{device_id}"
            resp = await self._http.get(url, headers=self._auth_headers)
        if resp.status_code == 404:
            raise ToolError(f"device {device_id} not found")
        resp.raise_for_status()
        return resp.json()

    async def _password_keys_for_template(self, template_id: str | None) -> set[str] | None:
        """Return the set of password-typed field keys for a template, or
        None if the template cannot be fetched (caller treats as
        closed-by-default and drops the whole field_data block).
        """
        if not template_id:
            return set()
        key = str(template_id)
        if key in self._template_cache:
            return self._template_cache[key]
        url = f"{settings.inventory_service_url.rstrip('/')}/templates/{template_id}"
        try:
            resp = await self._http.get(url, headers=self._auth_headers)
            resp.raise_for_status()
            template = resp.json()
        except httpx.HTTPError as exc:
            logger.warning(
                "tools_template_fetch_failed",
                extra={"template_id": key, "error": str(exc)},
            )
            self._template_cache[key] = None
            return None

        password_keys: set[str] = set()
        for section in template.get("sections", []) or []:
            for f in section.get("fields", []) or []:
                if f.get("type") == "password" and f.get("key"):
                    password_keys.add(f["key"])
        self._template_cache[key] = password_keys
        return password_keys

    async def _strip_password_fields(
        self, obj: dict[str, Any], template_id: str | None
    ) -> dict[str, Any]:
        result = dict(obj)
        if "field_data" not in result or not isinstance(result["field_data"], dict):
            return result
        password_keys = await self._password_keys_for_template(template_id)
        if password_keys is None:
            # Template fetch failed; closed-by-default: drop field_data wholesale.
            result["field_data"] = {}
            result["_field_data_stripped"] = "template fetch failed; closed-by-default"
            return result
        if password_keys:
            result["field_data"] = {
                k: v for k, v in result["field_data"].items() if k not in password_keys
            }
        return result


# --- Module-level helpers ---


def _require_uuid(args: dict[str, Any], key: str) -> uuid.UUID:
    raw = args.get(key)
    if raw is None:
        raise ToolError(f"missing required argument: {key}")
    try:
        return uuid.UUID(str(raw))
    except (ValueError, TypeError) as exc:
        raise ToolError(f"{key} must be a UUID; got {raw!r}") from exc


def _summarise_args(args: dict[str, Any]) -> str:
    """Short, log-safe representation of tool arguments."""
    if not args:
        return ""
    parts: list[str] = []
    for k, v in args.items():
        v_str = str(v)
        if len(v_str) > 32:
            v_str = v_str[:29] + "..."
        parts.append(f"{k}={v_str}")
    summary = ", ".join(parts)
    if len(summary) > 80:
        summary = summary[:77] + "..."
    return summary
