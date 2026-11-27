"""Unit tests for herd_common.jetstream's consumer startup warnings (issue #1418).

Each helper must log exactly one WARNING on the logger it is given, in the
exact wording below, carrying the exception CLASS name only: never the
exception's text, never a traceback (exc_info), and so never a NATS URL that
an exception text might quote with its credentials.
"""

import logging

import pytest
from herd_common.jetstream import (
    log_consumer_connect_failure,
    log_consumer_setup_failure,
    log_consumer_stream_check_failure,
)

LOGGER_NAME = "test.consumer_startup"
# Text an exception could carry: a URL with credentials, a broker reply.
EXC_TEXT = "nats://herd-user:tok3n-value@broker.example:4222 refused"


class BrokerReplyError(Exception):
    pass


def _only_record(caplog) -> logging.LogRecord:
    records = [r for r in caplog.records if r.name == LOGGER_NAME]
    assert len(records) == 1, [r.getMessage() for r in records]
    record = records[0]
    assert record.levelno == logging.WARNING
    assert record.exc_info is None
    assert "tok3n-value" not in record.getMessage()
    assert "broker.example" not in record.getMessage()
    return record


def test_connect_failure_wording(caplog):
    log = logging.getLogger(LOGGER_NAME)
    with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
        log_consumer_connect_failure(
            log, BrokerReplyError(EXC_TEXT), not_running=["a-consumer", "b-consumer"]
        )

    assert _only_record(caplog).getMessage() == (
        "NATS connect failed (BrokerReplyError); not running: a-consumer, b-consumer. "
        "Nothing retries the connect, so these consumers start only when the service "
        "is restarted with the broker reachable; events published to their streams "
        "meanwhile wait there, kept up to each stream's max age, and are processed "
        "after that restart"
    )


@pytest.mark.parametrize(
    ("not_running", "running", "lists"),
    [
        (
            ["a-consumer", "b-consumer"],
            [],
            "not running: a-consumer, b-consumer; still running: none",
        ),
        (["b-consumer"], ["a-consumer"], "not running: b-consumer; still running: a-consumer"),
    ],
)
def test_setup_failure_wording_names_the_durable_and_both_lists(
    caplog, not_running, running, lists
):
    log = logging.getLogger(LOGGER_NAME)
    with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
        log_consumer_setup_failure(
            log,
            BrokerReplyError(EXC_TEXT),
            durable=not_running[0],
            stream="HERD_X",
            not_running=not_running,
            running=running,
        )

    assert _only_record(caplog).getMessage() == (
        f"NATS consumer setup failed for durable {not_running[0]} on stream HERD_X "
        f"(BrokerReplyError); {lists}. The connection is kept, but nothing retries the "
        "setup, so a consumer that is not running starts only when the service is "
        "restarted; events published to its stream meanwhile wait there, kept up to "
        "the stream's max age, and are processed after that restart"
    )


def test_stream_check_failure_wording(caplog):
    log = logging.getLogger(LOGGER_NAME)
    with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
        log_consumer_stream_check_failure(log, BrokerReplyError(EXC_TEXT), stream="HERD_X")

    assert _only_record(caplog).getMessage() == (
        "JetStream stream HERD_X could not be checked or created (BrokerReplyError); "
        "the consumer setup on it goes ahead, and fails if the stream is missing"
    )


def test_no_line_says_events_are_lost_or_that_the_connect_failed_after_a_good_connect(caplog):
    """The two sentences issue #1418 removed must not come back in any helper."""
    log = logging.getLogger(LOGGER_NAME)
    with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
        log_consumer_connect_failure(log, BrokerReplyError(), not_running=["a"])
        log_consumer_setup_failure(
            log, BrokerReplyError(), durable="a", stream="S", not_running=["a"], running=[]
        )
        log_consumer_stream_check_failure(log, BrokerReplyError(), stream="S")

    messages = [r.getMessage() for r in caplog.records if r.name == LOGGER_NAME]
    assert len(messages) == 3
    for message in messages:
        assert "will not be delivered" not in message
        assert "Failed to connect" not in message
    assert "connect failed" not in messages[1]
