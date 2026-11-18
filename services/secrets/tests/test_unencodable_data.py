"""A secret's data is always text a reveal can return (issue #1456).

`SecretCreate.data` and `SecretUpdate.data` are a plain `dict[str, str]`, which accepts
a lone surrogate in a key or a value. Before the shared body check
(`herd_common.validation_errors.RefuseLoneSurrogates`), such a create answered 201,
the data encrypted (it is serialized with ASCII escapes), and then every reveal of it,
the internal ones included, answered 500 because the response could not be encoded;
a surrogate in a key came back changed (U+FFFD). Now:

- a create or update holding one is refused with the shared 422 and stores nothing
  (OPS-SECRET-1, OPS-SECRET-11);
- a row stored before the check, which a reveal cannot return as stored, answers a
  fixed 409 on every reveal until an admin replaces its data, never a 500 and never an
  altered credential (OPS-SECRET-19).

Bodies are sent raw with every non-ASCII code point escaped (`content=`, because
`json=` cannot encode a lone surrogate).
"""

from __future__ import annotations

import json
import logging
import uuid

import jwt
import pytest
from app import routers
from app.config import settings
from app.database import Base, get_db
from app.main import app
from app.models import Secret
from app.services import crypto
from app.services.keyring import (
    SECRET_DATA_UNREADABLE_DETAIL,
    SecretDataUnreadableError,
    bootstrap_keyring,
    deserialize_data,
    serialize_data,
)
from herd_common.validation_errors import STRING_UNICODE_MSG
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

SECRET_KEY = "3fcbb4bf16b0e162be24d2eb59065316c1e84194669ebf9270203280b18e18f7"
SUR = chr(0xD800)  # built, so no source escape is decoded early
LOW = chr(0xDFFF)
GOOD = {"username": "svc", "password": "good-plaintext-canary"}
INTERNAL = {"X-Internal-Token": "test-token"}


def _admin() -> dict:
    token = jwt.encode({"sub": str(uuid.uuid4()), "role": "admin"}, SECRET_KEY, algorithm="HS256")
    return {"Authorization": f"Bearer {token}", "content-type": "application/json"}


def _raw(value) -> bytes:
    return json.dumps(value, ensure_ascii=True).encode("ascii")


@pytest.fixture
async def env():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async def _override_get_db():
        async with factory() as session:
            yield session

    app.dependency_overrides[get_db] = _override_get_db
    async with factory() as session:
        app.state.keyring = await bootstrap_keyring(session, kek_encoded=settings.secrets_kek)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client, factory
    app.dependency_overrides.clear()
    await engine.dispose()


@pytest.fixture(autouse=True)
def _no_secret_references(monkeypatch):
    async def _none(secret_id):
        return []

    monkeypatch.setattr(routers.secrets, "find_hypervisors_referencing_secret", _none)


