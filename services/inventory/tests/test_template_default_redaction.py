"""Template reads mask password-field defaults for non-admin callers (INV-RED-5).

A device created without a value for a password field inherits the field's
``default``, so the default is the same secret the device reads already mask
(INV-RED-1). These tests pin the template side of that rule: a non-admin read
of ``GET /templates`` or ``GET /templates/{id}`` returns ``********`` for a
password field's non-empty default, an empty or null default and every
non-password default are returned as stored, admin, superadmin, and internal
reads return the clear value, and the stored sections are never rewritten.
The helper is also exercised directly.
"""

import copy

import pytest
from app.database import Base, get_db
from app.dependencies.auth import get_current_user_payload
from app.main import app
from app.services.field_redaction import REDACTED_VALUE, redact_template_sections
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

TEST_DATABASE_URL = "sqlite+aiosqlite:///:memory:"

engine = create_async_engine(TEST_DATABASE_URL, echo=False)
TestSessionLocal = async_sessionmaker(engine, expire_on_commit=False)

_INTERNAL = {"X-Internal-Token": "test-token"}
_SHARED_SECRET = "lab-shared-pw"


async def override_get_db():
    async with TestSessionLocal() as session:
        yield session


def override_auth_admin():
    return {"sub": "00000000-0000-0000-0000-000000000001", "username": "testadmin", "role": "admin"}


def override_auth_superadmin():
    return {
        "sub": "00000000-0000-0000-0000-000000000003",
        "username": "testsuper",
        "role": "superadmin",
    }


def override_auth_user():
    return {"sub": "00000000-0000-0000-0000-000000000002", "username": "testuser", "role": "user"}


def override_auth_no_role():
    return {"sub": "00000000-0000-0000-0000-000000000004", "username": "norole"}


@pytest.fixture(autouse=True)
async def setup_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


@pytest.fixture
async def client():
    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_current_user_payload] = override_auth_admin
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac
    app.dependency_overrides.clear()


_SECTIONS = [
    {
        "name": "Access",
        "fields": [
            {"key": "login", "label": "Login", "type": "string", "default": "admin"},
            {"key": "password", "label": "Password", "type": "password", "default": _SHARED_SECRET},
            {"key": "enable_secret", "label": "Enable", "type": "password", "default": ""},
            {"key": "console_pw", "label": "Console", "type": "password"},
        ],
    },
    {
        "name": "Out of band",
        "fields": [
            {"key": "oob_pw", "label": "OOB", "type": "password", "default": "oob-secret"},
            {"key": "oob_port", "label": "OOB port", "type": "number", "default": 22},
        ],
    },
]


