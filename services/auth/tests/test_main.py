"""Tests for main.py: lifespan seeding functions."""

import asyncio
import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from app.database import Base
from app.models.group import UserGroup
from app.models.user import Role
from app.schemas import auth as schemas
from app.services.auth_service import create_user, get_user_by_email, superadmin_exists
from app.services.group_service import get_group_by_name
from app.services.passwords import verify_password
from herd_common.logging import JSONFormatter
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from tests._seed_values import ACCEPTED_SEED_VALUES

ACCEPTED_IDS = [name for name, _, _ in ACCEPTED_SEED_VALUES]

TEST_DATABASE_URL = "sqlite+aiosqlite:///:memory:"

engine = create_async_engine(TEST_DATABASE_URL, echo=False)
TestSessionLocal = async_sessionmaker(engine, expire_on_commit=False)


@pytest.fixture(autouse=True)
async def setup_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


# --- _seed_superadmin ---


@pytest.mark.asyncio
async def test_seed_superadmin_skips_when_env_vars_empty():
    """When superadmin env vars are blank, _seed_superadmin returns early."""
    # The seed's session factory points at this test's database, so a seed
    # that did run would leave a superadmin row the read below finds.
    with (
        patch("app.main.settings") as mock_settings,
        patch("app.main.AsyncSessionLocal", TestSessionLocal),
    ):
        mock_settings.superadmin_email = ""
        mock_settings.superadmin_username = ""
        mock_settings.superadmin_password = ""

        from app.main import _seed_superadmin

        await _seed_superadmin()

    # No superadmin should exist
    async with TestSessionLocal() as db:
        assert await superadmin_exists(db) is False


@pytest.mark.asyncio
async def test_seed_superadmin_skips_when_already_exists():
    """When a superadmin already exists, _seed_superadmin is a no-op."""
    async with TestSessionLocal() as db:
        await create_user(db, "existing@test.com", "existingsa", "password123", Role.SUPERADMIN)

    with (
        patch("app.main.settings") as mock_settings,
        patch("app.main.AsyncSessionLocal", TestSessionLocal),
    ):
        mock_settings.superadmin_email = "new@test.com"
        mock_settings.superadmin_username = "newsa"
        mock_settings.superadmin_password = "password123"

        from app.main import _seed_superadmin

        await _seed_superadmin()

    # Only one superadmin should exist (the original one)
    async with TestSessionLocal() as db:
        result = await get_user_by_email(db, "new@test.com")
        assert result is None


@pytest.mark.asyncio
async def test_seed_superadmin_skips_when_email_already_registered():
    """When email is already registered as non-superadmin, warns and skips."""
    async with TestSessionLocal() as db:
        await create_user(db, "taken@test.com", "takenuser", "password123", Role.USER)

    with (
        patch("app.main.settings") as mock_settings,
        patch("app.main.AsyncSessionLocal", TestSessionLocal),
    ):
        mock_settings.superadmin_email = "taken@test.com"
        mock_settings.superadmin_username = "superadmin"
        mock_settings.superadmin_password = "password123"

        from app.main import _seed_superadmin

        await _seed_superadmin()

    # User should still be regular, not superadmin
    async with TestSessionLocal() as db:
        user = await get_user_by_email(db, "taken@test.com")
        assert user is not None
        assert user.role == Role.USER


@pytest.mark.asyncio
async def test_seed_superadmin_creates_new_superadmin():
    """When conditions are met, creates the superadmin account."""
    with (
        patch("app.main.settings") as mock_settings,
        patch("app.main.AsyncSessionLocal", TestSessionLocal),
    ):
        mock_settings.superadmin_email = "sa@test.com"
        mock_settings.superadmin_username = "superadmin"
        mock_settings.superadmin_password = "strongpass1"

        from app.main import _seed_superadmin

        await _seed_superadmin()

    async with TestSessionLocal() as db:
        user = await get_user_by_email(db, "sa@test.com")
        assert user is not None
        assert user.role == Role.SUPERADMIN
        assert user.username == "superadmin"


@pytest.mark.asyncio
async def test_seed_superadmin_skips_partial_env_vars():
    """When only some env vars are set (password missing), skip seeding."""
    # The seed's session factory points at this test's database, so a seed
    # that did run would leave a superadmin row the read below finds.
    with (
        patch("app.main.settings") as mock_settings,
        patch("app.main.AsyncSessionLocal", TestSessionLocal),
    ):
        mock_settings.superadmin_email = "sa@test.com"
        mock_settings.superadmin_username = "superadmin"
        mock_settings.superadmin_password = ""

        from app.main import _seed_superadmin

        await _seed_superadmin()

    async with TestSessionLocal() as db:
        assert await superadmin_exists(db) is False


