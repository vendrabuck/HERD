"""Every access-token decode goes through the one shared helper and names exactly
one algorithm, and every service's access-token settings use the shared startup
validation.

Two AST scans; nothing is imported or executed.

DECODE CALLS. Every Python file under `services/*/app` and
`services/common/herd_common` is read for references to PyJWT's decode
functions. A reference is any of these spellings:

- `jwt.decode` or `jwt.decode_complete` after `import jwt` (or `import jwt as j`,
  then `j.decode`);
- `jwt.api_jwt.decode`, `jwt.api_jws.decode`, and their `decode_complete`, after
  `import jwt`, `import jwt.api_jwt`, `from jwt import api_jwt`, or `import
  jwt.api_jws as S` (then `S.decode`);
- the bare name after `from jwt import decode`, `from jwt.api_jwt import
  decode`, or `from jwt.api_jws import decode_complete` (or `... as d`);
- `decode` and `decode_complete` on a `PyJWT` or `PyJWS` instance: a call of the
  class under any spelling above (`jwt.PyJWT()`, `from jwt import PyJWS` then
  `PyJWS()`, `jwt.api_jwt.PyJWT()`), or a name bound to one;
- `getattr(<object>, "decode")` on any of the module, class, or instance
  spellings above;
- any name bound by a plain assignment to one of the above, or to one of the
  module, class, or instance spellings (`decode = jwt.decode`, `codec = jwt`,
  `codec = jwt.PyJWT()`), followed transitively;
- any of the above reached from another scanned module: a name imported from a
  scanned module (`from herd_common.auth import jwt`, `from .codec import d`,
  `from app.codec import jwt`, `from herd_common.auth import *`) is what it is
  bound to there, and so is an attribute of a scanned module (`import
  herd_common.auth` then `herd_common.auth.jwt.decode`, `from herd_common import
  auth` then `auth.jwt.decode`), across any number of modules. `herd_common.*` is
  the shared package; `app.*` is resolved inside the importing file's own
  service, since every service has its own `app` package. Any other `import jwt`
  is PyJWT.

Every reference must be the function of a call (a bare reference handed on, as in
`functools.partial(jwt.decode, ...)`, is refused), and every such call must:

- sit in `decode_access_token` in `herd_common/jwt_codec.py` (`DECODE_HELPER`),
  or in a function named in `DIRECT_DECODE_ALLOWED` with a count and a reason;
  every other site calls the helper. A site is the file plus the qualified path
  of the innermost function or class around the call (`f`, `outer.inner`,
  `Class.method`, `<module>`), so a nested function or a method that reuses an
  allowed name is a different site and is refused, and each allowed site must
  hold exactly its count of decode calls (the helper one). The table holds only
  the config service's session decode, which may not import `herd_common`; an
  entry nothing matches fails as stale;
- pass the token and the key as the only positional arguments, with no `*args`
  and no `**kwargs`;
- pass `algorithms=` as a list literal of exactly one element, not starred;
- pass no `options=`, or a dict literal with no `**` whose `verify_signature`
  key, when present, is the literal `True`.

Also refused: a star import from `jwt` or any `jwt.*` module; a name starting
with `_` imported from `jwt`, `jwt.api_jwt`, or `jwt.api_jws`, or read as an
attribute of a PyJWT module, class, or instance (`jwt.api_jwt._jwt_global_obj`,
`jwt.__dict__`, `getattr(jwt, "__dict__")`); an attribute of one assigned or
deleted (`jwt.decode = f`, `codec.options = {...}`); a `PyJWT` or `PyJWS` built
with any argument (its `options` can turn verification off for every call); any
reference to `register_algorithm` or `unregister_algorithm` on the package,
`jwt.api_jws`, or a `PyJWS` class or instance; a PyJWT module, class, or instance
used in any way but an attribute read, a `getattr` with a literal name, a plain
alias assignment, or (a class) a call (`vars(jwt)`, `run(jwt)`, `(jwt,)`,
`setattr(jwt, ...)`, `getattr(jwt, name)`).

`get_unverified_header` on the package, `jwt.api_jws`, or a `PyJWS` class or
instance, under the same spellings or imported by name from `jwt` or
`jwt.api_jws`, may appear only inside a function named in
`UNVERIFIED_READ_ALLOWED` with a reason. The table is empty today; an entry
nothing matches fails as stale. (A decode with `verify_signature` off, the other
unverified read, is refused by the call rule above.)

SETTINGS. Every `services/*/app/config.py` with a class attribute named
`secret_key` (compared case-insensitively, since the Settings models read the
environment case-insensitively) must, in that class, annotate it with
`AuthSecretKey` from `herd_common.auth_settings` with no default, and annotate
`algorithm` with `JwtAlgorithm` from the same module with no default or a string
default that is one of HS256, HS384, HS512 (pydantic does not validate a default).

- The type may be imported under an alias, reached as an attribute of the module
  (`auth_settings.AuthSecretKey`, `herd_common.auth_settings.AuthSecretKey`),
  written as a string annotation (`"AuthSecretKey"`), or wrapped in `Annotated`
  (from `typing` or `typing_extensions`, under an alias too) whose other elements
  are strings or `Field(...)` calls with no default.
- A `Field(...)` value (pydantic's `Field`, under an import alias or as
  `pydantic.Field`) is not a default when it passes no positional default other
  than the Ellipsis and no `default=` or `default_factory=`; otherwise its default
  is judged as a plain one, and a factory or `**` is a default the scan does not
  evaluate (refused).
- Each attribute must be declared exactly once in the class and named by no
  `field_validator` or `validator` decorator, under its own name, an import alias
  (`from pydantic import field_validator as fv`), or a name assigned from either;
  the wildcard `"*"` names both. A validator could rewrite the value before or
  after the shared rule.
- No statement in the file may rebind the local name of either type or of the
  module (an assignment, a `def` or `class`, an `except ... as`, a loop or `with`
  target, another import), at module level, in a class, or in a function.

The config service has no `app/config.py` and is skipped by the settings scan by
construction; the decode scan reads it, and its session decode is the one
`DIRECT_DECODE_ALLOWED` entry.

Limits. What the scans cannot see:

- a decode reached by `importlib.import_module("jwt")`, `__import__`,
  `sys.modules[...]`, source run by `exec(...)` or `eval(...)` (a string is not
  parsed), a signature checked by hand (`jwt.algorithms`,
  `jwt.api_jws.PyJWS._verify_signature` is refused only as a private attribute),
  claims read without a decode (`jwt.utils.base64url_decode`, `json.loads` of a
  segment), or code outside the two scanned roots;
- a name followed from another module only through an import statement naming a
  scanned module, or an attribute or literal `getattr` of a scanned module; a
  scanned module object handed on (`vars(herd_common.auth)["jwt"]`,
  `herd_common.auth.__dict__`), a non-literal `getattr` on one, a function's
  return value (`make_codec().decode`), or an attribute of anything that is not a
  module, class, or instance the scan tracks (`obj.d(...)`) is not followed;
- a `PyJWT` or `PyJWS` instance is tracked only when it is built by a call the
  scan reads; one built through a subclass (`class C(jwt.PyJWT)`), returned by a
  function, or taken from a container is not;
- refused although it can be honest (false reds, by decision, because the scan
  cannot follow them): a decode stored in a class body, on an instance
  (`class V: d = jwt.decode`, `self.d = jwt.decode`), or in a container is
  reported as referenced, not called; a PyJWT module, class, or instance handed
  to anything (`vars`, a helper, a tuple) is refused; an unbound
  `jwt.PyJWT.decode(instance, t, k, ...)` is refused as passing more than the
  token and the key positionally; a rebinding of a shared type's name inside a
  function, where it cannot change a class annotation, is refused;
- names are followed without regard to statement order or scope, so a rebinding
  to another value can make an unrelated call read as a decode (refused, a false
  red), never the reverse; a name bound both to PyJWT and to a scanned module
  (`import jwt` and `from app.utils import jwt` in one file) is read as whichever
  binding the scan meets last;
- `algorithms=` whose single element is itself a list or a name bound to several
  algorithms at runtime (`algorithms=[ALGS]` with `ALGS` a list); the element is
  not evaluated; nor is `leeway`, which only `test_jwt_codec.py` pins;
- a Settings field reading `SECRET_KEY` or `ALGORITHM` under another attribute
  name through `Field(alias=...)` or `validation_alias`, a field or a validator
  added by a base class other than the one in the file, or a `model_validator`;
  `Field` and `Annotated` are recognized by name (a local function named `Field`
  that returns a default is read as pydantic's);
- whether the decode result is used, or whether the code is reached at all.
"""

import ast
import functools
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]

PYJWT = "jwt"
# The PyJWT modules the scan tracks, by dotted name, and the kind each binds.
PYJWT_MODULES = {"jwt": "package", "jwt.api_jwt": "jwt_module", "jwt.api_jws": "jws_module"}
_DECODES = {"decode": "decode", "decode_complete": "decode"}
_REGISTRY = {"register_algorithm": "registry", "unregister_algorithm": "registry"}
_JWS_READS = {**_DECODES, "get_unverified_header": "unverified", **_REGISTRY}
# What an attribute (or a name imported from a module) of each tracked kind is.
ATTRIBUTES: dict[str, dict[str, str]] = {
    "package": {
        "api_jwt": "jwt_module",
        "api_jws": "jws_module",
        "PyJWT": "jwt_class",
        "PyJWS": "jws_class",
        **_JWS_READS,
    },
    "jwt_module": {"PyJWT": "jwt_class", "PyJWS": "jws_class", **_DECODES},
    "jws_module": {"PyJWS": "jws_class", **_JWS_READS},
    "jwt_class": dict(_DECODES),
    "jws_class": dict(_JWS_READS),
    "jwt_instance": dict(_DECODES),
    "jws_instance": dict(_JWS_READS),
}
INSTANCE_OF = {"jwt_class": "jwt_instance", "jws_class": "jws_instance"}
OBJECT_KINDS = tuple(ATTRIBUTES)
AUTH_SETTINGS_MODULE = "herd_common.auth_settings"
SHARED_TYPES = ("AuthSecretKey", "JwtAlgorithm")
ALLOWED_ALGORITHMS = ("HS256", "HS384", "HS512")

