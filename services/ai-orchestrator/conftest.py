"""Test-runner env: forced BEFORE any app imports so pydantic-settings reads
sqlite + empty schema (sqlite has no native schema support; the model's
schema=settings.db_schema becomes None and tables land in the default
namespace). Mirrors the pattern in services/reservations/conftest.py.
"""

import os

os.environ["DB_SCHEMA"] = ""
os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///:memory:"
os.environ["SECRET_KEY"] = "3fcbb4bf16b0e162be24d2eb59065316c1e84194669ebf9270203280b18e18f7"
