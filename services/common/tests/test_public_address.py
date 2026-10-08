"""herd_common.public_address: the one public-address predicate, shared by
ai-orchestrator's web documentation fetch and integration's webhook
destination check."""

import socket

import pytest
from herd_common import public_address
from herd_common.public_address import default_resolver, is_public_address


@pytest.mark.parametrize(
    "address,why",
    [
        ("127.0.0.1", "loopback"),
        ("127.255.255.254", "loopback, high end"),
        ("10.0.0.5", "RFC 1918 class A"),
        ("172.16.0.1", "RFC 1918 class B"),
        ("192.168.1.27", "RFC 1918 class C"),
        ("169.254.169.254", "link-local metadata service"),
        ("100.64.0.1", "shared address space, low end"),
        ("100.127.255.254", "shared address space, high end"),
        ("224.0.0.1", "multicast"),
        ("0.0.0.0", "unspecified"),
        ("240.0.0.1", "reserved"),
        ("::1", "IPv6 loopback"),
        ("fc00::1", "IPv6 unique local, fc half"),
        ("fd00::1", "IPv6 unique local, fd half"),
        ("fe80::1", "IPv6 link-local"),
        ("ff02::1", "IPv6 multicast"),
        ("::", "IPv6 unspecified"),
        ("::ffff:127.0.0.1", "IPv4-mapped loopback"),
        ("::ffff:93.184.216.34", "IPv4-mapped, refused whatever it maps to"),
        ("2002:0a00:0005::1", "6to4 wrapping 10.0.0.5"),
        ("2001:0:4136:e378:8000:63bf:3fff:fdd2", "Teredo"),
        ("not-an-address", "unparseable"),
        ("", "empty"),
    ],
)
def test_refused_address_classes(address, why):
    assert is_public_address(address) is False, why


@pytest.mark.parametrize(
    "address",
    ["93.184.216.34", "8.8.8.8", "2606:2800:220:1:248:1893:25c8:1946", "100.63.255.255"],
)
def test_public_addresses_are_allowed(address):
    assert is_public_address(address) is True


def test_default_resolver_returns_every_answer_sorted_and_deduplicated(monkeypatch):
    seen = []

    def fake_getaddrinfo(host, port, proto=0):
        seen.append((host, port, proto))
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443)),
            (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("::1", 443, 0, 0)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.5", 443)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443)),
        ]

    monkeypatch.setattr(public_address.socket, "getaddrinfo", fake_getaddrinfo)
    assert default_resolver("hooks.example.com") == ["10.0.0.5", "93.184.216.34", "::1"]
    assert seen == [("hooks.example.com", 443, socket.IPPROTO_TCP)]


def test_default_resolver_propagates_resolution_errors(monkeypatch):
    def fake_getaddrinfo(host, port, proto=0):
        raise socket.gaierror(socket.EAI_NONAME, "Name or service not known")

    monkeypatch.setattr(public_address.socket, "getaddrinfo", fake_getaddrinfo)
    with pytest.raises(OSError):
        default_resolver("nowhere.invalid")
