"""Every service app renders 422s through the one shared handler (issue #1455).

FastAPI's default request-validation handler echoes the submitted `input` (for a
missing field, the whole body: a secret's plaintext, a password, a webhook signing
secret) and answers 500 when that input holds a lone surrogate.
`herd_common.validation_errors.install_validation_errors` replaces it. This guard
fails when a service app does not install it, so a NEW service that forgets the line
fails here.

What is scanned. The services are every directory under `services/` that has an
`app/` package, found from the directory, never from a list (today twelve; `common`
has no `app/`). For each one:

- `app/main.py` must exist and bind, at module level, at least one name to a
  `FastAPI(...)` call (`FastAPI` imported from `fastapi`, or `fastapi.FastAPI`).
- Every such name must be passed to the install function in a module-level
  statement of its own (`install_validation_errors(app)`), after the assignment,
  with no other argument. The install also adds the body check that refuses a lone
  surrogate (`RefuseLoneSurrogates`); only the config service may turn it off, and
  only with exactly `refuse_lone_surrogates=False` (it answers a surrogate in a
  password itself, OPS-CONFIG-8). Any other keyword, in any service, fails.
  The install function must be bound, at module level and exactly once, by an
  import from the expected module: `from <module> import install_validation_errors`
  (any alias), or `from <package> import validation_errors` (any alias) or
  `import <module> as <alias>` and called as an attribute of that name. The
  expected module is `herd_common.validation_errors`,
  except for the config service, which must not import herd_common and installs its
  standalone copy, `app.validation_errors` (pinned identical in code to the shared
  module by `services/config/tests/test_validation_errors.py`).
- The app name must be bound once in the module, so the installed app is the app.
- No `FastAPI(...)` call appears in any other module under `app/`: a second app
  built elsewhere (a sub-application, a mounted app) would not be checked here, so it
  is refused until this guard learns it.
- No module under `app/` registers another `RequestValidationError` handler: no
  `add_exception_handler(RequestValidationError, ...)` call, no
  `@<x>.exception_handler(RequestValidationError)` decorator, and no
  `exception_handlers=` keyword on `FastAPI(...)`. Config's own copy, which makes
  the one legitimate registration, is the exemption. No module under `app/` assigns
  `<x>.openapi` or `<x>.openapi_schema` (which would drop the advertised shape).

Each service's suite proves the installed handler at run time
(`tests/test_validation_errors.py`, `test_the_shared_handler_is_installed` or its
auth and config equivalents) and sends real refused bodies through the app.

Limits. The scan reads source; it executes nothing:

- not seen: whether the install line is reached at run time (a module-level call is
  always reached, but a `sys.exit()` or an exception raised before it is not
  modelled); a handler removed or replaced after install by mutating
  `app.exception_handlers` directly, by `app.router` surgery, or by a middleware
  that rewrites 422 bodies; the body check removed, or a middleware placed inside
  it, by editing `app.user_middleware` after install (each service suite's
  `test_the_body_check_is_installed` proves the check is the innermost middleware of
  the built app); a custom `APIRoute` subclass (a router's `route_class`) whose
  route handler wraps FastAPI's and could catch `RequestValidationError` and answer
  a body of its own (auth's `_SingleSignOnRoute` in `app/routers/oidc.py` is
  such a wrapper; it is benign: outside OIDC mode it answers a fixed 404 before
  validation runs, and otherwise it calls FastAPI's handler unchanged); a handler
  registered for a base class of
  `RequestValidationError` (`ValidationException`, `Exception`) or under an alias of
  the class name; `setattr(app, "openapi", ...)`; the shared module itself monkeypatched;
  an app object created by a factory or `type(...)` rather than a `FastAPI(...)` call;
  an ASGI app that is not FastAPI (Starlette, a bare callable) anywhere;
- read conservatively (refused though it may be sound): an install call inside a
  function, an `if`, or a `try`; an install whose argument is an expression other
  than the app's name; an app name bound twice (even to the same app); an install
  function bound twice in the module.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SERVICES_DIR = REPO / "services"

SHARED_MODULE = "herd_common.validation_errors"
# The one service that installs a copy instead: it must not import herd_common.
STANDALONE_COPY = {"config": "app.validation_errors"}
# The copy's own file legitimately registers the handler.
REGISTRATION_EXEMPT = {("config", "app/validation_errors.py")}

INSTALL = "install_validation_errors"
# The one service that may install without the body check, and the exact keyword.
BODY_CHECK_EXEMPT = {"config"}
OPT_OUT_KEYWORD = "refuse_lone_surrogates"


def service_dirs(services_dir: Path = SERVICES_DIR) -> list[Path]:
    """Every service directory: a directory under `services/` with an `app/` package."""
    return sorted(p for p in services_dir.iterdir() if p.is_dir() and (p / "app").is_dir())


def expected_module(service: str) -> str:
    return STANDALONE_COPY.get(service, SHARED_MODULE)


def _module_bindings(tree: ast.Module) -> dict[str, list[ast.stmt]]:
    """Each name bound at module level, with the statements that bind it."""
    bound: dict[str, list[ast.stmt]] = {}

    def add(name: str, stmt: ast.stmt) -> None:
        bound.setdefault(name, []).append(stmt)

    def targets(node: ast.AST, stmt: ast.stmt) -> None:
        if isinstance(node, ast.Name):
            add(node.id, stmt)
        elif isinstance(node, (ast.Tuple, ast.List)):
            for elt in node.elts:
                targets(elt, stmt)
        elif isinstance(node, ast.Starred):
            targets(node.value, stmt)

    for stmt in ast.walk(tree):
        if isinstance(stmt, (ast.Import, ast.ImportFrom)):
            for alias in stmt.names:
                add((alias.asname or alias.name).split(".")[0], stmt)
        elif isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            add(stmt.name, stmt)
        elif isinstance(stmt, ast.Assign):
            for target in stmt.targets:
                targets(target, stmt)
        elif isinstance(stmt, (ast.AnnAssign, ast.AugAssign)):
            targets(stmt.target, stmt)
        elif isinstance(stmt, (ast.For, ast.AsyncFor)):
            targets(stmt.target, stmt)
        elif isinstance(stmt, (ast.With, ast.AsyncWith)):
            for item in stmt.items:
                if item.optional_vars is not None:
                    targets(item.optional_vars, stmt)
        elif isinstance(stmt, ast.NamedExpr):
            targets(stmt.target, stmt)
        elif isinstance(stmt, ast.Global):
            for name in stmt.names:
                add(name, stmt)
    # Only module-level and `global` bindings matter, but a function-local binding of
    # the same name is harmless; keep every binding whose statement is at module level
    # or is a `global` (which rebinds the module name from inside a function).
    top = set(map(id, tree.body))
    return {
        name: [s for s in stmts if id(s) in top or isinstance(s, ast.Global)]
        for name, stmts in bound.items()
    }


def _is_fastapi_call(node: ast.AST, fastapi_names: set[str], fastapi_modules: set[str]) -> bool:
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    if isinstance(func, ast.Name):
        return func.id in fastapi_names
    return (
        isinstance(func, ast.Attribute)
        and func.attr == "FastAPI"
        and isinstance(func.value, ast.Name)
        and func.value.id in fastapi_modules
    )


def _fastapi_import_names(tree: ast.Module) -> tuple[set[str], set[str]]:
    names: set[str] = set()
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "fastapi":
            names |= {a.asname or a.name for a in node.names if a.name == "FastAPI"}
        elif isinstance(node, ast.Import):
            modules |= {a.asname or a.name for a in node.names if a.name == "fastapi"}
    return names, modules


def fastapi_calls(source: str) -> list[int]:
    """Line numbers of every `FastAPI(...)` call in a module."""
    tree = ast.parse(source)
    names, modules = _fastapi_import_names(tree)
    return sorted(node.lineno for node in ast.walk(tree) if _is_fastapi_call(node, names, modules))


def _opts_out(keywords: list[ast.keyword]) -> bool:
    """Whether the keywords are exactly `refuse_lone_surrogates=False`."""
    return (
        len(keywords) == 1
        and keywords[0].arg == OPT_OUT_KEYWORD
        and isinstance(keywords[0].value, ast.Constant)
        and keywords[0].value.value is False
    )


def install_problems(source: str, module: str, may_opt_out: bool = False) -> list[str]:
    """Why a service's `app/main.py` source does not install the handler from `module`
    on every app it builds; empty when it does. Only when `may_opt_out` may the call
    carry `refuse_lone_surrogates=False`, and no other keyword."""
    tree = ast.parse(source)
    bindings = _module_bindings(tree)
    names, modules = _fastapi_import_names(tree)
    problems: list[str] = []

    apps: dict[str, ast.stmt] = {}
    for stmt in tree.body:
        if isinstance(stmt, ast.Assign) and _is_fastapi_call(stmt.value, names, modules):
            for target in stmt.targets:
                if isinstance(target, ast.Name):
                    apps[target.id] = stmt
        elif (
            isinstance(stmt, ast.AnnAssign)
            and stmt.value is not None
            and isinstance(stmt.target, ast.Name)
            and _is_fastapi_call(stmt.value, names, modules)
        ):
            apps[stmt.target.id] = stmt
    if not apps:
        return ["no module-level `<name> = FastAPI(...)` assignment"]
    all_calls = fastapi_calls(source)
    if len(all_calls) != len(apps):
        problems.append(
            "a FastAPI(...) call that is not a module-level assignment to a name "
            f"(lines {all_calls})"
        )

    # How the install function may be called: a bare name, or an attribute of a module.
    parent, _, leaf = module.rpartition(".")
    direct: set[str] = set()
    via_module: set[str] = set()
    for stmt in tree.body:
        if isinstance(stmt, ast.ImportFrom) and stmt.level == 0:
            for alias in stmt.names:
                if stmt.module == module and alias.name == INSTALL:
                    direct.add(alias.asname or alias.name)
                elif stmt.module == parent and alias.name == leaf:
                    via_module.add(alias.asname or alias.name)
        elif isinstance(stmt, ast.Import):
            for alias in stmt.names:
                if alias.name == module and alias.asname:
                    via_module.add(alias.asname)
    direct = {n for n in direct if len(bindings.get(n, [])) == 1}
    via_module = {n for n in via_module if len(bindings.get(n, [])) == 1}
    if not direct and not via_module:
        return problems + [f"`{INSTALL}` is not imported from `{module}` (exactly once)"]

    installed: dict[str, int] = {}
    for index, stmt in enumerate(tree.body):
        if not (isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Call)):
            continue
        call = stmt.value
        func = call.func
        is_install = (isinstance(func, ast.Name) and func.id in direct) or (
            isinstance(func, ast.Attribute)
            and func.attr == INSTALL
            and isinstance(func.value, ast.Name)
            and func.value.id in via_module
        )
        if (
            is_install
            and len(call.args) == 1
            and (not call.keywords or (may_opt_out and _opts_out(call.keywords)))
            and isinstance(call.args[0], ast.Name)
        ):
            installed.setdefault(call.args[0].id, index)

    for name, assign in apps.items():
        if len(bindings.get(name, [])) != 1:
            problems.append(f"`{name}` is bound more than once")
            continue
        at = tree.body.index(assign)
        if name not in installed:
            problems.append(f"`{name}` is never passed to `{INSTALL}` at module level")
        elif installed[name] < at:
            problems.append(f"`{INSTALL}({name})` comes before `{name}` is built")
    return problems


def _is_rve(node: ast.AST) -> bool:
    return (isinstance(node, ast.Name) and node.id == "RequestValidationError") or (
        isinstance(node, ast.Attribute) and node.attr == "RequestValidationError"
    )


def foreign_registrations(source: str) -> list[int]:
    """Line numbers where a module registers a `RequestValidationError` handler of its
    own, or reassigns an app's `openapi` or `openapi_schema`."""
    hits: set[int] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Call):
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
            if name == "add_exception_handler" and node.args and _is_rve(node.args[0]):
                hits.add(node.lineno)
            if name == "exception_handler" and node.args and _is_rve(node.args[0]):
                hits.add(node.lineno)
            if any(k.arg == "exception_handlers" for k in node.keywords):
                hits.add(node.lineno)
        elif isinstance(node, (ast.Assign, ast.AugAssign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if isinstance(target, ast.Attribute) and target.attr in {
                    "openapi",
                    "openapi_schema",
                }:
                    hits.add(node.lineno)
    return sorted(hits)


SERVICES = service_dirs()


def test_services_are_discovered_from_the_directory():
    names = {p.name for p in SERVICES}
    assert names >= {
        "acl",
        "ai-orchestrator",
        "auth",
        "cabling",
        "config",
        "execution",
        "integration",
        "inventory",
        "notifications",
        "reservations",
        "secrets",
        "user-profile",
    }
    assert "common" not in names


def test_the_standalone_copy_exemption_is_not_stale():
    assert set(STANDALONE_COPY) <= {p.name for p in SERVICES}
    assert BODY_CHECK_EXEMPT <= {p.name for p in SERVICES}
    for service, relative in REGISTRATION_EXEMPT:
        assert (SERVICES_DIR / service / relative).is_file()


@pytest.mark.parametrize("service", SERVICES, ids=lambda p: p.name)
def test_every_service_app_installs_the_shared_handler(service):
    main = service / "app" / "main.py"
    assert main.is_file(), f"{service.name} has app/ but no app/main.py"
    problems = install_problems(
        main.read_text(encoding="utf-8"),
        expected_module(service.name),
        may_opt_out=service.name in BODY_CHECK_EXEMPT,
    )
    assert not problems, f"services/{service.name}/app/main.py: {problems}"


@pytest.mark.parametrize("service", SERVICES, ids=lambda p: p.name)
def test_no_other_module_builds_an_app_or_registers_another_handler(service):
    offenders = []
    for path in sorted((service / "app").rglob("*.py")):
        relative = path.relative_to(service).as_posix()
        source = path.read_text(encoding="utf-8")
        if relative != "app/main.py" and fastapi_calls(source):
            offenders.append(f"{relative}: FastAPI(...) at lines {fastapi_calls(source)}")
        if (service.name, relative) not in REGISTRATION_EXEMPT:
            lines = foreign_registrations(source)
            if lines:
                offenders.append(f"{relative}: own handler or openapi override at {lines}")
    assert not offenders, f"services/{service.name}: {offenders}"


# -- The guard against fixtures: an honest change passes, a broken one fails ------------

GOOD = """\
from fastapi import FastAPI
from herd_common.cors import add_cors_middleware
from herd_common.validation_errors import install_validation_errors

app = FastAPI(title="x")
add_cors_middleware(app, "")
install_validation_errors(app)
app.include_router(router)
"""

PASSING = {
    "good": GOOD,
    "reordered_after_routers": """\
from herd_common.validation_errors import install_validation_errors
from fastapi import FastAPI

app = FastAPI(
    title="x",
    version="1",
)
app.include_router(router)


@app.get("/health")
async def health():
    return {}


install_validation_errors(  # every 422 is {type, loc, msg}
    app
)
""",
    "aliased": """\
from fastapi import FastAPI
from herd_common.validation_errors import install_validation_errors as _install

service = FastAPI()
_install(service)
""",
    "module_attribute": """\
import fastapi
from herd_common import validation_errors as ve

app = fastapi.FastAPI()
ve.install_validation_errors(app)
""",
    "annotated": """\
from fastapi import FastAPI
from herd_common.validation_errors import install_validation_errors

app: FastAPI = FastAPI()
install_validation_errors(app)
""",
}

FAILING = {
    "removed": GOOD.replace("install_validation_errors(app)\n", ""),
    "commented_out": GOOD.replace(
        "install_validation_errors(app)", "# install_validation_errors(app)"
    ),
    "only_imported": GOOD.replace(
        "install_validation_errors(app)\n", "install_validation_errors\n"
    ),
    "wrong_module": GOOD.replace("herd_common.validation_errors", "app.validation_errors"),
    "lookalike_local_def": GOOD.replace(
        "app = FastAPI", "def install_validation_errors(app):\n    pass\n\n\napp = FastAPI"
    ),
    "name_rebound": GOOD.replace(
        "app = FastAPI", "install_validation_errors = print\napp = FastAPI"
    ),
    "under_if": GOOD.replace(
        "install_validation_errors(app)", "if False:\n    install_validation_errors(app)"
    ),
    "inside_a_function": GOOD.replace(
        "install_validation_errors(app)", "def setup():\n    install_validation_errors(app)"
    ),
    "other_object": GOOD.replace(
        "install_validation_errors(app)", "install_validation_errors(other)"
    ),
    "keyword_argument": GOOD.replace(
        "install_validation_errors(app)", "install_validation_errors(app=app)"
    ),
    "opts_out_of_the_body_check": GOOD.replace(
        "install_validation_errors(app)",
        "install_validation_errors(app, refuse_lone_surrogates=False)",
    ),
    "a_second_positional_argument": GOOD.replace(
        "install_validation_errors(app)", "install_validation_errors(app, False)"
    ),
    "keyword_splat": GOOD.replace(
        "install_validation_errors(app)", "install_validation_errors(app, **options)"
    ),
    "before_the_app": GOOD.replace('app = FastAPI(title="x")\n', "").replace(
        "install_validation_errors(app)\n", "install_validation_errors(app)\napp = FastAPI()\n"
    ),
    "app_rebound_after_install": GOOD + "app = FastAPI()\n",
    "second_app_not_installed": GOOD + "admin = FastAPI()\n",
    "app_built_in_a_function": GOOD + "def make():\n    return FastAPI()\n",
    "no_app": "from herd_common.validation_errors import install_validation_errors\n",
}


@pytest.mark.parametrize("source", PASSING.values(), ids=PASSING.keys())
def test_fixture_mains_that_pass(source):
    assert install_problems(source, SHARED_MODULE) == []


@pytest.mark.parametrize("source", FAILING.values(), ids=FAILING.keys())
def test_fixture_mains_that_fail(source):
    assert install_problems(source, SHARED_MODULE) != []


OPTED_OUT = GOOD.replace(
    "install_validation_errors(app)", "install_validation_errors(app, refuse_lone_surrogates=False)"
)


def test_only_an_exempt_service_may_opt_out_of_the_body_check():
    assert install_problems(OPTED_OUT, SHARED_MODULE, may_opt_out=True) == []
    assert install_problems(OPTED_OUT, SHARED_MODULE) != []
    assert install_problems(GOOD, SHARED_MODULE, may_opt_out=True) == []


@pytest.mark.parametrize(
    "call",
    [
        "install_validation_errors(app, refuse_lone_surrogates=0)",
        "install_validation_errors(app, refuse_lone_surrogates=None)",
        "install_validation_errors(app, refuse_lone_surrogates=flag)",
        "install_validation_errors(app, refuse_lone_surrogates=False, other=1)",
        "install_validation_errors(app, other=False)",
        "install_validation_errors(app, **{'refuse_lone_surrogates': False})",
        "install_validation_errors(app, False)",
    ],
)
def test_an_exempt_service_may_opt_out_only_with_exactly_that_keyword(call):
    source = GOOD.replace("install_validation_errors(app)", call)
    assert install_problems(source, SHARED_MODULE, may_opt_out=True) != []


def test_config_must_install_its_copy_not_the_shared_module():
    copy_main = GOOD.replace("herd_common.validation_errors", "app.validation_errors")
    assert install_problems(copy_main, expected_module("config")) == []
    assert install_problems(GOOD, expected_module("config")) != []
    assert install_problems(copy_main, expected_module("secrets")) != []


@pytest.mark.parametrize(
    "source",
    [
        "app.add_exception_handler(RequestValidationError, mine)\n",
        "app.add_exception_handler(exceptions.RequestValidationError, mine)\n",
        "@app.exception_handler(RequestValidationError)\nasync def h(r, e):\n    pass\n",
        "app = FastAPI(exception_handlers={RequestValidationError: mine})\n",
        "app.openapi = custom\n",
        "app.openapi_schema = {}\n",
    ],
)
def test_a_foreign_registration_is_found(source):
    assert foreign_registrations(source) != []


@pytest.mark.parametrize(
    "source",
    [
        "app.add_exception_handler(PermissionError, h)\n",
        "@app.exception_handler(ConfigAuthError)\nasync def h(r, e):\n    pass\n",
        "# app.add_exception_handler(RequestValidationError, mine)\n",
        "schema = app.openapi()\n",
    ],
)
def test_an_unrelated_registration_is_not_found(source):
    assert foreign_registrations(source) == []


def test_a_new_service_without_the_line_fails(tmp_path):
    """Discovery is by directory, so a service added later is checked with no edit."""
    for name, source in [("old", GOOD), ("brand-new", FAILING["removed"])]:
        (tmp_path / name / "app").mkdir(parents=True)
        (tmp_path / name / "app" / "main.py").write_text(source)
    (tmp_path / "common" / "herd_common").mkdir(parents=True)
    found = service_dirs(tmp_path)
    assert [p.name for p in found] == ["brand-new", "old"]
    verdicts = {
        p.name: install_problems((p / "app" / "main.py").read_text(), expected_module(p.name))
        for p in found
    }
    assert verdicts["old"] == []
    assert verdicts["brand-new"] != []
