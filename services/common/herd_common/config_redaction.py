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
- JSON IN A STRING. A string of at most `_MAX_EMBEDDED_JSON_CHARS` that, once
  stripped, starts with `{`, `[`, or `"` and parses as a JSON object, array, or
  string is masked as that value (one depth level deeper, under the same depth
  cap) and written back with `json.dumps` when anything changed. A bare JSON
  string is what execution stores as a run's `output` when a driver returns
  text. Text that does not parse, or is longer, falls through to the line rule,
  which also reads JSON-escaped text (below), so an oversized or cut JSON
  document is still masked.
- LINE RULE. Inside any other string, the text is cut into segments at each
  newline and at each JSON-escaped line break (the two-character `\\n`, `\\r`, and
  `\\r\\n`), and each segment is masked on its own. A `scheme://user:password@`
  URL has its password replaced (every quantifier of that pattern is bounded).
  Then the first credential keyword token is found: one of `_KEYWORDS` (the key
  parts above plus `key-string`), optionally after a prefix ending in a hyphen
  or underscore (`authentication-key`, `auth_password`, `tcp-md5-auth`,
  `hashed-password`, `pre-shared-key`, `wpa-psk`). It must start the segment or
  follow white space, one of `"{[(,;?&<>`, or a JSON escape (`\\n`, `\\r`, `\\t`,
  `\\"`), and be followed by white space, `:`, `=`, a quote, `<`, `>`, a
  backslash, or the end of the segment. After a single quote it counts only
  when the closing quote is followed by optional blanks (at most 64) and `:` or
  `=`: Python's repr of a dict (`{'password': 'x'}`) is masked, and a refusal
  that quotes a field name (`at ['password']: ...`, `field 'password' needs`,
  CFG-SCHEMA-13) is not. Separator characters right after the keyword (white
  space, `:`, `=`, quotes, `<`, `>`, backslashes) are kept and everything after
  them to the end of the segment becomes `REDACTED_VALUE`: `password 7 <hash>`
  becomes `password [redacted]`, `Password: x` becomes `Password: [redacted]`,
  `PASSWORD=x;y` becomes `PASSWORD=[redacted]`, `key:x` becomes
  `key:[redacted]`, and `\\"password\\": \\"x\\"` becomes
  `\\"password\\": \\"[redacted]`. An XML element whose tag is a keyword
  (`<password>x</password>`, `<phash>...</phash>`) has only its text replaced,
  up to the next `<`, and the rest of the segment is searched again. A keyword
  with nothing after it masks nothing (`no password`, `show key`).
  `authentication` and `auth` also mask a mode word that follows them (`ip ospf
  authentication message-digest`): a display copy fails safe.

Cost: masking is linear in the length of the text (each regular expression is
either anchored to a boundary that a word run cannot contain or bounded in
length, and an embedded JSON value is parsed at most once per depth level);
`tests/test_config_redaction_cost.py` pins it with relative timings.

Limits, by decision: a heuristic for the shapes the in-repo drivers and common
network CLIs produce, not a guarantee. Not masked:

- a secret under a key none of whose parts is a keyword and that the log
  pattern does not match (`pw`, `pass`, `otp`, `pin`, `privkey`, `keystring`,
  `hash`);
- a secret in free text with no keyword before it on its line, including a
  secret on the line or list item AFTER the one that ends in a keyword
  (`password\\nS3cret`, `["password", "S3cret"]`: masking the next item would
  also cut the command after every `no password` line), a value after a
  separator the rule does not know (`password|S3cret`, `-p S3cret`);
- a keyword glued to its value (`passwordS3cret`), a split, abbreviated, or
  joined keyword (`pass word`, `pw=`, `apikey=`), a keyword written with
  zero-width or fullwidth characters, and encoded text (base64);
- a URL password longer than 1024 characters, or behind a user name longer
  than 256 or a scheme longer than 32;
- the part of a secret after a literal two-character `\\n` inside it (the
  segment ends there);
- an XML element whose text is further elements (`<community><name>x</name>`):
  only the text before the first child is masked; an element with a namespace
  prefix (`<ns:password>`) and text inside CDATA are not masked;
- a tuple repr (`('password', 'S3cret')`: the closing quote is followed by a
  comma, not `:` or `=`);
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

