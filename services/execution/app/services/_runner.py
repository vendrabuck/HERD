#!/usr/bin/env python3
"""Standalone subprocess entry point for driver execution.

Usage:
    python -I -B _runner.py <driver_path> <action> <context_json_file> [method_kwargs_json_file]

Loads driver.py from driver_path, instantiates Driver(context), calls the
requested method, and prints the JSON result to stdout. Exits 0 on success,
1 on failure.

method_kwargs_json_file is the optional path of a JSON file holding the keyword
arguments to pass to the driver method (e.g. {"port_a": "0/0/1", "port_b":
"0/0/2"} for L1 or {"port": "eth1", "vlan_id": 100, "tag": "tagged"} for L2).
The arguments are read from a file, never taken from the command line, for the
same reason the context is: argv is readable by every process in the container.
"""

import importlib.util
import json
import os
import sys

# Make the runner's own directory importable, for `import _rlimits` below and
# so loaded drivers can do `from driver_transcript import record_command`. The
# sandbox starts this script with `-I` (issue #1180), which implies `-P`: Python
# does NOT put the script directory on sys.path, so this insert is load-bearing,
# not defensive. It stays at the front, ahead of the package directory that
# _put_package_on_path adds later, so `_rlimits` and `driver_transcript` always
# resolve to the runner's own siblings, never to a package file of that name.
_RUNNER_DIR = os.path.dirname(os.path.abspath(__file__))
if _RUNNER_DIR not in sys.path:
    sys.path.insert(0, _RUNNER_DIR)


def _put_package_on_path(driver_path: str) -> None:
    """Make the driver package (and its vendored `_deps`) importable.

    Called from main() only AFTER the resource limits are applied. Invariant
    (issue #1180): nothing under the package directory is importable until the
    caps are in force. The child starts in isolated mode (`-I`), so no package
    file runs at interpreter startup (a package `sitecustomize.py` is never
    found) and none can shadow a module the runner imports before this call:
    the standard library `json` imported at the top of this file is cached in
    sys.modules, so a package `json.py` never replaces it, for the runner or
    for the driver.

    The order a driver sees is the one PYTHONPATH used to give it: the runner
    directory, then the package directory, then `_deps` (when it exists, the
    same existence test the parent uses), then the standard library and
    site-packages. So a package module still shadows a standard library module
    the runner has not imported, exactly as before, but only once the caps
    apply.
    """
    entries = [driver_path]
    deps_dir = os.path.join(driver_path, "_deps")
    if os.path.exists(deps_dir):
        entries.append(deps_dir)
    at = sys.path.index(_RUNNER_DIR) + 1 if _RUNNER_DIR in sys.path else 0
    sys.path[at:at] = entries


def main():
    if len(sys.argv) < 4:
        print(
            "Usage: _runner.py <driver_path> <action> <context_file> [method_kwargs_file]",
            file=sys.stderr,
        )
        sys.exit(1)

    # Apply the POSIX resource caps the parent handed down BEFORE importing or
    # running any driver code, so a memory- or CPU-abusing driver is still bound
    # by RLIMIT_AS/RLIMIT_CPU/RLIMIT_NOFILE/RLIMIT_NPROC. These were historically
    # applied via subprocess preexec_fn, but CPython documents preexec_fn as not
    # thread-safe (the child runs Python between fork and exec and can deadlock
    # on a lock held by another thread at fork time). Driver execution now runs
    # under asyncio.to_thread, so multiple threads fork concurrently; applying
    # the identical caps here in the child sidesteps the fork-safety hazard (#307).
    _rlimits_json = os.environ.get("HERD_SANDBOX_RLIMITS")
    if _rlimits_json:
        import _rlimits

        _rlimits.apply_rlimits(json.loads(_rlimits_json))

    driver_path = sys.argv[1]
    # Only now, with the caps applied, does the package become importable. The
    # order is load-bearing: `_rlimits` imports `resource` lazily, inside
    # apply_rlimits, so `resource` must resolve before the package is on the
    # path; a package `resource.py` found first would make that import fail and
    # leave every limit off.
    _put_package_on_path(driver_path)
    action = sys.argv[2]
    context_file = sys.argv[3]
    method_kwargs = {}
    if len(sys.argv) > 4:
        with open(sys.argv[4]) as f:
            method_kwargs = json.load(f)

    # Load context from temp file
    with open(context_file) as f:
        context = json.load(f)

    # Load driver.py
    driver_py = f"{driver_path}/driver.py"
    spec = importlib.util.spec_from_file_location("driver", driver_py)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    # Sentinel action: read the driver's published config schema WITHOUT
    # instantiating Driver(context). config_schema() is a classmethod that
    # describes the driver class, not a device session, so it must not require
    # live credentials. Some drivers' __init__ reads context["HERD_ip_address"]
    # and would crash here; calling on the class object sidesteps that.
    if action == "__config_schema__":
        fn = getattr(module.Driver, "config_schema", None)
        result = {"has_schema": False, "schema": None}
        if callable(fn):
            schema = fn()  # classmethod: no instance, no context
            result = {"has_schema": True, "schema": schema}
        print(json.dumps(result, default=str))
        return

    # Instantiate Driver
    driver = module.Driver(context)

    # Call the requested method
    method = getattr(driver, action)
    if method_kwargs:
        result = method(**method_kwargs)
    else:
        result = method()

    # Output JSON result
    print(json.dumps(result, default=str))


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        # Structured, not plain text (issue #840): the parent (driver_sandbox.py)
        # needs to tell "the driver call raised" apart from every other failure
        # shape on stderr, so it can store only the exception CLASS on a run a
        # user can read, never the message (which can carry hosts, paths, or
        # credential-adjacent text) or a traceback. The full message still goes
        # out on this same stderr line for the parent to log; it just never
        # reaches an API response or a database row past that point.
        print(
            json.dumps({"exception_class": type(e).__name__, "message": str(e)}),
            file=sys.stderr,
        )
        sys.exit(1)
