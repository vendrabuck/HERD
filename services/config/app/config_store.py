import json
import logging
import os
import secrets
import stat

import bcrypt

from app.auth_rules import auth_setting_errors
from app.config_schema import CONFIG_SCHEMA, REQUIRED_KEYS

logger = logging.getLogger(__name__)

DATA_DIR = os.environ.get("HERD_CONFIG_DATA_DIR", "/data/herd-config")
CONFIG_FILE = os.path.join(DATA_DIR, "config.json")
AUTH_FILE = os.path.join(DATA_DIR, "config_auth.json")


class ConfigAuthError(Exception):
    """The stored config password cannot be used, so no login can succeed.

    Raised when the auth file exists but cannot be read or parsed, or parses to
    something other than a JSON object, and when there is no file and
    CONFIG_ADMIN_PASSWORD is set to a value password_problem refuses. Callers
    fail closed: login is refused, /status reports not rotated, and nothing
    rewrites the file (main.py answers a route that meets it with 503 and
    STORE_UNUSABLE_DETAIL).
    """


# The body of a 503 for ConfigAuthError: fixed text, never a path or a value.
STORE_UNUSABLE_DETAIL = (
    "The config password store is unusable; the config service log names the cause"
)


class DataDirNotWritable(PermissionError):
    """The auth file is missing and the data directory cannot take a new one.

    A PermissionError, so a route that meets it answers main.py's 503; read paths
    that can do without the file (is_password_changed) catch it instead.
    """


def _ensure_data_dir() -> None:
    os.makedirs(DATA_DIR, exist_ok=True)


# The config service runs as a non-root user (issue #1326). A herd-config volume
# created by an earlier, root-run image holds root-owned files it cannot write: reads
# keep working, and every write answers 503 with this detail until the operator runs
# the one-time command data_dir_remedy() names.
DATA_DIR_NOT_WRITABLE_DETAIL = (
    "The config data directory is not writable; the config service log names the one-time fix"
)


def data_dir_remedy() -> str:
    """The one log line naming the data directory, this process's uid, and the fix."""
    uid, gid = os.getuid(), os.getgid()
    return (
        f"config data directory {DATA_DIR} is not writable by uid {uid}; run: "
        f"docker compose run --rm --no-deps --user root config chown -R {uid}:{gid} {DATA_DIR}"
    )


def data_dir_writable() -> bool:
    """True when this process can create, replace, and delete the service's files.

    The directory needs write and search permission (create, delete); each of the
    three files that already exists needs write permission (replace in place).
    """
    try:
        _ensure_data_dir()
    except OSError:
        return False
    if not os.access(DATA_DIR, os.W_OK | os.X_OK):
        return False
    for path in (CONFIG_FILE, AUTH_FILE, _bootstrap_marker_file()):
        if os.path.exists(path) and not os.access(path, os.W_OK):
            return False
    return True


def _bootstrap_marker_file() -> str:
    # Derived at call time so the test fixture's DATA_DIR monkeypatch applies.
    return os.path.join(DATA_DIR, "config.bootstrapped")


# bcrypt reads at most 72 bytes of a password. bcrypt 4.0.1 dropped the rest
# silently; bcrypt 5 raises instead, so the cut is explicit (#1355) and every
# stored hash keeps verifying with exactly the passwords it accepted before.
BCRYPT_MAX_BYTES = 72
BCRYPT_ROUNDS = 12
UNUSABLE_HASH_MESSAGE = "Config auth file holds no usable password hash; login refused"


# -- Which strings are a config password (issues #1428, #1429) --

PASSWORD_NUL_DETAIL = "Password must not contain a NUL character"
PASSWORD_NOT_UTF8_DETAIL = "Password must be valid UTF-8 text"
PASSWORD_EMPTY_DETAIL = "Password must not be empty"
PASSWORD_TOO_SHORT_DETAIL = "Password must be at least 8 characters"
PASSWORD_TOO_LONG_DETAIL = "Password must be at most 32 characters"
NEW_PASSWORD_MIN_CHARS = 8
NEW_PASSWORD_MAX_CHARS = 32


class InvalidPassword(ValueError):
    """A password the config service never stores.

    Its message is always one of the fixed PASSWORD_*_DETAIL texts, never the
    value, so main.py answers it as the 422 detail as it stands.
    """


