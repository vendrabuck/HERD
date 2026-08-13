"""LDAP / Active Directory bind support for the auth service.

Flow (when settings.auth_method == "ldap"):

1. Open a connection to settings.ldap_server_url with TLS as configured.
2. Bind as the service account (settings.ldap_bind_dn + bind_password).
3. Search settings.ldap_user_base_dn with the configured filter to resolve the
   full user DN and the email/username attributes.
4. Open a second connection and bind as that user DN with the submitted
   password. Success here is proof the password is correct.

Beyond login binds, this module is the directory client for the ADR 0011
group sync (docs/design/0011-ldap-group-sync.md): group entry fetch, member
DN resolution, and the email-keyed user presence search. Those callers need
to distinguish "the directory answered: not there" (safe to act on) from
"the directory could not be asked" (proves nothing), so the sync-facing
functions raise LdapUnavailableError on the latter instead of collapsing
every failure to None the way the login path does.

Blocking ldap3 calls are dispatched to a worker thread so they do not stall
the event loop.
"""

from __future__ import annotations

import logging
import ssl
from collections.abc import Sequence
from contextlib import contextmanager
from dataclasses import dataclass

import anyio
from ldap3 import BASE, NONE, SIMPLE, SUBTREE, Connection, Server, Tls
from ldap3.core.exceptions import LDAPException, LDAPInvalidDnError
from ldap3.core.results import (
    RESULT_INVALID_DN_SYNTAX,
    RESULT_NO_SUCH_OBJECT,
    RESULT_SUCCESS,
)
from ldap3.utils.conv import escape_filter_chars
from ldap3.utils.dn import safe_dn

from app.config import settings

logger = logging.getLogger(__name__)


def _build_tls() -> Tls:
    """Build the TLS config for LDAP connections, validating server certificate.

    Validates the directory server's certificate by default: both SIMPLE binds
    below (service-account and user credentials) transmit passwords on the wire,
    so an unvalidated certificate lets an active network attacker MITM the
    connection and harvest credentials. ldap3's bare Tls() defaults to
    CERT_NONE, a dangerous default for password-carrying connections; validation
    is explicitly set here. Disabling validation (ldap_tls_validate=False) is an
    opt-in escape hatch for lab directories behind self-signed certs and logs a
    warning; production deployments should use a real CA cert or
    ldap_ca_cert pointing to a private CA bundle. This function is not
    authentication itself but a prerequisite that prevents credential leaks.
    """
    if not settings.ldap_tls_validate:
        logger.warning(
            "LDAP TLS certificate validation is DISABLED; the connection is "
            "vulnerable to man-in-the-middle credential capture. Set "
            "ldap_tls_validate=True (and ldap_ca_cert for a private CA).",
            extra={"action": "ldap_tls_validation_disabled"},
        )
        return Tls(validate=ssl.CERT_NONE)
    return Tls(
        validate=ssl.CERT_REQUIRED,
        version=ssl.PROTOCOL_TLS_CLIENT,
        ca_certs_file=settings.ldap_ca_cert or None,
    )


class LdapUnavailableError(Exception):
    """The directory could not be asked: bind failure, timeout, or LDAP error.

    Callers must treat this as proving nothing about the queried entry; per
    ADR 0011 an error is never absence, so fail-closed consumers skip the
    affected work and defer convergence rather than acting on it.
    """


@dataclass(frozen=True)
class LdapIdentity:
    username: str
    email: str
    dn: str


@dataclass(frozen=True)
class LdapGroupEntry:
    """A directory group entry: its DN, display name, and member DNs.

    member_attribute_present distinguishes an entry whose member attribute is
    absent (an AD-style empty group, or a non-group entry an admin typo'd a
    mapping onto) from one that is present. fetch_group proves existence, not
    group-ness, so mapping validation uses this flag to warn (decision with
    vendra 2026-08-12: accept with warning, never refuse, since empty AD
    groups legitimately lack the attribute).
    """

    dn: str
    name: str
    member_dns: tuple[str, ...]
    member_attribute_present: bool = True


# skip_reason vocabulary for LdapMemberResolution; the reconciler persists
# these strings in ldap_sync_runs.detail, so treat them as a small API.
MEMBER_SKIP_NOT_FOUND = "not_found"
MEMBER_SKIP_MISSING_EMAIL = "missing_email"
MEMBER_SKIP_MISSING_USERNAME = "missing_username"


