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

A failure raised in that fixture is reported by pytest as an ERROR at setup of each
test, not as a FAILED test; either way the run is red.

The rule (docs/specs/TESTING.md, pg-live suites). "Required" means
`HERD_TEST_PG_REQUIRED` is set to anything but empty or `0`; the gate phase
`_gate-pg-live-tests` sets it on every command.

- `HERD_TEST_PG_DSN` unset, empty, or only whitespace: SKIPPED with `SKIP_REASON` when
  not required, an ERROR (`REQUIRED_WITHOUT_DSN`) when required; either way nothing
  connects to anything.
- `HERD_TEST_PG_DSN` names a database that answers the probe: the suite runs.
- `HERD_TEST_PG_DSN` names one that does not answer (or a URL whose driver is not
  installed, or that is not a usable URL): SKIPPED when not required, with a reason
  that names the database and says what went wrong, so a typo shows in `-rs` output;
  an ERROR with the same text when required.

There is no default database: before #1401 every suite fell back to the dev stack's
published Postgres (127.0.0.1, port `POSTGRES_PORT` or 5433), so a bare `make test`
wrote into the working database. With a DSN named, one probe (`SELECT 1`, 10 s connect
timeout, `PROBE_CONNECT_TIMEOUT_SECONDS`) runs per DSN per process; its answer is
cached, so the suites a single pytest run collects share it. A message about a named
database shows only its driver, host, port, and database name (`display_url`), never
a user, a password, or a query string.

A strict mode, where a named database that does not answer is an error even when not
required, was considered for #1401 and left for the owner to turn on: tooling outside
the repository sets a dead address on purpose to make these suites skip.
"""

from __future__ import annotations

import asyncio
import os
import re
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
# Exception class names that mean the URL's driver (or dialect) cannot be loaded.
DRIVER_MISSING = frozenset({"ModuleNotFoundError", "ImportError", "NoSuchModuleError"})
NOT_A_URL = frozenset({"ArgumentError"})

_SAFE_HOST = re.compile(r"[A-Za-z0-9.\-:]+")
_SAFE_DATABASE = re.compile(r"[A-Za-z0-9_.\-]+")
_probe_results: dict[str, str | None] = {}


@dataclass(frozen=True)
class PgLiveTarget:
    """What a live-Postgres suite does: `dsn` is the database to use ("" when none is
    named); at most one of `skip_reason` and `failure` is set, and neither is set when
    the suite runs."""

    dsn: str
    skip_reason: str | None
    failure: str | None


def _probe_once(dsn: str) -> str | None:
    """Connect, send `SELECT 1`, and disconnect; None when that worked, else the class
    name of what was raised. Never raises; never logs the URL."""

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


def probe_database(dsn: str) -> str | None:
    """`_probe_once`, run at most once per DSN in this process (the answer is cached by
    the exact DSN string)."""
    if dsn not in _probe_results:
        _probe_results[dsn] = _probe_once(dsn)
    return _probe_results[dsn]


def display_url(dsn: str) -> str:
    """The driver, host, port, and database name of a URL, and nothing else: no user,
    password, or query string. A host or database name outside a plain character set
    (where a password with an unescaped `@` would land) is replaced by a placeholder."""
    from sqlalchemy.engine import make_url

    try:
        url = make_url(dsn)
    except Exception:
        return "a value that is not a database URL"
    host = url.host
    if not host:
        shown_host = "<no host>"
    elif _SAFE_HOST.fullmatch(host):
        shown_host = f"[{host}]" if ":" in host else host
    else:
        shown_host = "<host hidden>"
    port = f":{url.port}" if url.port else ""
    database = url.database or ""
    if database and not _SAFE_DATABASE.fullmatch(database):
        database = "<database hidden>"
    return f"{url.drivername}://{shown_host}{port}/{database}"


def _problem_text(dsn: str, problem: str) -> str:
    if problem in DRIVER_MISSING:
        what = f"whose driver is not installed ({problem})"
    elif problem in NOT_A_URL:
        what = f"which is not a usable database URL ({problem})"
    else:
        what = f"which did not answer ({problem})"
    return f"{DSN_VARIABLE} names {display_url(dsn)}, {what}"


def pg_live_target(
    environ: Mapping[str, str] | None = None,
    probe: Callable[[str], str | None] = probe_database,
) -> PgLiveTarget:
    """Decide, from the environment, whether a live-Postgres suite skips, errors, or
    runs, and against which database. `probe` is called only when a DSN is named."""
    env = os.environ if environ is None else environ
    dsn = env.get(DSN_VARIABLE, "").strip()
    required = env.get(REQUIRED_VARIABLE, "") not in ("", "0")
    if not dsn:
        if required:
            return PgLiveTarget(dsn="", skip_reason=None, failure=REQUIRED_WITHOUT_DSN)
        return PgLiveTarget(dsn="", skip_reason=SKIP_REASON, failure=None)
    problem = probe(dsn)
    if problem is None:
        return PgLiveTarget(dsn=dsn, skip_reason=None, failure=None)
    text = _problem_text(dsn, problem)
    if required:
        return PgLiveTarget(
            dsn=dsn,
            skip_reason=None,
            failure=f"{text}, and {REQUIRED_VARIABLE} is set: start that database.",
        )
    return PgLiveTarget(
        dsn=dsn,
        skip_reason=(
            f"{text}, so this live-Postgres suite is skipped. Start that database, or set "
            f"{REQUIRED_VARIABLE}=1 to make this an error."
        ),
        failure=None,
    )
