"""The live-Postgres suites' one DSN rule, `herd_common.pg_live_suite` (issue #1401).

Every `services/*/tests/*_live_pg.py` suite asks `pg_live_target()` whether to skip,
fail, or run. These tests drive it with an injected environment and a recording
probe, so they prove the decision without a database: with no DSN named, the probe
(the only code that connects) is never called. `probe_database` itself is driven
against a closed loopback port and an unparseable URL; its success path is exercised
by every live suite in the gate. `tests/unit/test_pg_live_dsn_source.py` is the
static half: no suite or fixture names a database or touches the variables itself.
"""

from __future__ import annotations

import socket

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


class RecordingProbe:
    """Stands in for `probe_database`: records each DSN it is asked about and answers
    with `problem` (None means the database answered)."""

    def __init__(self, problem: str | None = None) -> None:
        self.problem = problem
        self.calls: list[str] = []

    def __call__(self, dsn: str) -> str | None:
        self.calls.append(dsn)
        return self.problem


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


@pytest.mark.parametrize("required", ["1", "yes", "true", " ", "00"])
def test_no_dsn_when_required_fails_and_connects_to_nothing(required):
    probe = RecordingProbe()
    target = pg_live_target({REQUIRED_VARIABLE: required}, probe=probe)
    assert target == PgLiveTarget(dsn="", skip_reason=None, failure=REQUIRED_WITHOUT_DSN)
    assert probe.calls == []


@pytest.mark.parametrize("required", [None, "", "0", "1"])
def test_a_named_database_that_answers_runs(required):
    environ = {DSN_VARIABLE: DSN}
    if required is not None:
        environ[REQUIRED_VARIABLE] = required
    probe = RecordingProbe()
    target = pg_live_target(environ, probe=probe)
    assert target == PgLiveTarget(dsn=DSN, skip_reason=None, failure=None)
    assert probe.calls == [DSN]


def test_the_named_dsn_is_used_as_given_apart_from_surrounding_whitespace():
    probe = RecordingProbe()
    target = pg_live_target({DSN_VARIABLE: f"  {DSN}\n"}, probe=probe)
    assert target.dsn == DSN
    assert probe.calls == [DSN]


@pytest.mark.parametrize("required", [None, "", "0", "1"])
def test_a_named_database_that_does_not_answer_fails_never_skips(required):
    environ = {DSN_VARIABLE: DSN}
    if required is not None:
        environ[REQUIRED_VARIABLE] = required
    target = pg_live_target(environ, probe=RecordingProbe("ConnectionRefusedError"))
    assert target.skip_reason is None
    assert target.dsn == DSN
    assert target.failure == (
        "HERD_TEST_PG_DSN names postgresql+asyncpg://herd:***@db.example.test:6543/herd, "
        "which did not answer (ConnectionRefusedError): start that database, or unset "
        "HERD_TEST_PG_DSN to skip the live-Postgres suites."
    )
    assert "s3cret" not in target.failure


def test_an_unparseable_dsn_fails_without_echoing_it():
    target = pg_live_target(
        {DSN_VARIABLE: "not a url s3cret"}, probe=RecordingProbe("ArgumentError")
    )
    assert target.failure is not None
    assert "a value that is not a database URL" in target.failure
    assert "s3cret" not in target.failure


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


def _closed_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def test_probe_database_reports_a_refused_connection_by_class_name():
    port = _closed_loopback_port()
    problem = probe_database(f"postgresql+asyncpg://x:x@127.0.0.1:{port}/x")
    assert problem is not None
    assert problem in {"ConnectionRefusedError", "OSError"}


@pytest.mark.parametrize(
    ("dsn", "problem"),
    [
        ("not a url", "ArgumentError"),
        # An unknown dialect: refused before any connect.
        ("nosuchdialect://x:x@127.0.0.1:9/x", "NoSuchModuleError"),
    ],
)
def test_probe_database_reports_a_bad_url_by_class_name(dsn, problem):
    assert probe_database(dsn) == problem


def test_display_url_hides_the_password():
    assert display_url(DSN) == "postgresql+asyncpg://herd:***@db.example.test:6543/herd"
    assert display_url("not a url") == "a value that is not a database URL"
