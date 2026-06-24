"""Commits an accepted AI proposal to cabling + reservations.

The flow:
  1. POST /cabling/topologies to create an empty topology.
  2. PUT /cabling/topologies/{id} with canvas_data built from the proposal.
  3. POST /reservations/ for the proposal's devices, tagged with topology_id.

If step 2 or 3 fails, the topology is deleted to roll back so the user does
not end up with a dangling empty topology. All upstream calls use the caller's
JWT so existing RBAC and device-visibility rules apply.
"""

import logging
import uuid
from typing import Any

import httpx

from app.config import settings
from app.schemas.generate import (
    CommitRequest,
    CommitResponse,
    DeviceConfigResult,
)
from app.services.config_validator import (
    ConfigValidationError,
    validate_device_config,
)

logger = logging.getLogger(__name__)

HTTP_TIMEOUT = 15.0


class CommitError(Exception):
    """Raised when an upstream service rejects the commit."""

    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.message = message


def _detail(resp: httpx.Response) -> str:
    try:
        body = resp.json()
    except ValueError:
        return resp.text or f"HTTP {resp.status_code}"
    if isinstance(body, dict):
        return str(body.get("detail", body))
    return str(body)


def _build_canvas_data(req: CommitRequest) -> dict[str, Any]:
    """Build a React-Flow-compatible canvas_data from the accepted proposal.

    The frontend renders this via the standard topology load path, so the
    shape has to match what `loadCanvas` expects: nodes keyed by a canvas
    UUID with `device`/`label`/`topologyType`, edges keyed by UUID with
    `layer`, referencing the node ids as `source`/`target`.
    """
    role_to_node_id: dict[str, str] = {}
    nodes: list[dict[str, Any]] = []
    base_x, base_y, step_x = 200, 200, 220

    for idx, proposed in enumerate(req.devices):
        node_id = str(uuid.uuid4())
        role_to_node_id[proposed.role] = node_id
        position = proposed.position or {"x": base_x + idx * step_x, "y": base_y}
        nodes.append(
            {
                "id": node_id,
                "type": "deviceNode",
                "position": position,
                "data": {
                    "device": {"id": proposed.device_id},
                    "label": proposed.role,
                    "topologyType": "PHYSICAL",
                },
            }
        )

    edges: list[dict[str, Any]] = []
    for edge in req.edges:
        source_id = role_to_node_id.get(edge.source_role)
        target_id = role_to_node_id.get(edge.target_role)
        if not source_id or not target_id:
            continue
        edges.append(
            {
                "id": str(uuid.uuid4()),
                "source": source_id,
                "target": target_id,
                "data": {"layer": edge.layer},
            }
        )

    return {"nodes": nodes, "edges": edges, "selectedEdgeLayer": "L2"}


async def _create_topology(client: httpx.AsyncClient, headers: dict[str, str], name: str) -> str:
    url = f"{settings.cabling_service_url.rstrip('/')}/topologies"
    resp = await client.post(url, json={"name": name}, headers=headers)
    if resp.status_code >= 400:
        raise CommitError(resp.status_code, f"Failed to create topology: {_detail(resp)}")
    return resp.json()["id"]


async def _update_topology_canvas(
    client: httpx.AsyncClient,
    headers: dict[str, str],
    topology_id: str,
    canvas_data: dict[str, Any],
) -> None:
    url = f"{settings.cabling_service_url.rstrip('/')}/topologies/{topology_id}"
    resp = await client.put(url, json={"canvas_data": canvas_data}, headers=headers)
    if resp.status_code >= 400:
        raise CommitError(resp.status_code, f"Failed to save canvas: {_detail(resp)}")


async def _delete_topology(
    client: httpx.AsyncClient, headers: dict[str, str], topology_id: str
) -> None:
    url = f"{settings.cabling_service_url.rstrip('/')}/topologies/{topology_id}"
    try:
        await client.delete(url, headers=headers)
    except Exception:
        logger.exception("rollback_topology_delete_failed", extra={"topology_id": topology_id})


async def _create_reservation(
    client: httpx.AsyncClient,
    headers: dict[str, str],
    req: CommitRequest,
    topology_id: str,
) -> str:
    url = f"{settings.reservations_service_url.rstrip('/')}/"
    body = {
        "device_ids": [d.device_id for d in req.devices],
        "topology_id": topology_id,
        "purpose": req.purpose,
        "start_time": req.start_time.isoformat(),
        "end_time": req.end_time.isoformat(),
    }
    resp = await client.post(url, json=body, headers=headers)
    if resp.status_code >= 400:
        raise CommitError(resp.status_code, f"Failed to create reservation: {_detail(resp)}")
    return resp.json()["id"]


