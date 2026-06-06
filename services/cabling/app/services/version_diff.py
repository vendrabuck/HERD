from typing import Any


def _index_by_id(items: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    indexed: dict[str, dict[str, Any]] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        key = item.get("id")
        if key is None:
            continue
        indexed[str(key)] = item
    return indexed


def diff_collection(
    before: list[dict[str, Any]] | None, after: list[dict[str, Any]] | None
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    before_idx = _index_by_id(before or [])
    after_idx = _index_by_id(after or [])
    added = [after_idx[k] for k in after_idx.keys() - before_idx.keys()]
    removed = [before_idx[k] for k in before_idx.keys() - after_idx.keys()]
    modified: list[dict[str, Any]] = []
    for k in before_idx.keys() & after_idx.keys():
        if before_idx[k] != after_idx[k]:
            modified.append({"id": k, "before": before_idx[k], "after": after_idx[k]})
    return added, removed, modified


def diff_canvas(
    before: dict[str, Any] | None, after: dict[str, Any] | None
) -> dict[str, list[dict[str, Any]]]:
    before = before or {}
    after = after or {}
    nodes_added, nodes_removed, nodes_modified = diff_collection(
        before.get("nodes"), after.get("nodes")
    )
    edges_added, edges_removed, edges_modified = diff_collection(
        before.get("edges"), after.get("edges")
    )
    return {
        "nodes_added": nodes_added,
        "nodes_removed": nodes_removed,
        "nodes_modified": nodes_modified,
        "edges_added": edges_added,
        "edges_removed": edges_removed,
        "edges_modified": edges_modified,
    }
