"""Targeted unit tests closing service-layer, schema, expiration, and reporting
coverage gaps.

These exercise branches the existing suites skip: the internal-token device
fetch helpers (no-token and 404 paths), topology connectivity validation, the
advisory-lock Postgres branch, the update-reservation extend/add/remove device
paths, the cancel/release exclusive-device selection, the expiration_loop
exception handling, and a few small schema/reporting validators. All external
HTTP is mocked; NATS publishing is stubbed; no real DB or network is used.

It also holds a guard on expiration_loop's source (RES-SWEEP-10, issue #1531): the
calls each tick awaits must be exactly the four steps and the sleep. What that scan
reads and its Limits are in the docstring of `_per_tick_awaits`.
"""

import ast
import inspect
import textwrap
import uuid
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from app.models.reservation import (
    Reservation,
    ReservationStatus,
    TopologyType,
)
from app.schemas.reservation import ReservationUpdate, _naive_as_utc
from app.services.reservation_service import (
    TopologyDeviceNotMember,
    TopologyRoutingIntentInvalid,
    _acquire_device_locks,
    _fetch_devices_best_effort,
    _release_exclusive_devices_best_effort,
    _validate_topology_connectivity,
    cancel_reservation,
    release_reservation,
    update_reservation,
)
from sqlalchemy import select

from tests._harness import TestSessionLocal

USER_ID = uuid.uuid4()
DEVICE_A = uuid.uuid4()
DEVICE_B = uuid.uuid4()


@pytest.fixture(autouse=True)
def _clock():
    """Read the clock when each test starts, never at import (#1561)."""
    global NOW
    NOW = datetime.now(timezone.utc)


def _make_device(device_id, topology="PHYSICAL", status="AVAILABLE", exclusive=True):
    return {
        "id": str(device_id),
        "name": f"dev-{str(device_id)[:8]}",
        "topology_type": topology,
        "status": status,
        "exclusive": exclusive,
    }


async def _insert_reservation(
    db,
    user_id=None,
    device_ids=None,
    status=ReservationStatus.ACTIVE,
    start_offset_h=1,
    end_offset_h=3,
    topology_id=None,
):
    res = Reservation(
        user_id=user_id or USER_ID,
        device_ids=[str(d) for d in (device_ids or [DEVICE_A])],
        topology_id=topology_id,
        topology_type=TopologyType.PHYSICAL,
        purpose="test",
        start_time=NOW + timedelta(hours=start_offset_h),
        end_time=NOW + timedelta(hours=end_offset_h),
        status=status,
    )
    db.add(res)
    await db.commit()
    await db.refresh(res)
    return res


def _mock_httpx_client(get_resp=None, post_resp=None):
    client = AsyncMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    if get_resp is not None:
        client.get = AsyncMock(return_value=get_resp)
    if post_resp is not None:
        client.post = AsyncMock(return_value=post_resp)
    return client


# --- _fetch_devices_best_effort: no-token and 404 branches ---


@pytest.mark.asyncio
async def test_fetch_devices_best_effort_no_token_returns_runtime_errors():
    """Without an internal token the helper returns one RuntimeError per device
    so callers fall back to assuming every device is exclusive."""
    with patch("app.services.reservation_service.settings") as mock_settings:
        mock_settings.internal_api_token = ""
        results = await _fetch_devices_best_effort([DEVICE_A, DEVICE_B])
    assert len(results) == 2
    assert all(isinstance(r, RuntimeError) for r in results)


@pytest.mark.asyncio
async def test_fetch_devices_best_effort_404_is_value_error_entry():
    """A 404 on one device surfaces as a per-device ValueError entry (not a raise)."""
    resp = MagicMock()
    resp.status_code = 404
    client = _mock_httpx_client(get_resp=resp)
    with (
        patch("app.services.reservation_service.httpx.AsyncClient", return_value=client),
        patch("app.services.reservation_service.settings") as mock_settings,
    ):
        mock_settings.internal_api_token = "tok"
        mock_settings.inventory_service_url = "http://inv"
        results = await _fetch_devices_best_effort([DEVICE_A])
    assert len(results) == 1
    assert isinstance(results[0], ValueError)


# --- _validate_topology_connectivity ---


@pytest.mark.asyncio
async def test_validate_topology_connectivity_valid():
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {"valid": True}
    client = _mock_httpx_client(post_resp=resp)
    with (
        patch("app.services.reservation_service.httpx.AsyncClient", return_value=client),
        patch("app.services.reservation_service.settings") as mock_settings,
    ):
        mock_settings.internal_api_token = "tok"
        mock_settings.cabling_service_url = "http://cab"
        await _validate_topology_connectivity(uuid.uuid4(), [])


@pytest.mark.asyncio
async def test_validate_topology_connectivity_uses_20s_timeout():
    """S13 review fix, round 2: this call needs headroom over cabling's own
    12s L3-pass deadline, so it uses 20s, not the usual 10s."""
    from app.services import reservation_service

    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {"valid": True}
    client = _mock_httpx_client(post_resp=resp)
    with (
        patch("app.services.reservation_service.httpx.AsyncClient", return_value=client),
        patch("app.services.reservation_service.settings") as mock_settings,
    ):
        mock_settings.internal_api_token = "tok"
        mock_settings.cabling_service_url = "http://cab"
        await _validate_topology_connectivity(uuid.uuid4(), [])
    assert client.post.call_args.kwargs["timeout"] == 20.0
    assert reservation_service._VALIDATE_TOPOLOGY_TIMEOUT_SECONDS == 20.0


@pytest.mark.asyncio
async def test_validate_topology_connectivity_404_is_noop():
    """Topology deleted between selection and submit returns 404, treated as no
    validation rather than a hard failure."""
    resp = MagicMock()
    resp.status_code = 404
    client = _mock_httpx_client(post_resp=resp)
    with (
        patch("app.services.reservation_service.httpx.AsyncClient", return_value=client),
        patch("app.services.reservation_service.settings") as mock_settings,
    ):
        mock_settings.internal_api_token = "tok"
        mock_settings.cabling_service_url = "http://cab"
        await _validate_topology_connectivity(uuid.uuid4(), [])


@pytest.mark.asyncio
async def test_validate_topology_connectivity_server_error_raises_runtime():
    resp = MagicMock()
    resp.status_code = 500
    resp.text = "boom"
    client = _mock_httpx_client(post_resp=resp)
    with (
        patch("app.services.reservation_service.httpx.AsyncClient", return_value=client),
        patch("app.services.reservation_service.settings") as mock_settings,
    ):
        mock_settings.internal_api_token = "tok"
        mock_settings.cabling_service_url = "http://cab"
        with pytest.raises(RuntimeError, match="Cabling validation returned 500"):
            await _validate_topology_connectivity(uuid.uuid4(), [])


@pytest.mark.asyncio
async def test_validate_topology_connectivity_transport_error_raises_runtime():
    client = AsyncMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    client.post = AsyncMock(side_effect=httpx.ConnectError("down"))
    with (
        patch("app.services.reservation_service.httpx.AsyncClient", return_value=client),
        patch("app.services.reservation_service.settings") as mock_settings,
    ):
        mock_settings.internal_api_token = "tok"
        mock_settings.cabling_service_url = "http://cab"
        with pytest.raises(RuntimeError, match="Failed to contact cabling service"):
            await _validate_topology_connectivity(uuid.uuid4(), [])


@pytest.mark.asyncio
async def test_validate_topology_connectivity_invalid_edges_raises_value_error():
    """Unreachable edges are summarized (first five) with an "and N more" suffix."""
    invalid = [{"edge_id": f"e{i}", "reason": "no path"} for i in range(7)]
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {"valid": False, "invalid_edges": invalid}
    client = _mock_httpx_client(post_resp=resp)
    with (
        patch("app.services.reservation_service.httpx.AsyncClient", return_value=client),
        patch("app.services.reservation_service.settings") as mock_settings,
    ):
        mock_settings.internal_api_token = "tok"
        mock_settings.cabling_service_url = "http://cab"
        with pytest.raises(ValueError, match="unreachable edges") as excinfo:
            await _validate_topology_connectivity(uuid.uuid4(), [])
    # 7 invalid edges -> first 5 listed, "and 2 more" appended.
    assert "and 2 more" in str(excinfo.value)
    assert "e0 (no path)" in str(excinfo.value)


# --- _validate_topology_connectivity: invalid_routes folding (ADR 0014 phase 1,
# issue #34) ---