# The one shared decode: (path relative to the repo, qualified function path). It
# holds exactly one decode call.
DECODE_HELPER = ("services/common/herd_common/jwt_codec.py", "decode_access_token")

# (path relative to the repo, qualified function path) to (decode calls it holds,
# reason): the only other functions that may call a PyJWT decode directly.
DIRECT_DECODE_ALLOWED: dict[tuple[str, str], tuple[int, str]] = {
    ("services/config/app/auth.py", "_verify_token"): (
        1,
        "the config service is standalone and may not import herd_common (its image "
        "does not ship it), so it verifies its own config session token, signed with "
        "CONFIG_SESSION_SECRET, not an access token",
    ),
}

# (path relative to the repo, qualified function path) to reason. Empty by design.
UNVERIFIED_READ_ALLOWED: dict[tuple[str, str], str] = {}

# The four app functions that verify a token, each through the shared helper.
HELPER_CALL_SITES = {
    ("services/common/herd_common/auth.py", "make_auth_dependencies.get_current_user_payload"),
    ("services/common/herd_common/auth_settings.py", "_signs_and_verifies"),
    ("services/auth/app/utils/jwt.py", "verify_access_token"),
    ("services/notifications/app/routers/notifications.py", "get_bearer_credentials"),
}


def scanned_files() -> list[Path]:
    files = set((REPO / "services").glob("*/app/**/*.py"))
    files |= set((REPO / "services" / "common" / "herd_common").glob("**/*.py"))
    return sorted(files)


# --- the module index ---


def module_key(rel_path: str) -> str | None:
    """The importable name of a scanned file, or None for any other path.

    `herd_common` files are `herd_common.<...>`; a service's `app` files are
    `<service>:app.<...>`, because every service has its own `app` package.
    """
    parts = Path(rel_path).parts
    if len(parts) >= 4 and parts[:3] == ("services", "common", "herd_common"):
        prefix, dotted = "", list(parts[2:])
    elif len(parts) >= 4 and parts[0] == "services" and parts[2] == "app":
        prefix, dotted = f"{parts[1]}:", list(parts[2:])
    else:
        return None
    if not dotted[-1].endswith(".py"):
        return None
    dotted[-1] = dotted[-1][: -len(".py")]
    if dotted[-1] == "__init__":
        dotted = dotted[:-1]
    return prefix + ".".join(dotted)


class _Index:
    """Every scanned module's names, followed across modules to a fixed point."""

    MAX_PASSES = 50

    def __init__(self, sources: dict[str, str]):
        self.trees = {rel: ast.parse(src) for rel, src in sources.items()}
        self.keys = {rel: module_key(rel) for rel in sources}
        self.by_key = {key: rel for rel, key in self.keys.items() if key is not None}
        self.is_package = {
            key: rel.endswith("__init__.py") for rel, key in self.keys.items() if key is not None
        }
        self.known: set[str] = set()
        for key in self.by_key:
            prefix, sep, dotted = key.rpartition(":")
            pieces = dotted.split(".")
            for i in range(1, len(pieces) + 1):
                self.known.add(prefix + sep + ".".join(pieces[:i]))
        self.names: dict[str, _ModuleNames] = {}
        for _ in range(self.MAX_PASSES):
            changed = False
            for rel, tree in self.trees.items():
                names = _ModuleNames(tree, self, self.keys[rel])
                old = self.names.get(rel)
                if old is None or old.bindings != names.bindings:
                    changed = True
                self.names[rel] = names
            if not changed:
                break
        else:
            raise AssertionError("the module names did not reach a fixed point")

    def qualify(self, own_key: str | None, dotted: str, level: int) -> str | None:
        """The key of the scanned module an import names, or None."""
        if own_key is None:
            return None
        prefix, sep, own = own_key.rpartition(":")
        if level:
            package = own if self.is_package.get(own_key) else own.rpartition(".")[0]
            for _ in range(level - 1):
                package = package.rpartition(".")[0]
            if not package:
                return None
            dotted = f"{package}.{dotted}" if dotted else package
        if dotted == "herd_common" or dotted.startswith("herd_common."):
            key = dotted
        elif prefix and (dotted == "app" or dotted.startswith("app.")):
            key = prefix + sep + dotted
        else:
            return None
        return key if key in self.known else None

    def lookup(self, key: str, name: str) -> str | None:
        """What `name` is in the scanned module `key`: a binding, or a submodule."""
        rel = self.by_key.get(key)
        if rel is not None and rel in self.names:
            kind = self.names[rel].bindings.get(name)
            if kind is not None:
                return kind
        if f"{key}.{name}" in self.known:
            return f"mod:{key}.{name}"
        return None

    def exports(self, key: str) -> dict[str, str]:
        """The bindings `from <key> import *` copies (names without a leading `_`)."""
        rel = self.by_key.get(key)
        if rel is None or rel not in self.names:
            return {}
        return {n: k for n, k in self.names[rel].bindings.items() if not n.startswith("_")}


# --- decode references ---


def _is_pyjwt_module(dotted: str | None) -> bool:
    return dotted is not None and (dotted == PYJWT or dotted.startswith(PYJWT + "."))


def _attribute_target(node: ast.AST) -> tuple[ast.AST, str] | None:
    """(object, attribute name) for `obj.name` and `getattr(obj, "name")`."""
    if isinstance(node, ast.Attribute):
        return node.value, node.attr
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "getattr"
        and len(node.args) >= 2
        and isinstance(node.args[1], ast.Constant)
        and isinstance(node.args[1].value, str)
    ):
        return node.args[0], node.args[1].value
    return None


class _ModuleNames:
    """What each name in one module is bound to: a PyJWT module, class, or instance
    (a kind in `ATTRIBUTES`), `decode`, an unverified reader (`unverified`), the
    algorithm registry (`registry`), or a scanned module (`mod:<key>`). Names are
    read from every import and every plain assignment in the file, without regard
    to statement order or scope."""

    def __init__(self, tree: ast.AST, index: "_Index", own_key: str | None):
        self.index = index
        self.bindings: dict[str, str] = {}
        self.star_imports: list[ast.ImportFrom] = []
        self.private_imports: list[tuple[ast.ImportFrom, str]] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                self._import(node, own_key)
            elif isinstance(node, ast.ImportFrom):
                self._import_from(node, own_key)
        self._follow_assignments(tree)

    def _import(self, node: ast.Import, own_key: str | None) -> None:
        for alias in node.names:
            if _is_pyjwt_module(alias.name):
                if alias.asname:
                    kind = PYJWT_MODULES.get(alias.name)
                    if kind is not None:
                        self.bindings[alias.asname] = kind
                else:
                    self.bindings[PYJWT] = "package"
            elif alias.asname:
                key = self.index.qualify(own_key, alias.name, 0)
                if key is not None:
                    self.bindings[alias.asname] = f"mod:{key}"
            else:
                first = alias.name.split(".")[0]
                key = self.index.qualify(own_key, first, 0)
                if key is not None:
                    self.bindings[first] = f"mod:{key}"

    def _import_from(self, node: ast.ImportFrom, own_key: str | None) -> None:
        if node.level == 0 and _is_pyjwt_module(node.module):
            module_kind = PYJWT_MODULES.get(node.module)
            for alias in node.names:
                if alias.name == "*":
                    self.star_imports.append(node)
                    continue
                if module_kind is None:
                    continue
                if alias.name.startswith("_"):
                    self.private_imports.append((node, alias.name))
                    continue
                kind = ATTRIBUTES[module_kind].get(alias.name)
                if kind is not None:
                    self.bindings[alias.asname or alias.name] = kind
            return
        key = self.index.qualify(own_key, node.module or "", node.level)
        if key is None:
            return
        for alias in node.names:
            if alias.name == "*":
                self.bindings.update(self.index.exports(key))
                continue
            kind = self.index.lookup(key, alias.name)
            if kind is not None:
                self.bindings[alias.asname or alias.name] = kind

    def _follow_assignments(self, tree: ast.AST) -> None:
        assigns = [
            (node.targets, node.value) for node in ast.walk(tree) if isinstance(node, ast.Assign)
        ] + [
            ([node.target], node.value)
            for node in ast.walk(tree)
            if isinstance(node, ast.AnnAssign) and node.value is not None
        ]
        changed = True
        while changed:
            changed = False
            for targets, value in assigns:
                kind = self.kind_of(value)
                if kind is None:
                    continue
                for target in targets:
                    if isinstance(target, ast.Name) and self.bindings.get(target.id) != kind:
                        self.bindings[target.id] = kind
                        changed = True

    def kind_of(self, node: ast.AST) -> str | None:
        """What a node refers to: a binding kind as above, or None."""
        if isinstance(node, ast.Name):
            return self.bindings.get(node.id)
        target = _attribute_target(node)
        if target is None:
            if isinstance(node, ast.Call):
                return INSTANCE_OF.get(self.kind_of(node.func))
            return None
        obj, name = target
        obj_kind = self.kind_of(obj)
        if obj_kind in ATTRIBUTES:
            return ATTRIBUTES[obj_kind].get(name)
        if obj_kind is not None and obj_kind.startswith("mod:"):
            return self.index.lookup(obj_kind[len("mod:") :], name)
        return None


def _decode_call_problems(call: ast.Call) -> list[str]:
    problems = []
    if any(isinstance(a, ast.Starred) for a in call.args):
        problems.append("passes *args")
    if len(call.args) > 2:
        problems.append("passes more than the token and the key positionally")
    keywords = {}
    for kw in call.keywords:
        if kw.arg is None:
            problems.append("passes **kwargs")
        else:
            keywords[kw.arg] = kw.value
    algorithms = keywords.get("algorithms")
    if algorithms is None:
        problems.append("names no algorithms= keyword")
    elif not (
        isinstance(algorithms, ast.List)
        and len(algorithms.elts) == 1
        and not isinstance(algorithms.elts[0], ast.Starred)
    ):
        problems.append("algorithms= is not a one-element list literal")
    options = keywords.get("options")
    if options is not None:
        if not isinstance(options, ast.Dict) or any(k is None for k in options.keys):
            problems.append("options= is not a dict literal without **")
        else:
            for key, value in zip(options.keys, options.values):
                if (
                    isinstance(key, ast.Constant)
                    and key.value == "verify_signature"
                    and not (isinstance(value, ast.Constant) and value.value is True)
                ):
                    problems.append("options= turns verify_signature off")
                elif not isinstance(key, ast.Constant):
                    problems.append("options= has a key that is not a literal")
    return problems


