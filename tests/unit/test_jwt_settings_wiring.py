"""Every access-token decode names exactly one algorithm, and every service's
access-token settings use the shared startup validation.

Two AST scans; nothing is imported or executed.

DECODE CALLS. Every Python file under `services/*/app` and
`services/common/herd_common` is read for references to python-jose's
`jwt.decode`. A reference is any of these spellings:

- `jwt.decode` after `from jose import jwt` (or `from jose import jwt as j`, then
  `j.decode`);
- `jose.jwt.decode` after `import jose` or `import jose.jwt`, and `J.jwt.decode`
  after `import jose as J`;
- `JJ.decode` after `import jose.jwt as JJ`;
- the bare name after `from jose.jwt import decode` (or `... import decode as d`);
- `getattr(<module>, "decode")` on any of the module spellings above;
- any name bound by a plain assignment to one of the above, or to one of the
  module spellings (`decode = jwt.decode`, `codec = jwt`), followed transitively.

Every reference must be the function of a call (a bare reference handed on, as in
`functools.partial(jwt.decode, ...)`, is refused), and every such call must:

- pass the token and the key as the only positional arguments, with no `*args`
  and no `**kwargs`;
- pass `algorithms=` as a list literal of exactly one element, not starred;
- pass no `options=`, or a dict literal with no `**` whose `verify_signature`
  key, when present, is the literal `True`.

`get_unverified_claims`, `get_unverified_header`, and `get_unverified_headers`
on jose's `jwt` or `jws` module, under the same module spellings or imported by
name from `jose.jwt` or `jose.jws`, may appear only inside a function named in
`UNVERIFIED_READ_ALLOWED` with a reason. The table is empty today; an entry
nothing matches fails as stale.

SETTINGS. Every `services/*/app/config.py` with a class attribute named
`secret_key` (compared case-insensitively, since the Settings models read the
environment case-insensitively) must, in that class, annotate it with
`AuthSecretKey` from `herd_common.auth_settings` with no default, and annotate
`algorithm` with `JwtAlgorithm` from the same module with no default or a string
default that is one of HS256, HS384, HS512 (pydantic does not validate a default).
The type may be imported under an alias or reached as an attribute of the module
(`auth_settings.AuthSecretKey`, `herd_common.auth_settings.AuthSecretKey`). Each
attribute must be declared exactly once in the class and named by no
`field_validator` or `validator` decorator (one could rewrite the value before or
after the shared rule). The config service has no `app/config.py` and is skipped
by construction; it signs its own session with PyJWT, which this scan does not
read.

Limits. What the scans cannot see:

- a decode reached by `importlib.import_module("jose.jwt")`, `__import__`, a
  string passed to `getattr` that is not a literal, `jose.jws.verify` or
  `jose.jwe` (only `jwt.decode` is checked), or code outside the two scanned roots;
- an alias rebound to something else later in the file: names are followed
  without regard to statement order or scope, so a rebinding to another value
  can make an unrelated call read as a decode (refused, a false red), never the
  reverse;
- `algorithms=` whose single element is itself a list or a name bound to several
  algorithms at runtime (`algorithms=[ALGS]` with `ALGS` a list); the element is
  not evaluated;
- a Settings field reading `SECRET_KEY` or `ALGORITHM` under another attribute
  name through `Field(alias=...)` or `validation_alias`, a field added by a base
  class other than the one in the file, a `model_validator`, or a
  `field_validator("*")` (the wildcard is not refused today);
- whether the decode result is used, or whether the code is reached at all.
"""

import ast
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]

JOSE = "jose"
JOSE_JWT = "jose.jwt"
JOSE_JWS = "jose.jws"
UNVERIFIED_READS = {"get_unverified_claims", "get_unverified_header", "get_unverified_headers"}
AUTH_SETTINGS_MODULE = "herd_common.auth_settings"
ALLOWED_ALGORITHMS = ("HS256", "HS384", "HS512")

# (path relative to the repo, enclosing function name) to reason. Empty by design.
UNVERIFIED_READ_ALLOWED: dict[tuple[str, str], str] = {}


