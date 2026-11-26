"""The ONE place a live-Postgres test suite learns which database it may use (#1401).

Test support, not runtime code: only the `services/*/tests/*_live_pg.py` suites
import it, and it imports nothing from pytest, so the runtime package gains no test
dependency. Each suite calls `pg_live_target()` once at import and applies the answer:

    _PG = pg_live_target()
    PG_DSN = _PG.dsn
    pytestmark = pytest.mark.skipif(_PG.skip_reason is not None, reason=_PG.skip_reason or "")

    @pytest.fixture(autouse=True)
    def _fail_when_pg_live_cannot_run():
        if _PG.failure is not None:
            pytest.fail(_PG.failure)

The rule (docs/specs/TESTING.md, pg-live suites):

- `HERD_TEST_PG_DSN` unset, empty, or only whitespace, and `HERD_TEST_PG_REQUIRED`
  unset, empty, or `0`: the suite is SKIPPED with `SKIP_REASON` and no connection is
  attempted to anything. There is no default database: before #1401 every suite fell
  back to the dev stack's published Postgres (127.0.0.1, port `POSTGRES_PORT` or 5433),
  so a bare `make test` wrote into the working database.
- `HERD_TEST_PG_DSN` unset (as above) and `HERD_TEST_PG_REQUIRED` set to anything
  else: every test FAILS with `REQUIRED_WITHOUT_DSN`, and still nothing connects. The
  gate phase `_gate-pg-live-tests` sets both, so a lost DSN there is a red gate, never
  a skip.
- `HERD_TEST_PG_DSN` set: the suite runs against that database. One probe (`SELECT 1`,
  10 s connect timeout) runs at import; when it fails, every test FAILS with a message
  naming the URL with its password hidden and the exception class, whatever
  `HERD_TEST_PG_REQUIRED` says. A database you named and cannot reach is an error, not
  a skip.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass

DSN_VARIABLE = "HERD_TEST_PG_DSN"
REQUIRED_VARIABLE = "HERD_TEST_PG_REQUIRED"
PROBE_CONNECT_TIMEOUT_SECONDS = 10

SKIP_REASON = (
    f"{DSN_VARIABLE} is not set, so this live-Postgres suite is skipped and connects to "
    "nothing. To run it, name a database you own: "
    f"{DSN_VARIABLE}=postgresql+asyncpg://USER:PASSWORD@HOST:PORT/DB uv run pytest <file> "
    "from the service directory (docs/specs/TESTING.md, pg-live suites)."
)
REQUIRED_WITHOUT_DSN = (
    f"{REQUIRED_VARIABLE} is set but {DSN_VARIABLE} is not: a hard-required live-Postgres "
    f"run must name its database in {DSN_VARIABLE}; there is no default."
)


@dataclass(frozen=True)
class PgLiveTarget:
    """What a live-Postgres suite does: `dsn` is the database to use ("" when none is
    named); exactly one of `skip_reason` and `failure` is set when the suite must not
    run its tests, and neither is set when it runs."""

    dsn: str
    skip_reason: str | None
    failure: str | None


def probe_database(dsn: str) -> str | None:
    """None when `dsn` answers `SELECT 1`, else the class name of what was raised
    (an unparseable URL included). Never raises; never logs the URL."""

    async def _probe() -> None:
        from sqlalchemy import text
        from sqlalchemy.ext.asyncio import create_async_engine

        engine = create_async_engine(dsn, connect_args={"timeout": PROBE_CONNECT_TIMEOUT_SECONDS})
        try:
            async with engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
        finally:
            await engine.dispose()

    try:
        asyncio.run(_probe())
    except Exception as error:
        return type(error).__name__
    return None


def display_url(dsn: str) -> str:
    """The URL with its password hidden, for a failure message."""
    from sqlalchemy.engine import make_url

    try:
        return make_url(dsn).render_as_string(hide_password=True)
    except Exception:
        return "a value that is not a database URL"


def pg_live_target(
    environ: Mapping[str, str] | None = None,
    probe: Callable[[str], str | None] = probe_database,
) -> PgLiveTarget:
    """Decide, from the environment, whether a live-Postgres suite skips, fails, or
    runs, and against which database. `probe` is called only when a DSN is named."""
    env = os.environ if environ is None else environ
    dsn = env.get(DSN_VARIABLE, "").strip()
    required = env.get(REQUIRED_VARIABLE, "") not in ("", "0")
    if not dsn:
        if required:
            return PgLiveTarget(dsn="", skip_reason=None, failure=REQUIRED_WITHOUT_DSN)
        return PgLiveTarget(dsn="", skip_reason=SKIP_REASON, failure=None)
    problem = probe(dsn)
    if problem is not None:
        return PgLiveTarget(
            dsn=dsn,
            skip_reason=None,
            failure=(
                f"{DSN_VARIABLE} names {display_url(dsn)}, which did not answer "
                f"({problem}): start that database, or unset {DSN_VARIABLE} to skip the "
                "live-Postgres suites."
            ),
        )
    return PgLiveTarget(dsn=dsn, skip_reason=None, failure=None)
