"""Commits an accepted AI proposal to cabling + reservations.

The flow:
  1. Build canvas_data from the proposal (device and network-element nodes,
     plus edges; a device-to-element edge needs a GET to inventory to pick
     the device-side port, issue #632).
  2. POST /cabling/topologies to create an empty topology.
  3. PUT /cabling/topologies/{id} with the built canvas_data.
  4. POST /cabling/topologies/{id}/validate to fail fast on an unwireable
     proposal (an edge with no physical cable path) before a reservation is
     ever created (commit-side fail-fast hardening, diagnosis option 3).
  5. POST /reservations/ for the proposal's devices, tagged with topology_id.

If step 3, 4, or 5 fails, the topology is deleted to roll back so the user
does not end up with a dangling empty topology. All upstream calls use the
caller's JWT so existing RBAC and device-visibility rules apply.
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
    """Raised when an upstream service rejects the commit.

    `message` is usually a plain string, but the commit-time wireability
    check (`_validate_topology_wireable`) raises with a structured dict
    detail (`{"error": "topology_unwireable", ...}`) so the frontend can
    narrow it the same way it narrows other structured 4xx bodies; the route
    passes `message` straight through to `HTTPException(status_code, detail)`,
    which serializes either shape unchanged.
    """

    def __init__(self, status_code: int, message: str | dict[str, Any]) -> None:
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


def _structured_detail(resp: httpx.Response) -> dict[str, Any] | None:
    """Return an HTTP error response's `detail` object, or None.

    None covers a non-JSON body, a bare-string `detail` (the common FastAPI
    HTTPException shape), or no `detail` key at all. Used to read a plain-words
    `message` out of a structured 409 (issue #870) instead of `_detail`'s
    stringified-whole-dict fallback.
    """
    try:
        body = resp.json()
    except ValueError:
        return None
    if isinstance(body, dict) and isinstance(body.get("detail"), dict):
        return body["detail"]
    return None


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
    call to inventory's dedicated ports listing endpoint. A 404 means a
    genuinely portless device and is treated as "no ports", which the caller
    already handles by skipping the attachment. A transport failure or a 5xx
    means inventory could not answer the question at all, which is not the
    same thing: silently treating it as portless would drop a user-approved
    element attachment on a mere blip (issue #717), so it raises
    CommitError(503) instead, failing the whole commit closed before any
    topology is created.
    """
    url = f"{settings.inventory_service_url.rstrip('/')}/devices/{device_id}/ports"
    try:
        resp = await client.get(url, headers=headers)
    except Exception as e:
        logger.warning("ai_commit_device_ports_fetch_failed", extra={"device_id": device_id})
        raise CommitError(503, f"Failed to fetch ports for device {device_id}: {e}") from e
    if resp.status_code == 404:
        return []
    if resp.status_code >= 500:
        logger.warning(
            "ai_commit_device_ports_fetch_failed",
            extra={"device_id": device_id, "status_code": resp.status_code},
        )
        raise CommitError(503, f"Failed to fetch ports for device {device_id}: {_detail(resp)}")
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


async def _validate_topology_wireable(
    client: httpx.AsyncClient,
    headers: dict[str, str],
    topology_id: str,
    role_by_device_id: dict[str, str],
) -> None:
    """Fail fast when the just-saved canvas has no physical wiring path.

    Diagnosis option 3: before this, an edge the AI proposed between two
    devices with no cable between them surfaced only when
    `_create_reservation` called into reservations, whose
    `_validate_topology_connectivity` 422s with a single opaque STRING detail
    ("Topology has unreachable edges in the cabling graph: ...") built from
    cabling's own validate response. Calling cabling's user-facing
    `POST /topologies/{id}/validate` here, right after the canvas PUT and
    before the reservation is ever created, gets the SAME structured
    `TopologyValidationResponse` reservations computes internally, but early
    enough to report it with the proposal's ROLE names (node ids and device
    ids mean nothing to the user) instead of a generic sentence. This is the
    first line of defense; `_create_reservation`'s own 422 stays as the
    second, in case of a race between the two calls.

    A transport failure, a 5xx, a 200 whose body is not valid JSON, or a 200
    whose body has no boolean `valid` key all mean cabling could not actually
    answer the question, which is not the same as a clean pass: every one of
    those fails closed with a 503 (the same rule `_fetch_device_ports`
    follows for issue #717, and the same rule `_fetch_fork_intended_wires`
    follows service-side). Only an explicit `valid: true` proceeds.
    """
    url = f"{settings.cabling_service_url.rstrip('/')}/topologies/{topology_id}/validate"
    try:
        resp = await client.post(url, headers=headers)
    except Exception as e:
        logger.warning("ai_commit_validate_unreachable", extra={"topology_id": topology_id})
        raise CommitError(503, f"Failed to validate topology wireability: {e}") from e
    if resp.status_code >= 500:
        logger.warning(
            "ai_commit_validate_failed",
            extra={"topology_id": topology_id, "status_code": resp.status_code},
        )
        raise CommitError(503, f"Failed to validate topology wireability: {_detail(resp)}")
    if resp.status_code >= 400:
        # Not expected against a topology this same request just created with
        # the same JWT (creator-or-admin is always satisfied), but fail
        # closed on an unexpected 4xx rather than silently proceeding.
        raise CommitError(resp.status_code, f"Failed to validate topology: {_detail(resp)}")

    try:
        result = resp.json()
    except ValueError:
        # A 200 with an unparseable body means cabling could not actually
        # answer the question either, same as a transport failure or a 5xx:
        # fail closed rather than reading silence as a pass.
        logger.warning("ai_commit_validate_unparseable_body", extra={"topology_id": topology_id})
        raise CommitError(
            503, "Failed to validate topology wireability: response body was not JSON"
        )
    if not isinstance(result, dict) or not isinstance(result.get("valid"), bool):
        # No boolean `valid` key at all is likewise an unanswerable question,
        # not an implicit pass: only an explicit `valid: true` proceeds.
        logger.warning("ai_commit_validate_missing_valid_key", extra={"topology_id": topology_id})
        raise CommitError(
            503, "Failed to validate topology wireability: response had no boolean 'valid' field"
        )
    if result["valid"]:
        return

    def _role(device_id: str | None) -> str:
        if not device_id:
            return "unknown"
        return role_by_device_id.get(device_id, device_id)

    invalid_edges = [
        {
            "edge_id": edge.get("edge_id"),
            "source_role": _role(edge.get("source_device_id")),
            "target_role": _role(edge.get("target_device_id")),
            "reason": edge.get("reason"),
        }
        for edge in result.get("invalid_edges", [])
    ]
    logger.warning(
        "ai_commit_topology_unwireable",
        extra={"topology_id": topology_id, "invalid_edge_count": len(invalid_edges)},
    )
    raise CommitError(
        422,
        {
            "error": "topology_unwireable",
            "invalid_edges": invalid_edges,
            "message": (
                f"{len(invalid_edges)} proposed connection"
                f"{'' if len(invalid_edges) == 1 else 's'} cannot be wired with the "
                "current cabling; see invalid_edges for which ones and why."
            ),
        },
    )


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
            error_text = _detail(resp)
            # A 409 driver_cannot_configure/device_has_no_driver (issue #870:
            # execution now refuses a configure the driver's contract cannot
            # run, before this endpoint bypassed that gate entirely) carries a
            # structured detail whose `message` is plain words for the
            # operator; the generic _detail(resp) above stringifies the whole
            # dict instead, which reads as a stack-trace-shaped blob.
            if resp.status_code == 409:
                structured = _structured_detail(resp)
                if structured and structured.get("error") in (
                    "driver_cannot_configure",
                    "device_has_no_driver",
                ):
                    error_text = structured.get("message") or error_text
            results.append(
                DeviceConfigResult(
                    role=device.role,
                    device_id=device.device_id,
                    status="failed",
                    error=error_text,
                )
            )
            continue
        try:
            payload = resp.json()
        except ValueError:
            payload = {}
        # Safe default (issue #720's rule, applied here per issue #870): a
        # response with no status is never a success, so a device never gets
        # counted as applied on a malformed or status-less payload.
        run_status = str(payload.get("status", "FAILED")).upper()
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
    but the canvas save, the wireability validate, or the reservation create
    fails, the topology is deleted to roll back so the user does not end up
    with a dangling empty topology. All upstream calls carry the user's JWT so
    existing RBAC rules apply (device visibility, admin-only config apply, etc.).
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
    role_by_device_id = {d.device_id: d.role for d in req.devices}

    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
        # Built inside the client block: an element attachment edge needs a
        # port lookup against inventory (D2), which reuses this same client.
        canvas_data = await _build_canvas_data(client, headers, req)
        topology_id = await _create_topology(client, headers, req.topology_name)
        try:
            await _update_topology_canvas(client, headers, topology_id, canvas_data)
            # Commit-time fail-fast (diagnosis option 3): check wireability
            # before spending a reservation create call, and before the user
            # sees reservations' generic string-detail 422.
            await _validate_topology_wireable(client, headers, topology_id, role_by_device_id)
            reservation_id = await _create_reservation(client, headers, req, topology_id)
        except CommitError:
            # Canvas save, wireability validate, or reservation create failed:
            # delete the empty topology so the user does not end up with a
            # dangling stub. This rollback is best-effort and swallows errors
            # so a delete failure does not mask the root cause.
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
