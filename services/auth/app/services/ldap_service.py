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
DN resolution, and the email-keyed user presence search. Every caller needs
to distinguish "the directory answered" from "the directory could not be
asked" (proves nothing), so every client function raises LdapUnavailableError
on the latter. The login path (bind_user) does too since issue #1403: None is
the directory's refusal of a login (401), the error is an outage (503).

Blocking ldap3 calls are dispatched to a worker thread so they do not stall
the event loop.
"""

from __future__ import annotations

import logging
import ssl
from collections.abc import Sequence
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass

import anyio
from ldap3 import BASE, NONE, SIMPLE, SUBTREE, Connection, Server, Tls
from ldap3.core.exceptions import (
    LDAPCommunicationError,
    LDAPException,
    LDAPInvalidDnError,
    LDAPPasswordIsMandatoryError,
    LDAPSASLPrepError,
)
from ldap3.core.results import (
    RESULT_INVALID_DN_SYNTAX,
    RESULT_NO_SUCH_OBJECT,
    RESULT_SUCCESS,
)
from ldap3.protocol.sasl.sasl import validate_simple_password
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

    fetch_group proves existence, not group-ness: a non-group entry an admin
    typo'd a mapping onto resolves with an empty member_dns, exactly like an
    AD-style empty group. Attribute PRESENCE is deliberately not modeled:
    ldap3 back-fills every requested-but-missing attribute as an empty list
    (return_empty_attributes defaults to True), so emptiness of member_dns is
    the only observable signal, and mapping validation warns on it (decision
    with vendra 2026-08-12: accept with warning, never refuse).
    """

    dn: str
    name: str
    member_dns: tuple[str, ...]


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


