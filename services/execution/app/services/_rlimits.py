"""Shared POSIX rlimit application for the driver sandbox child.

Imported from two places, deliberately with no dependency on app.config or any
other service module:

- the parent (driver_sandbox.py, as app.services._rlimits) builds the
  (name, value) pairs from settings and hands them to the child; and
- the child wrapper (_runner.py, as _rlimits, since the services directory is
  on the child's sys.path) applies them before importing any driver code.

The child runs with only the driver path (and _deps) on PYTHONPATH, so it cannot
import settings; keeping this module dependency-free is what lets both sides
share the apply logic instead of duplicating it. The policy (which RLIMIT_* maps
to which setting, and the concrete values) lives once, in the parent.
"""


def rlimits_supported() -> bool:
    """True on a platform where POSIX resource limits can be applied.

    Windows has no `resource` module; the sandbox simply launches without
    rlimits there (backward compatible, no regression).
    """
    try:
        import resource  # noqa: F401
    except ImportError:
        return False
    return True


def apply_rlimits(limits) -> None:
    """Apply an iterable of (rlimit_name, value) pairs to the current process.

    `rlimit_name` is a resource.RLIMIT_* attribute name (e.g. "RLIMIT_AS"); the
    value is applied as both the soft and hard limit. A value of 0 or less
    leaves that resource unlimited (the pair is skipped), matching the parent's
    "0 means unlimited" convention. An unknown limit name is skipped.

    setrlimit failures (ValueError/OSError, e.g. insufficient permission to
    raise a hard limit) are swallowed: a limit that cannot be applied must not
    break the spawn. This preserves the exact failure behavior the previous
    preexec_fn had, which also swallowed setrlimit errors silently.
    """
    try:
        import resource
    except ImportError:
        return
    for name, val in limits:
        if not val or val <= 0:
            continue
        res = getattr(resource, name, None)
        if res is None:
            continue
        try:
            resource.setrlimit(res, (val, val))
        except (ValueError, OSError):
            pass
