"""Assert a built service image installs exactly the packages uv.lock pins.

Issue #593: every service Dockerfile now installs third-party dependencies via
`uv export --frozen | uv pip install -r -`, so the workspace lock is meant to be
the single source of truth for what an image contains, not just what `uv sync`
installs on a host. This script is the belt-and-suspenders check: it diffs a
built image's `pip list --format=freeze` output against the same `uv export`
output the Dockerfile itself ran, and fails on any version mismatch in either
direction.

Two inputs are compared:
  - a `uv export --frozen --package <svc> ...` requirements.txt (third-party
    pins plus environment markers, e.g. `colorama==0.4.6 ; sys_platform ==
    'win32'`)
  - a `pip list --format=freeze` dump from inside the built image

Names are normalized (case-folded, underscores and dots treated as hyphens,
per PEP 503) before comparing, since pip and uv do not always render a
distribution name identically (e.g. `pdfminer-six` vs `pdfminer.six`).

Expected, tolerated differences:
  - packages gated by an environment marker that the image's platform does not
    satisfy (e.g. a win32-only or emscripten-only package absent from a Linux
    image) are expected to be lock-only
  - the editable workspace packages themselves (the service and herd-common)
    and pip's own bootstrap package are expected to be image-only; pass them
    via --allow

Anything else surfacing on either side is a real drift and exits non-zero.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path


def normalize_name(name: str) -> str:
    """PEP 503 normalization: case-fold, treat -, _, and . as equivalent."""
    return re.sub(r"[-_.]+", "-", name).lower()


def parse_requirement_line(line: str) -> tuple[str, str, bool] | None:
    """Return (normalized_name, version, has_marker) or None for a non-pin line."""
    line = line.strip()
    if not line or line.startswith("#"):
        return None
    # Split off an environment marker (everything after the first ` ;`). A
    # marker means uv considered the package conditionally relevant (e.g.
    # `sys_platform == 'win32'`), so its absence from a given image's platform
    # is expected, not drift; has_marker records that for the caller.
    unmarked, sep, _marker = line.partition(" ;")
    has_marker = bool(sep)
    unmarked = unmarked.strip()
    if "==" not in unmarked:
        return None
    name, _, version = unmarked.partition("==")
    return normalize_name(name.strip()), version.strip(), has_marker


def load_pins(path: Path) -> tuple[dict[str, str], set[str]]:
    """Return a (name to version mapping, set of names carrying an environment marker) pair."""
    pins: dict[str, str] = {}
    markered: set[str] = set()
    for line in path.read_text().splitlines():
        parsed = parse_requirement_line(line)
        if parsed is None:
            continue
        name, version, has_marker = parsed
        pins[name] = version
        if has_marker:
            markered.add(name)
    return pins, markered


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("lock_export", type=Path, help="uv export --frozen output")
    parser.add_argument("image_installed", type=Path, help="pip list --format=freeze output")
    parser.add_argument(
        "--allow",
        action="append",
        default=[],
        metavar="PACKAGE",
        help="package name (any case/separator) tolerated as image-only, e.g. the "
        "editable workspace packages or pip's own bootstrap package; repeatable",
    )
    args = parser.parse_args()

    allowed_image_only = {normalize_name(p) for p in args.allow}

    lock_pins, lock_markered = load_pins(args.lock_export)
    image_pins, _image_markered = load_pins(args.image_installed)

    lock_names = set(lock_pins)
    image_names = set(image_pins)

    # Version mismatches: present on both sides but pinned differently. This
    # is the exact class of bug issue #593 exists to prevent (an image
    # resolving a newer release than the committed lock).
    mismatched = sorted(
        name for name in lock_names & image_names if lock_pins[name] != image_pins[name]
    )

    # Lock-only: expected when an environment marker excludes the package on
    # this platform (e.g. win32-only, emscripten-only); the marker itself is
    # the evidence uv considered the package conditional, so we trust it
    # rather than re-evaluating PEP 508 markers here. A marker-free lock-only
    # entry has no such excuse: the image is missing something the lock
    # unconditionally requires.
    lock_only = sorted((lock_names - image_names) - lock_markered)

    # Image-only: expected only for the explicitly allowed packages (the
    # editable workspace members and pip's bootstrap package). Anything else
    # here means the image installed something the lock export never pinned.
    image_only = sorted((image_names - lock_names) - allowed_image_only)

    ok = True

    if mismatched:
        ok = False
        print("VERSION MISMATCH (image installs a different version than uv.lock pins):")
        for name in mismatched:
            print(f"  {name}: lock={lock_pins[name]} image={image_pins[name]}")

    if lock_only:
        ok = False
        print("MISSING FROM IMAGE (lock pins it, image did not install it):")
        for name in lock_only:
            print(f"  {name}=={lock_pins[name]}")

    if image_only:
        ok = False
        print("UNEXPECTED IN IMAGE (not in the lock export, not in --allow):")
        for name in image_only:
            print(f"  {name}=={image_pins[name]}")

    if ok:
        print(f"OK: image matches uv.lock ({len(lock_names)} pinned packages checked)")
        return 0

    return 1


if __name__ == "__main__":
    sys.exit(main())
