"""A display copy of a device configuration with credential material masked.

Execution stores every driver call's keyword arguments on the run row, and the
run reads return that row, so the stored copy of a pushed configuration masks
what a reader should not see; the reservation assistant masks the configuration
versions and run rows its tools relay to a model provider with the same helper
(issue #1464). The rules, applied recursively through dicts and lists without
mutating the caller's object:

- KEY RULE. A value under a key is replaced whole by `REDACTED_VALUE` when the
  key's name matches `herd_common.logging`'s redaction key pattern (a substring
  search: password, secret, community, api_key, token, and the rest), OR when
  any PART of the normalized key is a credential keyword (the `_KEY_PARTS`
  set: `password`, `passwd`, `passphrase`, `secret`, `community`, `key`, `md5`,
  `authentication`, `auth`, `phash`, `psk`, `token`, `bearer`). A key is
  normalized by inserting a separator at each lower-to-upper case boundary
  (`tacacsKey` to `tacacs-Key`), lowercasing, and splitting on every run of
  characters other than ASCII letters and digits, so `authentication-key`,
  `key_string`, `message-digest-key`, `pre_shared_key`, `tacacsKey`,
  `wpa_passphrase`, and `md5` all match, and so do `key_id` and `auth_method`
  (a masked identifier fails safe in a display copy).
- JSON IN A STRING. A string that, once stripped, starts with `{` or `[`, is at
  most `_MAX_EMBEDDED_JSON_CHARS` long, and parses as a JSON object or array is
  masked as that structure (one depth level deeper, under the same depth cap)
  and written back with `json.dumps`; text that does not parse falls through to
  the line rule.
- LINE RULE. Inside any other string, each line (split on `\\n`) is masked on
  its own. A `scheme://user:password@` URL has its password replaced. Then the
  rest of the line after the first credential keyword token is replaced by
  `REDACTED_VALUE`. A keyword token is one of `_KEYWORDS` (the key parts above
  plus `key-string`), optionally after a prefix ending in a hyphen or
  underscore (`authentication-key`, `auth_password`, `tcp-md5-auth`,
  `hashed-password`, `pre-shared-key`, `wpa-psk`); it starts the line or follows
  white space or one of `"{[(,;?&` (not a single quote: a refusal quotes a field
  name with `repr`, CFG-SCHEMA-13), and it is followed by white space, `:`,
  `=`, a quote, or the end of the line. Separator characters right after it
  (white space, `:`, `=`, quotes) are kept, so `password 7 <hash>` becomes
  `password [redacted]`, `Password: x` becomes `Password: [redacted]`,
  `PASSWORD=x;y` becomes `PASSWORD=[redacted]`, and `key:x` becomes
  `key:[redacted]`. A keyword with nothing after it masks nothing (`no
  password`, `show key`). `authentication` and `auth` also mask a mode word
  that follows them (`ip ospf authentication message-digest`): a display copy
  fails safe.

Limits, by decision: a heuristic for the shapes the in-repo drivers and common
network CLIs produce, not a guarantee. Not masked:

- a secret under a key none of whose parts is a keyword and that the log
  pattern does not match (`pw`, `pass`, `otp`, `pin`, `privkey`, `keystring`,
  `hash`);
- a secret in free text with no keyword before it on its line, including a
  secret on the line or list item AFTER the one that ends in a keyword
  (`password\\nS3cret`, `["password", "S3cret"]`: masking the next item would
  also cut the command after every `no password` line);
- a keyword glued to its value (`passwordS3cret`), a split or abbreviated
  keyword (`pass word`, `pw=`), and encoded text (base64);
- a keyword right after a single quote (`{'password': 'x'}`, Python's repr of a
  dict, not a configuration syntax);
- a secret written as a dict key itself (keys are kept as written).

Words that are not keywords on purpose: `ao` (a TCP-AO option names a key
chain, not the key), `priv` (SNMPv3's privacy password always follows `auth`,
which already masks the rest of the line), `pass` and `pw` (firewall rules and
pseudowire configuration use them as plain words).

`redact_config` returns the copy and whether anything was masked. The copy is for
display; it is not a way to recover the configuration, and a caller that needs
the original must keep a reference to where it lives.
"""

from __future__ import annotations

import json
import re
from typing import Any

from herd_common.logging import _REDACT_KEY_PATTERN

REDACTED_VALUE = "[redacted]"

# Deeper structures are masked whole: a display copy fails safe.
_MAX_DEPTH = 32

