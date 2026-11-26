"""The live-Postgres suites' one DSN rule, `herd_common.pg_live_suite` (issue #1401).

Every `services/*/tests/*_live_pg.py` suite asks `pg_live_target()` whether to skip,
error, or run. These tests drive it with an injected environment and a recording
probe, so they prove the decision without a database: with no DSN named, the probe
(the only code that connects) is never called. `probe_database` itself is driven
against a closed loopback port, a listener that never answers, and URLs it cannot
use; its success path is exercised by every live suite in the gate.
`tests/unit/test_pg_live_dsn_source.py` is the static half: no suite or fixture names
a database or touches the variables itself.
"""

from __future__ import annotations

import socket
import threading
import time

import pytest
from herd_common import pg_live_suite
from herd_common.pg_live_suite import (
    DSN_VARIABLE,
    REQUIRED_VARIABLE,
    REQUIRED_WITHOUT_DSN,
    SKIP_REASON,
    PgLiveTarget,
    display_url,
    pg_live_target,
    probe_database,
)

DSN = "postgresql+asyncpg://herd:s3cret@db.example.test:6543/herd"
SHOWN = "postgresql+asyncpg://db.example.test:6543/herd"


@pytest.fixture(autouse=True)
def _fresh_probe_cache():
    pg_live_suite._probe_results.clear()
    yield
    pg_live_suite._probe_results.clear()


class RecordingProbe:
    """Stands in for `probe_database`: records each DSN it is asked about and answers
    with `problem` (None means the database answered)."""

    def __init__(self, problem: str | None = None) -> None:
        self.problem = problem
        self.calls: list[str] = []

    def __call__(self, dsn: str) -> str | None:
        self.calls.append(dsn)
        return self.problem


def _environ(dsn: str | None, required: str | None) -> dict[str, str]:
    environ = {}
    if dsn is not None:
        environ[DSN_VARIABLE] = dsn
    if required is not None:
        environ[REQUIRED_VARIABLE] = required
    return environ


NOT_REQUIRED = [None, "", "0"]
REQUIRED = ["1", "yes", "true", " ", "00"]


def test_the_variable_names_are_the_documented_ones():
    assert (DSN_VARIABLE, REQUIRED_VARIABLE) == ("HERD_TEST_PG_DSN", "HERD_TEST_PG_REQUIRED")


@pytest.mark.parametrize(
    "environ",
    [
        {},
        {DSN_VARIABLE: ""},
        {DSN_VARIABLE: "   "},
        {DSN_VARIABLE: "\t\n"},
        {REQUIRED_VARIABLE: ""},
        {REQUIRED_VARIABLE: "0"},
        {DSN_VARIABLE: "", REQUIRED_VARIABLE: "0"},
        # The old default's inputs no longer name a database.
        {"POSTGRES_PORT": "5433", "POSTGRES_USER": "herd", "POSTGRES_DB": "herd"},
        {"PGHOST": "127.0.0.1", "PGPORT": "5433", "DATABASE_URL": DSN},
    ],
    ids=[
        "unset",
        "empty",
        "spaces",
        "whitespace",
        "required-empty",
        "required-0",
        "empty-required-0",
        "postgres-port-set",
        "libpq-and-database-url-set",
    ],
)
def test_no_dsn_skips_and_connects_to_nothing(environ):
    probe = RecordingProbe()
    target = pg_live_target(environ, probe=probe)
    assert target == PgLiveTarget(dsn="", skip_reason=SKIP_REASON, failure=None)
    assert probe.calls == [], "no DSN named, yet the probe was asked to connect"


@pytest.mark.parametrize("required", REQUIRED)
def test_no_dsn_when_required_errors_and_connects_to_nothing(required):
    probe = RecordingProbe()
    target = pg_live_target({REQUIRED_VARIABLE: required}, probe=probe)
    assert target == PgLiveTarget(dsn="", skip_reason=None, failure=REQUIRED_WITHOUT_DSN)
    assert probe.calls == []


