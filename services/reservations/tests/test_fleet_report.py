"""Fleet-utilization report tests (issue #292).

Covers the fleet section of the utilization report end to end at the unit
level: the aggregation math (denominator, idle devices, unclamped pct), the
per-section status filtering (fleet counts ACTIVE by default, the legacy
sections do not), the paginated inventory fetch with its degrade-to-None
contract, the fleet CSV section, and the route wiring including the 503 on
an unreachable inventory when the fleet CSV is requested.
"""

import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from app.database import Base, get_db
from app.dependencies.auth import get_current_user_payload, require_admin
from app.main import app
from app.models.reservation import Reservation, ReservationStatus
from app.routers.reservations import bearer_scheme
from app.schemas.reservation import FleetDeviceBucket, FleetSection, UtilizationReport
from app.services.reporting_service import (
    _build_fleet_section,
    build_utilization_report,
    fetch_fleet_devices,
    report_to_csv,
)
from fastapi.security import HTTPAuthorizationCredentials
from herd_common.enums import TopologyType
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

NOW = datetime(2026, 5, 1, 0, 0, tzinfo=timezone.utc)
WINDOW_START = NOW - timedelta(hours=24)
WINDOW_END = NOW

USER_A = uuid.uuid4()
DEVICE_X = uuid.uuid4()
DEVICE_Y = uuid.uuid4()
DEVICE_Z = uuid.uuid4()


def _inventory_device(device_id: uuid.UUID, name: str, status: str = "AVAILABLE") -> dict:
    return {"id": str(device_id), "name": name, "status": status}


def _reservation(
    device_ids: list[uuid.UUID],
    start: datetime,
    end: datetime,
    status: ReservationStatus = ReservationStatus.COMPLETED,
) -> Reservation:
    return Reservation(
        id=uuid.uuid4(),
        user_id=USER_A,
        owner_name="alice",
        device_ids=[str(d) for d in device_ids],
        topology_id=None,
        topology_type=TopologyType.PHYSICAL,
        start_time=start,
        end_time=end,
        status=status,
    )


@pytest.fixture
async def db_session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        yield session
    await engine.dispose()


# --- aggregation math ---


@pytest.mark.asyncio
async def test_fleet_section_math_and_idle_devices(db_session):
    db_session.add(
        _reservation(
            [DEVICE_X], WINDOW_START + timedelta(hours=2), WINDOW_START + timedelta(hours=8)
        )
    )
    await db_session.commit()

    report = await build_utilization_report(
        db_session,
        WINDOW_START,
        WINDOW_END,
        [ReservationStatus.COMPLETED],
        fleet_status_filter=[ReservationStatus.ACTIVE, ReservationStatus.COMPLETED],
        fleet_devices=[
            _inventory_device(DEVICE_X, "sw-a"),
            _inventory_device(DEVICE_Y, "sw-b", status="MAINTENANCE"),
        ],
    )

    fleet = report.fleet
    assert fleet is not None
    assert fleet.device_count == 2
    assert fleet.idle_device_count == 1
    assert fleet.window_hours == pytest.approx(24.0)
    assert fleet.total_reserved_hours == pytest.approx(6.0)
    # 6 reserved device-hours over 2 devices x 24h = 12.5 percent fleet-wide.
    assert fleet.utilization_pct == pytest.approx(12.5)

    # Sorted by pct desc; the idle MAINTENANCE box is present with 0.0.
    assert fleet.devices[0].device_id == DEVICE_X
    assert fleet.devices[0].name == "sw-a"
    assert fleet.devices[0].hours == pytest.approx(6.0)
    assert fleet.devices[0].utilization_pct == pytest.approx(25.0)
    assert fleet.devices[0].reservation_count == 1
    assert fleet.devices[1].device_id == DEVICE_Y
    assert fleet.devices[1].status == "MAINTENANCE"
    assert fleet.devices[1].hours == 0.0
    assert fleet.devices[1].utilization_pct == 0.0