# --- _seed_superadmin: the registration password rules (issue #1450) ---


async def _run_seed(value: str, email: str = "sa@test.com") -> None:
    with (
        patch("app.main.settings") as mock_settings,
        patch("app.main.AsyncSessionLocal", TestSessionLocal),
    ):
        mock_settings.superadmin_email = email
        mock_settings.superadmin_username = "superadmin"
        mock_settings.superadmin_password = value

        from app.main import _seed_superadmin

        await _seed_superadmin()


def _fragments(value: str, size: int = 4) -> set[str]:
    """Every run of `size` consecutive characters of `value`."""
    return {value[i : i + size] for i in range(len(value) - size + 1)}


def _record_texts(record: logging.LogRecord) -> list[str]:
    """Everything a captured record could carry to an operator: its message,
    the line the service's real JSON formatter renders (which prints every
    `extra`, nested values included), and repr of the whole record, which keeps
    non-ASCII characters the JSON form escapes."""
    return [
        record.getMessage(),
        JSONFormatter("auth").format(record),
        repr(record.__dict__),
    ]


def test_the_refusal_log_text_is_pinned():
    from app.main import SEED_VALUE_REFUSED_LOG

    assert SEED_VALUE_REFUSED_LOG == (
        "SUPERADMIN_PASSWORD breaks a registration password rule: %s. "
        "The superadmin account was not created; set a value that registration "
        "accepts and recreate auth."
    )
    assert schemas.PASSWORD_TOO_SHORT_RULE == "Password must be at least 8 characters"
    assert schemas.PASSWORD_TOO_LONG_RULE == "Password must be at most 72 characters"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "value, rule",
    [
        ("zq" * 3 + "z", "Password must be at least 8 characters"),
        ("zq" * 36 + "z", "Password must be at most 72 characters"),
        # 73 two-byte characters: refused on characters like registration.
        ("\u00e9" * 73, "Password must be at most 72 characters"),
        # 9 as configured, 7 once trimmed: the trimmed value is judged.
        (" " + "zq" * 3 + "z" + " ", "Password must be at least 8 characters"),
        # 74 as configured with no white space to trim.
        ("zq" * 37, "Password must be at most 72 characters"),
        ("zqzq\x00zqzq", "Password must not contain a NUL character"),
        # A non-UTF-8 byte in the environment arrives as a lone surrogate.
        ("zqzq\udcffzqzq", "Password must be encodable as UTF-8"),
    ],
    ids=[
        "7-characters",
        "73-characters",
        "73-two-byte-characters",
        "7-after-trimming",
        "74-characters",
        "nul",
        "lone-surrogate",
    ],
)
async def test_seed_superadmin_refuses_a_value_registration_refuses(value, rule, caplog):
    """IAM-BOOT-3: a value registration would refuse creates no account and
    logs exactly one ERROR naming the rule, with no run of 4 or more of the
    value's characters in it; startup continues (the seed returns)."""
    from app.main import SEED_VALUE_REFUSED_LOG

    with caplog.at_level(logging.DEBUG):
        await _run_seed(value)

    async with TestSessionLocal() as db:
        assert await superadmin_exists(db) is False
        assert await get_user_by_email(db, "sa@test.com") is None
    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert len(errors) == 1
    assert errors[0].name == "app.main"
    assert errors[0].getMessage() == SEED_VALUE_REFUSED_LOG % rule
    assert errors[0].args == (rule,)
    for record in caplog.records:
        for text in _record_texts(record):
            assert value.strip() not in text
            assert not [f for f in _fragments(value) if f in text], text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "value, typed", [(v, t) for _, v, t in ACCEPTED_SEED_VALUES], ids=ACCEPTED_IDS
)
async def test_seed_superadmin_creates_at_every_accepted_bound(value, typed, caplog):
    """IAM-BOOT-3: 8 and 72 characters, 72 multi-byte characters (more than 72
    bytes), and a value that is 72 once trimmed all create the account, with
    no ERROR; the stored hash is of the trimmed value."""
    with caplog.at_level(logging.DEBUG):
        await _run_seed(value)

    async with TestSessionLocal() as db:
        user = await get_user_by_email(db, "sa@test.com")
    assert user is not None and user.role == Role.SUPERADMIN
    assert verify_password(typed, user.hashed_password)
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]


