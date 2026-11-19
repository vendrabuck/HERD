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
    # The review's tightening: white space alone, invisible format characters,
    # separators, and noncharacters are no address and no name.
    "space_only": (" ", None),
    "no_break_space_only": ("\u00a0", None),
    "spaces_and_tab": (" \u3000 ", None),
    "right_to_left_override": ("carol\u202e@example.com", None),
    "zero_width_space": ("car\u200bol", None),
    "zero_width_non_joiner_alone": ("\u200c", None),
    "zero_width_non_joiner_first": ("\u200c\u0645\u06cc", None),
    "zero_width_non_joiner_last": ("\u0645\u06cc\u200c", None),
    "zero_width_joiner_last": ("\u0915\u094d\u200d", None),
    "zero_width_non_joiner_after_a_digit": ("1\u200c\u0645", None),
    "zero_width_non_joiner_before_a_space": ("\u0645\u200c \u06cc", None),
    "zero_width_joiner_in_an_emoji_sequence": ("\U0001f468\u200d\U0001f469", None),
    "left_to_right_embedding": ("carol\u202a", None),
    "right_to_left_isolate": ("\u2067carol\u2069", None),
    "word_joiner": ("car\u2060ol", None),
    "byte_order_mark": ("\ufeffcarol", None),
    "soft_hyphen": ("car\u00adol", None),
    "line_separator": ("carol\u2028", None),
    "paragraph_separator": ("carol\u2029", None),
    "noncharacter_fffe": ("carol\ufffe", None),
    "noncharacter_fdd0": ("carol\ufdd0", None),
    "noncharacter_plane_1": ("carol\U0001ffff", None),
    # Ordinary international names and addresses still pass, as given.
    "accented_name": ("Jos\u00e9 M\u00fcller", "Jos\u00e9 M\u00fcller"),
    "cjk_name": ("\u674e\u5c0f\u9f99", "\u674e\u5c0f\u9f99"),
    "greek_name": ("\u0395\u03bb\u03ad\u03bd\u03b7", "\u0395\u03bb\u03ad\u03bd\u03b7"),
    "arabic_name": ("\u0639\u0644\u064a", "\u0639\u0644\u064a"),
    "devanagari_with_marks": (
        "\u0928\u092e\u0938\u094d\u0924\u0947",
        "\u0928\u092e\u0938\u094d\u0924\u0947",
    ),
    "apostrophe_email": ("o'brien@example.com", "o'brien@example.com"),
    "plus_email": ("user+tag@example.com", "user+tag@example.com"),
    "idn_email": ("j\u00f6rg@b\u00fccher.example", "j\u00f6rg@b\u00fccher.example"),
    "inner_space": ("Mary Ann", "Mary Ann"),
    # The zero width non-joiner and joiner between letters or marks (Persian,
    # Indic scripts) are text.
    "persian_with_zero_width_non_joiner": (
        "\u0645\u06cc\u200c\u062e\u0648\u0627\u0647\u0645",
        "\u0645\u06cc\u200c\u062e\u0648\u0627\u0647\u0645",
    ),
    "devanagari_conjunct_with_zero_width_joiner": (
        "\u0915\u094d\u200d\u0937",
        "\u0915\u094d\u200d\u0937",
    ),
    "latin_letters_around_a_joiner": ("car\u200col", "car\u200col"),
    "surrounding_space_kept": (" carol ", " carol "),
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


def test_the_bounds_are_the_columns():
    from app.models.user import User

    assert ldap_service.EMAIL_MAX_LENGTH == User.__table__.c.email.type.length == 255
    assert ldap_service.USERNAME_MAX_LENGTH == User.__table__.c.username.type.length == 100


@pytest.mark.parametrize("bound", [100, 255])
def test_a_value_longer_than_its_column_is_not_text(bound):
    assert ldap_service._text_value("a" * bound, bound) == "a" * bound
    assert ldap_service._text_value("a" * (bound + 1), bound) is None
    assert ldap_service._text_value("\u00e9" * bound, bound) == "\u00e9" * bound


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