@dataclass(frozen=True)
class LdapMemberResolution:
    """Outcome of resolving one member DN to a HERD-usable identity.

    identity is None when the directory PROVED the member unusable (entry
    gone, or present without the email/username attributes JIT provisioning
    requires); skip_reason then says why. A directory error never produces
    a resolution; it raises LdapUnavailableError instead.
    """

    identity: LdapIdentity | None
    skip_reason: str | None = None


# A black-holed directory (firewall DROP, lost route) otherwise stalls each
# connect for the OS TCP timeout (about 2 minutes on Linux) and can stall an
# established connection indefinitely; both bounds keep the failure inside
# the prompt raise-LdapUnavailableError path instead.
_CONNECT_TIMEOUT_SECONDS = 10
_RECEIVE_TIMEOUT_SECONDS = 30


def _build_server() -> Server:
    use_tls = settings.ldap_use_tls
    # ldap3 auto-negotiates TLS for ldaps:// URLs; for plain ldap:// with
    # use_tls=True we rely on the Connection.start_tls() call below. The Tls
    # object validates the server certificate by default (see _build_tls).
    # get_info=NONE: with ALL, ldap3 follows every bind with a rootDSE search
    # plus a full subschema fetch (hundreds of KB on AD), and nothing in this
    # module reads server.info or server.schema.
    tls = _build_tls() if use_tls else None
    return Server(
        settings.ldap_server_url,
        get_info=NONE,
        tls=tls,
        connect_timeout=_CONNECT_TIMEOUT_SECONDS,
    )


def _close_connection(conn: Connection) -> None:
    """Unbind and ensure the raw socket is closed, swallowing errors.

    ldap3's failed-connect path (_open_socket) abandons its created socket
    without closing it, and unbind() on a never-opened connection does not
    close it either; left to the GC it surfaces a ResourceWarning, which the
    root filterwarnings=error config turns into a failure in whatever test
    runs next. A clean unbind sets conn.socket to None, so the extra close
    never double-closes.
    """
    try:
        conn.unbind()
    except Exception:
        pass
    sock = getattr(conn, "socket", None)
    if sock is not None:
        try:
            sock.close()
        except Exception:
            pass


@contextmanager
def _service_connection(require_service_account: bool = False):
    """Yield a service-account-bound read-only connection.

    Raises LdapUnavailableError when the connection cannot be established
    (missing configuration, malformed server URL, TLS or transport failure,
    service-account bind refused). The connection is unbound on exit.

    require_service_account: the sync-facing callers set True so an empty
    ldap_bind_dn raises instead of silently falling back to an anonymous
    bind. Against a directory whose ACLs deny anonymous reads, the denial
    surfaces as noSuchObject, which would convert a pure credentials
    misconfiguration into false "proven absence"; the login path keeps the
    anonymous fallback it has always had.
    """
    if not settings.ldap_server_url:
        raise LdapUnavailableError("LDAP is not configured (ldap_server_url is empty)")
    if require_service_account and not settings.ldap_bind_dn:
        raise LdapUnavailableError(
            "group sync requires a service-account bind (ldap_bind_dn is empty)"
        )
    try:
        # Server() raises LDAPException subclasses at construction for a
        # malformed URL (bad scheme, invalid port), so it must sit inside
        # the converting try or the sync-facing contract leaks raw ldap3
        # exceptions.
        server = _build_server()
        conn = Connection(
            server,
            user=settings.ldap_bind_dn or None,
            password=settings.ldap_bind_password or None,
            authentication=SIMPLE if settings.ldap_bind_dn else None,
            auto_bind=False,
            read_only=True,
            receive_timeout=_RECEIVE_TIMEOUT_SECONDS,
        )
    except LDAPException as exc:
        raise LdapUnavailableError(f"LDAP connection setup failed: {exc}") from exc
    try:
        try:
            if settings.ldap_use_tls and not settings.ldap_server_url.lower().startswith(
                "ldaps://"
            ):
                conn.start_tls()
            if not conn.bind():
                logger.warning(
                    "LDAP service-account bind failed",
                    extra={"action": "ldap_bind_failure", "dn": settings.ldap_bind_dn},
                )
                raise LdapUnavailableError("service-account bind failed")
        except LDAPException as exc:
            raise LdapUnavailableError(f"LDAP connection failed: {exc}") from exc
        yield conn
    finally:
        _close_connection(conn)