@pytest.mark.asyncio
async def test_validate_topology_connectivity_invalid_routes_folded_into_summary():
    """invalid_routes entries are summarized as "<node_id>[<index or '-'>]
    (<reason>)" in their OWN sentence (R7 review fix on 2ade362c), separate from
    the edges-only sentence, since a routes-only failure carries no unreachable
    edges at all."""
    device_id = str(uuid.uuid4())
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {
        "valid": False,
        "invalid_edges": [],
        "invalid_routes": [
            {"node_id": "n0", "device_id": device_id, "index": 0, "reason": "l3_bad_destination"}
        ],
    }
    client = _mock_httpx_client(post_resp=resp)
    with (
        patch("app.services.reservation_service.httpx.AsyncClient", return_value=client),
        patch("app.services.reservation_service.settings") as mock_settings,
    ):
        mock_settings.internal_api_token = "tok"
        mock_settings.cabling_service_url = "http://cab"
        with pytest.raises(ValueError, match="invalid routing intent") as excinfo:
            await _validate_topology_connectivity(uuid.uuid4(), [])
    message = str(excinfo.value)
    assert "n0[0] (l3_bad_destination)" in message
    assert "unreachable edges" not in message


@pytest.mark.asyncio
async def test_validate_topology_connectivity_invalid_routes_switch_level_reason_uses_dash_index():
    """A switch-level refusal (index null) renders as "[-]", not "[None]"."""
    device_id = str(uuid.uuid4())
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {
        "valid": False,
        "invalid_edges": [],
        "invalid_routes": [
            {
                "node_id": "n0",
                "device_id": device_id,
                "index": None,
                "reason": "l3_switch_unattached",
            }
        ],
    }
    client = _mock_httpx_client(post_resp=resp)
    with (
        patch("app.services.reservation_service.httpx.AsyncClient", return_value=client),
        patch("app.services.reservation_service.settings") as mock_settings,
    ):
        mock_settings.internal_api_token = "tok"
        mock_settings.cabling_service_url = "http://cab"
        with pytest.raises(ValueError) as excinfo:
            await _validate_topology_connectivity(uuid.uuid4(), [])
    assert "n0[-] (l3_switch_unattached)" in str(excinfo.value)


@pytest.mark.asyncio
async def test_validate_topology_connectivity_invalid_edges_and_routes_combined_first_five():
    """Edge and route problems produce two joined sentences (R7 review fix on
    2ade362c): edges keep their own first-five truncation, routes keep theirs,
    and both sentences appear joined with "; " when both are non-empty."""
    device_id = str(uuid.uuid4())
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {
        "valid": False,
        "invalid_edges": [{"edge_id": f"e{i}", "reason": "no_path"} for i in range(4)],
        "invalid_routes": [
            {"node_id": "n0", "device_id": device_id, "index": 0, "reason": "l3_bad_destination"}
        ],
    }
    client = _mock_httpx_client(post_resp=resp)
    with (
        patch("app.services.reservation_service.httpx.AsyncClient", return_value=client),
        patch("app.services.reservation_service.settings") as mock_settings,
    ):
        mock_settings.internal_api_token = "tok"
        mock_settings.cabling_service_url = "http://cab"
        with pytest.raises(ValueError) as excinfo:
            await _validate_topology_connectivity(uuid.uuid4(), [])
    message = str(excinfo.value)
    assert "unreachable edges in the cabling graph: e0 (no_path)" in message
    assert "invalid routing intent: n0[0] (l3_bad_destination)" in message
    assert "; " in message
    assert " and " not in message  # 4 edges, 1 route: neither list truncates


@pytest.mark.asyncio
async def test_validate_topology_connectivity_503_from_cabling_raises_runtime():
    """A 503 (ADR 0014 Decision 5's l3_config_unavailable refusal) is refused the
    same way any other cabling >=400 status is: RuntimeError, which the reservation
    create route maps to a refused booking."""
    resp = MagicMock()
    resp.status_code = 503
    resp.text = '{"error": "l3_config_unavailable"}'
    client = _mock_httpx_client(post_resp=resp)
    with (
        patch("app.services.reservation_service.httpx.AsyncClient", return_value=client),
        patch("app.services.reservation_service.settings") as mock_settings,
    ):
        mock_settings.internal_api_token = "tok"
        mock_settings.cabling_service_url = "http://cab"
        with pytest.raises(RuntimeError, match="Cabling validation returned 503"):
            await _validate_topology_connectivity(uuid.uuid4(), [])


# --- S8 review fix, round 2: structured TopologyRoutingIntentInvalid ---


@pytest.mark.asyncio
async def test_validate_topology_connectivity_routes_only_raises_structured_type():
    """A routes-only failure raises TopologyRoutingIntentInvalid (a ValueError
    subclass), carrying the raw invalid_routes list."""
    device_id = str(uuid.uuid4())
    raw_invalid_routes = [
        {"node_id": "n0", "device_id": device_id, "index": 0, "reason": "l3_bad_destination"}
    ]
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {
        "valid": False,
        "invalid_edges": [],
        "invalid_routes": raw_invalid_routes,
    }
    client = _mock_httpx_client(post_resp=resp)
    with (
        patch("app.services.reservation_service.httpx.AsyncClient", return_value=client),
        patch("app.services.reservation_service.settings") as mock_settings,
    ):
        mock_settings.internal_api_token = "tok"
        mock_settings.cabling_service_url = "http://cab"
        with pytest.raises(TopologyRoutingIntentInvalid) as excinfo:
            await _validate_topology_connectivity(uuid.uuid4(), [])
    assert excinfo.value.invalid_routes == raw_invalid_routes
    assert "n0[0] (l3_bad_destination)" in str(excinfo.value)


@pytest.mark.asyncio
async def test_validate_topology_connectivity_edges_only_raises_plain_value_error():
    """An edges-only failure keeps raising a plain ValueError, NOT
    TopologyRoutingIntentInvalid, so an edge-only failure never carries a
    (necessarily empty) invalid_routes list through the structured type."""
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {
        "valid": False,
        "invalid_edges": [{"edge_id": "e0", "reason": "no_path"}],
        "invalid_routes": [],
    }
    client = _mock_httpx_client(post_resp=resp)
    with (
        patch("app.services.reservation_service.httpx.AsyncClient", return_value=client),
        patch("app.services.reservation_service.settings") as mock_settings,
    ):
        mock_settings.internal_api_token = "tok"
        mock_settings.cabling_service_url = "http://cab"
        with pytest.raises(ValueError) as excinfo:
            await _validate_topology_connectivity(uuid.uuid4(), [])
    assert not isinstance(excinfo.value, TopologyRoutingIntentInvalid)


@pytest.mark.asyncio
async def test_validate_topology_connectivity_both_failing_raises_structured_type():
    """Both edges and routes failing still raises TopologyRoutingIntentInvalid
    (routes win the type), with the edge summary folded into its message."""
    device_id = str(uuid.uuid4())
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {
        "valid": False,
        "invalid_edges": [{"edge_id": "e0", "reason": "no_path"}],
        "invalid_routes": [
            {"node_id": "n0", "device_id": device_id, "index": 0, "reason": "l3_bad_destination"}
        ],
    }
    client = _mock_httpx_client(post_resp=resp)
    with (
        patch("app.services.reservation_service.httpx.AsyncClient", return_value=client),
        patch("app.services.reservation_service.settings") as mock_settings,
    ):
        mock_settings.internal_api_token = "tok"
        mock_settings.cabling_service_url = "http://cab"
        with pytest.raises(TopologyRoutingIntentInvalid) as excinfo:
            await _validate_topology_connectivity(uuid.uuid4(), [])
    assert "e0 (no_path)" in str(excinfo.value)
    assert "n0[0] (l3_bad_destination)" in str(excinfo.value)


@pytest.mark.asyncio
async def test_validate_topology_connectivity_invalid_with_no_details_reported():
    """valid=false with both lists empty raises a plain, informative ValueError
    rather than an empty-message exception."""
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {"valid": False, "invalid_edges": [], "invalid_routes": []}
    client = _mock_httpx_client(post_resp=resp)
    with (
        patch("app.services.reservation_service.httpx.AsyncClient", return_value=client),
        patch("app.services.reservation_service.settings") as mock_settings,
    ):
        mock_settings.internal_api_token = "tok"
        mock_settings.cabling_service_url = "http://cab"
        with pytest.raises(ValueError, match="no details reported"):
            await _validate_topology_connectivity(uuid.uuid4(), [])


# --- _validate_topology_connectivity device membership (issue #701 phase 2) ---


@pytest.mark.asyncio
async def test_validate_topology_connectivity_partial_device_set_refused():
    """A canvas naming a device outside the booking raises TopologyDeviceNotMember
    with every offending device id, sorted."""
    foreign = str(uuid.uuid4())
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {
        "valid": True,
        "invalid_edges": [],
        "device_ids": sorted([str(DEVICE_A), foreign]),
    }
    client = _mock_httpx_client(post_resp=resp)
    with (
        patch("app.services.reservation_service.httpx.AsyncClient", return_value=client),
        patch("app.services.reservation_service.settings") as mock_settings,
    ):
        mock_settings.internal_api_token = "tok"
        mock_settings.cabling_service_url = "http://cab"
        with pytest.raises(TopologyDeviceNotMember) as excinfo:
            await _validate_topology_connectivity(uuid.uuid4(), [DEVICE_A])
    assert excinfo.value.device_ids == [foreign]


