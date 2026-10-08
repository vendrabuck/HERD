"""Webhook destinations must resolve to public addresses; the delivery ledger
carries a status or a class name only.

Covers app/services/destination.py and its two callers: registration
(`create_webhook`, 422 and nothing stored) and delivery (`deliver_one`, a
`failed` row with the fixed text and no POST). The resolver is the module
attribute `destination.resolver`; tests/conftest.py points it at a fixed public
answer and each test here replaces it as needed, so no test touches DNS.
"""

import logging
import socket
import uuid

import httpx
import pytest
from app.config import Settings, settings
from app.database import Base, get_db
from app.main import app
from app.models.webhook import WebhookDelivery, WebhookSubscription
from app.services import delivery as delivery_mod
from app.services import destination
from app.services.delivery import Target, deliver_one
from httpx import ASGITransport, AsyncClient
from jose import jwt
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

SECRET = "test-secret"
REFUSED = "target_url must resolve to a public address"


@pytest.fixture
async def session_factory():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


@pytest.fixture(autouse=True)
def _clear_overrides():
    yield
    app.dependency_overrides.clear()


def _admin_headers() -> dict:
    token = jwt.encode({"sub": str(uuid.uuid4()), "role": "admin"}, SECRET, algorithm="HS256")
    return {"Authorization": f"Bearer {token}"}


def _client(session_factory) -> AsyncClient:
    async def _override_get_db():
        async with session_factory() as s:
            yield s

    app.dependency_overrides[get_db] = _override_get_db
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


def _resolver(answers, calls=None):
    """A resolver seam: returns `answers` (or raises it when it is an exception)."""

    def resolve(host):
        if calls is not None:
            calls.append(host)
        if isinstance(answers, BaseException):
            raise answers
        return list(answers)

    return resolve


async def _register(session_factory, url: str):
    async with _client(session_factory) as c:
        return await c.post(
            "/webhooks",
            json={"target_url": url, "event_types": ["reservation.created"]},
            headers=_admin_headers(),
        )


async def _subscription_count(session_factory) -> int:
    async with session_factory() as s:
        return (await s.execute(select(func.count()).select_from(WebhookSubscription))).scalar()


# --- registration ------------------------------------------------------------


@pytest.mark.parametrize(
    "address,why",
    [
        ("127.0.0.1", "loopback"),
        ("10.0.0.5", "RFC 1918 class A"),
        ("172.18.0.5", "RFC 1918 class B, a compose network"),
        ("192.168.1.27", "RFC 1918 class C"),
        ("169.254.169.254", "link-local metadata service"),
        ("100.64.0.1", "shared address space"),
        ("224.0.0.1", "multicast"),
        ("0.0.0.0", "unspecified"),
        ("::1", "IPv6 loopback"),
        ("fd00::1", "IPv6 unique local"),
        ("fe80::1", "IPv6 link-local"),
        ("ff02::1", "IPv6 multicast"),
        ("::", "IPv6 unspecified"),
        ("::ffff:10.0.0.5", "IPv4-mapped private"),
    ],
)
async def test_webhook_target_must_be_public(session_factory, monkeypatch, address, why):
    monkeypatch.setattr(destination, "resolver", _resolver([address]))
    resp = await _register(session_factory, "https://hooks.example.com/in")
    assert resp.status_code == 422, why
    assert resp.json() == {"detail": REFUSED}
    assert await _subscription_count(session_factory) == 0


async def test_webhook_target_public_address_is_accepted(session_factory, monkeypatch):
    calls = []
    monkeypatch.setattr(destination, "resolver", _resolver(["93.184.216.34"], calls))
    resp = await _register(session_factory, "https://Hooks.Example.com./in")
    assert resp.status_code == 201, resp.text
    assert calls == ["hooks.example.com"]
    assert await _subscription_count(session_factory) == 1


async def test_webhook_target_refused_when_any_answer_is_not_public(session_factory, monkeypatch):
    monkeypatch.setattr(destination, "resolver", _resolver(["93.184.216.34", "10.1.2.3"]))
    resp = await _register(session_factory, "https://hooks.example.com/in")
    assert resp.status_code == 422
    assert resp.json() == {"detail": REFUSED}


@pytest.mark.parametrize(
    "answers",
    [socket.gaierror(socket.EAI_NONAME, "Name or service not known"), UnicodeError("idna"), []],
)
async def test_webhook_target_refused_when_host_does_not_resolve(
    session_factory, monkeypatch, answers
):
    monkeypatch.setattr(destination, "resolver", _resolver(answers))
    resp = await _register(session_factory, "https://hooks.example.com/in")
    assert resp.status_code == 422
    assert resp.json() == {"detail": REFUSED}


@pytest.mark.parametrize(
    "url",
    ["http://127.0.0.1:8000/hook", "http://[::1]/hook", "http://10.0.0.5/hook", "http:///hook"],
)
async def test_webhook_target_ip_literal_is_judged_without_resolving(
    session_factory, monkeypatch, url
):
    calls = []
    monkeypatch.setattr(destination, "resolver", _resolver(["93.184.216.34"], calls))
    resp = await _register(session_factory, url)
    assert resp.status_code == 422
    assert resp.json() == {"detail": REFUSED}
    assert calls == []


