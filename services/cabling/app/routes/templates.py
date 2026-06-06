import copy
import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.dependencies import get_current_user_payload, require_admin
from app.models.template import TopologyTemplate
from app.models.topology import Topology, TopologyVersion
from app.schemas.template import (
    InstantiateRequest,
    PaginatedTemplateResponse,
    TemplateCreate,
    TemplateDetail,
    TemplateFromTopologyRequest,
    TemplateUpdate,
)
from app.schemas.topology import TopologyDetail

router = APIRouter(prefix="/templates", tags=["topology-templates"])


def _is_admin(payload: dict) -> bool:
    return payload.get("role") in ("admin", "superadmin")


def _can_manage(template: TopologyTemplate, payload: dict) -> bool:
    if _is_admin(payload):
        return True
    return str(template.created_by) == payload["sub"]


def _extract_role_template(canvas_data: dict[str, Any]) -> dict[str, Any]:
    """Walk the canvas and replace each device id with an inferred role label.

    Roles are derived from `node.data.device.template_name` plus a counter, so
    repeated templates produce stable `<template>-1`, `<template>-2`, etc.
    Edges are kept verbatim.
    """
    if not canvas_data:
        return {"nodes": [], "edges": []}

    nodes = canvas_data.get("nodes") or []
    edges = canvas_data.get("edges") or []

    counters: dict[str, int] = {}
    new_nodes: list[dict[str, Any]] = []
    for node in nodes:
        n = copy.deepcopy(node)
        data = n.setdefault("data", {})
        device = data.get("device") or {}
        template_name = (
            device.get("template_name") or data.get("label") or "device"
        ).strip().lower().replace(" ", "-") or "device"
        counters[template_name] = counters.get(template_name, 0) + 1
        role = f"{template_name}-{counters[template_name]}"
        data["device"] = {"role": role}
        new_nodes.append(n)

    return {"nodes": new_nodes, "edges": copy.deepcopy(edges)}


def _instantiate_canvas(
    canvas_data: dict[str, Any], role_assignments: dict[str, uuid.UUID]
) -> dict[str, Any]:
    """Build a fresh canvas where each role is replaced by the assigned device id."""
    if not canvas_data:
        return {"nodes": [], "edges": []}

    new_nodes: list[dict[str, Any]] = []
    for node in canvas_data.get("nodes") or []:
        n = copy.deepcopy(node)
        device = (n.get("data") or {}).get("device") or {}
        role = device.get("role")
        if not role:
            new_nodes.append(n)
            continue
        if role not in role_assignments:
            raise HTTPException(
                status_code=422,
                detail=f"missing assignment for role {role!r}",
            )
        device_data = n.setdefault("data", {}).setdefault("device", {})
        device_data["id"] = str(role_assignments[role])
        new_nodes.append(n)

    return {
        "nodes": new_nodes,
        "edges": copy.deepcopy(canvas_data.get("edges") or []),
    }


