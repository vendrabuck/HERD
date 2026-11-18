"""herd_common.config_redaction: the masked display copy of a configuration."""

import copy
import json

from herd_common.config_redaction import (
    REDACTED_VALUE,
    is_keyword_token,
    redact_command_text,
    redact_config,
)

FRR_CONFIG = {
    "commands": [
        "router bgp 65000",
        " neighbor 192.0.2.1 remote-as 65001",
        " neighbor 192.0.2.1 password BgpS3cret",
        "snmp-server community Comm7nity RO",
        "enable secret 5 $1$abcd$Hash0123",
        "username admin password 7 0822455D0A16",
        "interface eth0",
        " ip ospf authentication-key OspfK3y",
        " ip ospf message-digest-key 1 md5 Md5Secret",
        "key chain KC",
        " key-string KeyStr1ng",
    ]
}
SECRET_VALUES = [
    "BgpS3cret",
    "Comm7nity",
    "$1$abcd$Hash0123",
    "0822455D0A16",
    "OspfK3y",
    "Md5Secret",
    "KeyStr1ng",
]


def test_frr_config_keeps_no_credential_value():
    redacted, changed = redact_config(FRR_CONFIG)
    assert changed is True
    text = repr(redacted)
    for value in SECRET_VALUES:
        assert value not in text
    lines = redacted["commands"]
    assert lines[0] == "router bgp 65000"
    assert lines[1] == " neighbor 192.0.2.1 remote-as 65001"
    assert lines[2] == f" neighbor 192.0.2.1 password {REDACTED_VALUE}"
    assert lines[3] == f"snmp-server community {REDACTED_VALUE}"
    assert lines[4] == f"enable secret {REDACTED_VALUE}"
    assert lines[6] == "interface eth0"
    assert lines[7] == f" ip ospf authentication-key {REDACTED_VALUE}"
    assert lines[8] == f" ip ospf message-digest-key {REDACTED_VALUE}"
    assert lines[10] == f" key-string {REDACTED_VALUE}"


def test_value_under_a_credential_named_key_is_masked_whole():
    redacted, changed = redact_config(
        {"snmp": {"community": "public", "location": "lab"}, "users": [{"password": "x"}]}
    )
    assert changed is True
    assert redacted == {
        "snmp": {"community": REDACTED_VALUE, "location": "lab"},
        "users": [{"password": REDACTED_VALUE}],
    }


def test_configuration_without_credentials_is_unchanged():
    config = {"hostname": "r1", "vlan": 100, "commands": ["interface eth1", " shutdown"]}
    redacted, changed = redact_config(config)
    assert changed is False
    assert redacted == config


def test_keyword_at_the_end_of_a_line_masks_nothing():
    assert redact_command_text("show key") == ("show key", False)
    assert redact_command_text("no password") == ("no password", False)


def test_words_that_only_contain_a_keyword_are_not_keywords():
    for line in ("description monkey business", "keyboard layout us", "passwordless on"):
        assert redact_command_text(line) == (line, False)


def test_each_line_of_a_multiline_string_is_masked_on_its_own():
    text = "router bgp 1\n neighbor 10.0.0.1 password Abc\n exit"
    assert redact_command_text(text) == (
        f"router bgp 1\n neighbor 10.0.0.1 password {REDACTED_VALUE}\n exit",
        True,
    )


def test_keywords_match_without_regard_to_case():
    assert redact_command_text("Enable SECRET abc") == (f"Enable SECRET {REDACTED_VALUE}", True)


def test_non_string_values_are_untouched():
    redacted, changed = redact_config({"vlan_id": 5, "tagged": True, "mtu": None, "w": 1.5})
    assert changed is False
    assert redacted == {"vlan_id": 5, "tagged": True, "mtu": None, "w": 1.5}


def test_the_callers_object_is_never_mutated():
    original = copy.deepcopy(FRR_CONFIG)
    redact_config(FRR_CONFIG)
    assert FRR_CONFIG == original


def test_structures_beyond_the_depth_cap_are_masked_whole():
    deep: dict = {"leaf": "value"}
    for _ in range(40):
        deep = {"next": deep}
    redacted, changed = redact_config(deep)
    assert changed is True
    node = redacted
    while isinstance(node, dict):
        node = node["next"]
    assert node == REDACTED_VALUE


