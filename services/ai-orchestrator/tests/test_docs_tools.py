"""Dispatch-level tests for the documentation tools (ADR 0015, issue #31).

Covers the advertisement gate, the dispatch-boundary refusals (which is where
the gates actually bind: a model can emit a tool call by name whatever the
advertised list says), and the read window the result cap dictates.
"""

import json
import uuid
from pathlib import Path

import httpx
import pytest
from app.config import settings
from app.services import docs_sources, docs_web
from app.services.ai_client import (
    RESERVATION_ASSISTANT_DOCS_TOOLS_PROMPT,
    reservation_assistant_system_prompt,
)
from app.services.tools import (
    DOCS_TOOL_DEFINITIONS,
    DOCS_TOOL_NAMES,
    ToolDispatcher,
    docs_tools_enabled,
    get_active_tool_definitions,
)

RESERVATION_ID = uuid.UUID("11111111-1111-1111-1111-111111111111")
PREFIX = "https://docs.example.com/frr/"
PAGE = f"{PREFIX}routing.html"


@pytest.fixture(autouse=True)
def _docs_defaults(monkeypatch):
    """Shipped defaults, with the manual root pointing nowhere so no source is
    enabled until a test sets one up."""
    monkeypatch.setattr(settings, "ai_docs_manual_enabled", True)
    monkeypatch.setattr(settings, "ai_docs_corpus_dirs", "")
    monkeypatch.setattr(settings, "ai_docs_web_enabled", False)
    monkeypatch.setattr(settings, "ai_docs_web_allowed_prefixes", "")
    monkeypatch.setattr(settings, "ai_docs_index_ttl_seconds", 600)
    monkeypatch.setattr(settings, "ai_write_tools_enabled", False)
    monkeypatch.setattr(docs_sources, "MANUAL_ROOT", Path("/nonexistent/herd/manual"))
    docs_sources.reset_caches()
    yield
    docs_sources.reset_caches()


def _manual(tmp_path, monkeypatch, pages: dict[str, str]) -> Path:
    root = tmp_path / "manual"
    for name, body in pages.items():
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body, encoding="utf-8")
    monkeypatch.setattr(docs_sources, "MANUAL_ROOT", root)
    docs_sources.reset_caches()
    return root


def _dispatcher(handler=None, *, char_cap: int = 8000, resolver=None) -> ToolDispatcher:
    handler = handler or (lambda _r: httpx.Response(404, json={"detail": "unmocked"}))
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return ToolDispatcher(
        token="test-token",
        reservation_id=RESERVATION_ID,
        http_client=client,
        char_cap=char_cap,
        docs_resolver=resolver or (lambda _host: ["93.184.216.34"]),
    )


# --- Advertisement gate -------------------------------------------------


def test_docs_tools_are_absent_when_no_source_is_enabled():
    assert docs_tools_enabled() is False
    names = {d["name"] for d in get_active_tool_definitions()}
    assert names.isdisjoint(DOCS_TOOL_NAMES)


def test_docs_tools_are_advertised_with_the_manual_alone(tmp_path, monkeypatch):
    _manual(tmp_path, monkeypatch, {"index.html": "<html><body><p>hello</p></body></html>"})

    assert docs_tools_enabled() is True
    names = {d["name"] for d in get_active_tool_definitions()}
    assert DOCS_TOOL_NAMES <= names
    assert {"search_docs", "read_doc"} == set(DOCS_TOOL_NAMES)


def test_docs_tools_are_advertised_for_web_only(monkeypatch):
    monkeypatch.setattr(settings, "ai_docs_web_enabled", True)
    monkeypatch.setattr(settings, "ai_docs_web_allowed_prefixes", PREFIX)

    assert docs_tools_enabled() is True
    assert DOCS_TOOL_NAMES <= {d["name"] for d in get_active_tool_definitions()}


def test_web_enabled_with_no_prefixes_is_not_a_source(monkeypatch):
    monkeypatch.setattr(settings, "ai_docs_web_enabled", True)

    assert docs_tools_enabled() is False


def test_tool_definitions_declare_their_schemas():
    by_name = {d["name"]: d for d in DOCS_TOOL_DEFINITIONS}
    assert by_name["search_docs"]["input_schema"]["required"] == ["query"]
    assert by_name["read_doc"]["input_schema"]["required"] == ["source", "path"]
    for definition in DOCS_TOOL_DEFINITIONS:
        assert definition["input_schema"]["additionalProperties"] is False


