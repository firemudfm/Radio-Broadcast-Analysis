from __future__ import annotations

from urllib.parse import urlparse


def parse_s3_uri(value: str, expected_bucket: str | None = None) -> str | None:
    text = value.strip()
    if not text:
        return None
    if not text.startswith("s3://"):
        return text.lstrip("/")
    parsed = urlparse(text)
    if parsed.scheme != "s3" or not parsed.netloc:
        return None
    if expected_bucket and parsed.netloc != expected_bucket:
        return None
    return parsed.path.lstrip("/") or None


def is_allowed_audio_key(key: str) -> bool:
    normalized = key.strip().lstrip("/")
    return normalized.startswith("clean-speech/") and not normalized.endswith("/")
