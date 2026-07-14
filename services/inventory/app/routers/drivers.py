import logging
import uuid

from fastapi import APIRouter, Depends, Form, Header, HTTPException, Query, UploadFile, status
from fastapi.responses import Response
from herd_common.device_config import CONFIG_SCHEMAS
from herd_common.internal_auth import internal_token_matches
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.dependencies.auth import get_current_user_payload, require_admin
from app.schemas.driver_package import DriverPackageInfo, DriverUpdate, PaginatedDriverResponse
from app.services.driver_service import (
    create_driver,
    delete_driver,
    download_driver,
    get_driver,
    list_drivers,
    replace_driver_file,
    update_driver,
)
from app.services.published_schema import published_schema_for_driver

logger = logging.getLogger(__name__)

router = APIRouter(tags=["drivers"])


@router.get("/drivers", response_model=PaginatedDriverResponse)
async def get_drivers(
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(get_current_user_payload),
):
    """List all driver packages. Available to all authenticated users."""
    drivers, total = await list_drivers(db, skip=skip, limit=limit)
    return PaginatedDriverResponse(
        items=drivers,
        total=total,
        skip=skip,
        limit=limit,
    )


@router.get("/drivers/{driver_id}", response_model=DriverPackageInfo)
async def get_driver_by_id(
    driver_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(get_current_user_payload),
):
    """Get driver package metadata. Available to all authenticated users."""
    return await get_driver(db, driver_id)


@router.get("/drivers/{driver_id}/config-schema")
async def get_driver_config_schema_proxy(
    driver_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(get_current_user_payload),
) -> dict:
    """Return the schema used to validate this driver's device configs.

    Prefers the driver-published config schema (proxied from execution); falls
    back to the hardcoded registry entry for the driver's connection_type when
    the driver publishes nothing or execution is unreachable (fail-open). Drives
    the config-editor UI. Available to all authenticated users; the validation
    write path uses the same resolver, not this HTTP route.
    """
    package = await get_driver(db, driver_id)
    published = await published_schema_for_driver(package)
    if published is not None:
        return {
            "driver_id": str(driver_id),
            "connection_type": package.connection_type,
            "schema": published,
            "source": "driver",
        }
    registry = CONFIG_SCHEMAS.get(package.connection_type)
    return {
        "driver_id": str(driver_id),
        "connection_type": package.connection_type,
        "schema": registry,
        "source": "registry" if registry is not None else "none",
    }


@router.post(
    "/drivers",
    response_model=DriverPackageInfo,
    status_code=status.HTTP_201_CREATED,
)
async def create_new_driver(
    file: UploadFile,
    name: str = Form(...),
    description: str | None = Form(None),
    connection_type: str = Form(...),
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_admin),
):
    """Upload a new driver package. Admin only."""
    file_data = await file.read()
    package = await create_driver(
        db,
        name,
        description,
        connection_type,
        file.filename or "driver.zip",
        file_data,
        user.get("username", "unknown"),
        settings.driver_max_size_bytes,
    )
    logger.info(
        "Driver created: %s",
        package.name,
        extra={"action": "driver_create", "driver_id": str(package.id)},
    )
    return package


@router.put("/drivers/{driver_id}", response_model=DriverPackageInfo)
async def update_driver_metadata(
    driver_id: uuid.UUID,
    body: DriverUpdate,
    db: AsyncSession = Depends(get_db),
    payload: dict = Depends(require_admin),
):
    """Update driver metadata. Admin only."""
    package = await update_driver(
        db,
        driver_id,
        body.name,
        body.description,
        body.connection_type,
        modified_by=uuid.UUID(payload["sub"]),
    )
    logger.info(
        "Driver updated: %s",
        driver_id,
        extra={"action": "driver_update", "driver_id": str(driver_id)},
    )
    return package


@router.put("/drivers/{driver_id}/file", response_model=DriverPackageInfo)
async def replace_driver_file_endpoint(
    driver_id: uuid.UUID,
    file: UploadFile,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_admin),
):
    """Replace a driver package file. Admin only."""
    file_data = await file.read()
    package = await replace_driver_file(
        db,
        driver_id,
        file.filename or "driver.zip",
        file_data,
        user.get("username", "unknown"),
        settings.driver_max_size_bytes,
    )
    logger.info(
        "Driver file replaced: %s",
        driver_id,
        extra={"action": "driver_file_replace", "driver_id": str(driver_id)},
    )
    return package


@router.get("/drivers/{driver_id}/download")
async def download_driver_file(
    driver_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(get_current_user_payload),
):
    """Download a driver package file. Available to all authenticated users."""
    package, data = await download_driver(db, driver_id)
    content_type = "application/zip" if package.filename.endswith(".zip") else "application/gzip"
    return Response(
        content=data,
        media_type=content_type,
        headers={"Content-Disposition": f'attachment; filename="{package.filename}"'},
    )


@router.get("/drivers/{driver_id}/internal-download")
async def download_driver_file_internal(
    driver_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    x_internal_token: str = Header(...),
):
    """Download a driver package file. Internal service-to-service endpoint."""
    if not internal_token_matches(x_internal_token, settings.internal_api_token):
        raise HTTPException(status_code=403, detail="Invalid internal token")
    package, data = await download_driver(db, driver_id)
    content_type = "application/zip" if package.filename.endswith(".zip") else "application/gzip"
    return Response(
        content=data,
        media_type=content_type,
        headers={"Content-Disposition": f'attachment; filename="{package.filename}"'},
    )


@router.delete(
    "/drivers/{driver_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_driver_by_id(
    driver_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(require_admin),
):
    """Delete a driver package. Admin only. Fails if templates reference it."""
    await delete_driver(db, driver_id)
    logger.info(
        "Driver deleted: %s",
        driver_id,
        extra={"action": "driver_delete", "driver_id": str(driver_id)},
    )