def _enclosing_functions(tree: ast.AST) -> dict[int, str]:
    """Map id(node) to the qualified path of the innermost function or class that
    contains it (`f`, `outer.inner`, `Class.method`), or `<module>`."""
    owners: dict[int, str] = {}

    def visit(node: ast.AST, path: str) -> None:
        for child in ast.iter_child_nodes(node):
            child_path = path
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                child_path = f"{path}.{child.name}" if path else child.name
            owners[id(child)] = child_path or "<module>"
            visit(child, child_path)

    visit(tree, "")
    return owners


def _is_store(node: ast.AST) -> bool:
    return isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del))


def direct_decode_allowed() -> dict[tuple[str, str], int]:
    """The sites that may call a PyJWT decode, each with how many calls it holds:
    the helper (one) and the table."""
    return {DECODE_HELPER: 1} | {site: count for site, (count, _) in DIRECT_DECODE_ALLOWED.items()}


def _module_findings(index: _Index, rel_path: str, direct_rule: bool) -> list[str]:
    tree = index.trees[rel_path]
    names = index.names[rel_path]
    owners = _enclosing_functions(tree)
    findings = [f"{rel_path}:{n.lineno} star import from {n.module}" for n in names.star_imports]
    findings += [
        f"{rel_path}:{n.lineno} private name {name} imported from {n.module}"
        for n, name in names.private_imports
    ]
    allowed = direct_decode_allowed()
    allowed_counts: dict[str, int] = {}
    called: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func_kind = names.kind_of(node.func)
        if func_kind == "decode":
            called.add(id(node.func))
            for problem in _decode_call_problems(node):
                findings.append(f"{rel_path}:{node.lineno} jwt decode {problem}")
            owner = owners.get(id(node), "<module>")
            if (rel_path, owner) in allowed:
                allowed_counts[owner] = allowed_counts.get(owner, 0) + 1
            elif direct_rule:
                findings.append(
                    f"{rel_path}:{node.lineno} jwt decode called directly in {owner}; "
                    "call herd_common.jwt_codec.decode_access_token"
                )
        elif func_kind in INSTANCE_OF:
            called.add(id(node.func))
            if node.args or node.keywords:
                findings.append(
                    f"{rel_path}:{node.lineno} constructs PyJWT or PyJWS with arguments"
                )
    if direct_rule:
        for owner, count in sorted(allowed_counts.items()):
            if count != allowed[(rel_path, owner)]:
                findings.append(
                    f"{rel_path} jwt decode called {count} times in {owner}; "
                    f"the allowlist admits {allowed[(rel_path, owner)]}"
                )
    # A plain assignment to names is an alias the scan follows, except directly in
    # a class body, where the alias becomes an attribute the scan cannot follow.
    class_body = {
        id(stmt) for n in ast.walk(tree) if isinstance(n, ast.ClassDef) for stmt in n.body
    }
    alias_values = {
        id(node.value)
        for node in ast.walk(tree)
        if isinstance(node, (ast.Assign, ast.AnnAssign))
        and node.value is not None
        and id(node) not in class_body
        and all(
            isinstance(t, ast.Name)
            for t in (node.targets if isinstance(node, ast.Assign) else [node.target])
        )
    }
    # A PyJWT module, class, or instance may be read only by attribute (a literal
    # getattr included); any other use hands it to code the scan does not follow.
    read_by_attribute: set[int] = set()
    for node in ast.walk(tree):
        target = _attribute_target(node)
        if target is None:
            continue
        obj, attr = target
        read_by_attribute.add(id(obj))
        if names.kind_of(obj) not in OBJECT_KINDS:
            continue
        if attr.startswith("_"):
            findings.append(f"{rel_path}:{node.lineno} jwt private attribute {attr}")
        if isinstance(node, ast.Attribute) and isinstance(node.ctx, (ast.Store, ast.Del)):
            findings.append(f"{rel_path}:{node.lineno} jwt attribute assigned {attr}")
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)) or _is_store(node):
            continue
        kind = names.kind_of(node)
        owner = owners.get(id(node), "<module>")
        if kind == "decode":
            if id(node) not in called and id(node) not in alias_values:
                findings.append(f"{rel_path}:{node.lineno} jwt decode referenced, not called")
        elif kind == "unverified":
            if (rel_path, owner) not in UNVERIFIED_READ_ALLOWED:
                findings.append(f"{rel_path}:{node.lineno} jwt unverified read in {owner}")
        elif kind == "registry":
            findings.append(f"{rel_path}:{node.lineno} jwt algorithm registry used in {owner}")
        elif kind in OBJECT_KINDS:
            if (
                id(node) not in read_by_attribute
                and id(node) not in alias_values
                and id(node) not in called
            ):
                findings.append(
                    f"{rel_path}:{node.lineno} jwt object referenced other than by attribute"
                )
    return sorted(set(findings))


def _index_findings(index: _Index, direct_rule: bool = True) -> list[str]:
    findings: list[str] = []
    for rel_path in index.trees:
        findings += _module_findings(index, rel_path, direct_rule)
    return findings


def scan_decodes(sources: dict[str, str], *, direct_rule: bool = True) -> list[str]:
    """Every refused decode, unverified read, or PyJWT object use across modules.

    `direct_rule=False` leaves out only the rule that a decode sits in the helper
    or an allowed function, so a fixture can pin the call shape on its own.
    """
    return _index_findings(_Index(sources), direct_rule)


def decode_findings(
    source: str, rel_path: str = "<fixture>", *, direct_rule: bool = False
) -> list[str]:
    """`scan_decodes` for one module on its own."""
    return scan_decodes({rel_path: source}, direct_rule=direct_rule)


def _sites(index: "_Index", wanted: str, *, calls_only: bool = False) -> set[tuple[str, str]]:
    """(path, enclosing function) of every reference of kind `wanted`."""
    sites = set()
    for rel_path, tree in index.trees.items():
        names = index.names[rel_path]
        owners = _enclosing_functions(tree)
        for node in ast.walk(tree):
            if calls_only:
                if isinstance(node, ast.Call) and names.kind_of(node.func) == wanted:
                    sites.add((rel_path, owners.get(id(node), "<module>")))
            elif (
                not isinstance(node, (ast.Import, ast.ImportFrom))
                and not _is_store(node)
                and names.kind_of(node) == wanted
            ):
                sites.add((rel_path, owners.get(id(node), "<module>")))
    return sites


def unverified_read_sites(index: "_Index") -> set[tuple[str, str]]:
    return _sites(index, "unverified")


def direct_decode_counts(index: "_Index") -> dict[tuple[str, str], int]:
    """How many PyJWT decode calls each (path, qualified owner) holds."""
    counts: dict[tuple[str, str], int] = {}
    for rel_path, tree in index.trees.items():
        names = index.names[rel_path]
        owners = _enclosing_functions(tree)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and names.kind_of(node.func) == "decode":
                site = (rel_path, owners.get(id(node), "<module>"))
                counts[site] = counts.get(site, 0) + 1
    return counts


def helper_call_sites(index: "_Index") -> set[tuple[str, str]]:
    """(path, function) of every call of the shared helper, imported by name from
    `herd_common.jwt_codec` (under an alias too) or read as its attribute."""
    sites = set()
    for rel_path, tree in index.trees.items():
        local = {
            alias.asname or alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
            and node.module == "herd_common.jwt_codec"
            and node.level == 0
            for alias in node.names
            if alias.name == DECODE_HELPER[1]
        }
        owners = _enclosing_functions(tree)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if (isinstance(func, ast.Name) and func.id in local) or (
                isinstance(func, ast.Attribute) and func.attr == DECODE_HELPER[1]
            ):
                sites.add((rel_path, owners.get(id(node), "<module>")))
    return sites


@functools.cache
def _repository_sources() -> tuple[tuple[str, str], ...]:
    return tuple((str(path.relative_to(REPO)), path.read_text()) for path in scanned_files())


def repository_sources() -> dict[str, str]:
    return dict(_repository_sources())


@functools.cache
def repository_index() -> _Index:
    return _Index(repository_sources())


# --- settings ---

PYDANTIC_MODULES = (
    "pydantic",
    "pydantic.functional_validators",
    "pydantic.class_validators",
    "pydantic.v1",
    "pydantic.fields",
)
VALIDATOR_NAMES = ("field_validator", "validator")
TYPING_MODULES = ("typing", "typing_extensions")


