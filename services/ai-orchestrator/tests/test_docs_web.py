"""Unit tests for the allowlisted web documentation fetch (ADR 0015, issue #31).

Nothing here touches DNS or the network: the resolver is injected and the
transport is an httpx.MockTransport, the same way the other ai-orchestrator
HTTP tests fake their downstreams.
"""

import httpx
import pytest
from app.config import settings
from app.services import docs_web
from app.services.docs_sources import DocsLookupError

PREFIX = "https://docs.example.com/frr/"
PAGE = f"{PREFIX}routing.html"


@pytest.fixture(autouse=True)
def _web_enabled(monkeypatch):
    monkeypatch.setattr(settings, "ai_docs_web_enabled", True)
    monkeypatch.setattr(settings, "ai_docs_web_allowed_prefixes", PREFIX)
    monkeypatch.setattr(settings, "ai_docs_web_max_bytes", 524288)


def _public_resolver(_host: str) -> list[str]:
    return ["93.184.216.34"]


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _html(body: str) -> httpx.Response:
    return httpx.Response(200, headers={"content-type": "text/html; charset=utf-8"}, text=body)


# --- URL normalization --------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("https://Docs.Example.COM/frr/", "https://docs.example.com/frr/"),
        ("https://docs.example.com:443/frr/", "https://docs.example.com/frr/"),
        (
            "https://docs.example.com/frr/page.html?x=1#frag",
            "https://docs.example.com/frr/page.html",
        ),
        ("https://docs.example.com", "https://docs.example.com/"),
        ("https://docs.example.com:8443/frr/", "https://docs.example.com:8443/frr/"),
    ],
)
def test_normalize_url_canonicalizes(raw, expected):
    assert docs_web.normalize_url(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "http://docs.example.com/frr/",
        "ftp://docs.example.com/frr/",
        "file:///etc/passwd",
        "https://user:pass@docs.example.com/frr/",
        "https:///nohost",
        "",
    ],
)
def test_normalize_url_refuses_non_https_and_userinfo(raw):
    assert docs_web.normalize_url(raw) is None


# --- Prefix matching ----------------------------------------------------


def test_prefix_match_accepts_a_url_under_the_prefix():
    assert docs_web.match_prefix(PAGE, [PREFIX]) == PAGE


@pytest.mark.parametrize(
    "candidate",
    [
        "https://docs.example.com/frr-evil/page.html",
        "https://docs.example.com.evil/frr/page.html",
        "https://evil.com/https://docs.example.com/frr/page.html",
        "https://docs.example.com/other/page.html",
        "http://docs.example.com/frr/page.html",
    ],
)
def test_prefix_match_refuses_lookalikes(candidate):
    assert docs_web.match_prefix(candidate, [PREFIX]) is None


def test_a_bare_host_prefix_gets_a_trailing_slash(monkeypatch):
    monkeypatch.setattr(settings, "ai_docs_web_allowed_prefixes", "https://docs.example.com")

    prefixes = docs_web.normalized_prefixes()

    assert prefixes == ["https://docs.example.com/"]
    assert docs_web.match_prefix("https://docs.example.com.evil/x", prefixes) is None
    assert docs_web.match_prefix("https://docs.example.com/x", prefixes) is not None


def test_invalid_prefix_entries_are_dropped(monkeypatch):
    monkeypatch.setattr(
        settings, "ai_docs_web_allowed_prefixes", f"http://plain.example.com/, ,{PREFIX}"
    )

    assert docs_web.normalized_prefixes() == [PREFIX]


# --- Address classes ----------------------------------------------------


@pytest.mark.parametrize(
    ("address", "why"),
    [
        ("127.0.0.1", "loopback"),
        ("10.0.0.5", "private class A"),
        ("172.16.4.4", "private class B"),
        ("192.168.1.27", "private class C"),
        ("169.254.169.254", "link-local metadata service"),
        ("224.0.0.1", "multicast"),
        ("0.0.0.0", "unspecified"),
        ("240.0.0.1", "reserved"),
        ("::1", "IPv6 loopback"),
        ("fd00::1", "IPv6 unique local"),
        ("fe80::1", "IPv6 link-local"),
        ("ff02::1", "IPv6 multicast"),
        ("::", "IPv6 unspecified"),
        ("::ffff:127.0.0.1", "IPv4-mapped loopback"),
        ("::ffff:10.0.0.5", "IPv4-mapped private"),
        ("not-an-address", "unparseable"),
    ],
)
def test_refused_address_classes(address, why):
    assert docs_web.is_public_address(address) is False, why


