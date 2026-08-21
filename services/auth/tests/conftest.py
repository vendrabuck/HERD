"""Fixtures shared by the whole auth test suite.

Today this file exists for exactly one seam: the LDAP stale-run reaper's
session factory (issue #528). The reaper deliberately runs on its OWN
session rather than the caller's, so in production it opens one from
app.database. Every LDAP sync test file, however, builds a PRIVATE in-memory
engine and hands run_sync a session from it, which leaves the reaper looking
at app.database's schema: no ldap_sync_runs table, a swallowed exception,
and a traceback in the captured log that has nothing to do with the test.
Worse, the reap those tests drive is then a no-op against an empty database,
so nothing they do can prove a corpse was ever flipped.

Pointing the reaper at the same engine the test seeded fixes both: the noise
goes away at its source, and the reap runs against real rows.
"""

import pytest
from app.services import ldap_sync_service


@pytest.fixture
def use_reap_session_factory(monkeypatch):
    """Point the stale-run reaper's own-session factory at a sessionmaker.

    A setter rather than a fixture that does the work itself, because the
    engine a test needs the reaper to see is not always known at fixture
    time: some files build it at module import, some in a fixture, some
    inside the test body. Each LDAP sync test file wires it once, usually
    from a three-line autouse fixture of its own.

    Deliberately NOT autouse here. Files that exercise the reaper's own
    session end to end (tests/test_ldap_sync_stale_run_reaper.py) must see
    the PRODUCTION default, app.database.AsyncSessionLocal, rather than a
    global override that would hide a broken default from every test at once.
    """

    def _use(session_factory) -> None:
        monkeypatch.setattr(ldap_sync_service, "_reap_session_factory", session_factory)

    return _use