async def test_allowed_hosts_admits_a_named_host_without_resolving(session_factory, monkeypatch):
    calls = []
    monkeypatch.setattr(destination, "resolver", _resolver(["172.18.0.5"], calls))
    monkeypatch.setattr(settings, "webhook_allowed_hosts", " Integration , other.internal ")
    resp = await _register(session_factory, "http://integration:8000/webhooks/echo")
    assert resp.status_code == 201, resp.text
    assert calls == []


async def test_allowed_hosts_name_match_is_exact(session_factory, monkeypatch):
    monkeypatch.setattr(destination, "resolver", _resolver(["172.18.0.5"]))
    monkeypatch.setattr(settings, "webhook_allowed_hosts", "integration")
    resp = await _register(session_factory, "http://integration.example.net/hook")
    assert resp.status_code == 422
    assert resp.json() == {"detail": REFUSED}


async def test_allowed_hosts_admits_addresses_inside_a_cidr(session_factory, monkeypatch):
    monkeypatch.setattr(destination, "resolver", _resolver(["172.18.0.5", "93.184.216.34"]))
    monkeypatch.setattr(settings, "webhook_allowed_hosts", "172.16.0.0/12")
    resp = await _register(session_factory, "http://receiver.internal/hook")
    assert resp.status_code == 201, resp.text


async def test_allowed_hosts_cidr_does_not_admit_other_private_addresses(
    session_factory, monkeypatch
):
    monkeypatch.setattr(destination, "resolver", _resolver(["172.18.0.5", "10.0.0.1"]))
    monkeypatch.setattr(settings, "webhook_allowed_hosts", "172.16.0.0/12")
    resp = await _register(session_factory, "http://receiver.internal/hook")
    assert resp.status_code == 422
    assert resp.json() == {"detail": REFUSED}


async def test_allowed_hosts_single_address_entry_admits_that_literal(session_factory, monkeypatch):
    monkeypatch.setattr(settings, "webhook_allowed_hosts", "10.0.0.5")
    resp = await _register(session_factory, "http://10.0.0.5:9000/hook")
    assert resp.status_code == 201, resp.text


def test_parse_allowed_hosts_splits_names_and_networks():
    allowed = destination.parse_allowed_hosts("integration, 10.0.0.0/8 ,,FD00::/8, Host.Example.")
    assert allowed.names == frozenset({"integration", "host.example"})
    assert [str(n) for n in allowed.networks] == ["10.0.0.0/8", "fd00::/8"]


def test_parse_allowed_hosts_empty_admits_nothing():
    allowed = destination.parse_allowed_hosts("")
    assert allowed.names == frozenset()
    assert allowed.networks == ()


@pytest.mark.parametrize("bad", ["10.0.0.0/33", "integration/8", "fd00::/200"])
def test_malformed_cidr_refuses_to_boot(bad):
    with pytest.raises(ValueError, match="WEBHOOK_ALLOWED_HOSTS entry"):
        Settings(webhook_allowed_hosts=bad)


def test_default_resolver_is_the_shared_one():
    from herd_common.public_address import default_resolver

    assert destination.default_resolver is default_resolver


# --- delivery ----------------------------------------------------------------


class _FakeResponse:
    def __init__(self, status_code: int):
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                "Server error '500' for url 'http://10.9.8.7/secret-path'",
                request=httpx.Request("POST", "http://x"),
                response=self,
            )


def _install_fake_httpx(monkeypatch, *, status_code=None, raise_exc=None):
    record = {"calls": [], "client_kwargs": []}

    class _FakeClient:
        def __init__(self, *args, **kwargs):
            record["client_kwargs"].append(kwargs)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, content=None, headers=None):
            record["calls"].append(url)
            if raise_exc is not None:
                raise raise_exc
            return _FakeResponse(status_code)

    monkeypatch.setattr(delivery_mod.httpx, "AsyncClient", _FakeClient)
    return record


async def _target(session_factory, url: str) -> Target:
    async with session_factory() as s:
        sub = WebhookSubscription(target_url=url, event_types=["reservation.created"], secret="k")
        s.add(sub)
        await s.commit()
        return Target(id=sub.id, target_url=sub.target_url, secret=sub.secret)


async def _rows(session_factory, subscription_id):
    async with session_factory() as s:
        return (
            (
                await s.execute(
                    select(WebhookDelivery).where(
                        WebhookDelivery.subscription_id == subscription_id
                    )
                )
            )
            .scalars()
            .all()
        )


async def _deliver(session_factory, target, event_id="evt-1", attempts=3):
    return await deliver_one(
        session_factory,
        target,
        b'{"event":"reservation.created"}',
        event_id,
        "reservation.created",
        timeout=1.0,
        attempts=attempts,
    )


