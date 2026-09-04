"""Commits an accepted AI proposal to cabling + reservations.

The flow:
  1. Build canvas_data from the proposal (device and network-element nodes,
     plus edges; a device-to-element edge needs a GET to inventory to pick
     the device-side port, issue #632).
  2. POST /cabling/topologies to create an empty topology.
  3. PUT /cabling/topologies/{id} with the built canvas_data.
  4. POST /reservations/ for the proposal's devices, tagged with topology_id.

If step 3 or 4 fails, the topology is deleted to roll back so the user does
not end up with a dangling empty topology. All upstream calls use the caller's
JWT so existing RBAC and device-visibility rules apply.
"""

import logging
import re
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


# Splits a port name into alternating non-digit/digit runs so "eth2" sorts
# before "eth10" (issue #632, D2's natural port order). re.split with a
# capturing group always alternates str/int-able chunks at the same parity
# for any port name, so comparing two keys never hits a str-vs-int
# comparison, which sorted() would otherwise raise on.
_PORT_NAME_RUNS = re.compile(r"(\d+)")


def _natural_port_key(name: str) -> tuple[Any, ...]:
    return tuple(int(part) if part.isdigit() else part for part in _PORT_NAME_RUNS.split(name))


async def _fetch_device_ports(
    client: httpx.AsyncClient, headers: dict[str, str], device_id: str
) -> list[dict[str, Any]]:
    """Fetch a device's ports, sorted in natural name order.

    CommitDevice.device (the raw inventory DeviceResponse payload the
    frontend forwards) carries no ports field (services/inventory/app/schemas
    /device.py's DeviceResponse has none), so port selection needs its own
    call to inventory's dedicated ports listing endpoint. Never raises: a
    fetch failure (unreachable inventory, 404, etc.) is logged and treated as
    "no ports", which the caller already handles by skipping the attachment.
    """
    url = f"{settings.inventory_service_url.rstrip('/')}/devices/{device_id}/ports"
    try:
        resp = await client.get(url, headers=headers)
    except Exception:
        logger.warning("ai_commit_device_ports_fetch_failed", extra={"device_id": device_id})
        return []
    if resp.status_code >= 400:
        logger.warning(
            "ai_commit_device_ports_fetch_failed",
            extra={"device_id": device_id, "status_code": resp.status_code},
        )
        return []
    try:
        ports = resp.json()
    except ValueError:
        return []
    return sorted(ports, key=lambda p: _natural_port_key(p["name"]))


async def _select_element_port(
    client: httpx.AsyncClient,
    headers: dict[str, str],
    device_role: str,
    device_id: str,
    ports_cache: dict[str, list[dict[str, Any]]],
    claimed_ports: dict[str, set[str]],
) -> dict[str, Any] | None:
    """Pick the next free port for one device's element attachment (D2).

    Ports come back pre-sorted in natural name order; the first one not
    already claimed by an earlier attachment of the SAME device in this
    proposal wins, so two attachments from one device to two elements land
    on two distinct ports. Cached per device role so a device with several
    attachments triggers one HTTP fetch, not one per edge.
    """
    if device_role not in ports_cache:
        ports_cache[device_role] = await _fetch_device_ports(client, headers, device_id)
    claimed = claimed_ports.setdefault(device_role, set())
    for port in ports_cache[device_role]:
        if port["id"] not in claimed:
            claimed.add(port["id"])
            return port
    return None


