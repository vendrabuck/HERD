"""
Cabling Service

Manages the physical and virtual backend connections between devices within a topology.
These connections are made by administrators and are not visible to end-users as raw
cable data; the frontend topology editor reflects them, but users cannot create or
delete cabling entries directly.

Current state: in-memory store (no database persistence).
A full implementation would use a cabling schema in PostgreSQL with proper ORM models.
"""
import uuid
from datetime import datetime, timezone

from fastapi import Depends, FastAPI, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from app.config import settings
from app.dependencies import get_current_user_payload, require_admin

app = FastAPI(
    title="HERD Cabling Service",
    description="Backend connection management between lab devices",
    version="0.1.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in settings.cors_origins.split(",") if o.strip()],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# In-memory store; replaced by a database in a full implementation
_connections: dict[str, dict] = {}


class ConnectionCreate(BaseModel):
    device_a_id: uuid.UUID
    port_a: str
    device_b_id: uuid.UUID
    port_b: str
    connection_type: str = "ethernet"
    notes: str | None = None


class ConnectionResponse(BaseModel):
    id: str
    device_a_id: uuid.UUID
    port_a: str
    device_b_id: uuid.UUID
    port_b: str
    connection_type: str
    notes: str | None
    created_by: str
    created_at: str


@app.get("/health")
async def health():
    return {"status": "ok", "service": "cabling"}


@app.get("/connections", response_model=list[ConnectionResponse])
async def list_connections(
    _: dict = Depends(get_current_user_payload),
):
    """List all backend connections. Available to all authenticated users (read-only)."""
    return list(_connections.values())


@app.get("/connections/{connection_id}", response_model=ConnectionResponse)
async def get_connection(
    connection_id: str,
    _: dict = Depends(get_current_user_payload),
):
    """Get a single backend connection. Available to all authenticated users."""
    conn = _connections.get(connection_id)
    if not conn:
        raise HTTPException(status_code=404, detail="Connection not found")
    return conn


@app.post("/connections", response_model=ConnectionResponse, status_code=status.HTTP_201_CREATED)
async def create_connection(
    body: ConnectionCreate,
    payload: dict = Depends(require_admin),
):
    """
    Create a backend connection between two device ports. Admin or superadmin only.
    This represents a physical cable or virtual link that end-users do not configure directly.
    """
    conn_id = str(uuid.uuid4())
    conn = {
        "id": conn_id,
        "device_a_id": body.device_a_id,
        "port_a": body.port_a,
        "device_b_id": body.device_b_id,
        "port_b": body.port_b,
        "connection_type": body.connection_type,
        "notes": body.notes,
        "created_by": payload.get("username", "unknown"),
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    _connections[conn_id] = conn
    return conn


@app.delete("/connections/{connection_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_connection(
    connection_id: str,
    _: dict = Depends(require_admin),
):
    """Remove a backend connection. Admin or superadmin only."""
    if connection_id not in _connections:
        raise HTTPException(status_code=404, detail="Connection not found")
    del _connections[connection_id]