# A string longer than this is never parsed as embedded JSON (it is line-masked
# instead), so a hostile value cannot make masking parse megabytes repeatedly.
_MAX_EMBEDDED_JSON_CHARS = 256 * 1024

# Single-word keywords: a key matches when any of its normalized parts is one.
_KEY_PARTS = frozenset(
    {
        "password",
        "passwd",
        "passphrase",
        "secret",
        "community",
        "key",
        "md5",
        "authentication",
        "auth",
        "phash",
        "psk",
        "token",
        "bearer",
    }
)
# Line keywords: the key parts plus the multi-word ones. Longest first, so the
# alternation never stops at a shorter keyword that prefixes a longer one.
_KEYWORDS = tuple(sorted(_KEY_PARTS | {"key-string"}, key=len, reverse=True))

_KEYWORD_IN_LINE = re.compile(
    r"(?<![^\s\"{\[(,;?&])"  # start of line, or after white space or punctuation
    r"(?:[\w.-]*[-_])?"  # optional prefix ending in a hyphen or underscore
    r"(?:" + "|".join(re.escape(k) for k in _KEYWORDS) + r")"
    r"(?=[\s:=\"']|$)",  # followed by a separator or the end of the line
    re.IGNORECASE,
)
_SEPARATORS = re.compile(r"[\s:=\"']*")
_URL_USERINFO = re.compile(r"(?i)(\b[a-z][a-z0-9+.-]*://[^\s/@:]*:)[^\s/@]+@")
_CASE_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_KEY_SPLIT = re.compile(r"[^a-z0-9]+")

_NOT_JSON = object()


_KEYWORD_TOKEN = re.compile(
    r"(?:[\w.-]*[-_])?(?:" + "|".join(re.escape(k) for k in _KEYWORDS) + r")",
    re.IGNORECASE,
)


def is_keyword_token(text: str) -> bool:
    """Whether `text` is, whole, a credential keyword token of the line rule
    (`password`, `auth_key`, `hashed-password`)."""
    return _KEYWORD_TOKEN.fullmatch(text) is not None


def _key_is_credential(key: Any) -> bool:
    """The key rule: the log pattern, or any normalized part is a keyword."""
    name = key if isinstance(key, str) else str(key)
    if _REDACT_KEY_PATTERN.search(name):
        return True
    parts = _KEY_SPLIT.split(_CASE_BOUNDARY.sub("-", name).lower())
    return any(part in _KEY_PARTS for part in parts)


def _redact_line(line: str) -> tuple[str, bool]:
    """Mask a URL password, then everything after the first keyword token."""
    line, url_count = _URL_USERINFO.subn(rf"\g<1>{REDACTED_VALUE}@", line)
    match = _KEYWORD_IN_LINE.search(line)
    if match is None:
        return line, url_count > 0
    value_start = _SEPARATORS.match(line, match.end()).end()
    if not line[value_start:].strip():
        return line, url_count > 0
    # The lookahead guarantees at least one separator, so the marker never
    # reads as part of the keyword.
    return f"{line[:value_start]}{REDACTED_VALUE}", True


def redact_command_text(text: str) -> tuple[str, bool]:
    """Mask the credential material in each line of a command string."""
    changed = False
    out = []
    for line in text.split("\n"):
        redacted, line_changed = _redact_line(line)
        out.append(redacted)
        changed = changed or line_changed
    return "\n".join(out), changed


def _embedded_json(value: str) -> Any:
    """The JSON object or array a string holds, or _NOT_JSON."""
    if len(value) > _MAX_EMBEDDED_JSON_CHARS:
        return _NOT_JSON
    stripped = value.strip()
    if not stripped or stripped[0] not in "{[":
        return _NOT_JSON
    try:
        parsed = json.loads(stripped)
    except (ValueError, RecursionError):
        return _NOT_JSON
    return parsed if isinstance(parsed, (dict, list)) else _NOT_JSON


def _redact(value: Any, depth: int) -> tuple[Any, bool]:
    if isinstance(value, dict):
        if depth >= _MAX_DEPTH:
            return REDACTED_VALUE, True
        changed = False
        result: dict[Any, Any] = {}
        for key, item in value.items():
            if _key_is_credential(key):
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
        parsed = _embedded_json(value)
        if parsed is not _NOT_JSON:
            masked, changed = _redact(parsed, depth + 1)
            return (json.dumps(masked) if changed else value), changed
        return redact_command_text(value)
    return value, False


def redact_config(value: Any) -> tuple[Any, bool]:
    """Return (a masked copy of value, whether anything was masked)."""
    return _redact(value, 0)
