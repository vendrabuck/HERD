"""The webhook destination rule: where the integration service may POST.

A subscription's `target_url` host must resolve to public addresses only, at
registration (`create_webhook` answers 422) and again before every delivery
(`deliver_one` records a `failed` row with the fixed text
`destination not allowed` and sends nothing). Every address the host answers
with must pass `herd_common.public_address.is_public_address`, so loopback,
link-local, private (RFC 1918 and IPv6 unique-local), shared address space
(100.64.0.0/10), multicast, and unspecified addresses are refused, and a host
that does not resolve is refused too (fail closed).

`WEBHOOK_ALLOWED_HOSTS` names the internal destinations an operator admits on
purpose: a comma-separated list of hostnames (matched exactly against the URL
host, case-insensitively) and CIDRs (an address inside one counts as allowed).
A named host is admitted without resolving it. The development and test
override sets it so the live tests can reach the in-network echo sink.

Known limit, shared with ai-orchestrator's documentation fetch: the check and
httpx's own connection resolve DNS separately, so a host whose answers change
between the two is not caught. Closing that needs a transport that connects to
the checked address.

This module reads no settings itself (config.py imports `parse_allowed_hosts`
for its boot-time validator); callers pass the allowlist text. The resolver is
the module attribute `resolver`, looked up at call time, so tests replace it
without DNS.
"""

from __future__ import annotations

import asyncio
import ipaddress
from dataclasses import dataclass
from urllib.parse import urlsplit

from herd_common.public_address import Resolver, default_resolver, is_public_address

# The 422 detail a refused registration answers with.
TARGET_NOT_PUBLIC_DETAIL = "target_url must resolve to a public address"
# The ledger text of a delivery refused before any POST.
DESTINATION_NOT_ALLOWED = "destination not allowed"

resolver: Resolver = default_resolver

_Network = ipaddress.IPv4Network | ipaddress.IPv6Network


@dataclass(frozen=True)
class AllowedHosts:
    names: frozenset[str]
    networks: tuple[_Network, ...]


def _normalize_host(host: str) -> str:
    return host.strip().lower().rstrip(".")


def parse_allowed_hosts(raw: str) -> AllowedHosts:
    """Parse the WEBHOOK_ALLOWED_HOSTS text.

    An entry that parses as an IP network (a bare address counts as a single
    address network) is a CIDR; any other entry is a hostname. An entry with a
    "/" that is not a valid network raises ValueError, so a mistyped CIDR
    refuses to boot instead of silently admitting nothing.
    """
    names: set[str] = set()
    networks: list[_Network] = []
    for chunk in (raw or "").split(","):
        entry = _normalize_host(chunk)
        if not entry:
            continue
        try:
            networks.append(ipaddress.ip_network(entry, strict=False))
            continue
        except ValueError:
            if "/" in entry:
                raise ValueError(
                    f"WEBHOOK_ALLOWED_HOSTS entry {entry!r} is not a valid CIDR"
                ) from None
        names.add(entry)
    return AllowedHosts(names=frozenset(names), networks=tuple(networks))


def target_host(url: str) -> str | None:
    """The URL's host, lowercased with any trailing dot removed, or None."""
    try:
        host = urlsplit(url).hostname
    except ValueError:
        return None
    if not host:
        return None
    return _normalize_host(host) or None


def _address_allowed(raw_address: str, allowed: AllowedHosts) -> bool:
    if is_public_address(raw_address):
        return True
    try:
        address = ipaddress.ip_address(raw_address)
    except ValueError:
        return False
    return any(address in network for network in allowed.networks)


def destination_allowed_sync(url: str, allowed: AllowedHosts, resolve: Resolver) -> bool:
    """True when the URL's host is a named allowlist entry, or when every
    address it resolves to is public or inside an allowlisted CIDR. A missing
    host, a resolver error, or an empty answer is False (fail closed). An IP
    literal host is judged as given, without the resolver."""
    host = target_host(url)
    if host is None:
        return False
    if host in allowed.names:
        return True
    try:
        ipaddress.ip_address(host)
        addresses = [host]
    except ValueError:
        try:
            addresses = resolve(host)
        except (OSError, UnicodeError, ValueError):
            return False
    if not addresses:
        return False
    return all(_address_allowed(address, allowed) for address in addresses)


async def destination_allowed(url: str, allowed_hosts: str) -> bool:
    """Async form for request and consumer code: DNS runs off the event loop."""
    allowed = parse_allowed_hosts(allowed_hosts)
    return await asyncio.to_thread(destination_allowed_sync, url, allowed, resolver)
