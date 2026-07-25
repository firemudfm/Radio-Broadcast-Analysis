from __future__ import annotations

import ipaddress
import socket
from typing import Any

import pytest

from app.services.net_safety import (
    MAX_REDIRECTS,
    NetSafetyError,
    SafeTarget,
    validate_ip_literal_or_resolved,
    validate_public_http_url,
)

PUBLIC_V4 = "93.184.216.34"
PUBLIC_V6 = "2606:2800:220:1:248:1893:25c8:1946"


def make_resolver(*ips: str):
    """Build a getaddrinfo-shaped fake resolver returning the given IPs."""

    def resolver(host: str, port: Any, *args: Any, **kwargs: Any) -> list[tuple[Any, ...]]:
        del host, port, args, kwargs
        results: list[tuple[Any, ...]] = []
        for ip in ips:
            if ":" in ip:
                results.append((socket.AF_INET6, socket.SOCK_STREAM, 6, "", (ip, 0, 0, 0)))
            else:
                results.append((socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 0)))
        return results

    return resolver


def failing_resolver(host: str, port: Any, *args: Any, **kwargs: Any) -> list[tuple[Any, ...]]:
    raise socket.gaierror(socket.EAI_NONAME, "Name or service not known")


# --- scheme / URL shape ------------------------------------------------------


def test_error_is_a_value_error() -> None:
    assert issubclass(NetSafetyError, ValueError)


def test_max_redirects_constant() -> None:
    assert MAX_REDIRECTS == 5


@pytest.mark.parametrize("url", ["ftp://x", "file:///etc/passwd", "gopher://host/"])
def test_rejects_non_http_schemes(url: str) -> None:
    with pytest.raises(NetSafetyError):
        validate_public_http_url(url, resolver=make_resolver(PUBLIC_V4))


@pytest.mark.parametrize("url", ["http://user:pass@host/", "http://user@host/"])
def test_rejects_embedded_credentials(url: str) -> None:
    # Resolver returns a public IP, so the only reason to reject is userinfo.
    with pytest.raises(NetSafetyError):
        validate_public_http_url(url, resolver=make_resolver(PUBLIC_V4))


@pytest.mark.parametrize("url", ["http://", "http:///stream", ""])
def test_rejects_missing_hostname(url: str) -> None:
    with pytest.raises(NetSafetyError):
        validate_public_http_url(url, resolver=make_resolver(PUBLIC_V4))


def test_rejects_port_zero() -> None:
    with pytest.raises(NetSafetyError):
        validate_public_http_url("http://public.example:0/x", resolver=make_resolver(PUBLIC_V4))


def test_rejects_out_of_range_port() -> None:
    with pytest.raises(NetSafetyError):
        validate_public_http_url("http://public.example:70000/x", resolver=make_resolver(PUBLIC_V4))


# --- IP literal rejection (resolver returns a PUBLIC IP on purpose: the
# --- literal itself must be validated, never trusted to DNS) -----------------


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/s",
        "http://0.0.0.0/",
        "http://169.254.169.254/latest",
        "http://10.1.2.3/",
        "http://192.168.1.5/",
        "http://172.16.9.1/",
        "http://[::1]/",
        "http://[::ffff:127.0.0.1]/",
        "http://[fd00:ec2::254]/",
        "http://2130706433/",
        "http://0x7f000001/",
    ],
)
def test_rejects_non_global_ip_literals(url: str) -> None:
    with pytest.raises(NetSafetyError):
        validate_public_http_url(url, resolver=make_resolver(PUBLIC_V4))


def test_aws_ipv4_metadata_endpoint_is_link_local_and_rejected() -> None:
    assert ipaddress.ip_address("169.254.169.254").is_link_local
    with pytest.raises(NetSafetyError):
        validate_public_http_url(
            "http://169.254.169.254/latest/meta-data/", resolver=make_resolver(PUBLIC_V4)
        )


def test_aws_ipv6_metadata_endpoint_is_unique_local_and_rejected() -> None:
    # fc00::/7 unique-local: ipaddress marks ULA as private, not global.
    address = ipaddress.ip_address("fd00:ec2::254")
    assert address.is_private
    assert not address.is_global
    with pytest.raises(NetSafetyError):
        validate_public_http_url("http://[fd00:ec2::254]/", resolver=make_resolver(PUBLIC_V4))


def test_ipv4_mapped_ipv6_is_unwrapped_and_rejected() -> None:
    with pytest.raises(NetSafetyError):
        validate_public_http_url("http://[::ffff:127.0.0.1]/", resolver=make_resolver(PUBLIC_V4))


