"""Unit tests for app/services/purpose_signals.py (issue #646 phase 2).

Covers prompt assembly for both passes, transcript inclusion gated by
ai_purpose_include_transcripts, that a signal-fetch failure is tolerated
(logged, omitted from signals_used) rather than raised, and (issue #709)
that per-item fetches are deduped and fanned out under a concurrency bound
without changing rendering order.
"""

import asyncio
import uuid
from datetime import datetime, timezone

import httpx
import pytest
from app import config as config_module
from app.database import Base, engine
from app.models.conversation import AssistantConversation, AssistantMessage, MessageRole
from app.schemas.purpose import DynamicRequestItem
from app.services import purpose_signals as sig_module
from sqlalchemy.ext.asyncio import async_sessionmaker

TestSessionLocal = async_sessionmaker(engine, expire_on_commit=False)

TOPOLOGY_ID = uuid.uuid4()
DEVICE_A = uuid.uuid4()
DEVICE_B = uuid.uuid4()
TEMPLATE_ID = uuid.uuid4()
RESERVATION_ID = uuid.uuid4()


@pytest.fixture(autouse=True)
async def setup_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


def _patch_httpx(monkeypatch, handler):
    transport = httpx.MockTransport(handler)
    real_client = httpx.AsyncClient

    class PatchedAsync(real_client):
        def __init__(self, *args, **kwargs):
            kwargs["transport"] = transport
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(sig_module.httpx, "AsyncClient", PatchedAsync)


def _device_payload(device_id, name, template="switch", vendor="Arista", model="7050"):
    return {
        "id": str(device_id),
        "name": name,
        "template_id": "tpl-1",
        "template_name": template,
        "template_vendor": vendor,
        "template_model": model,
        "field_data": {"password": "DO-NOT-LEAK"},
    }


def _canvas(device_ids, layers):
    nodes = [
        {"id": f"n{i}", "data": {"device": {"id": str(did)}}} for i, did in enumerate(device_ids)
    ]
    edges = [{"id": f"e{i}", "data": {"layer": layer}} for i, layer in enumerate(layers)]
    return {"nodes": nodes, "edges": edges}


# --- preview pass ---


@pytest.mark.asyncio
async def test_preview_signals_include_purpose_topology_and_dynamic_templates(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith(f"/topologies/{TOPOLOGY_ID}"):
            return httpx.Response(
                200, json={"canvas_data": _canvas([DEVICE_A, DEVICE_B], ["L1", "L2"])}
            )
        if path.endswith("/devices/batch"):
            return httpx.Response(
                200,
                json={
                    "items": [
                        _device_payload(DEVICE_A, "sw-a"),
                        _device_payload(DEVICE_B, "sw-b"),
                    ]
                },
            )
        if path.endswith(f"/templates/{TEMPLATE_ID}"):
            return httpx.Response(200, json={"name": "proxmox-clone"})
        return httpx.Response(404, json={"detail": "unmocked"})

    _patch_httpx(monkeypatch, handler)

    block, used = await sig_module.gather_preview_signals(
        token="test-token",
        purpose="regression pass for release 4.2",
        topology_id=TOPOLOGY_ID,
        device_ids=None,
        dynamic_requests=[DynamicRequestItem(template_id=TEMPLATE_ID, count=3)],
    )

    assert set(used) == {
        sig_module.SIGNAL_PURPOSE_TEXT,
        sig_module.SIGNAL_TOPOLOGY,
        sig_module.SIGNAL_DYNAMIC_TEMPLATES,
    }
    assert "regression pass for release 4.2" in block
    assert "sw-a" in block and "sw-b" in block
    assert "L1: 1" in block and "L2: 1" in block
    assert "proxmox-clone x3" in block
    assert "DO-NOT-LEAK" not in block


@pytest.mark.asyncio
async def test_preview_signals_empty_when_nothing_supplied(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"detail": "should not be called"})

    _patch_httpx(monkeypatch, handler)

    block, used = await sig_module.gather_preview_signals(
        token="test-token",
        purpose=None,
        topology_id=None,
        device_ids=None,
        dynamic_requests=None,
    )
    assert block == ""
    assert used == []


