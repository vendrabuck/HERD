"""Issue #911: every durable pull consumer must run the shared in-progress
heartbeat, so no in-flight message reaches ack_wait while its handler runs.

Structural, deliberately coarse: any service module that reaches for
`pull_subscribe` must also run its batches through `process_batch_with_heartbeat`
from herd_common.jetstream (execution since issue #944; no module keeps an inline
heartbeat loop, whose hand-rolled cancel handling once swallowed a shutdown cancel)
and (issue #944) take ack_wait from `settings.nats_ack_wait_seconds` and the
heartbeat cadence from `herd_common.jetstream.heartbeat_interval` of that value,
never a hardcoded value or its own fraction. A new consumer module that skips the
heartbeat fails here. The behavioral proof lives in each service's own tests.

Every check is an AST scan of what the module DOES (issue #1158): the helper's
name in a comment, a docstring, or an unused import is not a call, and two
correct-looking assignment lines do not help a module that then hands the helper
`NATS_HEARTBEAT_SECONDS * 2` or gives its durable `ack_wait=NATS_ACK_WAIT_SECONDS
/ 4`. What stays out of reach of a static scan is whether the function that calls
the helper is itself reached by the consumer loop; the service suites drive that.
"""

import ast
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]

JETSTREAM_MODULE = "herd_common.jetstream"
_PULL_CONSUMER_ATTRIBUTES = {"pull_subscribe", "pull_subscribe_bind"}


def reaches_for_a_pull_consumer(source: str) -> bool:
    """Whether a module touches `pull_subscribe` or `pull_subscribe_bind` as an
    attribute at all: a call under any spacing, or the bound method taken first
    and called later. A mention in a comment or a string is not a use."""
    if "pull_subscribe" not in source:
        return False
    return any(
        isinstance(node, ast.Attribute) and node.attr in _PULL_CONSUMER_ATTRIBUTES
        for node in ast.walk(ast.parse(source))
    )


CONSUMER_MODULES = sorted(
    p
    for p in (REPO / "services").glob("*/app/**/*.py")
    if reaches_for_a_pull_consumer(p.read_text())
)


def test_consumer_modules_are_discovered():
    names = {p.parts[-4] for p in CONSUMER_MODULES}
    assert {"execution", "integration", "notifications"} <= names


def test_discovery_is_by_use_not_by_spelling():
    assert reaches_for_a_pull_consumer("psub = await js.pull_subscribe('s', durable='d')\n")
    assert reaches_for_a_pull_consumer("psub = await js.pull_subscribe (\n    's'\n)\n")
    assert reaches_for_a_pull_consumer("subscribe = js.pull_subscribe\npsub = subscribe('s')\n")
    assert reaches_for_a_pull_consumer("psub = await js.pull_subscribe_bind('d', 's')\n")
    assert not reaches_for_a_pull_consumer("# js.pull_subscribe( binds to the durable\n")
    assert not reaches_for_a_pull_consumer('"""See js.pull_subscribe(...)."""\nx = 1\n')


# The shared helper's own name, plus the private alias the #944 inline shape used.
_HEARTBEAT_HELPER_NAMES = {"keep_messages_alive", "_keep_messages_alive"}


def find_inline_heartbeats(source: str) -> list[int]:
    """Line numbers where a module runs its own heartbeat instead of the shared
    `process_batch_with_heartbeat`.

    Two behaviors count, whatever the spelling: a call to `keep_messages_alive`
    under its own name, a private name, an import alias (`import ... as _ka`), or
    as a module attribute (`jetstream.keep_messages_alive(...)`); and any
    `.in_progress(` call, which is what a hand-rolled heartbeat loop must make.
    A mention in a comment or docstring is not a call and is not counted.
    """
    tree = ast.parse(source)
    helper_names = set(_HEARTBEAT_HELPER_NAMES)
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name == "keep_messages_alive":
                    helper_names.add(alias.asname or alias.name)
    hits: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name) and func.id in helper_names:
            hits.add(node.lineno)
        elif isinstance(func, ast.Attribute) and (
            func.attr == "in_progress" or func.attr in _HEARTBEAT_HELPER_NAMES
        ):
            hits.add(node.lineno)
    return sorted(hits)


