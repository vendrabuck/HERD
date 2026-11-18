"""herd_common.config_redaction: the masked display copy of a configuration."""

import copy

from herd_common.config_redaction import REDACTED_VALUE, redact_command_text, redact_config

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
    # Pinned so a widening of either rule is a deliberate change: a secret
    # under a key the name pattern does not match, and a secret in free text
    # with no keyword before it, are not masked (the module docstring's Limits).
    redacted, changed = redact_config(
        {"psk": "Psk5ecret", "commands": ["neighbor 192.0.2.1 remote-as 1 Fr3eText"]}
    )
    assert changed is False
    assert redacted["psk"] == "Psk5ecret"
    assert redacted["commands"] == ["neighbor 192.0.2.1 remote-as 1 Fr3eText"]
