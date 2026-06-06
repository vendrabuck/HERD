"""Tests for the auth_client module that fetches user groups from the auth service."""

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from app.services.auth_client import fetch_user_groups

_user_id = uuid.uuid4()
_group_id_1 = uuid.uuid4()
_group_id_2 = uuid.uuid4()
_auth_url = "http://auth:8000"
_token = "test-bearer-token"


@pytest.mark.asyncio
@patch("app.services.auth_client.httpx.AsyncClient")
async def test_fetch_user_groups_success(mock_client_cls):
    """Successful response returns list of group UUIDs."""
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = [
        {"id": str(_group_id_1), "name": "Group 1"},
        {"id": str(_group_id_2), "name": "Group 2"},
    ]

    mock_client = AsyncMock()
    mock_client.get.return_value = mock_response
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client_cls.return_value = mock_client

    result = await fetch_user_groups(_auth_url, _user_id, _token)

    assert len(result) == 2
    assert _group_id_1 in result
    assert _group_id_2 in result
    mock_client.get.assert_called_once_with(
        f"{_auth_url}/groups/user/{_user_id}",
        headers={"Authorization": f"Bearer {_token}"},
        timeout=10.0,
    )


@pytest.mark.asyncio
@patch("app.services.auth_client.httpx.AsyncClient")
async def test_fetch_user_groups_non_200_returns_empty(mock_client_cls):
    """Non-200 response (e.g. 404) returns empty list."""
    mock_response = MagicMock()
    mock_response.status_code = 404

    mock_client = AsyncMock()
    mock_client.get.return_value = mock_response
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client_cls.return_value = mock_client

    result = await fetch_user_groups(_auth_url, _user_id, _token)

    assert result == []


@pytest.mark.asyncio
@patch("app.services.auth_client.httpx.AsyncClient")
async def test_fetch_user_groups_500_returns_empty(mock_client_cls):
    """Server error (500) returns empty list."""
    mock_response = MagicMock()
    mock_response.status_code = 500

    mock_client = AsyncMock()
    mock_client.get.return_value = mock_response
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client_cls.return_value = mock_client

    result = await fetch_user_groups(_auth_url, _user_id, _token)

    assert result == []


@pytest.mark.asyncio
@patch("app.services.auth_client.httpx.AsyncClient")
async def test_fetch_user_groups_connection_error_returns_empty(mock_client_cls):
    """Connection error returns empty list."""
    mock_client = AsyncMock()
    mock_client.get.side_effect = httpx.ConnectError("Connection refused")
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client_cls.return_value = mock_client

    result = await fetch_user_groups(_auth_url, _user_id, _token)

    assert result == []


@pytest.mark.asyncio
@patch("app.services.auth_client.httpx.AsyncClient")
async def test_fetch_user_groups_timeout_returns_empty(mock_client_cls):
    """Timeout returns empty list."""
    mock_client = AsyncMock()
    mock_client.get.side_effect = httpx.TimeoutException("Request timed out")
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client_cls.return_value = mock_client

    result = await fetch_user_groups(_auth_url, _user_id, _token)

    assert result == []


@pytest.mark.asyncio
@patch("app.services.auth_client.httpx.AsyncClient")
async def test_fetch_user_groups_empty_groups(mock_client_cls):
    """200 with empty list returns empty list."""
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = []

    mock_client = AsyncMock()
    mock_client.get.return_value = mock_response
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client_cls.return_value = mock_client

    result = await fetch_user_groups(_auth_url, _user_id, _token)

    assert result == []


@pytest.mark.asyncio
@patch("app.services.auth_client.httpx.AsyncClient")
async def test_fetch_user_groups_single_group(mock_client_cls):
    """200 with one group returns list with one UUID."""
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = [{"id": str(_group_id_1), "name": "Only Group"}]

    mock_client = AsyncMock()
    mock_client.get.return_value = mock_response
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client_cls.return_value = mock_client

    result = await fetch_user_groups(_auth_url, _user_id, _token)

    assert len(result) == 1
    assert result[0] == _group_id_1