@pytest.mark.parametrize("required", NOT_REQUIRED + REQUIRED)
def test_a_named_database_that_answers_runs(required):
    probe = RecordingProbe()
    target = pg_live_target(_environ(DSN, required), probe=probe)
    assert target == PgLiveTarget(dsn=DSN, skip_reason=None, failure=None)
    assert probe.calls == [DSN]


def test_the_named_dsn_is_used_as_given_apart_from_surrounding_whitespace():
    probe = RecordingProbe()
    target = pg_live_target({DSN_VARIABLE: f"  {DSN}\n"}, probe=probe)
    assert target.dsn == DSN
    assert probe.calls == [DSN]


@pytest.mark.parametrize("required", NOT_REQUIRED)
def test_a_named_database_that_does_not_answer_skips_when_not_required(required):
    target = pg_live_target(_environ(DSN, required), probe=RecordingProbe("ConnectionRefusedError"))
    assert target == PgLiveTarget(
        dsn=DSN,
        skip_reason=(
            f"HERD_TEST_PG_DSN names {SHOWN}, which did not answer (ConnectionRefusedError), "
            "so this live-Postgres suite is skipped. Start that database, or set "
            "HERD_TEST_PG_REQUIRED=1 to make this an error."
        ),
        failure=None,
    )


@pytest.mark.parametrize("required", REQUIRED)
def test_a_named_database_that_does_not_answer_errors_when_required(required):
    target = pg_live_target(_environ(DSN, required), probe=RecordingProbe("ConnectionRefusedError"))
    assert target == PgLiveTarget(
        dsn=DSN,
        skip_reason=None,
        failure=(
            f"HERD_TEST_PG_DSN names {SHOWN}, which did not answer (ConnectionRefusedError), "
            "and HERD_TEST_PG_REQUIRED is set: start that database."
        ),
    )


@pytest.mark.parametrize(
    ("problem", "what"),
    [
        ("ModuleNotFoundError", "whose driver is not installed (ModuleNotFoundError)"),
        ("ImportError", "whose driver is not installed (ImportError)"),
        ("NoSuchModuleError", "whose driver is not installed (NoSuchModuleError)"),
        ("ArgumentError", "which is not a usable database URL (ArgumentError)"),
        ("TimeoutError", "which did not answer (TimeoutError)"),
    ],
)
@pytest.mark.parametrize("required", ["0", "1"])
def test_the_reason_says_what_went_wrong_by_class_name(problem, what, required):
    target = pg_live_target(_environ(DSN, required), probe=RecordingProbe(problem))
    text = target.failure if required == "1" else target.skip_reason
    assert text is not None
    assert text.startswith(f"HERD_TEST_PG_DSN names {SHOWN}, {what}, ")
    assert "s3cret" not in text


def test_the_default_environment_is_the_process_environment(monkeypatch):
    probe = RecordingProbe()
    monkeypatch.delenv(DSN_VARIABLE, raising=False)
    monkeypatch.delenv(REQUIRED_VARIABLE, raising=False)
    assert pg_live_target(probe=probe).skip_reason == SKIP_REASON
    monkeypatch.setenv(DSN_VARIABLE, DSN)
    assert pg_live_target(probe=probe).failure is None
    assert probe.calls == [DSN]


def test_the_suites_get_the_real_probe_and_the_process_environment():
    assert pg_live_target.__defaults__ == (None, pg_live_suite.probe_database)


def test_the_reasons_tell_the_developer_how_to_run_the_suite():
    assert SKIP_REASON == (
        "HERD_TEST_PG_DSN is not set, so this live-Postgres suite is skipped and connects "
        "to nothing. To run it, name a database you own: "
        "HERD_TEST_PG_DSN=postgresql+asyncpg://USER:PASSWORD@HOST:PORT/DB uv run pytest "
        "<file> from the service directory (docs/specs/TESTING.md, pg-live suites)."
    )
    assert REQUIRED_WITHOUT_DSN == (
        "HERD_TEST_PG_REQUIRED is set but HERD_TEST_PG_DSN is not: a hard-required "
        "live-Postgres run must name its database in HERD_TEST_PG_DSN; there is no default."
    )


# probe_database: the cache, the timeout, and real refusals.


