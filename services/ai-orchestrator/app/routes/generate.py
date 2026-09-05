import logging
import uuid

from fastapi import APIRouter, Depends, File, Form, Header, HTTPException, UploadFile, status
from herd_common.auth import make_auth_dependencies
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.schemas.generate import ExtractedFile, GenerateResponse
from app.services import usage_repo
from app.services.ai_client import (
    AI_NOT_CONFIGURED_DETAIL,
    AIClient,
    ai_is_configured,
    get_ai_client,
)
from app.services.extractor import ExtractionError, extract_files
from app.services.generator import GeneratorError, generate_topology
from app.services.inventory_client import InventorySummary, fetch_inventory_summary

logger = logging.getLogger(__name__)

get_current_user, _require_admin = make_auth_dependencies(
    secret_key=settings.secret_key,
    algorithm=settings.algorithm,
)

router = APIRouter(prefix="/generate", tags=["generate"])


async def _bearer_token(authorization: str = Header(...)) -> str:
    if not authorization.lower().startswith("bearer "):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Bearer token required")
    return authorization.split(" ", 1)[1]


async def _inventory_provider(
    token: str = Depends(_bearer_token),
) -> InventorySummary:
    try:
        return await fetch_inventory_summary(token)
    except Exception as e:
        logger.exception("inventory_fetch_failed")
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            f"Failed to fetch inventory summary: {e}",
        ) from e


_UPLOAD_READ_CHUNK_BYTES = 64 * 1024


async def _read_uploads(files: list[UploadFile] | None) -> list[ExtractedFile]:
    """Read the uploaded parts, enforcing the count and size caps before
    anything is fully buffered (issue #705).

    The count cap is checked before any part is read at all. Each part is
    then read in 64 KiB chunks with a running per-file total (against
    `upload_max_file_bytes`) and a running aggregate total across every part
    read so far in this request (against `upload_max_files *
    upload_max_file_bytes`); the read loop aborts the instant either is
    exceeded, so a malicious part is never read past the chunk that first
    crosses its cap. `UploadFile.size` (a client-reported Content-Length on
    the part, when present) is not trusted for enforcement: it is only ever a
    hint, since a caller controls it independently of the bytes actually
    sent. `extract_files` still re-checks the same caps on the fully read
    data as defense in depth; this function's job is only to stop an
    oversized or excessive upload from being buffered in memory in the first
    place.
    """
    if not files:
        return []
    if len(files) > settings.upload_max_files:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"Too many files: limit is {settings.upload_max_files}, got {len(files)}",
        )

    max_file_bytes = settings.upload_max_file_bytes
    max_aggregate_bytes = settings.upload_max_files * max_file_bytes
    aggregate_total = 0
    uploads: list[tuple[str, bytes]] = []

    for f in files:
        if not f.filename:
            continue
        chunks: list[bytes] = []
        file_total = 0
        while True:
            chunk = await f.read(_UPLOAD_READ_CHUNK_BYTES)
            if not chunk:
                break
            chunks.append(chunk)
            file_total += len(chunk)
            aggregate_total += len(chunk)
            if file_total > max_file_bytes:
                raise HTTPException(
                    status.HTTP_400_BAD_REQUEST,
                    f"File '{f.filename}' exceeds limit of {max_file_bytes} bytes",
                )
            if aggregate_total > max_aggregate_bytes:
                raise HTTPException(
                    status.HTTP_400_BAD_REQUEST,
                    f"Upload total exceeds limit of {max_aggregate_bytes} bytes",
                )
        if not chunks:
            continue
        uploads.append((f.filename, b"".join(chunks)))
    if not uploads:
        return []
    try:
        return extract_files(uploads)
    except ExtractionError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e)) from e


@router.post("", response_model=GenerateResponse)
async def generate(
    prompt: str = Form(..., min_length=1, max_length=20000),
    files: list[UploadFile] | None = File(None),
    user=Depends(get_current_user),
    token: str = Depends(_bearer_token),
    inventory: InventorySummary = Depends(_inventory_provider),
    ai: AIClient = Depends(get_ai_client),
    db: AsyncSession = Depends(get_db),
) -> GenerateResponse:
    if not ai_is_configured():
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            AI_NOT_CONFIGURED_DETAIL,
        )

    user_id = uuid.UUID(user["sub"])
    await usage_repo.enforce_quota(db, user_id)

    extracted = await _read_uploads(files)

    try:
        response, usage = await generate_topology(
            prompt=prompt,
            inventory=inventory,
            ai=ai,
            user_bearer_token=token,
            extracted_files=extracted,
        )
    except GeneratorError as e:
        raise HTTPException(e.status_code, e.message) from e

    await usage_repo.record_usage(
        db,
        user_id,
        usage,
        fallback_text=prompt + response.model_dump_json(),
    )

    logger.info(
        "ai_proposal_generated",
        extra={
            "device_count": len(response.devices),
            "edge_count": len(response.edges),
            "file_count": len(extracted),
        },
    )
    return response