def _search_user(username: str) -> tuple[str, dict[str, list]] | None:
    """Bind as service account, then search for the user DN and attributes.

    Returns (user_dn, {email_attr: [values], username_attr: [values]}) on
    success, or None when the connection cannot be established or the search
    finds nothing: the login path deliberately does not distinguish directory
    errors from absent users. A search-phase LDAPException still propagates;
    _bind_user_sync's except LDAPException completes the None contract, as it
    always has. The search_filter is template'd with the escaped username to
    prevent filter injection. This function is always synchronous (blocking
    ldap3 calls); callers use anyio.to_thread to dispatch it.
    """
    try:
        with _service_connection() as conn:
            safe_username = escape_filter_chars(username)
            search_filter = settings.ldap_user_filter.format(username=safe_username)
            ok = conn.search(
                search_base=settings.ldap_user_base_dn,
                search_filter=search_filter,
                search_scope=SUBTREE,
                attributes=[
                    settings.ldap_email_attribute,
                    settings.ldap_username_attribute,
                ],
            )
            if not ok or not conn.entries:
                return None
            entry = conn.entries[0]
            attrs = {
                settings.ldap_email_attribute: list(entry[settings.ldap_email_attribute].values)
                if settings.ldap_email_attribute in entry
                else [],
                settings.ldap_username_attribute: list(
                    entry[settings.ldap_username_attribute].values
                )
                if settings.ldap_username_attribute in entry
                else [],
            }
            return entry.entry_dn, attrs
    except LdapUnavailableError as exc:
        # Keep the pre-refactor taxonomy: an outage during login logs at
        # WARNING with the error detail (action ldap_error), so operators
        # and alerting see a down directory, not a flood of "user not
        # found" INFO lines.
        logger.warning(
            "LDAP unavailable during user search: %s",
            exc,
            extra={"action": "ldap_error", "username": username},
        )
        return None


def _bind_as_user(user_dn: str, password: str) -> bool:
    server = _build_server()
    conn = Connection(
        server,
        user=user_dn,
        password=password,
        authentication=SIMPLE,
        auto_bind=False,
        read_only=True,
    )
    try:
        if settings.ldap_use_tls and not settings.ldap_server_url.lower().startswith("ldaps://"):
            conn.start_tls()
        if not conn.bind():
            return False
        return True
    finally:
        _close_connection(conn)


def _bind_user_sync(username: str, password: str) -> LdapIdentity | None:
    """Authenticate a user against LDAP. Returns LdapIdentity on success, None on failure.

    Flow: (1) bind as service account and search for the user DN; (2) bind as
    that user with the submitted password; (3) extract and return email and
    username attributes. The search filters on ldap_user_filter (injected with
    escaped username to prevent LDAP filter injection). Returns None on any
    failure: configuration incomplete, empty password (rejects anonymous bind),
    service-account bind failure, search returned no results, user bind failure
    (wrong password, disabled account, etc.), missing email attribute, or LDAP
    exception. Callers wrap this in anyio.to_thread to keep it off the event loop.
    """
    if not settings.ldap_server_url or not settings.ldap_user_base_dn:
        logger.error("LDAP not fully configured; bind aborted")
        return None
    if not password:
        # LDAP anonymous bind succeeds with an empty password; reject early.
        return None
    try:
        found = _search_user(username)
        if found is None:
            logger.info(
                "LDAP user search returned no results",
                extra={"action": "ldap_user_not_found", "username": username},
            )
            return None
        user_dn, attrs = found
        if not _bind_as_user(user_dn, password):
            logger.warning(
                "LDAP user bind failed (wrong password?)",
                extra={"action": "ldap_bind_failure", "dn": user_dn},
            )
            return None

        email_values = attrs.get(settings.ldap_email_attribute) or []
        username_values = attrs.get(settings.ldap_username_attribute) or []
        email = str(email_values[0]) if email_values else ""
        ldap_username = str(username_values[0]) if username_values else username
        if not email:
            logger.warning(
                "LDAP user has no email attribute; cannot provision HERD account",
                extra={"action": "ldap_missing_email", "dn": user_dn},
            )
            return None
        return LdapIdentity(username=ldap_username, email=email, dn=user_dn)
    except LDAPException as exc:
        logger.warning(
            "LDAP error during bind: %s",
            exc,
            extra={"action": "ldap_error", "username": username},
        )
        return None


async def bind_user(username: str, password: str) -> LdapIdentity | None:
    """Verify the user's password against LDAP and return their identity, or None."""
    return await anyio.to_thread.run_sync(_bind_user_sync, username, password)


