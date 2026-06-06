import pytest


@pytest.fixture(autouse=True)
def _anyio_backend():
    """All async tests use asyncio."""
    return "asyncio"


pytest_plugins = []
