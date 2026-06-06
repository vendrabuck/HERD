import logging

from fastapi import APIRouter, Depends, Header, HTTPException, status
from herd_common.auth import make_auth_dependencies

from app.config import settings
from app.schemas.generate import CommitRequest, CommitResponse
from app.services.committer import CommitError, commit_proposal

logger = logging.getLogger(__name__)

get_current_user, _require_admin = make_auth_dependencies(
    secret_key=settings.secret_key,
    algorithm=settings.algorithm,
)

router = APIRouter(prefix="/commit", tags=["commit"])


async def _bearer_token(authorization: str = Header(...)) -> str:
    if not authorization.lower().startswith("bearer "):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Bearer token required")
    return authorization.split(" ", 1)[1]


@router.post("", response_model=CommitResponse)
async def commit(
    req: CommitRequest,
    user=Depends(get_current_user),
    token: str = Depends(_bearer_token),
) -> CommitResponse:
    user_id = str(user.get("sub") or user.get("id") or "")
    try:
        return await commit_proposal(req, token, user_id)
    except CommitError as e:
        raise HTTPException(e.status_code, e.message) from e