def _open_service_connection(require_service_account: bool = False) -> Connection:
    """Build, TLS-negotiate, and bind a service-account-bound read-only
    connection; the caller owns closing it (via _close_connection).

    Raises LdapUnavailableError when the connection cannot be established
    (missing configuration, malformed server URL, TLS or transport failure,
    service-account bind refused).

    require_service_account: the sync-facing callers set True so an empty
    ldap_bind_dn raises instead of silently falling back to an anonymous
    bind. Against a directory whose ACLs deny anonymous reads, the denial
    surfaces as noSuchObject, which would convert a pure credentials
    misconfiguration into false "proven absence"; the login path keeps the
    anonymous fallback it has always had.

    Extracted from _service_connection (issue #513 item 3) so the run-scoped
    shared connection (RunConnection/run_connection below) can hold one
    bound Connection open across many calls instead of only ever inside a
    single `with` block; _service_connection itself is now a thin wrapper
    that opens via this function and closes via _close_connection.
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
    except BaseException:
        # Bind/start_tls failed (or the caller was cancelled mid-bind): a
        # partially-opened socket must never leak past this function,
        # exactly matching _service_connection's original single try/finally
        # that wrapped both the bind phase and the yield.
        _close_connection(conn)
        raise
    return conn


@contextmanager
def _service_connection(require_service_account: bool = False):
    """Yield a service-account-bound read-only connection.

    Raises LdapUnavailableError when the connection cannot be established
    (see _open_service_connection). The connection is unbound on exit.
    """
    conn = _open_service_connection(require_service_account)
    try:
        yield conn
    finally:
        _close_connection(conn)


class RunConnection:
    """Holds one lazily-opened, service-account-bound connection shared by
    every fetch_group/resolve_members/present_emails/disabled_emails call a
    caller explicitly threads it into for the duration of one reconciler
    run (issue #513 items 2/3/7): a run with N mapped groups plus a
    deactivation sweep pays ONE connect+TLS+bind cycle instead of one per
    call.

    EXPLICIT, not global (issue #513 item 2, replacing the module-global
    design its first cut used): the holder is created by run_connection()
    and passed by the caller into each function's own optional keyword
    parameter. A concurrent, unrelated caller (e.g. the mapping-validation
    router's fetch_group call, running on its own asyncio task while a
    reconciler run is in flight) simply never receives this object, so it
    always gets a private, call-scoped connection and can never observe or
    disturb the run's shared socket. This makes the safety argument
    structural (an object reference must be deliberately passed) rather
    than an invariant owned by another module (the old design's "the sole
    caller already serializes every run" comment, which a second caller of
    fetch_group could silently violate without either module noticing).

    Not thread-safe against concurrent use of the SAME holder instance;
    that is fine because a holder is used only by the single async task
    that created it via run_connection(), which awaits each
    fetch_group/resolve_members/etc. call sequentially, never concurrently.
    """

    def __init__(self) -> None:
        self._conn: Connection | None = None

    @property
    def has_connection(self) -> bool:
        """True once a connection has actually been opened. Lets a caller
        skip a to_thread dispatch to close a holder that never opened
        anything (issue #513 item 10), and lets the retry-scope check
        (item 4) distinguish an already-live connection from an initial
        open/bind attempt."""
        return self._conn is not None

    def get(self) -> Connection:
        if self._conn is None:
            self._conn = _open_service_connection(require_service_account=True)
        return self._conn

    def reconnect(self) -> Connection:
        """Close whatever is open (if anything) and open fresh. Composed
        from close() + get() (issue #513 item 10) rather than duplicating
        the open call."""
        self.close()
        return self.get()

    def close(self) -> None:
        if self._conn is not None:
            _close_connection(self._conn)
            self._conn = None


@asynccontextmanager
async def run_connection():
    """Open a run-scoped shared connection and yield its holder.

    Pass the yielded holder explicitly into fetch_group / resolve_members /
    present_emails / disabled_emails (each function's optional run_holder
    keyword) to share it; a call that omits run_holder always opens its
    own private, call-scoped connection, exactly as before item 3 existed
    (issue #513 item 2: no module-global switch, so this is safe to use
    concurrently with unrelated callers that never received this holder).

    A connection that drops mid-run and is retried is scoped narrowly (see
    _is_retryable_after_open below): only a transport-class failure
    (ldap3's LDAPCommunicationError family: a socket open/send/receive/close
    error) on a connection that was ALREADY live before the failing
    operation triggers one reconnect-and-retry; an initial open/bind
    failure, or a non-benign directory result code on a live connection,
    never retries and raises LdapUnavailableError immediately, exactly like
    the non-shared path always has. This keeps per-group skip behavior on a
    genuine, persistent outage unchanged; the only observable difference is
    a TRANSIENT communication drop, which sharing a connection across
    mappings introduces as a new correlated-failure surface a private
    per-call connection never had, and which the one-time reconnect
    neutralizes (a mapping that would have failed only because an EARLIER
    mapping's call happened to kill the shared socket now succeeds instead)
    rather than needlessly skipping that group's reconcile.

    The holder is always closed on exit, success or failure, off the event
    loop thread UNLESS nothing was ever opened (issue #513 item 10: a
    caller with no actual work, e.g. zero mappings and the deactivation
    sweep disabled, pays no to_thread dispatch at all).
    """
    holder = RunConnection()
    try:
        yield holder
    finally:
        if holder.has_connection:
            await anyio.to_thread.run_sync(holder.close)


def _search_user(username: str) -> tuple[str, dict[str, list]] | None:
    """Bind as service account, then search for the user DN and attributes.

    Returns (user_dn, {email_attr: [values], username_attr: [values]}) when the
    search matched, or None only when the directory PROVED there is no match: a
    successful search with zero entries (issue #1403). Every other outcome
    raises LdapUnavailableError: the service connection could not be opened,
    TLS-negotiated, or bound; the search raised; or it found no entry and ended
    with any result code but success (noSuchObject on a misconfigured base DN, a
    referral that could not be followed, busy, access denied). A limited search
    that returned the entry is a match (ldap3 reports a non-empty response as
    found, whatever the result code). Like the presence probe, a
    failed search under the configured base proves nothing about the user. The
    search_filter is template'd with the escaped username to prevent filter
    injection. This function is always synchronous (blocking ldap3 calls);
    callers use anyio.to_thread to dispatch it.
    """
    with _service_connection() as conn:
        safe_username = escape_filter_chars(username)
        search_filter = settings.ldap_user_filter.format(username=safe_username)
        found = _checked_search(
            conn,
            search_base=settings.ldap_user_base_dn,
            search_filter=search_filter,
            search_scope=SUBTREE,
            attributes=[
                settings.ldap_email_attribute,
                settings.ldap_username_attribute,
            ],
            proven_absent_codes=_PRESENCE_PROVEN_ABSENT_CODES,
            description="login user search",
        )
        if not found:
            return None
        entry = conn.entries[0]
        attrs = {
            settings.ldap_email_attribute: _attr_values(entry, settings.ldap_email_attribute),
            settings.ldap_username_attribute: _attr_values(entry, settings.ldap_username_attribute),
        }
        return entry.entry_dn, attrs


# Errors ldap3 raises client-side, inside bind(), about the submitted password
# itself (RFC 4013 SASLprep refusing a character, an empty password). They depend
# on the input, never on the directory, so they are a refusal: as an outage they
# would answer an existing user (the only one who reaches the user bind)
# differently from an unknown one. _bind_user_sync refuses these passwords before
# any directory call (_password_refused_by_saslprep); this is the second guard.
_USER_BIND_INPUT_REFUSALS = (LDAPSASLPrepError, LDAPPasswordIsMandatoryError)


def _bind_as_user(user_dn: str, password: str) -> bool:
    """Bind as user_dn with the submitted password.

    True when the directory accepted the bind. False for every answer the
    directory gave, whatever the result code (invalidCredentials, the codes
    directories use for a disabled, locked, or expired account, and busy or
    unavailable too), and for a password ldap3 refuses client-side
    (_USER_BIND_INPUT_REFUSALS). Only an existing user reaches this bind, so any
    answer here but the refusal would tell an existing account from an unknown
    one (the #1403 review). Raises LdapUnavailableError only when the bind could
    not be attempted or answered at all: the connection could not be built or
    opened, TLS failed, or no answer came within the receive timeout (the
    service connection's 30 s), which ends a silent directory here instead of
    holding the worker thread.
    """
    try:
        server = _build_server()
        conn = Connection(
            server,
            user=user_dn,
            password=password,
            authentication=SIMPLE,
            auto_bind=False,
            read_only=True,
            receive_timeout=_RECEIVE_TIMEOUT_SECONDS,
        )
    except LDAPException as exc:
        raise LdapUnavailableError(f"user bind setup failed: {exc}") from exc
    try:
        try:
            if settings.ldap_use_tls and not settings.ldap_server_url.lower().startswith(
                "ldaps://"
            ):
                conn.start_tls()
            return bool(conn.bind())
        except _USER_BIND_INPUT_REFUSALS:
            return False
        except LDAPException as exc:
            raise LdapUnavailableError(f"user bind failed: {exc}") from exc
    finally:
        _close_connection(conn)


def _password_refused_by_saslprep(username: str, password: str) -> bool:
    """True when ldap3 would refuse this password client-side in the user bind.

    ldap3 prepares every simple-bind password with SASLprep (RFC 4013,
    validate_simple_password) inside bind() and raises for an ASCII or other
    control character (NUL and tab included), a private-use, non-character, or
    tagging code point, a display-changing character, mixed right-to-left and
    left-to-right text, or a password that maps to nothing. Asked here, before
    any directory call, the refusal is the same 401 for every username, with no
    directory traffic and one log line that names the username and the error
    class, never the password or a part of it. Asked only at the user bind, it
    would be reachable for existing users alone (the #1403 review's oracle).
    """
    try:
        validate_simple_password(password)
    except LDAPException as exc:
        logger.warning(
            "LDAP login refused before any directory call: the password is not a valid "
            "simple-bind password (SASLprep)",
            extra={
                "action": "ldap_password_refused",
                "username": username,
                "exception_class": type(exc).__name__,
            },
        )
        return True
    return False


def _bind_user_sync(username: str, password: str) -> LdapIdentity | None:
    """Authenticate a user against LDAP (issue #1403 split the two failure kinds).

    Flow: (1) bind as service account and search for the user DN; (2) bind as
    that user with the submitted password; (3) extract and return email and
    username attributes. The search filters on ldap_user_filter (injected with
    escaped username to prevent LDAP filter injection).

    Returns the LdapIdentity on success and None for every refusal: an empty
    password (refused before any call, since an LDAP simple bind with one
    succeeds anonymously), a password SASLprep refuses (also before any call,
    _password_refused_by_saslprep), no matching entry, a refused user bind, or an
    entry without the email attribute.

    Raises LdapUnavailableError whenever the directory could not be asked:
    incomplete configuration (logged at ERROR), or any connection, TLS,
    service-account bind, search, or user-bind failure (logged once at WARNING,
    action ldap_error, with the client's text in the message and the cause's
    class name in exception_class; never the password). Callers wrap this in
    anyio.to_thread to keep it off the event loop.
    """
    if not settings.ldap_server_url or not settings.ldap_user_base_dn:
        logger.error("LDAP not fully configured; login cannot be checked")
        raise LdapUnavailableError(
            "LDAP is not fully configured (ldap_server_url or ldap_user_base_dn is empty)"
        )
    if not password:
        # LDAP anonymous bind succeeds with an empty password; reject early.
        return None
    if _password_refused_by_saslprep(username, password):
        return None
    try:
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
        except LDAPException as exc:
            # Backstop: every ldap3 call above already converts its own errors;
            # one that does not is still an outage, never a refusal.
            raise LdapUnavailableError(f"LDAP error during login: {exc}") from exc
    except LdapUnavailableError as exc:
        # One WARNING per outage with the client's detail (action ldap_error),
        # so operators and alerting see a down directory, not a flood of "user
        # not found" INFO lines; the caller answers without that detail.
        logger.warning(
            "LDAP unavailable during login: %s",
            exc,
            extra={
                "action": "ldap_error",
                "username": username,
                "exception_class": type(exc.__cause__ or exc).__name__,
            },
        )
        raise

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


async def bind_user(username: str, password: str) -> LdapIdentity | None:
    """Verify the user's password against LDAP and return their identity, or None.

    None is the directory's refusal of this login; LdapUnavailableError means
    the directory could not be asked (see _bind_user_sync).
    """
    return await anyio.to_thread.run_sync(_bind_user_sync, username, password)


# ---------------------------------------------------------------------------
# ADR 0011 group-sync directory client. Like the login path above (since issue
# #1403), these raise LdapUnavailableError when the directory cannot be asked;
# None (or a skip_reason) is reserved for answers the directory actually gave.
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


def _is_transport_failure(exc: LdapUnavailableError) -> bool:
    """True when exc's cause chain traces back to an ldap3 transport-class
    error (LDAPCommunicationError: socket open/receive/send/close, or a
    server-terminated session), as opposed to a non-benign directory result
    code (raised with no __cause__ at all) or a non-transport LDAPException
    (e.g. a malformed filter). Only this narrow class is worth a
    reconnect-and-retry (issue #513 item 4): anything else would just fail
    identically again on a fresh connection.
    """
    return isinstance(exc.__cause__, LDAPCommunicationError)


def _call_with_connection(op, holder: "RunConnection | None"):
    """Run op(conn): using holder's shared connection when holder is given,
    or a private, call-scoped connection otherwise (issue #513 item 2: the
    holder is always an EXPLICIT argument, never read from a module
    global, so an unrelated concurrent caller that never received it can
    never share or disturb it).

    Retry scope (item 4): a retry is attempted ONLY when ALL of the
    following hold: a holder was given, its connection was ALREADY live
    before this call (so this op is reusing an established session rather
    than performing the initial open/bind), and the failure is a
    transport-class LdapUnavailableError (_is_transport_failure). Every
    other case, no holder, an initial open/bind failure, or a non-benign
    directory result code on a live connection, raises immediately with no
    retry, exactly matching the non-shared path's single-attempt behavior:
    a connection failure must never produce a novel failure mode, only a
    possibly-recovered one.
    """
    if holder is None:
        with _service_connection(require_service_account=True) as conn:
            return op(conn)
    had_live_connection = holder.has_connection
    try:
        return op(holder.get())
    except LdapUnavailableError as exc:
        if not had_live_connection or not _is_transport_failure(exc):
            raise
        return op(holder.reconnect())


def _fetch_group_sync(
    group_dn: str, holder: "RunConnection | None" = None
) -> LdapGroupEntry | None:
    name_attr = settings.ldap_group_name_attribute
    member_attr = settings.ldap_group_member_attribute
    entry = _call_with_connection(
        lambda conn: _base_entry(conn, group_dn, [name_attr, member_attr]), holder
    )
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
    # validation warns on an empty member_dns.
    return LdapGroupEntry(
        dn=entry.entry_dn,
        name=str(name_values[0]) if name_values else group_dn,
        member_dns=tuple(str(v) for v in _attr_values(entry, member_attr)),
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


def _resolve_members_sync(
    member_dns: tuple[str, ...], holder: "RunConnection | None" = None
) -> list[LdapMemberResolution]:
    return _call_with_connection(
        lambda conn: [_resolution_for(conn, dn) for dn in member_dns], holder
    )


def _user_present_by_email_sync(email: str, holder: "RunConnection | None" = None) -> bool:
    """SUBTREE-search ldap_user_base_dn for the email; True iff an entry matched.

    Asymmetric with _base_entry on purpose: here even noSuchObject raises,
    because a missing or misconfigured user_base_dn proves nothing about the
    user being searched for, and the deactivation sweep may only act on
    proven absence.
    """

    def _do(conn: Connection) -> bool:
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

    return _call_with_connection(_do, holder)


async def fetch_group(
    group_dn: str, *, run_holder: "RunConnection | None" = None
) -> LdapGroupEntry | None:
    """Fetch a mapped group's entry: None means the DN provably does not resolve.

    That None is the ADR 0011 dangling case (mapping validation 422s on it;
    the reconciler skips the group fail-closed). Directory errors raise
    LdapUnavailableError instead.

    run_holder: pass the holder from `async with run_connection() as holder:`
    to reuse its shared connection (issue #513 items 2/3); omitted
    (default None), opens a private, call-scoped connection exactly as
    before item 3 existed.
    """
    return await anyio.to_thread.run_sync(_fetch_group_sync, group_dn, run_holder)


async def resolve_member(member_dn: str) -> LdapMemberResolution:
    """Resolve one group-member DN to the (email, username) JIT provisioning trusts.

    For a single DN (the phase 2 mapping-validation path). Reconcile passes
    over a whole membership should use resolve_members, which shares one
    connection. Resolution is a base search on the DN itself, anywhere in
    the tree; the login path and the presence probe are scoped to
    ldap_user_base_dn, so a member outside that base resolves here but
    cannot log in and is invisible to user_present_by_email (the ADR's
    group-presence credit is what keeps such users from being swept). Never
    shares a run_connection() holder: this is the single-DN validation path,
    outside any reconciler run.
    """
    return await anyio.to_thread.run_sync(_resolve_member_sync, member_dn)


async def resolve_members(
    member_dns: Sequence[str], *, run_holder: "RunConnection | None" = None
) -> list[LdapMemberResolution]:
    """Resolve a group's member DNs over ONE shared service connection.

    One connect+TLS+bind cycle per batch instead of per member (and, when
    the caller passes run_holder from an active run_connection(), the SAME
    connection fetch_group and every other mapping's resolve_members in
    that run already used: issue #513 items 2/3). A raise mid-batch
    discards the partial result, which is exactly ADR 0011's
    any-error-skips-the-group rule: the reconciler must not apply a
    half-resolved desired set.
    """
    return await anyio.to_thread.run_sync(_resolve_members_sync, tuple(member_dns), run_holder)


async def user_present_by_email(email: str, *, run_holder: "RunConnection | None" = None) -> bool:
    """Email-keyed presence probe; errors raise (never absence).

    Resolved 2026-08-12: the deactivation sweep answers presence via ONE
    paged enumeration under ldap_user_base_dn (present_emails, below), not
    per-user calls to this probe. This function's remaining sweep role is
    the disabled-account check, which needs a variant that conjoins
    ldap_disabled_filter into the search filter; disabled_emails is that
    variant, built as a paged enumeration alongside it, since this filter is
    presence-only.
    """
    return await anyio.to_thread.run_sync(_user_present_by_email_sync, email, run_holder)


# ---------------------------------------------------------------------------
# ADR 0011 phase 4: paged presence enumeration. present_emails/disabled_emails
# replace a per-user presence probe for the deactivation sweep's main pass: a
# single paged SUBTREE search under ldap_user_base_dn scales with directory
# size instead of user count, and (the load-bearing rule) a failed or
# truncated page raises LdapUnavailableError rather than returning whatever
# was collected so far. A partial enumeration must never be read as a
# complete one: the sweep would otherwise deactivate everyone the outage
# happened to cut off before.
# ---------------------------------------------------------------------------

_PAGED_SEARCH_SIZE = 500
_PAGED_COOKIE_OID = "1.2.840.113556.1.4.319"


def _paged_user_emails_sync(
    search_filter: str, description: str, holder: "RunConnection | None" = None
) -> frozenset[str]:
    """Paged SUBTREE enumeration under ldap_user_base_dn, collecting every
    value of ldap_email_attribute (lowercased) across every matching entry
    (an entry may be multi-valued; every value is included).

    Loops the paged-results cookie, held on ONE connection for the whole
    enumeration, until the directory reports it exhausted. A missing paging
    control is treated as end-of-pages ONLY once this page's result has
    already been proven success below; any other outcome (a failed search,
    a non-success result code, an LDAP exception mid-page) raises
    LdapUnavailableError instead of returning a partial set.

    holder (issue #513 item 7): when given and its connection drops
    mid-enumeration, _call_with_connection's retry-once restarts this WHOLE
    function body (including the paging cookie, reset to empty) on a fresh
    connection rather than resuming mid-page, since a paged-results cookie
    is tied to the specific session that issued it and cannot be replayed
    on a different connection.
    """
    email_attr = settings.ldap_email_attribute

    def _do(conn: Connection) -> frozenset[str]:
        emails: set[str] = set()
        cookie: bytes = b""
        while True:
            try:
                # ldap3's boolean return is NOT a success signal: for a
                # SUBTREE search it is True only when entries were found, so
                # a legitimately successful page with zero matches (the last
                # page, or a filter matching nothing) returns False. Proof
                # of success is the result CODE (checked below), the same
                # idiom _checked_search uses; the return value is ignored.
                conn.search(
                    search_base=settings.ldap_user_base_dn,
                    search_filter=search_filter,
                    search_scope=SUBTREE,
                    attributes=[email_attr],
                    paged_size=_PAGED_SEARCH_SIZE,
                    paged_cookie=cookie or None,
                )
            except LDAPException as exc:
                raise LdapUnavailableError(f"{description} failed: {exc}") from exc
            result_code = (conn.result or {}).get("result")
            if result_code != RESULT_SUCCESS:
                raise LdapUnavailableError(f"{description} failed: {conn.result}")
            for entry in conn.entries:
                for value in _attr_values(entry, email_attr):
                    emails.add(str(value).lower())
            controls = (conn.result or {}).get("controls") or {}
            page_control = controls.get(_PAGED_COOKIE_OID)
            if not page_control:
                # No paging control on an already-proven-successful page:
                # the directory has nothing further to page through.
                break
            cookie = (page_control.get("value") or {}).get("cookie") or b""
            if not cookie:
                break
        return frozenset(emails)

    return _call_with_connection(_do, holder)


def _present_emails_sync(holder: "RunConnection | None" = None) -> frozenset[str]:
    return _paged_user_emails_sync(
        f"({settings.ldap_email_attribute}=*)", "present-emails enumeration", holder
    )


def _disabled_emails_sync(holder: "RunConnection | None" = None) -> frozenset[str]:
    return _paged_user_emails_sync(
        f"(&({settings.ldap_email_attribute}=*){settings.ldap_disabled_filter})",
        "disabled-emails enumeration",
        holder,
    )


async def present_emails(*, run_holder: "RunConnection | None" = None) -> frozenset[str]:
    """Every email attribute value seen under ldap_user_base_dn, lowercased.

    The deactivation sweep's absence proof: a candidate whose email is not
    in this set (and was not group-presence-credited this run) is proven
    absent. Raises LdapUnavailableError on any failure or truncated page;
    never returns a partial enumeration. run_holder shares the run's
    connection (issue #513 item 7) exactly like fetch_group/resolve_members.
    """
    return await anyio.to_thread.run_sync(_present_emails_sync, run_holder)


async def disabled_emails(*, run_holder: "RunConnection | None" = None) -> frozenset[str]:
    """Every email attribute value under ldap_user_base_dn matching the
    ldap_disabled_filter setting, conjoined with the email-presence filter.

    Only meaningful (and only ever called) when ldap_disabled_filter is
    nonempty; raises ValueError otherwise (a caller bug, not a directory
    outage). The setting is trusted admin configuration and is NOT escaped:
    it is a filter expression, not a value. It is rejected early with
    ValueError when it does not start with "(", to catch an obvious
    misconfiguration (e.g. a bare attribute name) before it reaches the
    directory as a malformed filter.
    """
    disabled_filter = settings.ldap_disabled_filter
    if not disabled_filter:
        raise ValueError("disabled_emails() called with ldap_disabled_filter unset")
    if not disabled_filter.startswith("("):
        raise ValueError(
            "ldap_disabled_filter must be a complete filter expression starting with '('"
        )
    return await anyio.to_thread.run_sync(_disabled_emails_sync, run_holder)
