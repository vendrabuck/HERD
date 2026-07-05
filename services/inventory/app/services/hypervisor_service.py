import logging
import uuid

import httpx
from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.hypervisor import Hypervisor
from app.models.template import DeviceTemplate
from app.schemas.hypervisor import HypervisorCreate, HypervisorUpdate

logger = logging.getLogger(__name__)


async def validate_secret_exists(secret_id: uuid.UUID) -> None:
    """Confirm the secret exists in the secrets service, over the internal API.

    A 404 means the referenced secret does not exist and the registration is
    rejected with 422. A transport error or any non-2xx/404 (including 5xx) is
    an upstream-unreachable condition and standardizes to 503. The returned
    plaintext is never read, stored, or logged; only the existence check
    (status code) matters here. The secret UUID is also kept out of log lines
    deliberately: nothing secret-named reaches a logger, so scanners cannot
    mistake the reference for credential material.
    """
    url = f"{settings.secrets_service_url}/internal/secrets/{secret_id}/value"
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                url,
                headers={"X-Internal-Token": settings.internal_api_token},
                timeout=10.0,
            )
    except httpx.HTTPError as exc:
        logger.error("secrets service unreachable while validating a secret reference: %s", exc)
        raise HTTPException(
            status_code=503, detail="secrets service unreachable while validating secret"
        ) from exc

    if resp.status_code == 404:
        raise HTTPException(status_code=422, detail="Secret does not exist")
    if resp.status_code != 200:
        logger.error(
            "secrets service returned %s while validating a secret reference", resp.status_code
        )
        raise HTTPException(
            status_code=503,
            detail=f"secrets service returned {resp.status_code} while validating secret",
        )


async def list_hypervisors(
    db: AsyncSession, skip: int = 0, limit: int = 50
) -> tuple[list[Hypervisor], int]:
    query = select(Hypervisor).order_by(Hypervisor.created_at.desc())
    count_query = select(func.count()).select_from(query.subquery())
    total = (await db.execute(count_query)).scalar() or 0
    query = query.offset(skip).limit(limit)
    result = await db.execute(query)
    return list(result.scalars().all()), total


async def get_hypervisor(db: AsyncSession, hypervisor_id: uuid.UUID) -> Hypervisor | None:
    result = await db.execute(select(Hypervisor).where(Hypervisor.id == hypervisor_id))
    return result.scalar_one_or_none()


async def create_hypervisor(
    db: AsyncSession, data: HypervisorCreate, modified_by: uuid.UUID | None = None
) -> Hypervisor:
    await validate_secret_exists(data.secret_id)
    hypervisor = Hypervisor(
        name=data.name,
        description=data.description,
        endpoint=data.endpoint,
        hypervisor_type=data.hypervisor_type,
        secret_id=data.secret_id,
        enabled=data.enabled,
        modified_by=modified_by,
    )
    db.add(hypervisor)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(
            status_code=409,
            detail=f"Hypervisor with name '{data.name}' already exists",
        )
    await db.refresh(hypervisor)
    return hypervisor


async def update_hypervisor(
    db: AsyncSession,
    hypervisor_id: uuid.UUID,
    data: HypervisorUpdate,
    modified_by: uuid.UUID | None = None,
) -> Hypervisor | None:
    hypervisor = await get_hypervisor(db, hypervisor_id)
    if not hypervisor:
        return None
    update_data = data.model_dump(exclude_unset=True)
    # Re-validate the secret only when secret_id actually changes.
    if "secret_id" in update_data and update_data["secret_id"] != hypervisor.secret_id:
        await validate_secret_exists(update_data["secret_id"])
    if modified_by is not None:
        hypervisor.modified_by = modified_by
    conflict_name = update_data.get("name", hypervisor.name)
    for field, value in update_data.items():
        setattr(hypervisor, field, value)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(
            status_code=409,
            detail=f"Hypervisor with name '{conflict_name}' already exists",
        )
    await db.refresh(hypervisor)
    return hypervisor


async def delete_hypervisor(db: AsyncSession, hypervisor_id: uuid.UUID) -> bool:
    hypervisor = await get_hypervisor(db, hypervisor_id)
    if not hypervisor:
        return False
    # Block deletion while any template still references this hypervisor, mirroring
    # the template-delete-block on referencing devices/ports.
    count_result = await db.execute(
        select(func.count())
        .select_from(DeviceTemplate)
        .where(DeviceTemplate.hypervisor_id == hypervisor_id)
    )
    template_count = count_result.scalar()
    if template_count and template_count > 0:
        raise HTTPException(
            status_code=409,
            detail="Cannot delete hypervisor: templates still reference it",
        )
    await db.delete(hypervisor)
    await db.commit()
    return True