class _SettingsNames:
    """The names one config.py binds to the shared types and module, to pydantic's
    `Field` and validator decorators, and to `Annotated`."""

    def __init__(self, tree: ast.AST):
        self.modules: set[str] = set()  # herd_common.auth_settings
        self.types: dict[str, str] = {}  # local name to AuthSecretKey or JwtAlgorithm
        self.pydantic: set[str] = set()  # the pydantic module
        self.typing: set[str] = set()  # typing or typing_extensions
        self.validators: set[str] = set(VALIDATOR_NAMES)
        self.fields: set[str] = {"Field"}
        self.annotated: set[str] = {"Annotated"}
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.level == 0:
                for alias in node.names:
                    bound = alias.asname or alias.name
                    if node.module == AUTH_SETTINGS_MODULE and alias.name in SHARED_TYPES:
                        self.types[bound] = alias.name
                    elif node.module == "herd_common" and alias.name == "auth_settings":
                        self.modules.add(bound)
                    elif node.module in PYDANTIC_MODULES and alias.name in VALIDATOR_NAMES:
                        self.validators.add(bound)
                    elif node.module in PYDANTIC_MODULES and alias.name == "Field":
                        self.fields.add(bound)
                    elif node.module in TYPING_MODULES and alias.name == "Annotated":
                        self.annotated.add(bound)
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == AUTH_SETTINGS_MODULE:
                        self.modules.add(alias.asname or AUTH_SETTINGS_MODULE)
                    elif alias.name in PYDANTIC_MODULES:
                        self.pydantic.add(alias.asname or alias.name)
                    elif alias.name in TYPING_MODULES:
                        self.typing.add(alias.asname or alias.name)
        # A validator decorator assigned to another name (`fv = field_validator`).
        changed = True
        while changed:
            changed = False
            for node in ast.walk(tree):
                if isinstance(node, ast.Assign) and self.is_validator(node.value):
                    for target in node.targets:
                        if isinstance(target, ast.Name) and target.id not in self.validators:
                            self.validators.add(target.id)
                            changed = True

    def is_validator(self, node: ast.AST) -> bool:
        if isinstance(node, ast.Name):
            return node.id in self.validators
        return isinstance(node, ast.Attribute) and node.attr in VALIDATOR_NAMES

    def is_field(self, node: ast.AST) -> bool:
        if isinstance(node, ast.Name):
            return node.id in self.fields
        return (
            isinstance(node, ast.Attribute)
            and node.attr == "Field"
            and ast.unparse(node.value) in self.pydantic | {"pydantic"}
        )

    def is_annotated(self, node: ast.AST) -> bool:
        if isinstance(node, ast.Name):
            return node.id in self.annotated
        return (
            isinstance(node, ast.Attribute)
            and node.attr == "Annotated"
            and ast.unparse(node.value) in self.typing | set(TYPING_MODULES)
        )

    def annotation(self, node: ast.AST) -> tuple[str | None, list[ast.expr]]:
        """(shared type the annotation names, `Annotated` metadata around it)."""
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            try:
                return self.annotation(ast.parse(node.value, mode="eval").body)
            except SyntaxError:
                return None, []
        if isinstance(node, ast.Name):
            return self.types.get(node.id), []
        if isinstance(node, ast.Attribute) and node.attr in SHARED_TYPES:
            if ast.unparse(node.value) in self.modules:
                return node.attr, []
            return None, []
        if (
            isinstance(node, ast.Subscript)
            and self.is_annotated(node.value)
            and isinstance(node.slice, ast.Tuple)
            and node.slice.elts
        ):
            inner, metadata = self.annotation(node.slice.elts[0])
            return inner, metadata + list(node.slice.elts[1:])
        return None, []

    def default_of(self, value: ast.AST | None) -> tuple[str, ast.AST | None]:
        """("none", None) for no default, ("value", node) for a default value, or
        ("other", node) for a default the scan does not evaluate (a factory, `**`).
        A `Field(...)` call with no positional default (or the Ellipsis) and no
        `default=` or `default_factory=` keyword is not a default."""
        if value is None:
            return "none", None
        if isinstance(value, ast.Call) and self.is_field(value.func):
            for kw in value.keywords:
                if kw.arg is None or kw.arg == "default_factory":
                    return "other", value
                if kw.arg == "default":
                    return "value", kw.value
            if value.args:
                first = value.args[0]
                if isinstance(first, ast.Starred):
                    return "other", value
                if isinstance(first, ast.Constant) and first.value is Ellipsis:
                    return "none", None
                return "value", first
            return "none", None
        return "value", value


def _rebinding_findings(tree: ast.AST, names: _SettingsNames, rel_path: str) -> list[str]:
    """A binding of a shared type's local name, or of the module's, that is not
    the import from herd_common.auth_settings, anywhere in the file."""
    watched = set(names.types) | {m.split(".")[0] for m in names.modules}
    findings = []
    for node in ast.walk(tree):
        bound: list[str] = []
        if _is_store(node):
            bound = [node.id]
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            bound = [node.name]
        elif isinstance(node, ast.ExceptHandler) and node.name:
            bound = [node.name]
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.asname and alias.name != AUTH_SETTINGS_MODULE:
                    bound.append(alias.asname)
                elif not alias.asname and alias.name.split(".")[0] != "herd_common":
                    bound.append(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                shared = (node.module == AUTH_SETTINGS_MODULE and alias.name in SHARED_TYPES) or (
                    node.module == "herd_common" and alias.name == "auth_settings"
                )
                if not shared:
                    bound.append(alias.asname or alias.name)
        for name in bound:
            if name in watched:
                findings.append(f"{rel_path}:{node.lineno} rebinds {name}")
    return findings


def _validator_fields(stmt: ast.stmt, names: _SettingsNames) -> set[str]:
    fields: set[str] = set()
    if not isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return fields
    for decorator in stmt.decorator_list:
        if isinstance(decorator, ast.Call) and names.is_validator(decorator.func):
            for arg in decorator.args:
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    if arg.value == "*":
                        fields |= {"secret_key", "algorithm"}
                    else:
                        fields.add(arg.value.lower())
    return fields


def _assigned_names(stmt: ast.stmt) -> list[str]:
    if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
        return [stmt.target.id]
    if isinstance(stmt, ast.Assign):
        return [t.id for t in stmt.targets if isinstance(t, ast.Name)]
    return []


def _metadata_problem(element: ast.expr, names: _SettingsNames) -> bool:
    """True unless an `Annotated` element is a string or a Field with no default."""
    if isinstance(element, ast.Constant) and isinstance(element.value, str):
        return False
    if isinstance(element, ast.Call) and names.is_field(element.func):
        return names.default_of(element)[0] != "none"
    return True


def settings_findings(source: str, rel_path: str = "<fixture>") -> list[str]:
    tree = ast.parse(source)
    names = _SettingsNames(tree)
    findings: list[str] = []
    declares_secret = False
    for cls in (n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)):
        declared: dict[str, list[ast.stmt]] = {}
        validated: set[str] = set()
        for stmt in cls.body:
            for name in _assigned_names(stmt):
                declared.setdefault(name.lower(), []).append(stmt)
            validated |= _validator_fields(stmt, names)
        if "secret_key" not in declared:
            continue
        declares_secret = True
        where = f"{rel_path} class {cls.name}"
        for field, wanted in (("secret_key", "AuthSecretKey"), ("algorithm", "JwtAlgorithm")):
            stmts = declared.get(field, [])
            if not stmts:
                findings.append(f"{where}: {field} is not declared")
                continue
            if len(stmts) > 1:
                findings.append(f"{where}: {field} is declared {len(stmts)} times")
            stmt = stmts[0]
            if not isinstance(stmt, ast.AnnAssign):
                findings.append(f"{where}: {field} has no annotation")
                continue
            annotated, metadata = names.annotation(stmt.annotation)
            if annotated != wanted:
                findings.append(f"{where}: {field} is not annotated {wanted}")
            if any(_metadata_problem(element, names) for element in metadata):
                findings.append(
                    f"{where}: {field} Annotated metadata is not a Field without a default"
                )
            kind, default = names.default_of(stmt.value)
            if field == "secret_key" and kind != "none":
                findings.append(f"{where}: secret_key has a default")
            if field == "algorithm" and kind != "none":
                if not (
                    kind == "value"
                    and isinstance(default, ast.Constant)
                    and default.value in ALLOWED_ALGORITHMS
                ):
                    findings.append(f"{where}: algorithm default is not HS256, HS384, or HS512")
            if field in validated:
                findings.append(f"{where}: {field} is named by a validator decorator")
    if declares_secret:
        findings += _rebinding_findings(tree, names, rel_path)
    return findings


def settings_modules() -> list[Path]:
    return sorted((REPO / "services").glob("*/app/config.py"))


# --- the repository ---


def test_the_only_direct_decodes_are_the_helper_and_the_allowed_table():
    # Also the stale check: every allowed entry, and the helper, is a real site.
    assert direct_decode_counts(repository_index()) == direct_decode_allowed()
    assert all(count >= 1 and reason.strip() for count, reason in DIRECT_DECODE_ALLOWED.values())


def test_every_token_verifying_function_calls_the_shared_helper():
    assert HELPER_CALL_SITES <= helper_call_sites(repository_index())


def test_the_index_keys_every_scanned_file():
    index = repository_index()
    assert all(key is not None for key in index.keys.values())
    assert index.keys["services/common/herd_common/auth.py"] == "herd_common.auth"
    assert index.keys["services/common/herd_common/__init__.py"] == "herd_common"
    assert index.keys["services/auth/app/utils/jwt.py"] == "auth:app.utils.jwt"


def test_every_decode_goes_through_the_helper_with_one_algorithm():
    assert _index_findings(repository_index()) == []


def test_unverified_read_allowlist_has_no_stale_entries():
    sites = unverified_read_sites(repository_index())
    assert set(UNVERIFIED_READ_ALLOWED) <= sites
    assert all(reason.strip() for reason in UNVERIFIED_READ_ALLOWED.values())


def test_settings_modules_are_discovered():
    declaring = {
        p.parts[-3]
        for p in settings_modules()
        if any(
            name.lower() == "secret_key"
            for cls in ast.walk(ast.parse(p.read_text()))
            if isinstance(cls, ast.ClassDef)
            for stmt in cls.body
            for name in _assigned_names(stmt)
        )
    }
    assert declaring == {
        "acl",
        "ai-orchestrator",
        "auth",
        "cabling",
        "execution",
        "integration",
        "inventory",
        "notifications",
        "reservations",
        "secrets",
        "user-profile",
    }


def test_every_settings_model_uses_the_shared_types():
    findings = []
    for path in settings_modules():
        findings += settings_findings(path.read_text(), str(path.relative_to(REPO)))
    assert findings == []


def test_allowed_algorithms_match_the_shared_module():
    from herd_common.auth_settings import ALLOWED_JWT_ALGORITHMS

    assert ALLOWED_JWT_ALGORITHMS == ALLOWED_ALGORITHMS


# --- fixture cases: decode ---

