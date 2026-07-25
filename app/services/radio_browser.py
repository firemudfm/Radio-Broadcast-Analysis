"""Client for the community Radio Browser API (https://api.radio-browser.info).

Backend-only helper for the distributed Radio Browser mirror pool:

- Mirrors are discovered at runtime (DNS SRV first, then A/AAAA plus reverse
  lookups of ``all.api.radio-browser.info``) and re-discovered every
  ``mirror_refresh_seconds`` so load keeps spreading across the pool instead
  of pinning a single host.
- Every request walks a freshly shuffled copy of the mirror list and fails
  over on timeouts, connection errors, HTTP 429/5xx, and invalid JSON.
- Read endpoints are cached in-process. ``resolve_url`` is deliberately never
  cached because each call registers a click for the station on the API side.

This module must never call ``db.radio-browser.info`` or the full-dump
``/json/stations`` / ``all.json`` endpoints; only the filtered and paginated
endpoints below are used.
"""

from __future__ import annotations

import http.client
import json
import random
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterable, Sequence
from typing import Any

_SRV_RECORD_NAME = "_api._tcp.radio-browser.info"
_ALL_MIRRORS_HOSTNAME = "all.api.radio-browser.info"

# Last-resort seed used only when both SRV discovery and the A/AAAA + reverse
# lookup fallback fail (for example a DNS outage on the host). It is NOT a
# permanent mirror pin: discovery runs again after ``mirror_refresh_seconds``.
_FALLBACK_MIRRORS = (
    "de1.api.radio-browser.info",
    "de2.api.radio-browser.info",
    "fi1.api.radio-browser.info",
)

_STATION_INT_FIELDS = ("votes", "clickcount", "bitrate")
_STATION_BOOL_FIELDS = ("hls", "lastcheckok")
_STATION_STR_FIELDS = (
    "stationuuid",
    "name",
    "countrycode",
    "state",
    "codec",
    "favicon",
    "homepage",
    "language",
    "languagecodes",
    "tags",
)

HttpGet = Callable[[str, dict[str, str], float, int], bytes]
SrvLookup = Callable[[], list[str]]
HostResolver = Callable[[str], list[str]]
ReverseResolver = Callable[[str], str]
Clock = Callable[[], float]


class RadioBrowserError(RuntimeError):
    """Raised when the Radio Browser API cannot satisfy a request."""


class RadioBrowserHTTPStatus(RadioBrowserError):
    """Non-200 HTTP response from a mirror; carries the status code and body.

    The default transport raises this for every non-200 response. Injected
    ``http_get`` callables must follow the same contract so the retry logic
    can distinguish retryable statuses (429, 5xx) from hard client errors.
    """

    def __init__(self, code: int, body: bytes = b"") -> None:
        super().__init__(f"HTTP {code}")
        self.code = code
        self.body = body


def default_http_get(url: str, headers: dict[str, str], timeout: float, max_bytes: int) -> bytes:
    """Fetch ``url`` with urllib, capping the response at ``max_bytes``."""
    request = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # nosec B310 (https mirror URLs from the vetted pool)
            body = response.read(max_bytes + 1)
    except urllib.error.HTTPError as exc:
        try:
            detail = exc.read()
        except (OSError, http.client.HTTPException):
            detail = b""
        finally:
            exc.close()
        raise RadioBrowserHTTPStatus(exc.code, detail) from exc
    if len(body) > max_bytes:
        raise RadioBrowserError(f"response from {url} exceeded {max_bytes} bytes")
    return body


def _default_srv_lookup() -> list[str]:
    """Resolve the Radio Browser SRV record; needs the optional dnspython."""
    try:
        import dns.resolver  # noqa: PLC0415  # optional dep; stdlib has no SRV support
    except ImportError as exc:
        raise RadioBrowserError("dnspython not installed, SRV lookup unavailable") from exc
    answers = dns.resolver.resolve(_SRV_RECORD_NAME, "SRV")
    return [str(answer.target).rstrip(".") for answer in answers]


def _default_host_resolver(name: str) -> list[str]:
    infos = socket.getaddrinfo(name, None, proto=socket.IPPROTO_TCP)
    return [info[4][0] for info in infos]


def _default_reverse_resolver(ip: str) -> str:
    return socket.gethostbyaddr(ip)[0]


def _dedupe(values: Iterable[str]) -> list[str]:
    output: list[str] = []
    for value in values:
        item = value.strip()
        if item and item not in output:
            output.append(item)
    return output