@pytest.mark.parametrize(
    "address", ["93.184.216.34", "8.8.8.8", "2606:2800:220:1:248:1893:25c8:1946"]
)
def test_public_addresses_are_allowed(address):
    assert docs_web.is_public_address(address) is True


async def test_fetch_refuses_when_any_resolved_address_is_private():
    def mixed_resolver(_host: str) -> list[str]:
        return ["93.184.216.34", "10.1.2.3"]

    async with _client(lambda _r: _html("<p>hi</p>")) as client:
        with pytest.raises(DocsLookupError, match="non-public address"):
            await docs_web.fetch_web_document(
                PAGE, client=client, resolver=mixed_resolver, window=4000
            )


async def test_fetch_refuses_when_the_host_does_not_resolve():
    def failing_resolver(_host: str) -> list[str]:
        raise OSError("nxdomain")

    async with _client(lambda _r: _html("<p>hi</p>")) as client:
        with pytest.raises(DocsLookupError, match="did not resolve"):
            await docs_web.fetch_web_document(
                PAGE, client=client, resolver=failing_resolver, window=4000
            )


# --- Fetch behavior -----------------------------------------------------


async def test_fetch_returns_converted_text():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["user-agent"] == docs_web.USER_AGENT
        assert "authorization" not in request.headers
        assert "x-internal-token" not in request.headers
        return _html("<html><title>FRR routing</title><body><p>ip route works</p></body></html>")

    async with _client(handler) as client:
        doc = await docs_web.fetch_web_document(
            PAGE, client=client, resolver=_public_resolver, window=4000
        )

    assert doc["source"] == "web"
    assert doc["path"] == PAGE
    assert doc["title"] == "FRR routing"
    assert "ip route works" in doc["text"]
    assert doc["next_offset"] is None


async def test_fetch_refuses_a_url_outside_the_allowlist():
    async with _client(lambda _r: _html("<p>hi</p>")) as client:
        with pytest.raises(DocsLookupError, match="not in the allowed prefix list"):
            await docs_web.fetch_web_document(
                "https://docs.example.com/frr-evil/page.html",
                client=client,
                resolver=_public_resolver,
                window=4000,
            )


async def test_fetch_refuses_while_the_feature_is_disabled(monkeypatch):
    monkeypatch.setattr(settings, "ai_docs_web_enabled", False)

    async with _client(lambda _r: _html("<p>hi</p>")) as client:
        with pytest.raises(DocsLookupError, match="disabled"):
            await docs_web.fetch_web_document(
                PAGE, client=client, resolver=_public_resolver, window=4000
            )


async def test_fetch_refuses_with_an_empty_allowlist(monkeypatch):
    monkeypatch.setattr(settings, "ai_docs_web_allowed_prefixes", "")

    async with _client(lambda _r: _html("<p>hi</p>")) as client:
        with pytest.raises(DocsLookupError, match="no web documentation prefixes"):
            await docs_web.fetch_web_document(
                PAGE, client=client, resolver=_public_resolver, window=4000
            )


@pytest.mark.parametrize("content_type", ["application/pdf", "image/png", "application/json", None])
async def test_fetch_refuses_a_non_text_content_type(content_type):
    headers = {"content-type": content_type} if content_type else {}

    async with _client(lambda _r: httpx.Response(200, headers=headers, content=b"x")) as client:
        with pytest.raises(DocsLookupError, match="unsupported content type"):
            await docs_web.fetch_web_document(
                PAGE, client=client, resolver=_public_resolver, window=4000
            )


