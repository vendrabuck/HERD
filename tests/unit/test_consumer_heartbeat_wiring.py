"""Issue #911: every durable pull consumer must run the shared in-progress
heartbeat, so no in-flight message reaches ack_wait while its handler runs.

Structural, deliberately coarse: any service module that calls
`pull_subscribe(` must also use `keep_messages_alive` (execution) or
`process_batch_with_heartbeat` (integration, notifications) from
herd_common.jetstream and define a `NATS_HEARTBEAT_SECONDS` below its
`NATS_ACK_WAIT_SECONDS`. A new consumer module that skips the heartbeat fails
here. The behavioral proof lives in each service's own tests.
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


def test_every_pull_consumer_module_uses_the_shared_heartbeat():
    missing = [
        str(p.relative_to(REPO))
        for p in CONSUMER_MODULES
        if not re.search(r"\b(keep_messages_alive|process_batch_with_heartbeat)\b", p.read_text())
    ]
    assert not missing, f"pull consumer without the #911 heartbeat: {missing}"


def test_every_pull_consumer_module_pins_heartbeat_below_ack_wait():
    for p in CONSUMER_MODULES:
        text = p.read_text()
        ack = re.search(r"^NATS_ACK_WAIT_SECONDS = (\d+)$", text, re.M)
        hb = re.search(r"^NATS_HEARTBEAT_SECONDS = NATS_ACK_WAIT_SECONDS // (\d+)$", text, re.M)
        assert ack and hb, f"{p} must define NATS_ACK_WAIT_SECONDS and NATS_HEARTBEAT_SECONDS"
        assert int(hb.group(1)) >= 2, f"{p}: heartbeat must be below ack_wait"