async def _create(client, name: str, data: dict) -> dict:
    resp = await client.post(
        "/secrets", content=_raw({"name": name, "data": data}), headers=_admin()
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _reveals(client, secret: dict) -> list:
    """The user reveal, the internal reveal by id, and the internal reveal by name."""
    return [
        await client.get(f"/secrets/{secret['id']}/value", headers=_admin()),
        await client.get(f"/internal/secrets/{secret['id']}/value", headers=INTERNAL),
        await client.get(f"/internal/secrets/by-name/{secret['name']}/value", headers=INTERNAL),
    ]


async def _store_past_the_schema(factory, secret_id: str, data: dict) -> None:
    """Re-encrypt a row's data the way the service did before the body check: the
    surrogate is written as an ASCII escape, so it encrypts without error."""
    async with factory() as session:
        row = await session.get(Secret, uuid.UUID(secret_id))
        version, dek = await app.state.keyring.current(session)
        row.key_version = version
        row.nonce, row.ciphertext = crypto.encrypt_value(
            dek, serialize_data(data), secret_id=row.id, key_version=version
        )
        await session.commit()


# -- Unit: what a stored payload may hold ---------------------------------------------


@pytest.mark.parametrize(
    "data",
    [{"password": "ab" + SUR + "cd"}, {"pa" + SUR + "ss": "v"}, {"k": LOW}, {"k": LOW + SUR}],
    ids=["value", "key", "low_surrogate", "reversed_pair"],
)
def test_deserialize_refuses_data_holding_a_lone_surrogate(data):
    with pytest.raises(SecretDataUnreadableError) as caught:
        deserialize_data(serialize_data(data))
    assert str(caught.value) == "stored secret data is not valid UTF-8 text"


@pytest.mark.parametrize(
    "data",
    [
        GOOD,
        {"päss": "wörd 日本"},
        {"key": "\U0001f511"},  # serialized as an escaped surrogate PAIR
        {"password": "ab\x00cd"},  # a NUL round-trips, by decision unchanged
    ],
    ids=["ascii", "non_ascii", "non_bmp", "nul"],
)
def test_deserialize_returns_honest_data_unchanged(data):
    assert deserialize_data(serialize_data(data)) == data


# -- Functional: a write holding one is refused and stores nothing --------------------


@pytest.mark.parametrize(
    ("data", "loc"),
    [
        # the shared body check answers at the body, whatever the field (OPS-VALID-6)
        ({"password": "ab" + SUR + "cd"}, ["body"]),
        ({"password": "x", "token": LOW}, ["body"]),
        ({"pa" + SUR + "ss": "v"}, ["body"]),
    ],
    ids=["value", "second_value", "key"],
)
async def test_a_create_holding_a_lone_surrogate_is_422_and_stores_nothing(env, data, loc):
    client, _ = env
    resp = await client.post(
        "/secrets", content=_raw({"name": "s", "data": data}), headers=_admin()
    )
    assert resp.status_code == 422, resp.text
    assert resp.json() == {
        "detail": [{"type": "string_unicode", "loc": loc, "msg": STRING_UNICODE_MSG}]
    }
    listed = await client.get("/secrets", headers=_admin())
    assert listed.json() == []


@pytest.mark.parametrize(
    "data",
    [{"password": "x" + SUR}, {"k" + LOW: "v"}],
    ids=["value", "key"],
)
async def test_an_update_holding_a_lone_surrogate_is_422_and_keeps_the_old_data(env, data):
    client, _ = env
    secret = await _create(client, "kept", GOOD)
    resp = await client.put(
        f"/secrets/{secret['id']}", content=_raw({"data": data}), headers=_admin()
    )
    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"][0]["type"] == "string_unicode"
    for reveal in await _reveals(client, secret):
        assert (reveal.status_code, reveal.json()["data"]) == (200, GOOD)


@pytest.mark.parametrize(
    "data",
    [
        {"pässwörd": "日本語"},
        {"key": "\U0001f511-ssh"},
        {"password": "ab\x00cd"},
    ],
    ids=["non_ascii", "surrogate_pair_escape", "nul"],
)
async def test_honest_data_sent_escaped_round_trips_exactly(env, data):
    client, _ = env
    secret = await _create(client, "honest", data)
    for reveal in await _reveals(client, secret):
        assert (reveal.status_code, reveal.json()["data"]) == (200, data)


# -- Rows stored before the check: a fixed 409 on every reveal, never a 500 ----------


@pytest.mark.parametrize(
    "stored",
    [{"password": "canary" + SUR + "cd"}, {"canary" + SUR + "ss": "v"}],
    ids=["value", "key"],
)
async def test_a_stored_lone_surrogate_is_a_fixed_409_on_every_reveal(env, stored, caplog):
    client, factory = env
    secret = await _create(client, "legacy", GOOD)
    await _store_past_the_schema(factory, secret["id"], stored)
    with caplog.at_level(logging.DEBUG):
        reveals = await _reveals(client, secret)
    for reveal in reveals:
        assert reveal.status_code == 409, reveal.text
        assert reveal.json() == {"detail": SECRET_DATA_UNREADABLE_DETAIL}
    # The log names the secret by id and holds nothing from its data.
    logged = "\n".join(r.getMessage() + str(r.__dict__) for r in caplog.records)
    assert secret["id"] in logged
    assert "canary" not in logged


async def test_a_stored_lone_surrogate_leaves_metadata_rotation_and_replacement_working(env):
    client, factory = env
    secret = await _create(client, "legacy", GOOD)
    await _store_past_the_schema(factory, secret["id"], {"password": "x" + SUR})

    assert (await client.get(f"/secrets/{secret['id']}", headers=_admin())).status_code == 200
    assert [s["name"] for s in (await client.get("/secrets", headers=_admin())).json()] == [
        "legacy"
    ]
    rotated = await client.post("/keys/rotate", headers=_admin())
    assert rotated.status_code == 200, rotated.text
    assert {r.status_code for r in await _reveals(client, secret)} == {409}

    replaced = await client.put(
        f"/secrets/{secret['id']}", content=_raw({"data": GOOD}), headers=_admin()
    )
    assert replaced.status_code == 200
    for reveal in await _reveals(client, secret):
        assert (reveal.status_code, reveal.json()["data"]) == (200, GOOD)