async def test_fetch_reports_an_http_error_status():
    async with _client(lambda _r: httpx.Response(404, text="nope")) as client:
        with pytest.raises(DocsLookupError, match="HTTP 404"):
            await docs_web.fetch_web_document(
                PAGE, client=client, resolver=_public_resolver, window=4000
            )


async def test_fetch_cuts_the_body_at_the_byte_cap(monkeypatch):
    monkeypatch.setattr(settings, "ai_docs_web_max_bytes", 512)
    body = "<html><body><p>" + ("A" * 5000) + "</p></body></html>"

    async with _client(lambda _r: _html(body)) as client:
        doc = await docs_web.fetch_web_document(
            PAGE, client=client, resolver=_public_resolver, window=100_000
        )

    assert doc["bytes_truncated"] is True
    # The cap is on BYTES read, so the converted text cannot exceed it either.
    assert len(doc["text"]) <= 512
    assert "A" * 400 in doc["text"]


async def test_fetch_pages_with_offset_and_next_offset():
    body = "<html><body><p>" + ("word " * 400) + "</p></body></html>"

    async with _client(lambda _r: _html(body)) as client:
        first = await docs_web.fetch_web_document(
            PAGE, client=client, resolver=_public_resolver, window=200
        )
        assert first["next_offset"] == 200
        second = await docs_web.fetch_web_document(
            PAGE,
            client=client,
            resolver=_public_resolver,
            offset=first["next_offset"],
            window=100_000,
        )

    assert second["offset"] == 200
    assert second["next_offset"] is None
    assert len(first["text"]) == 200


# --- Redirects ----------------------------------------------------------


async def test_fetch_follows_an_allowlisted_redirect():
    target = f"{PREFIX}routing-v2.html"

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == PAGE:
            return httpx.Response(302, headers={"location": target})
        return _html("<html><title>Moved</title><body><p>final</p></body></html>")

    async with _client(handler) as client:
        doc = await docs_web.fetch_web_document(
            PAGE, client=client, resolver=_public_resolver, window=4000
        )

    assert doc["path"] == target
    assert "final" in doc["text"]


async def test_redirect_to_a_private_address_is_refused_mid_chain(monkeypatch):
    """The allowlist alone is not enough: a second allowlisted host that
    resolves to a private address must be refused at the redirect hop."""
    internal = "https://internal.example.com/frr/secret.html"
    monkeypatch.setattr(
        settings, "ai_docs_web_allowed_prefixes", f"{PREFIX},https://internal.example.com/frr/"
    )
    seen: list[str] = []

    def resolver(host: str) -> list[str]:
        seen.append(host)
        return ["10.0.0.9"] if host == "internal.example.com" else ["93.184.216.34"]

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == PAGE:
            return httpx.Response(302, headers={"location": internal})
        raise AssertionError("the redirect target must never be fetched")

    async with _client(handler) as client:
        with pytest.raises(DocsLookupError, match="non-public address"):
            await docs_web.fetch_web_document(PAGE, client=client, resolver=resolver, window=4000)

    assert seen == ["docs.example.com", "internal.example.com"]


async def test_redirect_outside_the_allowlist_is_refused():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "https://evil.example.com/x.html"})

    async with _client(handler) as client:
        with pytest.raises(DocsLookupError, match="redirect target is not in the allowed"):
            await docs_web.fetch_web_document(
                PAGE, client=client, resolver=_public_resolver, window=4000
            )


async def test_redirect_chain_is_bounded():
    def handler(request: httpx.Request) -> httpx.Response:
        nxt = f"{PREFIX}hop-{len(str(request.url))}.html"
        return httpx.Response(302, headers={"location": nxt})

    async with _client(handler) as client:
        with pytest.raises(DocsLookupError, match="too many redirects"):
            await docs_web.fetch_web_document(
                PAGE, client=client, resolver=_public_resolver, window=4000
            )


async def test_redirect_without_a_location_is_refused():
    async with _client(lambda _r: httpx.Response(302)) as client:
        with pytest.raises(DocsLookupError, match="redirect without a location"):
            await docs_web.fetch_web_document(
                PAGE, client=client, resolver=_public_resolver, window=4000
            )