async def _build_canvas_data(
    client: httpx.AsyncClient, headers: dict[str, str], req: CommitRequest
) -> dict[str, Any]:
    """Build a React-Flow-compatible canvas_data from the accepted proposal.

    The frontend renders this via the standard topology load path, so the
    shape has to match what `loadCanvas` expects: nodes keyed by a canvas
    UUID with `device`/`label`/`topologyType` (or, for a network element,
    `element`), edges keyed by UUID with `layer`, referencing the node ids as
    `source`/`target`.

    Network elements (issue #632, ADR 0012) persist as one `networkElementNode`
    per proposed element, positioned in a row below the devices. A device-to-
    element edge needs a concrete device-side port, which the model never
    sees (D2: port selection is the committer's job, not the LLM's), so this
    is async and takes the caller's httpx client to fetch each attaching
    device's ports on demand.
    """
    role_to_node_id: dict[str, str] = {}
    device_node_id_by_role: dict[str, str] = {}
    device_id_by_role: dict[str, str] = {}
    element_node_id_by_role: dict[str, str] = {}
    nodes: list[dict[str, Any]] = []
    base_x, base_y, step_x = 200, 200, 220

    for idx, proposed in enumerate(req.devices):
        node_id = str(uuid.uuid4())
        role_to_node_id[proposed.role] = node_id
        device_node_id_by_role[proposed.role] = node_id
        device_id_by_role[proposed.role] = proposed.device_id
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

    element_row_y = base_y + step_x
    for idx, proposed in enumerate(req.elements):
        node_id = str(uuid.uuid4())
        role_to_node_id[proposed.role] = node_id
        element_node_id_by_role[proposed.role] = node_id
        nodes.append(
            {
                "id": node_id,
                "type": "networkElementNode",
                "position": {"x": base_x + idx * step_x, "y": element_row_y},
                "data": {
                    "element": {
                        "id": str(uuid.uuid4()),
                        "element_type": proposed.element_type,
                        "label": proposed.label,
                        "attrs": proposed.attrs,
                    }
                },
            }
        )

    edges: list[dict[str, Any]] = []
    ports_cache: dict[str, list[dict[str, Any]]] = {}
    claimed_ports: dict[str, set[str]] = {}

    for edge in req.edges:
        source_is_device = edge.source_role in device_node_id_by_role
        target_is_device = edge.target_role in device_node_id_by_role
        source_is_element = edge.source_role in element_node_id_by_role
        target_is_element = edge.target_role in element_node_id_by_role

        if source_is_device and target_is_device:
            # Device-to-device: unchanged from the pre-#632 shape.
            edges.append(
                {
                    "id": str(uuid.uuid4()),
                    "source": device_node_id_by_role[edge.source_role],
                    "target": device_node_id_by_role[edge.target_role],
                    "data": {"layer": edge.layer},
                }
            )
            continue

        if (source_is_device and target_is_element) or (source_is_element and target_is_device):
            device_role = edge.source_role if source_is_device else edge.target_role
            element_role = edge.target_role if source_is_device else edge.source_role
            port = await _select_element_port(
                client,
                headers,
                device_role,
                device_id_by_role[device_role],
                ports_cache,
                claimed_ports,
            )
            if port is None:
                # No port left to attach with (zero ports on the device, or
                # every port already claimed by another element attachment
                # of this same device in the proposal): skip the edge rather
                # than emit an attachment with no source_port_name, which
                # cabling's classify_element_edge would reject as
                # element_edge_no_port anyway.
                logger.warning(
                    "ai_commit_element_attachment_skipped_no_port",
                    extra={"role": device_role, "device_id": device_id_by_role[device_role]},
                )
                continue
            edges.append(
                {
                    "id": str(uuid.uuid4()),
                    "source": device_node_id_by_role[device_role],
                    "target": element_node_id_by_role[element_role],
                    "data": {
                        "layer": edge.layer,
                        "source_port_id": port["id"],
                        "source_port_name": port["name"],
                    },
                }
            )
            continue

        # Neither side resolved to a device-plus-element pair: a dangling
        # role (unknown on one or both sides) or an element_to_element edge
        # (rejected upstream by the generator's validation, D4, but a direct
        # /commit caller could still send one). Both are silently dropped,
        # matching the pre-#632 dangling-role treatment.
        continue

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

    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
        # Built inside the client block: an element attachment edge needs a
        # port lookup against inventory (D2), which reuses this same client.
        canvas_data = await _build_canvas_data(client, headers, req)
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
