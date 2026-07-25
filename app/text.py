from __future__ import annotations

import hashlib
import re
import unicodedata

_NON_ALNUM = re.compile(r"[^a-z0-9]+")
_TOKEN = re.compile(r"[\w]+", flags=re.UNICODE)


def normalize_text(value: str) -> str:
    folded = unicodedata.normalize("NFKD", value).casefold()
    return " ".join(_TOKEN.findall(folded))


def slugify(value: str) -> str:
    ascii_text = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode()
    slug = _NON_ALNUM.sub("-", ascii_text.casefold()).strip("-")
    if not slug:
        slug = "keyword"
    return slug[:48]


def entity_id_for(value: str) -> str:
    normalized = normalize_text(value)
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:10]
    return f"api-{slugify(value)}-{digest}"