@pytest.mark.asyncio
async def test_fleet_counts_active_but_legacy_default_does_not(db_session):
    db_session.add(
        _reservation(
            [DEVICE_X],
            WINDOW_START + timedelta(hours=1),
            WINDOW_START + timedelta(hours=5),
            status=ReservationStatus.ACTIVE,
        )
    )
    await db_session.commit()

    report = await build_utilization_report(
        db_session,
        WINDOW_START,
        WINDOW_END,
        [ReservationStatus.COMPLETED],
        fleet_status_filter=[ReservationStatus.ACTIVE, ReservationStatus.COMPLETED],
        fleet_devices=[_inventory_device(DEVICE_X, "sw-a")],
    )

    # The in-flight reservation is invisible to the legacy sections...
    assert report.total_hours == 0.0
    assert report.by_user == []
    assert report.by_device == []
    # ...but counts toward fleet utilization.
    assert report.fleet is not None
    assert report.fleet.devices[0].hours == pytest.approx(4.0)
    assert report.fleet.idle_device_count == 0


@pytest.mark.asyncio
async def test_explicit_filter_applies_to_fleet_too(db_session):
    db_session.add(
        _reservation(
            [DEVICE_X],
            WINDOW_START + timedelta(hours=1),
            WINDOW_START + timedelta(hours=5),
            status=ReservationStatus.ACTIVE,
        )
    )
    await db_session.commit()

    # Caller explicitly asked for COMPLETED only: the route passes the same
    # filter to both sections, so the ACTIVE reservation counts nowhere.
    report = await build_utilization_report(
        db_session,
        WINDOW_START,
        WINDOW_END,
        [ReservationStatus.COMPLETED],
        fleet_status_filter=[ReservationStatus.COMPLETED],
        fleet_devices=[_inventory_device(DEVICE_X, "sw-a")],
    )

    assert report.fleet is not None
    assert report.fleet.total_reserved_hours == 0.0
    assert report.fleet.idle_device_count == 1


@pytest.mark.asyncio
async def test_device_missing_from_inventory_stays_in_legacy_section_only(db_session):
    db_session.add(
        _reservation(
            [DEVICE_Z], WINDOW_START + timedelta(hours=1), WINDOW_START + timedelta(hours=3)
        )
    )
    await db_session.commit()

    report = await build_utilization_report(
        db_session,
        WINDOW_START,
        WINDOW_END,
        [ReservationStatus.COMPLETED],
        fleet_status_filter=[ReservationStatus.COMPLETED],
        fleet_devices=[_inventory_device(DEVICE_X, "sw-a")],
    )

    # Deleted-from-inventory device: still in by_device, absent from fleet.
    assert [b.device_id for b in report.by_device] == [DEVICE_Z]
    assert report.fleet is not None
    assert [b.device_id for b in report.fleet.devices] == [DEVICE_X]
    assert report.fleet.total_reserved_hours == 0.0


@pytest.mark.asyncio
async def test_fleet_omitted_when_devices_none(db_session):
    report = await build_utilization_report(
        db_session,
        WINDOW_START,
        WINDOW_END,
        [ReservationStatus.COMPLETED],
        fleet_status_filter=[ReservationStatus.ACTIVE, ReservationStatus.COMPLETED],
        fleet_devices=None,
    )
    assert report.fleet is None


@pytest.mark.asyncio
async def test_fleet_pct_is_not_clamped_at_100(db_session):
    # Two full-window rows on one device. Conflict detection would prevent
    # this for live bookings, but a filter that includes FAILED can see
    # overlapping rows; the report shows the raw 200 percent rather than
    # clamping away the signal.
    for _ in range(2):
        db_session.add(_reservation([DEVICE_X], WINDOW_START, WINDOW_END))
    await db_session.commit()

    report = await build_utilization_report(
        db_session,
        WINDOW_START,
        WINDOW_END,
        [ReservationStatus.COMPLETED],
        fleet_status_filter=[ReservationStatus.COMPLETED],
        fleet_devices=[_inventory_device(DEVICE_X, "sw-a")],
    )

    assert report.fleet is not None
    assert report.fleet.devices[0].utilization_pct == pytest.approx(200.0)


@pytest.mark.asyncio
async def test_empty_inventory_yields_empty_fleet_section(db_session):
    report = await build_utilization_report(
        db_session,
        WINDOW_START,
        WINDOW_END,
        [ReservationStatus.COMPLETED],
        fleet_status_filter=[ReservationStatus.COMPLETED],
        fleet_devices=[],
    )
    fleet = report.fleet
    assert fleet is not None
    assert fleet.device_count == 0
    assert fleet.idle_device_count == 0
    assert fleet.utilization_pct == 0.0
    assert fleet.devices == []