def scanned_files() -> list[Path]:
    files = set((REPO / "services").glob("*/app/**/*.py"))
    files |= set((REPO / "services" / "common" / "herd_common").glob("**/*.py"))
    return sorted(files)


# --- decode references ---


class _JoseNames:
    """Names in one module bound to jose's jwt module, its decode, or its
    unverified readers, plus names bound to the `jose` package itself."""

    def __init__(self, tree: ast.AST):
        self.package: set[str] = set()  # `jose`
        self.jwt_module: set[str] = set()  # `jose.jwt`
        self.jws_module: set[str] = set()  # `jose.jws`
        self.decode: set[str] = set()  # `jose.jwt.decode`
        self.unverified: set[str] = set()  # an unverified reader by name
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == JOSE:
                        self.package.add(alias.asname or JOSE)
                    elif alias.name == JOSE_JWT:
                        if alias.asname:
                            self.jwt_module.add(alias.asname)
                        else:
                            self.package.add(JOSE)
                    elif alias.name == JOSE_JWS:
                        if alias.asname:
                            self.jws_module.add(alias.asname)
                        else:
                            self.package.add(JOSE)
            elif isinstance(node, ast.ImportFrom) and node.level == 0:
                for alias in node.names:
                    bound = alias.asname or alias.name
                    if node.module == JOSE and alias.name == "jwt":
                        self.jwt_module.add(bound)
                    elif node.module == JOSE and alias.name == "jws":
                        self.jws_module.add(bound)
                    elif node.module == JOSE_JWT and alias.name == "decode":
                        self.decode.add(bound)
                    elif node.module in (JOSE_JWT, JOSE_JWS) and alias.name in UNVERIFIED_READS:
                        self.unverified.add(bound)
        self._follow_assignments(tree)

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
                    if isinstance(target, ast.Name):
                        bucket = getattr(self, kind)
                        if target.id not in bucket:
                            bucket.add(target.id)
                            changed = True

    def _module_kind(self, node: ast.AST) -> str | None:
        if isinstance(node, ast.Name):
            if node.id in self.jwt_module:
                return "jwt_module"
            if node.id in self.jws_module:
                return "jws_module"
            if node.id in self.package:
                return "package"
        if isinstance(node, ast.Attribute) and self._module_kind(node.value) == "package":
            if node.attr == "jwt":
                return "jwt_module"
            if node.attr == "jws":
                return "jws_module"
        return None

    def _attribute_target(self, node: ast.AST) -> tuple[ast.AST, str] | None:
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

    def kind_of(self, node: ast.AST) -> str | None:
        """What a node refers to: a module bucket, `decode`, `unverified`, or None."""
        module = self._module_kind(node)
        if module is not None:
            return module
        if isinstance(node, ast.Name):
            if node.id in self.decode:
                return "decode"
            if node.id in self.unverified:
                return "unverified"
            return None
        target = self._attribute_target(node)
        if target is None:
            return None
        obj, name = target
        obj_kind = self._module_kind(obj)
        if obj_kind == "jwt_module" and name == "decode":
            return "decode"
        if obj_kind in ("jwt_module", "jws_module") and name in UNVERIFIED_READS:
            return "unverified"
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
    """Map id(node) to the name of the innermost function containing it."""
    owners: dict[int, str] = {}

    def visit(node: ast.AST, owner: str) -> None:
        for child in ast.iter_child_nodes(node):
            child_owner = (
                child.name if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) else owner
            )
            owners[id(child)] = child_owner
            visit(child, child_owner)

    visit(tree, "<module>")
    return owners


def decode_findings(source: str, rel_path: str = "<fixture>") -> list[str]:
    """Every refused decode or unverified read in one module, as readable lines."""
    if "jose" not in source:
        return []
    tree = ast.parse(source)
    names = _JoseNames(tree)
    owners = _enclosing_functions(tree)
    called: set[int] = set()
    findings: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            kind = names.kind_of(node.func)
            if kind == "decode":
                called.add(id(node.func))
                for problem in _decode_call_problems(node):
                    findings.append(f"{rel_path}:{node.lineno} jose decode {problem}")
    alias_values = {
        id(node.value)
        for node in ast.walk(tree)
        if isinstance(node, (ast.Assign, ast.AnnAssign))
        and node.value is not None
        and all(
            isinstance(t, ast.Name)
            for t in (node.targets if isinstance(node, ast.Assign) else [node.target])
        )
    }
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            continue
        kind = names.kind_of(node)
        if kind == "decode" and id(node) not in called and id(node) not in alias_values:
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
                continue
            findings.append(f"{rel_path}:{node.lineno} jose decode referenced, not called")
        elif kind == "unverified":
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
                continue
            owner = owners.get(id(node), "<module>")
            if (rel_path, owner) not in UNVERIFIED_READ_ALLOWED:
                findings.append(f"{rel_path}:{node.lineno} jose unverified read in {owner}")
    return sorted(set(findings))