@pytest.mark.asyncio
async def test_validate_topology_connectivity_full_device_set_passes():
    """Booking every canvas device (in any order) never raises."""
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {
        "valid": True,
        "invalid_edges": [],
        "device_ids": sorted(str(d) for d in (DEVICE_A, DEVICE_B)),
    }
    client = _mock_httpx_client(post_resp=resp)
    with (
        patch("app.services.reservation_service.httpx.AsyncClient", return_value=client),
        patch("app.services.reservation_service.settings") as mock_settings,
    ):
        mock_settings.internal_api_token = "tok"
        mock_settings.cabling_service_url = "http://cab"
        await _validate_topology_connectivity(uuid.uuid4(), [DEVICE_B, DEVICE_A])


@pytest.mark.asyncio
async def test_validate_topology_connectivity_membership_checked_before_connectivity():
    """A canvas that is BOTH missing a booked device and has unreachable edges
    refuses on membership first: the authorization boundary is checked before the
    (informational-by-comparison) physical-path check."""
    foreign = str(uuid.uuid4())
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {
        "valid": False,
        "invalid_edges": [{"edge_id": "e1", "reason": "no_path"}],
        "device_ids": [foreign],
    }
    client = _mock_httpx_client(post_resp=resp)
    with (
        patch("app.services.reservation_service.httpx.AsyncClient", return_value=client),
        patch("app.services.reservation_service.settings") as mock_settings,
    ):
        mock_settings.internal_api_token = "tok"
        mock_settings.cabling_service_url = "http://cab"
        with pytest.raises(TopologyDeviceNotMember) as excinfo:
            await _validate_topology_connectivity(uuid.uuid4(), [DEVICE_A])
    assert excinfo.value.device_ids == [foreign]


@pytest.mark.asyncio
async def test_validate_topology_connectivity_element_nodes_never_counted():
    """A network element node is never a device: cabling's device_ids field
    already excludes it (mirrored here as a canvas with one real device), so
    booking only that device passes even though the canvas also carries an
    element attachment."""
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {"valid": True, "invalid_edges": [], "device_ids": [str(DEVICE_A)]}
    client = _mock_httpx_client(post_resp=resp)
    with (
        patch("app.services.reservation_service.httpx.AsyncClient", return_value=client),
        patch("app.services.reservation_service.settings") as mock_settings,
    ):
        mock_settings.internal_api_token = "tok"
        mock_settings.cabling_service_url = "http://cab"
        await _validate_topology_connectivity(uuid.uuid4(), [DEVICE_A])


@pytest.mark.asyncio
async def test_validate_topology_connectivity_missing_device_ids_field_is_no_violation():
    """A cabling response with no device_ids key at all (e.g. an older cached
    schema) is treated as an empty canvas device set, never a violation: the
    field is additive and its absence must not be read as membership failure."""
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {"valid": True, "invalid_edges": []}
    client = _mock_httpx_client(post_resp=resp)
    with (
        patch("app.services.reservation_service.httpx.AsyncClient", return_value=client),
        patch("app.services.reservation_service.settings") as mock_settings,
    ):
        mock_settings.internal_api_token = "tok"
        mock_settings.cabling_service_url = "http://cab"
        await _validate_topology_connectivity(uuid.uuid4(), [DEVICE_A])


# --- _acquire_device_locks postgresql branch ---


@pytest.mark.asyncio
async def test_acquire_device_locks_postgres_issues_advisory_locks():
    """On Postgres the helper issues one pg_advisory_xact_lock per device, in
    sorted order to avoid deadlocks."""
    db = MagicMock()
    db.bind = MagicMock()
    db.bind.dialect.name = "postgresql"
    db.execute = AsyncMock()
    await _acquire_device_locks(db, [DEVICE_A, DEVICE_B])
    assert db.execute.await_count == 2


# --- update_reservation gaps ---


@pytest.mark.asyncio
async def test_update_reservation_not_found_returns_none():
    async with TestSessionLocal() as db:
        result = await update_reservation(db, uuid.uuid4(), USER_ID, ReservationUpdate(purpose="x"))
        assert result is None


@pytest.mark.asyncio
async def test_update_reservation_end_time_not_after_start_rejected():
    async with TestSessionLocal() as db:
        res = await _insert_reservation(db, start_offset_h=2, end_offset_h=4)
        # New end before the reservation start.
        with pytest.raises(ValueError, match="after start_time"):
            await update_reservation(
                db,
                res.id,
                USER_ID,
                ReservationUpdate(end_time=NOW + timedelta(hours=1)),
            )


@pytest.mark.asyncio
async def test_update_reservation_end_time_in_past_rejected():
    """A new end_time after start but already in the past is rejected."""
    async with TestSessionLocal() as db:
        res = await _insert_reservation(db, start_offset_h=-5, end_offset_h=5)
        with pytest.raises(ValueError, match="in the future"):
            await update_reservation(
                db,
                res.id,
                USER_ID,
                ReservationUpdate(end_time=NOW - timedelta(hours=1)),
            )


@pytest.mark.asyncio
async def test_update_reservation_extend_fetch_failure_falls_back_to_exclusive():
    """If the device fetch during an extend fails, every device is treated as
    exclusive and the conflict check still runs: a second reservation holding
    DEVICE_A over hours 4 to 6 refuses the extension into that span (issue #1136).
    Skipping the check on a failed fetch would let the extension through."""
    async with TestSessionLocal() as db:
        res = await _insert_reservation(db, device_ids=[DEVICE_A], start_offset_h=1, end_offset_h=3)
        await _insert_reservation(db, device_ids=[DEVICE_A], start_offset_h=4, end_offset_h=6)
        old_end = res.end_time
        res_id = res.id
        with (
            patch(
                "app.services.reservation_service._fetch_devices",
                new=AsyncMock(side_effect=RuntimeError("inventory down")),
            ),
        ):
            with pytest.raises(LookupError, match="already reserved in the extended window"):
                await update_reservation(
                    db,
                    res_id,
                    USER_ID,
                    ReservationUpdate(end_time=NOW + timedelta(hours=5)),
                    token="t",
                )

    async with TestSessionLocal() as db:
        stored = await db.get(Reservation, res_id)
        assert stored.end_time == old_end


@pytest.mark.asyncio
async def test_update_reservation_extend_conflict_rejected():
    """Extending into a window already held by another exclusive reservation on
    the same device raises LookupError."""
    async with TestSessionLocal() as db:
        res = await _insert_reservation(db, device_ids=[DEVICE_A], start_offset_h=1, end_offset_h=3)
        # A conflicting reservation occupying hours 4-6 on the same device.
        await _insert_reservation(db, device_ids=[DEVICE_A], start_offset_h=4, end_offset_h=6)
        with (
            patch(
                "app.services.reservation_service._fetch_devices",
                new=AsyncMock(return_value=[_make_device(DEVICE_A)]),
            ),
        ):
            with pytest.raises(LookupError, match="extended window"):
                await update_reservation(
                    db,
                    res.id,
                    USER_ID,
                    ReservationUpdate(end_time=NOW + timedelta(hours=5)),
                    token="t",
                )


@pytest.mark.asyncio
async def test_update_reservation_purpose_only():
    async with TestSessionLocal() as db:
        res = await _insert_reservation(db, device_ids=[DEVICE_A])
        result = await update_reservation(
            db, res.id, USER_ID, ReservationUpdate(purpose="new purpose")
        )
        assert result.purpose == "new purpose"


@pytest.mark.asyncio
async def test_update_reservation_add_device_fetch_failure_raises_runtime():
    """A non-ValueError fetch failure while validating an added device surfaces
    as RuntimeError (mapped to 503 upstream)."""
    async with TestSessionLocal() as db:
        res = await _insert_reservation(db, device_ids=[DEVICE_A])
        with patch(
            "app.services.reservation_service._fetch_devices",
            new=AsyncMock(side_effect=RuntimeError("inventory down")),
        ):
            with pytest.raises(RuntimeError, match="Failed to contact inventory"):
                await update_reservation(
                    db,
                    res.id,
                    USER_ID,
                    ReservationUpdate(device_ids=[DEVICE_A, DEVICE_B]),
                    token="t",
                )


@pytest.mark.asyncio
async def test_update_reservation_add_device_fetch_value_error_propagates():
    async with TestSessionLocal() as db:
        res = await _insert_reservation(db, device_ids=[DEVICE_A])
        with patch(
            "app.services.reservation_service._fetch_devices",
            new=AsyncMock(side_effect=ValueError("Device not found")),
        ):
            with pytest.raises(ValueError, match="not found"):
                await update_reservation(
                    db,
                    res.id,
                    USER_ID,
                    ReservationUpdate(device_ids=[DEVICE_A, DEVICE_B]),
                    token="t",
                )


