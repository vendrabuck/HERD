"""The one way the NOS lab suites open a test-side netmiko session (issue #1263).

Right after `make nos-up` a lab node can be healthy and still reset the first
SSH connection during the banner exchange (paramiko's `_check_banner` raising
`ConnectionResetError`). The lab's own readiness is fixed at the source, in the
SR Linux healthcheck (infra/nos-test/srl/ssh_ready.sh), so `--wait` returns
only once sshd answers with a banner. This helper is the second line: a
boot-time transport failure is retried a small, fixed number of times, so one
reset does not fail the run, while a lab that is really broken still fails in
a few seconds.

What is retried: a transport failure, meaning `OSError` (which covers
`ConnectionResetError` and a refused connect), `EOFError` (the peer closed
mid-handshake), and netmiko's `NetmikoTimeoutException` (netmiko's wrapper for
a socket error or a paramiko `SSHException` during connect). What is NOT
retried: `NetmikoAuthenticationException`, or any other exception. A rejected
password is a real failure, and retrying it would only delay the answer.

Only the TEST-side sessions go through here (the sessions a test uses to set
up or check state). A driver under test opens its own session in its own
`login()`, which this helper must never wrap: retrying there would hide a
driver defect.
"""

from __future__ import annotations

import time
from typing import Any

from netmiko import ConnectHandler, NetmikoTimeoutException

CONNECT_ATTEMPTS = 3
CONNECT_RETRY_DELAY_SECONDS = 2.0

RETRYABLE_CONNECT_ERRORS: tuple[type[BaseException], ...] = (
    OSError,
    EOFError,
    NetmikoTimeoutException,
)


class LabConnectError(RuntimeError):
    """Every attempt to open a lab session failed with a retryable error."""


def open_lab_session(label: str, **connect_kwargs: Any) -> Any:
    """Return `ConnectHandler(**connect_kwargs)`, retrying a transport failure.

    At most `CONNECT_ATTEMPTS` attempts, `CONNECT_RETRY_DELAY_SECONDS` apart
    (no sleep after the last one). When every attempt fails, raises
    `LabConnectError` naming `label`, the attempt count, and the last
    exception's class, chained to that exception so its full text stays in the
    traceback.
    """
    last_error: BaseException | None = None
    for attempt in range(1, CONNECT_ATTEMPTS + 1):
        try:
            return ConnectHandler(**connect_kwargs)
        except RETRYABLE_CONNECT_ERRORS as exc:
            last_error = exc
            if attempt < CONNECT_ATTEMPTS:
                time.sleep(CONNECT_RETRY_DELAY_SECONDS)
    assert last_error is not None
    raise LabConnectError(
        f"could not open an SSH session to {label} after {CONNECT_ATTEMPTS} attempts "
        f"{CONNECT_RETRY_DELAY_SECONDS:g} s apart; last error: {type(last_error).__name__}"
    ) from last_error