def unverified_read_sites(source: str, rel_path: str) -> set[tuple[str, str]]:
    if "jose" not in source:
        return set()
    tree = ast.parse(source)
    names = _JoseNames(tree)
    owners = _enclosing_functions(tree)
    return {
        (rel_path, owners.get(id(node), "<module>"))
        for node in ast.walk(tree)
        if not isinstance(node, (ast.Import, ast.ImportFrom))
        and names.kind_of(node) == "unverified"
        and not (isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store))
    }


# --- settings ---


def _shared_type_names(tree: ast.AST) -> tuple[set[str], set[str]]:
    """(names bound to herd_common.auth_settings, names bound to its two types)."""
    modules: set[str] = set()
    types: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.level == 0:
            for alias in node.names:
                if node.module == AUTH_SETTINGS_MODULE and alias.name in (
                    "AuthSecretKey",
                    "JwtAlgorithm",
                ):
                    types[alias.asname or alias.name] = alias.name
                elif node.module == "herd_common" and alias.name == "auth_settings":
                    modules.add(alias.asname or alias.name)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == AUTH_SETTINGS_MODULE:
                    modules.add(alias.asname or AUTH_SETTINGS_MODULE)
    return modules, types


def _annotation_type(annotation: ast.AST, modules: set[str], types: dict[str, str]) -> str | None:
    if isinstance(annotation, ast.Name):
        return types.get(annotation.id)
    if isinstance(annotation, ast.Attribute) and annotation.attr in (
        "AuthSecretKey",
        "JwtAlgorithm",
    ):
        if ast.unparse(annotation.value) in modules:
            return annotation.attr
    return None


def _assigned_names(stmt: ast.stmt) -> list[str]:
    if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
        return [stmt.target.id]
    if isinstance(stmt, ast.Assign):
        return [t.id for t in stmt.targets if isinstance(t, ast.Name)]
    return []


def _validator_fields(stmt: ast.stmt) -> set[str]:
    fields: set[str] = set()
    if not isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return fields
    for decorator in stmt.decorator_list:
        if isinstance(decorator, ast.Call):
            name = ast.unparse(decorator.func).rsplit(".", 1)[-1]
            if name in ("field_validator", "validator"):
                for arg in decorator.args:
                    if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                        fields.add(arg.value.lower())
    return fields


def settings_findings(source: str, rel_path: str = "<fixture>") -> list[str]:
    tree = ast.parse(source)
    modules, types = _shared_type_names(tree)
    findings: list[str] = []
    for cls in (n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)):
        declared: dict[str, list[ast.stmt]] = {}
        validated: set[str] = set()
        for stmt in cls.body:
            for name in _assigned_names(stmt):
                declared.setdefault(name.lower(), []).append(stmt)
            validated |= _validator_fields(stmt)
        if "secret_key" not in declared:
            continue
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
            if _annotation_type(stmt.annotation, modules, types) != wanted:
                findings.append(f"{where}: {field} is not annotated {wanted}")
            if field == "secret_key" and stmt.value is not None:
                findings.append(f"{where}: secret_key has a default")
            if field == "algorithm" and stmt.value is not None:
                if not (
                    isinstance(stmt.value, ast.Constant) and stmt.value.value in ALLOWED_ALGORITHMS
                ):
                    findings.append(f"{where}: algorithm default is not HS256, HS384, or HS512")
            if field in validated:
                findings.append(f"{where}: {field} is named by a validator decorator")
    return findings


