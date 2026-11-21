"""SUPERADMIN_PASSWORD values at the registration bounds (issue #1450).

Shared by the seed's unit tests (test_main.py) and its login tests
(test_seed_superadmin_login.py), so every value the seed is shown to accept is
also shown to sign in. Each entry is (id, value as configured, value typed at
login). The seed trims the value (IAM-BOOT-3), so the typed value is the
trimmed one. The bound counts characters, as registration and login do, not
UTF-8 bytes: the multi-byte entries are 72 characters but 144 and 288 bytes,
and hashing keeps their first 72 bytes (IAM-HASH-2).

The values are built from "zq" so that no run of 4 or more of their
characters can appear in a log line by chance.
"""

ACCEPTED_SEED_VALUES = [
    ("8-characters", "zq" * 4, "zq" * 4),
    ("72-characters", "zq" * 36, "zq" * 36),
    # Two UTF-8 bytes each: 144 bytes.
    ("72-two-byte-characters", "\u00e9" * 72, "\u00e9" * 72),
    # Four UTF-8 bytes each (outside the Basic Multilingual Plane): 288 bytes.
    ("72-four-byte-characters", "\U00010348" * 72, "\U00010348" * 72),
    # 74 as configured, 72 once trimmed.
    ("72-characters-after-trimming", " " + "zq" * 36 + "\t", "zq" * 36),
]