def test_frr_lines_whose_secret_follows_an_authentication_keyword_are_masked():
    # Issue #1464: the FRR forms whose secret follows no password, key, or md5
    # token. Each is reachable through the in-repo frr_mgmt driver's commands.
    lines = {
        " ip rip authentication string RipS3cret": " ip rip authentication",
        " ip nhrp authentication NhrpS3cret": " ip nhrp authentication",
        "  tcp-md5-auth PcepS3cret": "  tcp-md5-auth",
        "snmp-server user u1 g1 v3 auth sha AuthS3cret priv aes PrivS3cret": (
            "snmp-server user u1 g1 v3 auth"
        ),
    }
    for line, kept in lines.items():
        redacted, changed = redact_command_text(line)
        assert changed is True, line
        assert redacted == f"{kept} {REDACTED_VALUE}"


def test_a_line_that_ends_at_an_authentication_keyword_is_unchanged():
    for line in (" ip ospf authentication", "no ip rip authentication", "aaa auth"):
        assert redact_command_text(line) == (line, False)


def test_words_that_only_contain_auth_are_not_keywords():
    for line in ("description author jordan", "authorized users only", "oauth2 client on"):
        assert redact_command_text(line) == (line, False)


def test_known_limits_are_left_as_written():
    # Pinned so a widening of either rule is a deliberate change: each shape
    # below is a stated limit (the module docstring's Limits; issue #1464).
    secret = "Lim1tS3cret"
    lines = [
        f"password\n{secret}",  # the secret on the next line
        f"neighbor 1 password{secret}",  # a keyword glued to its value
        f"pass word {secret}",  # a split keyword
        f"pw={secret}",  # an abbreviation that is not a keyword
        f"neighbor 192.0.2.1 remote-as 1 {secret}",  # no keyword at all
        "ZGF0YQ== cGFzc3dvcmQgTGltMXRTM2NyZXQ=",  # base64 of "password <secret>"
        f"password|{secret}",  # a separator the rule does not know
        f"mysql -u root -p {secret}",  # a flag, not a keyword
        f"apikey={secret}",  # a joined keyword
        f"pass\u200bword {secret}",  # a zero-width character inside the keyword
        f"\uff50\uff41\uff53\uff53\uff57\uff4f\uff52\uff44 {secret}",  # fullwidth
        f"neighbor 1.1.1.1 ao {secret}",  # TCP-AO names a key chain, not a key
    ]
    redacted, changed = redact_config(
        {"pw": secret, "keystring": secret, "otp": secret, "commands": lines + ["password", secret]}
    )
    assert changed is False
    assert redacted["pw"] == secret
    assert redacted["keystring"] == secret
    assert redacted["commands"] == lines + ["password", secret]


def test_keys_the_line_keywords_name_are_masked_as_keys():
    keys = [
        "authentication-key",
        "key-string",
        "key_string",
        "message-digest-key",
        "passphrase",
        "pre_shared_key",
        "preshared-key",
        "tacacs_key",
        "tacacsKey",
        "RadiusKey",
        "wpa_passphrase",
        "wpa_psk",
        "md5",
        "psk",
        "phash",
        "auth",
        "authentication",
        "key",
        "bgp.neighbor.key",
        "Auth Password",
    ]
    for key in keys:
        assert redact_config({key: "S3cret"}) == ({key: REDACTED_VALUE}, True), key


def test_keys_whose_parts_are_not_keywords_are_left_alone():
    config = {
        "author_name": "jordan",
        "monkey": "x",
        "keyboard_layout": "us",
        "keys_total": 3,
        "hostname": "r1",
        "oauth_client": "herd",
    }
    assert redact_config(config) == (config, False)


