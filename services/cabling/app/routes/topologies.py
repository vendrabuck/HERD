import copy
import uuid
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status
from herd_common.internal_auth import internal_token_matches
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.dependencies import get_current_user_payload
from app.models.topology import Topology, TopologyVersion
from app.schemas.topology import (
    PaginatedTopologyResponse,
    TopologyClone,
    TopologyCreate,
    TopologyDetail,
    TopologyUpdate,
    TopologyValidationResponse,
)
from app.services.canvas_nodes import redact_invisible_device_nodes, strip_device_nodes
from app.services.reservation_guard import assert_topology_deletable, find_blocking_reservations
from app.services.topology_validation import run_full_topology_validation
from app.services.version_service import commit_with_new_version
from app.services.visible_devices import resolve_caller_visibility

router = APIRouter(prefix="/topologies", tags=["topologies"])


# Topologies list controls (issue #958), the same shape as the reservations
# list's sort_by/sort_dir (issue #844): module-level Literal allowlists, so an
# unrecognized value is a 422 from FastAPI's validation instead of a silent
# no-op, and the OpenAPI schema carries each set as an enum.
TopologySortField = Literal["name", "owner_name", "created_at", "updated_at"]
TopologySortDir = Literal["asc", "desc"]
TopologyOwnerFilter = Literal["mine", "all"]

_SORTABLE_COLUMNS = {
    "name": Topology.name,
    "owner_name": Topology.owner_name,
    "created_at": Topology.created_at,
    "updated_at": Topology.updated_at,
}
_TEXT_SORT_FIELDS = frozenset({"name", "owner_name"})


def _topology_order_by(sort_by: str, sort_dir: str, dialect_name: str):
    """Build the ORDER BY for the topologies list, identical on every dialect.

    Text fields sort case-insensitively by ``lower(coalesce(column, ''))``:
    - coalesce, because owner_name is nullable and the two dialects place a NULL
      at opposite ends (Postgres treats it as the largest value, SQLite as the
      smallest); an empty owner then sorts first ascending on both.
    - lower plus the byte-order "C" collation on Postgres, because the database
      default collation decides text order there and differs per deployment
      (the gate stack's en_US.utf8 orders "B" before "a"); SQLite's default
      BINARY collation is already byte order. Pinned on a real Postgres by
      tests/test_topology_list_order_live_pg.py.
    Every ordering ends with an id tiebreak (ascending, whatever the primary
    direction), so rows that tie on the primary key keep one stable order across
    pages.
    """
    column = _SORTABLE_COLUMNS[sort_by]
    if sort_by in _TEXT_SORT_FIELDS:
        column = func.lower(func.coalesce(column, ""))
        if dialect_name == "postgresql":
            column = column.collate("C")
    primary = column.asc() if sort_dir == "asc" else column.desc()
    return primary, Topology.id


@router.get("", response_model=PaginatedTopologyResponse)
async def list_topologies(
    skip: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
    search: Annotated[
        str | None,
        Query(
            max_length=255,
            description=(
                "Case-insensitive substring match on the topology name. Blank means no filter."
            ),
        ),
    ] = None,
    owner: Annotated[
        TopologyOwnerFilter,
        Query(description="mine: only topologies the caller created. all (default): every one."),
    ] = "all",
    sort_by: Annotated[
        TopologySortField,
        Query(
            description=(
                "Field to sort by. Defaults to updated_at, the previous fixed ordering. "
                "Text fields sort case-insensitively. Tiebroken by id for stable pagination."
            ),
        ),
    ] = "updated_at",
    sort_dir: Annotated[
        TopologySortDir,
        Query(description="Sort direction. Defaults to desc (most recently updated first)."),
    ] = "desc",
    payload: dict = Depends(get_current_user_payload),
    db: AsyncSession = Depends(get_db),
):
    # Every role reads every topology (docs/ROLES.md), so the filters narrow the
    # list and never widen it; total is counted over the same filtered set.
    conditions = []
    term = (search or "").strip()
    if term:
        conditions.append(Topology.name.icontains(term, autoescape=True))
    if owner == "mine":
        conditions.append(Topology.created_by == uuid.UUID(payload["sub"]))

    count_query = select(func.count()).select_from(Topology).where(*conditions)
    total = (await db.execute(count_query)).scalar() or 0

    order_by = _topology_order_by(sort_by, sort_dir, db.get_bind().dialect.name)
    result = await db.execute(
        select(Topology).where(*conditions).order_by(*order_by).offset(skip).limit(limit)
    )
    topologies = result.scalars().all()
    return PaginatedTopologyResponse(
        items=list(topologies),
        total=total,
        skip=skip,
        limit=limit,
    )


@router.post("", response_model=TopologyDetail, status_code=status.HTTP_201_CREATED)
async def create_topology(
    body: TopologyCreate,
    payload: dict = Depends(get_current_user_payload),
    db: AsyncSession = Depends(get_db),
):
    topology = Topology(
        name=body.name,
        created_by=uuid.UUID(payload["sub"]),
        owner_name=payload.get("username", ""),
        canvas_data=None,
    )
    db.add(topology)
    await db.commit()
    await db.refresh(topology)
    return topology


