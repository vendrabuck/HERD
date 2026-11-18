"""A directory attribute value that is not text never becomes account data (issue #1448).

The invariant: a directory value that is not text (bytes that are not UTF-8, which
ldap3 returns as `bytes`; an empty value; a value holding a control character) is
never turned into an email, a username, a group name, a member DN, or a presence
entry. It counts as absent, so the rule for a missing attribute applies: login
refuses a missing email (401, `ldap_missing_email`) and falls back to the login name
for a missing username (IAM-LDAP-11); sync skips the member as `missing_email` or
`missing_username` (which suppresses the group's removals, IAM-SYNC-5); the group
name falls back to the DN; the enumeration leaves the value out. Each read that left
a value out logs one WARNING naming the attribute, the DN, and the value classes,
never the value. A login reads the attributes only after the user bind succeeded, so
none of this is reachable without the user's password.

Before the fix every consumer called `str()` on the raw value, so `b"\\xffcarol@..."`
was provisioned as the email `"b'\\\\xffcarol@...'"`.

Everything input-dependent runs through a real ldap3 `Connection` (`MOCK_SYNC`,
tests/_mock_directory.py), whose response decoding is what returns `bytes`.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

import pytest
from app.config import settings
from app.models.group import GroupMember, UserGroup
from app.models.ldap_group_mapping import LdapGroupMapping
from app.models.user import Role, User
from app.services import ldap_service, ldap_sync_service
from sqlalchemy import func, select

from tests import _mock_directory
from tests._harness import TestSessionLocal, mock_user

BASE_DN = _mock_directory.BASE_DN
PASSWORD = "right-password"
NOT_UTF8 = b"\xffcarol@example.com"
GROUP_DN = "cn=eng,dc=example,dc=com"

VALUES = {
    "str": ("carol@example.com", "carol@example.com"),
    "non_ascii_str": ("jörg@example.com", "jörg@example.com"),
    "astral_str": ("\U0001f600@example.com", "\U0001f600@example.com"),
    "no_break_space_str": ("a b", "a b"),
    "utf8_bytes": ("jörg".encode(), "jörg"),
    "not_utf8_bytes": (NOT_UTF8, None),
    "latin1_bytes": ("jörg".encode("latin-1"), None),
    "lone_surrogate_bytes": (b"\xed\xa0\x80", None),
    "empty_str": ("", None),
    "empty_bytes": (b"", None),
    "nul": ("car\x00ol", None),
    "nul_bytes": (b"car\x00ol", None),
    "tab": ("car\tol", None),
    "newline": ("carol\n", None),
    "escape": ("\x1b[31mcarol", None),
    "delete": ("carol\x7f", None),
    "c1_control": ("carol\x85", None),
    "int": (1001, None),
    "datetime": (datetime(2026, 1, 1, tzinfo=timezone.utc), None),
    "none": (None, None),
}


@pytest.fixture
def directory(monkeypatch):
    return _mock_directory.configure(monkeypatch, settings, ldap_service)


def _records(caplog) -> list[tuple]:
    return [
        (r.levelname, r.action, getattr(r, "attribute", None), getattr(r, "value_classes", None))
        for r in caplog.records
        if getattr(r, "action", None)
    ]


def _assert_no_value_leaks(caplog, *values: bytes) -> None:
    for record in caplog.records:
        for field in (record.getMessage(), *vars(record).values()):
            text = field if isinstance(field, str) else repr(field)
            assert "b'" not in text and "\\xff" not in text
            for value in values:
                assert value.decode("latin-1") not in text


# -- 1. The one rule ---------------------------------------------------------------------


@pytest.mark.parametrize("value,expected", VALUES.values(), ids=VALUES.keys())
def test_text_value_is_text_or_none(value, expected):
    assert ldap_service._text_value(value) == expected


def test_ldap3_returns_a_value_that_is_not_utf8_as_bytes(directory):
    """The premise: the real client hands the raw octets back, not text."""
    directory.add_user("carol", PASSWORD, mail=[NOT_UTF8, "carol@example.com"])
    conn = directory.connection(None, user=_mock_directory.SERVICE_DN, password="svc-pw")
    conn.bind()
    conn.search(BASE_DN, "(uid=carol)", attributes=["mail"])
    assert conn.entries[0]["mail"].values == [NOT_UTF8, "carol@example.com"]


# -- 2. Login ----------------------------------------------------------------------------


def test_a_login_whose_only_email_is_not_text_is_refused(directory, caplog):
    directory.add_user("carol", PASSWORD, mail=NOT_UTF8)
    with caplog.at_level(logging.INFO, logger="app.services.ldap_service"):
        assert ldap_service._bind_user_sync("carol", PASSWORD) is None
    assert _records(caplog) == [
        ("WARNING", "ldap_attribute_not_text", "mail", ["bytes"]),
        ("WARNING", "ldap_missing_email", None, None),
    ]
    _assert_no_value_leaks(caplog, NOT_UTF8)


@pytest.mark.parametrize("mail", ["", "carol\x00@example.com", "carol@example.com\n"])
def test_a_login_whose_email_holds_no_usable_text_is_refused(directory, mail):
    directory.add_user("carol", PASSWORD, mail=mail)
    assert ldap_service._bind_user_sync("carol", PASSWORD) is None


@pytest.mark.parametrize(
    "mail",
    [[NOT_UTF8, "carol@example.com"], ["carol@example.com", NOT_UTF8]],
    ids=["binary_first", "binary_second"],
)
def test_the_first_text_email_is_used(directory, caplog, mail):
    directory.add_user("carol", PASSWORD, mail=mail)
    with caplog.at_level(logging.WARNING, logger="app.services.ldap_service"):
        identity = ldap_service._bind_user_sync("carol", PASSWORD)
    assert identity is not None and identity.email == "carol@example.com"
    assert _records(caplog) == [("WARNING", "ldap_attribute_not_text", "mail", ["bytes"])]


def test_a_username_that_is_not_text_falls_back_to_the_login_name(directory, monkeypatch):
    monkeypatch.setattr(settings, "ldap_username_attribute", "displayName")
    directory.add_user("dave", PASSWORD, displayName=b"\xfedave")
    identity = ldap_service._bind_user_sync("dave", PASSWORD)
    assert identity is not None
    assert (identity.username, identity.email) == ("dave", "dave@example.com")


def test_a_missing_username_falls_back_to_the_login_name(directory, monkeypatch):
    """IAM-LDAP-11, which had no test."""
    monkeypatch.setattr(settings, "ldap_username_attribute", "displayName")
    directory.add_user("erin", PASSWORD)
    identity = ldap_service._bind_user_sync("erin", PASSWORD)
    assert identity is not None and identity.username == "erin"


def test_a_text_username_attribute_is_used(directory, monkeypatch):
    monkeypatch.setattr(settings, "ldap_username_attribute", "displayName")
    directory.add_user("frank", PASSWORD, displayName="Frank.Directory")
    identity = ldap_service._bind_user_sync("frank", PASSWORD)
    assert identity is not None and identity.username == "Frank.Directory"


def test_the_attributes_are_read_only_after_the_user_bind(directory, caplog):
    """No oracle: with a wrong password the binary entry logs exactly what a
    normal entry logs; the attribute problem is reachable only with the password."""
    directory.add_user("carol", PASSWORD, mail=NOT_UTF8)
    directory.add_user("alice", PASSWORD)
    shapes = []
    for name in ("carol", "alice"):
        caplog.clear()
        with caplog.at_level(logging.INFO, logger="app.services.ldap_service"):
            assert ldap_service._bind_user_sync(name, "wrong-password") is None
        shapes.append([(r.levelname, r.action) for r in caplog.records])
    assert shapes[0] == shapes[1] == [("WARNING", "ldap_bind_failure")]


# -- 3. Login through the route --------------------------------------------------------------


@pytest.fixture
async def client(make_client):
    async with make_client() as ac:
        yield ac


async def _users() -> list[tuple[str, str]]:
    async with TestSessionLocal() as db:
        return [(u.username, u.email) for u in (await db.execute(select(User))).scalars()]


async def test_the_route_refuses_an_email_that_is_not_text_and_creates_nothing(client, directory):
    directory.add_user("carol", PASSWORD, mail=NOT_UTF8)
    response = await client.post("/login", json={"email": "carol", "password": PASSWORD})
    assert (response.status_code, response.json()) == (401, {"detail": "Invalid credentials"})
    assert await _users() == []


async def test_the_route_provisions_only_text(client, directory, monkeypatch):
    monkeypatch.setattr(settings, "ldap_username_attribute", "displayName")
    directory.add_user("dave", PASSWORD, mail=[NOT_UTF8, "dave@example.com"], displayName=b"\xfe")
    response = await client.post("/login", json={"email": "dave", "password": PASSWORD})
    assert response.status_code == 200, response.text
    assert await _users() == [("dave", "dave@example.com")]


# -- 4. Sync: member resolution, group fetch, enumeration --------------------------------------


MEMBERS = {
    "email_not_text": ({"mail": NOT_UTF8}, ldap_service.MEMBER_SKIP_MISSING_EMAIL),
    "email_empty": ({"mail": ""}, ldap_service.MEMBER_SKIP_MISSING_EMAIL),
    "username_not_text": ({"uid": b"\xfeg"}, ldap_service.MEMBER_SKIP_MISSING_USERNAME),
    "username_with_nul": ({"uid": "g\x00"}, ldap_service.MEMBER_SKIP_MISSING_USERNAME),
    "both_not_text": ({"mail": NOT_UTF8, "uid": b"\xfeg"}, ldap_service.MEMBER_SKIP_MISSING_EMAIL),
}


@pytest.mark.parametrize("attributes,reason", MEMBERS.values(), ids=MEMBERS.keys())
def test_a_member_whose_identity_is_not_text_is_skipped_as_missing(
    directory, caplog, attributes, reason
):
    dn = directory.add_entry(
        f"cn=gina,{BASE_DN}",
        {"objectClass": "person", "uid": "gina", "mail": "gina@example.com", **attributes},
    )
    with caplog.at_level(logging.WARNING, logger="app.services.ldap_service"):
        resolution = ldap_service._resolve_member_sync(dn)
    assert resolution == ldap_service.LdapMemberResolution(None, reason)
    assert all(r.action == "ldap_attribute_not_text" for r in caplog.records)
    assert caplog.records, "the value left out is logged"
    _assert_no_value_leaks(caplog, NOT_UTF8)


def test_a_member_with_a_text_value_after_a_binary_one_resolves(directory):
    dn = directory.add_user("hank", PASSWORD, mail=[NOT_UTF8, "hank@example.com"])
    resolution = ldap_service._resolve_member_sync(dn)
    assert resolution.identity == ldap_service.LdapIdentity("hank", "hank@example.com", dn)


def test_a_group_name_or_member_that_is_not_text_is_left_out(directory, monkeypatch, caplog):
    # The name attribute is not the RDN's: an entry always holds its RDN value
    # (cn=eng puts "eng" in cn), which would be the first text value.
    monkeypatch.setattr(settings, "ldap_group_name_attribute", "displayName")
    alice = directory.add_user("alice", PASSWORD)
    directory.add_entry(
        GROUP_DN,
        {"objectClass": "groupOfNames", "displayName": b"\xffeng", "member": [b"\xff", alice]},
    )
    with caplog.at_level(logging.WARNING, logger="app.services.ldap_service"):
        entry = ldap_service._fetch_group_sync(GROUP_DN)
    assert entry == ldap_service.LdapGroupEntry(dn=GROUP_DN, name=GROUP_DN, member_dns=(alice,))
    assert [(r.action, getattr(r, "attribute", None)) for r in caplog.records] == [
        ("ldap_attribute_not_text", "displayName"),
        ("ldap_group_missing_name", None),
        ("ldap_attribute_not_text", "member"),
    ]


def test_the_enumeration_leaves_out_what_is_not_text_with_one_line(directory, caplog):
    directory.add_user("alice", PASSWORD, mail="Alice@Example.com")
    directory.add_user("carol", PASSWORD, mail=NOT_UTF8)
    directory.add_user("dave", PASSWORD, mail=[b"\xfe", "dave@example.com", "d\x00@x"])
    with caplog.at_level(logging.WARNING, logger="app.services.ldap_service"):
        emails = ldap_service._present_emails_sync()
    assert emails == frozenset({"alice@example.com", "dave@example.com"})
    assert [(r.action, r.count, r.value_classes) for r in caplog.records] == [
        ("ldap_attribute_not_text", 3, ["bytes", "str"])
    ]
    _assert_no_value_leaks(caplog, NOT_UTF8)


# -- 5. Sync and mapping validation end to end -------------------------------------------------


@pytest.fixture
def _reap_on_the_harness_engine(use_reap_session_factory):
    use_reap_session_factory(TestSessionLocal)


async def test_a_sync_pass_never_provisions_a_bytes_repr_and_keeps_removals_suppressed(
    directory, _reap_on_the_harness_engine
):
    alice = directory.add_user("alice", PASSWORD)
    carol = directory.add_user("carol", PASSWORD, mail=NOT_UTF8)
    dave = directory.add_entry(
        f"cn=dave,{BASE_DN}", {"objectClass": "person", "mail": "dave@example.com"}
    )
    directory.add_entry(
        GROUP_DN, {"objectClass": "groupOfNames", "cn": "eng", "member": [alice, carol, dave]}
    )
    async with TestSessionLocal() as db:
        group = UserGroup(name="Engineering")
        stale = User(
            email="stale@example.com", username="stale", auth_source="ldap", role=Role.USER
        )
        db.add_all([group, stale])
        await db.flush()
        db.add(GroupMember(group_id=group.id, user_id=stale.id))
        db.add(LdapGroupMapping(group_dn=GROUP_DN, directory_name="eng", herd_group_id=group.id))
        await db.commit()
        run = await ldap_sync_service.run_sync(db)
    assert run.members_added == 1
    assert run.members_removed == 0, "a missing_email skip suppresses removals (IAM-SYNC-5)"
    skips = {(s["member_dn"], s["reason"]) for s in run.detail["skipped_members"]}
    assert skips == {(carol, "missing_email"), (dave, "missing_username")}
    users = await _users()
    assert sorted(users) == [("alice", "alice@example.com"), ("stale", "stale@example.com")]


async def test_a_mapping_of_a_group_whose_name_is_not_text_caches_the_dn(
    make_client, directory, monkeypatch
):
    monkeypatch.setattr(settings, "ldap_group_name_attribute", "displayName")
    directory.add_entry(
        GROUP_DN, {"objectClass": "groupOfNames", "displayName": b"\xffeng", "member": b"\xff"}
    )
    async with make_client(mock_user(Role.ADMIN, username="admin")) as admin:
        group = await admin.post("/groups", json={"name": "Engineering", "description": None})
        response = await admin.post(
            "/admin/ldap-sync/mappings",
            json={"group_dn": GROUP_DN, "herd_group_id": group.json()["id"]},
        )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["directory_name"] == GROUP_DN
    assert body["warning"] is not None, "no text member is the empty-group warning"
    async with TestSessionLocal() as db:
        count = (await db.execute(select(func.count()).select_from(User))).scalar_one()
    assert count == 0