def password_problem(password: str) -> str | None:
    """The ONE rule for which strings are a config password: None when legal.

    Every entry point applies it: hashing refuses an illegal password (so neither
    change-password nor the CONFIG_ADMIN_PASSWORD seed can store one), and the
    login check answers False for one without trusting bcrypt's verdict.

    - A NUL anywhere is refused. bcrypt appends a NUL to the key and repeats it to
      fill 72 bytes, so NULs make a password equal a shorter one and a password of
      NULs alone equal the empty one (#1429). The auth service refuses NUL too
      (IAM-HASH-3).
    - A string that cannot be encoded as UTF-8 (a lone surrogate) is refused: it
      cannot be hashed at all (#1428).
    - The empty string is refused: its key is the key of every all-NUL password.

    Every other string is legal, other control characters included; the 8 to 32
    character bound is change-password's alone (new_password_problem).
    """
    if "\x00" in password:
        return PASSWORD_NUL_DETAIL
    try:
        password.encode("utf-8")
    except UnicodeEncodeError:
        return PASSWORD_NOT_UTF8_DETAIL
    if not password:
        return PASSWORD_EMPTY_DETAIL
    return None


def new_password_problem(password: str) -> str | None:
    """What change-password refuses: the length bound first, then password_problem.

    The bound counts characters (code points), not bytes: 32 characters can be 128
    UTF-8 bytes, of which bcrypt reads 72 (OPS-CONFIG-6). Checking the bound first
    keeps the wording every short or long password always got.
    """
    if len(password) < NEW_PASSWORD_MIN_CHARS:
        return PASSWORD_TOO_SHORT_DETAIL
    if len(password) > NEW_PASSWORD_MAX_CHARS:
        return PASSWORD_TOO_LONG_DETAIL
    return password_problem(password)


def _password_bytes(password: str) -> bytes:
    return password.encode()[:BCRYPT_MAX_BYTES]


def _hash_password(password: str) -> str:
    problem = password_problem(password)
    if problem is not None:
        raise InvalidPassword(problem)
    return bcrypt.hashpw(_password_bytes(password), bcrypt.gensalt(BCRYPT_ROUNDS)).decode()


def _check_password(password: str, hashed: object) -> bool:
    """True when `password` matches `hashed`; False, never an exception, otherwise.

    The unusable-hash ERROR is about the STORED value only: it is logged exactly
    when `hashed` is not a string bcrypt can read (by class name, never the
    value), whatever the client sent. A client password that cannot be a
    password (not a string, or refused by password_problem: a NUL, a lone
    surrogate, empty) is refused with no log, and the stored value is still
    checked so a broken file is still reported. Refusing it here, not only when
    hashing, is what keeps a hash stored before #1429 from a password of NULs
    from signing in with the empty password.
    """
    secret: bytes | None = None
    if isinstance(password, str) and password_problem(password) is None:
        secret = _password_bytes(password)
    if not isinstance(hashed, str):
        logger.error("%s (%s)", UNUSABLE_HASH_MESSAGE, type(hashed).__name__)
        return False
    try:
        matched = bcrypt.checkpw(b"" if secret is None else secret, hashed.encode())
    except ValueError as exc:
        logger.error("%s (%s)", UNUSABLE_HASH_MESSAGE, type(exc).__name__)
        return False
    return secret is not None and matched


# -- Auth file --


# Logged, with no arguments, on each use of a CONFIG_ADMIN_PASSWORD that
# password_problem refuses; the only reachable refusal is the UTF-8 one.
# The name avoids the word password on purpose: CodeQL's clear-text-logging
# query treats a variable named like a credential as one (alert 47 on PR #1443).
ENV_VALUE_REFUSED_LOG = (
    "CONFIG_ADMIN_PASSWORD is not valid UTF-8 text, so it cannot be the config "
    "password; no auth file written, login refused"
)


def _initial_password() -> tuple[str, bool]:
    """Resolve the config-page password seeded on first boot.

    Returns (password, changed). An operator-provided CONFIG_ADMIN_PASSWORD is
    treated as a chosen credential (changed=True, so the write surface is
    unlocked immediately). Otherwise a random one-time password is generated and
    logged ONCE at WARNING for the operator to read from the container logs, and
    marked unrotated (changed=False) so the write/apply surface stays locked
    until it is changed. A guessable source-visible constant is never used: the
    previous "admin123!" default let anyone reach the publicly-routed config
    write surface (issue #256).
    """
    env_pw = os.environ.get("CONFIG_ADMIN_PASSWORD", "").strip()
    if env_pw:
        # The environment cannot hold a NUL, and a blank value never reaches this
        # branch, so the one refusal password_problem can make here is a byte
        # that is not UTF-8 (it arrives as a lone surrogate). Fail closed like a
        # corrupt file: store nothing (a generated password would replace the
        # operator's choice), refuse login, and let fixing the variable be the
        # recovery. The log line is a constant with no arguments: nothing derived
        # from the password, its refusal text included, reaches a log.
        if password_problem(env_pw) is not None:
            logger.error(ENV_VALUE_REFUSED_LOG)
            raise ConfigAuthError("CONFIG_ADMIN_PASSWORD is not a usable config password")
        return env_pw, True
    generated = secrets.token_urlsafe(24)
    logger.warning(
        "No CONFIG_ADMIN_PASSWORD set; generated a one-time config-page password: %s "
        "Log in with it and change it; the config write surface is locked until you do.",
        generated,
    )
    return generated, False


