"""Test-runner env: forced BEFORE any app imports so pydantic-settings reads
sqlite + empty schema (sqlite has no native schema support; the model's
schema=settings.db_schema becomes None and tables land in the default
namespace). Mirrors the pattern in services/reservations/conftest.py.
"""

import os

os.environ["DB_SCHEMA"] = ""
os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///:memory:"
os.environ["SECRET_KEY"] = "test-secret"