_GOOD_DECODES = [
    "import jwt\njwt.decode(t, k, algorithms=[alg])\n",
    "import jwt as j\nj.decode(t, k, algorithms=['HS256'])\n",
    "import jwt\njwt.decode_complete(t, k, algorithms=[alg])\n",
    "import jwt.api_jwt\njwt.api_jwt.decode(t, k, algorithms=[alg])\n",
    "from jwt import api_jws\napi_jws.decode(t, k, algorithms=[alg])\n",
    "from jwt import decode\ndecode(t, k, algorithms=[alg])\n",
    "from jwt.api_jwt import decode_complete as dc\ndc(t, k, algorithms=[alg])\n",
    "import jwt\njwt.decode(t, k, algorithms=[alg], leeway=10)\n",
    "import jwt\njwt.decode(t, k, algorithms=[alg], options={'verify_exp': False})\n",
    "import jwt\njwt.decode(t, k, algorithms=[a], options={'verify_signature': True})\n",
    "import jwt\nd = jwt.decode\nd(t, k, algorithms=[alg])\n",
    "import jwt\njwt.PyJWT().decode(t, k, algorithms=[alg])\n",
    "from jwt import PyJWS\ncodec = PyJWS()\ncodec.decode(t, k, algorithms=[alg])\n",
    "# import jwt; jwt.decode(t, k)\nx = 1\n",
    "import jwt\njwt.encode(c, k, algorithm=alg)\n",
    # Reading other attributes of PyJWT is fine.
    "import jwt\ntry:\n    pass\nexcept jwt.PyJWTError:\n    pass\n",
    "import jwt\ntry:\n    pass\nexcept jwt.exceptions.InvalidSubjectError:\n    pass\n",
    "from jwt.exceptions import InvalidTokenError\nx = InvalidTokenError\n",
    "from jwt.warnings import InsecureKeyLengthWarning\nx = InsecureKeyLengthWarning\n",
    "import jwt\ngetattr(jwt, 'encode')(c, k, algorithm=alg)\n",
    "import jwt\ncodec = jwt\ncodec.decode(t, k, algorithms=[alg])\n",
]


@pytest.mark.parametrize("source", _GOOD_DECODES)
def test_fixture_decodes_that_pass(source):
    assert decode_findings(source) == []


_BAD_DECODES = {
    "no-algorithms": "import jwt\njwt.decode(t, k)\n",
    "two-algorithms": "import jwt\njwt.decode(t, k, algorithms=['HS256', 'none'])\n",
    "empty-list": "import jwt\njwt.decode(t, k, algorithms=[])\n",
    "name-not-list": "import jwt\njwt.decode(t, k, algorithms=ALGS)\n",
    "tuple": "import jwt\njwt.decode(t, k, algorithms=(alg,))\n",
    "starred": "import jwt\njwt.decode(t, k, algorithms=[*algs])\n",
    "positional-algorithms": "import jwt\njwt.decode(t, k, [alg])\n",
    "star-args": "import jwt\njwt.decode(*args, algorithms=[alg])\n",
    "star-kwargs": "import jwt\njwt.decode(t, k, **kw)\n",
    "verify-off": (
        "import jwt\njwt.decode(t, k, algorithms=[alg], options={'verify_signature': False})\n"
    ),
    "verify-off-api-jwt": (
        "import jwt\n"
        "jwt.api_jwt.decode(t, k, algorithms=[alg], options={'verify_signature': False})\n"
    ),
    "verify-name": (
        "import jwt\njwt.decode(t, k, algorithms=[alg], options={'verify_signature': v})\n"
    ),
    "options-name": "import jwt\njwt.decode(t, k, algorithms=[alg], options=opts)\n",
    "options-splat": "import jwt\njwt.decode(t, k, algorithms=[alg], options={**o})\n",
    # The spelling cases: the call is found under each spelling, so the missing
    # algorithms= is reported.
    "alias-package": "import jwt as j\nj.decode(t, k)\n",
    "decode-complete": "import jwt\njwt.decode_complete(t, k)\n",
    "api-jwt": "import jwt.api_jwt\njwt.api_jwt.decode(t, k)\n",
    "api-jwt-alias": "import jwt.api_jwt as A\nA.decode(t, k)\n",
    "api-jwt-from": "from jwt import api_jwt\napi_jwt.decode_complete(t, k)\n",
    "api-jws": "import jwt\njwt.api_jws.decode(t, k)\n",
    "api-jws-alias": "import jwt.api_jws as S\nS.decode_complete(t, k)\n",
    "from-decode": "from jwt import decode\ndecode(t, k)\n",
    "from-decode-alias": "from jwt import decode as d\nd(t, k)\n",
    "from-api-jwt-decode": "from jwt.api_jwt import decode\ndecode(t, k)\n",
    "from-api-jws-decode-complete": (
        "from jwt.api_jws import decode_complete\ndecode_complete(t, k)\n"
    ),
    "pyjwt-instance": "import jwt\njwt.PyJWT().decode(t, k)\n",
    "pyjwt-instance-bound": "from jwt import PyJWT\ncodec = PyJWT()\ncodec.decode_complete(t, k)\n",
    "pyjwt-from-api-jwt": "from jwt.api_jwt import PyJWT\nPyJWT().decode(t, k)\n",
    "pyjwt-of-api-jwt": "import jwt\njwt.api_jwt.PyJWT().decode(t, k)\n",
    "pyjws-instance": "import jwt\njwt.PyJWS().decode(t, k)\n",
    "pyjws-of-api-jws": "import jwt.api_jws as S\nS.PyJWS().decode_complete(t, k)\n",
    "assigned-function": "import jwt\nd = jwt.decode\nd(t, k)\n",
    "assigned-module": "import jwt\ncodec = jwt\ncodec.decode(t, k)\n",
    "assigned-twice": "import jwt\na = jwt\nb = a.decode\nb(t, k)\n",
    "assigned-class": "import jwt\nC = jwt.PyJWT\nC().decode(t, k)\n",
    "getattr": "import jwt\ngetattr(jwt, 'decode')(t, k)\n",
    "getattr-instance": "import jwt\ngetattr(jwt.PyJWT(), 'decode')(t, k)\n",
    "in-function": "import jwt\ndef f(t):\n    return jwt.decode(t, k)\n",
    "constructed-with-options": (
        "import jwt\njwt.PyJWT(options={'verify_signature': False}).decode(t, k, algorithms=[a])\n"
    ),
    "pyjws-constructed-with-arguments": (
        "from jwt import PyJWS\nPyJWS(['HS256']).decode(t, k, algorithms=[a])\n"
    ),
    "partial": "import functools\nimport jwt\nf = functools.partial(jwt.decode, algorithms=[a])\n",
    "passed-on": "import jwt\nrun(jwt.decode, t, k)\n",
    "unverified-header": "import jwt\ndef f(t):\n    return jwt.get_unverified_header(t)\n",
    "unverified-api-jws": (
        "import jwt\ndef f(t):\n    return jwt.api_jws.get_unverified_header(t)\n"
    ),
    "unverified-pyjws": "import jwt\ndef f(t):\n    return jwt.PyJWS().get_unverified_header(t)\n",
    "unverified-by-name": (
        "from jwt import get_unverified_header as g\ndef f(t):\n    return g(t)\n"
    ),
    "unverified-module-level": "import jwt\nx = jwt.get_unverified_header(t)\n",
    "register-algorithm": "import jwt\njwt.register_algorithm('HS256', alg)\n",
    "unregister-by-name": (
        "from jwt.api_jws import unregister_algorithm\nunregister_algorithm('HS256')\n"
    ),
    "register-on-pyjws": "import jwt\ndef f():\n    jwt.PyJWS().register_algorithm('HS256', a)\n",
    "star-jwt": "from jwt import *\ndecode(t, k)\n",
    "star-api-jwt": "from jwt.api_jwt import *\ndecode(t, k)\n",
    "star-api-jws": "from jwt.api_jws import *\nget_unverified_header(t)\n",
    "star-exceptions": "from jwt.exceptions import *\n",
    "private-attribute": "import jwt\njwt.api_jwt._jwt_global_obj.decode(t, k, algorithms=[a])\n",
    "private-import": "from jwt.api_jwt import _jwt_global_obj\n_jwt_global_obj.decode(t, k)\n",
    "private-method": "import jwt\njwt.PyJWS()._verify_signature(t, h, s, k, a)\n",
    "dunder-dict": "import jwt\njwt.__dict__['decode'](t, k)\n",
    "getattr-dunder": "import jwt\ngetattr(jwt, '__dict__')['decode'](t, k)\n",
    "attribute-assigned": "import jwt\njwt.decode = fake\n",
    "instance-options-assigned": "import jwt\ncodec = jwt.PyJWT()\ncodec.options = {}\n",
    "attribute-deleted": "import jwt\ndel jwt.api_jwt.decode\n",
    "module-handed-on": "import jwt\nrun(jwt)\n",
    "module-in-vars": "import jwt\nvars(jwt)['decode'](t, k)\n",
    "module-in-tuple": "import jwt\nfor m in (jwt,):\n    m.decode(t, k)\n",
    "submodule-handed-on": "import jwt\nrun(jwt.api_jwt)\n",
    "class-handed-on": "import jwt\nrun(jwt.PyJWT)\n",
    "instance-handed-on": "import jwt\nrun(jwt.PyJWT())\n",
    "setattr": "import jwt\nsetattr(jwt, 'decode', fake)\n",
    "getattr-not-literal": "import jwt\ngetattr(jwt, name)(t, k)\n",
    "class-attribute-alias": "import jwt\nclass V:\n    d = jwt.decode\nV.d(t, k)\n",
    "instance-attribute-alias": (
        "import jwt\nclass V:\n    def __init__(self):\n        self.d = jwt.decode\n"
    ),
    "decode-in-dict": "import jwt\ntable = {'d': jwt.decode}\n",
}