@router.get("", response_model=PaginatedTemplateResponse)
async def list_templates(
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=500),
    payload: dict = Depends(get_current_user_payload),
    db: AsyncSession = Depends(get_db),
):
    total = (await db.execute(select(func.count()).select_from(TopologyTemplate))).scalar() or 0
    rows = (
        (
            await db.execute(
                select(TopologyTemplate)
                .order_by(TopologyTemplate.updated_at.desc())
                .offset(skip)
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    return PaginatedTemplateResponse(items=list(rows), total=total, skip=skip, limit=limit)


@router.post("", response_model=TemplateDetail, status_code=status.HTTP_201_CREATED)
async def create_template(
    body: TemplateCreate,
    payload: dict = Depends(get_current_user_payload),
    db: AsyncSession = Depends(get_db),
):
    template = TopologyTemplate(
        name=body.name,
        description=body.description,
        canvas_data=body.canvas_data,
        created_by=uuid.UUID(payload["sub"]),
        owner_name=payload.get("username", ""),
    )
    db.add(template)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(status_code=409, detail=f"Template name {body.name!r} already exists")
    await db.refresh(template)
    return template


@router.post(
    "/from-topology/{topology_id}",
    response_model=TemplateDetail,
    status_code=status.HTTP_201_CREATED,
)
async def create_template_from_topology(
    topology_id: uuid.UUID,
    body: TemplateFromTopologyRequest,
    payload: dict = Depends(get_current_user_payload),
    db: AsyncSession = Depends(get_db),
):
    topology = await db.get(Topology, topology_id)
    if not topology:
        raise HTTPException(status_code=404, detail="Topology not found")

    role_canvas = _extract_role_template(topology.canvas_data or {})
    template = TopologyTemplate(
        name=body.name,
        description=body.description,
        canvas_data=role_canvas,
        created_by=uuid.UUID(payload["sub"]),
        owner_name=payload.get("username", ""),
    )
    db.add(template)
    await db.commit()
    await db.refresh(template)
    return template


@router.get("/{template_id}", response_model=TemplateDetail)
async def get_template(
    template_id: uuid.UUID,
    payload: dict = Depends(get_current_user_payload),
    db: AsyncSession = Depends(get_db),
):
    template = await db.get(TopologyTemplate, template_id)
    if not template:
        raise HTTPException(status_code=404, detail="Template not found")
    return template


@router.put("/{template_id}", response_model=TemplateDetail)
async def update_template(
    template_id: uuid.UUID,
    body: TemplateUpdate,
    payload: dict = Depends(get_current_user_payload),
    db: AsyncSession = Depends(get_db),
):
    template = await db.get(TopologyTemplate, template_id)
    if not template:
        raise HTTPException(status_code=404, detail="Template not found")
    if not _can_manage(template, payload):
        raise HTTPException(status_code=403, detail="Not authorized to modify this template")

    if body.name is not None:
        template.name = body.name
    if body.description is not None:
        template.description = body.description
    if body.canvas_data is not None:
        template.canvas_data = body.canvas_data
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(status_code=409, detail=f"Template name {body.name!r} already exists")
    await db.refresh(template)
    return template


@router.delete("/{template_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_template(
    template_id: uuid.UUID,
    payload: dict = Depends(get_current_user_payload),
    db: AsyncSession = Depends(get_db),
):
    template = await db.get(TopologyTemplate, template_id)
    if not template:
        raise HTTPException(status_code=404, detail="Template not found")
    if not _can_manage(template, payload):
        raise HTTPException(status_code=403, detail="Not authorized to delete this template")
    await db.delete(template)
    await db.commit()


@router.post(
    "/{template_id}/instantiate",
    response_model=TopologyDetail,
    status_code=status.HTTP_201_CREATED,
)
async def instantiate_template(
    template_id: uuid.UUID,
    body: InstantiateRequest,
    payload: dict = Depends(get_current_user_payload),
    db: AsyncSession = Depends(get_db),
):
    template = await db.get(TopologyTemplate, template_id)
    if not template:
        raise HTTPException(status_code=404, detail="Template not found")

    role_assignments = body.role_assignments or {}
    new_canvas = _instantiate_canvas(template.canvas_data or {}, role_assignments)

    topology = Topology(
        name=body.name,
        created_by=uuid.UUID(payload["sub"]),
        owner_name=payload.get("username", ""),
        canvas_data=new_canvas,
    )
    db.add(topology)
    await db.flush()

    snapshot = TopologyVersion(
        topology_id=topology.id,
        version_number=1,
        canvas_data=new_canvas,
        name=topology.name,
        description=f"Instantiated from template {template.name}",
        created_by=uuid.UUID(payload["sub"]),
        author_name=payload.get("username", ""),
    )
    db.add(snapshot)

    await db.commit()
    await db.refresh(topology)
    return topology


# require_admin is referenced for symmetry with other routers but the manage-
# permission check above already covers admin/owner via _can_manage.
__all__ = ["router", "require_admin"]
