"""Issue #911: every durable pull consumer must run the shared in-progress
heartbeat, so no in-flight message reaches ack_wait while its handler runs.

Structural, deliberately coarse: any service module that calls
`pull_subscribe(` must also run its batches through `process_batch_with_heartbeat`
from herd_common.jetstream (execution since issue #944; no module keeps an inline
heartbeat loop, whose hand-rolled cancel handling once swallowed a shutdown cancel)
and (issue #944) take `NATS_ACK_WAIT_SECONDS` from settings and
`NATS_HEARTBEAT_SECONDS` from `herd_common.jetstream.heartbeat_interval`, never a
hardcoded value. A new consumer module that skips the heartbeat fails here. The
behavioral proof lives in each service's own tests.
"""

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
CONSUMER_MODULES = sorted(
    p for p in (REPO / "services").glob("*/app/**/*.py") if "pull_subscribe(" in p.read_text()
)


def test_consumer_modules_are_discovered():
    names = {p.parts[-4] for p in CONSUMER_MODULES}
    assert {"execution", "integration", "notifications"} <= names


def test_no_pull_consumer_module_keeps_an_inline_heartbeat():
    """Issue #944: the inline `try: await heartbeat / except CancelledError: pass`
    shape cannot tell a cancel aimed at the consumer from the heartbeat's own."""
    inline = [
        str(p.relative_to(REPO))
        for p in CONSUMER_MODULES
        if re.search(
            r"\bkeep_messages_alive\(|create_task\(\s*_?keep_messages_alive", p.read_text()
        )
    ]
    assert not inline, f"pull consumer with its own heartbeat task: {inline}"


def test_every_pull_consumer_module_uses_the_shared_heartbeat():
    missing = [
        str(p.relative_to(REPO))
        for p in CONSUMER_MODULES
        if "process_batch_with_heartbeat" not in p.read_text()
    ]
    assert not missing, f"pull consumer without the #911 heartbeat: {missing}"


def test_every_pull_consumer_module_takes_ack_wait_and_heartbeat_from_one_source():
    """Issue #944: ack_wait comes from settings and the heartbeat cadence from the
    shared `heartbeat_interval` helper (half of ack_wait, derived once). A module
    that hardcodes either, or computes its own fraction, fails here."""
    for p in CONSUMER_MODULES:
        text = p.read_text()
        assert re.search(
            r"^NATS_ACK_WAIT_SECONDS = settings\.nats_ack_wait_seconds$", text, re.M
        ), f"{p}: NATS_ACK_WAIT_SECONDS must come from settings.nats_ack_wait_seconds"
        assert re.search(
            r"^NATS_HEARTBEAT_SECONDS = heartbeat_interval\(NATS_ACK_WAIT_SECONDS\)$",
            text,
            re.M,
        ), f"{p}: NATS_HEARTBEAT_SECONDS must be heartbeat_interval(NATS_ACK_WAIT_SECONDS)"
        assert not re.search(r"ack_wait\s*=\s*\d", text), f"{p}: hardcoded ack_wait"
