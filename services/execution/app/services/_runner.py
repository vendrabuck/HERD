#!/usr/bin/env python3
"""Standalone subprocess entry point for driver execution.

Usage:
    python _runner.py <driver_path> <action> <context_json_file> [method_kwargs_json]

Loads driver.py from driver_path, instantiates Driver(context), calls the
requested method, and prints the JSON result to stdout. Exits 0 on success,
1 on failure.

method_kwargs_json is an optional JSON string of keyword arguments to pass
to the driver method (e.g. '{"port_a": "0/0/1", "port_b": "0/0/2"}' for L1
or '{"port": "eth1", "vlan_id": 100, "tag": "tagged"}' for L2).
"""

import importlib.util
import json
import os
import sys

# Make the runner's own directory importable so loaded drivers can do
# `from driver_transcript import record_command`. Python normally adds the
# script directory to sys.path[0] automatically; this explicit insert is
# defensive and a hint to readers that drivers may import siblings of _runner.py.
_RUNNER_DIR = os.path.dirname(os.path.abspath(__file__))
if _RUNNER_DIR not in sys.path:
    sys.path.insert(0, _RUNNER_DIR)


def main():
    if len(sys.argv) < 4:
        print(
            "Usage: _runner.py <driver_path> <action> <context_file> [method_kwargs_json]",
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
    action = sys.argv[2]
    context_file = sys.argv[3]
    method_kwargs = json.loads(sys.argv[4]) if len(sys.argv) > 4 else {}

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
        print(str(e), file=sys.stderr)
        sys.exit(1)
