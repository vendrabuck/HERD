"""Regenerate `passlib_1_7_4_fixtures.json`, the legacy-hash table of issue #1355.

Until #1355 the auth service hashed and verified passwords through
`CryptContext(schemes=["bcrypt"], deprecated="auto")` from passlib 1.7.4 on bcrypt
4.0.1. This script runs THAT configuration (it needs passlib, which the service no
longer depends on) and records, for every (password, stored hash) pair below, what
passlib decided: `accept`, `reject`, or `raise` with the exception class name. Each distinct
password is stored once under `passwords` and a case names it by `password_key`.
`tests/test_password_hashing.py` asserts that the direct-bcrypt replacement accepts
exactly the pairs passlib accepted and refuses every other one (a passlib raise was
never an admission, so it maps to a refusal).

Not collected by pytest (no `test_` prefix). Run it from the repository root with
the old libraries pinned:

    uv run --no-project --with passlib==1.7.4 --with bcrypt==4.0.1 \
        python services/auth/tests/data/generate_passlib_fixtures.py

Salts are random, so a rerun produces different hashes with the same decisions.
"""

from __future__ import annotations

import json
import unicodedata
import warnings
from pathlib import Path

import bcrypt as _bcrypt
import passlib
from passlib.context import CryptContext
from passlib.hash import bcrypt as passlib_bcrypt

OUT = Path(__file__).with_name("passlib_1_7_4_fixtures.json")

# The production configuration, verbatim from the pre-#1355 auth_service.py.
PRODUCTION = CryptContext(schemes=["bcrypt"], deprecated="auto")
# The same configuration at the minimum cost, for the large matrices; passlib's
# accept and reject logic does not depend on the cost.
FAST = CryptContext(schemes=["bcrypt"], deprecated="auto", bcrypt__rounds=4)

BCRYPT64 = "./ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"

ASCII_72 = "".join(chr(ord("A") + (i % 26)) for i in range(72))
PASSWORDS = {
    "empty": "",
    "ascii": "correct horse battery staple",
    "latin1_nfc": unicodedata.normalize("NFC", "p\u00e4ssw\u00f6rd"),
    "cjk_and_emoji": "\u65e5\u672c\u8a9e-\U0001f511-key",
    "ascii_71": ASCII_72[:71],
    "ascii_72": ASCII_72,
    "ascii_73": ASCII_72 + "!",
    "ascii_200": ASCII_72 + "z" * 128,
    # 30 characters, 120 UTF-8 bytes: inside the 72-character schema cap, hashed
    # on its first 72 bytes (18 key characters).
    "emoji_30": "\U0001f511" * 30,
    # 73 bytes; the 72-byte cut falls inside the last character.
    "split_boundary": "a" + "\u65e5" * 24,
    # 100 UTF-8 bytes in 50 two-byte characters.
    "e_acute_100_bytes": "\u00e9" * 50,
}


# The hashes whose mutations include the 4096 and 4097-byte paddings (kept to a
# few, since every padded password is 4 KiB in the table).
PADDED = {"ascii", "ascii_73", "emoji_30"}


def _mutations(pw: str, padded: bool = False) -> dict[str, str]:
    raw = pw.encode("utf-8")
    out = {
        "self": pw,
        "append_x": pw + "x",
        "prepend_x": "x" + pw,
        "nul_inside": pw[:1] + "\x00" + pw[1:],
        "nul_appended": pw + "\x00",
    }
    if padded:
        # Same first 72 bytes as `pw`; passlib accepts 4096 bytes and refuses 4097,
        # counted in UTF-8 bytes (padded emoji_30 is 4097 bytes in 4007 characters).
        out["pad_to_4096_bytes"] = pw + "q" * (4096 - len(raw))
        out["pad_to_4097_bytes"] = pw + "q" * (4097 - len(raw))
    if pw:
        out["drop_last"] = pw[:-1]
        out["swapcase"] = pw.swapcase()
        out["change_first"] = ("y" if pw[0] != "y" else "z") + pw[1:]
    nfd = unicodedata.normalize("NFD", pw)
    if nfd != pw:
        out["nfd"] = nfd
    try:
        out["first_72_bytes"] = raw[:72].decode("utf-8")
    except UnicodeDecodeError:
        pass
    if pw == PASSWORDS["split_boundary"]:
        # U+65E6 shares its first two UTF-8 bytes with U+65E5, so this differs
        # from the hashed password only in byte 73.
        out["same_first_72_bytes_other_last_char"] = "a" + "\u65e5" * 23 + "\u65e6"
    return out


def _outcome(ctx: CryptContext, password, stored) -> dict:
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        try:
            verdict = ctx.verify(password, stored)
        except Exception as exc:  # noqa: BLE001 - recording what passlib raised
            return {"passlib": "raise", "error": type(exc).__name__}
    result = {"passlib": "accept" if verdict else "reject"}
    if any("padding bits" in str(w.message) for w in caught):
        result["passlib_warned"] = "padding bits"
    return result


def _case(group: str, label: str, ctx: CryptContext, password: str, stored) -> dict:
    return {"group": group, "label": label, "password": password, "hash": stored} | _outcome(
        ctx, password, stored
    )