_NO_ALGORITHMS = "names no algorithms= keyword"
_NOT_ONE = "algorithms= is not a one-element list literal"
_VERIFY_OFF = "options= turns verify_signature off"
_NOT_CALLED = "jwt decode referenced, not called"
_REFERENCED = "jwt object referenced other than by attribute"
_REGISTRY = "jwt algorithm registry used in"
_EXPECTED_DECODE_REASON = {
    "no-algorithms": _NO_ALGORITHMS,
    "two-algorithms": _NOT_ONE,
    "empty-list": _NOT_ONE,
    "name-not-list": _NOT_ONE,
    "tuple": _NOT_ONE,
    "starred": _NOT_ONE,
    "positional-algorithms": "passes more than the token and the key positionally",
    "star-args": "passes *args",
    "star-kwargs": "passes **kwargs",
    "verify-off": _VERIFY_OFF,
    "verify-off-api-jwt": _VERIFY_OFF,
    "verify-name": _VERIFY_OFF,
    "options-name": "options= is not a dict literal without **",
    "options-splat": "options= is not a dict literal without **",
    "alias-package": _NO_ALGORITHMS,
    "decode-complete": _NO_ALGORITHMS,
    "api-jwt": _NO_ALGORITHMS,
    "api-jwt-alias": _NO_ALGORITHMS,
    "api-jwt-from": _NO_ALGORITHMS,
    "api-jws": _NO_ALGORITHMS,
    "api-jws-alias": _NO_ALGORITHMS,
    "from-decode": _NO_ALGORITHMS,
    "from-decode-alias": _NO_ALGORITHMS,
    "from-api-jwt-decode": _NO_ALGORITHMS,
    "from-api-jws-decode-complete": _NO_ALGORITHMS,
    "pyjwt-instance": _NO_ALGORITHMS,
    "pyjwt-instance-bound": _NO_ALGORITHMS,
    "pyjwt-from-api-jwt": _NO_ALGORITHMS,
    "pyjwt-of-api-jwt": _NO_ALGORITHMS,
    "pyjws-instance": _NO_ALGORITHMS,
    "pyjws-of-api-jws": _NO_ALGORITHMS,
    "assigned-function": _NO_ALGORITHMS,
    "assigned-module": _NO_ALGORITHMS,
    "assigned-twice": _NO_ALGORITHMS,
    "assigned-class": _NO_ALGORITHMS,
    "getattr": _NO_ALGORITHMS,
    "getattr-instance": _NO_ALGORITHMS,
    "in-function": _NO_ALGORITHMS,
    "constructed-with-options": "constructs PyJWT or PyJWS with arguments",
    "pyjws-constructed-with-arguments": "constructs PyJWT or PyJWS with arguments",
    "partial": _NOT_CALLED,
    "passed-on": _NOT_CALLED,
    "unverified-header": "jwt unverified read in f",
    "unverified-api-jws": "jwt unverified read in f",
    "unverified-pyjws": "jwt unverified read in f",
    "unverified-by-name": "jwt unverified read in f",
    "unverified-module-level": "jwt unverified read in <module>",
    "register-algorithm": f"{_REGISTRY} <module>",
    "unregister-by-name": f"{_REGISTRY} <module>",
    "register-on-pyjws": f"{_REGISTRY} f",
    "star-jwt": "star import from jwt",
    "star-api-jwt": "star import from jwt.api_jwt",
    "star-api-jws": "star import from jwt.api_jws",
    "star-exceptions": "star import from jwt.exceptions",
    "private-attribute": "jwt private attribute _jwt_global_obj",
    "private-import": "private name _jwt_global_obj imported from jwt.api_jwt",
    "private-method": "jwt private attribute _verify_signature",
    "dunder-dict": "jwt private attribute __dict__",
    "getattr-dunder": "jwt private attribute __dict__",
    "attribute-assigned": "jwt attribute assigned decode",
    "instance-options-assigned": "jwt attribute assigned options",
    "attribute-deleted": "jwt attribute assigned decode",
    "module-handed-on": _REFERENCED,
    "module-in-vars": _REFERENCED,
    "module-in-tuple": _REFERENCED,
    "submodule-handed-on": _REFERENCED,
    "class-handed-on": _REFERENCED,
    "instance-handed-on": _REFERENCED,
    "setattr": _REFERENCED,
    "getattr-not-literal": _REFERENCED,
    "class-attribute-alias": _NOT_CALLED,
    "instance-attribute-alias": _NOT_CALLED,
    "decode-in-dict": _NOT_CALLED,
}


def test_every_bad_decode_fixture_names_its_reason():
    assert set(_EXPECTED_DECODE_REASON) == set(_BAD_DECODES)


@pytest.mark.parametrize("name", sorted(_BAD_DECODES))
def test_fixture_decodes_that_fail(name):
    findings = decode_findings(_BAD_DECODES[name])
    assert any(_EXPECTED_DECODE_REASON[name] in f for f in findings), findings


def test_unverified_read_allowlist_admits_only_its_function(monkeypatch):
    source = (
        "import jwt\n"
        "def peek(t):\n    return jwt.get_unverified_header(t)\n"
        "def other(t):\n    return jwt.get_unverified_header(t)\n"
    )
    monkeypatch.setitem(UNVERIFIED_READ_ALLOWED, ("x.py", "peek"), "fixture")
    findings = decode_findings(source, "x.py")
    assert len(findings) == 1 and "in other" in findings[0]


# --- fixture cases: the direct-decode rule ---

_HELPER_PATH, _HELPER_FUNCTION = DECODE_HELPER
_CONFIG_PATH = "services/config/app/auth.py"
_HONEST_CALL = "jwt.decode(t, k, algorithms=[alg])"

_GOOD_DIRECT = {
    "the-helper": (
        _HELPER_PATH,
        f"import jwt\ndef {_HELPER_FUNCTION}(t, k, alg):\n    return {_HONEST_CALL}\n",
    ),
    "the-config-session": (
        _CONFIG_PATH,
        f"import jwt\ndef _verify_token(t):\n    return {_HONEST_CALL}\n",
    ),
    "a-helper-caller": (
        "services/svc/app/a.py",
        "from herd_common.jwt_codec import decode_access_token\n"
        "def f(t):\n    return decode_access_token(t, k, alg)\n",
    ),
}


@pytest.mark.parametrize("name", sorted(_GOOD_DIRECT))
def test_fixture_direct_decodes_that_pass(name):
    rel_path, source = _GOOD_DIRECT[name]
    assert decode_findings(source, rel_path, direct_rule=True) == []


_BAD_DIRECT = {
    "app-function": (
        "services/svc/app/a.py",
        f"import jwt\ndef f(t):\n    return {_HONEST_CALL}\n",
    ),
    "module-level": ("services/svc/app/a.py", f"import jwt\nx = {_HONEST_CALL}\n"),
    "herd-common-module": (
        "services/common/herd_common/auth.py",
        f"import jwt\ndef get_current_user_payload(t):\n    return {_HONEST_CALL}\n",
    ),
    "helper-file-other-function": (
        _HELPER_PATH,
        f"import jwt\ndef peek(t, k, alg):\n    return {_HONEST_CALL}\n",
    ),
    "config-other-function": (
        _CONFIG_PATH,
        f"import jwt\ndef require_config_session(t):\n    return {_HONEST_CALL}\n",
    ),
    "helper-name-in-another-file": (
        "services/svc/app/a.py",
        f"import jwt\ndef {_HELPER_FUNCTION}(t, k, alg):\n    return {_HONEST_CALL}\n",
    ),
    "pyjwt-instance": (
        "services/svc/app/a.py",
        "import jwt\ndef f(t):\n    return jwt.PyJWT().decode(t, k, algorithms=[alg])\n",
    ),
    # An allowed name reused by a nested function or a method is another site.
    "config-nested-def": (
        _CONFIG_PATH,
        "import jwt\ndef other(t):\n    def _verify_token(t):\n"
        f"        return {_HONEST_CALL}\n    return _verify_token(t)\n",
    ),
    "config-class-method": (
        _CONFIG_PATH,
        "import jwt\nclass Session:\n    def _verify_token(self, t):\n"
        f"        return {_HONEST_CALL}\n",
    ),
    "helper-file-class-method": (
        _HELPER_PATH,
        "import jwt\nclass Codec:\n"
        f"    def {_HELPER_FUNCTION}(self, t, k, alg):\n"
        "        return jwt.decode(t, k, algorithms=[alg], options={'verify_exp': False})\n",
    ),
    "helper-nested-def": (
        _HELPER_PATH,
        f"import jwt\ndef {_HELPER_FUNCTION}(t, k, alg):\n    def inner(t):\n"
        f"        return {_HONEST_CALL}\n    return inner(t)\n",
    ),
}

_EXPECTED_DIRECT = {
    "app-function": "f",
    "module-level": "<module>",
    "herd-common-module": "get_current_user_payload",
    "helper-file-other-function": "peek",
    "config-other-function": "require_config_session",
    "helper-name-in-another-file": _HELPER_FUNCTION,
    "pyjwt-instance": "f",
    "config-nested-def": "other._verify_token",
    "config-class-method": "Session._verify_token",
    "helper-file-class-method": f"Codec.{_HELPER_FUNCTION}",
    "helper-nested-def": f"{_HELPER_FUNCTION}.inner",
}


def test_every_bad_direct_fixture_names_its_function():
    assert set(_EXPECTED_DIRECT) == set(_BAD_DIRECT)


@pytest.mark.parametrize("name", sorted(_BAD_DIRECT))
def test_fixture_direct_decodes_that_fail(name):
    rel_path, source = _BAD_DIRECT[name]
    findings = decode_findings(source, rel_path, direct_rule=True)
    line = next(n for n, text in enumerate(source.splitlines(), 1) if ".decode(t" in text)
    assert findings == [
        f"{rel_path}:{line} jwt decode called directly in {_EXPECTED_DIRECT[name]}; "
        "call herd_common.jwt_codec.decode_access_token"
    ], findings


@pytest.mark.parametrize(
    "rel_path, function",
    [(_HELPER_PATH, _HELPER_FUNCTION), (_CONFIG_PATH, "_verify_token")],
    ids=["the-helper", "the-config-session"],
)
def test_an_allowed_site_holding_a_second_decode_is_refused(rel_path, function):
    source = (
        f"import jwt\ndef {function}(t, k, alg):\n"
        f"    if t:\n        return {_HONEST_CALL}\n    return {_HONEST_CALL}\n"
    )
    assert decode_findings(source, rel_path, direct_rule=True) == [
        f"{rel_path} jwt decode called 2 times in {function}; the allowlist admits 1"
    ]


def test_direct_decode_table_admits_only_its_function(monkeypatch):
    source = (
        f"import jwt\ndef a(t):\n    return {_HONEST_CALL}\ndef b(t):\n    return {_HONEST_CALL}\n"
    )
    monkeypatch.setitem(DIRECT_DECODE_ALLOWED, ("services/svc/app/x.py", "a"), (1, "fixture"))
    findings = decode_findings(source, "services/svc/app/x.py", direct_rule=True)
    assert len(findings) == 1 and "called directly in b;" in findings[0]