@router.get("/{topology_id}", response_model=TopologyDetail)
async def get_topology(
    topology_id: uuid.UUID,
    payload: dict = Depends(get_current_user_payload),
    db: AsyncSession = Depends(get_db),
):
    topology = await db.get(Topology, topology_id)
    if not topology:
        raise HTTPException(status_code=404, detail="Topology not found")
    # Read-side strip (belt and braces): the write side already reduces a
    # device node's data.device to the allowlist before a row is stored, but a
    # stack that upgraded images without running `make migrate` (the missed
    # migration is a logged warning, not a boot failure, per the migration
    # lifecycle notes) could still be serving a pre-fix row. Build the
    # response model explicitly and overwrite its canvas_data rather than
    # mutate the ORM object: this must never re-persist what it strips.
    detail = TopologyDetail.model_validate(topology)
    detail.canvas_data = strip_device_nodes(detail.canvas_data)
    return detail


@router.put("/{topology_id}", response_model=TopologyDetail)
async def update_topology(
    topology_id: uuid.UUID,
    body: TopologyUpdate,
    request: Request,
    payload: dict = Depends(get_current_user_payload),
    db: AsyncSession = Depends(get_db),
):
    topology = await db.get(Topology, topology_id)
    if not topology:
        raise HTTPException(status_code=404, detail="Topology not found")

    # Only creator or admin can update
    user_role = payload.get("role", "user")
    is_admin = user_role in ("admin", "superadmin")
    if str(topology.created_by) != payload["sub"] and not is_admin:
        raise HTTPException(status_code=403, detail="Not authorized to update this topology")

    # Reduce a device node's `data.device` to the allowlist before it ever
    # reaches a comparison or a write: canvas_changed must judge the stripped
    # shape against the already-stripped stored canvas, and the stripped value
    # is what gets persisted below and into the version snapshot.
    stripped_canvas_data = (
        None if body.canvas_data is None else strip_device_nodes(body.canvas_data)
    )
    canvas_changed = (
        stripped_canvas_data is not None and stripped_canvas_data != topology.canvas_data
    )

    # Reservation-scoped lock: a topology with a live reservation may only have
    # its wiring changed by the reservation owner (or an admin). This is what
    # makes "edit the running reservation's topology" safe: the owner editing
    # their own live reservation is allowed and flows through to provisioning,
    # while an unrelated user cannot mutate wiring out from under an active
    # reservation. Name/description-only edits do not touch the live wiring, so
    # they are not gated. The guard fails open if the reservations service is
    # unreachable (see find_blocking_reservations).
    if canvas_changed:
        authorization = request.headers.get("authorization", "")
        token = authorization.removeprefix("Bearer ").strip() if authorization else ""
        if token and not is_admin:
            blocking = await find_blocking_reservations(topology_id)
            others = [b for b in blocking if str(b.get("user_id") or "") != payload["sub"]]
            if others:
                raise HTTPException(
                    status_code=409,
                    detail={
                        "message": (
                            "Topology is in use by an active reservation owned by "
                            "another user; editing its wiring is blocked"
                        ),
                        "reservations": [
                            {
                                "id": str(b.get("id")),
                                "status": b.get("status"),
                                "end_time": b.get("end_time"),
                            }
                            for b in others
                        ],
                    },
                )

    if body.name is not None:
        topology.name = body.name
    if stripped_canvas_data is not None:
        topology.canvas_data = stripped_canvas_data
    topology.modified_by = uuid.UUID(payload["sub"])

    if canvas_changed:
        # version_number is allocated as max+1 under a unique constraint; serialize
        # against concurrent writers with a bounded retry loop rather than risking a
        # raw IntegrityError 500 (see commit_with_new_version).
        snapshot = TopologyVersion(
            topology_id=topology.id,
            canvas_data=stripped_canvas_data,
            name=topology.name,
            description=body.description,
            created_by=uuid.UUID(payload["sub"]),
            author_name=payload.get("username", ""),
        )
        await commit_with_new_version(db, topology, snapshot)
    else:
        await db.commit()
    await db.refresh(topology)
    return topology