def test_build_fleet_section_skips_device_with_unusable_id():
    section = _build_fleet_section(
        [{"name": "no-id"}, _inventory_device(DEVICE_X, "sw-a")],
        {},
        {},
        WINDOW_START,
        WINDOW_END,
    )
    assert section.device_count == 1
    assert section.devices[0].device_id == DEVICE_X


# --- inventory fetch ---


@pytest.mark.asyncio
async def test_fetch_fleet_devices_none_without_auth_header():
    assert await fetch_fleet_devices(None) is None


@pytest.mark.asyncio
async def test_fetch_fleet_devices_single_page():
    d = _inventory_device(DEVICE_X, "sw-a")
    resp = httpx.Response(200, json={"items": [d], "total": 1, "skip": 0, "limit": 500})
    with patch("httpx.AsyncClient") as mock_client_cls:
        instance = mock_client_cls.return_value.__aenter__.return_value
        instance.get = AsyncMock(return_value=resp)
        result = await fetch_fleet_devices("Bearer tok")

    assert result == [d]
    assert instance.get.await_count == 1
    called_params = instance.get.await_args.kwargs["params"]
    assert called_params == {"skip": 0, "limit": 500}


@pytest.mark.asyncio
async def test_fetch_fleet_devices_paginates_past_500():
    page1 = [_inventory_device(uuid.uuid4(), f"d{i}") for i in range(500)]
    page2 = [_inventory_device(uuid.uuid4(), f"d{500 + i}") for i in range(100)]
    responses = [
        httpx.Response(200, json={"items": page1, "total": 600, "skip": 0, "limit": 500}),
        httpx.Response(200, json={"items": page2, "total": 600, "skip": 500, "limit": 500}),
    ]
    with patch("httpx.AsyncClient") as mock_client_cls:
        instance = mock_client_cls.return_value.__aenter__.return_value
        instance.get = AsyncMock(side_effect=responses)
        result = await fetch_fleet_devices("Bearer tok")

    assert result is not None
    assert len(result) == 600
    assert instance.get.await_count == 2
    assert instance.get.await_args_list[1].kwargs["params"] == {"skip": 500, "limit": 500}


@pytest.mark.asyncio
async def test_fetch_fleet_devices_none_on_non_200():
    with patch("httpx.AsyncClient") as mock_client_cls:
        instance = mock_client_cls.return_value.__aenter__.return_value
        instance.get = AsyncMock(return_value=httpx.Response(503, text="down"))
        assert await fetch_fleet_devices("Bearer tok") is None


@pytest.mark.asyncio
async def test_fetch_fleet_devices_none_on_unreachable():
    with patch("httpx.AsyncClient") as mock_client_cls:
        instance = mock_client_cls.return_value.__aenter__.return_value
        instance.get = AsyncMock(side_effect=httpx.ConnectError("boom"))
        assert await fetch_fleet_devices("Bearer tok") is None


# --- CSV ---


def _report_with_fleet(fleet: FleetSection | None) -> UtilizationReport:
    return UtilizationReport(
        window_start=WINDOW_START,
        window_end=WINDOW_END,
        total_hours=0.0,
        total_reservations=0,
        by_user=[],
        by_device=[],
        fleet=fleet,
    )


def test_report_to_csv_fleet_section():
    fleet = FleetSection(
        device_count=2,
        idle_device_count=1,
        window_hours=24.0,
        total_reserved_hours=6.0,
        utilization_pct=12.5,
        devices=[
            FleetDeviceBucket(
                device_id=DEVICE_X,
                name="sw, with comma",
                status="AVAILABLE",
                reservation_count=1,
                hours=6.0,
                utilization_pct=25.0,
            ),
            FleetDeviceBucket(
                device_id=DEVICE_Y,
                name="sw-b",
                status="MAINTENANCE",
                reservation_count=0,
                hours=0.0,
                utilization_pct=0.0,
            ),
        ],
    )
    body = report_to_csv(_report_with_fleet(fleet), "fleet")
    lines = body.splitlines()
    assert lines[0] == "device_id,name,status,hours,utilization_pct,reservation_count"
    assert lines[1] == f'{DEVICE_X},"sw, with comma",AVAILABLE,6.0000,25.00,1'
    assert lines[2] == f"{DEVICE_Y},sw-b,MAINTENANCE,0.0000,0.00,0"


def test_report_to_csv_fleet_requires_populated_section():
    with pytest.raises(ValueError, match="fleet section is not populated on this report"):
        report_to_csv(_report_with_fleet(None), "fleet")


# --- routes ---

