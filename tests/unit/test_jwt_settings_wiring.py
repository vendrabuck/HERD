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
  module spellings (`decode = jwt.decode`, `codec = jwt`), followed transitively;
- any of the above reached from another scanned module: a name imported from a
  scanned module (`from herd_common.auth import jwt`, `from .codec import d`,
  `from app.codec import jwt`, `from herd_common.auth import *`) is what it is
  bound to there, and so is an attribute of a scanned module (`import
  herd_common.auth` then `herd_common.auth.jwt.decode`, `from herd_common import
  auth` then `auth.jwt.decode`), across any number of modules. `herd_common.*` is
  the shared package; `app.*` is resolved inside the importing file's own
  service, since every service has its own `app` package.

Every reference must be the function of a call (a bare reference handed on, as in
`functools.partial(jwt.decode, ...)`, is refused), and every such call must:

- pass the token and the key as the only positional arguments, with no `*args`
  and no `**kwargs`;
- pass `algorithms=` as a list literal of exactly one element, not starred;
- pass no `options=`, or a dict literal with no `**` whose `verify_signature`
  key, when present, is the literal `True`.

Also refused: a star import from `jose` or any `jose.*` module; a jose module
object (`jose`, its `jwt` or `jws` module, under any spelling above) used in any
way but an attribute read, a `getattr` with a literal name, or a plain alias
assignment (`vars(jwt)`, `run(jwt)`, `(jwt,)`, `getattr(jwt, name)`); and a dunder
attribute of one (`jwt.__dict__`, `getattr(jwt, "__dict__")`).

`get_unverified_claims`, `get_unverified_header`, and `get_unverified_headers`
on jose's `jwt` or `jws` module, under the same spellings or imported by name
from `jose.jwt` or `jose.jws`, may appear only inside a function named in
`UNVERIFIED_READ_ALLOWED` with a reason. The table is empty today; an entry
nothing matches fails as stale.

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

The config service has no `app/config.py` and is skipped by construction; it
signs its own session with PyJWT, which this scan does not read.

Limits. What the scans cannot see:

- a decode reached by `importlib.import_module("jose.jwt")`, `__import__`,
  `sys.modules[...]`, `jose.jws.verify` or `jose.jwe` (only `jwt.decode` is
  checked), or code outside the two scanned roots;
- a name followed from another module only through an import statement naming a
  scanned module, or an attribute or literal `getattr` of a scanned module; a
  scanned module object handed on (`vars(herd_common.auth)["jwt"]`,
  `herd_common.auth.__dict__`), a non-literal `getattr` on one, a function's
  return value, or an attribute of anything that is not a module (`obj.d(...)`)
  is not followed;
- refused although it can be honest (false reds, by decision, because the scan
  cannot follow them): a decode stored in a class body, on an instance
  (`class V: d = jwt.decode`, `self.d = jwt.decode`), or in a container is
  reported as referenced, not called; a jose module handed to anything (`vars`,
  a helper, a tuple) is refused; a rebinding of a shared type's name inside a
  function, where it cannot change a class annotation, is refused;
- names are followed without regard to statement order or scope, so a rebinding
  to another value can make an unrelated call read as a decode (refused, a false
  red), never the reverse;
- `algorithms=` whose single element is itself a list or a name bound to several
  algorithms at runtime (`algorithms=[ALGS]` with `ALGS` a list); the element is
  not evaluated;
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

JOSE = "jose"
JOSE_JWT = "jose.jwt"
JOSE_JWS = "jose.jws"
UNVERIFIED_READS = {"get_unverified_claims", "get_unverified_header", "get_unverified_headers"}
AUTH_SETTINGS_MODULE = "herd_common.auth_settings"
SHARED_TYPES = ("AuthSecretKey", "JwtAlgorithm")
ALLOWED_ALGORITHMS = ("HS256", "HS384", "HS512")

# (path relative to the repo, enclosing function name) to reason. Empty by design.
UNVERIFIED_READ_ALLOWED: dict[tuple[str, str], str] = {}


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

JOSE_MODULE_KINDS = ("package", "jwt_module", "jws_module")