@pytest.mark.parametrize(
    "answers", [["10.0.0.5"], ["93.184.216.34", "127.0.0.1"], socket.gaierror("nope"), []]
)
async def test_delivery_to_a_destination_not_allowed_is_failed_and_not_sent(
    session_factory, monkeypatch, answers
):
    """A subscription stored before the rule, or whose host now resolves
    elsewhere, is checked again at delivery: nothing is POSTed, one `failed`
    row with the fixed text, no attempts counted, no retry."""
    record = _install_fake_httpx(monkeypatch, status_code=200)
    target = await _target(session_factory, "https://hooks.example.com/in")
    monkeypatch.setattr(destination, "resolver", _resolver(answers))

    assert await _deliver(session_factory, target) == "failed"

    assert record["calls"] == []
    rows = await _rows(session_factory, target.id)
    assert len(rows) == 1
    assert rows[0].status == "failed"
    assert rows[0].attempts == 0
    assert rows[0].response_status is None
    assert rows[0].last_error == "destination not allowed"
    assert rows[0].delivered_at is None


async def test_delivery_to_an_internal_literal_is_failed(session_factory, monkeypatch):
    record = _install_fake_httpx(monkeypatch, status_code=200)
    target = await _target(session_factory, "http://169.254.169.254/latest/meta-data/")
    assert await _deliver(session_factory, target) == "failed"
    assert record["calls"] == []
    assert (await _rows(session_factory, target.id))[0].last_error == "destination not allowed"


async def test_delivery_to_an_allowed_host_is_sent(session_factory, monkeypatch):
    record = _install_fake_httpx(monkeypatch, status_code=200)
    monkeypatch.setattr(destination, "resolver", _resolver(["172.18.0.5"]))
    monkeypatch.setattr(settings, "webhook_allowed_hosts", "integration")
    target = await _target(session_factory, "http://integration:8000/webhooks/echo")
    assert await _deliver(session_factory, target) == "delivered"
    assert record["calls"] == ["http://integration:8000/webhooks/echo"]


async def test_failed_destination_row_is_retried_on_redelivery(session_factory, monkeypatch):
    """A `failed` row is not `delivered`, so a redelivered event checks again
    and sends once the destination is allowed."""
    record = _install_fake_httpx(monkeypatch, status_code=200)
    target = await _target(session_factory, "https://hooks.example.com/in")
    monkeypatch.setattr(destination, "resolver", _resolver(["10.0.0.5"]))
    assert await _deliver(session_factory, target) == "failed"
    monkeypatch.setattr(destination, "resolver", _resolver(["93.184.216.34"]))
    assert await _deliver(session_factory, target) == "delivered"
    rows = await _rows(session_factory, target.id)
    assert len(rows) == 1
    assert rows[0].status == "delivered"
    assert rows[0].last_error is None
    assert len(record["calls"]) == 1


async def test_delivery_ledger_records_the_answer_status_only(session_factory, monkeypatch, caplog):
    _install_fake_httpx(monkeypatch, status_code=500)
    target = await _target(session_factory, "https://hooks.example.com/in")
    with caplog.at_level(logging.WARNING, logger=delivery_mod.logger.name):
        assert await _deliver(session_factory, target, attempts=2) == "dead"
    row = (await _rows(session_factory, target.id))[0]
    assert row.last_error == "upstream answered HTTP 500"
    assert row.response_status == 500
    messages = [r.getMessage() for r in caplog.records if r.name == delivery_mod.logger.name]
    assert any("secret-path" in m for m in messages)


async def test_delivery_ledger_records_the_exception_class_only(
    session_factory, monkeypatch, caplog
):
    _install_fake_httpx(
        monkeypatch, raise_exc=httpx.ConnectError("connect to 10.9.8.7:443 refused")
    )
    target = await _target(session_factory, "https://hooks.example.com/in")
    with caplog.at_level(logging.WARNING, logger=delivery_mod.logger.name):
        assert await _deliver(session_factory, target, attempts=2) == "dead"
    row = (await _rows(session_factory, target.id))[0]
    assert row.last_error == "delivery failed (ConnectError)"
    assert row.response_status is None
    messages = [r.getMessage() for r in caplog.records if r.name == delivery_mod.logger.name]
    assert any("10.9.8.7:443 refused" in m for m in messages)


async def test_delivery_does_not_follow_redirects(session_factory, monkeypatch):
    """A 3xx is a failed attempt: the redirect target is never requested."""
    requested = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        return httpx.Response(302, headers={"location": "http://169.254.169.254/"})

    real_client = httpx.AsyncClient

    def _client_factory(*args, **kwargs):
        assert kwargs.get("follow_redirects") is False
        return real_client(*args, transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(delivery_mod.httpx, "AsyncClient", _client_factory)
    target = await _target(session_factory, "https://hooks.example.com/in")

    assert await _deliver(session_factory, target, attempts=2) == "dead"

    assert requested == ["https://hooks.example.com/in", "https://hooks.example.com/in"]
    row = (await _rows(session_factory, target.id))[0]
    assert row.last_error == "upstream answered HTTP 302"
    assert row.response_status == 302
