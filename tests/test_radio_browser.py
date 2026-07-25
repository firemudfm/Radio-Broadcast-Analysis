from __future__ import annotations

import json
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest

from app.services.radio_browser import (
    RadioBrowserClient,
    RadioBrowserError,
    RadioBrowserHTTPStatus,
    coerce_station,
)

MIRRORS = ["m1.example.org", "m2.example.org", "m3.example.org"]

SEED_MIRRORS = {
    "de1.api.radio-browser.info",
    "de2.api.radio-browser.info",
    "fi1.api.radio-browser.info",
}

STATION_RAW: dict[str, Any] = {
    "stationuuid": "uuid-1",
    "name": "Hertz 87.9",
    "countrycode": "DE",
    "state": "NRW",
    "codec": "MP3",
    "favicon": None,
    "homepage": "https://hertz879.de",
    "language": "german",
    "languagecodes": "de",
    "tags": "college,indie",
    "votes": "123",
    "clickcount": "45",
    "bitrate": "128",
    "hls": 1,
    "lastcheckok": "1",
}


def station_body(*stations: dict[str, Any]) -> bytes:
    return json.dumps(list(stations)).encode()


class FakeClock:
    def __init__(self) -> None:
        self.value = 1000.0

    def __call__(self) -> float:
        return self.value


class FakeTransport:
    """Scripted http_get double: raises scripted exceptions, returns scripted bodies."""

    def __init__(self, script: list[Any]) -> None:
        self.script = list(script)
        self.calls: list[tuple[str, dict[str, str], float, int]] = []

    def __call__(self, url: str, headers: dict[str, str], timeout: float, max_bytes: int) -> bytes:
        self.calls.append((url, headers, timeout, max_bytes))
        if not self.script:
            raise AssertionError(f"unexpected transport call: {url}")
        step = self.script.pop(0)
        if isinstance(step, BaseException):
            raise step
        return step

    @property
    def urls(self) -> list[str]:
        return [call[0] for call in self.calls]

    def host(self, index: int) -> str:
        return urlsplit(self.calls[index][0]).hostname or ""


def make_client(transport: FakeTransport, **overrides: Any) -> RadioBrowserClient:
    options: dict[str, Any] = {
        "srv_lookup": lambda: list(MIRRORS),
        "http_get": transport,
    }
    options.update(overrides)
    return RadioBrowserClient(**options)


def test_mirror_failover_on_timeout() -> None:
    transport = FakeTransport([TimeoutError("read timed out"), station_body(STATION_RAW)])
    client = make_client(transport)
    stations = client.search_stations(name="hertz")
    assert len(stations) == 1
    assert len(transport.calls) == 2
    assert transport.host(0) != transport.host(1)
    assert client.last_mirror == transport.host(1)


def test_retry_on_http_429_then_success() -> None:
    transport = FakeTransport([RadioBrowserHTTPStatus(429, b"slow down"), b"[]"])
    client = make_client(transport)
    assert client.countries() == []
    assert len(transport.calls) == 2
    assert client.last_mirror == transport.host(1)


def test_invalid_json_moves_to_next_mirror() -> None:
    transport = FakeTransport([b"<html>not json</html>", b'[{"name": "Germany"}]'])
    client = make_client(transport)
    assert client.countries() == [{"name": "Germany"}]
    assert len(transport.calls) == 2


def test_http_404_raises_immediately_without_retry() -> None:
    transport = FakeTransport([RadioBrowserHTTPStatus(404, b"missing")])
    client = make_client(transport)
    with pytest.raises(RadioBrowserError, match="404"):
        client.countries()
    assert len(transport.calls) == 1


def test_exhausted_attempts_raise_with_mirror_names() -> None:
    transport = FakeTransport([RadioBrowserHTTPStatus(503, b"")] * 3)
    client = make_client(transport)
    with pytest.raises(RadioBrowserError) as exc_info:
        client.countries()
    assert len(transport.calls) == 3
    message = str(exc_info.value)
    for index in range(3):
        assert transport.host(index) in message


def test_station_field_coercion() -> None:
    transport = FakeTransport([station_body(STATION_RAW)])
    client = make_client(transport)
    station = client.station_by_uuid("uuid-1")
    assert station is not None
    assert station["votes"] == 123
    assert station["clickcount"] == 45
    assert station["bitrate"] == 128
    assert station["hls"] is True
    assert station["lastcheckok"] is True
    assert station["favicon"] == ""
    assert station["stationuuid"] == "uuid-1"
    assert station["tags"] == "college,indie"


def test_coerce_station_helper_defaults() -> None:
    station = coerce_station({"name": "Bare"})
    assert station["votes"] == 0
    assert station["clickcount"] == 0
    assert station["bitrate"] == 0
    assert station["hls"] is False
    assert station["lastcheckok"] is False
    assert station["stationuuid"] == ""
    assert station["countrycode"] == ""