def test_an_email_longer_than_the_column_is_refused(directory, caplog):
    directory.add_user("carol", PASSWORD, mail="c" * 244 + "@example.com")
    with caplog.at_level(logging.WARNING, logger="app.services.ldap_service"):
        assert ldap_service._bind_user_sync("carol", PASSWORD) is None
    assert [r.action for r in caplog.records] == ["ldap_attribute_not_text", "ldap_missing_email"]


def test_two_entries_whose_email_is_a_space_never_share_an_account(directory):
    directory.add_user("carol", PASSWORD, mail=" ")
    directory.add_user("dave", PASSWORD, mail="\u00a0")
    assert ldap_service._bind_user_sync("carol", PASSWORD) is None
    assert ldap_service._bind_user_sync("dave", PASSWORD) is None


def test_a_username_longer_than_the_column_falls_back_to_the_login_name(directory, monkeypatch):
    monkeypatch.setattr(settings, "ldap_username_attribute", "displayName")
    directory.add_user("dave", PASSWORD, displayName="d" * 101)
    identity = ldap_service._bind_user_sync("dave", PASSWORD)
    assert identity is not None and identity.username == "dave"


@pytest.mark.parametrize("login_name", ["u" * 101, "us\u202eer"], ids=["too_long", "refused_char"])
def test_a_login_name_that_cannot_stand_in_gives_no_username(directory, monkeypatch, login_name):
    """No usable username attribute and a login name that is not usable text:
    the identity carries no username, and only provisioning refuses it."""
    monkeypatch.setattr(settings, "ldap_username_attribute", "displayName")
    directory.add_user(login_name, PASSWORD, mail="nameless@example.com")
    identity = ldap_service._bind_user_sync(login_name, PASSWORD)
    assert identity is not None
    assert (identity.username, identity.email) == (None, "nameless@example.com")


async def test_without_a_username_a_new_account_is_refused_and_a_returning_one_signs_in(
    client, directory, monkeypatch, caplog
):
    """Issue #1448 review: a returning user is found by email and needs no
    username, so only a login that would provision a new account is refused.
    Both outcomes come after a successful bind, so neither tells whether an
    account exists to anyone without the password."""
    monkeypatch.setattr(settings, "ldap_username_attribute", "displayName")
    new_name, old_name = "n" * 101, "o" * 101
    directory.add_user(new_name, PASSWORD)
    directory.add_user(old_name, PASSWORD)
    async with TestSessionLocal() as db:
        db.add(
            User(
                email=f"{old_name}@example.com",
                username="old-account",
                auth_source="ldap",
                role=Role.USER,
            )
        )
        await db.commit()
    with caplog.at_level(logging.WARNING, logger="app.services.auth_service"):
        refused = await client.post("/login", json={"email": new_name, "password": PASSWORD})
    assert [getattr(r, "reason", None) for r in caplog.records] == ["missing_username"]
    returning = await client.post("/login", json={"email": old_name, "password": PASSWORD})
    assert (refused.status_code, refused.json()) == (401, {"detail": "Invalid credentials"})
    assert returning.status_code == 200, returning.text
    assert await _users() == [("old-account", f"{old_name}@example.com")]


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
    "email_space": ({"mail": " "}, ldap_service.MEMBER_SKIP_MISSING_EMAIL),
    "email_too_long": ({"mail": "g" * 256}, ldap_service.MEMBER_SKIP_MISSING_EMAIL),
    "username_too_long": ({"uid": "g" * 101}, ldap_service.MEMBER_SKIP_MISSING_USERNAME),
    "username_zero_width": ({"uid": "gi\u200bna"}, ldap_service.MEMBER_SKIP_MISSING_USERNAME),
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
    assert entry == ldap_service.LdapGroupEntry(
        dn=GROUP_DN, name=GROUP_DN, member_dns=(alice,), unreadable_members=1
    )
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
    # carol and dave each have a value that cannot be read (dave beside a
    # readable one); both have a readable username.
    assert emails.unreadable_entries == 2
    assert emails.held_usernames == {"carol", "dave"}
    assert emails.unidentified_entries == 0
    assert [(r.action, r.count, r.value_classes) for r in caplog.records] == [
        ("ldap_attribute_not_text", 3, ["bytes", "str"])
    ]
    _assert_no_value_leaks(caplog, NOT_UTF8)