def _default_auth() -> dict:
    password, changed = _initial_password()
    return {
        "password_hash": _hash_password(password),
        "password_changed": changed,
    }


def load_auth() -> dict:
    if not os.path.exists(AUTH_FILE):
        # Checked BEFORE a password is generated: on a directory that cannot keep
        # the file, a generated password would be logged, never stored, and
        # regenerated (with a bcrypt hash) on every call (issue #1326).
        if not data_dir_writable():
            raise DataDirNotWritable(f"cannot create {AUTH_FILE}")
        auth = _default_auth()
        _save_auth(auth)
        return auth
    try:
        with open(AUTH_FILE) as f:
            loaded = json.load(f)
    except (ValueError, RecursionError, OSError) as exc:
        # ValueError covers JSONDecodeError, a byte that is not UTF-8
        # (UnicodeDecodeError), and an integer over Python's digit limit;
        # RecursionError is nesting deeper than the parser allows; OSError is an
        # unreadable path, a directory included. MemoryError on a huge file is
        # not handled.
        # Fail closed: do NOT regenerate the default here, that would silently
        # reset the admin password. Recovery is operator-driven: remove the
        # corrupt file and the missing-file branch above regenerates the default.
        logger.error("Corrupt config auth file %s: %s", AUTH_FILE, exc)
        raise ConfigAuthError(f"corrupt auth file {AUTH_FILE}") from exc
    if not isinstance(loaded, dict):
        # Valid JSON that is not an object holds no hash and no rotation flag:
        # fail closed exactly like a corrupt file (issue #1427), and name the
        # class only, never the value.
        logger.error("%s (%s)", UNUSABLE_HASH_MESSAGE, type(loaded).__name__)
        raise ConfigAuthError(f"auth file {AUTH_FILE} is not a JSON object")
    return loaded


def _save_auth(auth: dict) -> None:
    _ensure_data_dir()
    with open(AUTH_FILE, "w") as f:
        json.dump(auth, f, indent=2)


def verify_password(password: str) -> bool:
    try:
        auth = load_auth()
    except ConfigAuthError:
        # Fail closed: deny login when the auth file is present but corrupt.
        return False
    # load_auth answers an object or raises; an object that holds no hash (the
    # key missing) fails closed the same way, in _check_password.
    return _check_password(password, auth.get("password_hash"))


def change_password(new_password: str) -> None:
    """Store a new password and mark it rotated, or raise InvalidPassword.

    The password is judged before the file is read, so a refused one never
    touches it.
    """
    problem = new_password_problem(new_password)
    if problem is not None:
        raise InvalidPassword(problem)
    # Reached with an unusable auth file only by a session issued before the file
    # broke; load_auth's ConfigAuthError then propagates (main.py: 503) and the
    # file is never rewritten, so a session cannot turn it into a rotated one.
    auth = load_auth()
    auth["password_hash"] = _hash_password(new_password)
    auth["password_changed"] = True
    _save_auth(auth)


def is_password_changed() -> bool:
    """True only when the stored flag is the JSON boolean true (issue #1427).

    Any other stored value ("yes", 1, null, missing) is not rotated, so the write
    surface stays locked; it never reads a truthy non-boolean as rotated.
    """
    try:
        return load_auth().get("password_changed") is True
    except (ConfigAuthError, DataDirNotWritable):
        # Keep the public /status endpoint up; report the safe default. An
        # unusable file, or an unwritable directory with no auth file, has no
        # usable password.
        return False


# -- Config file --


def is_configured() -> bool:
    return os.path.exists(CONFIG_FILE)


def load_config() -> dict:
    """The config.json object, or {} when there is none to use.

    The same answer for the same path state as the services' loader
    (herd_common.config_loader._read_config_object, copied here because this
    service imports no herd_common): a missing path is {} with no log; a path that
    is not a regular file (checked before any read, so a FIFO never blocks), a
    dangling link, an unreadable file, JSON that does not parse as UTF-8 text, and
    JSON that is not an object are {} with one WARNING naming the path and the
    reason, never the content. MemoryError on a huge file is not handled.
    """
    if not os.path.lexists(CONFIG_FILE):
        return {}
    loaded, reason = _read_config_object(CONFIG_FILE)
    if reason is not None:
        logger.warning("Ignoring unusable config file %s (%s)", CONFIG_FILE, reason)
        return {}
    return loaded