async def _create_template(client) -> str:
    resp = await client.post(
        "/templates",
        json={"name": "Shared credential port", "template_type": "port", "sections": _SECTIONS},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def _fields(template_json: dict) -> dict[str, dict]:
    return {f["key"]: f for s in template_json["sections"] for f in s["fields"]}


def _assert_masked(template_json: dict) -> None:
    fields = _fields(template_json)
    assert fields["password"]["default"] == REDACTED_VALUE
    assert fields["oob_pw"]["default"] == REDACTED_VALUE
    # An empty or absent default stays as it is: a mask always means one is set.
    assert fields["enable_secret"]["default"] == ""
    assert fields["console_pw"]["default"] is None
    # Non-password defaults are untouched, and so are the keys and types.
    assert fields["login"]["default"] == "admin"
    assert fields["oob_port"]["default"] == 22
    assert fields["password"]["type"] == "password"
    assert _SHARED_SECRET not in str(template_json)


def _assert_clear(template_json: dict) -> None:
    fields = _fields(template_json)
    assert fields["password"]["default"] == _SHARED_SECRET
    assert fields["oob_pw"]["default"] == "oob-secret"
    assert fields["enable_secret"]["default"] == ""


@pytest.mark.asyncio
async def test_non_admin_get_template_masks_password_defaults(client):
    tid = await _create_template(client)
    app.dependency_overrides[get_current_user_payload] = override_auth_user
    resp = await client.get(f"/templates/{tid}")
    assert resp.status_code == 200
    _assert_masked(resp.json())


@pytest.mark.asyncio
async def test_non_admin_list_templates_masks_password_defaults(client):
    tid = await _create_template(client)
    app.dependency_overrides[get_current_user_payload] = override_auth_user
    resp = await client.get("/templates")
    assert resp.status_code == 200
    items = [t for t in resp.json()["items"] if t["id"] == tid]
    assert len(items) == 1
    _assert_masked(items[0])


@pytest.mark.asyncio
async def test_template_read_without_a_role_claim_is_masked(client):
    """A payload with no role counts as a user, as on the device reads."""
    tid = await _create_template(client)
    app.dependency_overrides[get_current_user_payload] = override_auth_no_role
    resp = await client.get(f"/templates/{tid}")
    assert resp.status_code == 200
    _assert_masked(resp.json())


@pytest.mark.asyncio
@pytest.mark.parametrize("override", [override_auth_admin, override_auth_superadmin])
async def test_admin_template_reads_return_password_defaults_in_clear(client, override):
    tid = await _create_template(client)
    app.dependency_overrides[get_current_user_payload] = override
    single = await client.get(f"/templates/{tid}")
    assert single.status_code == 200
    _assert_clear(single.json())
    listed = await client.get("/templates")
    assert listed.status_code == 200
    _assert_clear(next(t for t in listed.json()["items"] if t["id"] == tid))


@pytest.mark.asyncio
async def test_internal_template_read_returns_password_defaults_in_clear(client):
    tid = await _create_template(client)
    resp = await client.get(f"/templates/{tid}/internal", headers=_INTERNAL)
    assert resp.status_code == 200
    _assert_clear(resp.json())


@pytest.mark.asyncio
async def test_non_admin_read_does_not_rewrite_the_stored_default(client):
    """A masked read leaves the stored sections alone: a later admin read and a
    device created afterwards both see the real default."""
    tid = await _create_template(client)
    app.dependency_overrides[get_current_user_payload] = override_auth_user
    assert (await client.get(f"/templates/{tid}")).status_code == 200
    assert (await client.get("/templates")).status_code == 200

    app.dependency_overrides[get_current_user_payload] = override_auth_admin
    resp = await client.get(f"/templates/{tid}")
    _assert_clear(resp.json())
    internal = await client.get(f"/templates/{tid}/internal", headers=_INTERNAL)
    _assert_clear(internal.json())


# The helper, directly.


def test_redact_template_sections_masks_only_non_empty_password_defaults():
    out = redact_template_sections(_SECTIONS)
    fields = {f["key"]: f for s in out for f in s["fields"]}
    assert fields["password"]["default"] == REDACTED_VALUE
    assert fields["oob_pw"]["default"] == REDACTED_VALUE
    assert fields["enable_secret"]["default"] == ""
    assert "default" not in fields["console_pw"]
    assert fields["login"]["default"] == "admin"
    assert fields["oob_port"]["default"] == 22


def test_redact_template_sections_never_mutates_its_input():
    original = copy.deepcopy(_SECTIONS)
    out = redact_template_sections(_SECTIONS)
    assert _SECTIONS == original
    assert out is not _SECTIONS
    for section_out, section_in in zip(out, _SECTIONS, strict=True):
        assert section_out is not section_in
        assert section_out["fields"] is not section_in["fields"]
        for field_out, field_in in zip(section_out["fields"], section_in["fields"], strict=True):
            assert field_out is not field_in


def test_redact_template_sections_copies_even_with_no_password_field():
    sections = [{"name": "S", "fields": [{"key": "a", "type": "string", "default": "x"}]}]
    out = redact_template_sections(sections)
    assert out == sections
    assert out is not sections
    assert out[0]["fields"][0] is not sections[0]["fields"][0]


@pytest.mark.parametrize("sections", [None, []])
def test_redact_template_sections_empty_input(sections):
    assert redact_template_sections(sections) == []


def test_redact_template_sections_tolerates_malformed_entries():
    """Malformed sections and fields pass through rather than breaking a read."""
    sections = [
        "not a section",
        {"name": "no fields"},
        {"name": "fields not a list", "fields": "x"},
        {"name": "odd fields", "fields": ["not a field", {"type": "password", "default": 7}]},
        {"name": "ok", "fields": [{"key": "p", "type": "password", "default": "s"}]},
    ]
    original = copy.deepcopy(sections)
    out = redact_template_sections(sections)
    assert sections == original
    assert out[:3] == original[:3]
    # A non-string default on a password field is not a stored secret shape the
    # schema accepts; it is returned as stored.
    assert out[3] == original[3]
    assert out[4]["fields"][0]["default"] == REDACTED_VALUE