# ---------------------------------------------------------------------------
# ADR 0011 group-sync directory client. Unlike the login path above, these
# raise LdapUnavailableError when the directory cannot be asked; None (or a
# skip_reason) is reserved for answers the directory actually gave.
# ---------------------------------------------------------------------------

# Proven-absent result codes for base-scope DN lookups. ldap3 reports a base
# search on a nonexistent DN as a FAILED search with noSuchObject, not as an
# empty success, so proof must be told apart from errors by result code.
# invalidDNSyntax is proof too: a syntactically invalid DN provably resolves
# nothing, and cannot start resolving without the mapping being re-created,
# so classifying it as transient would misreport a permanent condition as a
# perpetual outage.
_DN_PROVEN_ABSENT_CODES = frozenset(
    {RESULT_SUCCESS, RESULT_NO_SUCH_OBJECT, RESULT_INVALID_DN_SYNTAX}
)
# For the presence probe, ONLY a plain success with zero entries is proof;
# see _user_present_by_email_sync for why noSuchObject must raise there.
_PRESENCE_PROVEN_ABSENT_CODES = frozenset({RESULT_SUCCESS})


def _checked_search(
    conn: Connection,
    *,
    search_base: str,
    search_filter: str,
    search_scope: str,
    attributes: list[str],
    proven_absent_codes: frozenset[int],
    description: str,
) -> bool:
    """Run a search whose outcome must be classified as proof or error.

    True: the search matched (conn.entries holds the results). False: the
    directory PROVED absence (no entries and the result code is in the
    caller's proven_absent_codes). Anything else raises LdapUnavailableError.
    This ladder is the module's load-bearing safety logic; every directory
    search with proof semantics must route through it so the benign-code set
    is a conscious per-call-site choice rather than a re-implementation.
    """
    try:
        ok = conn.search(
            search_base=search_base,
            search_filter=search_filter,
            search_scope=search_scope,
            attributes=attributes,
        )
    except LDAPException as exc:
        raise LdapUnavailableError(f"{description} failed: {exc}") from exc
    if ok and conn.entries:
        return True
    result_code = (conn.result or {}).get("result")
    if result_code in proven_absent_codes:
        return False
    raise LdapUnavailableError(f"{description} failed: {conn.result}")


def _base_entry(conn: Connection, dn: str, attributes: list[str]):
    """Base-scope search for one entry by DN.

    Returns the entry, or None when the DN provably resolves nothing: a
    syntactically invalid DN (ldap3 validates client-side, before any
    request; server-side invalidDNSyntax is kept in the benign set as the
    belt for DNs that pass the client parser), an empty success, or
    noSuchObject. Raises LdapUnavailableError on any other outcome: an
    errored search proves nothing. The presence probe deliberately does NOT
    get this treatment; its base DN comes from configuration, and a
    misconfiguration there proves nothing about the user (see
    _user_present_by_email_sync).
    """
    try:
        safe_dn(dn)
    except LDAPInvalidDnError:
        return None
    if _checked_search(
        conn,
        search_base=dn,
        search_filter="(objectClass=*)",
        search_scope=BASE,
        attributes=attributes,
        proven_absent_codes=_DN_PROVEN_ABSENT_CODES,
        description=f"base search on {dn}",
    ):
        return conn.entries[0]
    return None


def _attr_values(entry, attr: str) -> list:
    """Values of an attribute on an ldap3 Entry, [] when absent."""
    return list(entry[attr].values) if attr in entry else []


def _fetch_group_sync(group_dn: str) -> LdapGroupEntry | None:
    name_attr = settings.ldap_group_name_attribute
    member_attr = settings.ldap_group_member_attribute
    with _service_connection(require_service_account=True) as conn:
        entry = _base_entry(conn, group_dn, [name_attr, member_attr])
    if entry is None:
        return None
    name_values = _attr_values(entry, name_attr)
    if not name_values:
        # A group without its display-name attribute is still usable; the DN
        # stands in so the cached directory_name is never empty.
        logger.warning(
            "LDAP group has no %s attribute; falling back to its DN",
            name_attr,
            extra={"action": "ldap_group_missing_name", "dn": group_dn},
        )
    # An entry lacking the member attribute yields an empty tuple: AD models
    # an empty group that way, so it cannot be refused outright. Note this
    # also means fetch_group proves existence, not group-ness; mapping
    # validation surfaces that via member_attribute_present.
    return LdapGroupEntry(
        dn=entry.entry_dn,
        name=str(name_values[0]) if name_values else group_dn,
        member_dns=tuple(str(v) for v in _attr_values(entry, member_attr)),
        member_attribute_present=member_attr in entry,
    )