def test_a_keyword_with_punctuation_attached_masks_its_value():
    cases = {
        "Password: S3cret": f"Password: {REDACTED_VALUE}",
        "PASSWORD=S3cret": f"PASSWORD={REDACTED_VALUE}",
        "password=S3cret;next=1": f"password={REDACTED_VALUE}",
        "login password=S3cret": f"login password={REDACTED_VALUE}",
        "key:S3cret": f"key:{REDACTED_VALUE}",
        'hashed-password "S3cret"': f'hashed-password "{REDACTED_VALUE}',
        "GET /v1/x?key=S3cret&page=2": f"GET /v1/x?key={REDACTED_VALUE}",
        "Authorization: Bearer S3cret": f"Authorization: Bearer {REDACTED_VALUE}",
    }
    for line, expected in cases.items():
        assert redact_command_text(line) == (expected, True), line


def test_ordinary_colons_and_equals_are_left_alone():
    for line in (
        "clock set 12:30:00",
        "description uplink: eth1",
        "interface ethernet-1/1:1",
        "mtu=9000",
        "keyboard: us",
        "password:",
        "set key=",
    ):
        assert redact_command_text(line) == (line, False), line


def test_new_keywords_mask_the_rest_of_their_line():
    cases = {
        "set mgt-config users admin phash S3cret": "set mgt-config users admin phash",
        "psk S3cret": "psk",
        "wpa-psk ascii S3cret": "wpa-psk",
        "set network ike gateway g authentication pre-shared-key key S3cret": (
            "set network ike gateway g authentication"
        ),
        "token S3cret": "token",
    }
    for line, kept in cases.items():
        assert redact_command_text(line) == (f"{kept} {REDACTED_VALUE}", True), line


def test_a_url_password_is_masked():
    assert redact_command_text("remote https://admin:S3cret@10.0.0.1/api") == (
        f"remote https://admin:{REDACTED_VALUE}@10.0.0.1/api",
        True,
    )
    assert redact_command_text("remote https://10.0.0.1:8443/api") == (
        "remote https://10.0.0.1:8443/api",
        False,
    )


def test_json_inside_a_string_is_masked_as_a_structure():
    redacted, changed = redact_config(
        {"blob": '{"password":"S3cret","user":"admin"}', "list": '[{"admin-user": {"psk": "x"}}]'}
    )
    assert changed is True
    assert json.loads(redacted["blob"]) == {"password": REDACTED_VALUE, "user": "admin"}
    assert json.loads(redacted["list"]) == [{"admin-user": {"psk": REDACTED_VALUE}}]


def test_json_without_credentials_is_kept_byte_for_byte():
    text = '{"a":1,  "b":[2,3]}'
    assert redact_config(text) == (text, False)


def test_text_that_only_looks_like_json_is_line_masked():
    assert redact_config("[edit]\nset system root-authentication plain-text-password S3cret") == (
        f"[edit]\nset system root-authentication {REDACTED_VALUE}",
        True,
    )


def test_embedded_json_is_bounded_in_size_and_depth():
    # Past the size bound the text is not parsed; the line rule still masks it.
    big = json.dumps({"password": "S3cret", "pad": "x" * 300_000})
    redacted, changed = redact_command_text(big)
    assert changed is True
    assert redact_config(big) == (redacted, True)
    assert "S3cret" not in redacted
    # Nesting deeper than the JSON parser allows falls back to the line rule
    # instead of raising.
    deep = "[" * 100_000 + "]" * 100_000  # under the size bound, over the parser depth
    assert redact_config(deep) == (deep, False)
    # Nested JSON in JSON is parsed one level deeper each time, under the cap.
    # (Each level of escaping doubles the backslashes, so the size bound itself
    # keeps a real nesting shallow.)
    nested: object = {"password": "S3cret"}
    for _ in range(12):
        nested = json.dumps({"inner": nested})
    redacted_nested, changed = redact_config(nested)
    assert changed is True
    assert "S3cret" not in json.dumps(redacted_nested)


def test_is_keyword_token_is_the_line_rules_token():
    for text in ("password", "auth_key", "hashed-password", "PSK", "key-string"):
        assert is_keyword_token(text), text
    for text in ("passwords", "keyboard", "author", "key-id", "pass"):
        assert not is_keyword_token(text), text


# --- round 3 of the issue #1464 review ---


def test_json_escaped_text_is_masked_like_raw_text():
    # A run's output is JSON text (execution stores json.dumps of what the
    # driver returned); a bare string result starts with a quote.
    redacted, changed = redact_config({"output": json.dumps("hostname r1\npassword zebra")})
    assert changed is True
    assert json.loads(redacted["output"]) == f"hostname r1\npassword {REDACTED_VALUE}"


