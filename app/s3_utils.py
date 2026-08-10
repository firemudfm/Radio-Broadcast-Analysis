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


# clean-speech/ carries the legacy v0.4.1 pipeline's clips; the evidence
# prefix carries the shared pipeline's mention clips (EvidenceClipService).
# Nothing else in the bucket is audio a caller may stream. The evidence prefix
# is configurable (RADIO_EVIDENCE_PREFIX), so callers that hold settings pass
# it through; hardcoding it here would strand clips under a renamed prefix as
# unstreamable while the database claims audio_available.
def is_allowed_audio_key(key: str, *, evidence_prefix: str = "evidence/") -> bool:
    normalized = key.strip().lstrip("/")
    if normalized.endswith("/"):
        return False
    return normalized.startswith(("clean-speech/", evidence_prefix))
