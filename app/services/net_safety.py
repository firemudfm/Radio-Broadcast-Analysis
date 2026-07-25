from __future__ import annotations

# SSRF defence for untrusted radio stream URLs.
#
# Radio Browser station data is untrusted input. Call
# validate_public_http_url() before ANY outbound connection to a stream URL,
# and call it again for EVERY redirect hop (capped at MAX_REDIRECTS).
# Validation fails closed: anything that cannot be proven to point at a
# global (public) unicast address raises NetSafetyError.

import ipaddress
import socket
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit, urlunsplit

MAX_REDIRECTS = 5

_ALLOWED_SCHEMES = frozenset({"http", "https"})
_DEFAULT_PORTS = {"http": 80, "https": 443}

IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address
Resolver = Callable[..., Iterable[Sequence[Any]]]


class NetSafetyError(ValueError):
    """Raised when a URL must not be fetched."""


@dataclass(frozen=True)
class SafeTarget:
    """A validated outbound target; every resolved address is global."""

    url: str
    scheme: str
    hostname: str
    port: int
    addresses: tuple[str, ...]


def validate_public_http_url(url: str, *, resolver: Resolver | None = None) -> SafeTarget:
    """Validate an untrusted URL for outbound fetching.

    Returns a :class:`SafeTarget` when the URL is an http(s) URL without
    credentials whose hostname resolves exclusively to global unicast
    addresses. Raises :class:`NetSafetyError` otherwise.
    """
    if not url or not url.strip():
        raise NetSafetyError("URL is empty")
    try:
        parts = urlsplit(url.strip())
    except ValueError as error:
        raise NetSafetyError(f"URL could not be parsed: {error}") from error

    scheme = parts.scheme.lower()
    if scheme not in _ALLOWED_SCHEMES:
        raise NetSafetyError(f"scheme {scheme or '(none)'!r} is not allowed, only http and https are")
    if parts.username is not None or parts.password is not None:
        raise NetSafetyError("URLs with embedded credentials (userinfo) are not allowed")

    hostname = parts.hostname
    if not hostname:
        raise NetSafetyError("URL has no hostname")

    try:
        port = parts.port
    except ValueError as error:
        raise NetSafetyError(f"URL has an invalid port: {error}") from error
    if port is None:
        port = _DEFAULT_PORTS[scheme]
    if not 1 <= port <= 65535:
        raise NetSafetyError(f"port {port} is not allowed, must be 1-65535")

    addresses = validate_ip_literal_or_resolved(hostname, resolver=resolver)

    host_display = f"[{hostname}]" if ":" in hostname else hostname
    netloc = host_display if port == _DEFAULT_PORTS[scheme] else f"{host_display}:{port}"
    normalized = urlunsplit((scheme, netloc, parts.path or "/", parts.query, ""))
    return SafeTarget(
        url=normalized,
        scheme=scheme,
        hostname=hostname,
        port=port,
        addresses=addresses,
    )


def validate_ip_literal_or_resolved(
    hostname: str, *, resolver: Resolver | None = None
) -> tuple[str, ...]:
    """Validate a hostname or IP literal, returning every global address.

    IP literals (dotted quad, IPv6, and numeric decimal/hex forms such as
    ``2130706433`` or ``0x7f000001``) are validated directly without touching
    DNS. Hostnames are resolved via ``resolver`` (default
    ``socket.getaddrinfo``) with ``AF_UNSPEC`` so both IPv4 and IPv6 records
    are checked. If ANY address is non-global the whole set is rejected.
    """
    if not hostname:
        raise NetSafetyError("hostname is empty")

    literal = _parse_ip_literal(hostname)
    if literal is not None:
        _validate_global_address(literal)
        return (str(literal),)

    raw_addresses = _resolve(hostname, resolver or socket.getaddrinfo)
    validated: list[str] = []
    for value in raw_addresses:
        address = _parse_resolved_address(hostname, value)
        _validate_global_address(address)
        text = str(address)
        if text not in validated:
            validated.append(text)
    if not validated:
        raise NetSafetyError(f"DNS resolution returned no addresses for {hostname!r}")
    return tuple(validated)


def _parse_ip_literal(hostname: str) -> IPAddress | None:
    """Return the address when ``hostname`` is an IP literal, else None."""
    host = hostname.strip()
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        pass
    lowered = host.lower()
    if lowered.isascii() and (lowered.isdigit() or lowered.startswith("0x")):
        try:
            return ipaddress.ip_address(int(lowered, 0))
        except ValueError:
            return None
    return None


def _resolve(hostname: str, resolver: Resolver) -> list[str]:
    """Resolve ``hostname`` to raw address strings via getaddrinfo semantics."""
    try:
        entries = list(resolver(hostname, None, socket.AF_UNSPEC, socket.SOCK_STREAM))
    except NetSafetyError:
        raise
    except Exception as error:
        raise NetSafetyError(f"DNS resolution failed for {hostname!r}: {error}") from error
    if not entries:
        raise NetSafetyError(f"DNS resolution returned no addresses for {hostname!r}")
    addresses: list[str] = []
    for entry in entries:
        try:
            sockaddr = entry[4]
            candidate = sockaddr[0]
        except (IndexError, KeyError, TypeError) as error:
            raise NetSafetyError(
                f"resolver returned a malformed result for {hostname!r}"
            ) from error
        if not isinstance(candidate, str):
            raise NetSafetyError(f"resolver returned a malformed address for {hostname!r}")
        addresses.append(candidate)
    return addresses


def _parse_resolved_address(hostname: str, value: str) -> IPAddress:
    try:
        return ipaddress.ip_address(value)
    except ValueError as error:
        raise NetSafetyError(
            f"resolver returned an invalid address {value!r} for {hostname!r}"
        ) from error


def _validate_global_address(address: IPAddress) -> None:
    """Reject ``address`` unless it is a global unicast address.

    IPv4-mapped IPv6 addresses (::ffff:a.b.c.d) are unwrapped and the inner
    IPv4 address is validated as well.
    """
    candidates: list[IPAddress] = [address]
    mapped = address.ipv4_mapped if isinstance(address, ipaddress.IPv6Address) else None
    if mapped is not None:
        candidates.append(mapped)
    for candidate in candidates:
        reason = _rejection_reason(candidate)
        if reason is not None:
            raise NetSafetyError(f"address {address} is not allowed: {reason}")


def _rejection_reason(address: IPAddress) -> str | None:
    if address.is_loopback:
        return "loopback address"
    if address.is_link_local:
        return "link-local address"
    if address.is_multicast:
        return "multicast address"
    if address.is_unspecified:
        return "unspecified address"
    if address.is_reserved:
        return "reserved address"
    if address.is_private:
        return "private address"
    if not address.is_global:
        return "not a global address"
    return None