@pytest.mark.asyncio
async def test_an_existing_superadmin_is_untouched_and_a_refused_value_logs_nothing(caplog):
    """IAM-BOOT-3: the rules are checked only when an account would be
    created. With a superadmin already present, a value the rules refuse
    leaves that account as it is and logs no ERROR on this boot or any other."""
    async with TestSessionLocal() as db:
        existing = await create_user(db, "first@test.com", "firstsa", "zq" * 4, Role.SUPERADMIN)
        original_hash = existing.hashed_password

    with caplog.at_level(logging.DEBUG):
        await _run_seed("zq" * 40, email="first@test.com")
        await _run_seed("zq" * 40, email="other@test.com")

    async with TestSessionLocal() as db:
        user = await get_user_by_email(db, "first@test.com")
        assert user.hashed_password == original_hash
        assert user.role == Role.SUPERADMIN
        assert await get_user_by_email(db, "other@test.com") is None
    assert not [r for r in caplog.records if r.name == "app.main" and r.levelno >= logging.WARNING]


@pytest.mark.asyncio
async def test_a_taken_email_skips_the_seed_before_the_value_is_judged(caplog):
    """IAM-BOOT-2 and IAM-BOOT-3: with no superadmin and the email held by a
    regular user, the seed stops at the email check: the WARNING is logged and
    a value the rules refuse is never judged, so no ERROR."""
    async with TestSessionLocal() as db:
        await create_user(db, "taken@test.com", "takenuser", "zq" * 4, Role.USER)

    with caplog.at_level(logging.DEBUG):
        await _run_seed("zq" * 40, email="taken@test.com")

    async with TestSessionLocal() as db:
        assert await superadmin_exists(db) is False
    seed_records = [
        r for r in caplog.records if r.name == "app.main" and r.levelno >= logging.WARNING
    ]
    assert [(r.levelno, r.getMessage()) for r in seed_records] == [
        (
            logging.WARNING,
            "SUPERADMIN_EMAIL 'taken@test.com' is already registered as a regular user. "
            "Superadmin was not created.",
        )
    ]


def test_registration_has_no_model_level_validator():
    """refused_registration_rule runs RegisterRequest's validator on the
    password field of an empty instance, so a model-level validator would not
    run (a `before` one) or would fail on the missing fields (an `after` one).
    Adding one to RegisterRequest means the seed judge must be revisited
    (issue #1450)."""
    assert not schemas.RegisterRequest.__pydantic_decorators__.model_validators