TEST_DATABASE_URL = "sqlite+aiosqlite:///:memory:"
route_engine = create_async_engine(TEST_DATABASE_URL, echo=False)
RouteSessionLocal = async_sessionmaker(route_engine, expire_on_commit=False)


async def override_get_db():
    async with RouteSessionLocal() as session:
        yield session


def override_bearer():
    return HTTPAuthorizationCredentials(scheme="Bearer", credentials="fake-token")


@pytest.fixture
async def route_db():
    async with route_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with route_engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


@pytest.fixture
async def admin_client(route_db):
    admin_payload = {"sub": str(USER_A), "username": "adminuser", "role": "admin"}
    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_current_user_payload] = lambda: admin_payload
    app.dependency_overrides[require_admin] = lambda: admin_payload
    app.dependency_overrides[bearer_scheme] = override_bearer
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac
    app.dependency_overrides.clear()


async def _seed_route_reservation(status: ReservationStatus) -> None:
    async with RouteSessionLocal() as session:
        session.add(
            _reservation(
                [DEVICE_X],
                WINDOW_START + timedelta(hours=1),
                WINDOW_START + timedelta(hours=5),
                status=status,
            )
        )
        await session.commit()


def _window_params(**extra) -> dict:
    return {"start": WINDOW_START.isoformat(), "end": WINDOW_END.isoformat(), **extra}


@pytest.mark.asyncio
async def test_report_route_fleet_defaults_count_active(admin_client):
    await _seed_route_reservation(ReservationStatus.ACTIVE)
    devices = [_inventory_device(DEVICE_X, "sw-a"), _inventory_device(DEVICE_Y, "sw-b")]
    with patch(
        "app.routers.reservations.fetch_fleet_devices",
        new=AsyncMock(return_value=devices),
    ):
        resp = await admin_client.get("/reports/utilization", params=_window_params())
    assert resp.status_code == 200
    data = resp.json()
    # Legacy default (COMPLETED only) sees nothing; fleet default counts ACTIVE.
    assert data["total_hours"] == 0.0
    fleet = data["fleet"]
    assert fleet["device_count"] == 2
    assert fleet["idle_device_count"] == 1
    assert fleet["devices"][0]["device_id"] == str(DEVICE_X)
    assert fleet["devices"][0]["hours"] == pytest.approx(4.0)


@pytest.mark.asyncio
async def test_report_route_fleet_null_when_inventory_unreachable(admin_client):
    with patch(
        "app.routers.reservations.fetch_fleet_devices",
        new=AsyncMock(return_value=None),
    ):
        resp = await admin_client.get("/reports/utilization", params=_window_params())
    assert resp.status_code == 200
    assert resp.json()["fleet"] is None


@pytest.mark.asyncio
async def test_csv_route_fleet_section(admin_client):
    await _seed_route_reservation(ReservationStatus.COMPLETED)
    with patch(
        "app.routers.reservations.fetch_fleet_devices",
        new=AsyncMock(return_value=[_inventory_device(DEVICE_X, "sw-a")]),
    ):
        resp = await admin_client.get(
            "/reports/utilization.csv", params=_window_params(section="fleet")
        )
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/csv")
    assert "utilization-fleet-" in resp.headers["content-disposition"]
    lines = resp.text.splitlines()
    assert lines[0] == "device_id,name,status,hours,utilization_pct,reservation_count"
    assert lines[1].startswith(f"{DEVICE_X},sw-a,AVAILABLE,4.0000,")


@pytest.mark.asyncio
async def test_csv_route_fleet_503_when_inventory_unreachable(admin_client):
    with patch(
        "app.routers.reservations.fetch_fleet_devices",
        new=AsyncMock(return_value=None),
    ):
        resp = await admin_client.get(
            "/reports/utilization.csv", params=_window_params(section="fleet")
        )
    assert resp.status_code == 503
    assert resp.json()["detail"] == "Inventory service is unreachable"


@pytest.mark.asyncio
async def test_csv_route_other_sections_skip_inventory_fetch(admin_client):
    await _seed_route_reservation(ReservationStatus.COMPLETED)
    fetch = AsyncMock(return_value=None)
    with patch("app.routers.reservations.fetch_fleet_devices", new=fetch):
        resp = await admin_client.get(
            "/reports/utilization.csv", params=_window_params(section="user")
        )
    assert resp.status_code == 200
    fetch.assert_not_awaited()