@pytest.mark.asyncio
async def test_update_reservation_add_device_mixed_topology_rejected():
    async with TestSessionLocal() as db:
        res = await _insert_reservation(db, device_ids=[DEVICE_A])
        devices = [
            _make_device(DEVICE_A, topology="PHYSICAL"),
            _make_device(DEVICE_B, topology="CLOUD"),
        ]
        with patch(
            "app.services.reservation_service._fetch_devices",
            new=AsyncMock(return_value=devices),
        ):
            with pytest.raises(ValueError, match="topology type"):
                await update_reservation(
                    db,
                    res.id,
                    USER_ID,
                    ReservationUpdate(device_ids=[DEVICE_A, DEVICE_B]),
                    token="t",
                )


@pytest.mark.asyncio
async def test_update_reservation_add_device_unavailable_rejected():
    async with TestSessionLocal() as db:
        res = await _insert_reservation(db, device_ids=[DEVICE_A])
        devices = [
            _make_device(DEVICE_A),
            _make_device(DEVICE_B, status="RESERVED"),
        ]
        with patch(
            "app.services.reservation_service._fetch_devices",
            new=AsyncMock(return_value=devices),
        ):
            with pytest.raises(ValueError, match="not available"):
                await update_reservation(
                    db,
                    res.id,
                    USER_ID,
                    ReservationUpdate(device_ids=[DEVICE_A, DEVICE_B]),
                    token="t",
                )


@pytest.mark.asyncio
async def test_update_reservation_add_device_conflict_rejected():
    """Adding an exclusive device that is already reserved in the overlapping
    window raises LookupError."""
    async with TestSessionLocal() as db:
        res = await _insert_reservation(db, device_ids=[DEVICE_A], start_offset_h=1, end_offset_h=3)
        # DEVICE_B is held by another reservation overlapping the same window.
        await _insert_reservation(db, device_ids=[DEVICE_B], start_offset_h=1, end_offset_h=3)
        devices = [_make_device(DEVICE_A), _make_device(DEVICE_B)]
        with patch(
            "app.services.reservation_service._fetch_devices",
            new=AsyncMock(return_value=devices),
        ):
            with pytest.raises(LookupError, match="already reserved"):
                await update_reservation(
                    db,
                    res.id,
                    USER_ID,
                    ReservationUpdate(device_ids=[DEVICE_A, DEVICE_B]),
                    token="t",
                )


@pytest.mark.asyncio
async def test_update_reservation_add_exclusive_device_marks_reserved():
    """Successfully adding an exclusive device flips it to RESERVED in inventory
    and persists the new membership."""
    async with TestSessionLocal() as db:
        res = await _insert_reservation(db, device_ids=[DEVICE_A], start_offset_h=1, end_offset_h=3)
        devices = [_make_device(DEVICE_A), _make_device(DEVICE_B)]
        update_mock = AsyncMock()
        with (
            patch(
                "app.services.reservation_service._fetch_devices",
                new=AsyncMock(return_value=devices),
            ),
            patch("app.services.reservation_service._update_device_statuses", new=update_mock),
        ):
            result = await update_reservation(
                db,
                res.id,
                USER_ID,
                ReservationUpdate(device_ids=[DEVICE_A, DEVICE_B]),
                token="t",
            )
        assert {str(d) for d in result.device_ids} == {str(DEVICE_A), str(DEVICE_B)}
        reserved_calls = [c for c in update_mock.call_args_list if c[0][1] == "RESERVED"]
        assert reserved_calls, "added exclusive device should be marked RESERVED"


@pytest.mark.asyncio
async def test_update_reservation_remove_exclusive_device_marks_available():
    """Removing an exclusive device flips it back to AVAILABLE."""
    async with TestSessionLocal() as db:
        res = await _insert_reservation(db, device_ids=[DEVICE_A, DEVICE_B])
        mock_fetch = AsyncMock(
            side_effect=[
                [_make_device(DEVICE_A)],  # new device set validation
                [_make_device(DEVICE_B)],  # removed device fetch (exclusive)
            ]
        )
        update_mock = AsyncMock()
        with (
            patch("app.services.reservation_service._fetch_devices", new=mock_fetch),
            patch("app.services.reservation_service._update_device_statuses", new=update_mock),
        ):
            result = await update_reservation(
                db, res.id, USER_ID, ReservationUpdate(device_ids=[DEVICE_A]), token="t"
            )
        assert [str(d) for d in result.device_ids] == [str(DEVICE_A)]
        available_calls = [c for c in update_mock.call_args_list if c[0][1] == "AVAILABLE"]
        assert available_calls, "removed exclusive device should be marked AVAILABLE"


@pytest.mark.asyncio
async def test_update_reservation_remove_device_fetch_failure_assumes_exclusive():
    """If the removed-device fetch fails, the device is assumed exclusive and
    released to AVAILABLE anyway."""
    async with TestSessionLocal() as db:
        res = await _insert_reservation(db, device_ids=[DEVICE_A, DEVICE_B])
        mock_fetch = AsyncMock(
            side_effect=[
                [_make_device(DEVICE_A)],  # new device set validation
                RuntimeError("inventory down"),  # removed device fetch fails
            ]
        )
        update_mock = AsyncMock()
        with (
            patch("app.services.reservation_service._fetch_devices", new=mock_fetch),
            patch("app.services.reservation_service._update_device_statuses", new=update_mock),
        ):
            result = await update_reservation(
                db, res.id, USER_ID, ReservationUpdate(device_ids=[DEVICE_A]), token="t"
            )
        assert [str(d) for d in result.device_ids] == [str(DEVICE_A)]
        available_calls = [c for c in update_mock.call_args_list if c[0][1] == "AVAILABLE"]
        assert available_calls, "fetch failure should still release the removed device"


@pytest.mark.asyncio
async def test_update_reservation_device_change_revalidates_topology():
    """A device-set change on a reservation that references a topology
    re-validates connectivity before any inventory mutation, with check_routes
    =False (R11 review fix, ADR 0014 phase 1, issue #34): a device-set PATCH
    judges only physical connectivity for the revised membership, never L3
    routing intent."""
    async with TestSessionLocal() as db:
        topo = uuid.uuid4()
        res = await _insert_reservation(db, device_ids=[DEVICE_A], topology_id=topo)
        validate_mock = AsyncMock()
        with (
            patch(
                "app.services.reservation_service._fetch_devices",
                new=AsyncMock(return_value=[_make_device(DEVICE_A), _make_device(DEVICE_B)]),
            ),
            patch(
                "app.services.reservation_service._validate_topology_connectivity",
                new=validate_mock,
            ),
            patch("app.services.reservation_service._update_device_statuses", new=AsyncMock()),
        ):
            await update_reservation(
                db,
                res.id,
                USER_ID,
                ReservationUpdate(device_ids=[DEVICE_A, DEVICE_B]),
                token="t",
            )
        validate_mock.assert_awaited_once_with(topo, [DEVICE_A, DEVICE_B], check_routes=False)


# --- cancel/release exclusive-device selection via fetched dict ---


def _fetch_aligned(device_ids, exclusive_id):
    """Build best-effort fetch results aligned to the reservation's device order,
    marking only `exclusive_id` exclusive. The service zips this list positionally
    against list(reservation.device_ids), so it must follow the same order."""
    return [_make_device(d, exclusive=(uuid.UUID(str(d)) == exclusive_id)) for d in device_ids]


@pytest.mark.asyncio
async def test_cancel_reservation_selects_exclusive_from_fetch_result():
    """When best-effort fetch returns a dict with exclusive=True, that device is
    released; a non-exclusive device is left alone."""
    async with TestSessionLocal() as db:
        res = await _insert_reservation(db, device_ids=[DEVICE_A, DEVICE_B])
        order = [uuid.UUID(str(d)) for d in res.device_ids]
        fetch_results = _fetch_aligned(order, DEVICE_A)
        update_mock = AsyncMock()
        with (
            patch(
                "app.services.reservation_service._fetch_devices_best_effort",
                new=AsyncMock(return_value=fetch_results),
            ),
            patch("app.services.reservation_service._update_device_statuses", new=update_mock),
        ):
            cancelled = await cancel_reservation(db, res.id, USER_ID, "token")
        assert cancelled.status == ReservationStatus.CANCELLED
        # Only DEVICE_A (exclusive) is released.
        assert update_mock.await_count == 1
        released_ids = update_mock.call_args[0][0]
        assert released_ids == [DEVICE_A]