def test_helper_call_sites_follow_an_import_alias():
    index = _Index(
        {
            "services/svc/app/a.py": (
                "from herd_common.jwt_codec import decode_access_token as dat\n"
                "def f(t):\n    return dat(t, k, alg)\n"
            ),
            "services/svc/app/b.py": (
                "from herd_common import jwt_codec\n"
                "def g(t):\n    return jwt_codec.decode_access_token(t, k, alg)\n"
            ),
            "services/svc/app/c.py": "def decode_access_token(t):\n    return t\nx = 1\n",
        }
    )
    assert helper_call_sites(index) == {
        ("services/svc/app/a.py", "f"),
        ("services/svc/app/b.py", "g"),
    }


# --- fixture cases: decodes across modules ---

_COMMON = "services/common/herd_common"
_APP = "services/svc/app"

_REEXPORT = {f"{_COMMON}/auth.py": "import jwt\n"}

_GOOD_ACROSS_MODULES = {
    "reexported-module-honest": {
        **_REEXPORT,
        f"{_APP}/a.py": "from herd_common.auth import jwt\njwt.decode(t, k, algorithms=[alg])\n",
    },
    "other-name-from-the-module": {
        **_REEXPORT,
        f"{_APP}/a.py": "from herd_common.auth import make_auth_dependencies\nx = 1\n",
    },
    # Each service has its own `app` package: another service's binding is not
    # this one's.
    "app-is-per-service": {
        "services/other/app/codec.py": "import jwt\n",
        f"{_APP}/codec.py": "x = 1\n",
        f"{_APP}/b.py": "from app.codec import jwt\njwt.decode(t, k)\n",
    },
}


@pytest.mark.parametrize("name", sorted(_GOOD_ACROSS_MODULES))
def test_fixture_decodes_across_modules_that_pass(name):
    assert scan_decodes(_GOOD_ACROSS_MODULES[name], direct_rule=False) == []


_BAD_ACROSS_MODULES = {
    "reexported-module": {
        **_REEXPORT,
        f"{_APP}/a.py": "from herd_common.auth import jwt\njwt.decode(t, k)\n",
    },
    "reexported-module-alias": {
        **_REEXPORT,
        f"{_APP}/a.py": "from herd_common.auth import jwt as j\nj.decode(t, k)\n",
    },
    "module-attribute": {
        **_REEXPORT,
        f"{_APP}/a.py": "import herd_common.auth\nherd_common.auth.jwt.decode(t, k)\n",
    },
    "module-alias-attribute": {
        **_REEXPORT,
        f"{_APP}/a.py": "import herd_common.auth as ha\nha.jwt.decode(t, k)\n",
    },
    "package-attribute": {
        **_REEXPORT,
        f"{_APP}/a.py": "from herd_common import auth\nauth.jwt.decode(t, k)\n",
    },
    "getattr-on-module": {
        **_REEXPORT,
        f"{_APP}/a.py": "from herd_common import auth\ngetattr(auth, 'jwt').decode(t, k)\n",
    },
    "alias-defined-elsewhere": {
        f"{_COMMON}/codec.py": "import jwt\nd = jwt.decode\n",
        f"{_APP}/a.py": "from herd_common.codec import d\nd(t, k)\n",
    },
    "instance-defined-elsewhere": {
        f"{_COMMON}/codec.py": "import jwt\ncodec = jwt.PyJWT()\n",
        f"{_APP}/a.py": "from herd_common.codec import codec\ncodec.decode(t, k)\n",
    },
    "star-from-scanned": {
        **_REEXPORT,
        f"{_APP}/a.py": "from herd_common.auth import *\njwt.decode(t, k)\n",
    },
    "two-hops": {
        f"{_COMMON}/a.py": "import jwt\n",
        f"{_COMMON}/b.py": "from herd_common.a import jwt\n",
        f"{_APP}/c.py": "from herd_common.b import jwt\njwt.decode(t, k)\n",
    },
    "app-absolute": {
        f"{_APP}/codec.py": "import jwt\n",
        f"{_APP}/b.py": "from app.codec import jwt\njwt.decode(t, k)\n",
    },
    "relative-import": {
        f"{_APP}/codec.py": "import jwt\n",
        f"{_APP}/sub/__init__.py": "",
        f"{_APP}/sub/b.py": "from ..codec import jwt\njwt.decode(t, k)\n",
    },
    "relative-from-package": {
        f"{_APP}/__init__.py": "",
        f"{_APP}/codec.py": "import jwt\n",
        f"{_APP}/b.py": "from . import codec\ncodec.jwt.decode(t, k)\n",
    },
    "reexported-unverified": {
        **_REEXPORT,
        f"{_APP}/a.py": (
            "from herd_common.auth import jwt\ndef f(t):\n    return jwt.get_unverified_header(t)\n"
        ),
    },
    "reexported-module-handed-on": {
        **_REEXPORT,
        f"{_APP}/a.py": "from herd_common.auth import jwt\nvars(jwt)\n",
    },
    "reexported-direct-decode": {
        **_REEXPORT,
        f"{_APP}/a.py": (
            "from herd_common.auth import jwt\ndef f(t):\n"
            "    return jwt.decode(t, k, algorithms=[alg])\n"
        ),
    },
}

_EXPECTED_ACROSS_MODULES = {
    "reexported-module": (f"{_APP}/a.py", _NO_ALGORITHMS),
    "reexported-module-alias": (f"{_APP}/a.py", _NO_ALGORITHMS),
    "module-attribute": (f"{_APP}/a.py", _NO_ALGORITHMS),
    "module-alias-attribute": (f"{_APP}/a.py", _NO_ALGORITHMS),
    "package-attribute": (f"{_APP}/a.py", _NO_ALGORITHMS),
    "getattr-on-module": (f"{_APP}/a.py", _NO_ALGORITHMS),
    "alias-defined-elsewhere": (f"{_APP}/a.py", _NO_ALGORITHMS),
    "instance-defined-elsewhere": (f"{_APP}/a.py", _NO_ALGORITHMS),
    "star-from-scanned": (f"{_APP}/a.py", _NO_ALGORITHMS),
    "two-hops": (f"{_APP}/c.py", _NO_ALGORITHMS),
    "app-absolute": (f"{_APP}/b.py", _NO_ALGORITHMS),
    "relative-import": (f"{_APP}/sub/b.py", _NO_ALGORITHMS),
    "relative-from-package": (f"{_APP}/b.py", _NO_ALGORITHMS),
    "reexported-unverified": (f"{_APP}/a.py", "jwt unverified read in f"),
    "reexported-module-handed-on": (f"{_APP}/a.py", _REFERENCED),
    "reexported-direct-decode": (f"{_APP}/a.py", "jwt decode called directly in f;"),
}


def test_every_bad_across_modules_fixture_names_its_reason():
    assert set(_EXPECTED_ACROSS_MODULES) == set(_BAD_ACROSS_MODULES)


@pytest.mark.parametrize("name", sorted(_BAD_ACROSS_MODULES))
def test_fixture_decodes_across_modules_that_fail(name):
    findings = scan_decodes(_BAD_ACROSS_MODULES[name])
    where, reason = _EXPECTED_ACROSS_MODULES[name]
    assert any(f.startswith(f"{where}:") and reason in f for f in findings), findings


# --- fixture cases: settings ---

_SETTINGS_HEADER = (
    "from herd_common.auth_settings import AuthSecretKey, JwtAlgorithm\n"
    "from herd_common.base_settings import HerdBaseSettings\n"
)

_GOOD_SETTINGS = [
    _SETTINGS_HEADER
    + "class Settings(HerdBaseSettings):\n"
    + "    secret_key: AuthSecretKey\n    algorithm: JwtAlgorithm = 'HS256'\n",
    _SETTINGS_HEADER
    + "class Settings(HerdBaseSettings):\n"
    + "    secret_key: AuthSecretKey\n    algorithm: JwtAlgorithm\n",
    "from herd_common.auth_settings import AuthSecretKey as K, JwtAlgorithm as A\n"
    "class Settings:\n    secret_key: K\n    algorithm: A = 'HS512'\n",
    "from herd_common import auth_settings\n"
    "class Settings:\n    secret_key: auth_settings.AuthSecretKey\n"
    "    algorithm: auth_settings.JwtAlgorithm = 'HS384'\n",
    "import herd_common.auth_settings\n"
    "class Settings:\n    secret_key: herd_common.auth_settings.AuthSecretKey\n"
    "    algorithm: herd_common.auth_settings.JwtAlgorithm = 'HS256'\n",
    # No secret_key: nothing to check.
    "class Settings:\n    database_url: str\n",
    # A Field with no default is not a default.
    _SETTINGS_HEADER
    + "from pydantic import Field\n"
    + "class Settings:\n    secret_key: AuthSecretKey = Field(...)\n"
    + "    algorithm: JwtAlgorithm = Field('HS512')\n",
    _SETTINGS_HEADER
    + "from pydantic import Field as F\n"
    + "class Settings:\n    secret_key: AuthSecretKey = F(description='signing key')\n"
    + "    algorithm: JwtAlgorithm = F(default='HS384')\n",
    _SETTINGS_HEADER
    + "import pydantic\n"
    + "class Settings:\n    secret_key: AuthSecretKey = pydantic.Field(..., description='x')\n"
    + "    algorithm: JwtAlgorithm\n",
    _SETTINGS_HEADER
    + "from typing import Annotated\nfrom pydantic import Field\n"
    + "class Settings:\n    secret_key: Annotated[AuthSecretKey, Field(description='x')]\n"
    + "    algorithm: Annotated[JwtAlgorithm, 'doc'] = 'HS256'\n",
    _SETTINGS_HEADER
    + "class Settings:\n    secret_key: 'AuthSecretKey'\n    algorithm: 'JwtAlgorithm' = 'HS256'\n",
    _SETTINGS_HEADER
    + "import typing\n"
    + "class Settings:\n    secret_key: typing.Annotated[AuthSecretKey, 'doc']\n"
    + "    algorithm: JwtAlgorithm = 'HS256'\n",
    # A validator on another field, under an alias, is fine.
    _SETTINGS_HEADER
    + "from pydantic import field_validator as fv\n"
    + "class Settings:\n    secret_key: AuthSecretKey\n    algorithm: JwtAlgorithm = 'HS256'\n"
    + "    nats: str = ''\n"
    + "    @fv('nats')\n    @classmethod\n    def _n(cls, v):\n        return v\n",
]


@pytest.mark.parametrize("source", _GOOD_SETTINGS)
def test_fixture_settings_that_pass(source):
    assert settings_findings(source) == []


