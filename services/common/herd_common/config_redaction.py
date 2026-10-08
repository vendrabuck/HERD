"""A display copy of a device configuration with credential material masked.

Execution stores every driver call's keyword arguments on the run row, and the
run reads return that row, so the stored copy of a pushed configuration masks
what a reader should not see. Two rules, applied recursively through dicts and
lists without mutating the caller's object:

- a value under a key whose NAME matches `herd_common.logging`'s redaction key
  pattern (password, secret, community, key-like names, tokens, and the rest)
  is replaced whole by `REDACTED_VALUE`;
- inside a string (a vtysh or CLI command line, for example), everything after
  a credential keyword on the same line is replaced by `REDACTED_VALUE`. A
  keyword is a whitespace-separated token equal to one of `password`, `passwd`,
  `passphrase`, `secret`, `community`, `key`, `key-string`, or `md5`, or ending
  in one of them after a hyphen or underscore (`authentication-key`,
  `auth_password`). Masking the rest of the line, not one token, covers the
  `secret 5 <hash>` and `password 7 <hash>` forms.

`redact_config` returns the copy and whether anything was masked. The copy is for
display; it is not a way to recover the configuration, and a caller that needs
the original must keep a reference to where it lives.
"""

from __future__ import annotations

import re
from typing import Any

from herd_common.logging import _REDACT_KEY_PATTERN

REDACTED_VALUE = "[redacted]"

# Deeper structures are masked whole: a display copy fails safe.
_MAX_DEPTH = 32

_KEYWORDS = (
    "password",
    "passwd",
    "passphrase",
    "secret",
    "community",
    "key-string",
    "key",
    "md5",
)
_KEYWORD_TOKEN = re.compile(
    r"(?:[\w.-]*[-_])?(?:" + "|".join(re.escape(k) for k in _KEYWORDS) + r")",
    re.IGNORECASE,
)


def _redact_line(line: str) -> tuple[str, bool]:
    """Mask everything after the first credential keyword token on one line."""
    for match in re.finditer(r"\S+", line):
        if not _KEYWORD_TOKEN.fullmatch(match.group(0)):
            continue
        rest = line[match.end() :]
        if not rest.strip():
            return line, False
        return f"{line[: match.end()]} {REDACTED_VALUE}", True
    return line, False


def redact_command_text(text: str) -> tuple[str, bool]:
    """Mask the credential material in each line of a command string."""
    changed = False
    lines = text.split("\n")
    out = []
    for line in lines:
        redacted, line_changed = _redact_line(line)
        out.append(redacted)
        changed = changed or line_changed
    return "\n".join(out), changed


def _redact(value: Any, depth: int) -> tuple[Any, bool]:
    if isinstance(value, dict):
        if depth >= _MAX_DEPTH:
            return REDACTED_VALUE, True
        changed = False
        result: dict[Any, Any] = {}
        for key, item in value.items():
            match_key = key if isinstance(key, str) else str(key)
            if _REDACT_KEY_PATTERN.search(match_key):
                result[key] = REDACTED_VALUE
                changed = True
                continue
            result[key], item_changed = _redact(item, depth + 1)
            changed = changed or item_changed
        return result, changed
    if isinstance(value, (list, tuple)):
        if depth >= _MAX_DEPTH:
            return REDACTED_VALUE, True
        changed = False
        items = []
        for item in value:
            redacted, item_changed = _redact(item, depth + 1)
            items.append(redacted)
            changed = changed or item_changed
        return items, changed
    if isinstance(value, str):
        return redact_command_text(value)
    return value, False


def redact_config(value: Any) -> tuple[Any, bool]:
    """Return (a masked copy of value, whether anything was masked)."""
    return _redact(value, 0)