def _resolution_for(conn: Connection, member_dn: str) -> LdapMemberResolution:
    """Resolve one member DN on an already-bound connection."""
    entry = _base_entry(
        conn, member_dn, [settings.ldap_email_attribute, settings.ldap_username_attribute]
    )
    if entry is None:
        return LdapMemberResolution(None, MEMBER_SKIP_NOT_FOUND)
    email_values = _attr_values(entry, settings.ldap_email_attribute)
    username_values = _attr_values(entry, settings.ldap_username_attribute)
    if not email_values:
        return LdapMemberResolution(None, MEMBER_SKIP_MISSING_EMAIL)
    if not username_values:
        return LdapMemberResolution(None, MEMBER_SKIP_MISSING_USERNAME)
    return LdapMemberResolution(
        LdapIdentity(
            username=str(username_values[0]),
            email=str(email_values[0]),
            dn=entry.entry_dn,
        )
    )


def _resolve_member_sync(member_dn: str) -> LdapMemberResolution:
    with _service_connection(require_service_account=True) as conn:
        return _resolution_for(conn, member_dn)


def _resolve_members_sync(member_dns: tuple[str, ...]) -> list[LdapMemberResolution]:
    with _service_connection(require_service_account=True) as conn:
        return [_resolution_for(conn, dn) for dn in member_dns]


def _user_present_by_email_sync(email: str) -> bool:
    """SUBTREE-search ldap_user_base_dn for the email; True iff an entry matched.

    Asymmetric with _base_entry on purpose: here even noSuchObject raises,
    because a missing or misconfigured user_base_dn proves nothing about the
    user being searched for, and the deactivation sweep may only act on
    proven absence.
    """
    with _service_connection(require_service_account=True) as conn:
        safe_email = escape_filter_chars(email)
        return _checked_search(
            conn,
            search_base=settings.ldap_user_base_dn,
            search_filter=f"({settings.ldap_email_attribute}={safe_email})",
            search_scope=SUBTREE,
            attributes=[settings.ldap_email_attribute],
            proven_absent_codes=_PRESENCE_PROVEN_ABSENT_CODES,
            description=f"presence search for {email}",
        )


async def fetch_group(group_dn: str) -> LdapGroupEntry | None:
    """Fetch a mapped group's entry: None means the DN provably does not resolve.

    That None is the ADR 0011 dangling case (mapping validation 422s on it;
    the reconciler skips the group fail-closed). Directory errors raise
    LdapUnavailableError instead.
    """
    return await anyio.to_thread.run_sync(_fetch_group_sync, group_dn)


async def resolve_member(member_dn: str) -> LdapMemberResolution:
    """Resolve one group-member DN to the (email, username) JIT provisioning trusts.

    For a single DN (the phase 2 mapping-validation path). Reconcile passes
    over a whole membership should use resolve_members, which shares one
    connection. Resolution is a base search on the DN itself, anywhere in
    the tree; the login path and the presence probe are scoped to
    ldap_user_base_dn, so a member outside that base resolves here but
    cannot log in and is invisible to user_present_by_email (the ADR's
    group-presence credit is what keeps such users from being swept).
    """
    return await anyio.to_thread.run_sync(_resolve_member_sync, member_dn)


async def resolve_members(member_dns: Sequence[str]) -> list[LdapMemberResolution]:
    """Resolve a group's member DNs over ONE shared service connection.

    One connect+TLS+bind cycle per batch instead of per member. A raise
    mid-batch discards the partial result, which is exactly ADR 0011's
    any-error-skips-the-group rule: the reconciler must not apply a
    half-resolved desired set.
    """
    return await anyio.to_thread.run_sync(_resolve_members_sync, tuple(member_dns))


async def user_present_by_email(email: str) -> bool:
    """Email-keyed presence probe for the deactivation sweep; errors raise.

    Deliberately per-user: the ADR's sweep isolates errors per user (an
    errored search leaves that user untouched and marks the run partial).
    Whether phase 4 also wants a batched or paged variant is a phase 4
    design decision; do not fold one in here without settling that.
    """
    return await anyio.to_thread.run_sync(_user_present_by_email_sync, email)