def settings_modules() -> list[Path]:
    return sorted((REPO / "services").glob("*/app/config.py"))


# --- the repository ---


def test_scan_covers_the_three_decode_sites():
    hits = set()
    for path in scanned_files():
        source = path.read_text()
        if "jose" in source:
            tree = ast.parse(source)
            names = _JoseNames(tree)
            if any(
                isinstance(n, ast.Call) and names.kind_of(n.func) == "decode"
                for n in ast.walk(tree)
            ):
                hits.add(str(path.relative_to(REPO)))
    assert {
        "services/common/herd_common/auth.py",
        "services/auth/app/utils/jwt.py",
        "services/notifications/app/routers/notifications.py",
    } <= hits


def test_every_jose_decode_names_one_algorithm_and_keeps_verification():
    findings = []
    for path in scanned_files():
        findings += decode_findings(path.read_text(), str(path.relative_to(REPO)))
    assert findings == []


def test_unverified_read_allowlist_has_no_stale_entries():
    sites = set()
    for path in scanned_files():
        sites |= unverified_read_sites(path.read_text(), str(path.relative_to(REPO)))
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
    "from jose import jwt\njwt.decode(t, k, algorithms=[alg])\n",
    "from jose import jwt as j\nj.decode(t, k, algorithms=['HS256'])\n",
    "import jose\njose.jwt.decode(t, k, algorithms=[alg])\n",
    "import jose.jwt\njose.jwt.decode(t, k, algorithms=[alg])\n",
    "from jose.jwt import decode\ndecode(t, k, algorithms=[alg])\n",
    "from jose import jwt\njwt.decode(t, k, algorithms=[alg], options={'verify_exp': False})\n",
    "from jose import jwt\njwt.decode(t, k, algorithms=[a], options={'verify_signature': True})\n",
    "from jose import jwt\nd = jwt.decode\nd(t, k, algorithms=[alg])\n",
    # PyJWT is not jose.
    "import jwt\njwt.decode(t, k)\n",
    "# from jose import jwt; jwt.decode(t, k)\nx = 1\n",
    "from jose import jwt\njwt.encode(c, k, algorithm=alg)\n",
]


@pytest.mark.parametrize("source", _GOOD_DECODES)
def test_fixture_decodes_that_pass(source):
    assert decode_findings(source) == []


_BAD_DECODES = {
    "no-algorithms": "from jose import jwt\njwt.decode(t, k)\n",
    "two-algorithms": "from jose import jwt\njwt.decode(t, k, algorithms=['HS256', 'none'])\n",
    "empty-list": "from jose import jwt\njwt.decode(t, k, algorithms=[])\n",
    "name-not-list": "from jose import jwt\njwt.decode(t, k, algorithms=ALGS)\n",
    "tuple": "from jose import jwt\njwt.decode(t, k, algorithms=(alg,))\n",
    "starred": "from jose import jwt\njwt.decode(t, k, algorithms=[*algs])\n",
    "positional-algorithms": "from jose import jwt\njwt.decode(t, k, [alg])\n",
    "star-args": "from jose import jwt\njwt.decode(*args, algorithms=[alg])\n",
    "star-kwargs": "from jose import jwt\njwt.decode(t, k, **kw)\n",
    "verify-off": (
        "from jose import jwt\n"
        "jwt.decode(t, k, algorithms=[alg], options={'verify_signature': False})\n"
    ),
    "verify-name": (
        "from jose import jwt\n"
        "jwt.decode(t, k, algorithms=[alg], options={'verify_signature': v})\n"
    ),
    "options-name": "from jose import jwt\njwt.decode(t, k, algorithms=[alg], options=opts)\n",
    "options-splat": ("from jose import jwt\njwt.decode(t, k, algorithms=[alg], options={**o})\n"),
    "alias-module": "from jose import jwt as j\nj.decode(t, k)\n",
    "package": "import jose\njose.jwt.decode(t, k)\n",
    "package-alias": "import jose as J\nJ.jwt.decode(t, k)\n",
    "submodule": "import jose.jwt\njose.jwt.decode(t, k)\n",
    "submodule-alias": "import jose.jwt as JJ\nJJ.decode(t, k)\n",
    "from-decode": "from jose.jwt import decode\ndecode(t, k)\n",
    "from-decode-alias": "from jose.jwt import decode as d\nd(t, k)\n",
    "assigned-function": "from jose import jwt\nd = jwt.decode\nd(t, k)\n",
    "assigned-module": "from jose import jwt\ncodec = jwt\ncodec.decode(t, k)\n",
    "assigned-twice": "from jose import jwt\na = jwt\nb = a.decode\nb(t, k)\n",
    "getattr": "from jose import jwt\ngetattr(jwt, 'decode')(t, k)\n",
    "partial": (
        "import functools\nfrom jose import jwt\n"
        "f = functools.partial(jwt.decode, algorithms=[alg])\n"
    ),
    "passed-on": "from jose import jwt\nrun(jwt.decode, t, k)\n",
    "in-function": "from jose import jwt\ndef f(t):\n    return jwt.decode(t, k)\n",
    "unverified-claims": (
        "from jose import jwt\ndef f(t):\n    return jwt.get_unverified_claims(t)\n"
    ),
    "unverified-header": (
        "from jose import jws\ndef f(t):\n    return jws.get_unverified_header(t)\n"
    ),
    "unverified-by-name": (
        "from jose.jwt import get_unverified_claims as g\ndef f(t):\n    return g(t)\n"
    ),
    "unverified-package": "import jose\nx = jose.jwt.get_unverified_header(t)\n",
}


