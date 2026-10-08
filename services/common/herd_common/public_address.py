"""Public-address predicate shared by every outbound fetch or POST to an
operator- or admin-supplied destination.

One copy, used by ai-orchestrator's web documentation fetch
(app/services/docs_web.py) and by integration's webhook destination check
(app/services/destination.py). A destination is acceptable only when every
address its host resolves to passes `is_public_address`; callers inject the
resolver so tests cover every address class without DNS.
"""

from __future__ import annotations

import ipaddress
import socket
from collections.abc import Callable

Resolver = Callable[[str], list[str]]

# RFC 6598 shared address space (carrier-grade NAT; also Tailscale and some
# cloud and Kubernetes networks). ipaddress does not count it as private, so it
# is refused explicitly (issue #1055).
SHARED_ADDRESS_SPACE = ipaddress.ip_network("100.64.0.0/10")


def default_resolver(host: str) -> list[str]:
    """Resolve a hostname to every address it answers with. Sorted so the
    refusal a caller sees for a multi-address host is deterministic."""
    infos = socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
    return sorted({info[4][0] for info in infos})


def is_public_address(raw_address: str) -> bool:
    """True only for an address the container may legitimately talk to.

    Refuses loopback, private (RFC 1918 and IPv6 unique-local), shared address
    space (100.64.0.0/10), link-local (which is where cloud metadata services
    live), multicast, unspecified, and reserved ranges, plus the IPv4-mapped,
    6to4, and Teredo IPv6 forms, which are the usual way a private address
    sneaks past a naive IPv4-only check. Anything that does not parse as an IP
    address is refused.
    """
    try:
        address = ipaddress.ip_address(raw_address)
    except ValueError:
        return False
    if isinstance(address, ipaddress.IPv6Address):
        if address.ipv4_mapped is not None:
            return False
        if address.sixtofour is not None or address.teredo is not None:
            return False
    elif address in SHARED_ADDRESS_SPACE:
        return False
    return not (
        address.is_loopback
        or address.is_private
        or address.is_link_local
        or address.is_multicast
        or address.is_unspecified
        or address.is_reserved
    )
