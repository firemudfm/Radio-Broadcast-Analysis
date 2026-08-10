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


# clean-speech/ carries the legacy v0.4.1 pipeline's clips; evidence/ carries
# the shared pipeline's mention clips (EvidenceClipService). Nothing else in
# the bucket is audio a caller may stream.
_ALLOWED_AUDIO_PREFIXES = ("clean-speech/", "evidence/")


def is_allowed_audio_key(key: str) -> bool:
    normalized = key.strip().lstrip("/")
    return normalized.startswith(_ALLOWED_AUDIO_PREFIXES) and not normalized.endswith("/")