@pytest.mark.parametrize("url", ["http://2130706433/", "http://0x7f000001/"])
def test_rejects_decimal_and_hex_loopback_forms_directly(url: str) -> None:
    # These parse as 127.0.0.1; a public-returning resolver must not save them.
    with pytest.raises(NetSafetyError):
        validate_public_http_url(url, resolver=make_resolver(PUBLIC_V4))


# --- resolution behaviour ----------------------------------------------------


def test_rejects_localhost_via_resolution() -> None:
    with pytest.raises(NetSafetyError):
        validate_public_http_url("http://localhost/s", resolver=make_resolver("127.0.0.1"))


def test_rejects_hostname_resolving_to_private_ip() -> None:
    with pytest.raises(NetSafetyError):
        validate_public_http_url("http://rebind.example/s", resolver=make_resolver("10.0.0.5"))


def test_rejects_mixed_public_and_private_records() -> None:
    # DNS returning one public and one private record must reject the URL.
    with pytest.raises(NetSafetyError):
        validate_public_http_url(
            "http://dual.example/stream", resolver=make_resolver(PUBLIC_V4, "10.0.0.5")
        )


def test_rejects_when_resolution_fails() -> None:
    with pytest.raises(NetSafetyError):
        validate_public_http_url("http://nxdomain.example/s", resolver=failing_resolver)


def test_rejects_when_resolution_returns_nothing() -> None:
    with pytest.raises(NetSafetyError):
        validate_public_http_url("http://empty.example/s", resolver=make_resolver())


def test_resolver_is_called_with_af_unspec() -> None:
    calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    def resolver(host: str, port: Any, *args: Any, **kwargs: Any) -> list[tuple[Any, ...]]:
        calls.append((args, kwargs))
        return make_resolver(PUBLIC_V4)(host, port)

    validate_public_http_url("http://public.example/x", resolver=resolver)
    assert calls
    args, kwargs = calls[0]
    family = args[0] if args else kwargs.get("family")
    assert family == socket.AF_UNSPEC


# --- accepted targets --------------------------------------------------------


def test_accepts_public_ipv4_literal() -> None:
    target = validate_public_http_url("http://93.184.216.34/stream", resolver=make_resolver(PUBLIC_V4))
    assert isinstance(target, SafeTarget)
    assert target.scheme == "http"
    assert target.hostname == "93.184.216.34"
    assert target.port == 80
    assert target.addresses == (PUBLIC_V4,)
    assert target.url == "http://93.184.216.34/stream"


def test_accepts_public_hostname_with_dual_stack_records() -> None:
    target = validate_public_http_url(
        "https://public.example/stream.mp3", resolver=make_resolver(PUBLIC_V4, PUBLIC_V6)
    )
    assert target.scheme == "https"
    assert target.hostname == "public.example"
    assert target.port == 443
    assert target.addresses == (PUBLIC_V4, PUBLIC_V6)
    assert target.url == "https://public.example/stream.mp3"


def test_accepts_weird_stream_port() -> None:
    target = validate_public_http_url("http://public.example:8000/x", resolver=make_resolver(PUBLIC_V4))
    assert target.port == 8000
    assert target.url == "http://public.example:8000/x"


def test_safe_target_carries_all_resolved_addresses() -> None:
    second_v4 = "151.101.1.140"
    target = validate_public_http_url(
        "http://cdn.example/live", resolver=make_resolver(PUBLIC_V4, second_v4, PUBLIC_V6)
    )
    assert target.addresses == (PUBLIC_V4, second_v4, PUBLIC_V6)


# --- validate_ip_literal_or_resolved directly --------------------------------


def test_validate_hostname_returns_every_address() -> None:
    result = validate_ip_literal_or_resolved(
        "public.example", resolver=make_resolver(PUBLIC_V4, PUBLIC_V6)
    )
    assert result == (PUBLIC_V4, PUBLIC_V6)


def test_validate_public_literal_without_dns() -> None:
    def exploding_resolver(*args: Any, **kwargs: Any) -> list[tuple[Any, ...]]:
        raise socket.gaierror("must not be used for literals")

    assert validate_ip_literal_or_resolved(PUBLIC_V4, resolver=exploding_resolver) == (PUBLIC_V4,)


@pytest.mark.parametrize(
    "hostname",
    ["192.168.0.1", "127.0.0.1", "::1", "fd00:ec2::254", "2130706433", "0x7f000001"],
)
def test_validate_literal_rejects_non_global(hostname: str) -> None:
    with pytest.raises(NetSafetyError):
        validate_ip_literal_or_resolved(hostname, resolver=make_resolver(PUBLIC_V4))


def test_validate_empty_hostname_rejected() -> None:
    with pytest.raises(NetSafetyError):
        validate_ip_literal_or_resolved("", resolver=make_resolver(PUBLIC_V4))
