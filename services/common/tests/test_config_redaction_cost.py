"""Masking cost is linear in the text's length (issue #1464).

The mask runs synchronously inside async handlers on text a caller or a device
controls (execution masks `method_kwargs` before validating them; the assistant
masks driver output), so a super-linear pattern is a denial of service: before
the URL pattern's quantifiers were bounded, `" " + "a-" * n` took 11 s at 64 KB.

Each case times redact_config at three sizes, n, 4n, and 16n, and requires each
step to cost at most RATIO_BOUND times the one before: linear work gives about 4
(the worst step measured in the #1477 review was 7.3, over about 200 runs pinned,
unpinned, beside CPU hogs, and under coverage), a dominant quadratic about 16. The
bound is RELATIVE, so the test holds on a slow, busy, or single-core host (it must
pass under `taskset -c 0`). The timing is `growth_ratios` in `tests/_cost.py`, the
one clock every herd_common cost test reads (#1477): CPU time, n first grown until
one call takes at least MIN_BASE_SECONDS, the sizes timed in REPEATS alternating
rounds keeping each size's fastest call, and a case over the bound measured once more
keeping each size's fastest time across both attempts; `tests/_cost.py` says which
growth that catches and which it does not. The URL-shaped cases carry a `://`,
because the URL pattern only runs on a line that has one.
"""

import json

import pytest
from herd_common.config_redaction import redact_config

from tests._cost import growth_ratios

RATIO_BOUND = 8.0
MIN_BASE_SECONDS = 0.005
REPEATS = 3

SHAPES = {
    "hyphen word run": lambda n: " " + "a-" * (n // 2),
    "dotted word run": lambda n: " " + "a." * (n // 2),
    "boundary character run": lambda n: '"{[(,;?&<>' * (n // 10),
    "key= repeated": lambda n: "key= " * (n // 5),
    "double quote run": lambda n: '"' * n,
    "single quote run": lambda n: "'" * n,
    "keyword glued run": lambda n: "password" * (n // 8),
    "one long line of words": lambda n: "abc " * (n // 4),
    "scheme separator run": lambda n: "://" * (n // 3),
    "url-like run": lambda n: "a://" * (n // 4),
    "hyphen run after a scheme marker": lambda n: "x:// " + "a-" * (n // 2),
    "dotted run after a scheme marker": lambda n: "x:// " + "a." * (n // 2),
    "user name run after a scheme": lambda n: "http://" + "u" * n,
    "escaped newline run": lambda n: "\\n" * (n // 2),
    "escaped quote keyword run": lambda n: '\\"password\\": \\"x\\" ' * (n // 22),
    "xml element run": lambda n: "<password>x</password>" * (n // 22),
    "python repr run": lambda n: "'password' " * (n // 11),
    "json string of lines": lambda n: json.dumps("password x\n" * (n // 11)),
    "lines with keywords": lambda n: "password x\n" * (n // 11),
}


@pytest.mark.parametrize("shape", sorted(SHAPES))
def test_masking_cost_grows_linearly(shape):
    n, times, ratios = growth_ratios(
        SHAPES[shape],
        redact_config,
        bound=RATIO_BOUND,
        repeats=REPEATS,
        min_base_seconds=MIN_BASE_SECONDS,
    )
    assert max(ratios) <= RATIO_BOUND, (shape, n, times, ratios)
