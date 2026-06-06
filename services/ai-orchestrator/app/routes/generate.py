import logging

from fastapi import APIRouter, Depends, File, Form, Header, HTTPException, UploadFile, status
from herd_common.auth import make_auth_dependencies

from app.config import settings
from app.schemas.generate import ExtractedFile, GenerateResponse
from app.services.ai_client import AIClient, ai_is_configured, get_ai_client
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


async def _read_uploads(files: list[UploadFile] | None) -> list[ExtractedFile]:
    if not files:
        return []
    uploads: list[tuple[str, bytes]] = []
    for f in files:
        if not f.filename:
            continue
        data = await f.read()
        if not data:
            continue
        uploads.append((f.filename, data))
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
    _user=Depends(get_current_user),
    token: str = Depends(_bearer_token),
    inventory: InventorySummary = Depends(_inventory_provider),
    ai: AIClient = Depends(get_ai_client),
) -> GenerateResponse:
    if not ai_is_configured():
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "AI orchestrator is not configured",
        )

    extracted = await _read_uploads(files)

    try:
        response = await generate_topology(
            prompt=prompt,
            inventory=inventory,
            ai=ai,
            user_bearer_token=token,
            extracted_files=extracted,
        )
    except GeneratorError as e:
        raise HTTPException(e.status_code, e.message) from e

    logger.info(
        "ai_proposal_generated",
        extra={
            "device_count": len(response.devices),
            "edge_count": len(response.edges),
            "file_count": len(extracted),
        },
    )
    return response