_NO_ALGORITHMS = "names no algorithms= keyword"
_NOT_ONE = "algorithms= is not a one-element list literal"
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
    "verify-off": "options= turns verify_signature off",
    "verify-name": "options= turns verify_signature off",
    "options-name": "options= is not a dict literal without **",
    "options-splat": "options= is not a dict literal without **",
    # The spelling cases: the call is found under each spelling, so the missing
    # algorithms= is reported.
    "alias-module": _NO_ALGORITHMS,
    "package": _NO_ALGORITHMS,
    "package-alias": _NO_ALGORITHMS,
    "submodule": _NO_ALGORITHMS,
    "submodule-alias": _NO_ALGORITHMS,
    "from-decode": _NO_ALGORITHMS,
    "from-decode-alias": _NO_ALGORITHMS,
    "assigned-function": _NO_ALGORITHMS,
    "assigned-module": _NO_ALGORITHMS,
    "assigned-twice": _NO_ALGORITHMS,
    "getattr": _NO_ALGORITHMS,
    "in-function": _NO_ALGORITHMS,
    "partial": "jose decode referenced, not called",
    "passed-on": "jose decode referenced, not called",
    "unverified-claims": "jose unverified read in f",
    "unverified-header": "jose unverified read in f",
    "unverified-by-name": "jose unverified read in f",
    "unverified-package": "jose unverified read in <module>",
}


def test_every_bad_decode_fixture_names_its_reason():
    assert set(_EXPECTED_DECODE_REASON) == set(_BAD_DECODES)


@pytest.mark.parametrize("name", sorted(_BAD_DECODES))
def test_fixture_decodes_that_fail(name):
    findings = decode_findings(_BAD_DECODES[name])
    assert any(_EXPECTED_DECODE_REASON[name] in f for f in findings), findings


def test_unverified_read_allowlist_admits_only_its_function(monkeypatch):
    source = (
        "from jose import jwt\n"
        "def peek(t):\n    return jwt.get_unverified_header(t)\n"
        "def other(t):\n    return jwt.get_unverified_header(t)\n"
    )
    monkeypatch.setitem(UNVERIFIED_READ_ALLOWED, ("x.py", "peek"), "fixture")
    findings = decode_findings(source, "x.py")
    assert len(findings) == 1 and "in other" in findings[0]


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
}


def test_every_bad_settings_fixture_names_its_reason():
    assert set(_EXPECTED_SETTINGS_REASON) == set(_BAD_SETTINGS)


@pytest.mark.parametrize("name", sorted(_BAD_SETTINGS))
def test_fixture_settings_that_fail(name):
    findings = settings_findings(_BAD_SETTINGS[name])
    assert any(_EXPECTED_SETTINGS_REASON[name] in f for f in findings), findings