@pytest.mark.asyncio
async def test_release_reservation_selects_exclusive_from_fetch_result():
    async with TestSessionLocal() as db:
        res = await _insert_reservation(db, device_ids=[DEVICE_A, DEVICE_B])
        order = [uuid.UUID(str(d)) for d in res.device_ids]
        fetch_results = _fetch_aligned(order, DEVICE_B)
        update_mock = AsyncMock()
        with (
            patch(
                "app.services.reservation_service._fetch_devices_best_effort",
                new=AsyncMock(return_value=fetch_results),
            ),
            patch("app.services.reservation_service._update_device_statuses", new=update_mock),
        ):
            released = await release_reservation(db, res.id, USER_ID, "token")
        assert released.status == ReservationStatus.COMPLETED
        assert update_mock.await_count == 1
        assert update_mock.call_args[0][0] == [DEVICE_B]


@pytest.mark.asyncio
async def test_cancel_reservation_all_non_exclusive_skips_inventory():
    """Every device non-exclusive: no inventory release call is made."""
    async with TestSessionLocal() as db:
        res = await _insert_reservation(db, device_ids=[DEVICE_A])
        update_mock = AsyncMock()
        with (
            patch(
                "app.services.reservation_service._fetch_devices_best_effort",
                new=AsyncMock(return_value=[_make_device(DEVICE_A, exclusive=False)]),
            ),
            patch("app.services.reservation_service._update_device_statuses", new=update_mock),
        ):
            cancelled = await cancel_reservation(db, res.id, USER_ID, "token")
        assert cancelled.status == ReservationStatus.CANCELLED
        update_mock.assert_not_called()


# --- _release_exclusive_devices_best_effort retry-exhaustion parity (issue #571 item 4) --
#
# The exclusivity filtering itself is exercised above (via cancel/release); this pins
# the retry-exhaustion logging branch, which the sibling cancel/release paths pin at
# test_reservation_service_unit.py:1194 and :1257 (await_count == 3 plus a structured
# log). _release_exclusive_devices_best_effort is the shared helper both the
# provision-result-failed path (reservation_service.py, log_action=
# "reservation_provision_failed_release") and the expiration sweep's timeout backstop
# (expiration.py, log_action="provision_timeout_release") call; it swallows after
# retries rather than raising, so the caller's already-committed FAILED transition is
# never revisited.


@pytest.mark.asyncio
async def test_release_exclusive_devices_best_effort_retries_and_swallows_on_exhaustion(
    caplog,
):
    """A persistently failing inventory release retries 3 times, swallows the
    final exception, and emits the structured failure log the caller passes in
    as log_action."""
    reservation_id = uuid.uuid4()
    update_mock = AsyncMock(side_effect=RuntimeError("inventory 503"))
    with (
        patch(
            "app.services.reservation_service._fetch_devices_best_effort",
            new=AsyncMock(return_value=[_make_device(DEVICE_A, exclusive=True)]),
        ),
        patch("app.services.reservation_service._update_device_statuses", new=update_mock),
        caplog.at_level("ERROR", logger="app.services.reservation_service"),
    ):
        await _release_exclusive_devices_best_effort(
            reservation_id, [DEVICE_A], "reservation_provision_failed_release"
        )

    assert update_mock.await_count == 3, "expected retry_with_backoff(attempts=3)"
    failed = [
        rec
        for rec in caplog.records
        if getattr(rec, "action", None) == "reservation_provision_failed_release"
    ]
    assert failed, "expected structured log action=reservation_provision_failed_release"
    assert str(reservation_id) in failed[0].getMessage() or getattr(
        failed[0], "reservation_id", None
    ) == str(reservation_id)


@pytest.mark.asyncio
async def test_release_exclusive_devices_best_effort_honors_caller_log_action(caplog):
    """The log_action string is caller-supplied, not hardcoded: the expiration
    sweep's timeout backstop passes "provision_timeout_release" through the same
    helper, and that action name (not the provision-result sibling's) is what
    lands in the structured log."""
    reservation_id = uuid.uuid4()
    update_mock = AsyncMock(side_effect=RuntimeError("inventory 503"))
    with (
        patch(
            "app.services.reservation_service._fetch_devices_best_effort",
            new=AsyncMock(return_value=[_make_device(DEVICE_A, exclusive=True)]),
        ),
        patch("app.services.reservation_service._update_device_statuses", new=update_mock),
        caplog.at_level("ERROR", logger="app.services.reservation_service"),
    ):
        await _release_exclusive_devices_best_effort(
            reservation_id, [DEVICE_A], "provision_timeout_release"
        )

    assert update_mock.await_count == 3
    actions = {getattr(rec, "action", None) for rec in caplog.records}
    assert "provision_timeout_release" in actions
    assert "reservation_provision_failed_release" not in actions


@pytest.mark.asyncio
async def test_release_exclusive_devices_best_effort_includes_user_id_when_supplied(caplog):
    """When a caller passes user_id, it lands in the retry-exhausted error
    log's extra (issue #599 part 1, gap 3: cancel/release both include the
    acting user, which the helper did not carry before)."""
    reservation_id = uuid.uuid4()
    user_id = uuid.uuid4()
    update_mock = AsyncMock(side_effect=RuntimeError("inventory 503"))
    with (
        patch(
            "app.services.reservation_service._fetch_devices_best_effort",
            new=AsyncMock(return_value=[_make_device(DEVICE_A, exclusive=True)]),
        ),
        patch("app.services.reservation_service._update_device_statuses", new=update_mock),
        caplog.at_level("ERROR", logger="app.services.reservation_service"),
    ):
        await _release_exclusive_devices_best_effort(
            reservation_id,
            [DEVICE_A],
            "reservation_cancel_release_failed",
            user_id=user_id,
        )

    failed = [
        rec
        for rec in caplog.records
        if getattr(rec, "action", None) == "reservation_cancel_release_failed"
    ]
    assert failed, "expected structured log action=reservation_cancel_release_failed"
    assert getattr(failed[0], "user_id", None) == str(user_id)


@pytest.mark.asyncio
async def test_release_exclusive_devices_best_effort_omits_user_id_by_default(caplog):
    """Without a user_id argument, the retry-exhausted error log's extra has no
    user_id key at all, byte-for-byte matching the helper's behavior before
    this parameter existed (the provision-result and expiration-timeout
    callers have no acting user in scope and must keep passing none)."""
    reservation_id = uuid.uuid4()
    update_mock = AsyncMock(side_effect=RuntimeError("inventory 503"))
    with (
        patch(
            "app.services.reservation_service._fetch_devices_best_effort",
            new=AsyncMock(return_value=[_make_device(DEVICE_A, exclusive=True)]),
        ),
        patch("app.services.reservation_service._update_device_statuses", new=update_mock),
        caplog.at_level("ERROR", logger="app.services.reservation_service"),
    ):
        await _release_exclusive_devices_best_effort(
            reservation_id, [DEVICE_A], "reservation_provision_failed_release"
        )

    failed = [
        rec
        for rec in caplog.records
        if getattr(rec, "action", None) == "reservation_provision_failed_release"
    ]
    assert failed, "expected structured log action=reservation_provision_failed_release"
    assert not hasattr(failed[0], "user_id")


# --- expiration_loop one-iteration exception handling ---


class _StopLoop(Exception):
    pass


# RES-SWEEP-10 (issue #1531): every tick runs every step, each once, and a step that
# raises skips none of the others. RES-SWEEP-10 states no order between the steps, so
# neither test below compares order: both compare multisets (each name, how many
# times). Two tests hold it:
#
# - a guard on the loop's source (`_per_tick_awaits`, its Limits in its docstring):
#   the awaited calls in the while body must be exactly the four steps plus
#   asyncio.sleep, so a fifth awaited call of any spelling it reads fails until it is
#   added to `_LOOP_AWAITS` and, if it is a step, to `_LOOP_STEPS`;
# - a runtime test over `_LOOP_STEPS`: one tick with no step, each step, and every step
#   raising, each step run exactly once.
#
# A behavior-preserving refactor of the loop (a `for` loop over a table of steps, the
# steps gathered, a helper per step) fails the guard on purpose: update `_LOOP_AWAITS`
# to the new spelling and make the runtime test patch whatever the loop now calls.
_LOOP_STEPS = {
    "_run_expiration_cycle": "Expiration cycle failed",
    "_run_reminder_cycle": "Expiry reminder cycle failed",
    "_run_fork_archive_reconcile": "Fork archive reconcile cycle failed",
    "_run_pending_prune_reconcile": "Pending fork-prune reconcile cycle failed",
}
_LOOP_AWAITS = [*_LOOP_STEPS, "asyncio.sleep"]


