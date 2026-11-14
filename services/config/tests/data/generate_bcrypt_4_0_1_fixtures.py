"""Regenerate `bcrypt_4_0_1_fixtures.json`, the config service's legacy-hash table (#1355).

Until #1355 `app/config_store.py` hashed with
`bcrypt.hashpw(password.encode(), bcrypt.gensalt())` and checked with
`bcrypt.checkpw(password.encode(), hashed.encode())` on bcrypt 4.0.1, which
silently used the first 72 bytes. This script runs those two expressions verbatim
under bcrypt 4.0.1 and records, for every (password, stored hash) pair, what the old
check decided: `accept`, `reject`, or `raise` with the exception class name.
`tests/test_config_passwords.py` asserts that the bcrypt 5 code accepts exactly the
pairs the old check accepted and refuses every other one, without raising.

Not collected by pytest (no `test_` prefix). Run it from the repository root with
the old library pinned:

    uv run --no-project --with bcrypt==4.0.1 \
        python services/config/tests/data/generate_bcrypt_4_0_1_fixtures.py

Salts are random, so a rerun produces different hashes with the same decisions.
"""

from __future__ import annotations

import json
from pathlib import Path

import bcrypt

OUT = Path(__file__).with_name("bcrypt_4_0_1_fixtures.json")
BCRYPT64 = "./ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"

ASCII_72 = "".join(chr(ord("A") + (i % 26)) for i in range(72))
PASSWORDS = {
    "empty": "",
    "ascii": "config-page-pass",
    # 32 characters (the change-password cap) and 128 UTF-8 bytes.
    "emoji_32": "\U0001f511" * 32,
    "ascii_72": ASCII_72,
    "ascii_73": ASCII_72 + "!",
    # The config service never refused NUL; bcrypt 4.0.1 hashed it as a byte.
    "nul_inside": "abc\x00defgh",
    # 73 bytes; the 72-byte cut falls inside the last character.
    "split_boundary": "a" + "\u65e5" * 24,
}


def _old_hash(password: str, rounds: int) -> str:
    # Verbatim from the pre-#1355 config_store._hash_password, cost made explicit.
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt(rounds)).decode()


def _old_check(password: str, hashed: str) -> dict:
    # Verbatim from the pre-#1355 config_store._check_password.
    try:
        verdict = bcrypt.checkpw(password.encode(), hashed.encode())
    except Exception as exc:  # noqa: BLE001 - recording what the old check raised
        return {"old": "raise", "error": type(exc).__name__}
    return {"old": "accept" if verdict else "reject"}


def _mutations(pw: str) -> dict[str, str]:
    raw = pw.encode("utf-8")
    out = {
        "self": pw,
        "append_x": pw + "x",
        "prepend_x": "x" + pw,
        "nul_appended": pw + "\x00",
        # Login is unbounded: a long password that shares the first 72 bytes.
        "pad_to_10000_bytes": pw + "q" * (10000 - len(raw)),
    }
    if pw:
        out["drop_last"] = pw[:-1]
        out["nul_inside"] = pw[:1] + "\x00" + pw[1:]
    if "\x00" in pw:
        out["cut_at_nul"] = pw.split("\x00")[0]
    try:
        out["first_72_bytes"] = raw[:72].decode("utf-8")
    except UnicodeDecodeError:
        pass
    if pw == PASSWORDS["split_boundary"]:
        out["same_first_72_bytes_other_last_char"] = "a" + "\u65e5" * 23 + "\u65e6"
    return out


def _case(group: str, label: str, password: str, stored: str) -> dict:
    return {"group": group, "label": label, "password": password, "hash": stored} | _old_check(
        password, stored
    )


def main() -> None:
    cases: list[dict] = []
    for name, pw in PASSWORDS.items():
        stored = _old_hash(pw, 12)
        cases.append(_case("production", f"{name}/self", pw, stored))
        cases.append(_case("production", f"{name}/append_x", pw + "x", stored))
    fast = {name: _old_hash(pw, 4) for name, pw in PASSWORDS.items()}
    for hname, stored in fast.items():
        for pname, pw in PASSWORDS.items():
            cases.append(_case("cross", f"{pname} vs {hname}", pw, stored))
        for mname, candidate in _mutations(PASSWORDS[hname]).items():
            cases.append(_case("mutation", f"{hname}/{mname}", candidate, stored))
    for prefix in ("2a", "2y", "2x", "2"):
        stored = "$" + prefix + "$" + fast["ascii"][4:]
        cases.append(_case("ident", f"${prefix}$/self", PASSWORDS["ascii"], stored))
        cases.append(_case("ident", f"${prefix}$/wrong", "wrong", stored))
    for rounds in (4, 5, 6, 8, 10, 11):
        stored = _old_hash("other-cost", rounds)
        cases.append(_case("cost", f"rounds {rounds}/self", "other-cost", stored))
        cases.append(_case("cost", f"rounds {rounds}/wrong", "other-cost!", stored))
    pw = "malformed-base"
    good = _old_hash(pw, 4)
    malformed = {
        "valid": good,
        "empty": "",
        "garbage": "garbage",
        "truncated_40": good[:40],
        "drop_last_char": good[:-1],
        "extra_char": good + "A",
        "trailing_newline": good + "\n",
        "leading_space": " " + good,
        "uppercase_ident": "$2B$" + good[4:],
        "ident_2c": "$2c$" + good[4:],
        "cost_03": good[:4] + "03" + good[6:],
        "cost_32": good[:4] + "32" + good[6:],
        "cost_not_digits": good[:4] + "1x" + good[6:],
        "salt_only_config": good[:29],
        "bad_char_in_checksum": good[:-1] + "!",
        "bad_char_in_salt": good[:10] + "!" + good[11:],
        "non_ascii_char": good[:-1] + "\u00e9",
        "md5_crypt": "$1$saltsalt$2vN3oTUt4pk6p9I4j6CT1/",
    }
    for label, stored in malformed.items():
        cases.append(_case("malformed", label, pw, stored))
    stored = _old_hash("padding", 4)
    for ch in BCRYPT64:
        cases.append(_case("padding", f"salt_final={ch}", "padding", stored[:28] + ch + stored[29:]))
        cases.append(_case("padding", f"checksum_final={ch}", "padding", stored[:-1] + ch))

    passwords: dict[str, str] = {}
    for case in cases:
        pw = case.pop("password")
        key = next((k for k, v in passwords.items() if v == pw), None)
        if key is None:
            key = f"p{len(passwords)}"
            passwords[key] = pw
        case["password_key"] = key
    head = {
        "provenance": {
            "issue": 1355,
            "generator": "services/config/tests/data/generate_bcrypt_4_0_1_fixtures.py",
            "bcrypt": bcrypt.__version__,
            "old_hash": "bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()",
            "old_check": "bcrypt.checkpw(password.encode(), hashed.encode())",
            "rule": "accept exactly the pairs the old check accepted; refuse every other pair",
        },
        "passwords": passwords,
    }
    lines = json.dumps(head, indent=1, ensure_ascii=True)[:-2]
    body = ",\n".join(json.dumps(c, ensure_ascii=True) for c in cases)
    OUT.write_text(f'{lines},\n "cases": [\n{body}\n ]\n}}\n')
    print(f"wrote {len(cases)} cases to {OUT}")


if __name__ == "__main__":
    main()