def main() -> None:
    cases: list[dict] = []
    refused_to_hash: list[dict] = []

    # 1. Production configuration (cost 12, $2b$): each password and one mutation.
    for name, pw in PASSWORDS.items():
        stored = PRODUCTION.hash(pw)
        cases.append(_case("production", f"{name}/self", PRODUCTION, pw, stored))
        cases.append(_case("production", f"{name}/append_x", PRODUCTION, pw + "x", stored))

    # 2. Cost 4: every password against every hash, then every mutation.
    fast = {name: FAST.hash(pw) for name, pw in PASSWORDS.items()}
    for hname, stored in fast.items():
        for pname, pw in PASSWORDS.items():
            cases.append(_case("cross", f"{pname} vs {hname}", FAST, pw, stored))
        for mname, candidate in _mutations(PASSWORDS[hname], hname in PADDED).items():
            cases.append(_case("mutation", f"{hname}/{mname}", FAST, candidate, stored))

    # 3. Passwords passlib refuses to hash at all.
    for label, pw in [("nul_inside", "ab\x00cd"), ("over_4096_bytes", "\u00e9" * 2048 + "a")]:
        try:
            PRODUCTION.hash(pw)
            refused_to_hash.append({"label": label, "password": pw, "passlib": "hashed"})
        except Exception as exc:  # noqa: BLE001
            refused_to_hash.append(
                {"label": label, "password": pw, "passlib": "raise", "error": type(exc).__name__}
            )

    # 4. Legacy idents passlib produces, and $2x$, which it never produces.
    for ident in ("2", "2a", "2y", "2b"):
        handler = passlib_bcrypt.using(ident=ident, rounds=4)
        for name in ("empty", "ascii", "ascii_73", "emoji_30"):
            pw = PASSWORDS[name]
            stored = handler.hash(pw)
            for mname, candidate in _mutations(pw).items():
                cases.append(_case("ident", f"${ident}$/{name}/{mname}", FAST, candidate, stored))
            if ident == "2a":
                as_2x = "$2x$" + stored[4:]
                cases.append(_case("ident", f"$2x$/{name}/self", FAST, pw, as_2x))

    # 5. Lower and higher costs than the default still verify (no rehash, no refusal).
    for rounds in (4, 5, 6, 8, 10, 11):
        stored = passlib_bcrypt.using(rounds=rounds).hash("ascii-at-another-cost")
        cases.append(_case("cost", f"rounds {rounds}/self", FAST, "ascii-at-another-cost", stored))
        cases.append(
            _case("cost", f"rounds {rounds}/wrong", FAST, "ascii-at-another-cost!", stored)
        )

    # 6. Malformed stored values derived from one valid hash.
    pw = "malformed-base"
    good = FAST.hash(pw)
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
        "ident_3": "$3$" + good[4:],
        "cost_03": good[:4] + "03" + good[6:],
        "cost_32": good[:4] + "32" + good[6:],
        "cost_99": good[:4] + "99" + good[6:],
        "cost_single_digit": good[:4] + "4" + good[6:],
        "cost_not_digits": good[:4] + "1x" + good[6:],
        "salt_only_config": good[:29],
        "bad_char_in_checksum": good[:-1] + "!",
        "bad_char_in_salt": good[:10] + "!" + good[11:],
        "non_ascii_char": good[:-1] + "\u00e9",
        "md5_crypt": "$1$saltsalt$2vN3oTUt4pk6p9I4j6CT1/",
        "sha512_crypt": "$6$saltsalt$" + "a" * 86,
        "missing_cost_separator": good[:6] + good[7:],
    }
    for label, stored in malformed.items():
        cases.append(_case("malformed", label, FAST, pw, stored))

    # 7. Every final salt character and every final checksum character, whose low
    # bits are padding. passlib clears padding bits before comparing.
    for ident in ("2a", "2b", "2y"):
        stored = passlib_bcrypt.using(ident=ident, rounds=4).hash("padding")
        for ch in BCRYPT64:
            salt_variant = stored[:28] + ch + stored[29:]
            cases.append(
                _case("padding", f"${ident}$/salt_final={ch}", FAST, "padding", salt_variant)
            )
            sum_variant = stored[:-1] + ch
            cases.append(
                _case("padding", f"${ident}$/checksum_final={ch}", FAST, "padding", sum_variant)
            )

    # Store each distinct password once; a case names it by key.
    passwords: dict[str, str] = {}
    for case in cases:
        pw = case.pop("password")
        key = next((k for k, v in passwords.items() if v == pw), None)
        if key is None:
            key = f"p{len(passwords)}"
            passwords[key] = pw
        case["password_key"] = key
    payload = {
        "provenance": {
            "issue": 1355,
            "generator": "services/auth/tests/data/generate_passlib_fixtures.py",
            "passlib": passlib.__version__,
            "bcrypt": _bcrypt.__version__,
            "production_context": 'CryptContext(schemes=["bcrypt"], deprecated="auto")',
            "production_default_ident": passlib_bcrypt.default_ident,
            "production_default_rounds": passlib_bcrypt.default_rounds,
            "rule": "accept exactly the pairs passlib accepted; refuse every other pair",
        },
        "refused_to_hash": refused_to_hash,
        "passwords": passwords,
        "cases": cases,
    }
    # One case per line keeps the file small and its diffs readable.
    head = {k: v for k, v in payload.items() if k != "cases"}
    lines = json.dumps(head, indent=1, ensure_ascii=True)[:-2]
    body = ",\n".join(json.dumps(c, ensure_ascii=True) for c in cases)
    OUT.write_text(f'{lines},\n "cases": [\n{body}\n ]\n}}\n')
    print(f"wrote {len(cases)} cases to {OUT}")


if __name__ == "__main__":
    main()