def test_escaped_text_the_parser_cannot_read_is_masked_per_escaped_line():
    cases = {
        # cut by a char cap: not JSON any more
        '{"config": "hostname r1\\npassword zebra\\nline vty': (
            f'{{"config": "hostname r1\\npassword {REDACTED_VALUE}\\nline vty'
        ),
        '{"o": "{\\"password\\": \\"S3cret\\"}': f'{{"o": "{{\\"password\\": \\"{REDACTED_VALUE}',
        '"a\\n\\tsecret S3cret\\nexit': f'"a\\n\\tsecret {REDACTED_VALUE}\\nexit',
    }
    for text, expected in cases.items():
        assert redact_command_text(text) == (expected, True), text


def test_an_escaped_line_break_ends_the_masked_value():
    # Without it, one keyword would mask the rest of a megabyte of escaped text.
    text = "password zebra\\nhostname r1\\r\\ninterface eth0\\rexit"
    assert redact_command_text(text) == (
        f"password {REDACTED_VALUE}\\nhostname r1\\r\\ninterface eth0\\rexit",
        True,
    )


def test_json_over_the_parse_bound_is_still_masked_line_by_line():
    running = "frr version 9\nhostname r1\npassword zebra\nenable password S3cret\n"
    output = json.dumps({"success": True, "config": running + "!\n" * 140_000})
    assert len(output) > 256 * 1024
    redacted, changed = redact_config({"output": output})
    assert changed is True
    assert "zebra" not in redacted["output"]
    assert "S3cret" not in redacted["output"]
    assert "hostname r1" in redacted["output"]


def test_xml_elements_named_by_a_keyword_have_their_text_masked():
    cases = {
        "<password>S3cret</password>": f"<password>{REDACTED_VALUE}</password>",
        "<entry name='admin'><phash>$1$S3cret</phash><permissions/></entry>": (
            f"<entry name='admin'><phash>{REDACTED_VALUE}</phash><permissions/></entry>"
        ),
        "<key>S3cret</key><name>r1</name><community>public</community>": (
            f"<key>{REDACTED_VALUE}</key><name>r1</name><community>{REDACTED_VALUE}</community>"
        ),
        # NETCONF reply shape
        "<rpc-reply><data><user><name>a</name><password>$6$S3cret</password></user>": (
            f"<rpc-reply><data><user><name>a</name><password>{REDACTED_VALUE}</password></user>"
        ),
        # an attribute is the `=` form: masked to the end of the line
        '<user name="a" password="S3cret"/><x/>': f'<user name="a" password="{REDACTED_VALUE}',
    }
    for text, expected in cases.items():
        assert redact_command_text(text) == (expected, True), text


def test_xml_elements_without_text_or_closing_tags_are_left_alone():
    for text in ("<password></password>", "</password>", "<password/>", "<community><name>"):
        assert redact_command_text(text) == (text, False), text


def test_a_python_repr_is_masked_but_a_quoted_field_name_is_not():
    cases = {
        "{'password': 'S3cret'}": f"{{'password': '{REDACTED_VALUE}",
        "configure failed: {'commands': ['x'], 'auth_key': 'S3cret'}": (
            f"configure failed: {{'commands': ['x'], 'auth_key': '{REDACTED_VALUE}"
        ),
        "Namespace(password='S3cret')": f"Namespace(password='{REDACTED_VALUE}",
        "password='S3cret'": f"password='{REDACTED_VALUE}",
    }
    for text, expected in cases.items():
        assert redact_command_text(text) == (expected, True), text
    for text in (
        "config failed schema validation at ['password']: must be at most 3 characters long",
        "config failed schema validation at ['auth'].password: must be at most 3 characters long",
        "config failed schema validation at the top level: needs the fields 'password', 'key'",
        "config failed schema validation at the top level: does not allow the field 'token'",
        "config failed schema validation at the top level: field 'md5' needs the field 'key'",
        "config failed schema validation at ['key']: must be of type integer",
        "config failed schema validation at x['secret']: must be of type integer",
    ):
        assert redact_command_text(text) == (text, False), text
