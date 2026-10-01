"""Issue #944: NATS_ACK_WAIT_SECONDS is a Settings field with a floor, and the
consumer module takes both its ack_wait and its heartbeat cadence from it."""

import pytest
from app.config import Settings
from herd_common.jetstream import MIN_ACK_WAIT_SECONDS, heartbeat_interval
from pydantic import ValidationError


def test_default_ack_wait_is_30():
    assert Settings().nats_ack_wait_seconds == 30


def test_ack_wait_reads_the_shared_env_name(monkeypatch):
    monkeypatch.setenv("NATS_ACK_WAIT_SECONDS", "4")

    assert Settings().nats_ack_wait_seconds == 4


@pytest.mark.parametrize("bad", [1, 0, -3])
def test_ack_wait_below_the_minimum_refuses_to_load(bad):
    with pytest.raises(ValidationError) as exc:
        Settings(nats_ack_wait_seconds=bad)

    assert f"NATS_ACK_WAIT_SECONDS must be at least {MIN_ACK_WAIT_SECONDS} seconds" in str(
        exc.value
    )


def test_ack_wait_at_the_minimum_loads():
    assert Settings(nats_ack_wait_seconds=MIN_ACK_WAIT_SECONDS).nats_ack_wait_seconds == 2


def test_consumer_module_derives_both_values_from_settings():
    from app.config import settings
    from app.services import nats_consumer

    assert nats_consumer.NATS_ACK_WAIT_SECONDS == settings.nats_ack_wait_seconds
    assert nats_consumer.NATS_HEARTBEAT_SECONDS == heartbeat_interval(
        nats_consumer.NATS_ACK_WAIT_SECONDS
    )
