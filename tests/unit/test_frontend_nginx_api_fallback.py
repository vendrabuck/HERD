"""Static pin for the frontend nginx's /api fallback (issue #1320).

An /api path that no Traefik service router claims (its service is stopped or
unhealthy) falls through to the frontend container. Its nginx must answer a 503
with a JSON `detail`, never the SPA's index.html with a 200, which the pages
read as data. The stack test `tests/integration/test_gateway_unrouted_api.py`
proves the answer through the gateway; this one pins the config text so a
change to it fails without a stack.

Limits: a text scan of `frontend/nginx.conf`, not nginx's own parser. It finds
the `location ^~ /api/ { ... }` block by a regular expression over the file,
checks the directives inside it, and parses the quoted `return` body as JSON.
It does not prove the file loads in nginx (`nginx -t`); the stack test does.
"""

import json
import re
from pathlib import Path

NGINX_CONF = Path(__file__).resolve().parents[2] / "frontend" / "nginx.conf"
DETAIL = "This part of HERD is not answering right now. Try again in a moment."


def _api_block() -> str:
    text = NGINX_CONF.read_text()
    # The block ends at the first `}` that is alone on its line (the JSON body
    # in `return` holds braces of its own).
    match = re.search(r"location\s+\^~\s+/api/\s*\{(?P<body>.*?)\n\s*\}\s*\n", text, re.S)
    assert match, "frontend/nginx.conf has no `location ^~ /api/ { ... }` block"
    return match.group("body")


def test_api_block_answers_503_json_with_the_fixed_detail():
    body = _api_block()
    assert re.search(r"^\s*default_type\s+application/json;", body, re.M)
    # nginx types a `return` body by the URI's extension before default_type,
    # so the block empties the types map: every /api path answers JSON.
    assert re.search(r"^\s*types\s*\{\s*\}\s*$", body, re.M)
    ret = re.search(r"^\s*return\s+(\d+)\s+'(?P<payload>[^']*)';", body, re.M)
    assert ret, "the /api/ block must `return <status> '<json>';`"
    assert ret.group(1) == "503"
    assert json.loads(ret.group("payload")) == {"detail": DETAIL}


def test_api_block_is_prefix_priority_so_the_asset_regex_cannot_claim_it():
    # Without ^~ a regex location (the static-asset cache rule) outranks a
    # plain prefix location, so /api/x/logo.svg would get try_files' SPA page.
    text = NGINX_CONF.read_text()
    assert re.search(r"location\s+\^~\s+/api/\s*\{", text)
    assert not re.search(r"location\s+/api/?\s*\{", text)


def test_api_block_does_not_fall_back_to_the_spa():
    body = _api_block()
    assert "try_files" not in body
    assert "index.html" not in body
