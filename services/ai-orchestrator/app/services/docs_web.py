"""Allowlisted web documentation fetch for the reservation assistant (ADR 0015, issue #31).

This is the only part of the docs tools that reaches the network, and it is
off unless an operator sets AI_DOCS_WEB_ENABLED and at least one allowed
prefix. Every fetch is checked twice over:

- the URL must be https, must normalize cleanly (dot segments in the path
  resolved first, issue #1055), and must match one of the configured prefixes
  as a plain string prefix;
- every address the host resolves to must be public, so an allowlisted
  hostname that happens to resolve to 127.0.0.1, a 10.x address, or a
  link-local address cannot be used to reach a service inside the stack.

Redirects are followed by hand, at most three, and each hop is re-checked the
same way, which is the reason follow_redirects is never delegated to httpx: a
302 to http://169.254.169.254/ must be refused, not followed.

The request carries no HERD credentials, no caller JWT, and no cookie; only a
fixed User-Agent. The response must declare a text content type and is read to
at most AI_DOCS_WEB_MAX_BYTES before being cut. The per-hop timeout is the
dispatcher's existing HTTP client timeout.

The resolver is injected so the unit tests cover every refused address class
without touching DNS or the network. The address predicate, the resolver type,
and the default resolver live once in herd_common.public_address (shared with
integration's webhook destination check) and are re-exported here under their
old names.
"""

from __future__ import annotations

import logging
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx
from herd_common.public_address import Resolver, default_resolver, is_public_address

from app.config import settings
from app.services.docs_sources import DocsLookupError, html_to_text, normalize_plain_text

logger = logging.getLogger(__name__)

MAX_REDIRECTS = 3
USER_AGENT = "HERD-ai-orchestrator docs-lookup"
ALLOWED_CONTENT_TYPES = frozenset({"text/html", "text/plain", "text/markdown", "text/x-markdown"})
REDIRECT_STATUS = frozenset({301, 302, 303, 307, 308})


def _dot_segment(segment: str) -> str | None:
    """ "." or ".." when the segment is a dot segment, else None.

    Percent-encoded dots count: a server may decode %2e before it collapses
    segments, so the encoded spellings are resolved here too.
    """
    decoded = segment.lower().replace("%2e", ".")
    return decoded if decoded in (".", "..") else None


def remove_dot_segments(path: str) -> str:
    """Resolve "." and ".." segments in an absolute path (RFC 3986 5.2.4).

    The result is the path the server ends up serving, so the prefix compare
    runs on that path rather than on the text as written: a written path that
    starts with an allowlisted prefix and climbs out of it with ".." no longer
    matches (issue #1055). A ".." at the root stays at the root, and a trailing
    dot segment leaves a trailing slash, as RFC 3986 specifies.
    """
    segments = path.split("/")[1:] if path.startswith("/") else path.split("/")
    output: list[str] = []
    for index, segment in enumerate(segments):
        last = index == len(segments) - 1
        dot = _dot_segment(segment)
        if dot == ".":
            if last:
                output.append("")
            continue
        if dot == "..":
            if output:
                output.pop()
            if last:
                output.append("")
            continue
        output.append(segment)
    return "/" + "/".join(output)


def normalize_url(raw: str) -> str | None:
    """Normalize an https URL for prefix matching, or None if it is not one.

    Refuses (returns None for) a URL that carries userinfo. Lowercases the
    scheme and host, drops the default port, drops the query and the fragment,
    and resolves dot segments in the path (literal or percent-encoded), but
    otherwise leaves the path as written (percent-encoding included) so
    matching stays a plain string compare. The normalized URL is also the one
    fetched, so the path that was matched is the path that is requested.
    """
    if not raw or not isinstance(raw, str):
        return None
    try:
        parts = urlsplit(raw.strip())
    except ValueError:
        return None
    if parts.scheme.lower() != "https":
        return None
    try:
        host = parts.hostname
    except ValueError:
        return None
    if not host:
        return None
    if parts.username or parts.password:
        return None
    host = host.lower()
    try:
        port = parts.port
    except ValueError:
        return None
    netloc = host if port in (None, 443) else f"{host}:{port}"
    path = remove_dot_segments(parts.path or "/")
    return urlunsplit(("https", netloc, path, "", ""))


def normalized_prefixes() -> list[str]:
    """The configured allowlist, normalized. An entry with no path gets a
    trailing slash so a bare host prefix cannot match a longer hostname
    (`https://docs.example.com` must not admit `https://docs.example.com.evil/`).
    Operators should end every prefix with "/" for the same reason at the path
    level."""
    prefixes: list[str] = []
    for chunk in settings.ai_docs_web_allowed_prefixes.split(","):
        entry = chunk.strip()
        if not entry:
            continue
        normalized = normalize_url(entry)
        if normalized is None:
            logger.warning("docs_web_prefix_invalid", extra={"prefix": entry})
            continue
        prefixes.append(normalized)
    return prefixes