_KW = "|".join(re.escape(k) for k in _KEYWORDS)
_PREFIX = r"(?:[\w.-]*[-_])?"  # optional prefix ending in a hyphen or underscore
_KEYWORD_IN_LINE = re.compile(
    # After a boundary character or at the start of the segment, or after a
    # JSON escape (backslash then n, r, t, or a double quote).
    r"(?:(?<![^\s\"{\[(,;?&<>])|(?<=\\[nrt\"]))"
    + _PREFIX
    + r"(?:"
    + _KW
    + r")"
    + r"(?=[\s:=\"'<>\\]|$)"
    # After a single quote only when the quote closes before `:` or `=`.
    + r"|(?<=')"
    + _PREFIX
    + r"(?:"
    + _KW
    + r")(?='[ \t]{0,64}[:=])",
    re.IGNORECASE,
)
_SEPARATORS = re.compile(r"[\s:=\"'<>\\]*")
# Every quantifier is bounded, so a run of word characters split by `-` or `.`
# (a `\b` at each of them) costs a constant per start, not the rest of the run.
_URL_USERINFO = re.compile(r"(?i)(\b[a-z][a-z0-9+.-]{0,31}://[^\s/@:]{0,256}:)[^\s/@]{1,1024}@")
# A newline, or a JSON-escaped line break written as two characters.
_SEGMENT_BREAK = re.compile(r"(\n|\\r\\n|\\n|\\r)")
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


def _redact_segment(segment: str) -> tuple[str, bool]:
    """Mask a URL password, then the value after the first keyword token (an
    XML element's text only, and the search goes on after it)."""
    changed = False
    if "://" in segment:
        segment, url_count = _URL_USERINFO.subn(rf"\g<1>{REDACTED_VALUE}@", segment)
        changed = url_count > 0
    out: list[str] = []
    cursor = 0
    pos = 0
    while True:
        match = _KEYWORD_IN_LINE.search(segment, pos)
        if match is None:
            break
        start, end = match.span()
        if start > 0 and segment[start - 1] == "<" and segment.startswith(">", end):
            text_start = end + 1
            text_end = segment.find("<", text_start)
            if text_end == -1:
                text_end = len(segment)
            if segment[text_start:text_end].strip():
                out.append(segment[cursor:text_start])
                out.append(REDACTED_VALUE)
                cursor = text_end
                changed = True
            pos = max(text_end, end)
            continue
        value_start = _SEPARATORS.match(segment, end).end()
        if segment[value_start:].strip():
            # The lookahead guarantees at least one separator, so the marker
            # never reads as part of the keyword.
            out.append(segment[cursor:value_start])
            out.append(REDACTED_VALUE)
            cursor = len(segment)
            changed = True
        break
    out.append(segment[cursor:])
    return "".join(out), changed


def redact_command_text(text: str) -> tuple[str, bool]:
    """Mask the credential material in each segment of a command string: each
    line, and each JSON-escaped line of escaped text."""
    pieces = _SEGMENT_BREAK.split(text)
    changed = False
    # split() with one group alternates text and the break it found.
    for index in range(0, len(pieces), 2):
        pieces[index], piece_changed = _redact_segment(pieces[index])
        changed = changed or piece_changed
    return "".join(pieces), changed


def _embedded_json(value: str) -> Any:
    """The JSON object, array, or string a string holds, or _NOT_JSON."""
    if len(value) > _MAX_EMBEDDED_JSON_CHARS:
        return _NOT_JSON
    stripped = value.strip()
    if not stripped or stripped[0] not in '{["':
        return _NOT_JSON
    try:
        parsed = json.loads(stripped)
    except (ValueError, RecursionError):
        return _NOT_JSON
    return parsed if isinstance(parsed, (dict, list, str)) else _NOT_JSON


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
        parsed = _embedded_json(value) if depth < _MAX_DEPTH else _NOT_JSON
        if parsed is not _NOT_JSON:
            masked, changed = _redact(parsed, depth + 1)
            return (json.dumps(masked) if changed else value), changed
        return redact_command_text(value)
    return value, False


def redact_config(value: Any) -> tuple[Any, bool]:
    """Return (a masked copy of value, whether anything was masked)."""
    return _redact(value, 0)