def _is_jose_module(dotted: str | None) -> bool:
    return dotted is not None and (dotted == JOSE or dotted.startswith(JOSE + "."))


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
    """What each name in one module is bound to: jose's package (`package`), its
    `jwt` or `jws` module, `decode`, an unverified reader (`unverified`), or a
    scanned module (`mod:<key>`). Names are read from every import and every
    plain assignment in the file, without regard to statement order or scope."""

    def __init__(self, tree: ast.AST, index: "_Index", own_key: str | None):
        self.index = index
        self.bindings: dict[str, str] = {}
        self.star_jose: list[ast.ImportFrom] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                self._import(node, own_key)
            elif isinstance(node, ast.ImportFrom):
                self._import_from(node, own_key)
        self._follow_assignments(tree)

    def _import(self, node: ast.Import, own_key: str | None) -> None:
        for alias in node.names:
            if alias.name == JOSE:
                self.bindings[alias.asname or JOSE] = "package"
            elif alias.name in (JOSE_JWT, JOSE_JWS):
                if alias.asname:
                    kind = "jwt_module" if alias.name == JOSE_JWT else "jws_module"
                    self.bindings[alias.asname] = kind
                else:
                    self.bindings[JOSE] = "package"
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
        if node.level == 0 and _is_jose_module(node.module):
            for alias in node.names:
                bound = alias.asname or alias.name
                if alias.name == "*":
                    self.star_jose.append(node)
                elif node.module == JOSE and alias.name == "jwt":
                    self.bindings[bound] = "jwt_module"
                elif node.module == JOSE and alias.name == "jws":
                    self.bindings[bound] = "jws_module"
                elif node.module == JOSE_JWT and alias.name == "decode":
                    self.bindings[bound] = "decode"
                elif node.module in (JOSE_JWT, JOSE_JWS) and alias.name in UNVERIFIED_READS:
                    self.bindings[bound] = "unverified"
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
            return None
        obj, name = target
        obj_kind = self.kind_of(obj)
        if obj_kind == "package":
            return {"jwt": "jwt_module", "jws": "jws_module"}.get(name)
        if obj_kind == "jwt_module":
            if name == "decode":
                return "decode"
            return "unverified" if name in UNVERIFIED_READS else None
        if obj_kind == "jws_module":
            return "unverified" if name in UNVERIFIED_READS else None
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


def _is_store(node: ast.AST) -> bool:
    return isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del))


def _module_findings(index: _Index, rel_path: str) -> list[str]:
    tree = index.trees[rel_path]
    names = index.names[rel_path]
    owners = _enclosing_functions(tree)
    findings = [f"{rel_path}:{n.lineno} star import from {n.module}" for n in names.star_jose]
    called: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and names.kind_of(node.func) == "decode":
            called.add(id(node.func))
            for problem in _decode_call_problems(node):
                findings.append(f"{rel_path}:{node.lineno} jose decode {problem}")
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
    # A jose module object may be read only by attribute (a literal getattr
    # included); any other use hands it to code the scan does not follow.
    read_by_attribute: set[int] = set()
    for node in ast.walk(tree):
        target = _attribute_target(node)
        if target is None:
            continue
        obj, attr = target
        read_by_attribute.add(id(obj))
        if attr.startswith("__") and names.kind_of(obj) in JOSE_MODULE_KINDS:
            findings.append(f"{rel_path}:{node.lineno} jose module dunder attribute {attr}")
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)) or _is_store(node):
            continue
        kind = names.kind_of(node)
        if kind == "decode":
            if id(node) not in called and id(node) not in alias_values:
                findings.append(f"{rel_path}:{node.lineno} jose decode referenced, not called")
        elif kind == "unverified":
            owner = owners.get(id(node), "<module>")
            if (rel_path, owner) not in UNVERIFIED_READ_ALLOWED:
                findings.append(f"{rel_path}:{node.lineno} jose unverified read in {owner}")
        elif kind in JOSE_MODULE_KINDS:
            if id(node) not in read_by_attribute and id(node) not in alias_values:
                findings.append(
                    f"{rel_path}:{node.lineno} jose module referenced other than by attribute"
                )
    return sorted(set(findings))


def _index_findings(index: _Index) -> list[str]:
    findings: list[str] = []
    for rel_path in index.trees:
        findings += _module_findings(index, rel_path)
    return findings