def test_an_enumeration_with_every_email_readable_counts_none(directory):
    directory.add_user("alice", PASSWORD)
    assert ldap_service._present_emails_sync().unreadable_entries == 0


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


async def _seed_ldap_accounts(db, *specs) -> dict[str, User]:
    users = {}
    for username, email, active, by_sync in specs:
        user = User(
            email=email,
            username=username,
            auth_source="ldap",
            role=Role.USER,
            is_active=active,
            deactivated_by_sync=by_sync,
        )
        db.add(user)
        users[username] = user
    await db.flush()
    return users


async def test_member_values_that_are_not_text_are_skipped_and_suppress_removals(
    directory, _reap_on_the_harness_engine
):
    """The review's case: a group whose only two member values are not text,
    with two HERD members. Before the fix round the run was `success`, removed
    both, and recorded nothing."""
    directory.add_entry(
        GROUP_DN, {"objectClass": "groupOfNames", "cn": "eng", "member": [b"\xff1", b"\xff2"]}
    )
    async with TestSessionLocal() as db:
        group = UserGroup(name="Engineering")
        db.add(group)
        users = await _seed_ldap_accounts(
            db, ("ann", "ann@example.com", True, False), ("ben", "ben@example.com", True, False)
        )
        for user in users.values():
            db.add(GroupMember(group_id=group.id, user_id=user.id))
        db.add(LdapGroupMapping(group_dn=GROUP_DN, directory_name="eng", herd_group_id=group.id))
        await db.commit()
        run = await ldap_sync_service.run_sync(db)
        members = (
            await db.execute(
                select(func.count())
                .select_from(GroupMember)
                .where(GroupMember.group_id == group.id)
            )
        ).scalar_one()
    assert run.status == "partial"
    assert (run.members_removed, run.members_skipped) == (0, 2)
    assert [s["reason"] for s in run.detail["skipped_members"]] == ["not_text", "not_text"]
    assert all(
        s["member_dn"] == ldap_sync_service.MEMBER_DN_NOT_TEXT
        for s in run.detail["skipped_members"]
    )
    assert run.detail["suppressed_removals"] == [
        {"group_dn": GROUP_DN, "unresolved": 2, "would_remove": 2}
    ]
    assert members == 2


async def _sweep(directory, monkeypatch, *, carol_mail) -> tuple:
    monkeypatch.setattr(settings, "ldap_sync_deactivation_enabled", True, raising=False)
    monkeypatch.setattr(settings, "ldap_sync_deactivation_min_count", 10, raising=False)
    directory.add_user("alice", PASSWORD)
    directory.add_user("carol", PASSWORD, mail=carol_mail)
    async with TestSessionLocal() as db:
        await _seed_ldap_accounts(
            db,
            ("alice", "alice@example.com", True, False),
            # Provisioned before the fix from carol's binary mail.
            ("carol", "b'\\xffcarol@example.com'", True, False),
            ("gone", "gone@example.com", True, False),
            ("back", "alice2@example.com", False, True),
        )
        await db.commit()
        run = await ldap_sync_service.run_sync(db)
        rows = {u.username: u.is_active for u in (await db.execute(select(User))).scalars()}
    return run, rows


