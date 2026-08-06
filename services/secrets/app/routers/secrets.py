"""User-facing and admin CRUD for secrets (issue #39, ADR 0003).

Admins manage secrets; non-admin visibility rides the ACL service's `secret`
resource type. Reveal (plaintext) is gated on the `manage` permission; `view`
sees metadata only. Denials follow the inventory precedent: a caller with no
grant at all gets 404 (existence not confirmed), a caller with view but not
manage gets 403 on reveal.

Plaintext appears only in SecretValueResponse bodies. It is never logged
(RequestLoggingMiddleware logs method/path/status only) and never included in
metadata responses; tests pin both properties.
"""

import logging
import uuid

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response, status
from herd_common.acl import user_has_grant
from herd_common.auth import make_auth_dependencies
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.models import Secret
from app.schemas.secret import (
    RotateResponse,
    SecretCreate,
    SecretResponse,
    SecretUpdate,
    SecretValueResponse,
)
from app.services import crypto
from app.services.inventory_guard import find_hypervisors_referencing_secret
from app.services.keyring import deserialize_data, rotate_dek, serialize_data

logger = logging.getLogger(__name__)

get_current_user_payload, require_admin = make_auth_dependencies(
    secret_key=settings.secret_key,
    algorithm=settings.algorithm,
)

router = APIRouter(prefix="/secrets", tags=["secrets"])
keys_router = APIRouter(prefix="/keys", tags=["secrets-keys"])

_ADMIN_ROLES = {"admin", "superadmin"}
_ACL_HTTP_TIMEOUT_SECONDS = 5.0


def _is_admin(payload: dict) -> bool:
    return payload.get("role") in _ADMIN_ROLES


def _principal_id(payload: dict) -> uuid.UUID | None:
    sub = payload.get("sub")
    try:
        return uuid.UUID(str(sub)) if sub else None
    except (ValueError, TypeError):
        return None


async def _granted_secret_ids(user_id: str, permission: str, authorization: str) -> set[uuid.UUID]:
    """IDs of secrets the user holds `permission` on, via ACL /resources.

    Closed-by-default like herd_common.acl: any failure returns the empty set.
    """
    url = f"{settings.acl_service_url.rstrip('/')}/resources"
    params = {"user_id": user_id, "resource_type": "secret", "permission": permission}
    headers = {"Authorization": authorization}
    try:
        async with httpx.AsyncClient(timeout=_ACL_HTTP_TIMEOUT_SECONDS) as client:
            resp = await client.get(url, params=params, headers=headers)
    except httpx.HTTPError as exc:
        logger.info("acl_resources_unreachable", extra={"error": str(exc)})
        return set()
    if resp.status_code != 200:
        return set()
    try:
        return {uuid.UUID(item["resource_id"]) for item in resp.json()}
    except (ValueError, KeyError, TypeError):
        return set()


async def _has_grant(
    payload: dict, secret_id: uuid.UUID, permission: str, authorization: str
) -> bool:
    return await user_has_grant(
        user_id=str(payload.get("sub")),
        resource_type="secret",
        resource_id=str(secret_id),
        permission=permission,
        authorization=authorization,
        acl_service_url=settings.acl_service_url,
    )


def _encrypt_into(secret: Secret, data: dict, request: Request) -> None:
    keyring = request.app.state.keyring
    secret.key_version = keyring.active_version
    secret.nonce, secret.ciphertext = crypto.encrypt_value(
        keyring.active_dek,
        serialize_data(data),
        secret_id=secret.id,
        key_version=secret.key_version,
    )


def _decrypt(secret: Secret, request: Request) -> dict:
    keyring = request.app.state.keyring
    plaintext = crypto.decrypt_value(
        keyring.dek(secret.key_version),
        secret.nonce,
        secret.ciphertext,
        secret_id=secret.id,
        key_version=secret.key_version,
    )
    return deserialize_data(plaintext)


