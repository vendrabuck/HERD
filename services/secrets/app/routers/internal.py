"""Service-to-service plaintext retrieval (issue #39, ADR 0003).

The X-Internal-Token surface that automated provisioning (#32) will consume:
no acting user, 403 on a missing or wrong token, matching the repo-wide
internal-endpoint convention. Lookup is by id or by name; recipes are expected
to reference secrets by stable name.
"""

import uuid

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from herd_common.internal_auth import internal_token_matches
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.models import Secret
from app.schemas.secret import SecretValueResponse
from app.services import crypto
from app.services.keyring import (
    KeyVersionUnavailableError,
    deserialize_data,
    key_unavailable_http,
)

router = APIRouter(prefix="/internal/secrets", tags=["secrets-internal"])


def _check_internal_token(token: str) -> None:
    if not internal_token_matches(token, settings.internal_api_token):
        raise HTTPException(status_code=403, detail="Invalid internal token")


async def _reveal(secret: Secret, request: Request, db: AsyncSession) -> SecretValueResponse:
    keyring = request.app.state.keyring
    try:
        # A version another replica's rotation created is loaded on first use
        # (issue #1085); it used to be a KeyError and an unhandled 500.
        dek = await keyring.load_dek(db, secret.key_version)
    except KeyVersionUnavailableError as exc:
        raise key_unavailable_http(exc) from None
    plaintext = crypto.decrypt_value(
        dek,
        secret.nonce,
        secret.ciphertext,
        secret_id=secret.id,
        key_version=secret.key_version,
    )
    return SecretValueResponse(id=secret.id, name=secret.name, data=deserialize_data(plaintext))


@router.get("/{secret_id}/value", response_model=SecretValueResponse)
async def internal_reveal_by_id(
    secret_id: uuid.UUID,
    request: Request,
    x_internal_token: str = Header(default=""),
    db: AsyncSession = Depends(get_db),
):
    _check_internal_token(x_internal_token)
    secret = await db.get(Secret, secret_id)
    if secret is None:
        raise HTTPException(status_code=404, detail="Secret not found")
    return await _reveal(secret, request, db)


@router.get("/by-name/{name}/value", response_model=SecretValueResponse)
async def internal_reveal_by_name(
    name: str,
    request: Request,
    x_internal_token: str = Header(default=""),
    db: AsyncSession = Depends(get_db),
):
    _check_internal_token(x_internal_token)
    secret = (await db.execute(select(Secret).where(Secret.name == name))).scalar_one_or_none()
    if secret is None:
        raise HTTPException(status_code=404, detail="Secret not found")
    return await _reveal(secret, request, db)
