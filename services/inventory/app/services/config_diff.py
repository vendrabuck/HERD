"""Render unified text diffs between two device config snapshots."""

from __future__ import annotations

import difflib
import json
from typing import Any


def _canonicalize(config: dict[str, Any]) -> list[str]:
    text = json.dumps(config, sort_keys=True, indent=2)
    return text.splitlines(keepends=False)


def render_unified_diff(
    config_a: dict[str, Any],
    config_b: dict[str, Any],
    *,
    label_a: str = "before",
    label_b: str = "after",
) -> str:
    a_lines = _canonicalize(config_a)
    b_lines = _canonicalize(config_b)
    diff_lines = difflib.unified_diff(
        a_lines,
        b_lines,
        fromfile=label_a,
        tofile=label_b,
        lineterm="",
    )
    return "\n".join(diff_lines)
