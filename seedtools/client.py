"""HTTP client plumbing: base URL, credential resolution, login, paginated reads.

Credential resolution lives here and nowhere else. It absorbs what the two
retired shell wrappers (scripts/seed_frr_demo.sh, scripts/seed_nos_lab.sh) did
with grep, so every subcommand resolves the same way from the same code:

  1. SEED_EMAIL / SEED_PASSWORD in the environment (the Makefile exports these).
  2. SUPERADMIN_EMAIL / SUPERADMIN_PASSWORD in the environment (the stack's
     actual bootstrap credentials).
  3. SUPERADMIN_EMAIL / SUPERADMIN_PASSWORD read from a .env in the repo root.
     Parsed line by line rather than sourced, so an unquoted placeholder value
     elsewhere in .env cannot break the run; the first match wins and the value
     is taken verbatim after the first "=", so an embedded "=" survives.
  4. A generic, non-personal placeholder. Never hardcode a real address here.

Email and password resolve independently, the same way the shell wrappers and
the Makefile recipe resolved them with two separate greps.
"""

import os
import sys

import httpx
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# The repo root is the package's parent directory: seedtools/ sits at the root
# next to drivers/, tests/ and .env, so everything the seed reads from disk
# (driver packages, .env) resolves from here.
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# SEED_BASE_URL wins, then HERD_BASE_URL (which the shell wrappers offered),
# then the dev stack's default.
BASE = os.environ.get("SEED_BASE_URL") or os.environ.get("HERD_BASE_URL") or "https://localhost/api"

DEFAULT_EMAIL = "admin@example.com"
DEFAULT_PASSWORD = "admin123!"


def env_file_value(key: str, path: str | None = None) -> str | None:
    """Read `key` from the repo-root .env, or None if absent or unreadable.

    First match wins; the value is everything after the first "=", stripped of
    trailing whitespace only. Surrounding quotes are NOT stripped, matching the
    grep the Makefile and the retired wrappers used, so keep .env values
    unquoted.
    """
    path = path or os.path.join(REPO_ROOT, ".env")
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                name, sep, value = line.partition("=")
                if sep and name.strip() == key:
                    return value.rstrip("\n").rstrip("\r") or None
    except OSError:
        return None
    return None


def resolve_credentials() -> tuple[str, str]:
    """Resolve (email, password) through the four-rung ladder above."""
    email = (
        os.environ.get("SEED_EMAIL")
        or os.environ.get("SUPERADMIN_EMAIL")
        or env_file_value("SUPERADMIN_EMAIL")
        or DEFAULT_EMAIL
    )
    password = (
        os.environ.get("SEED_PASSWORD")
        or os.environ.get("SUPERADMIN_PASSWORD")
        or env_file_value("SUPERADMIN_PASSWORD")
        or DEFAULT_PASSWORD
    )
    return email, password


EMAIL, PASSWORD = resolve_credentials()


def fetch_all_items(
    client: httpx.Client,
    url: str,
    params: dict | None = None,
    page_size: int = 500,
) -> list[dict]:
    """Fetch all items from a paginated endpoint."""
    params = dict(params or {})
    items: list[dict] = []
    skip = 0
    while True:
        params["skip"] = skip
        params["limit"] = page_size
        resp = client.get(url, params=params)
        if resp.status_code != 200:
            print(f"  fetch_all_items failed for {url} ({resp.status_code}): {resp.text}")
            return items
        data = resp.json()
        items.extend(data["items"])
        if skip + page_size >= data["total"]:
            break
        skip += page_size
    return items


def login(client: httpx.Client) -> str:
    resp = client.post(f"{BASE}/auth/login", json={"email": EMAIL, "password": PASSWORD})
    if resp.status_code != 200:
        print(f"Login failed: {resp.text}")
        sys.exit(1)
    token = resp.json()["access_token"]
    print(f"Logged in as {EMAIL}")
    return token