async def test_an_unreadable_email_holds_back_only_the_account_of_that_username(
    directory, monkeypatch, caplog, _reap_on_the_harness_engine
):
    """carol is listed but her email cannot be read; her username can, so only
    the account named carol is held back, and the departed account still goes."""
    with caplog.at_level(logging.ERROR, logger="app.services.ldap_sync_service"):
        run, rows = await _sweep(directory, monkeypatch, carol_mail=NOT_UTF8)
    assert rows == {"alice": True, "carol": True, "gone": False, "back": False}
    assert run.status == "partial"
    assert run.users_deactivated == 1
    assert run.detail["sweep"] == [
        {
            "reason": "email_unreadable",
            "unreadable_entries": 1,
            "unidentified_entries": 0,
            "held": 1,
            "held_passes": 1,
        }
    ]
    assert [(r.levelname, r.action, r.held) for r in caplog.records] == [
        ("ERROR", "ldap_sync_deactivation_held", 1)
    ]


@pytest.mark.parametrize(
    "old_value,stored",
    [
        (NOT_UTF8, "b'\\xffcarol@example.com'"),
        ("carol@exam\u200bple.com", "carol@exam\u200bple.com"),
        ("carol@example.com\t", "carol@example.com\t"),
    ],
    ids=["binary", "zero_width_space", "trailing_tab"],
)
async def test_an_unreadable_value_beside_a_readable_one_still_holds(
    directory, monkeypatch, _reap_on_the_harness_engine, old_value, stored
):
    """The review's case: carol's mail is [old, alias] where old cannot be read
    and is what her account holds. Before the fix the readable alias made the
    entry count as readable, so carol was deactivated and the run said success."""
    monkeypatch.setattr(settings, "ldap_sync_deactivation_enabled", True, raising=False)
    directory.add_user("carol", PASSWORD, mail=[old_value, "carol.alias@example.com"])
    async with TestSessionLocal() as db:
        await _seed_ldap_accounts(db, ("carol", stored, True, False))
        await db.commit()
        run = await ldap_sync_service.run_sync(db)
        carol = (await db.execute(select(User))).scalars().one()
    assert carol.is_active is True
    assert run.status == "partial"
    assert run.detail["sweep"][0]["held"] == 1


def _unidentified(directory, name: str, mail) -> None:
    """An entry with no readable username: cn in the RDN, uid not text."""
    directory.add_entry(
        f"cn={name},{BASE_DN}", {"objectClass": "person", "uid": b"\xff", "mail": mail}
    )


async def test_an_entry_with_neither_attribute_readable_holds_every_unproven_one(
    directory, monkeypatch, _reap_on_the_harness_engine
):
    """mallory's mail ends in a tab and her uid is binary, so nothing ties her
    entry to an account: every deactivation proven only by absence is held.
    dan's, proven by the disabled filter, still applies, and additions and
    reactivations are unaffected."""
    monkeypatch.setattr(settings, "ldap_sync_deactivation_enabled", True, raising=False)
    monkeypatch.setattr(settings, "ldap_disabled_filter", "(employeeType=disabled)")
    alice = directory.add_user("alice", PASSWORD)
    directory.add_user("dan", PASSWORD, employeeType="disabled")
    directory.add_user("back", PASSWORD)
    _unidentified(directory, "mallory", "mallory@example.com\t")
    directory.add_entry(GROUP_DN, {"objectClass": "groupOfNames", "cn": "eng", "member": alice})
    async with TestSessionLocal() as db:
        group = UserGroup(name="Engineering")
        db.add(group)
        await _seed_ldap_accounts(
            db,
            ("dan", "dan@example.com", True, False),
            ("gone", "gone@example.com", True, False),
            ("back", "back@example.com", False, True),
        )
        db.add(LdapGroupMapping(group_dn=GROUP_DN, directory_name="eng", herd_group_id=group.id))
        await db.commit()
        run = await ldap_sync_service.run_sync(db)
        rows = {u.username: u.is_active for u in (await db.execute(select(User))).scalars()}
    assert rows == {"dan": False, "gone": True, "back": True, "alice": True}
    assert (run.users_deactivated, run.users_reactivated, run.members_added) == (1, 1, 1)
    assert run.detail["sweep"][0] == {
        "reason": "email_unreadable",
        "unreadable_entries": 1,
        "unidentified_entries": 1,
        "held": 1,
        "held_passes": 1,
    }


