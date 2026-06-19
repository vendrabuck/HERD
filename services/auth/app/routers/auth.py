from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.dependencies.auth import get_current_user
from app.models.user import User
from app.schemas.auth import (
    LoginRequest,
    LogoutRequest,
    RefreshRequest,
    RegisterRequest,
    TokenResponse,
    UserResponse,
)
from app.services.auth_service import (
    authenticate_user,
    create_tokens_for_user,
    create_user,
    get_user_by_email,
    get_user_by_username,
    revoke_refresh_token,
    rotate_refresh_token,
)

router = APIRouter(tags=["auth"])


@router.post("/register", response_model=UserResponse, status_code=status.HTTP_201_CREATED)
async def register(body: RegisterRequest, db: AsyncSession = Depends(get_db)):
    if settings.auth_method == "ldap":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Local registration is disabled; this deployment uses LDAP authentication.",
        )
    # Single generic 409 for any collision (email OR username, pre-check OR the
    # IntegrityError race below). Distinct messages let an unauthenticated caller
    # enumerate which emails and usernames already exist by reading which one
    # comes back. Keep this wording identical across all three paths.
    if await get_user_by_email(db, body.email) or await get_user_by_username(db, body.username):
        raise HTTPException(status_code=409, detail="Email or username already exists")
    try:
        user = await create_user(db, body.email, body.username, body.password)
    except IntegrityError:
        raise HTTPException(status_code=409, detail="Email or username already exists")
    return user


@router.post("/login", response_model=TokenResponse)
async def login(body: LoginRequest, db: AsyncSession = Depends(get_db)):
    user = await authenticate_user(db, body.email, body.password)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials",
            headers={"WWW-Authenticate": "Bearer"},
        )
    access_token, refresh_token = await create_tokens_for_user(db, user)
    return TokenResponse(access_token=access_token, refresh_token=refresh_token)


@router.post("/refresh", response_model=TokenResponse)
async def refresh(body: RefreshRequest, db: AsyncSession = Depends(get_db)):
    result = await rotate_refresh_token(db, body.refresh_token)
    if not result:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired refresh token",
        )
    access_token, new_refresh_token = result
    return TokenResponse(access_token=access_token, refresh_token=new_refresh_token)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(body: LogoutRequest, db: AsyncSession = Depends(get_db)):
    await revoke_refresh_token(db, body.refresh_token)


@router.get("/me", response_model=UserResponse)
async def me(current_user: User = Depends(get_current_user)):
    return current_user