def test_the_probe_runs_once_per_dsn_per_process(monkeypatch):
    calls: list[str] = []

    def fake_once(dsn: str) -> str | None:
        calls.append(dsn)
        return None if dsn.endswith("/a") else "TimeoutError"

    monkeypatch.setattr(pg_live_suite, "_probe_once", fake_once)
    a, b = "postgresql+asyncpg://h/a", "postgresql+asyncpg://h/b"
    assert [probe_database(a), probe_database(b), probe_database(a), probe_database(b)] == [
        None,
        "TimeoutError",
        None,
        "TimeoutError",
    ]
    assert calls == [a, b]


def test_the_probe_connects_with_the_ten_second_timeout(monkeypatch):
    import sqlalchemy.ext.asyncio

    seen: list[dict] = []

    def recording_engine(dsn, **kwargs):
        seen.append(kwargs)
        raise RuntimeError("stop before connecting")

    monkeypatch.setattr(sqlalchemy.ext.asyncio, "create_async_engine", recording_engine)
    assert probe_database("postgresql+asyncpg://x:x@127.0.0.1:9/x") == "RuntimeError"
    assert pg_live_suite.PROBE_CONNECT_TIMEOUT_SECONDS == 10
    assert seen == [{"connect_args": {"timeout": 10}}]


def test_a_listener_that_never_answers_is_given_up_on_after_the_timeout(monkeypatch):
    """A half-open listener (accepted by the kernel, never answered) holds a probe
    without a timeout for asyncpg's default 60 s; with it, the probe gives up."""
    monkeypatch.setattr(pg_live_suite, "PROBE_CONNECT_TIMEOUT_SECONDS", 0.5)
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(4)
        dsn = f"postgresql+asyncpg://x:x@127.0.0.1:{listener.getsockname()[1]}/x"
        answer: list[str | None] = []
        worker = threading.Thread(target=lambda: answer.append(probe_database(dsn)), daemon=True)
        started = time.monotonic()
        worker.start()
        worker.join(timeout=10)
        assert not worker.is_alive(), "the probe did not give up within 10 s"
    assert answer == ["TimeoutError"]
    assert time.monotonic() - started < 10


def _closed_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def test_probe_database_reports_a_refused_connection_by_class_name():
    port = _closed_loopback_port()
    problem = probe_database(f"postgresql+asyncpg://x:x@127.0.0.1:{port}/x")
    assert problem in {"ConnectionRefusedError", "OSError"}


@pytest.mark.parametrize(
    ("dsn", "problem"),
    [
        ("not a url", "ArgumentError"),
        ("nosuchdialect://x:x@127.0.0.1:9/x", "NoSuchModuleError"),
    ],
)
def test_probe_database_reports_a_bad_url_by_class_name(dsn, problem):
    assert probe_database(dsn) == problem


# display_url never shows a user, a password, or a query string.


@pytest.mark.parametrize(
    ("dsn", "shown"),
    [
        (DSN, SHOWN),
        ("postgresql+asyncpg://u@h/db?password=SECRETQ", "postgresql+asyncpg://h/db"),
        (
            "postgresql+asyncpg://u@/db?host=/tmp&password=SECRETQ",
            "postgresql+asyncpg://<no host>/db",
        ),
        ("postgresql+asyncpg://u:SECRETQ@ss@h:5/db", "postgresql+asyncpg://<host hidden>:5/db"),
        ("postgresql+asyncpg://u:SECRETQ@[::1]:5/db", "postgresql+asyncpg://[::1]:5/db"),
        ("postgresql+asyncpg://u:SECRETQ@h/db@SECRETQ", "postgresql+asyncpg://h/<database hidden>"),
        ("postgresql+asyncpg:///db", "postgresql+asyncpg://<no host>/db"),
        ("not a url SECRETQ", "a value that is not a database URL"),
    ],
)
def test_display_url_shows_only_driver_host_port_and_database(dsn, shown):
    assert display_url(dsn) == shown
    assert "SECRETQ" not in display_url(dsn)
    for required in ("0", "1"):
        target = pg_live_target(_environ(dsn, required), probe=RecordingProbe("OSError"))
        assert "SECRETQ" not in (target.failure or target.skip_reason or "")
