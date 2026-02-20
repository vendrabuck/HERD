import uuid

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.dependencies.auth import get_current_user_payload
from app.schemas.reservation import ReservationCreate, ReservationResponse
from app.services.reservation_service import (
    cancel_reservation,
    create_reservation,
    get_reservation,
    list_user_reservations,
    release_reservation,
)

router = APIRouter(tags=["reservations"])
bearer_scheme = HTTPBearer()


@router.post("/", response_model=ReservationResponse, status_code=status.HTTP_201_CREATED)
async def create_new_reservation(
    body: ReservationCreate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    payload: dict = Depends(get_current_user_payload),
    credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme),
):
    user_id = uuid.UUID(payload["sub"])
    try:
        reservation = await create_reservation(db, body, user_id, credentials.credentials)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    except LookupError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    return reservation


@router.get("/", response_model=list[ReservationResponse])
async def get_my_reservations(
    db: AsyncSession = Depends(get_db),
    payload: dict = Depends(get_current_user_payload),
):
    user_id = uuid.UUID(payload["sub"])
    return await list_user_reservations(db, user_id)


@router.get("/{reservation_id}", response_model=ReservationResponse)
async def get_reservation_by_id(
    reservation_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    payload: dict = Depends(get_current_user_payload),
):
    user_id = uuid.UUID(payload["sub"])
    reservation = await get_reservation(db, reservation_id, user_id)
    if not reservation:
        raise HTTPException(status_code=404, detail="Reservation not found")
    return reservation


@router.delete("/{reservation_id}", status_code=status.HTTP_204_NO_CONTENT)
async def cancel_reservation_by_id(
    reservation_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    payload: dict = Depends(get_current_user_payload),
):
    user_id = uuid.UUID(payload["sub"])
    reservation = await cancel_reservation(db, reservation_id, user_id)
    if not reservation:
        raise HTTPException(status_code=404, detail="Reservation not found")


@router.put("/{reservation_id}/release", response_model=ReservationResponse)
async def release_reservation_early(
    reservation_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    payload: dict = Depends(get_current_user_payload),
):
    user_id = uuid.UUID(payload["sub"])
    reservation = await release_reservation(db, reservation_id, user_id)
    if not reservation:
        raise HTTPException(status_code=404, detail="Reservation not found")
    return reservation
