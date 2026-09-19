"""GET /version for the config service (issue #846), without herd_common.

Every other service registers this route through herd_common.version. The
config service cannot: it is deliberately standalone, its pyproject.toml has no
herd-common dependency and its image never copies that package in, so an import
of herd_common here fails at boot with ModuleNotFoundError. This module is the
same contract in stdlib plus FastAPI alone. tests/test_version.py pins its
response fields to herd_common.version.VersionResponse on the host, where both
are importable, so the two copies cannot drift, and pins that nothing under
app/ imports herd_common at all.
"""

import importlib.metadata
import os

from fastapi import FastAPI
from pydantic import BaseModel

DISTRIBUTION = "herd-config"


class VersionResponse(BaseModel):
    service: str
    version: str
    build: str
    build_date: str | None = None


def service_version() -> str:
    """The installed herd-config version, or "0+unknown"; never raises, because
    a version lookup must not be able to stop the service booting."""
    try:
        return importlib.metadata.version(DISTRIBUTION)
    except importlib.metadata.PackageNotFoundError:
        return "0+unknown"


def add_version_route(app: FastAPI) -> None:
    """Register GET /version, unauthenticated exactly like /health. The build
    environment is read at call time, and an empty string counts as unset."""

    @app.get("/version", response_model=VersionResponse)
    async def version() -> dict:
        return {
            "service": "config",
            "version": service_version(),
            "build": os.environ.get("HERD_BUILD") or "dev",
            "build_date": os.environ.get("HERD_BUILD_DATE") or None,
        }