def _dotted(node: ast.AST) -> str:
    """A key for an expression, built from the AST (never `ast.unparse`): a name, an
    attribute chain `a.b.c`, a call as its callee plus `()`, else `<NodeType>`."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return f"{_dotted(node.value)}.{node.attr}"
    if isinstance(node, ast.Call):
        return f"{_dotted(node.func)}()"
    return f"<{type(node).__name__}>"


def _run_in_this_scope(node: ast.AST) -> list[ast.AST]:
    """The child nodes of `node` that run where `node` runs. A nested function, lambda,
    or class contributes only what is evaluated at its definition (decorators, default
    values, annotations, a class's bases and keywords), never its body."""
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
        args = node.args
        parts: list[ast.AST | None] = [*args.defaults, *args.kw_defaults]
        if not isinstance(node, ast.Lambda):
            parts += [*node.decorator_list, node.returns]
            every_arg = [*args.posonlyargs, *args.args, *args.kwonlyargs]
            every_arg += [arg for arg in (args.vararg, args.kwarg) if arg is not None]
            parts += [arg.annotation for arg in every_arg]
        return [part for part in parts if part is not None]
    if isinstance(node, ast.ClassDef):
        return [*node.decorator_list, *node.bases, *node.keywords]
    return list(ast.iter_child_nodes(node))


def _awaits_in(node: ast.AST) -> list[str]:
    keys: list[str] = []
    if isinstance(node, ast.Await):
        value = node.value
        if isinstance(value, ast.Call):
            keys.append(_dotted(value.func))
        else:
            keys.append(f"<awaited non-call {_dotted(value)}>")
    elif isinstance(node, ast.AsyncWith):
        keys += [f"async with {_dotted(item.context_expr)}" for item in node.items]
    elif isinstance(node, ast.AsyncFor) or (isinstance(node, ast.comprehension) and node.is_async):
        keys.append(f"async for {_dotted(node.iter)}")
    for child in _run_in_this_scope(node):
        keys.extend(_awaits_in(child))
    return keys


def _per_tick_awaits(source: str) -> list[str]:
    """Every await inside the one top-level `while` loop of the async function in
    `source`, keyed by what it awaits. An `await` of a call is keyed by its callee's
    dotted name (`_run_reminder_cycle`, `asyncio.sleep`, `asyncio.gather`,
    `mod._run_x`), any other `await` as `<awaited non-call ...>`. The implicit awaits
    are keyed too: each `async with` item as `async with <its expression>`, and each
    `async for` statement or comprehension as `async for <its iterable>`. Awaits in the
    loop's condition, its body at any depth (inside `try`, `if`, `with`, nested loops,
    comprehensions, another await's arguments), and its `else` are counted, and so are
    those in the parts of a nested `def`, `async def`, lambda, or class that run in the
    loop's scope: decorators, default values, annotations (Python 3.12 evaluates them
    where the def runs; Python 3.14 refuses an await in one), and a class's bases and
    keywords.

    The function's body must hold exactly one top-level `while` statement; zero or two
    fail with a message naming the count.

    Limits (each one is a fixture case in `_SCAN_LIMITS` below, which proves the scan
    really is blind to it):
    - a call that is not awaited (a coroutine created and dropped, or work started
      with `asyncio.create_task` and never awaited) is not seen;
    - an await outside the `while` (before the loop, so it runs once) is not seen;
    - the BODY of a nested `def`, `async def`, lambda, or class in the loop is not
      read, whether or not it runs; the call of a nested helper, if awaited, is;
    - a step passed un-awaited to an awaited wrapper (`asyncio.gather`,
      `asyncio.wait_for`, any awaited call) is seen only as the wrapper, so a step
      moved into the argument list of another awaited step passes;
    - names are keyed as written, not resolved: a different function bound to a step's
      name (an import alias, a rebinding outside the loop) passes;
    - reachability and error handling are not judged: an awaited step under `if False:`
      or outside any `try` is counted the same (the runtime test judges handling).
    """
    (function,) = ast.parse(textwrap.dedent(source)).body
    loops = [stmt for stmt in function.body if isinstance(stmt, ast.While)]
    assert len(loops) == 1, (
        f"{function.name} must hold exactly one top-level while loop, found {len(loops)}"
    )
    return _awaits_in(loops[0])


def _expiration_loop_source() -> str:
    from app.tasks import expiration

    return inspect.getsource(expiration.expiration_loop)


def test_expiration_loop_awaits_exactly_the_four_named_steps_and_the_sleep():
    assert sorted(_per_tick_awaits(_expiration_loop_source())) == sorted(_LOOP_AWAITS)


# Fixture sources for the guard. `_SCAN_BASE` has the loop's shape; each case is an
# (old, new) edit of it with the keys the scan must then return.
_SCAN_BASE = textwrap.dedent(
    """\
    async def expiration_loop(interval_seconds=60):
        while True:
            try:
                await _run_expiration_cycle()
            except Exception:
                pass
            try:
                await _run_reminder_cycle()
            except Exception:
                pass
            try:
                await _run_fork_archive_reconcile()
            except Exception:
                pass
            try:
                await _run_pending_prune_reconcile()
            except Exception:
                pass
            await asyncio.sleep(interval_seconds)
    """
)
_SLEEP = "        await asyncio.sleep(interval_seconds)\n"
_REMINDER = "            await _run_reminder_cycle()\n"


def _before_sleep(line: str) -> tuple[str, str]:
    return _SLEEP, f"        {line}\n{_SLEEP}"


def _with(*added: str, without: str | None = None) -> list[str]:
    keys = [*_LOOP_AWAITS, *added]
    if without is not None:
        keys.remove(without)
    return keys


# Spellings the scan sees: each changes the keys, so the guard fails.
_SCAN_SEES = {
    "fifth_step_bare_name": (_before_sleep("await _run_new_step()"), _with("_run_new_step")),
    "fifth_step_to_thread": (
        _before_sleep("await asyncio.to_thread(int)"),
        _with("asyncio.to_thread"),
    ),
    "fifth_step_module_attribute": (
        _before_sleep("await expiration._run_reminder_cycle()"),
        _with("expiration._run_reminder_cycle"),
    ),
    "step_gathered": (
        (_REMINDER, "            await asyncio.gather(_run_reminder_cycle())\n"),
        _with("asyncio.gather", without="_run_reminder_cycle"),
    ),
    "step_awaited_twice": (
        _before_sleep("await _run_reminder_cycle()"),
        _with("_run_reminder_cycle"),
    ),
    "step_removed": ((_REMINDER, "            pass\n"), _with(without="_run_reminder_cycle")),
    "step_through_a_nested_helper": (
        (_REMINDER, "            await helper()\n"),
        _with("helper", without="_run_reminder_cycle"),
    ),
    "sleep_renamed_import": (
        (_SLEEP, "        await sleep(interval_seconds)\n"),
        _with("sleep", without="asyncio.sleep"),
    ),
    "sleep_removed": ((_SLEEP, ""), _with(without="asyncio.sleep")),
    "awaited_non_call": (_before_sleep("await pending"), _with("<awaited non-call pending>")),
    "await_in_a_nested_while": (
        _before_sleep("while False:\n            await _run_new_step()"),
        _with("_run_new_step"),
    ),
    "await_in_a_comprehension": (
        _before_sleep("[await _run_new_step() for _ in ()]"),
        _with("_run_new_step"),
    ),
    "async_with": (
        _before_sleep("async with _tick_guard():\n            pass"),
        _with("async with _tick_guard()"),
    ),
    "async_with_two_items": (
        _before_sleep("async with lock, _tick_guard() as guard:\n            pass"),
        _with("async with lock", "async with _tick_guard()"),
    ),
    "async_for": (
        _before_sleep("async for _ in _run_new_gen():\n            pass"),
        _with("async for _run_new_gen()"),
    ),
    "async_comprehension": (
        _before_sleep("[x async for x in _run_new_gen()]"),
        _with("async for _run_new_gen()"),
    ),
    "await_in_a_nested_def_decorator": (
        _before_sleep("@deco(await _run_new_step())\n        def later():\n            pass"),
        _with("_run_new_step"),
    ),
    "await_in_a_nested_def_default": (
        _before_sleep("async def later(x=await _run_new_step()):\n            pass"),
        _with("_run_new_step"),
    ),
    "await_in_a_nested_def_keyword_only_default": (
        _before_sleep("def later(*, x=await _run_new_step()):\n            pass"),
        _with("_run_new_step"),
    ),
    "await_in_a_nested_def_annotation": (
        _before_sleep("def later(x: await _run_new_step()):\n            pass"),
        _with("_run_new_step"),
    ),
    "await_in_a_nested_def_return_annotation": (
        _before_sleep("def later() -> (await _run_new_step()):\n            pass"),
        _with("_run_new_step"),
    ),
    "await_in_a_lambda_default": (
        _before_sleep("later = lambda x=await _run_new_step(): x"),
        _with("_run_new_step"),
    ),
    "await_in_a_class_base": (
        _before_sleep("class Later(await _run_new_step()):\n            pass"),
        _with("_run_new_step"),
    ),
    "await_in_a_class_keyword": (
        _before_sleep("class Later(metaclass=await _run_new_step()):\n            pass"),
        _with("_run_new_step"),
    ),
    "await_in_a_class_decorator": (
        _before_sleep("@deco(await _run_new_step())\n        class Later:\n            pass"),
        _with("_run_new_step"),
    ),
    "steps_from_a_table": (
        (
            _REMINDER,
            "            for run in (_run_reminder_cycle,):\n                await run()\n",
        ),
        _with("run", without="_run_reminder_cycle"),
    ),
}

# Changes the scan is blind to (its Limits): the keys stay those of `_SCAN_BASE`.
_SCAN_LIMITS = {
    "call_not_awaited": _before_sleep("_run_new_step()"),
    "create_task_not_awaited": _before_sleep("asyncio.create_task(_run_new_step())"),
    "await_before_the_loop": (
        "    while True:\n",
        "    await _run_new_step()\n    while True:\n",
    ),
    "await_in_a_nested_def": _before_sleep("async def later():\n            await _run_new_step()"),
    "await_in_a_nested_sync_def": _before_sleep("def later():\n            await _run_new_step()"),
    "await_in_a_lambda_body": _before_sleep("later = lambda: (await _run_new_step())"),
    "await_in_a_class_body": _before_sleep("class Later:\n            x = await _run_new_step()"),
    "step_inside_an_awaited_wrapper": (
        _REMINDER,
        "            await _run_reminder_cycle(_run_new_step())\n",
    ),
    "name_rebound_outside_the_loop": (
        "    while True:\n",
        "    _run_reminder_cycle = _run_new_step\n    while True:\n",
    ),
    "step_unreachable": (_REMINDER, "            if False:\n" + "    " + _REMINDER),
    "step_outside_any_try": (
        "        try:\n            await _run_pending_prune_reconcile()\n"
        "        except Exception:\n            pass\n",
        "        await _run_pending_prune_reconcile()\n",
    ),
}


def _edited(old: str, new: str) -> str:
    assert _SCAN_BASE.count(old) == 1, old
    return _SCAN_BASE.replace(old, new)


def test_the_step_scan_base_fixture_reads_as_the_loop_does():
    assert sorted(_per_tick_awaits(_SCAN_BASE)) == sorted(_LOOP_AWAITS)


@pytest.mark.parametrize("case", sorted(_SCAN_SEES))
def test_the_step_scan_sees_each_listed_spelling(case):
    (old, new), keys = _SCAN_SEES[case]
    found = sorted(_per_tick_awaits(_edited(old, new)))
    assert found == sorted(keys)
    assert found != sorted(_LOOP_AWAITS)


@pytest.mark.parametrize("case", sorted(_SCAN_LIMITS))
def test_the_step_scan_is_blind_to_each_listed_limit(case):
    old, new = _SCAN_LIMITS[case]
    assert sorted(_per_tick_awaits(_edited(old, new))) == sorted(_LOOP_AWAITS)


_NO_LOOP = "async def expiration_loop(interval_seconds=60):\n    pass\n"
_TWO_LOOPS = _SCAN_BASE + _SCAN_BASE.split("\n", 1)[1]


@pytest.mark.parametrize(("source", "count"), [(_NO_LOOP, 0), (_TWO_LOOPS, 2)])
def test_the_step_scan_refuses_a_function_without_exactly_one_top_level_loop(source, count):
    with pytest.raises(AssertionError, match=f"exactly one top-level while loop, found {count}"):
        _per_tick_awaits(source)


class _StepFailed(Exception):
    """Raised by a fake step; private, so narrowing a step's handler to any other
    exception type lets it escape the loop and fails the test."""


class _StepRanTwice(BaseException):
    """Raised by a fake step called a second time. Not an Exception, so the loop's
    handlers do not catch it: a loop that lost its sleep fails at once, not by hanging."""


_FAILING_SETS = {
    "none_raise": (),
    **{f"{name}_raises": (name,) for name in _LOOP_STEPS},
    "all_raise": tuple(_LOOP_STEPS),
}


@pytest.mark.asyncio
@pytest.mark.timeout(60)
@pytest.mark.parametrize("case", list(_FAILING_SETS))
async def test_expiration_loop_runs_every_step_and_a_failing_step_skips_none(case, caplog):
    """One tick awaits each step exactly once (in any order; RES-SWEEP-10 states none)
    whichever steps raise, logs one error line per raising step carrying the exact
    exception that step raised, and reaches the sleep. The otherwise-infinite loop
    ends because the patched sleep raises; no real clock. A step called a second time
    raises `_StepRanTwice`, so a loop without its sleep fails at once; the timeout
    marker is the backstop for any other hang."""
    from app.tasks import expiration

    failing = _FAILING_SETS[case]
    raised = {name: _StepFailed(name) for name in failing}
    ran: list[str] = []

    def step(name):
        async def run():
            if name in ran:
                raise _StepRanTwice(
                    f"{name} ran twice in one tick: the sleep is missing or the step is "
                    "awaited twice; if intended, update _LOOP_AWAITS and _LOOP_STEPS"
                )
            ran.append(name)
            if name in raised:
                raise raised[name]

        return run

    sleep = AsyncMock(side_effect=_StopLoop())
    with ExitStack() as stack:
        for name in _LOOP_STEPS:
            stack.enter_context(patch.object(expiration, name, new=step(name)))
        stack.enter_context(patch.object(expiration.asyncio, "sleep", new=sleep))
        stack.enter_context(caplog.at_level("ERROR", logger="app.tasks.expiration"))
        with pytest.raises(_StopLoop):
            await expiration.expiration_loop(interval_seconds=7)

    assert sorted(ran) == sorted(_LOOP_STEPS)
    sleep.assert_awaited_once_with(7)
    errors = sorted(
        (
            (r.getMessage(), r.exc_info[1])
            for r in caplog.records
            if r.name == "app.tasks.expiration" and r.levelname == "ERROR"
        ),
        key=lambda error: error[0],
    )
    # Each line names the step's failure by its class (RES-UPSTREAM-3's label).
    assert errors == sorted(
        ((f"{_LOOP_STEPS[name]} (_StepFailed)", raised[name]) for name in failing),
        key=lambda error: error[0],
    )


@pytest.mark.asyncio
@pytest.mark.timeout(60)
async def test_expiration_loop_no_longer_drives_purpose_classify_reconcile():
    """Issue #702: the purpose-classify reconciler moved to its own task
    (purpose_classify_loop); expiration_loop must never call it, even though
    it still drives every other standing reconciler (fork archive, pending
    prune)."""
    from app.tasks import expiration

    purpose_classify = AsyncMock()
    with (
        patch.object(expiration, "_run_expiration_cycle", new=AsyncMock()),
        patch.object(expiration, "_run_reminder_cycle", new=AsyncMock()),
        patch.object(expiration, "_run_fork_archive_reconcile", new=AsyncMock()),
        patch.object(expiration, "_run_pending_prune_reconcile", new=AsyncMock()),
        patch.object(expiration, "_run_purpose_classify_reconcile", new=purpose_classify),
        patch.object(expiration.asyncio, "sleep", new=AsyncMock(side_effect=_StopLoop())),
    ):
        with pytest.raises(_StopLoop):
            await expiration.expiration_loop(interval_seconds=5)

    purpose_classify.assert_not_awaited()


@pytest.mark.asyncio
async def test_purpose_classify_loop_runs_reconcile_and_handles_errors(caplog):
    """purpose_classify_loop (issue #702) drives the reconciler on its own
    loop and logs, rather than raises, when a tick fails."""
    from app.tasks import expiration

    reconcile = AsyncMock(side_effect=RuntimeError("reconcile boom"))
    with (
        patch.object(expiration, "_run_purpose_classify_reconcile", new=reconcile),
        patch.object(expiration.asyncio, "sleep", new=AsyncMock(side_effect=_StopLoop())),
        caplog.at_level("ERROR", logger="app.tasks.expiration"),
    ):
        with pytest.raises(_StopLoop):
            await expiration.purpose_classify_loop(interval_seconds=1)

    reconcile.assert_awaited_once()
    messages = [r.getMessage() for r in caplog.records]
    assert any("Purpose classify reconcile cycle failed" in m for m in messages)


@pytest.mark.asyncio
async def test_purpose_classify_loop_happy_path_single_iteration():
    from app.tasks import expiration

    reconcile = AsyncMock()
    with (
        patch.object(expiration, "_run_purpose_classify_reconcile", new=reconcile),
        patch.object(expiration.asyncio, "sleep", new=AsyncMock(side_effect=_StopLoop())),
    ):
        with pytest.raises(_StopLoop):
            await expiration.purpose_classify_loop(interval_seconds=5)

    reconcile.assert_awaited_once()


# --- schema validators ---


def test_as_utc_naive_treated_as_utc():
    naive = datetime(2026, 1, 1, 0, 0)
    out = _naive_as_utc(naive)
    assert out.tzinfo == timezone.utc


def test_as_utc_aware_left_unchanged():
    aware = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)
    assert _naive_as_utc(aware) is aware