@pytest.mark.asyncio
async def test_preview_signal_fetch_failure_is_tolerated(monkeypatch):
    """A topology fetch failure never raises; the topology signal is simply
    absent, and dynamic_templates (an independent fetch) still succeeds."""

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith(f"/topologies/{TOPOLOGY_ID}"):
            return httpx.Response(500, json={"detail": "boom"})
        if path.endswith(f"/templates/{TEMPLATE_ID}"):
            return httpx.Response(200, json={"name": "proxmox-clone"})
        return httpx.Response(404, json={"detail": "unmocked"})

    _patch_httpx(monkeypatch, handler)

    block, used = await sig_module.gather_preview_signals(
        token="test-token",
        purpose=None,
        topology_id=TOPOLOGY_ID,
        device_ids=None,
        dynamic_requests=[DynamicRequestItem(template_id=TEMPLATE_ID, count=1)],
    )
    assert sig_module.SIGNAL_TOPOLOGY not in used
    assert sig_module.SIGNAL_DYNAMIC_TEMPLATES in used
    assert "proxmox-clone" in block


@pytest.mark.asyncio
async def test_preview_uses_explicit_device_ids_without_topology(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/devices/batch"):
            body = request.read()
            assert str(DEVICE_A) in body.decode()
            return httpx.Response(200, json={"items": [_device_payload(DEVICE_A, "sw-a")]})
        return httpx.Response(404, json={"detail": "unmocked"})

    _patch_httpx(monkeypatch, handler)

    block, used = await sig_module.gather_preview_signals(
        token="test-token",
        purpose=None,
        topology_id=None,
        device_ids=[DEVICE_A],
        dynamic_requests=None,
    )
    assert sig_module.SIGNAL_TOPOLOGY in used
    assert "sw-a" in block


# --- internal pass ---


@pytest.mark.asyncio
async def test_internal_signals_include_all_structured_signals(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        assert request.headers.get("x-internal-token") == "internal-secret"
        if path.endswith(f"/devices/{DEVICE_A}/internal"):
            return httpx.Response(200, json=_device_payload(DEVICE_A, "sw-a"))
        if path.endswith(f"/devices/{DEVICE_A}/apply-jobs/internal"):
            return httpx.Response(200, json={"count": 2, "names": ["apply-x"]})
        if path.endswith(f"/templates/{TEMPLATE_ID}/internal"):
            return httpx.Response(200, json={"name": "proxmox-clone"})
        if path.endswith(f"/internal/forks/{RESERVATION_ID}"):
            return httpx.Response(
                200,
                json={
                    "connections": [{"layer": "L1"}, {"layer": "L1"}, {"layer": "L2"}],
                    "versions": [{}, {}, {}],
                },
            )
        return httpx.Response(404, json={"detail": "unmocked"})

    monkeypatch.setattr(config_module.settings, "internal_api_token", "internal-secret")
    _patch_httpx(monkeypatch, handler)

    block, used = await sig_module.gather_internal_signals(
        db=None,  # not touched: transcripts disabled below
        reservation_id=RESERVATION_ID,
        purpose="support case replication",
        device_ids=[DEVICE_A],
        dynamic_requests=[DynamicRequestItem(template_id=TEMPLATE_ID, count=1)],
        start_time=datetime(2026, 1, 1, tzinfo=timezone.utc),
        end_time=datetime(2026, 1, 1, 2, 30, tzinfo=timezone.utc),
        status="COMPLETED",
    )

    assert set(used) == {
        sig_module.SIGNAL_PURPOSE_TEXT,
        sig_module.SIGNAL_TOPOLOGY,
        sig_module.SIGNAL_DYNAMIC_TEMPLATES,
        sig_module.SIGNAL_CONFIG_APPLY_JOBS,
        sig_module.SIGNAL_FORK,
        sig_module.SIGNAL_DURATION_STATUS,
    }
    assert "support case replication" in block
    assert "sw-a" in block
    assert "apply-x" in block
    assert "2 jobs" in block
    assert "version_count: 3" in block
    assert "L1: 2, L2: 1" in block
    assert "COMPLETED" in block
    assert "duration_hours: 2.50" in block


@pytest.mark.asyncio
async def test_internal_config_apply_jobs_never_include_config_contents(monkeypatch):
    """The job summary carries only names/counts (see the inventory-side
    endpoint); this test pins that a raw config-shaped value never leaks
    through the rendered block even if a caller-supplied name looked like one."""

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith(f"/devices/{DEVICE_A}/internal"):
            return httpx.Response(200, json=_device_payload(DEVICE_A, "sw-a"))
        if path.endswith(f"/devices/{DEVICE_A}/apply-jobs/internal"):
            return httpx.Response(200, json={"count": 1, "names": ["apply-x"]})
        if path.endswith(f"/internal/forks/{RESERVATION_ID}"):
            return httpx.Response(404, json={"detail": "no fork"})
        return httpx.Response(404, json={"detail": "unmocked"})

    monkeypatch.setattr(config_module.settings, "internal_api_token", "internal-secret")
    _patch_httpx(monkeypatch, handler)

    block, used = await sig_module.gather_internal_signals(
        db=None,
        reservation_id=RESERVATION_ID,
        purpose=None,
        device_ids=[DEVICE_A],
        dynamic_requests=None,
        start_time=datetime(2026, 1, 1, tzinfo=timezone.utc),
        end_time=datetime(2026, 1, 1, 1, tzinfo=timezone.utc),
        status="FAILED",
    )
    assert sig_module.SIGNAL_FORK not in used  # 404 -> no fork -> signal absent
    assert "apply-x" in block
    assert "secret" not in block.lower()
    assert "vlan" not in block.lower()


@pytest.mark.asyncio
async def test_internal_signal_fetch_failure_is_tolerated(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"detail": "boom"})

    monkeypatch.setattr(config_module.settings, "internal_api_token", "internal-secret")
    _patch_httpx(monkeypatch, handler)

    block, used = await sig_module.gather_internal_signals(
        db=None,
        reservation_id=RESERVATION_ID,
        purpose="demo",
        device_ids=[DEVICE_A],
        dynamic_requests=None,
        start_time=datetime(2026, 1, 1, tzinfo=timezone.utc),
        end_time=datetime(2026, 1, 1, 1, tzinfo=timezone.utc),
        status="FAILED",
    )
    # Every external fetch failed; only the always-available signals remain.
    assert set(used) == {sig_module.SIGNAL_PURPOSE_TEXT, sig_module.SIGNAL_DURATION_STATUS}


# --- transcripts ---


async def _seed_conversation(user_id, reservation_id, turns):
    async with TestSessionLocal() as db:
        conv = AssistantConversation(
            id=uuid.uuid4(),
            user_id=user_id,
            reservation_id=reservation_id,
            seed_block="<seed/>",
        )
        db.add(conv)
        await db.flush()
        for position, (role, text) in enumerate(turns):
            db.add(
                AssistantMessage(
                    id=uuid.uuid4(),
                    conversation_id=conv.id,
                    role=role,
                    content_blocks=[{"type": "text", "text": text}],
                    position=position,
                )
            )
        await db.commit()


@pytest.mark.asyncio
async def test_transcripts_included_when_flag_on(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"detail": "unmocked"})

    monkeypatch.setattr(config_module.settings, "internal_api_token", "internal-secret")
    monkeypatch.setattr(config_module.settings, "ai_purpose_include_transcripts", True)
    _patch_httpx(monkeypatch, handler)

    await _seed_conversation(
        uuid.uuid4(),
        RESERVATION_ID,
        [
            (MessageRole.USER, "why is the switch dropping packets"),
            (MessageRole.ASSISTANT, "checking the port counters now"),
        ],
    )

    async with TestSessionLocal() as db:
        block, used = await sig_module.gather_internal_signals(
            db,
            reservation_id=RESERVATION_ID,
            purpose=None,
            device_ids=[],
            dynamic_requests=None,
            start_time=datetime(2026, 1, 1, tzinfo=timezone.utc),
            end_time=datetime(2026, 1, 1, 1, tzinfo=timezone.utc),
            status="FAILED",
        )

    assert sig_module.SIGNAL_TRANSCRIPTS in used
    assert "why is the switch dropping packets" in block
    assert "checking the port counters now" in block


