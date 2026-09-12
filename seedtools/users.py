"""User registration: the bulk seed population and single ACL-fixture users."""

import httpx

from .catalog import SEED_USERS
from .client import BASE
from .groups import get_user_id


def create_users(client: httpx.Client) -> None:
    total = len(SEED_USERS)
    created = 0
    existed = 0
    failed = 0
    for u in SEED_USERS:
        resp = client.post(
            f"{BASE}/auth/register",
            json={
                "email": u["email"],
                "username": u["username"],
                "password": u["password"],
            },
        )
        if resp.status_code == 201:
            uid = resp.json()["id"]
            created += 1
            if u["role"] == "admin":
                client.put(
                    f"{BASE}/auth/users/{uid}/role",
                    json={"role": "admin"},
                )
        elif resp.status_code == 409:
            existed += 1
        else:
            failed += 1

        done = created + existed + failed
        if done % 100 == 0 or done == total:
            print(
                f"  Users: {done}/{total} (created={created}, existed={existed}, failed={failed})"
            )


def register_acl_user(client: httpx.Client, spec: dict) -> str | None:
    """Idempotently register one user; set admin role if requested. Returns user id."""
    resp = client.post(
        f"{BASE}/auth/register",
        json={
            "email": spec["email"],
            "username": spec["username"],
            "password": spec["password"],
        },
    )
    if resp.status_code == 201:
        uid = resp.json()["id"]
        print(f"  Created user: {spec['username']} ({uid})")
    elif resp.status_code == 409:
        uid = get_user_id(client, spec["username"])
        print(f"  Exists user: {spec['username']} ({uid})")
    else:
        print(f"  Failed to register {spec['username']}: {resp.status_code} {resp.text}")
        return None
    if spec["role"] == "admin" and uid:
        client.put(f"{BASE}/auth/users/{uid}/role", json={"role": "admin"})
    return uid
