"""Product version and build metadata for every service (issue #846).

One source of truth: `service_version` reads a service's own installed
distribution version straight from its `pyproject.toml` via
`importlib.metadata`, so a release bump only touches the pyprojects (and
`frontend/package.json` for the frontend) and every `/docs` page and
`/version` response follows without a second edit.

The build identifier is a second, independent axis: `HERD_BUILD` (a
`git describe` string, e.g. `v0.5.0-16-gb29c8812`) and `HERD_BUILD_DATE` (an
ISO 8601 UTC timestamp of the HEAD commit) reach every service container as
environment variables, set by the Makefile's build targets. Neither is read
at import time, so a test can monkeypatch the environment without reloading
this module.

Usage in a service's `app/main.py`:

    from herd_common.version import add_version_route, service_version

    app = FastAPI(..., version=service_version("herd-<svc>"))
    add_version_route(app, service="<svc>", distribution="herd-<svc>")
"""

import importlib.metadata
import os

from fastapi import FastAPI
from pydantic import BaseModel


def service_version(distribution: str) -> str:
    """Return `distribution`'s installed version, or "0+unknown" if unresolvable.

    A version lookup must never be able to stop a service booting: an
    editable install missing its metadata, or a mistyped distribution name,
    falls back to "0+unknown" instead of raising PackageNotFoundError.
    """
    try:
        return importlib.metadata.version(distribution)
    except importlib.metadata.PackageNotFoundError:
        return "0+unknown"


def build_info() -> dict:
    """Read HERD_BUILD and HERD_BUILD_DATE from the environment at call time.

    An unset or empty-string HERD_BUILD reports as "dev"; an unset or
    empty-string HERD_BUILD_DATE reports as None.
    """
    return {
        "build": os.environ.get("HERD_BUILD") or "dev",
        "build_date": os.environ.get("HERD_BUILD_DATE") or None,
    }


def version_payload(service: str, distribution: str) -> dict:
    """Build the /version response body for `service`."""
    info = build_info()
    return {
        "service": service,
        "version": service_version(distribution),
        "build": info["build"],
        "build_date": info["build_date"],
    }


class VersionResponse(BaseModel):
    service: str
    version: str
    build: str
    build_date: str | None = None


def add_version_route(
    app: FastAPI,
    *,
    service: str,
    distribution: str,
    include_in_schema: bool = True,
) -> None:
    """Register GET /version on `app`, unauthenticated exactly like /health.

    `include_in_schema=False` is for the integration service only: its
    OpenAPI document is the published `/api/v1` facade contract, and product
    build info must not enter that contract even though the route still
    answers.
    """

    @app.get("/version", response_model=VersionResponse, include_in_schema=include_in_schema)
    async def version() -> dict:
        return version_payload(service, distribution)