def test_search_params_serialized_in_url() -> None:
    transport = FakeTransport([b"[]"])
    client = make_client(transport)
    client.search_stations(offset=20, limit=5, tag_list=["college", "indie"], is_https=True)
    query = parse_qs(urlsplit(transport.urls[0]).query)
    assert query["offset"] == ["20"]
    assert query["limit"] == ["5"]
    assert query["tagList"] == ["college,indie"]
    assert query["is_https"] == ["true"]
    assert query["hidebroken"] == ["true"]
    assert query["order"] == ["votes"]
    assert query["reverse"] == ["true"]
    assert urlsplit(transport.urls[0]).path == "/json/stations/search"


def test_search_cache_deduplicates_identical_queries() -> None:
    transport = FakeTransport([station_body(STATION_RAW), b"[]"])
    client = make_client(transport)
    first = client.search_stations(tag="college")
    second = client.search_stations(tag="college")
    assert first == second
    assert len(transport.calls) == 1
    client.search_stations(tag="jazz")
    assert len(transport.calls) == 2


def test_search_cache_expires_with_clock() -> None:
    clock = FakeClock()
    transport = FakeTransport([b"[]", b"[]"])
    client = make_client(transport, now=clock, search_cache_seconds=10.0)
    client.search_stations(tag="college")
    clock.value += 11.0
    client.search_stations(tag="college")
    assert len(transport.calls) == 2


def test_countries_cached() -> None:
    transport = FakeTransport([b'[{"name": "Germany"}]'])
    client = make_client(transport)
    assert client.countries() == client.countries()
    assert len(transport.calls) == 1


def test_srv_lookup_preferred_over_host_resolver() -> None:
    def fail_host_resolver(name: str) -> list[str]:
        raise AssertionError("host_resolver must not be called when SRV lookup works")

    transport = FakeTransport([b"[]"])
    client = make_client(
        transport,
        srv_lookup=lambda: ["srv1.example.org"],
        host_resolver=fail_host_resolver,
    )
    client.countries()
    assert transport.host(0) == "srv1.example.org"


def test_reverse_lookup_fallback_when_srv_fails() -> None:
    def broken_srv() -> list[str]:
        raise OSError("no srv answer")

    def host_resolver(name: str) -> list[str]:
        assert name == "all.api.radio-browser.info"
        return ["192.0.2.1", "192.0.2.2", "192.0.2.1"]

    def reverse_resolver(ip: str) -> str:
        if ip == "192.0.2.2":
            raise OSError("no PTR record")
        return "rev1.example.org"

    transport = FakeTransport([b"[]"])
    client = make_client(
        transport,
        srv_lookup=broken_srv,
        host_resolver=host_resolver,
        reverse_resolver=reverse_resolver,
    )
    client.countries()
    assert len(transport.calls) == 1
    assert transport.host(0) == "rev1.example.org"


def test_seed_mirrors_used_when_all_discovery_fails() -> None:
    def broken(*args: Any) -> Any:
        raise OSError("dns down")

    transport = FakeTransport([b"[]"])
    client = make_client(transport, srv_lookup=broken, host_resolver=broken)
    client.countries()
    assert transport.host(0) in SEED_MIRRORS


def test_user_agent_and_accept_headers_sent() -> None:
    transport = FakeTransport([b"[]"])
    client = make_client(transport)
    client.countries()
    headers = transport.calls[0][1]
    assert headers["User-Agent"] == "FireMudRadioMonitor/0.4 (+EC2 pilot)"
    assert headers["Accept"] == "application/json"


def test_oversized_response_raises() -> None:
    transport = FakeTransport([b"x" * 65])
    client = make_client(transport, max_response_bytes=64)
    with pytest.raises(RadioBrowserError, match="65 bytes"):
        client.countries()
    assert len(transport.calls) == 1


def test_resolve_url_never_cached() -> None:
    body = json.dumps({"ok": True, "url": "https://stream.example.org/live"}).encode()
    transport = FakeTransport([body, body])
    client = make_client(transport)
    first = client.resolve_url("uuid-1")
    second = client.resolve_url("uuid-1")
    assert first == second
    assert first["url"] == "https://stream.example.org/live"
    assert len(transport.calls) == 2
    assert urlsplit(transport.urls[0]).path == "/json/url/uuid-1"


def test_station_by_uuid_empty_result_returns_none_and_caches() -> None:
    transport = FakeTransport([b"[]"])
    client = make_client(transport)
    assert client.station_by_uuid("missing-uuid") is None
    assert client.station_by_uuid("missing-uuid") is None
    assert len(transport.calls) == 1
    query = parse_qs(urlsplit(transport.urls[0]).query)
    assert query["uuids"] == ["missing-uuid"]
