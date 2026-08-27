from app.schemas._types import UUIDStr
from pydantic import BaseModel


class FabricResponse(BaseModel):
    device_id: UUIDStr
    fabric_id: UUIDStr
    component_size: int