def test_reservation_update_device_ids_none_passes():
    """Explicit device_ids=None is a valid no-op update; the validator's
    None-guard branch returns None rather than rejecting an empty list."""
    upd = ReservationUpdate(purpose="x", device_ids=None)
    assert upd.device_ids is None


def test_reservation_update_device_ids_dedupes():
    upd = ReservationUpdate(device_ids=[DEVICE_A, DEVICE_A, DEVICE_B])
    assert upd.device_ids == [DEVICE_A, DEVICE_B]


def test_reservation_response_coerces_string_device_ids():
    """ReservationResponse.coerce_device_ids accepts string ids and returns UUIDs."""
    from app.schemas.reservation import ReservationResponse

    now = datetime.now(timezone.utc)
    resp = ReservationResponse(
        id=uuid.uuid4(),
        user_id=USER_ID,
        device_ids=[str(DEVICE_A), str(DEVICE_B)],
        topology_id=None,
        topology_type=TopologyType.PHYSICAL,
        purpose=None,
        start_time=now,
        end_time=now + timedelta(hours=1),
        status=ReservationStatus.ACTIVE,
        created_at=now,
        updated_at=now,
    )
    assert resp.device_ids == [DEVICE_A, DEVICE_B]
    assert all(isinstance(d, uuid.UUID) for d in resp.device_ids)


