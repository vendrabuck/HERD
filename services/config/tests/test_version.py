"""Unit test for GET /version (issue #846).

config is the one service that previously passed no version= at all, and the
one service that implements this route WITHOUT herd_common: its image has never
contained that package (no dependency in pyproject.toml, no COPY in the
Dockerfile), so app/version.py is a standalone copy of the contract. The last
two tests here are what keep that safe: one pins the copy's fields to
herd_common's, the other fails if anything under app/ imports herd_common.
"""

import ast
import importlib.metadata
from pathlib import Path

import pytest
from app.main import app
from httpx import ASGITransport, AsyncClient

DISTRIBUTION = "herd-config"


@pytest.fixture
def async_client():
    transport = ASGITransport(app=app)
    return AsyncClient(transport=transport, base_url="http://test")


@pytest.mark.asyncio
async def test_version(async_client):
    async with async_client as client:
        resp = await client.get("/version")
    assert resp.status_code == 200
    data = resp.json()
    assert data["service"] == "config"
    assert data["version"] == importlib.metadata.version(DISTRIBUTION)


@pytest.mark.asyncio
async def test_version_build_unset_is_dev(async_client, monkeypatch):
    monkeypatch.delenv("HERD_BUILD", raising=False)
    monkeypatch.delenv("HERD_BUILD_DATE", raising=False)
    async with async_client as client:
        resp = await client.get("/version")
    data = resp.json()
    assert data["build"] == "dev"
    assert data["build_date"] is None


@pytest.mark.asyncio
async def test_version_build_set(async_client, monkeypatch):
    monkeypatch.setenv("HERD_BUILD", "v0.5.0-16-gb29c8812")
    monkeypatch.setenv("HERD_BUILD_DATE", "2026-09-15T00:00:00Z")
    async with async_client as client:
        resp = await client.get("/version")
    data = resp.json()
    assert data["build"] == "v0.5.0-16-gb29c8812"
    assert data["build_date"] == "2026-09-15T00:00:00Z"


def test_openapi_info_version_matches_distribution():
    assert app.openapi()["info"]["version"] == importlib.metadata.version(DISTRIBUTION)


def test_response_fields_match_the_shared_contract():
    """app/version.py is a standalone copy of herd_common.version's contract.
    On the host both are importable, so pin the copy's fields and defaults to
    the original; a change to either side alone fails here."""
    from app.version import VersionResponse as ConfigVersionResponse
    from herd_common.version import VersionResponse as SharedVersionResponse

    def shape(model):
        return {
            name: (str(field.annotation), field.is_required())
            for name, field in model.model_fields.items()
        }

    assert shape(ConfigVersionResponse) == shape(SharedVersionResponse)


def test_config_app_never_imports_herd_common():
    """The config image does not contain herd_common, so an import of it boots
    fine on the host (the workspace venv has every package) and crashes the
    container with ModuleNotFoundError. No unit test can see that crash, so
    pin the invariant statically instead: parse every module under app/ and
    refuse any import of herd_common."""
    app_dir = Path(__file__).resolve().parents[1] / "app"
    offenders = []
    for py_file in sorted(app_dir.rglob("*.py")):
        tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            for name in names:
                if name == "herd_common" or name.startswith("herd_common."):
                    offenders.append(f"{py_file.relative_to(app_dir)}:{node.lineno}")
    assert not offenders, (
        "services/config/app must not import herd_common: the config image does not "
        "contain it, so this boots on the host and crashes in the container. "
        f"Offending imports: {offenders}"
    )
