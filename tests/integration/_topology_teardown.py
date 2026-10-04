"""Strict topology teardown for integration tests (issue #977).

Since #977 cabling refuses `DELETE /topologies/{id}` with 409 `topology_in_use`
while a PENDING, PENDING_PROVISION, or ACTIVE reservation references the
topology, and with 503 when it cannot ask reservations. A teardown that ignores
the delete result would therefore leak the topology silently, so tests route
their topology deletes through `delete_topology_checked`.

Returns on 204 or 404 (already gone). Anything else raises
`TopologyTeardownError` with the refusal body. A live reservation at topology
teardown is a test bug: cancel or release it first (both are synchronous
compare-and-swap transitions, so the delete right after needs no wait). There
is deliberately no auto-cancel here, matching `STRICT_IN_USE` in
`_device_teardown.py`.
"""


class TopologyTeardownError(AssertionError):
    """A topology delete was refused during test teardown."""


async def delete_topology_checked(client, topology_id: str) -> None:
    """DELETE the topology; raise TopologyTeardownError unless it is gone."""
    resp = await client.delete(f"/cabling/topologies/{topology_id}")
    if resp.status_code in (204, 404):
        return
    raise TopologyTeardownError(
        f"topology {topology_id} could not be deleted during teardown: "
        f"{resp.status_code} {resp.text}"
    )
