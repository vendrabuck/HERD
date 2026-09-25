"""Live-NATS proof for issue #895: each of the three durable consumers'
server-side config actually carries a 30s ack_wait and no backoff.

Connects directly to NATS_URL_HOST (mirroring test_dlq_and_idempotency.py and
_nats_helpers.py) and reads `consumer_info` for each durable on
HERD_RESERVATIONS. This is a read-only proof against whatever consumers are
already running against that stream; it creates nothing and deletes nothing.

Expected to FAIL against a stack still running the pre-#895 code (a durable
provisioned under the old `ConsumerConfig(backoff=[1, 5, 15, 60, 120])` has
`ack_wait=1.0` because JetStream substitutes `backoff[0]`, and `backoff` is
still populated) until every one of execution, notifications, and integration
has been rebuilt/restarted on the fixed code AND their durables have gone
through the create-or-update path (`herd_common.jetstream.ensure_consumer`)
at least once since. This is by design: the durable-config upgrade path is
proven separately, live, against a throwaway stream (see the issue's PR
description / commit messages), not by mutating the shared
HERD_RESERVATIONS durables from a test.
"""

import os

import nats
import pytest

pytestmark = pytest.mark.asyncio

NATS_URL_HOST = os.getenv("NATS_URL_HOST", "nats://localhost:4222")
_STREAM = "HERD_RESERVATIONS"

# (service, durable name) -- see each service's app/services/nats_consumer.py.
_DURABLES = [
    ("execution", "execution-consumer"),
    ("notifications", "notifications-consumer"),
    ("integration", "integration-webhooks-consumer"),
]


async def _probe_nats() -> str | None:
    try:
        nc = await nats.connect(NATS_URL_HOST, connect_timeout=5)
        await nc.close()
        return None
    except Exception as exc:  # noqa: BLE001 - host may not reach NATS in some envs
        return str(exc)


async def test_reservations_consumers_have_real_ack_wait_and_no_backoff():
    """Every durable consuming HERD_RESERVATIONS reports ack_wait == 30 and an
    empty/None backoff (issue #895)."""
    nats_error = await _probe_nats()
    if nats_error is not None:
        pytest.skip(f"NATS unreachable from test host: {nats_error}")

    nc = await nats.connect(NATS_URL_HOST, connect_timeout=5)
    try:
        js = nc.jetstream()
        failures = []
        for service, durable in _DURABLES:
            try:
                info = await js.consumer_info(_STREAM, durable)
            except nats.js.errors.NotFoundError:
                failures.append(f"{service}'s durable {durable!r} does not exist on {_STREAM}")
                continue
            if info.config.ack_wait != 30:
                failures.append(
                    f"{service}'s durable {durable!r}: ack_wait={info.config.ack_wait!r}, "
                    "expected 30 (issue #895: a lingering `backoff` would report 1.0 here, "
                    "since JetStream substitutes backoff[0] for ack_wait)"
                )
            if info.config.backoff:
                failures.append(
                    f"{service}'s durable {durable!r}: backoff={info.config.backoff!r}, "
                    "expected empty/None"
                )
        assert not failures, "\n".join(failures)
    finally:
        await nc.close()
