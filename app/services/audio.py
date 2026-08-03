from __future__ import annotations

import base64
import hashlib
import hmac
import json
import mimetypes
import re
import time
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

from fastapi import HTTPException, Request, status
from fastapi.responses import StreamingResponse

from ..config import Settings
from ..s3_utils import is_allowed_audio_key

_RANGE = re.compile(r"^bytes=(\d*)-(\d*)$")


def _encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)


class AudioService:
    def __init__(self, settings: Settings, database: Any, s3_client: Any) -> None:
        self._settings = settings
        self._database = database
        self._s3 = s3_client
        self._secret = settings.RADIO_AUDIO_TOKEN_SECRET.get_secret_value().encode("utf-8")

    def create_token(self, mention_id: str, request: Request) -> dict[str, Any]:
        reference = self._database.mention_audio(mention_id)
        if reference is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Mention not found")
        audio_key = str(reference["audio_s3_key"] or "")
        if not audio_key or not is_allowed_audio_key(audio_key):
            # Pipeline mentions carry no captured clip yet (evidence capture is
            # not implemented), and keys outside clean-speech/ are not
            # streamable. Both mean "this mention has no audio", not "not found".
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Audio is unavailable")
        expires = int(time.time()) + self._settings.RADIO_AUDIO_TOKEN_TTL_SECONDS
        payload = {"mention_id": mention_id, "audio_key": audio_key, "exp": expires}
        raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
        body = _encode(raw)
        signature = _encode(hmac.new(self._secret, body.encode("ascii"), hashlib.sha256).digest())
        token = f"{body}.{signature}"
        return {
            "url": str(request.url_for("stream_audio", token=token)),
            "expires_at_utc": datetime.fromtimestamp(expires, tz=UTC),
        }

    def stream(self, token: str, range_header: str | None) -> StreamingResponse:
        payload = self._verify(token)
        key = str(payload["audio_key"])
        head = self._s3.head_object(Bucket=self._settings.RADIO_S3_BUCKET, Key=key)
        total = int(head["ContentLength"])
        start, end = self._range(range_header, total)
        request: dict[str, Any] = {"Bucket": self._settings.RADIO_S3_BUCKET, "Key": key}
        status_code = 200
        headers = {
            "Accept-Ranges": "bytes",
            "Cache-Control": "private, max-age=60",
            "Content-Disposition": "inline",
        }
        if start is not None and end is not None:
            request["Range"] = f"bytes={start}-{end}"
            status_code = 206
            headers["Content-Range"] = f"bytes {start}-{end}/{total}"
            headers["Content-Length"] = str(end - start + 1)
        else:
            headers["Content-Length"] = str(total)
        response = self._s3.get_object(**request)
        body = response["Body"]
        media_type = response.get("ContentType") or mimetypes.guess_type(key)[0] or "audio/wav"
        return StreamingResponse(
            self._chunks(body),
            status_code=status_code,
            media_type=media_type,
            headers=headers,
        )

    def _verify(self, token: str) -> dict[str, Any]:
        try:
            body, signature = token.split(".", 1)
            expected = _encode(hmac.new(self._secret, body.encode("ascii"), hashlib.sha256).digest())
            if not hmac.compare_digest(signature, expected):
                raise ValueError("Bad signature")
            payload = json.loads(_decode(body))
            if not isinstance(payload, dict):
                raise ValueError("Bad payload")
            if int(payload.get("exp", 0)) < int(time.time()):
                raise ValueError("Expired")
            audio_key = str(payload.get("audio_key") or "")
            if not is_allowed_audio_key(audio_key):
                raise ValueError("Bad audio key")
            return payload
        except Exception as error:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Audio link is invalid or expired",
            ) from error

    @staticmethod
    def _range(header: str | None, total: int) -> tuple[int | None, int | None]:
        if not header:
            return None, None
        match = _RANGE.fullmatch(header.strip())
        if not match:
            raise HTTPException(status_code=416, detail="Invalid byte range")
        first, last = match.groups()
        if first:
            start = int(first)
            end = int(last) if last else total - 1
        elif last:
            length = int(last)
            start = max(0, total - length)
            end = total - 1
        else:
            raise HTTPException(status_code=416, detail="Invalid byte range")
        if start < 0 or end < start or start >= total:
            raise HTTPException(status_code=416, detail="Byte range is outside the audio file")
        return start, min(end, total - 1)

    @staticmethod
    def _chunks(body: Any, size: int = 64 * 1024) -> Iterator[bytes]:
        try:
            while True:
                chunk = body.read(size)
                if not chunk:
                    break
                yield chunk
        finally:
            body.close()
