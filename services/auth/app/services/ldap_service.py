"""LDAP / Active Directory bind support for the auth service.

Flow (when settings.auth_method == "ldap"):

1. Open a connection to settings.ldap_server_url with TLS as configured.
2. Bind as the service account (settings.ldap_bind_dn + bind_password).
3. Search settings.ldap_user_base_dn with the configured filter to resolve the
   full user DN and the email/username attributes.
4. Open a second connection and bind as that user DN with the submitted
   password. Success here is proof the password is correct.

Blocking ldap3 calls are dispatched to a worker thread so they do not stall
the event loop.
"""

from __future__ import annotations

import logging
import ssl
from dataclasses import dataclass

import anyio
from ldap3 import ALL, SIMPLE, SUBTREE, Connection, Server, Tls
from ldap3.core.exceptions import LDAPException
from ldap3.utils.conv import escape_filter_chars

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


@dataclass(frozen=True)
class LdapIdentity:
    username: str
    email: str
    dn: str


def _build_server() -> Server:
    use_tls = settings.ldap_use_tls
    # ldap3 auto-negotiates TLS for ldaps:// URLs; for plain ldap:// with
    # use_tls=True we rely on the Connection.start_tls() call below. The Tls
    # object validates the server certificate by default (see _build_tls).
    tls = _build_tls() if use_tls else None
    return Server(settings.ldap_server_url, get_info=ALL, tls=tls)


def _search_user(username: str) -> tuple[str, dict[str, list]] | None:
    """Bind as service account, then search for the user DN and attributes.

    Returns (user_dn, {email_attr: [values], username_attr: [values]}) on
    success, or None on any failure (bind failed, search returned no results,
    LDAP exception). The service account (ldap_bind_dn + ldap_bind_password)
    must have search permission on ldap_user_base_dn. The search_filter is
    template'd with the escaped username to prevent filter injection. This
    function is always synchronous (blocking ldap3 calls); callers use
    anyio.to_thread to dispatch it.
    """
    server = _build_server()
    conn = Connection(
        server,
        user=settings.ldap_bind_dn or None,
        password=settings.ldap_bind_password or None,
        authentication=SIMPLE if settings.ldap_bind_dn else None,
        auto_bind=False,
        read_only=True,
    )
    try:
        if settings.ldap_use_tls and not settings.ldap_server_url.lower().startswith("ldaps://"):
            conn.start_tls()
        if not conn.bind():
            logger.warning(
                "LDAP service-account bind failed",
                extra={"action": "ldap_bind_failure", "dn": settings.ldap_bind_dn},
            )
            return None
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
            settings.ldap_username_attribute: list(entry[settings.ldap_username_attribute].values)
            if settings.ldap_username_attribute in entry
            else [],
        }
        return entry.entry_dn, attrs
    finally:
        conn.unbind()


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
        try:
            conn.unbind()
        except Exception:
            pass


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