def test_detector_flags_every_spelling_of_an_own_heartbeat():
    src = (
        "from herd_common.jetstream import keep_messages_alive as _ka\n"
        "import herd_common.jetstream as js\n"
        "async def historic(msg):\n"
        "    hb = asyncio.create_task(_keep_messages_alive([msg]))\n"
        "async def plain(msg):\n"
        "    await keep_messages_alive([msg], 5)\n"
        "async def aliased(msg):\n"
        "    hb = asyncio.create_task(_ka([msg], 5))\n"
        "async def attribute(msg):\n"
        "    hb = asyncio.create_task(js.keep_messages_alive([msg], 5))\n"
        "async def hand_rolled(msg):\n"
        "    while True:\n"
        "        await msg.in_progress()\n"
        "        await asyncio.sleep(5)\n"
    )
    assert find_inline_heartbeats(src) == [4, 6, 8, 10, 13]


def test_detector_ignores_the_shared_helper_and_mentions():
    src = (
        '"""Every message gets `in_progress` through keep_messages_alive."""\n'
        "from herd_common.jetstream import process_batch_with_heartbeat\n"
        "# keep_messages_alive( and msg.in_progress() are mentioned here only\n"
        "async def process_batch(msgs):\n"
        "    await process_batch_with_heartbeat(msgs, handle, 5)\n"
        "outcome = 'in_progress'\n"
    )
    assert find_inline_heartbeats(src) == []


def test_no_pull_consumer_module_keeps_an_inline_heartbeat():
    """Issue #944: the inline `try: await heartbeat / except CancelledError: pass`
    shape cannot tell a cancel aimed at the consumer from the heartbeat's own.
    Any own heartbeat, under any spelling, is refused (issue #1145)."""
    inline = [
        f"{p.relative_to(REPO)}:{lineno}"
        for p in CONSUMER_MODULES
        for lineno in find_inline_heartbeats(p.read_text())
    ]
    assert not inline, f"pull consumer with its own heartbeat task: {inline}"


class _Module:
    """Name resolution for one module, enough to follow a value back to the shared
    helpers and to `settings.nats_ack_wait_seconds`."""

    def __init__(self, source: str):
        self.tree = ast.parse(source)
        # local name to the herd_common.jetstream name it was imported as
        self.jetstream_names: dict[str, str] = {}
        # local names bound to the herd_common.jetstream module itself
        self.jetstream_modules: set[str] = set()
        self.consumer_config_names = {"ConsumerConfig"}
        # every place a plain name is bound: assignments, defs, parameters, imports
        self.bindings: dict[str, list[ast.AST]] = {}
        for node in ast.walk(self.tree):
            if isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    local = alias.asname or alias.name
                    self._bind(local, node)
                    if node.module == JETSTREAM_MODULE:
                        self.jetstream_names[local] = alias.name
                    elif node.module == "herd_common" and alias.name == "jetstream":
                        self.jetstream_modules.add(local)
                    if alias.name == "ConsumerConfig":
                        self.consumer_config_names.add(local)
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    self._bind(alias.asname or alias.name.split(".")[0], node)
                    if alias.name == JETSTREAM_MODULE and alias.asname:
                        self.jetstream_modules.add(alias.asname)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                self._bind(node.name, node)
            elif isinstance(node, ast.arg):
                self._bind(node.arg, node)
            elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
                self._bind(node.id, node)

    def _bind(self, name: str, node: ast.AST) -> None:
        self.bindings.setdefault(name, []).append(node)

    def shared_helper(self, func: ast.expr) -> str | None:
        """The herd_common.jetstream helper a call target is, or None. A local
        name counts only while its import is its ONE binding: a module that also
        defines or reassigns the name is calling its own code."""
        if isinstance(func, ast.Name) and func.id in self.jetstream_names:
            if len(self.bindings[func.id]) == 1:
                return self.jetstream_names[func.id]
        if isinstance(func, ast.Attribute):
            value = func.value
            if isinstance(value, ast.Name) and value.id in self.jetstream_modules:
                return func.attr
            if ast.unparse(value) == JETSTREAM_MODULE:
                return func.attr
        return None

    def _single_assigned_value(self, name: str) -> ast.expr | None:
        """The value of a name bound exactly once in the whole module, by a plain
        single-target assignment; None for anything else (rebound, augmented,
        unpacked, a parameter)."""
        if len(self.bindings.get(name, [])) != 1:
            return None
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Assign) and len(node.targets) == 1:
                target = node.targets[0]
                if isinstance(target, ast.Name) and target.id == name:
                    return node.value
            elif isinstance(node, ast.AnnAssign) and node.value is not None:
                if isinstance(node.target, ast.Name) and node.target.id == name:
                    return node.value
        return None

    def is_settings_ack_wait(self, expr: ast.expr, depth: int = 0) -> bool:
        """`settings.nats_ack_wait_seconds`, or a name whose one assignment is it."""
        if isinstance(expr, ast.Attribute):
            return ast.unparse(expr) == "settings.nats_ack_wait_seconds"
        if isinstance(expr, ast.Name) and depth < 4:
            value = self._single_assigned_value(expr.id)
            return value is not None and self.is_settings_ack_wait(value, depth + 1)
        return False

    def is_shared_cadence(self, expr: ast.expr, depth: int = 0) -> bool:
        """`heartbeat_interval(<settings ack_wait>)` from herd_common.jetstream, or
        a name whose one assignment is that. Arithmetic on either is not."""
        if isinstance(expr, ast.Call):
            return (
                self.shared_helper(expr.func) == "heartbeat_interval"
                and len(expr.args) == 1
                and not expr.keywords
                and self.is_settings_ack_wait(expr.args[0])
            )
        if isinstance(expr, ast.Name) and depth < 4:
            value = self._single_assigned_value(expr.id)
            return value is not None and self.is_shared_cadence(value, depth + 1)
        return False