@router.post("", response_model=SecretResponse, status_code=status.HTTP_201_CREATED)
async def create_secret(
    body: SecretCreate,
    request: Request,
    payload: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    secret = Secret(
        id=uuid.uuid4(),
        name=body.name,
        type=body.type,
        description=body.description,
        created_by=_principal_id(payload),
        updated_by=_principal_id(payload),
    )
    _encrypt_into(secret, body.data, request)
    db.add(secret)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(status_code=409, detail="A secret with this name already exists")
    await db.refresh(secret)
    return SecretResponse.model_validate(secret)


@router.get("", response_model=list[SecretResponse])
async def list_secrets(
    payload: dict = Depends(get_current_user_payload),
    db: AsyncSession = Depends(get_db),
    authorization: str = Header(default=""),
):
    rows = (await db.execute(select(Secret).order_by(Secret.name))).scalars().all()
    if _is_admin(payload):
        return [SecretResponse.model_validate(r) for r in rows]
    user_id = str(payload.get("sub"))
    visible = await _granted_secret_ids(user_id, "view", authorization)
    visible |= await _granted_secret_ids(user_id, "manage", authorization)
    return [SecretResponse.model_validate(r) for r in rows if r.id in visible]


@router.get("/{secret_id}", response_model=SecretResponse)
async def get_secret(
    secret_id: uuid.UUID,
    payload: dict = Depends(get_current_user_payload),
    db: AsyncSession = Depends(get_db),
    authorization: str = Header(default=""),
):
    secret = await db.get(Secret, secret_id)
    if secret is None:
        raise HTTPException(status_code=404, detail="Secret not found")
    if not _is_admin(payload):
        if not (
            await _has_grant(payload, secret_id, "view", authorization)
            or await _has_grant(payload, secret_id, "manage", authorization)
        ):
            raise HTTPException(status_code=404, detail="Secret not found")
    return SecretResponse.model_validate(secret)


@router.get("/{secret_id}/value", response_model=SecretValueResponse)
async def reveal_secret(
    secret_id: uuid.UUID,
    request: Request,
    payload: dict = Depends(get_current_user_payload),
    db: AsyncSession = Depends(get_db),
    authorization: str = Header(default=""),
):
    """Plaintext reveal. `manage` grant or admin (ADR 0003 decision point 1)."""
    secret = await db.get(Secret, secret_id)
    if secret is None:
        raise HTTPException(status_code=404, detail="Secret not found")
    if not _is_admin(payload):
        if not await _has_grant(payload, secret_id, "manage", authorization):
            # view-only callers know the secret exists; the gap is permission.
            if await _has_grant(payload, secret_id, "view", authorization):
                raise HTTPException(status_code=403, detail="manage permission required")
            raise HTTPException(status_code=404, detail="Secret not found")
    return SecretValueResponse(id=secret.id, name=secret.name, data=_decrypt(secret, request))


@router.put("/{secret_id}", response_model=SecretResponse)
async def update_secret(
    secret_id: uuid.UUID,
    body: SecretUpdate,
    request: Request,
    payload: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    secret = await db.get(Secret, secret_id)
    if secret is None:
        raise HTTPException(status_code=404, detail="Secret not found")
    if body.type is not None:
        secret.type = body.type
    if body.description is not None:
        secret.description = body.description
    if body.data is not None:
        _encrypt_into(secret, body.data, request)
    secret.updated_by = _principal_id(payload)
    await db.commit()
    await db.refresh(secret)
    return SecretResponse.model_validate(secret)


@router.delete("/{secret_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_secret(
    secret_id: uuid.UUID,
    _payload: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Delete a secret. Admin or superadmin only.

    Issue #456: refused with 409 while any hypervisor still references the
    secret (inventory reverse lookup, fail-closed 503 when inventory is
    unreachable, no force flag; see inventory_guard's module docstring). The
    409 names the referencing hypervisors so the fix is actionable: re-point
    or delete them first.
    """
    secret = await db.get(Secret, secret_id)
    if secret is None:
        raise HTTPException(status_code=404, detail="Secret not found")
    referencing = await find_hypervisors_referencing_secret(secret_id)
    if referencing:
        raise HTTPException(
            status_code=409,
            detail={
                "error": "secret_in_use",
                "hypervisor_ids": [str(h.get("id")) for h in referencing],
                "hypervisor_names": [str(h.get("name")) for h in referencing],
            },
        )
    await db.delete(secret)
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@keys_router.post("/rotate", response_model=RotateResponse)
async def rotate_keys(
    request: Request,
    _payload: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Introduce a new DEK version and re-encrypt every secret to it."""
    result = await rotate_dek(db, request.app.state.keyring)
    return RotateResponse(**result)
