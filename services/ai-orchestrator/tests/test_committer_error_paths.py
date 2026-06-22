"""Error-path coverage for the AI commit flow (app/services/committer.py).

test_commit.py covers the happy path and the CommitError rollbacks through the
ASGI route. This module pins the remaining error branches by calling the
committer helpers directly with hand-built httpx stubs: the _detail body
parser, the canvas edge-skip on a dangling role, the rollback-delete that
swallows its own failure, the per-device apply branches (request exception and
malformed JSON), and the unexpected-non-CommitError rollback in
commit_proposal.
"""

from datetime import datetime, timedelta, timezone

import httpx
import pytest
import respx
from app import config as config_module
from app.schemas.generate import CommitDevice, CommitEdge, CommitRequest
from app.services import committer
from app.services.committer import (
    CommitError,
    _apply_configs,
    _build_canvas_data,
    _delete_topology,
    _detail,
)

CABLING_URL = config_module.settings.cabling_service_url.rstrip("/")
RESERVATIONS_URL = config_module.settings.reservations_service_url.rstrip("/")
EXECUTION_URL = config_module.settings.execution_service_url.rstrip("/")

TOPOLOGY_ID = "11111111-1111-1111-1111-111111111111"
RESERVATION_ID = "22222222-2222-2222-2222-222222222222"
DEVICE_A = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"


def _req(**overrides) -> CommitRequest:
    start = datetime.now(timezone.utc).replace(microsecond=0) + timedelta(hours=1)
    end = start + timedelta(hours=4)
    base = dict(
        topology_name="t",
        purpose="p",
        start_time=start,
        end_time=end,
        devices=[CommitDevice(role="fw-a", device_id=DEVICE_A)],
        edges=[],
    )
    base.update(overrides)
    return CommitRequest(**base)


# --- _detail: response-body parsing (lines 45-51) ---


def test_detail_non_json_body_falls_back_to_text():
    resp = httpx.Response(500, text="raw server error")
    assert _detail(resp) == "raw server error"


def test_detail_empty_non_json_body_falls_back_to_status():
    resp = httpx.Response(503, content=b"")
    # No text and not JSON: the f-string fallback wins.
    assert _detail(resp) == "HTTP 503"


def test_detail_dict_body_uses_detail_key():
    resp = httpx.Response(409, json={"detail": "conflict here"})
    assert _detail(resp) == "conflict here"


def test_detail_non_dict_json_body_stringifies():
    resp = httpx.Response(400, json=["a", "b"])
    assert _detail(resp) == "['a', 'b']"


# --- _build_canvas_data: dangling edge role is skipped (line 88) ---


def test_build_canvas_skips_edge_with_unknown_role():
    req = _req(
        devices=[CommitDevice(role="fw-a", device_id=DEVICE_A)],
        edges=[CommitEdge(source_role="fw-a", target_role="does-not-exist", layer="L2")],
    )
    canvas = _build_canvas_data(req)
    # One node, but the edge references a role with no node, so it is dropped.
    assert len(canvas["nodes"]) == 1
    assert canvas["edges"] == []


# --- _delete_topology: rollback delete swallows its own failure (127-128) ---


async def test_delete_topology_swallows_exception(caplog):
    import logging

    async with httpx.AsyncClient() as client:
        with respx.mock as mock:
            mock.delete(f"{CABLING_URL}/topologies/{TOPOLOGY_ID}").mock(
                side_effect=httpx.ConnectError("cabling down")
            )
            with caplog.at_level(logging.ERROR):
                # Must NOT raise: a failed rollback only logs.
                await _delete_topology(client, {"Authorization": "Bearer t"}, TOPOLOGY_ID)
    assert any(r.message == "rollback_topology_delete_failed" for r in caplog.records)


# --- _apply_configs: per-device failure branches (182-191, 204-205) ---


async def test_apply_configs_records_request_exception_as_failed():
    req = _req(
        devices=[
            CommitDevice(
                role="fw-a", device_id=DEVICE_A, config={"vlan": 10}, connection_type="Management"
            )
        ],
        apply_configs=True,
    )
    async with httpx.AsyncClient() as client:
        with respx.mock as mock:
            mock.post(f"{EXECUTION_URL}/execute").mock(
                side_effect=httpx.ConnectError("execution unreachable")
            )
            results = await _apply_configs(
                client, {"Authorization": "Bearer t"}, req, "user-1", RESERVATION_ID
            )
    assert len(results) == 1
    assert results[0].status == "failed"
    assert results[0].error.startswith("request failed: ")
    assert "execution unreachable" in results[0].error


async def test_apply_configs_handles_non_json_success_body():
    """A 200 with a non-JSON body defaults run_status to SUCCESS (payload={})."""
    req = _req(
        devices=[
            CommitDevice(
                role="fw-a", device_id=DEVICE_A, config={"vlan": 10}, connection_type="Management"
            )
        ],
        apply_configs=True,
    )
    async with httpx.AsyncClient() as client:
        with respx.mock as mock:
            mock.post(f"{EXECUTION_URL}/execute").respond(200, text="not json")
            results = await _apply_configs(
                client, {"Authorization": "Bearer t"}, req, "user-1", RESERVATION_ID
            )
    assert len(results) == 1
    # payload={} so status defaults to SUCCESS, run_id None, error None.
    assert results[0].status == "success"
    assert results[0].run_id is None
    assert results[0].error is None


async def test_apply_configs_non_success_status_marks_failed():
    """A 200 whose JSON status is not SUCCESS is recorded as failed."""
    req = _req(
        devices=[
            CommitDevice(
                role="fw-a", device_id=DEVICE_A, config={"vlan": 10}, connection_type="Management"
            )
        ],
        apply_configs=True,
    )
    async with httpx.AsyncClient() as client:
        with respx.mock as mock:
            mock.post(f"{EXECUTION_URL}/execute").respond(
                200, json={"id": "run-1", "status": "FAILURE", "error": "driver refused"}
            )
            results = await _apply_configs(
                client, {"Authorization": "Bearer t"}, req, "user-1", RESERVATION_ID
            )
    assert results[0].status == "failed"
    assert results[0].error == "driver refused"
    assert results[0].run_id == "run-1"


# --- commit_proposal: unexpected non-CommitError triggers rollback (244-246) ---


async def test_commit_unexpected_error_rolls_back_and_wraps_502(monkeypatch):
    """If a step raises something other than CommitError (e.g. a KeyError on a
    malformed upstream body), commit_proposal deletes the topology and re-raises
    as a 502 CommitError, never leaking the raw exception as a 500.
    """
    with respx.mock(assert_all_called=True) as mock:
        mock.post(f"{CABLING_URL}/topologies").respond(201, json={"id": TOPOLOGY_ID})
        mock.put(f"{CABLING_URL}/topologies/{TOPOLOGY_ID}").respond(200, json={})
        # Reservation responds 201 but with NO "id" key: _create_reservation does
        # resp.json()["id"], raising KeyError, which is the non-CommitError branch.
        mock.post(f"{RESERVATIONS_URL}/").respond(201, json={})
        rollback = mock.delete(f"{CABLING_URL}/topologies/{TOPOLOGY_ID}").respond(204)

        with pytest.raises(CommitError) as exc:
            await committer.commit_proposal(_req(), "user-bearer", "user-1")

    assert exc.value.status_code == 502
    assert "Unexpected upstream failure" in exc.value.message
    assert rollback.called
