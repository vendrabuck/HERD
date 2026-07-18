import logging
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, field_validator

from app.auth import create_session_token, require_config_session
from app.config_schema import CONFIG_SCHEMA
from app.config_store import (
    bootstrap_from_env,
    change_password,
    is_configured,
    is_password_changed,
    load_config,
    load_env_values,
    save_config,
    verify_password,
)
from app.docker_ctl import restart_services

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    bootstrap_from_env()
    yield


app = FastAPI(title="HERD Config Service", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# -- Schemas --


class LoginRequest(BaseModel):
    password: str


class ChangePasswordRequest(BaseModel):
    new_password: str

    @field_validator("new_password")
    @classmethod
    def validate_password_length(cls, v: str) -> str:
        if len(v) < 8:
            raise ValueError("Password must be at least 8 characters")
        if len(v) > 32:
            raise ValueError("Password must be at most 32 characters")
        return v


class SaveSettingsRequest(BaseModel):
    values: dict


# -- Endpoints --


@app.get("/health")
async def health():
    return {"status": "ok", "service": "config"}


@app.get("/status")
async def get_status():
    return {
        "configured": is_configured(),
        "password_changed": is_password_changed(),
    }


@app.post("/login")
async def login(req: LoginRequest):
    if not verify_password(req.password):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid password")
    token = create_session_token()
    return {
        "token": token,
        "password_changed": is_password_changed(),
    }


def require_password_rotated(_session: dict = Depends(require_config_session)) -> None:
    """Gate the powerful config-write surface until the seeded password is rotated.

    Layered on require_config_session: the caller is already authenticated, but a
    deploy seeded with an auto-generated (unrotated) password must change it
    before writing config or restarting the stack, so the surface never operates
    under a seeded credential (issue #256). An operator who set
    CONFIG_ADMIN_PASSWORD is already rotated and unaffected. Login, reads, and
    change-password stay open so the operator can clear the lock.
    """
    if not is_password_changed():
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "Change the config password before modifying or applying configuration",
        )


@app.post("/change-password")
async def change_password_endpoint(
    req: ChangePasswordRequest,
    _session: dict = Depends(require_config_session),
):
    change_password(req.new_password)
    return {"message": "Password changed"}


@app.get("/schema")
async def get_schema():
    return {"fields": CONFIG_SCHEMA}


@app.get("/settings")
async def get_settings(_session: dict = Depends(require_config_session)):
    # Layer env values under file values so deployer-supplied env vars show up
    # in the editor when config.json is missing the key; file wins on conflict.
    # This mirrors the runtime source order for a UI-saved file in
    # herd_common.config_loader.herd_settings_sources; keep the two in sync.
    merged = {**load_env_values(), **load_config()}
    secret_keys = {f["key"] for f in CONFIG_SCHEMA if f.get("secret")}
    redacted = {}
    for key, value in merged.items():
        if key in secret_keys and value:
            redacted[key] = "********"
        else:
            redacted[key] = value
    return {"values": redacted}


@app.put("/settings")
async def update_settings(
    req: SaveSettingsRequest,
    _rotated: None = Depends(require_password_rotated),
):
    # Merge: a "********" value means "unchanged". Resolve it to the existing
    # file value, else the env value the editor rendered it from, and never
    # write the placeholder itself: once saved, config.json outranks the
    # environment at runtime (herd_common.config_loader.herd_settings_sources),
    # so a literal "********" would become the live credential.
    existing = load_config()
    env_values = load_env_values()
    secret_keys = {f["key"] for f in CONFIG_SCHEMA if f.get("secret")}
    merged = dict(req.values)
    for key in secret_keys:
        if merged.get(key) == "********":
            if key in existing:
                merged[key] = existing[key]
            elif key in env_values:
                merged[key] = env_values[key]
            else:
                merged.pop(key, None)

    errors = save_config(merged)
    if errors:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, {"errors": errors})
    return {"message": "Configuration saved"}


@app.post("/apply")
async def apply_config(_rotated: None = Depends(require_password_rotated)):
    if not is_configured():
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "No configuration to apply")
    result = restart_services()
    return result
