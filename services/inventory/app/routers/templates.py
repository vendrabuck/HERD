import logging
import uuid

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from herd_common.internal_auth import internal_token_matches
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.dependencies.auth import get_current_user_payload, require_admin
from app.models.template import DeviceTemplate
from app.schemas.template import (
    PaginatedTemplateResponse,
    TemplateCreate,
    TemplateResponse,
    TemplateUpdate,
)
from app.services.template_service import (
    create_template,
    delete_template,
    get_template,
    list_templates,
    update_template,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["templates"])


def _template_to_response(template: DeviceTemplate) -> TemplateResponse:
    return TemplateResponse(
        id=template.id,
        name=template.name,
        template_type=template.template_type,
        driver_id=template.driver_id,
        driver_name=template.driver.name if template.driver else None,
        driver_sha256=template.driver.sha256 if template.driver else None,
        driver_filename=template.driver.filename if template.driver else None,
        hypervisor_id=template.hypervisor_id,
        connection_type=template.driver.connection_type if template.driver else None,
        exclusive=template.exclusive,
        icon=template.icon,
        description=template.description,
        vendor=template.vendor,
        model=template.model,
        part_number=template.part_number,
        sections=template.sections,
        created_at=template.created_at,
        updated_at=template.updated_at,
        modified_by=template.modified_by,
        poll_interval_seconds=template.poll_interval_seconds,
    )


@router.get("/templates", response_model=PaginatedTemplateResponse)
async def get_templates(
    template_type: str | None = Query(None),
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(get_current_user_payload),
):
    """List all templates. Available to all authenticated users."""
    templates, total = await list_templates(db, template_type=template_type, skip=skip, limit=limit)
    return PaginatedTemplateResponse(
        items=[_template_to_response(t) for t in templates],
        total=total,
        skip=skip,
        limit=limit,
    )


@router.get("/templates/{template_id}", response_model=TemplateResponse)
async def get_template_by_id(
    template_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(get_current_user_payload),
):
    """Get a single template. Available to all authenticated users."""
    template = await get_template(db, template_id)
    if not template:
        raise HTTPException(status_code=404, detail="Template not found")
    return _template_to_response(template)


@router.get("/templates/{template_id}/internal", response_model=TemplateResponse)
async def get_template_by_id_internal(
    template_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    x_internal_token: str = Header(...),
):
    """Get a single template. Internal service-to-service endpoint, guarded by token."""
    if not internal_token_matches(x_internal_token, settings.internal_api_token):
        raise HTTPException(status_code=403, detail="Invalid internal token")
    template = await get_template(db, template_id)
    if not template:
        raise HTTPException(status_code=404, detail="Template not found")
    return _template_to_response(template)


@router.post(
    "/templates",
    response_model=TemplateResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_new_template(
    body: TemplateCreate,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(require_admin),
):
    """Create a device template. Admin or superadmin only."""
    template = await create_template(db, body)
    logger.info(
        "Template created: %s",
        template.name,
        extra={"action": "template_create", "template_id": str(template.id)},
    )
    return _template_to_response(template)


@router.put("/templates/{template_id}", response_model=TemplateResponse)
async def update_template_by_id(
    template_id: uuid.UUID,
    body: TemplateUpdate,
    db: AsyncSession = Depends(get_db),
    payload: dict = Depends(require_admin),
):
    """Update a device template. Admin or superadmin only."""
    template = await update_template(db, template_id, body, modified_by=uuid.UUID(payload["sub"]))
    if not template:
        raise HTTPException(status_code=404, detail="Template not found")
    logger.info(
        "Template updated: %s",
        template_id,
        extra={"action": "template_update", "template_id": str(template_id)},
    )
    return _template_to_response(template)


@router.delete("/templates/{template_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_template_by_id(
    template_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(require_admin),
):
    """Delete a device template. Admin or superadmin only. Fails if devices reference it."""
    deleted = await delete_template(db, template_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Template not found")
    logger.info(
        "Template deleted: %s",
        template_id,
        extra={"action": "template_delete", "template_id": str(template_id)},
    )