NO_SHARED_HEARTBEAT = "never calls herd_common.jetstream.process_batch_with_heartbeat"


def heartbeat_wiring_problems(source: str) -> list[str]:
    """Why a pull-consumer module does not run the shared heartbeat at the shared
    cadence under the configured ack_wait; empty when it does.

    - It must CALL herd_common.jetstream's `process_batch_with_heartbeat` (own
      name, import alias, or module attribute); an import, a comment, or a local
      function of the same name is not that call.
    - The interval of every such call (third positional argument or `interval=`)
      must be `heartbeat_interval(...)` of `settings.nats_ack_wait_seconds`,
      directly or through names assigned once. `X * 2`, `ACK_WAIT / 3`, a literal,
      a reassigned name, or a local `heartbeat_interval` all fail.
    - Every `ConsumerConfig(...)` must pass `ack_wait=`, and every `ack_wait`
      the module sets (keyword, attribute, or dict key) must be
      `settings.nats_ack_wait_seconds`, directly or through a name assigned once.
    """
    module = _Module(source)
    problems: list[str] = []
    helper_calls = [
        node
        for node in ast.walk(module.tree)
        if isinstance(node, ast.Call)
        and module.shared_helper(node.func) == "process_batch_with_heartbeat"
    ]
    if not helper_calls:
        problems.append(NO_SHARED_HEARTBEAT)
    for call in helper_calls:
        interval = call.args[2] if len(call.args) > 2 else None
        for keyword in call.keywords:
            if keyword.arg == "interval":
                interval = keyword.value
        if interval is None or not module.is_shared_cadence(interval):
            shown = "nothing" if interval is None else ast.unparse(interval)
            problems.append(
                f"line {call.lineno}: the heartbeat interval is {shown}, not "
                "heartbeat_interval(settings.nats_ack_wait_seconds)"
            )
    for node in ast.walk(module.tree):
        ack_wait_values: list[ast.expr | None] = []
        if isinstance(node, ast.Call):
            keywords = {k.arg: k.value for k in node.keywords}
            is_config = (
                isinstance(node.func, ast.Name) and node.func.id in module.consumer_config_names
            ) or (isinstance(node.func, ast.Attribute) and node.func.attr == "ConsumerConfig")
            if "ack_wait" in keywords:
                ack_wait_values.append(keywords["ack_wait"])
            elif is_config:
                ack_wait_values.append(None)
        elif isinstance(node, (ast.Assign, ast.AugAssign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if any(isinstance(t, ast.Attribute) and t.attr == "ack_wait" for t in targets):
                ack_wait_values.append(None if isinstance(node, ast.AugAssign) else node.value)
        elif isinstance(node, ast.Dict):
            for key, value in zip(node.keys, node.values):
                if isinstance(key, ast.Constant) and key.value == "ack_wait":
                    ack_wait_values.append(value)
        for value in ack_wait_values:
            if value is None or not module.is_settings_ack_wait(value):
                shown = "not set from settings" if value is None else ast.unparse(value)
                problems.append(
                    f"line {node.lineno}: ack_wait is {shown}, not settings.nats_ack_wait_seconds"
                )
    return problems


_IMPORTS = (
    "from app.config import settings\n"
    "from herd_common.jetstream import heartbeat_interval, process_batch_with_heartbeat\n"
)
_CONSTANTS = (
    "NATS_ACK_WAIT_SECONDS = settings.nats_ack_wait_seconds\n"
    "NATS_HEARTBEAT_SECONDS = heartbeat_interval(NATS_ACK_WAIT_SECONDS)\n"
)
_BATCH = (
    "async def process_batch(msgs):\n"
    "    await process_batch_with_heartbeat(msgs, handle, NATS_HEARTBEAT_SECONDS)\n"
)
_START = (
    "async def start(js):\n"
    "    from nats.js.api import ConsumerConfig\n"
    "    config = ConsumerConfig(max_deliver=5, ack_wait=NATS_ACK_WAIT_SECONDS)\n"
    "    return await js.pull_subscribe('s', durable='d', config=config)\n"
)
GOOD_CONSUMER = _IMPORTS + _CONSTANTS + _BATCH + _START

SOUND_SHAPES = {
    "the shape the three services use": GOOD_CONSUMER,
    "an import alias and the interval keyword": (
        "from app.config import settings\n"
        "from herd_common.jetstream import heartbeat_interval as hi\n"
        "from herd_common.jetstream import process_batch_with_heartbeat as run_batch\n"
        + _CONSTANTS.replace("heartbeat_interval(", "hi(")
        + "async def process_batch(msgs):\n"
        "    await run_batch(msgs, handle, interval=NATS_HEARTBEAT_SECONDS)\n" + _START
    ),
    "module attributes and no intermediate names": (
        "from app.config import settings\n"
        "from herd_common import jetstream\n"
        "async def process_batch(msgs):\n"
        "    await jetstream.process_batch_with_heartbeat(\n"
        "        msgs, handle, jetstream.heartbeat_interval(settings.nats_ack_wait_seconds)\n"
        "    )\n"
        "async def start(js, api):\n"
        "    config = api.ConsumerConfig(ack_wait=settings.nats_ack_wait_seconds)\n"
        "    return await js.pull_subscribe('s', durable='d', config=config)\n"
    ),
}


@pytest.mark.parametrize("shape", sorted(SOUND_SHAPES))
def test_wiring_scan_accepts_the_shared_heartbeat_under_any_spelling(shape: str):
    assert heartbeat_wiring_problems(SOUND_SHAPES[shape]) == []


_CALL = "process_batch_with_heartbeat(msgs, handle, NATS_HEARTBEAT_SECONDS)"
_ACK = "ack_wait=NATS_ACK_WAIT_SECONDS"

FALSE_PASSES = {
    "helper imported and named in a comment, never called": GOOD_CONSUMER.replace(
        f"    await {_CALL}\n",
        f"    # await {_CALL}\n    for msg in msgs:\n        await handle(msg)\n",
    ),
    "helper named only in a docstring": GOOD_CONSUMER.replace(
        f"    await {_CALL}\n", f'    """Runs {_CALL}."""\n    await handle(msgs[0])\n'
    ),
    "helper referenced but not called": GOOD_CONSUMER.replace(
        f"    await {_CALL}\n", "    runner = process_batch_with_heartbeat\n"
    ),
    "a local function of the same name": GOOD_CONSUMER.replace(
        _BATCH,
        "async def process_batch_with_heartbeat(msgs, one, interval):\n"
        "    for msg in msgs:\n"
        "        await one(msg)\n" + _BATCH,
    ),
    "interval is twice the shared cadence": GOOD_CONSUMER.replace(
        _CALL, _CALL.replace("SECONDS)", "SECONDS * 2)")
    ),
    "interval is a third of ack_wait": GOOD_CONSUMER.replace(
        _CALL, _CALL.replace("NATS_HEARTBEAT_SECONDS", "NATS_ACK_WAIT_SECONDS / 3")
    ),
    "interval is ack_wait itself": GOOD_CONSUMER.replace(
        _CALL, _CALL.replace("NATS_HEARTBEAT_SECONDS", "NATS_ACK_WAIT_SECONDS")
    ),
    "interval is a literal": GOOD_CONSUMER.replace(
        _CALL, _CALL.replace("NATS_HEARTBEAT_SECONDS", "15")
    ),
    "interval keyword overrides the positional shape": GOOD_CONSUMER.replace(
        _CALL, "process_batch_with_heartbeat(msgs, handle, interval=NATS_HEARTBEAT_SECONDS / 2)"
    ),
    "heartbeat name reassigned after the pinned line": GOOD_CONSUMER.replace(
        _CONSTANTS, _CONSTANTS + "NATS_HEARTBEAT_SECONDS = NATS_ACK_WAIT_SECONDS * 2\n"
    ),
    "heartbeat name augmented after the pinned line": GOOD_CONSUMER.replace(
        _CONSTANTS, _CONSTANTS + "NATS_HEARTBEAT_SECONDS *= 2\n"
    ),
    "heartbeat name rebound inside the function": GOOD_CONSUMER.replace(
        f"    await {_CALL}\n", f"    NATS_HEARTBEAT_SECONDS = 20\n    await {_CALL}\n"
    ),
    "a local heartbeat_interval shadows the shared one": GOOD_CONSUMER.replace(
        _CONSTANTS, "def heartbeat_interval(ack_wait):\n    return ack_wait * 2\n" + _CONSTANTS
    ),
    "heartbeat_interval of a hardcoded ack_wait": GOOD_CONSUMER.replace(
        "heartbeat_interval(NATS_ACK_WAIT_SECONDS)", "heartbeat_interval(30)"
    ),
    "ack_wait name hardcoded": GOOD_CONSUMER.replace(
        "NATS_ACK_WAIT_SECONDS = settings.nats_ack_wait_seconds", "NATS_ACK_WAIT_SECONDS = 30"
    ),
    "ack_wait name rebound after the pinned line": GOOD_CONSUMER.replace(
        _CONSTANTS, _CONSTANTS + "NATS_ACK_WAIT_SECONDS = 5\n"
    ),
    "durable ack_wait is a fraction of the setting": GOOD_CONSUMER.replace(_ACK, _ACK + " / 4"),
    "durable ack_wait is a literal": GOOD_CONSUMER.replace(_ACK, "ack_wait=30"),
    "durable ack_wait is a literal behind a call": GOOD_CONSUMER.replace(
        _ACK, "ack_wait=float(30)"
    ),
    "durable ack_wait is another name": GOOD_CONSUMER.replace(_ACK, "ack_wait=NATS_MAX_DELIVER"),
    "durable ack_wait left to the server default": GOOD_CONSUMER.replace(", " + _ACK, ""),
    "durable ack_wait set by attribute afterwards": GOOD_CONSUMER.replace(
        "    return await", "    config.ack_wait = 5\n    return await"
    ),
    "durable ack_wait passed through a dict": GOOD_CONSUMER.replace(
        f"ConsumerConfig(max_deliver=5, {_ACK})", "ConsumerConfig(**{'ack_wait': 30})"
    ),
}


@pytest.mark.parametrize("spelling", sorted(FALSE_PASSES))
def test_wiring_scan_flags_every_way_to_look_wired_without_being_wired(spelling: str):
    source = FALSE_PASSES[spelling]
    assert source != GOOD_CONSUMER, "the case must change the module"
    assert heartbeat_wiring_problems(source), spelling


def _real_tree_problems() -> list[tuple[str, str]]:
    assert CONSUMER_MODULES, "no pull-consumer module found"
    return [
        (str(p.relative_to(REPO)), problem)
        for p in CONSUMER_MODULES
        for problem in heartbeat_wiring_problems(p.read_text())
    ]


def test_every_pull_consumer_module_uses_the_shared_heartbeat():
    """Issue #911: each consumer module CALLS the shared
    `process_batch_with_heartbeat`; its name in an import, a comment, or a local
    function of the same name is not a call (issue #1158)."""
    missing = [path for path, problem in _real_tree_problems() if problem == NO_SHARED_HEARTBEAT]
    assert not missing, f"pull consumer without the #911 heartbeat: {missing}"


def test_every_pull_consumer_module_takes_ack_wait_and_heartbeat_from_one_source():
    """Issue #944: ack_wait comes from settings and the heartbeat cadence from the
    shared `heartbeat_interval` helper (half of ack_wait, derived once). A module
    that hardcodes either, computes its own fraction, or hands the helper or the
    durable anything but those two values fails here (issue #1158)."""
    problems = [
        f"{path}: {problem}"
        for path, problem in _real_tree_problems()
        if problem != NO_SHARED_HEARTBEAT
    ]
    assert not problems, "\n".join(problems)
