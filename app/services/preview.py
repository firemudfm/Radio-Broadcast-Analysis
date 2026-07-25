"""Short-lived station preview: signed token + FastAPI streaming proxy.

The browser never sees url_resolved. Playback resolution happens when the
preview starts (Radio Browser /json/url click endpoint), the upstream URL is
SSRF-validated per hop, bytes proxy through FastAPI with a hard duration cap,
a concurrency cap, and no caching. The upstream connection closes when the
client disconnects or the byte/duration budget is spent.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

from ..config import Settings
from ..db_catalog import CatalogStore
from .net_safety import MAX_REDIRECTS, NetSafetyError, validate_public_http_url
from .radio_browser import RadioBrowserClient


def _encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)


class PreviewError(RuntimeError):
    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[override]
        return None


# ~256 kbps ceiling so one preview cannot saturate the instance.
_BYTES_PER_SECOND_CAP = 32_000
_CHUNK_BYTES = 8_192


class PreviewService:
    def __init__(
        self,
        settings: Settings,
        store: CatalogStore,
        client: RadioBrowserClient,
        *,
        opener_factory=None,
    ) -> None:
        self._settings = settings
        self._store = store
        self._client = client
        self._secret = settings.RADIO_AUDIO_TOKEN_SECRET.get_secret_value().encode("utf-8")
        self._active = 0
        self._lock = threading.Lock()
        self._opener_factory = opener_factory or (
            lambda: urllib.request.build_opener(_NoRedirect)
        )

    # -- token -----------------------------------------------------------------

    def create_token(self, station_uuid: str, *, deleted: bool) -> dict[str, Any]:
        if deleted:
            raise PreviewError(410, "Station is removed by the curated deletion list")
        expires = int(time.time()) + self._settings.RADIO_PREVIEW_TOKEN_TTL_SECONDS
        payload = {"station_uuid": station_uuid.lower(), "exp": expires, "kind": "preview"}
        raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
        body = _encode(raw)
        signature = _encode(hmac.new(self._secret, body.encode("ascii"), hashlib.sha256).digest())
        self._store.record_preview_audit(station_uuid, "token_issued", None)
        return {
            "token": f"{body}.{signature}",
            "expires_at_utc": datetime.fromtimestamp(expires, tz=UTC),
            "max_seconds": self._settings.RADIO_PREVIEW_MAX_SECONDS,
        }

    def verify_token(self, token: str) -> dict[str, Any]:
        try:
            body, signature = token.split(".", 1)
            expected = _encode(
                hmac.new(self._secret, body.encode("ascii"), hashlib.sha256).digest()
            )
            if not hmac.compare_digest(signature, expected):
                raise PreviewError(403, "Invalid preview token signature")
            payload = json.loads(_decode(body))
        except PreviewError:
            raise
        except Exception as error:  # noqa: BLE001 - any malformed token is a 403
            raise PreviewError(403, "Malformed preview token") from error
        if payload.get("kind") != "preview":
            raise PreviewError(403, "Wrong token kind")
        if int(payload.get("exp", 0)) < int(time.time()):
            raise PreviewError(403, "Preview token expired")
        return payload

    # -- streaming proxy ---------------------------------------------------------

    def open_stream(self, station_uuid: str) -> tuple[Any, str]:
        """Resolve the station playback URL and open a validated upstream.

        Returns (http response object, content_type). Redirects are followed
        manually with SSRF re-validation on every hop.
        """
        resolved = self._client.resolve_url(station_uuid)
        url = str(resolved.get("url") or "").strip()
        if not url:
            raise PreviewError(502, "Radio Browser did not return a playback URL")
        opener = self._opener_factory()
        current = url
        for _hop in range(MAX_REDIRECTS + 1):
            try:
                target = validate_public_http_url(current)
            except NetSafetyError as error:
                self._store.record_preview_audit(station_uuid, "blocked", str(error))
                raise PreviewError(502, f"Stream target rejected: {error}") from error
            request = urllib.request.Request(
                target.url,
                headers={
                    "User-Agent": self._settings.RADIO_BROWSER_USER_AGENT,
                    "Icy-MetaData": "0",
                },
            )
            try:
                response = opener.open(
                    request, timeout=self._settings.RADIO_BROWSER_REQUEST_TIMEOUT_SECONDS
                )
            except urllib.error.HTTPError as error:
                location = error.headers.get("Location") if error.headers else None
                if error.code in {301, 302, 303, 307, 308} and location:
                    current = urllib.parse.urljoin(current, location)
                    continue
                raise PreviewError(502, f"Upstream stream error: HTTP {error.code}") from error
            except OSError as error:
                raise PreviewError(502, f"Could not connect to the stream: {error}") from error
            content_type = str(response.headers.get("Content-Type") or "audio/mpeg")
            if "text/html" in content_type.lower():
                response.close()
                raise PreviewError(502, "Stream returned HTML, not audio")
            return response, content_type
        raise PreviewError(502, "Too many redirects while resolving the stream")

    def stream_preview(self, token: str) -> tuple[Iterator[bytes], str]:
        payload = self.verify_token(token)
        station_uuid = str(payload["station_uuid"])
        with self._lock:
            if self._active >= self._settings.RADIO_PREVIEW_MAX_CONCURRENT:
                raise PreviewError(429, "Preview limit reached; try again shortly")
            self._active += 1
        try:
            response, content_type = self.open_stream(station_uuid)
        except Exception:
            with self._lock:
                self._active -= 1
            raise
        self._store.record_preview_audit(station_uuid, "preview_started", None)
        max_bytes = self._settings.RADIO_PREVIEW_MAX_SECONDS * _BYTES_PER_SECOND_CAP
        deadline = time.monotonic() + self._settings.RADIO_PREVIEW_MAX_SECONDS

        def generator() -> Iterator[bytes]:
            sent = 0
            try:
                while sent < max_bytes and time.monotonic() < deadline:
                    chunk = response.read(_CHUNK_BYTES)
                    if not chunk:
                        break
                    sent += len(chunk)
                    yield chunk
            finally:
                try:
                    response.close()
                finally:
                    with self._lock:
                        self._active -= 1
                self._store.record_preview_audit(
                    station_uuid, "preview_finished", f"bytes={sent}"
                )

        return generator(), content_type
