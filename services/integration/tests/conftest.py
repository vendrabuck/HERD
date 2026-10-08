"""Shared fixtures for the integration service's unit tests.

Webhook registration and delivery resolve the destination host
(app/services/destination.py). Unit tests never touch DNS: this autouse fixture
replaces the resolver with one that answers a fixed public address for every
host, and clears WEBHOOK_ALLOWED_HOSTS. A test that needs another answer
replaces `destination.resolver` itself.
"""

import pytest
from app.config import settings
from app.services import destination

PUBLIC_TEST_ADDRESS = "93.184.216.34"


@pytest.fixture(autouse=True)
def _public_destination_resolver(monkeypatch):
    monkeypatch.setattr(destination, "resolver", lambda _host: [PUBLIC_TEST_ADDRESS])
    monkeypatch.setattr(settings, "webhook_allowed_hosts", "")