def scan_decodes(sources: dict[str, str]) -> list[str]:
    """Every refused decode, unverified read, or jose module use across modules."""
    return _index_findings(_Index(sources))


def decode_findings(source: str, rel_path: str = "<fixture>") -> list[str]:
    """`scan_decodes` for one module on its own."""
    return scan_decodes({rel_path: source})


def unverified_read_sites(index: "_Index") -> set[tuple[str, str]]:
    sites = set()
    for rel_path, tree in index.trees.items():
        names = index.names[rel_path]
        owners = _enclosing_functions(tree)
        sites |= {
            (rel_path, owners.get(id(node), "<module>"))
            for node in ast.walk(tree)
            if not isinstance(node, (ast.Import, ast.ImportFrom))
            and not _is_store(node)
            and names.kind_of(node) == "unverified"
        }
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


def test_scan_covers_the_three_decode_sites():
    index = repository_index()
    hits = {
        rel
        for rel, tree in index.trees.items()
        if any(
            isinstance(n, ast.Call) and index.names[rel].kind_of(n.func) == "decode"
            for n in ast.walk(tree)
        )
    }
    assert {
        "services/common/herd_common/auth.py",
        "services/common/herd_common/auth_settings.py",
        "services/auth/app/utils/jwt.py",
        "services/notifications/app/routers/notifications.py",
    } <= hits


def test_the_index_keys_every_scanned_file():
    index = repository_index()
    assert all(key is not None for key in index.keys.values())
    assert index.keys["services/common/herd_common/auth.py"] == "herd_common.auth"
    assert index.keys["services/common/herd_common/__init__.py"] == "herd_common"
    assert index.keys["services/auth/app/utils/jwt.py"] == "auth:app.utils.jwt"


def test_every_jose_decode_names_one_algorithm_and_keeps_verification():
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
    # Reading other attributes of a jose module is fine.
    "from jose import jwt\ntry:\n    pass\nexcept jwt.JWTError:\n    pass\n",
    "from jose import jwt\ngetattr(jwt, 'encode')(c, k, algorithm=alg)\n",
    "from jose import jwt\ncodec = jwt\ncodec.decode(t, k, algorithms=[alg])\n",
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
    "star-jose": "from jose import *\njwt.decode(t, k)\n",
    "star-jose-jwt": "from jose.jwt import *\ndecode(t, k)\n",
    "star-jose-jws": "from jose.jws import *\nget_unverified_header(t)\n",
    "module-handed-on": "from jose import jwt\nrun(jwt)\n",
    "module-in-vars": "from jose import jwt\nvars(jwt)['decode'](t, k)\n",
    "module-in-tuple": "from jose import jwt\nfor m in (jwt,):\n    m.decode(t, k)\n",
    "package-handed-on": "import jose\nrun(jose)\n",
    "jwt-of-package-handed-on": "import jose\nrun(jose.jwt)\n",
    "getattr-not-literal": "from jose import jwt\ngetattr(jwt, name)(t, k)\n",
    "dunder-dict": "from jose import jwt\njwt.__dict__['decode'](t, k)\n",
    "getattr-dunder": "from jose import jwt\ngetattr(jwt, '__dict__')['decode'](t, k)\n",
    "class-attribute-alias": "from jose import jwt\nclass V:\n    d = jwt.decode\nV.d(t, k)\n",
    "instance-attribute-alias": (
        "from jose import jwt\nclass V:\n    def __init__(self):\n        self.d = jwt.decode\n"
    ),
    "decode-in-dict": "from jose import jwt\ntable = {'d': jwt.decode}\n",
}