@pytest.mark.asyncio
async def test_transcripts_omitted_when_flag_off(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"detail": "unmocked"})

    monkeypatch.setattr(config_module.settings, "internal_api_token", "internal-secret")
    monkeypatch.setattr(config_module.settings, "ai_purpose_include_transcripts", False)
    _patch_httpx(monkeypatch, handler)

    await _seed_conversation(
        uuid.uuid4(),
        RESERVATION_ID,
        [(MessageRole.USER, "sensitive question about the outage")],
    )

    async with TestSessionLocal() as db:
        block, used = await sig_module.gather_internal_signals(
            db,
            reservation_id=RESERVATION_ID,
            purpose=None,
            device_ids=[],
            dynamic_requests=None,
            start_time=datetime(2026, 1, 1, tzinfo=timezone.utc),
            end_time=datetime(2026, 1, 1, 1, tzinfo=timezone.utc),
            status="FAILED",
        )

    assert sig_module.SIGNAL_TRANSCRIPTS not in used
    assert "sensitive question about the outage" not in block


@pytest.mark.asyncio
async def test_transcripts_skip_tool_role_and_truncate_keeping_most_recent(monkeypatch):
    monkeypatch.setattr(config_module.settings, "internal_api_token", "internal-secret")
    monkeypatch.setattr(config_module.settings, "ai_purpose_include_transcripts", True)
    monkeypatch.setattr(sig_module, "TRANSCRIPT_CHAR_BUDGET", 60)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"detail": "unmocked"})

    _patch_httpx(monkeypatch, handler)

    await _seed_conversation(
        uuid.uuid4(),
        RESERVATION_ID,
        [
            (MessageRole.USER, "oldest turn should be dropped for budget"),
            (MessageRole.TOOL, "tool_result_should_never_appear"),
            (MessageRole.ASSISTANT, "newest turn kept"),
        ],
    )

    async with TestSessionLocal() as db:
        block, used = await sig_module.gather_internal_signals(
            db,
            reservation_id=RESERVATION_ID,
            purpose=None,
            device_ids=[],
            dynamic_requests=None,
            start_time=datetime(2026, 1, 1, tzinfo=timezone.utc),
            end_time=datetime(2026, 1, 1, 1, tzinfo=timezone.utc),
            status="FAILED",
        )

    assert sig_module.SIGNAL_TRANSCRIPTS in used
    assert "tool_result_should_never_appear" not in block
    assert "newest turn kept" in block


