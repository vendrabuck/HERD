import pytest
from app.database import Base, get_db
from app.main import app
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

TEST_DATABASE_URL = "sqlite+aiosqlite:///:memory:"

engine = create_async_engine(TEST_DATABASE_URL, echo=False)
TestSessionLocal = async_sessionmaker(engine, expire_on_commit=False)


async def override_get_db():
    async with TestSessionLocal() as session:
        yield session


@pytest.fixture(autouse=True)
async def setup_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


@pytest.fixture
async def client():
    app.dependency_overrides[get_db] = override_get_db
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac
    app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_register(client):
    resp = await client.post(
        "/register",
        json={"email": "test@example.com", "username": "testuser", "password": "secret123"},
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["email"] == "test@example.com"
    assert data["username"] == "testuser"


@pytest.mark.asyncio
async def test_register_duplicate_email(client):
    payload = {"email": "dup@example.com", "username": "user1", "password": "password123"}
    await client.post("/register", json=payload)
    resp = await client.post(
        "/register",
        json={"email": "dup@example.com", "username": "user2", "password": "password123"},
    )
    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_login(client):
    await client.post(
        "/register",
        json={"email": "login@example.com", "username": "loginuser", "password": "mypassword"},
    )
    resp = await client.post(
        "/login", json={"email": "login@example.com", "password": "mypassword"}
    )
    assert resp.status_code == 200
    data = resp.json()
    assert "access_token" in data
    assert "refresh_token" in data


@pytest.mark.asyncio
async def test_login_wrong_password(client):
    await client.post(
        "/register",
        json={"email": "wrong@example.com", "username": "wronguser", "password": "correct123"},
    )
    resp = await client.post(
        "/login", json={"email": "wrong@example.com", "password": "incorrect1"}
    )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_me(client):
    await client.post(
        "/register",
        json={"email": "me@example.com", "username": "meuser", "password": "pass12345"},
    )
    login_resp = await client.post(
        "/login", json={"email": "me@example.com", "password": "pass12345"}
    )
    token = login_resp.json()["access_token"]
    resp = await client.get("/me", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    assert resp.json()["email"] == "me@example.com"


@pytest.mark.asyncio
async def test_refresh(client):
    await client.post(
        "/register",
        json={"email": "refresh@example.com", "username": "refreshuser", "password": "password123"},
    )
    login_resp = await client.post(
        "/login", json={"email": "refresh@example.com", "password": "password123"}
    )
    refresh_token = login_resp.json()["refresh_token"]
    resp = await client.post("/refresh", json={"refresh_token": refresh_token})
    assert resp.status_code == 200
    assert "access_token" in resp.json()