_NO_ALGORITHMS = "names no algorithms= keyword"
_NOT_ONE = "algorithms= is not a one-element list literal"
_NOT_CALLED = "jose decode referenced, not called"
_MODULE_REFERENCED = "jose module referenced other than by attribute"
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
    "partial": _NOT_CALLED,
    "passed-on": _NOT_CALLED,
    "unverified-claims": "jose unverified read in f",
    "unverified-header": "jose unverified read in f",
    "unverified-by-name": "jose unverified read in f",
    "unverified-package": "jose unverified read in <module>",
    "star-jose": "star import from jose",
    "star-jose-jwt": "star import from jose.jwt",
    "star-jose-jws": "star import from jose.jws",
    "module-handed-on": _MODULE_REFERENCED,
    "module-in-vars": _MODULE_REFERENCED,
    "module-in-tuple": _MODULE_REFERENCED,
    "package-handed-on": _MODULE_REFERENCED,
    "jwt-of-package-handed-on": _MODULE_REFERENCED,
    "getattr-not-literal": _MODULE_REFERENCED,
    "dunder-dict": "jose module dunder attribute __dict__",
    "getattr-dunder": "jose module dunder attribute __dict__",
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
        "from jose import jwt\n"
        "def peek(t):\n    return jwt.get_unverified_header(t)\n"
        "def other(t):\n    return jwt.get_unverified_header(t)\n"
    )
    monkeypatch.setitem(UNVERIFIED_READ_ALLOWED, ("x.py", "peek"), "fixture")
    findings = decode_findings(source, "x.py")
    assert len(findings) == 1 and "in other" in findings[0]


# --- fixture cases: decodes across modules ---

_COMMON = "services/common/herd_common"
_APP = "services/svc/app"

_REEXPORT = {f"{_COMMON}/auth.py": "from jose import jwt\n"}

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
        "services/other/app/codec.py": "from jose import jwt\n",
        f"{_APP}/codec.py": "x = 1\n",
        f"{_APP}/b.py": "from app.codec import jwt\njwt.decode(t, k)\n",
    },
}


@pytest.mark.parametrize("name", sorted(_GOOD_ACROSS_MODULES))
def test_fixture_decodes_across_modules_that_pass(name):
    assert scan_decodes(_GOOD_ACROSS_MODULES[name]) == []


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
        f"{_COMMON}/codec.py": "from jose import jwt\nd = jwt.decode\n",
        f"{_APP}/a.py": "from herd_common.codec import d\nd(t, k)\n",
    },
    "star-from-scanned": {
        **_REEXPORT,
        f"{_APP}/a.py": "from herd_common.auth import *\njwt.decode(t, k)\n",
    },
    "two-hops": {
        f"{_COMMON}/a.py": "from jose import jwt\n",
        f"{_COMMON}/b.py": "from herd_common.a import jwt\n",
        f"{_APP}/c.py": "from herd_common.b import jwt\njwt.decode(t, k)\n",
    },
    "app-absolute": {
        f"{_APP}/codec.py": "from jose import jwt\n",
        f"{_APP}/b.py": "from app.codec import jwt\njwt.decode(t, k)\n",
    },
    "relative-import": {
        f"{_APP}/codec.py": "from jose import jwt\n",
        f"{_APP}/sub/__init__.py": "",
        f"{_APP}/sub/b.py": "from ..codec import jwt\njwt.decode(t, k)\n",
    },
    "relative-from-package": {
        f"{_APP}/__init__.py": "",
        f"{_APP}/codec.py": "from jose import jwt\n",
        f"{_APP}/b.py": "from . import codec\ncodec.jwt.decode(t, k)\n",
    },
    "reexported-unverified": {
        **_REEXPORT,
        f"{_APP}/a.py": (
            "from herd_common.auth import jwt\ndef f(t):\n    return jwt.get_unverified_claims(t)\n"
        ),
    },
    "reexported-module-handed-on": {
        **_REEXPORT,
        f"{_APP}/a.py": "from herd_common.auth import jwt\nvars(jwt)\n",
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
    "star-from-scanned": (f"{_APP}/a.py", _NO_ALGORITHMS),
    "two-hops": (f"{_APP}/c.py", _NO_ALGORITHMS),
    "app-absolute": (f"{_APP}/b.py", _NO_ALGORITHMS),
    "relative-import": (f"{_APP}/sub/b.py", _NO_ALGORITHMS),
    "relative-from-package": (f"{_APP}/b.py", _NO_ALGORITHMS),
    "reexported-unverified": (f"{_APP}/a.py", "jose unverified read in f"),
    "reexported-module-handed-on": (f"{_APP}/a.py", _MODULE_REFERENCED),
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