def _judge_space() -> list[str]:
    """Lengths 0 to 80 in one-, two-, and four-byte characters, and a NUL or a
    lone surrogate at the start, middle, and end of values at the bounds."""
    values = []
    for n in range(81):
        values += [("zq" * 41)[:n], "\u00e9" * n, "\U00010348" * n]
    for n in (7, 8, 72, 73):
        for position in (0, n // 2, n - 1):
            for bad in ("\x00", "\udcff"):
                chars = list(("zq" * 41)[:n])
                chars[position] = bad
                values.append("".join(chars))
    return values


def _register_takes(value: str) -> bool:
    try:
        schemas.RegisterRequest(email="sa@example.com", username="superadmin", password=value)
    except ValidationError:
        return False
    return True


def test_the_seed_judge_is_registration():
    """refused_registration_rule refuses exactly what RegisterRequest refuses,
    across the space around both bounds (issue #1450: one judge, not a copy),
    and always answers with one of the fixed rule texts."""
    fixed = {
        schemas.PASSWORD_TOO_SHORT_RULE,
        schemas.PASSWORD_TOO_LONG_RULE,
        "Password must not contain a NUL character",
        "Password must be encodable as UTF-8",
    }
    for value in _judge_space():
        refusal = schemas.refused_registration_rule(value)
        assert (refusal is None) == _register_takes(value), repr(value)
        assert refusal is None or refusal in fixed, (repr(value), refusal)


# --- _seed_not_grouped ---


@pytest.mark.asyncio
async def test_seed_not_grouped_creates_default_group():
    """Creates 'Not Grouped' default user group on first run."""
    with patch("app.main.AsyncSessionLocal", TestSessionLocal):
        from app.main import _seed_not_grouped

        await _seed_not_grouped()

    async with TestSessionLocal() as db:
        group = await get_group_by_name(db, "Not Grouped")
        assert group is not None
        assert group.name == "Not Grouped"


@pytest.mark.asyncio
async def test_seed_not_grouped_skips_when_exists():
    """When 'Not Grouped' already exists, does not create a duplicate."""
    async with TestSessionLocal() as db:
        db.add(UserGroup(name="Not Grouped", description="Default"))
        await db.commit()

    with patch("app.main.AsyncSessionLocal", TestSessionLocal):
        from app.main import _seed_not_grouped

        await _seed_not_grouped()

    async with TestSessionLocal() as db:
        from sqlalchemy import func, select

        count = (
            await db.execute(
                select(func.count()).select_from(UserGroup).where(UserGroup.name == "Not Grouped")
            )
        ).scalar()
        assert count == 1


@pytest.mark.asyncio
async def test_seed_not_grouped_handles_integrity_error():
    """IntegrityError during insert is caught and rolled back."""
    # Pre-create the group so the insert will cause IntegrityError
    async with TestSessionLocal() as db:
        db.add(UserGroup(name="Not Grouped", description="Default"))
        await db.commit()

    # Make the seed's existence check answer "absent" so it attempts the
    # insert. The seed imports group_name_exists at call time, so patching the
    # module attribute reaches it; the assertion below proves the patch did.
    fake_exists = AsyncMock(return_value=False)

    with (
        patch("app.main.AsyncSessionLocal", TestSessionLocal),
        patch("app.services.group_service.group_name_exists", fake_exists),
    ):
        from app.main import _seed_not_grouped

        # Should not raise; IntegrityError is caught
        await _seed_not_grouped()

    fake_exists.assert_awaited_once()
    async with TestSessionLocal() as db:
        from sqlalchemy import func, select

        count = (
            await db.execute(
                select(func.count()).select_from(UserGroup).where(UserGroup.name == "Not Grouped")
            )
        ).scalar()
        assert count == 1


# --- lifespan ---


@pytest.mark.asyncio
async def test_lifespan_runs_seeding():
    """lifespan creates tables and runs seed functions."""
    from app.main import lifespan

    seed_sa_called = False
    seed_ng_called = False

    async def mock_seed_sa():
        nonlocal seed_sa_called
        seed_sa_called = True

    async def mock_seed_ng():
        nonlocal seed_ng_called
        seed_ng_called = True

    with (
        patch("app.main.engine", engine),
        patch("app.main._seed_superadmin", mock_seed_sa),
        patch("app.main._seed_not_grouped", mock_seed_ng),
        patch("app.main.ldap_group_sync_loop_enabled", return_value=False),
    ):
        mock_app = MagicMock()
        async with lifespan(mock_app):
            pass

    assert seed_sa_called
    assert seed_ng_called


# --- lifespan: ADR 0011 phase 5 interval-loop gating ---


@pytest.mark.asyncio
async def test_lifespan_starts_ldap_sync_loop_when_enabled():
    """gate true: the lifespan starts the loop task and cancels it cleanly on
    shutdown. ldap_sync_loop is stood in for by a fake that signals it ran
    and then blocks until cancelled, so the assertion proves the task was
    actually scheduled, not just that create_task would have been reachable.
    """
    from app.main import lifespan

    started = asyncio.Event()

    async def fake_loop(*args, **kwargs):
        started.set()
        await asyncio.Event().wait()

    with (
        patch("app.main.engine", engine),
        patch("app.main._seed_superadmin", AsyncMock()),
        patch("app.main._seed_not_grouped", AsyncMock()),
        patch("app.main.ldap_group_sync_loop_enabled", return_value=True),
        patch("app.main.ldap_sync_loop", fake_loop),
    ):
        mock_app = MagicMock()
        async with lifespan(mock_app):
            await asyncio.wait_for(started.wait(), timeout=5)

    assert started.is_set()


@pytest.mark.asyncio
async def test_lifespan_skips_ldap_sync_loop_when_disabled():
    """gate false (either auth_method != 'ldap' or the flag off, both
    collapsed into the helper's return value here since the helper itself is
    unit-tested directly in test_ldap_sync_loop.py): no task is created."""
    from app.main import lifespan

    with (
        patch("app.main.engine", engine),
        patch("app.main._seed_superadmin", AsyncMock()),
        patch("app.main._seed_not_grouped", AsyncMock()),
        patch("app.main.ldap_group_sync_loop_enabled", return_value=False),
        patch("app.main.ldap_sync_loop") as mock_loop,
    ):
        mock_app = MagicMock()
        async with lifespan(mock_app):
            pass

    mock_loop.assert_not_called()
