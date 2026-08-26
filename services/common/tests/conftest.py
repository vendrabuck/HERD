import gc

import pytest


@pytest.fixture(autouse=True)
def _anyio_backend():
    """All async tests use asyncio."""
    return "asyncio"


@pytest.fixture(autouse=True)
def _collect_garbage_after_each_test():
    """Force a GC pass after every test (issue #534).

    The root `filterwarnings = ["error"]` setting turns any warning into a
    test failure, including `PytestUnraisableExceptionWarning`. A leaked
    resource (e.g. an aiosqlite `Connection` never closed via
    `engine.dispose()`) only raises that warning when the garbage collector
    finally collects it, which can happen at an arbitrary later point, so the
    failure lands on whatever unrelated test happens to be running at the
    time, not on the test that actually leaked the resource (issue #534:
    `test_gated_defers_then_starts_when_table_appears` failed this way on a
    frontend-only dependency bump PR that could not have touched a SQLite
    connection). Collecting after every test pins any future leak to its
    actual culprit instead of an arbitrary victim.
    """
    yield
    gc.collect()


pytest_plugins = []