def test_system_prompt_mentions_the_docs_tools_only_when_enabled(tmp_path, monkeypatch):
    assert RESERVATION_ASSISTANT_DOCS_TOOLS_PROMPT not in reservation_assistant_system_prompt()

    _manual(tmp_path, monkeypatch, {"index.html": "<html><body><p>hello</p></body></html>"})

    assert RESERVATION_ASSISTANT_DOCS_TOOLS_PROMPT in reservation_assistant_system_prompt()


# --- Dispatch-boundary gates -------------------------------------------


async def test_dispatch_refuses_docs_tools_when_every_source_is_disabled():
    async with _dispatcher() as dispatcher:
        result = await dispatcher.dispatch("search_docs", {"query": "ldap"})

    assert result["is_error"] is True
    assert "documentation tools are disabled" in json.loads(result["content"])["message"]


async def test_dispatch_refuses_a_web_read_while_web_is_disabled(tmp_path, monkeypatch):
    """The corpus source keeps the tools advertised, so this exercises the
    web-specific gate rather than the blanket one."""
    _manual(tmp_path, monkeypatch, {"index.html": "<html><body><p>hello</p></body></html>"})

    def handler(_request):
        raise AssertionError("no HTTP request may be made while web lookup is disabled")

    async with _dispatcher(handler) as dispatcher:
        result = await dispatcher.dispatch("read_doc", {"source": "web", "path": PAGE})

    assert result["is_error"] is True
    assert "web documentation lookup is disabled" in json.loads(result["content"])["message"]


async def test_dispatch_refuses_an_unknown_source(tmp_path, monkeypatch):
    _manual(tmp_path, monkeypatch, {"index.html": "<html><body><p>hello</p></body></html>"})

    async with _dispatcher() as dispatcher:
        read = await dispatcher.dispatch("read_doc", {"source": "vendor-x", "path": "a.md"})
        search = await dispatcher.dispatch("search_docs", {"query": "x", "source": "vendor-x"})

    assert read["is_error"] is True
    assert "unknown documentation source" in json.loads(read["content"])["message"]
    assert search["is_error"] is True
    assert "unknown documentation source" in json.loads(search["content"])["message"]


async def test_search_docs_refuses_the_web_source(tmp_path, monkeypatch):
    _manual(tmp_path, monkeypatch, {"index.html": "<html><body><p>hello</p></body></html>"})

    async with _dispatcher() as dispatcher:
        result = await dispatcher.dispatch("search_docs", {"query": "x", "source": "web"})

    assert result["is_error"] is True
    assert "not searchable" in json.loads(result["content"])["message"]


async def test_search_docs_requires_a_query(tmp_path, monkeypatch):
    _manual(tmp_path, monkeypatch, {"index.html": "<html><body><p>hello</p></body></html>"})

    async with _dispatcher() as dispatcher:
        result = await dispatcher.dispatch("search_docs", {"query": "   "})

    assert result["is_error"] is True
    assert "non-empty" in json.loads(result["content"])["message"]


async def test_read_doc_refuses_a_traversal_path(tmp_path, monkeypatch):
    (tmp_path / "outside.md").write_text("secret", encoding="utf-8")
    _manual(tmp_path, monkeypatch, {"index.html": "<html><body><p>hello</p></body></html>"})

    async with _dispatcher() as dispatcher:
        result = await dispatcher.dispatch(
            "read_doc", {"source": docs_sources.MANUAL_SOURCE_NAME, "path": "../outside.md"}
        )

    assert result["is_error"] is True
    body = json.loads(result["content"])
    assert "not found" in body["message"]
    assert "secret" not in result["content"]


async def test_read_doc_refuses_a_negative_offset(tmp_path, monkeypatch):
    _manual(tmp_path, monkeypatch, {"a.md": "body"})

    async with _dispatcher() as dispatcher:
        result = await dispatcher.dispatch(
            "read_doc",
            {"source": docs_sources.MANUAL_SOURCE_NAME, "path": "a.md", "offset": -5},
        )

    assert result["is_error"] is True
    assert "negative" in json.loads(result["content"])["message"]


# --- Happy paths --------------------------------------------------------


