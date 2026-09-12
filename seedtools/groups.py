"""User groups, device groups, membership, and device-group permissions."""

import sys

import httpx

from .client import BASE, fetch_all_items


def get_or_create_group(client: httpx.Client, name: str, description: str) -> str:
    resp = client.post(
        f"{BASE}/auth/groups",
        json={"name": name, "description": description},
    )
    if resp.status_code == 201:
        gid = resp.json()["id"]
        print(f"  Created group: {name} ({gid})")
        return gid

    # Already exists (409); look it up
    listing = client.get(f"{BASE}/auth/groups", params={"limit": 500})
    if listing.status_code != 200:
        print(f"  GET /auth/groups failed ({listing.status_code}): {listing.text}")
        sys.exit(1)
    for g in listing.json()["items"]:
        if g["name"] == name:
            print(f"  Exists group: {name} ({g['id']})")
            return g["id"]

    print(f"  Failed to create or find group {name}: {resp.text}")
    sys.exit(1)


def get_user_id(client: httpx.Client, username: str) -> str | None:
    all_users = fetch_all_items(client, f"{BASE}/auth/users")
    for u in all_users:
        if u["username"] == username:
            return u["id"]
    return None


def get_all_user_ids(client: httpx.Client) -> dict[str, str]:
    """Fetch all users once and return username to id mapping."""
    all_users = fetch_all_items(client, f"{BASE}/auth/users")
    return {u["username"]: u["id"] for u in all_users}


def add_group_member(client: httpx.Client, group_id: str, user_id: str) -> None:
    resp = client.post(
        f"{BASE}/auth/groups/{group_id}/members",
        json={"user_id": user_id},
    )
    if resp.status_code not in (201, 409):
        print(f"    Failed to add member: {resp.text}")


def get_or_create_device_group(client: httpx.Client, name: str, description: str) -> str:
    resp = client.post(
        f"{BASE}/inventory/device-groups",
        json={"name": name, "description": description},
    )
    if resp.status_code == 201:
        gid = resp.json()["id"]
        print(f"  Created device group: {name} ({gid})")
        return gid

    # Already exists (409); look it up
    listing = client.get(f"{BASE}/inventory/device-groups", params={"limit": 500})
    if listing.status_code != 200:
        print(f"  GET /inventory/device-groups failed ({listing.status_code}): {listing.text}")
        sys.exit(1)
    for g in listing.json()["items"]:
        if g["name"] == name:
            print(f"  Exists device group: {name} ({g['id']})")
            return g["id"]

    print(f"  Failed to create or find device group {name}: {resp.text}")
    sys.exit(1)


def bulk_remove_from_group(
    client: httpx.Client, group_id: str, user_ids: list[str], batch_size: int = 500
) -> None:
    """Remove users from a user group in batches."""
    total = len(user_ids)
    for start in range(0, total, batch_size):
        batch = user_ids[start : start + batch_size]
        resp = client.post(
            f"{BASE}/auth/groups/{group_id}/members/bulk-remove",
            json={"user_ids": batch},
        )
        done = min(start + batch_size, total)
        if resp.status_code == 200:
            data = resp.json()
            removed = data["removed"]
            not_found = data["not_found"]
            print(f"    Batch {done}/{total}: removed={removed}, not_found={not_found}")
        else:
            print(f"    Batch {done}/{total}: failed ({resp.status_code})")


def bulk_remove_devices_from_group(
    client: httpx.Client, group_id: str, device_ids: list[str], batch_size: int = 500
) -> None:
    """Remove devices from a device group in batches."""
    total = len(device_ids)
    for start in range(0, total, batch_size):
        batch = device_ids[start : start + batch_size]
        resp = client.post(
            f"{BASE}/inventory/device-groups/{group_id}/devices/bulk-remove",
            json={"device_ids": batch},
        )
        done = min(start + batch_size, total)
        if resp.status_code == 200:
            data = resp.json()
            removed = data["removed"]
            not_found = data["not_found"]
            print(f"    Batch {done}/{total}: removed={removed}, not_found={not_found}")
        else:
            print(f"    Batch {done}/{total}: failed ({resp.status_code})")


def bulk_add_devices_to_group(
    client: httpx.Client, group_id: str, device_ids: list[str], batch_size: int = 500
) -> None:
    """Add devices to a device group in batches."""
    total = len(device_ids)
    for start in range(0, total, batch_size):
        batch = device_ids[start : start + batch_size]
        resp = client.post(
            f"{BASE}/inventory/device-groups/{group_id}/devices/bulk",
            json={"device_ids": batch},
        )
        done = min(start + batch_size, total)
        if resp.status_code == 200:
            data = resp.json()
            print(f"    Batch {done}/{total}: added={data['added']}, skipped={data['skipped']}")
        else:
            print(f"    Batch {done}/{total}: failed ({resp.status_code})")


def bulk_add_permissions_to_device_group(
    client: httpx.Client,
    device_group_id: str,
    user_group_ids: list[str],
) -> None:
    """Add user group permissions to a device group."""
    resp = client.post(
        f"{BASE}/inventory/device-groups/{device_group_id}/permissions/bulk",
        json={"user_group_ids": user_group_ids},
    )
    if resp.status_code == 200:
        data = resp.json()
        print(f"    Permissions: added={data['added']}, skipped={data['skipped']}")
    else:
        print(f"    Permissions failed ({resp.status_code}): {resp.text}")