async def _apply_configs(
    client: httpx.AsyncClient,
    headers: dict[str, str],
    req: CommitRequest,
    user_id: str,
    reservation_id: str,
) -> list[DeviceConfigResult]:
    """Call /execution/execute per device with config; never raises.

    Per-device failures are captured as result entries. The /execute endpoint
    is admin-only, so non-admins will see 403 entries rather than a blocked
    commit. Config is optional, so devices without a config are marked
    'skipped'.
    """
    url = f"{settings.execution_service_url.rstrip('/')}/execute"
    results: list[DeviceConfigResult] = []
    for device in req.devices:
        if not device.config:
            results.append(
                DeviceConfigResult(role=device.role, device_id=device.device_id, status="skipped")
            )
            continue
        body = {
            "device_id": device.device_id,
            "action": "configure",
            "user_id": user_id,
            "reservation_id": reservation_id,
            "method_kwargs": device.config,
        }
        try:
            resp = await client.post(url, json=body, headers=headers)
        except Exception as exc:
            results.append(
                DeviceConfigResult(
                    role=device.role,
                    device_id=device.device_id,
                    status="failed",
                    error=f"request failed: {exc}",
                )
            )
            continue
        if resp.status_code >= 400:
            results.append(
                DeviceConfigResult(
                    role=device.role,
                    device_id=device.device_id,
                    status="failed",
                    error=_detail(resp),
                )
            )
            continue
        try:
            payload = resp.json()
        except ValueError:
            payload = {}
        run_status = str(payload.get("status", "SUCCESS")).upper()
        results.append(
            DeviceConfigResult(
                role=device.role,
                device_id=device.device_id,
                status="success" if run_status == "SUCCESS" else "failed",
                error=payload.get("error"),
                run_id=payload.get("id"),
            )
        )
    return results


async def commit_proposal(
    req: CommitRequest,
    user_bearer_token: str,
    user_id: str,
) -> CommitResponse:
    """Commit an AI proposal: create topology + reservation, optionally run configs.

    Validates every device config upfront so the request fails fast with a 422
    before we write to any upstream service. This is the guardrail between
    LLM-proposed kwargs and driver method_kwargs. If topology creation succeeds
    but canvas or reservation fails, the topology is deleted to roll back so the
    user does not end up with a dangling empty topology. All upstream calls
    carry the user's JWT so existing RBAC rules apply (device visibility, admin-only
    config apply, etc.).
    """
    # Validate every device's config up-front so the request fails fast with
    # a clear 422 before we write to cabling or reservations. This is the
    # guardrail between LLM-proposed kwargs and driver method_kwargs.
    for device in req.devices:
        try:
            validate_device_config(device.connection_type, device.config, role=device.role)
        except ConfigValidationError as exc:
            raise CommitError(422, str(exc)) from exc

    headers = {"Authorization": f"Bearer {user_bearer_token}"}
    canvas_data = _build_canvas_data(req)

    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
        topology_id = await _create_topology(client, headers, req.topology_name)
        try:
            await _update_topology_canvas(client, headers, topology_id, canvas_data)
            reservation_id = await _create_reservation(client, headers, req, topology_id)
        except CommitError:
            # Canvas or reservation failed: delete the empty topology so the user
            # does not end up with a dangling stub. This rollback is best-effort and
            # swallows errors so a delete failure does not mask the root cause.
            await _delete_topology(client, headers, topology_id)
            raise
        except Exception as e:
            await _delete_topology(client, headers, topology_id)
            raise CommitError(502, f"Unexpected upstream failure: {e}") from e

        config_results: list[DeviceConfigResult] = []
        if req.apply_configs:
            # Config apply is optional and never blocks the commit. Per-device
            # failures (403, 404, etc.) are captured and returned as result entries
            # so the user sees partial success (e.g., 2 of 3 configured).
            config_results = await _apply_configs(client, headers, req, user_id, reservation_id)

    logger.info(
        "ai_proposal_committed",
        extra={
            "topology_id": topology_id,
            "reservation_id": reservation_id,
            "device_count": len(req.devices),
            "apply_configs": req.apply_configs,
            "config_failures": sum(1 for r in config_results if r.status == "failed"),
        },
    )
    return CommitResponse(
        topology_id=topology_id,
        reservation_id=reservation_id,
        config_results=config_results,
    )