async def test_search_then_read_a_manual_page(tmp_path, monkeypatch):
    _manual(
        tmp_path,
        monkeypatch,
        {
            "admin-ldap-sync.html": (
                "<html><title>HERD Manual: LDAP group sync</title><body>"
                "<p>LDAP group sync maps directory groups into HERD groups.</p>"
                "</body></html>"
            ),
            "quickstart.html": (
                "<html><title>Quickstart</title><body><p>Book a device.</p></body></html>"
            ),
        },
    )

    async with _dispatcher() as dispatcher:
        search = await dispatcher.dispatch("search_docs", {"query": "ldap group sync"})
        hits = json.loads(search["content"])["hits"]
        read = await dispatcher.dispatch(
            "read_doc", {"source": hits[0]["source"], "path": hits[0]["path"]}
        )

    assert search["is_error"] is False
    assert hits[0]["path"] == "admin-ldap-sync.html"
    assert hits[0]["source"] == docs_sources.MANUAL_SOURCE_NAME
    body = json.loads(read["content"])
    assert body["title"] == "HERD Manual: LDAP group sync"
    assert "directory groups" in body["text"]
    assert body["next_offset"] is None
    assert [record.name for record in dispatcher.call_log] == ["search_docs", "read_doc"]


async def test_read_doc_window_is_derived_from_the_result_cap(tmp_path, monkeypatch):
    _manual(tmp_path, monkeypatch, {"long.txt": "x" * 20_000})

    async with _dispatcher(char_cap=2000) as dispatcher:
        result = await dispatcher.dispatch(
            "read_doc", {"source": docs_sources.MANUAL_SOURCE_NAME, "path": "long.txt"}
        )

    body = json.loads(result["content"])
    # The window leaves headroom under the cap, so the payload is never cut by
    # the dispatcher's truncation and next_offset describes text the model saw.
    assert len(body["text"]) == 1200
    assert body["next_offset"] == 1200
    assert "[truncated:" not in result["content"]
    assert len(result["content"]) <= 2000


async def test_read_doc_pages_through_a_long_page(tmp_path, monkeypatch):
    _manual(tmp_path, monkeypatch, {"long.txt": "y" * 3000})

    async with _dispatcher(char_cap=2000) as dispatcher:
        first = json.loads(
            (
                await dispatcher.dispatch(
                    "read_doc", {"source": docs_sources.MANUAL_SOURCE_NAME, "path": "long.txt"}
                )
            )["content"]
        )
        second = json.loads(
            (
                await dispatcher.dispatch(
                    "read_doc",
                    {
                        "source": docs_sources.MANUAL_SOURCE_NAME,
                        "path": "long.txt",
                        "offset": first["next_offset"],
                    },
                )
            )["content"]
        )

    assert first["next_offset"] == 1200
    assert second["offset"] == 1200
    # Two windows of 1200 leave 600 characters, so the page is not done yet.
    assert len(first["text"]) + len(second["text"]) == 2400
    assert second["next_offset"] == 2400


async def test_read_doc_fetches_an_allowlisted_web_page(monkeypatch):
    monkeypatch.setattr(settings, "ai_docs_web_enabled", True)
    monkeypatch.setattr(settings, "ai_docs_web_allowed_prefixes", PREFIX)

    def handler(request: httpx.Request) -> httpx.Response:
        assert "authorization" not in request.headers
        return httpx.Response(
            200,
            headers={"content-type": "text/html"},
            text=(
                "<html><title>FRR static routes</title>"
                "<body><p>ip route 0.0.0.0/0</p></body></html>"
            ),
        )

    async with _dispatcher(handler) as dispatcher:
        result = await dispatcher.dispatch("read_doc", {"source": "web", "path": PAGE})

    body = json.loads(result["content"])
    assert result["is_error"] is False
    assert body["source"] == "web"
    assert body["title"] == "FRR static routes"
    assert "ip route" in body["text"]


async def test_read_doc_refuses_a_web_page_that_resolves_privately(monkeypatch):
    monkeypatch.setattr(settings, "ai_docs_web_enabled", True)
    monkeypatch.setattr(settings, "ai_docs_web_allowed_prefixes", PREFIX)

    def handler(_request):
        raise AssertionError("a private address must be refused before any request")

    async with _dispatcher(handler, resolver=lambda _host: ["127.0.0.1"]) as dispatcher:
        result = await dispatcher.dispatch("read_doc", {"source": "web", "path": PAGE})

    assert result["is_error"] is True
    assert "non-public address" in json.loads(result["content"])["message"]


def test_default_resolver_is_used_when_none_is_injected():
    dispatcher = ToolDispatcher(token="t", reservation_id=RESERVATION_ID)
    assert dispatcher._docs_resolver is docs_web.default_resolver