async def test_a_disabled_proven_deactivation_applies_under_a_narrow_hold(
    directory, monkeypatch, _reap_on_the_harness_engine
):
    """The review's repro: mallory's mail ends in a tab (her uid is readable),
    and dan matches the disabled filter: dan is deactivated."""
    monkeypatch.setattr(settings, "ldap_sync_deactivation_enabled", True, raising=False)
    monkeypatch.setattr(settings, "ldap_disabled_filter", "(employeeType=disabled)")
    directory.add_user("dan", PASSWORD, employeeType="disabled")
    directory.add_user("mallory", PASSWORD, mail="mallory@example.com\t")
    async with TestSessionLocal() as db:
        await _seed_ldap_accounts(
            db,
            ("dan", "dan@example.com", True, False),
            ("mallory", "mallory@example.com", True, False),
        )
        await db.commit()
        run = await ldap_sync_service.run_sync(db)
        rows = {u.username: u.is_active for u in (await db.execute(select(User))).scalars()}
    assert rows == {"dan": False, "mallory": True}
    assert run.users_deactivated == 1


async def test_a_hold_that_lasts_counts_its_passes(
    directory, monkeypatch, _reap_on_the_harness_engine
):
    monkeypatch.setattr(settings, "ldap_sync_deactivation_enabled", True, raising=False)
    _unidentified(directory, "mallory", "mallory@example.com\t")
    async with TestSessionLocal() as db:
        await _seed_ldap_accounts(db, ("gone", "gone@example.com", True, False))
        await db.commit()
        passes = []
        for _ in range(3):
            run = await ldap_sync_service.run_sync(db)
            passes.append(run.detail["sweep"][0]["held_passes"])
    assert passes == [1, 2, 3]


async def test_an_unreadable_entry_that_holds_no_one_is_still_recorded(
    directory, monkeypatch, _reap_on_the_harness_engine
):
    monkeypatch.setattr(settings, "ldap_sync_deactivation_enabled", True, raising=False)
    directory.add_user("carol", PASSWORD, mail=NOT_UTF8)
    async with TestSessionLocal() as db:
        await _seed_ldap_accounts(db, ("gone", "gone@example.com", True, False))
        await db.commit()
        run = await ldap_sync_service.run_sync(db)
    assert run.users_deactivated == 1
    assert run.status == "partial"
    assert run.detail["sweep"][0]["held"] == 0
    assert run.detail["sweep"][0]["held_passes"] == 0


async def test_a_readable_directory_still_deactivates_the_absent(
    directory, monkeypatch, _reap_on_the_harness_engine
):
    """The control: with carol's email readable, the sweep proves absence as
    before, so the repr-email account and the departed account go."""
    run, rows = await _sweep(directory, monkeypatch, carol_mail="carol@example.com")
    assert rows == {"alice": True, "carol": False, "gone": False, "back": False}
    assert run.users_deactivated == 2


async def test_reactivations_still_apply_when_deactivations_are_suppressed(
    directory, monkeypatch, _reap_on_the_harness_engine
):
    directory.add_user("back", PASSWORD, mail="back@example.com")
    monkeypatch.setattr(settings, "ldap_sync_deactivation_enabled", True, raising=False)
    directory.add_user("carol", PASSWORD, mail=NOT_UTF8)
    async with TestSessionLocal() as db:
        await _seed_ldap_accounts(db, ("back", "back@example.com", False, True))
        await db.commit()
        run = await ldap_sync_service.run_sync(db)
        back = (await db.execute(select(User))).scalars().one()
    assert back.is_active is True
    assert run.users_reactivated == 1