# --- dedupe and bounded fan-out (issue #709) ---


@pytest.mark.asyncio
async def test_dynamic_templates_fetch_count_equals_distinct_templates():
    """Three entries over two distinct templates issue two fetches, in
    first-seen order, and the repeated template's counts are summed."""
    other = uuid.uuid4()
    fetched: list[uuid.UUID] = []

    async def fetch_template(template_id):
        fetched.append(template_id)
        return {"name": "tpl-" + ("a" if template_id == TEMPLATE_ID else "b")}

    block = await sig_module._gather_dynamic_templates_block(
        fetch_template,
        [
            DynamicRequestItem(template_id=TEMPLATE_ID, count=1),
            DynamicRequestItem(template_id=other, count=5),
            DynamicRequestItem(template_id=TEMPLATE_ID, count=2),
        ],
    )

    assert fetched == [TEMPLATE_ID, other]
    assert block == "<dynamic_templates>\n  - tpl-a x3\n  - tpl-b x5\n</dynamic_templates>"


@pytest.mark.asyncio
async def test_dynamic_templates_untolerated_exception_still_propagates():
    """The drop-on-failure contract covers transport and body errors only; a
    defect inside a fetch is not swallowed by the fan-out."""

    async def fetch_template(template_id):
        raise RuntimeError("bug")

    with pytest.raises(RuntimeError, match="bug"):
        await sig_module._gather_dynamic_templates_block(
            fetch_template, [DynamicRequestItem(template_id=TEMPLATE_ID, count=1)]
        )


@pytest.mark.asyncio
async def test_internal_device_fanout_is_bounded_and_order_preserving(monkeypatch):
    """Twenty devices: never more than FANOUT_CONCURRENCY requests in
    flight, every device rendered in device_ids order even though responses
    complete out of order, one failing device dropped rather than fatal."""
    device_ids = [uuid.uuid4() for _ in range(20)]
    failing = device_ids[7]
    in_flight = 0
    peak = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal in_flight, peak
        path = request.url.path
        in_flight += 1
        peak = max(peak, in_flight)
        try:
            # Yield so other fan-out tasks get scheduled while this one is
            # "waiting"; later devices finish first to exercise ordering.
            for did in reversed(device_ids):
                if path.endswith(f"/devices/{did}/internal"):
                    await asyncio.sleep(0.001 * (20 - device_ids.index(did)))
                    if did == failing:
                        return httpx.Response(500, json={"detail": "boom"})
                    return httpx.Response(200, json=_device_payload(did, f"dev-{did}"))
                if path.endswith(f"/devices/{did}/apply-jobs/internal"):
                    await asyncio.sleep(0)
                    return httpx.Response(200, json={"count": 0, "names": []})
            return httpx.Response(404, json={"detail": "unmocked"})
        finally:
            in_flight -= 1

    monkeypatch.setattr(config_module.settings, "internal_api_token", "internal-secret")
    _patch_httpx(monkeypatch, handler)

    block, used = await sig_module.gather_internal_signals(
        db=None,
        reservation_id=RESERVATION_ID,
        purpose=None,
        device_ids=device_ids,
        dynamic_requests=None,
        start_time=datetime(2026, 1, 1, tzinfo=timezone.utc),
        end_time=datetime(2026, 1, 1, 1, tzinfo=timezone.utc),
        status="COMPLETED",
    )

    assert sig_module.SIGNAL_TOPOLOGY in used
    assert peak <= sig_module.FANOUT_CONCURRENCY
    assert peak > 1, "the fan-out ran sequentially"
    rendered = [line for line in block.splitlines() if line.startswith("  - dev-")]
    expected = [
        f"  - dev-{did}: template=switch (Arista 7050)" for did in device_ids if did != failing
    ]
    assert rendered == expected