def match_prefix(url: str, prefixes: list[str]) -> str | None:
    """Return the normalized URL when it sits under one of the prefixes."""
    normalized = normalize_url(url)
    if normalized is None:
        return None
    for prefix in prefixes:
        if normalized.startswith(prefix):
            return normalized
    return None


def assert_public_host(url: str, resolver: Resolver) -> None:
    """Resolve the URL's host and refuse unless EVERY answer is public.

    All of them, not just the first: a host that answers with one public and
    one loopback address would otherwise be a coin flip at connect time.
    """
    host = urlsplit(url).hostname
    if not host:
        raise DocsLookupError("web fetch refused: no host in URL")
    try:
        addresses = resolver(host)
    except OSError as exc:
        raise DocsLookupError(f"web fetch refused: host {host} did not resolve") from exc
    if not addresses:
        raise DocsLookupError(f"web fetch refused: host {host} did not resolve")
    for address in addresses:
        if not is_public_address(address):
            raise DocsLookupError(
                f"web fetch refused: host {host} resolves to the non-public address {address}"
            )


def _content_type_allowed(header: str | None) -> bool:
    if not header:
        return False
    return header.split(";", 1)[0].strip().lower() in ALLOWED_CONTENT_TYPES


async def _read_capped(response: httpx.Response, max_bytes: int) -> tuple[bytes, bool]:
    body = bytearray()
    async for chunk in response.aiter_bytes():
        body.extend(chunk)
        if len(body) >= max_bytes:
            return bytes(body[:max_bytes]), True
    return bytes(body), False


async def fetch_web_document(
    url: str,
    *,
    client: httpx.AsyncClient,
    resolver: Resolver = default_resolver,
    offset: int = 0,
    window: int,
) -> dict:
    """Fetch one allowlisted documentation URL and return a read_doc payload.

    Raises DocsLookupError for anything refused: a disabled feature, an empty
    allowlist, a URL outside the allowlist, a non-public address at any hop,
    too many redirects, a non-text content type, or a transport failure. The
    caller turns that into a tool error the model can recover from.
    """
    if not settings.ai_docs_web_enabled:
        raise DocsLookupError("web documentation lookup is disabled")
    prefixes = normalized_prefixes()
    if not prefixes:
        raise DocsLookupError("no web documentation prefixes are allowed")

    current = match_prefix(url, prefixes)
    if current is None:
        raise DocsLookupError(f"web fetch refused: {url!r} is not in the allowed prefix list")

    max_bytes = max(1, int(settings.ai_docs_web_max_bytes))
    headers = {"User-Agent": USER_AGENT, "Accept": "text/html, text/plain, text/markdown"}

    for _hop in range(MAX_REDIRECTS + 1):
        assert_public_host(current, resolver)
        request = client.build_request("GET", current, headers=headers)
        response = await client.send(request, stream=True, follow_redirects=False)
        try:
            if response.status_code in REDIRECT_STATUS:
                location = response.headers.get("location")
                if not location:
                    raise DocsLookupError("web fetch refused: redirect without a location")
                candidate = urljoin(current, location)
                nxt = match_prefix(candidate, prefixes)
                if nxt is None:
                    raise DocsLookupError(
                        "web fetch refused: redirect target is not in the allowed prefix list"
                    )
                current = nxt
                continue
            if response.status_code >= 400:
                raise DocsLookupError(f"web fetch failed: HTTP {response.status_code}")
            content_type = response.headers.get("content-type")
            if not _content_type_allowed(content_type):
                raise DocsLookupError(
                    f"web fetch refused: unsupported content type {content_type!r}"
                )
            body, truncated = await _read_capped(response, max_bytes)
        finally:
            await response.aclose()

        raw = body.decode("utf-8", errors="replace")
        if content_type.split(";", 1)[0].strip().lower() == "text/html":
            title, text = html_to_text(raw)
        else:
            text = normalize_plain_text(raw)
            title = ""
        start = max(0, int(offset))
        window = max(1, int(window))
        chunk = text[start : start + window]
        next_offset = start + len(chunk) if start + len(chunk) < len(text) else None
        return {
            "source": "web",
            "path": current,
            "title": title or current,
            "text": chunk,
            "offset": start,
            "next_offset": next_offset,
            "bytes_truncated": truncated,
        }

    raise DocsLookupError("web fetch refused: too many redirects")