_BAD_SETTINGS = {
    "plain-str": "class Settings:\n    secret_key: str\n    algorithm: str = 'HS256'\n",
    "secret-default": (
        _SETTINGS_HEADER
        + "class Settings:\n    secret_key: AuthSecretKey = 'dev'\n"
        + "    algorithm: JwtAlgorithm = 'HS256'\n"
    ),
    "algorithm-plain": (
        _SETTINGS_HEADER
        + "class Settings:\n    secret_key: AuthSecretKey\n    algorithm: str = 'HS256'\n"
    ),
    "algorithm-missing": _SETTINGS_HEADER + "class Settings:\n    secret_key: AuthSecretKey\n",
    "algorithm-rs256-default": (
        _SETTINGS_HEADER
        + "class Settings:\n    secret_key: AuthSecretKey\n"
        + "    algorithm: JwtAlgorithm = 'RS256'\n"
    ),
    "algorithm-name-default": (
        _SETTINGS_HEADER
        + "class Settings:\n    secret_key: AuthSecretKey\n"
        + "    algorithm: JwtAlgorithm = DEFAULT\n"
    ),
    "same-name-from-elsewhere": (
        "from somewhere import AuthSecretKey, JwtAlgorithm\n"
        "class Settings:\n    secret_key: AuthSecretKey\n    algorithm: JwtAlgorithm = 'HS256'\n"
    ),
    "upper-case-field": ("class Settings:\n    SECRET_KEY: str\n    ALGORITHM: str = 'HS256'\n"),
    "declared-twice": (
        _SETTINGS_HEADER
        + "class Settings:\n    secret_key: AuthSecretKey\n"
        + "    algorithm: JwtAlgorithm = 'HS256'\n    secret_key = 'x'\n"
    ),
    "plain-assignment": (
        _SETTINGS_HEADER + "class Settings:\n    secret_key = 'x'\n    algorithm: JwtAlgorithm\n"
    ),
    "validator-on-secret": (
        _SETTINGS_HEADER
        + "from pydantic import field_validator\n"
        + "class Settings:\n    secret_key: AuthSecretKey\n"
        + "    algorithm: JwtAlgorithm = 'HS256'\n"
        + "    @field_validator('secret_key', mode='before')\n"
        + "    @classmethod\n    def _fill(cls, v):\n        return v or 'dev'\n"
    ),
    "validator-on-algorithm": (
        _SETTINGS_HEADER
        + "import pydantic\n"
        + "class Settings:\n    secret_key: AuthSecretKey\n"
        + "    algorithm: JwtAlgorithm = 'HS256'\n"
        + "    @pydantic.validator('algorithm')\n    def _a(cls, v):\n        return v\n"
    ),
    "validator-import-alias": (
        _SETTINGS_HEADER
        + "from pydantic import field_validator as fv\n"
        + "class Settings:\n    secret_key: AuthSecretKey\n"
        + "    algorithm: JwtAlgorithm = 'HS256'\n"
        + "    @fv('secret_key', mode='before')\n    @classmethod\n    def _s(cls, v):\n"
        + "        return v\n"
    ),
    "validator-assigned-alias": (
        _SETTINGS_HEADER
        + "from pydantic import field_validator\nfv = field_validator\n"
        + "class Settings:\n    secret_key: AuthSecretKey\n"
        + "    algorithm: JwtAlgorithm = 'HS256'\n"
        + "    @fv('algorithm')\n    @classmethod\n    def _a(cls, v):\n        return v\n"
    ),
    "validator-wildcard": (
        _SETTINGS_HEADER
        + "from pydantic import field_validator\n"
        + "class Settings:\n    secret_key: AuthSecretKey\n"
        + "    algorithm: JwtAlgorithm = 'HS256'\n"
        + "    @field_validator('*')\n    @classmethod\n    def _all(cls, v):\n        return v\n"
    ),
    "type-rebound": (
        _SETTINGS_HEADER
        + "AuthSecretKey = str\n"
        + "class Settings:\n    secret_key: AuthSecretKey\n    algorithm: JwtAlgorithm = 'HS256'\n"
    ),
    "type-rebound-in-class": (
        _SETTINGS_HEADER
        + "class Settings:\n    JwtAlgorithm = str\n    secret_key: AuthSecretKey\n"
        + "    algorithm: JwtAlgorithm = 'HS256'\n"
    ),
    "type-imported-again": (
        _SETTINGS_HEADER
        + "from elsewhere import AuthSecretKey\n"
        + "class Settings:\n    secret_key: AuthSecretKey\n    algorithm: JwtAlgorithm = 'HS256'\n"
    ),
    "type-alias-rebound": (
        "from herd_common.auth_settings import AuthSecretKey as K, JwtAlgorithm\n"
        "K = str\n"
        "class Settings:\n    secret_key: K\n    algorithm: JwtAlgorithm = 'HS256'\n"
    ),
    "module-rebound": (
        "from herd_common import auth_settings\nauth_settings = other\n"
        "class Settings:\n    secret_key: auth_settings.AuthSecretKey\n"
        "    algorithm: auth_settings.JwtAlgorithm = 'HS256'\n"
    ),
    "field-positional-default": (
        _SETTINGS_HEADER
        + "from pydantic import Field\n"
        + "class Settings:\n    secret_key: AuthSecretKey = Field('dev')\n"
        + "    algorithm: JwtAlgorithm = 'HS256'\n"
    ),
    "field-keyword-default": (
        _SETTINGS_HEADER
        + "from pydantic import Field\n"
        + "class Settings:\n    secret_key: AuthSecretKey = Field(default='dev')\n"
        + "    algorithm: JwtAlgorithm = 'HS256'\n"
    ),
    "field-default-factory": (
        _SETTINGS_HEADER
        + "from pydantic import Field\n"
        + "class Settings:\n    secret_key: AuthSecretKey = Field(default_factory=make)\n"
        + "    algorithm: JwtAlgorithm = 'HS256'\n"
    ),
    "field-kwargs": (
        _SETTINGS_HEADER
        + "from pydantic import Field\n"
        + "class Settings:\n    secret_key: AuthSecretKey = Field(**kw)\n"
        + "    algorithm: JwtAlgorithm = 'HS256'\n"
    ),
    "algorithm-field-rs256": (
        _SETTINGS_HEADER
        + "from pydantic import Field\n"
        + "class Settings:\n    secret_key: AuthSecretKey\n"
        + "    algorithm: JwtAlgorithm = Field('RS256')\n"
    ),
    "algorithm-field-factory": (
        _SETTINGS_HEADER
        + "from pydantic import Field\n"
        + "class Settings:\n    secret_key: AuthSecretKey\n"
        + "    algorithm: JwtAlgorithm = Field(default_factory=pick)\n"
    ),
    "annotated-validator": (
        _SETTINGS_HEADER
        + "from typing import Annotated\nfrom pydantic import BeforeValidator\n"
        + "class Settings:\n    secret_key: Annotated[AuthSecretKey, BeforeValidator(fill)]\n"
        + "    algorithm: JwtAlgorithm = 'HS256'\n"
    ),
    "annotated-field-default": (
        _SETTINGS_HEADER
        + "from typing import Annotated\nfrom pydantic import Field\n"
        + "class Settings:\n    secret_key: AuthSecretKey\n"
        + "    algorithm: Annotated[JwtAlgorithm, Field(default='RS256')]\n"
    ),
    "annotated-other-type": (
        _SETTINGS_HEADER
        + "from typing import Annotated\nfrom pydantic import Field\n"
        + "class Settings:\n    secret_key: Annotated[str, Field()]\n"
        + "    algorithm: JwtAlgorithm = 'HS256'\n"
    ),
    "string-other-type": (
        _SETTINGS_HEADER + "class Settings:\n    secret_key: 'str'\n    algorithm: JwtAlgorithm\n"
    ),
}


_EXPECTED_SETTINGS_REASON = {
    "plain-str": "secret_key is not annotated AuthSecretKey",
    "secret-default": "secret_key has a default",
    "algorithm-plain": "algorithm is not annotated JwtAlgorithm",
    "algorithm-missing": "algorithm is not declared",
    "algorithm-rs256-default": "algorithm default is not HS256, HS384, or HS512",
    "algorithm-name-default": "algorithm default is not HS256, HS384, or HS512",
    "same-name-from-elsewhere": "secret_key is not annotated AuthSecretKey",
    "upper-case-field": "secret_key is not annotated AuthSecretKey",
    "declared-twice": "secret_key is declared 2 times",
    "plain-assignment": "secret_key has no annotation",
    "validator-on-secret": "secret_key is named by a validator decorator",
    "validator-on-algorithm": "algorithm is named by a validator decorator",
    "validator-import-alias": "secret_key is named by a validator decorator",
    "validator-assigned-alias": "algorithm is named by a validator decorator",
    "validator-wildcard": "secret_key is named by a validator decorator",
    "type-rebound": "rebinds AuthSecretKey",
    "type-rebound-in-class": "rebinds JwtAlgorithm",
    "type-imported-again": "rebinds AuthSecretKey",
    "type-alias-rebound": "rebinds K",
    "module-rebound": "rebinds auth_settings",
    "field-positional-default": "secret_key has a default",
    "field-keyword-default": "secret_key has a default",
    "field-default-factory": "secret_key has a default",
    "field-kwargs": "secret_key has a default",
    "algorithm-field-rs256": "algorithm default is not HS256, HS384, or HS512",
    "algorithm-field-factory": "algorithm default is not HS256, HS384, or HS512",
    "annotated-validator": "secret_key Annotated metadata is not a Field without a default",
    "annotated-field-default": "algorithm Annotated metadata is not a Field without a default",
    "annotated-other-type": "secret_key is not annotated AuthSecretKey",
    "string-other-type": "secret_key is not annotated AuthSecretKey",
}


def test_every_bad_settings_fixture_names_its_reason():
    assert set(_EXPECTED_SETTINGS_REASON) == set(_BAD_SETTINGS)


@pytest.mark.parametrize("name", sorted(_BAD_SETTINGS))
def test_fixture_settings_that_fail(name):
    findings = settings_findings(_BAD_SETTINGS[name])
    assert any(_EXPECTED_SETTINGS_REASON[name] in f for f in findings), findings
