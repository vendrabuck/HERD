"""Shared CORS middleware registration for downstream services.

Usage in a service's `app/main.py`:
    from herd_common.cors import add_cors_middleware

    add_cors_middleware(app, settings.cors_origins)

`services/config` is the deliberate exception: it serves the pre-auth bootstrap UI
before `cors_origins` can be configured, so it keeps its own hand-written
`allow_origins=["*"]` block instead of calling this helper.
"""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware


def add_cors_middleware(app: FastAPI, cors_origins: str) -> None:
    """Register CORSMiddleware with the workspace-standard origin/credentials policy."""
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[o.strip() for o in cors_origins.split(",") if o.strip()],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