def _read_config_object(path: str) -> tuple[dict, None] | tuple[None, str]:
    """(object, None) for a regular file holding a JSON object, else (None, reason)."""
    try:
        mode = os.stat(path).st_mode
        if not stat.S_ISREG(mode):
            return None, f"not a regular file: {_file_kind(mode)}"
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC)
        with open(fd, encoding="utf-8") as f:
            mode = os.fstat(f.fileno()).st_mode
            if not stat.S_ISREG(mode):
                return None, f"not a regular file: {_file_kind(mode)}"
            loaded = json.load(f)
    except (ValueError, RecursionError, OSError) as exc:
        # The same failures load_auth catches. Named by class only: the text of a
        # UnicodeDecodeError quotes a byte of the file.
        return None, type(exc).__name__
    if not isinstance(loaded, dict):
        # Valid JSON that is not an object holds no settings; treat it like a
        # corrupt file rather than handing callers a list or a scalar.
        return None, f"not a JSON object: {type(loaded).__name__}"
    return loaded, None


def _file_kind(mode: int) -> str:
    for test, kind in (
        (stat.S_ISDIR, "directory"),
        (stat.S_ISFIFO, "FIFO"),
        (stat.S_ISSOCK, "socket"),
        (stat.S_ISCHR, "character device"),
        (stat.S_ISBLK, "block device"),
    ):
        if test(mode):
            return kind
    return "other"


def save_config(values: dict) -> list[str]:
    """Save config values. Returns list of validation errors (empty on success).

    Nothing is written when any error is returned. AUTH_SECRET_KEY and
    AUTH_ALGORITHM are judged by app.auth_rules (the services' startup rules), so
    a blank or placeholder secret reports that rule's message rather than the
    generic "is required".
    """
    errors = []
    for key in REQUIRED_KEYS:
        if key == "AUTH_SECRET_KEY":
            continue
        val = values.get(key)
        if val is None or (isinstance(val, str) and val.strip() == ""):
            errors.append(f"{key} is required")
    errors += auth_setting_errors(values)
    if errors:
        return errors

    _ensure_data_dir()
    with open(CONFIG_FILE, "w") as f:
        json.dump(values, f, indent=2)
    # A save is a deliberate operator decision: dropping the bootstrap marker
    # promotes config.json above the environment in every service from its
    # next start (see herd_common.config_loader.herd_settings_sources).
    try:
        os.remove(_bootstrap_marker_file())
    except FileNotFoundError:
        pass
    return []


def load_env_values() -> dict[str, str]:
    """Return CONFIG_SCHEMA keys that are present and non-blank in os.environ."""
    values: dict[str, str] = {}
    for field in CONFIG_SCHEMA:
        raw = os.environ.get(field["key"], "")
        if raw.strip():
            values[field["key"]] = raw
    return values


def bootstrap_from_env() -> bool:
    """Write config.json from process env on first startup when possible.

    Idempotent: returns False if config.json already exists. Reads every
    CONFIG_SCHEMA key from os.environ and writes a config file when every
    REQUIRED_KEYS entry is present and non-empty and the access-token settings
    pass app.auth_rules (so a placeholder secret is never copied into a file
    that outranks a later .env fix once the UI saves). When required values are
    missing or refused the user still reaches the config UI as before.

    The admin password file is left untouched so the config page keeps its
    default-password gate on first visit.
    """
    if os.path.exists(CONFIG_FILE):
        return False

    values = load_env_values()

    missing = [key for key in REQUIRED_KEYS if not values.get(key, "").strip()]
    if missing:
        logger.warning(
            "Config auto-bootstrap skipped; missing required env vars: %s",
            ", ".join(missing),
        )
        return False

    refused = auth_setting_errors(values)
    if refused:
        logger.warning("Config auto-bootstrap skipped; %s", " ".join(refused))
        return False

    _ensure_data_dir()
    with open(CONFIG_FILE, "w") as f:
        json.dump(values, f, indent=2)
    # Mark the file as env-bootstrapped: it is a copy of the environment, not
    # an operator decision, so services keep treating the environment as
    # authoritative until the first real config-UI save deletes this marker.
    with open(_bootstrap_marker_file(), "w") as f:
        f.write("created by first-boot auto-bootstrap; deleted on first config-UI save\n")
    logger.info("Config bootstrapped from environment into %s", CONFIG_FILE)
    return True
