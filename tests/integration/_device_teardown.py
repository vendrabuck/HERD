"""Strict device teardown for integration fixtures (issue #940).

Since #940 an admin device DELETE is refused with 409 `device_cabled` while a
cabling Connection still names the device, and since #900 with 409
`device_in_use` while a live reservation depends on it. A teardown that ignores
the delete result would therefore leak the device silently, so fixtures route
their deletes through `delete_device_checked`:

- `device_cabled`: delete every connection naming the device (admin
  connections API, listed by device) and retry; a leak a test left behind is
  cleaned up, not hidden.
- `device_in_use`: retry for a short bounded window, because a delete just
  after a cancel can precede execution's asynchronous teardown (#900 known
  limit). A refusal that outlasts the window is raised, because `STRICT_IN_USE`
  is True: a full integration run on merged code left no device held at
  teardown, so a lingering reservation is now a test bug that must fail
  loudly. Passing `strict_in_use=False` reports it on stderr instead.
- anything else (including a persistent `device_cabled` and a 503), after the
  window: raise `DeviceTeardownError` carrying the refusal body, so a leak can
  never hide.

`delete_devices_checked` is the same rule for the devices a TEST BODY creates
(switches and other devices made outside the `fresh_device(s)` fixtures, issue
#1158): every device gets its delete attempt before any refusal is raised, and
the in-use wait is one budget for the whole batch.

`delete_port_checked` is the port half of the contract (issue #1023): a port
delete is refused with 409 `port_cabled` while a cable names the port, and 503
when cabling cannot be asked. It does no cable cleanup and no waiting, because
a test deletes a port's cables before the port; a refusal is raised.
"""

import asyncio
import sys
import time

# See the module docstring: loud by default for cables and for lingering
# reservations alike.
STRICT_IN_USE = True

IN_USE_WAIT_SECONDS = 6.0
IN_USE_POLL_SECONDS = 0.5
MAX_CABLE_CLEANUP_ROUNDS = 3


class DeviceTeardownError(AssertionError):
    """A device delete was refused during fixture teardown and could not be cleared."""


async def remove_device_connections(client, device_id: str) -> int:
    """Delete every cabling connection naming `device_id`; returns how many were removed.

    Raises DeviceTeardownError when a connection delete is refused, naming the
    connection and the response body.
    """
    removed = 0
    while True:
        resp = await client.get(
            "/cabling/connections", params={"device_id": device_id, "limit": 500}
        )
        resp.raise_for_status()
        items = resp.json()["items"]
        if not items:
            return removed
        for conn in items:
            gone = await client.delete(f"/cabling/connections/{conn['id']}")
            if gone.status_code not in (204, 404):
                raise DeviceTeardownError(
                    f"could not remove connection {conn['id']} naming device {device_id}: "
                    f"{gone.status_code} {gone.text}"
                )
            removed += 1


def _detail(resp) -> dict | None:
    try:
        detail = resp.json().get("detail")
    except (ValueError, AttributeError):
        return None
    return detail if isinstance(detail, dict) else None


async def delete_device_checked(
    client,
    device_id: str,
    *,
    in_use_wait_seconds: float = IN_USE_WAIT_SECONDS,
    strict_in_use: bool | None = None,
) -> None:
    """DELETE the device, clearing leftover cables and waiting out in-flight teardown.

    Returns on 204 or 404 (already gone). See the module docstring for the rest.
    """
    strict = STRICT_IN_USE if strict_in_use is None else strict_in_use
    deadline = time.monotonic() + in_use_wait_seconds
    cable_rounds = 0
    while True:
        resp = await client.delete(f"/inventory/devices/{device_id}")
        if resp.status_code in (204, 404):
            return
        detail = _detail(resp) if resp.status_code == 409 else None
        error = detail.get("error") if detail else None
        if error == "device_cabled" and cable_rounds < MAX_CABLE_CLEANUP_ROUNDS:
            cable_rounds += 1
            await remove_device_connections(client, device_id)
            continue
        if error == "device_in_use" or resp.status_code == 503:
            if time.monotonic() < deadline:
                await asyncio.sleep(IN_USE_POLL_SECONDS)
                continue
            if error == "device_in_use" and not strict:
                print(
                    f"WARNING: device {device_id} left behind, live reservation still holds "
                    f"it: {resp.text}",
                    file=sys.stderr,
                )
                return
        raise DeviceTeardownError(
            f"device {device_id} could not be deleted during teardown: "
            f"{resp.status_code} {resp.text}"
        )


async def delete_devices_checked(
    client,
    device_ids,
    *,
    in_use_wait_seconds: float = IN_USE_WAIT_SECONDS,
) -> None:
    """`delete_device_checked` over several devices, for a test body's own teardown.

    Every device gets its delete attempt before any refusal is raised, so one stuck
    device does not leak the rest, and the in-use wait is ONE budget shared by the
    batch, so a test that leaves a live reservation on N devices does not add N
    waits under the 30 s test cap (the `fresh_devices` fixture's rule). Raises one
    `DeviceTeardownError` naming every device that could not be deleted.
    """
    failures: list[str] = []
    deadline = time.monotonic() + in_use_wait_seconds
    for device_id in device_ids:
        try:
            await delete_device_checked(
                client,
                device_id,
                in_use_wait_seconds=max(0.0, deadline - time.monotonic()),
            )
        except Exception as exc:
            failures.append(f"{device_id}: {exc}")
    if failures:
        raise DeviceTeardownError("; ".join(failures))


async def delete_port_checked(client, port_id: str) -> None:
    """DELETE the port; raise DeviceTeardownError unless it is gone (204 or 404)."""
    resp = await client.delete(f"/inventory/ports/{port_id}")
    if resp.status_code in (204, 404):
        return
    raise DeviceTeardownError(
        f"port {port_id} could not be deleted during teardown: {resp.status_code} {resp.text}"
    )