def _format_param(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _to_int(value: object) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return 0
        try:
            return int(float(text))
        except ValueError:
            return 0
    return 0


def _to_bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return False


def _to_text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return str(value)


def coerce_station(raw: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of ``raw`` with numeric, boolean, and string fields normalized.

    Radio Browser mirrors are inconsistent about types (votes as strings,
    hls as 0/1, missing favicons), so every station dict passes through here
    before it leaves this module.
    """
    station = dict(raw)
    for field in _STATION_INT_FIELDS:
        station[field] = _to_int(station.get(field))
    for field in _STATION_BOOL_FIELDS:
        station[field] = _to_bool(station.get(field))
    for field in _STATION_STR_FIELDS:
        station[field] = _to_text(station.get(field))
    return station


def _expect_dict_list(payload: Any, path: str) -> list[dict[str, Any]]:
    if not isinstance(payload, list) or any(not isinstance(item, dict) for item in payload):
        raise RadioBrowserError(f"unexpected payload for {path}: expected a list of objects")
    return payload


class RadioBrowserClient:
    """Thread-safe Radio Browser API client with mirror failover and caching."""

    def __init__(
        self,
        *,
        user_agent: str = "FireMudRadioMonitor/0.4 (+EC2 pilot)",
        request_timeout_seconds: float = 10.0,
        max_attempts: int = 3,
        max_response_bytes: int = 8_000_000,
        mirror_refresh_seconds: float = 1800.0,
        country_cache_seconds: float = 21600.0,
        list_cache_seconds: float = 21600.0,
        search_cache_seconds: float = 600.0,
        station_cache_seconds: float = 300.0,
        http_get: HttpGet | None = None,
        srv_lookup: SrvLookup | None = None,
        host_resolver: HostResolver | None = None,
        reverse_resolver: ReverseResolver | None = None,
        now: Clock | None = None,
    ) -> None:
        if max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        self._user_agent = user_agent
        self._request_timeout_seconds = request_timeout_seconds
        self._max_attempts = max_attempts
        self._max_response_bytes = max_response_bytes
        self._mirror_refresh_seconds = mirror_refresh_seconds
        self._country_cache_seconds = country_cache_seconds
        self._list_cache_seconds = list_cache_seconds
        self._search_cache_seconds = search_cache_seconds
        self._station_cache_seconds = station_cache_seconds
        self._http_get = http_get or default_http_get
        self._srv_lookup = srv_lookup or _default_srv_lookup
        self._host_resolver = host_resolver or _default_host_resolver
        self._reverse_resolver = reverse_resolver or _default_reverse_resolver
        self._now = now or time.monotonic
        self._lock = threading.Lock()
        self._cache: dict[tuple[object, ...], tuple[float, Any]] = {}
        self._mirror_cache: list[str] | None = None
        self._mirror_expires = 0.0
        self.last_mirror: str | None = None

    # -- public API ---------------------------------------------------------

    def countries(self) -> list[dict[str, Any]]:
        return self._cached_list(("countries",), self._country_cache_seconds, "/json/countries")

    def languages(self) -> list[dict[str, Any]]:
        return self._cached_list(("languages",), self._list_cache_seconds, "/json/languages")

    def tags(self) -> list[dict[str, Any]]:
        return self._cached_list(("tags",), self._list_cache_seconds, "/json/tags")

    def codecs(self) -> list[dict[str, Any]]:
        return self._cached_list(("codecs",), self._list_cache_seconds, "/json/codecs")

    def search_stations(
        self,
        *,
        name: str | None = None,
        countrycode: str | None = None,
        state: str | None = None,
        language: str | None = None,
        tag: str | None = None,
        tag_list: Sequence[str] | str | None = None,
        codec: str | None = None,
        bitrate_min: int | None = None,
        bitrate_max: int | None = None,
        is_https: bool | None = None,
        hidebroken: bool = True,
        order: str = "votes",
        reverse: bool = True,
        offset: int = 0,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        params: dict[str, object] = {
            "hidebroken": hidebroken,
            "order": order,
            "reverse": reverse,
            "offset": offset,
            "limit": limit,
        }
        optional: dict[str, object | None] = {
            "name": name,
            "countrycode": countrycode,
            "state": state,
            "language": language,
            "tag": tag,
            "codec": codec,
            "bitrateMin": bitrate_min,
            "bitrateMax": bitrate_max,
            "is_https": is_https,
        }
        for key, value in optional.items():
            if value is not None:
                params[key] = value
        if tag_list is not None:
            params["tagList"] = tag_list if isinstance(tag_list, str) else ",".join(tag_list)

        serialized = self._serialize_params(params)
        cache_key: tuple[object, ...] = ("search", frozenset(serialized.items()))
        path = "/json/stations/search"

        def load() -> list[dict[str, Any]]:
            payload = _expect_dict_list(self._get_json(path, params), path)
            return [coerce_station(item) for item in payload]

        stations = self._cached(cache_key, self._search_cache_seconds, load)
        return [dict(item) for item in stations]

    def station_by_uuid(self, station_uuid: str) -> dict[str, Any] | None:
        uuid = station_uuid.strip()
        if not uuid:
            raise ValueError("station_uuid must not be empty")
        path = "/json/stations/byuuid"

        def load() -> dict[str, Any] | None:
            payload = _expect_dict_list(self._get_json(path, {"uuids": uuid}), path)
            if not payload:
                return None
            return coerce_station(payload[0])

        station = self._cached(("station", uuid), self._station_cache_seconds, load)
        return dict(station) if station is not None else None

    def resolve_url(self, station_uuid: str) -> dict[str, Any]:
        uuid = station_uuid.strip()
        if not uuid:
            raise ValueError("station_uuid must not be empty")
        # Never cached: every call counts a click for the station on the API side.
        path = f"/json/url/{urllib.parse.quote(uuid, safe='')}"
        payload = self._get_json(path, None)
        if not isinstance(payload, dict):
            raise RadioBrowserError(f"unexpected payload for {path}: expected an object")
        return payload

    # -- mirror discovery ----------------------------------------------------

    def _mirrors(self) -> list[str]:
        now = self._now()
        with self._lock:
            if self._mirror_cache is not None and self._mirror_expires > now:
                return list(self._mirror_cache)
        mirrors = self._discover_mirrors()
        with self._lock:
            self._mirror_cache = mirrors
            self._mirror_expires = self._now() + self._mirror_refresh_seconds
        return list(mirrors)

    def _discover_mirrors(self) -> list[str]:
        try:
            hosts = _dedupe(self._srv_lookup())
        except Exception:
            hosts = []
        if hosts:
            return hosts
        try:
            ips = _dedupe(self._host_resolver(_ALL_MIRRORS_HOSTNAME))
        except Exception:
            ips = []
        names: list[str] = []
        for ip in ips:
            try:
                name = self._reverse_resolver(ip)
            except Exception:
                continue  # IPs without a PTR record are dropped
            if name:
                names.append(name)
        names = _dedupe(names)
        if names:
            return names
        # See _FALLBACK_MIRRORS: a last-resort seed while DNS is unavailable,
        # not a permanent single-mirror configuration.
        return list(_FALLBACK_MIRRORS)

    # -- request core ---------------------------------------------------------

    def _get_json(self, path: str, params: dict[str, object] | None = None) -> Any:
        query = urllib.parse.urlencode(sorted(self._serialize_params(params).items()))
        order = self._mirrors()
        random.shuffle(order)
        plan = [order[index % len(order)] for index in range(self._max_attempts)]
        tried: list[str] = []
        last_error: Exception | None = None
        for mirror in plan:
            tried.append(mirror)
            url = f"https://{mirror}{path}"
            if query:
                url = f"{url}?{query}"
            headers = {"User-Agent": self._user_agent, "Accept": "application/json"}
            try:
                body = self._http_get(
                    url, headers, self._request_timeout_seconds, self._max_response_bytes
                )
            except RadioBrowserHTTPStatus as exc:
                if exc.code == 429 or exc.code >= 500:
                    last_error = exc
                    continue
                raise RadioBrowserError(
                    f"mirror {mirror} answered HTTP {exc.code} for {path}"
                ) from exc
            except (OSError, http.client.HTTPException) as exc:
                # Covers TimeoutError, connection errors, and urllib URLError.
                last_error = exc
                continue
            if len(body) > self._max_response_bytes:
                raise RadioBrowserError(
                    f"mirror {mirror} answered {len(body)} bytes for {path}, "
                    f"limit is {self._max_response_bytes}"
                )
            try:
                payload = json.loads(body.decode("utf-8"))
            except (UnicodeDecodeError, ValueError) as exc:
                last_error = exc
                continue
            self.last_mirror = mirror
            return payload
        raise RadioBrowserError(
            f"all attempts failed for {path} (mirrors tried: {', '.join(tried)}): {last_error!r}"
        ) from last_error

    def _serialize_params(self, params: dict[str, object] | None) -> dict[str, str]:
        if not params:
            return {}
        return {key: _format_param(value) for key, value in params.items() if value is not None}

    # -- caching ---------------------------------------------------------------

    def _cached(self, key: tuple[object, ...], ttl_seconds: float, load: Callable[[], Any]) -> Any:
        now = self._now()
        with self._lock:
            entry = self._cache.get(key)
            if entry is not None and entry[0] > now:
                return entry[1]
        value = load()
        with self._lock:
            self._cache[key] = (self._now() + ttl_seconds, value)
        return value

    def _cached_list(
        self, key: tuple[object, ...], ttl_seconds: float, path: str
    ) -> list[dict[str, Any]]:
        def load() -> list[dict[str, Any]]:
            return _expect_dict_list(self._get_json(path), path)

        value = self._cached(key, ttl_seconds, load)
        return [dict(item) for item in value]