# --- guard: device membership rows actually persisted on add (sanity) ---


@pytest.mark.asyncio
async def test_update_reservation_add_persists_membership_rows(caplog):
    async with TestSessionLocal() as db:
        res = await _insert_reservation(db, device_ids=[DEVICE_A], start_offset_h=1, end_offset_h=3)
        devices = [_make_device(DEVICE_A), _make_device(DEVICE_B)]
        with (
            patch(
                "app.services.reservation_service._fetch_devices",
                new=AsyncMock(return_value=devices),
            ),
            patch("app.services.reservation_service._update_device_statuses", new=AsyncMock()),
        ):
            await update_reservation(
                db,
                res.id,
                USER_ID,
                ReservationUpdate(device_ids=[DEVICE_A, DEVICE_B]),
                token="t",
            )
        rows = (await db.execute(select(Reservation).where(Reservation.id == res.id))).scalar_one()
        assert {str(d) for d in rows.device_ids} == {str(DEVICE_A), str(DEVICE_B)}


# --- reporting_service residual gaps ---


@pytest.mark.asyncio
async def test_build_report_zero_duration_inside_window_is_skipped():
    """A zero-duration reservation strictly inside the window clips to zero hours
    and is skipped (the hours<=0 continue), so it contributes no buckets."""
    from app.services.reporting_service import build_utilization_report

    async with TestSessionLocal() as db:
        instant = NOW + timedelta(hours=1)
        res = Reservation(
            user_id=USER_ID,
            device_ids=[str(DEVICE_A)],
            topology_type=TopologyType.PHYSICAL,
            purpose="zero",
            start_time=instant,
            end_time=instant,  # zero-width window, strictly inside the query window
            status=ReservationStatus.COMPLETED,
        )
        db.add(res)
        await db.commit()

        report = await build_utilization_report(
            db, NOW, NOW + timedelta(hours=5), [ReservationStatus.COMPLETED]
        )
    assert report.total_reservations == 0
    assert report.by_user == []
    assert report.total_hours == 0.0


@pytest.mark.asyncio
async def test_build_report_skips_malformed_device_id_defensively():
    """The aggregation guards against a non-UUID device id (defense in depth even
    though the typed join table normally rejects it at write time). We feed a
    fake reservation carrying a bad id through a stubbed query result and assert
    user/topology hours still accrue while the bad device id is skipped."""
    from app.services import reporting_service

    fake = MagicMock()
    fake.user_id = USER_ID
    fake.owner_name = "alice"
    fake.topology_type = TopologyType.PHYSICAL
    fake.start_time = NOW + timedelta(hours=1)
    fake.end_time = NOW + timedelta(hours=2)
    fake.device_ids = ["not-a-uuid", str(DEVICE_A)]
    fake.purpose_category = None

    scalars = MagicMock()
    scalars.all.return_value = [fake]
    exec_result = MagicMock()
    exec_result.scalars.return_value = scalars
    db = MagicMock()
    db.execute = AsyncMock(return_value=exec_result)

    report = await reporting_service.build_utilization_report(
        db, NOW, NOW + timedelta(hours=5), None
    )
    # The reservation still counts; only the malformed device id is dropped.
    assert report.total_reservations == 1
    assert len(report.by_device) == 1
    assert str(report.by_device[0].device_id) == str(DEVICE_A)


@pytest.mark.asyncio
async def test_fetch_user_groups_map_outer_exception_is_swallowed(caplog):
    """If the AsyncClient context manager itself blows up (not a per-user
    HTTPError), the outer guard logs and returns empty groups for every user
    rather than failing the whole report."""
    from app.services.reporting_service import fetch_user_groups_map

    with (
        patch("app.services.reporting_service.httpx.AsyncClient", side_effect=RuntimeError("boom")),
        caplog.at_level("WARNING", logger="app.services.reporting_service"),
    ):
        result = await fetch_user_groups_map([USER_ID, DEVICE_B], "Bearer tok")
    assert result == {USER_ID: [], DEVICE_B: []}
    assert any("unexpected error" in r.getMessage() for r in caplog.records)


# --- router residual gaps (csv ValueError handler, visible-devices non-200) ---


@pytest.fixture
async def admin_router_client():
    """Admin-authenticated ASGI client bound to this module's in-memory DB."""
    from app.database import get_db
    from app.dependencies.auth import get_current_user_payload, require_admin
    from app.main import app
    from app.routers.reservations import bearer_scheme
    from httpx import ASGITransport, AsyncClient

    from tests._harness import override_bearer, override_get_db

    admin_payload = {"sub": str(USER_ID), "username": "adminuser", "role": "admin"}
    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_current_user_payload] = lambda: admin_payload
    app.dependency_overrides[require_admin] = lambda: admin_payload
    app.dependency_overrides[bearer_scheme] = override_bearer
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac
    app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_csv_report_inverted_window_returns_422(admin_router_client):
    """build_utilization_report raises ValueError on an inverted window; the csv
    route maps it to 422 (covers the csv handler's except branch)."""
    start = (NOW).isoformat()
    end = (NOW - timedelta(hours=1)).isoformat()  # end before start
    resp = await admin_router_client.get(
        "/reports/utilization.csv",
        params={"start": start, "end": end, "section": "user"},
    )
    assert resp.status_code == 422


@pytest.fixture
async def non_admin_router_client():
    from app.database import get_db
    from app.dependencies.auth import get_current_user_payload
    from app.main import app
    from app.routers.reservations import bearer_scheme
    from httpx import ASGITransport, AsyncClient

    from tests._harness import override_bearer, override_get_db

    payload = {"sub": str(USER_ID), "username": "regular", "role": "user"}
    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_current_user_payload] = lambda: payload
    app.dependency_overrides[bearer_scheme] = override_bearer
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac
    app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_visible_devices_non_200_logs_and_fails_open(non_admin_router_client, caplog):
    """A non-200 (but non-exception) response from inventory's visible-devices
    endpoint logs a warning and returns None, so the visibility filter is
    skipped (fail-open) and the create proceeds to device validation."""
    inv_resp = MagicMock()
    inv_resp.status_code = 500
    inv_client = _mock_httpx_client(get_resp=inv_resp)

    with (
        # _fetch_visible_device_ids runs its real body against this stubbed httpx.
        patch("app.routers.reservations.httpx.AsyncClient", return_value=inv_client),
        patch(
            "app.services.reservation_service._fetch_devices",
            new=AsyncMock(return_value=[_make_device(DEVICE_A)]),
        ),
        patch("app.services.reservation_service._update_device_statuses", new=AsyncMock()),
        caplog.at_level("WARNING", logger="app.routers.reservations"),
    ):
        resp = await non_admin_router_client.post(
            "/",
            json={
                "device_ids": [str(DEVICE_A)],
                "purpose": "test",
                "start_time": (NOW + timedelta(hours=1)).isoformat(),
                "end_time": (NOW + timedelta(hours=3)).isoformat(),
            },
        )
    # Fail-open: visibility None means no 403; the create succeeds.
    assert resp.status_code == 201
    assert any("visible-devices returned 500" in r.getMessage() for r in caplog.records)