@router.post(
    "/{topology_id}/clone",
    response_model=TopologyDetail,
    status_code=status.HTTP_201_CREATED,
)
async def clone_topology(
    topology_id: uuid.UUID,
    body: TopologyClone,
    payload: dict = Depends(get_current_user_payload),
    db: AsyncSession = Depends(get_db),
):
    source = await db.get(Topology, topology_id)
    if not source:
        raise HTTPException(status_code=404, detail="Topology not found")

    # source.canvas_data was already stripped when it was written; strip again
    # here anyway (cheap and idempotent) rather than trust that invariant across
    # a clone, which is a fresh write boundary of its own.
    cloned_canvas = (
        None
        if source.canvas_data is None
        else strip_device_nodes(copy.deepcopy(source.canvas_data))
    )

    clone = Topology(
        name=body.name,
        created_by=uuid.UUID(payload["sub"]),
        owner_name=payload.get("username", ""),
        canvas_data=cloned_canvas,
    )
    db.add(clone)
    await db.flush()

    snapshot = TopologyVersion(
        topology_id=clone.id,
        version_number=1,
        canvas_data=cloned_canvas,
        name=clone.name,
        description=f"Cloned from {source.name}",
        created_by=uuid.UUID(payload["sub"]),
        author_name=payload.get("username", ""),
    )
    db.add(snapshot)

    await db.commit()
    await db.refresh(clone)
    return clone


@router.post("/{topology_id}/validate/internal", response_model=TopologyValidationResponse)
async def validate_topology_internal(
    topology_id: uuid.UUID,
    l3: bool = Query(True),
    x_internal_token: str = Header(..., alias="X-Internal-Token"),
    db: AsyncSession = Depends(get_db),
):
    """Service-to-service validate; X-Internal-Token only.

    Reservations calls this during create to gate bookings against topologies
    with unreachable edges. The booking user does not necessarily own the
    topology being reserved, so JWT-forward against the public /validate
    endpoint would 403 on the creator-or-admin check.

    ``l3=0`` (R11 review fix, ADR 0014 phase 1, issue #34) skips the L3 routing
    -intent pass entirely: no ``resolve_canvas_wiring`` call, no inventory call,
    ``invalid_routes`` empty. Reservations' ACTIVE device-set PATCH path (the
    issue #701 membership check) passes this, since a device-set edit judges only
    physical connectivity for the revised membership; the topology's routing
    intent was already judged at create time.
    """
    if not internal_token_matches(x_internal_token, settings.internal_api_token):
        raise HTTPException(status_code=403, detail="Invalid internal token")
    topology = await db.get(Topology, topology_id)
    if not topology:
        raise HTTPException(status_code=404, detail="Topology not found")
    return await run_full_topology_validation(topology.canvas_data, db, check_routes=l3)


@router.post("/{topology_id}/validate", response_model=TopologyValidationResponse)
async def validate_topology(
    topology_id: uuid.UUID,
    payload: dict = Depends(get_current_user_payload),
    authorization: str | None = Header(None),
    db: AsyncSession = Depends(get_db),
):
    """User-facing validate: returns invalid edges for the topology editor.

    RBAC: validation reveals which device pairs lack physical paths, so it is
    restricted to the topology creator or admins. Service-to-service callers
    use /validate/internal instead.

    Issue #763: for a NON-ADMIN caller the canvas is first redacted to the
    devices that caller can see. A canvas may name any device uuid, and this
    route answers, per named device, whether it is physically reachable and
    (since ADR 0014 phase 1) whether it is a Layer 3 switch, which interfaces
    it has, and which subnets those interfaces carry: a config-content oracle
    for gear outside the caller's device-group visibility. A node naming a
    hidden device is therefore reported through the EXISTING ``missing_device``
    reason, indistinguishable from a node whose device reference resolves to
    nothing, and it is excluded from the edge pass and the L3 pass exactly as
    such a node already is. Admins are never filtered and never trigger the
    inventory lookup. The visibility lookup fails CLOSED (503): an
    unverifiable answer must not degrade into an unfiltered one.
    """
    topology = await db.get(Topology, topology_id)
    if not topology:
        raise HTTPException(status_code=404, detail="Topology not found")

    user_role = payload.get("role", "user")
    if str(topology.created_by) != payload["sub"] and user_role not in ("admin", "superadmin"):
        raise HTTPException(status_code=403, detail="Not authorized to validate this topology")

    visible_ids = await resolve_caller_visibility(
        payload,
        authorization,
        unavailable_detail=(
            "Could not verify device visibility; the topology was not validated. Retry the request."
        ),
    )
    canvas = topology.canvas_data
    if visible_ids is not None:
        canvas = redact_invisible_device_nodes(canvas, visible_ids)

    return await run_full_topology_validation(canvas, db)


@router.delete("/{topology_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_topology(
    topology_id: uuid.UUID,
    payload: dict = Depends(get_current_user_payload),
    db: AsyncSession = Depends(get_db),
):
    topology = await db.get(Topology, topology_id)
    if not topology:
        raise HTTPException(status_code=404, detail="Topology not found")

    # Only creator or admin can delete
    user_role = payload.get("role", "user")
    if str(topology.created_by) != payload["sub"] and user_role not in ("admin", "superadmin"):
        raise HTTPException(status_code=403, detail="Not authorized to delete this topology")

    # Issue #977: refuse while a live reservation references the topology (409
    # topology_in_use; 503 when reservations cannot answer). Runs after the 404
    # and the 403 so an unauthorized caller learns nothing about reservations.
    await assert_topology_deletable(topology_id)

    await db.delete(topology)
    await db.commit()
