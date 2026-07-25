from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import PurePosixPath
from typing import Any

from ..config import Settings


def _datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _float(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    return float(value) if isinstance(value, (int, float)) else None


def _normalized_with_map(text: str) -> tuple[str, list[int]]:
    normalized: list[str] = []
    indexes: list[int] = []
    previous_space = True
    for index, char in enumerate(text):
        folded = unicodedata.normalize("NFKD", char.casefold())
        ascii_chars = "".join(part for part in folded if not unicodedata.combining(part))
        for part in ascii_chars:
            if part.isalnum():
                normalized.append(part)
                indexes.append(index)
                previous_space = False
            elif not previous_space:
                normalized.append(" ")
                indexes.append(index)
                previous_space = True
    while normalized and normalized[-1] == " ":
        normalized.pop()
        indexes.pop()
    return "".join(normalized), indexes


def find_normalized_span(text: str, needle: str) -> tuple[int, int] | None:
    normalized_text, mapping = _normalized_with_map(text)
    normalized_needle, _ = _normalized_with_map(needle)
    if not normalized_needle:
        return None
    start = normalized_text.find(normalized_needle)
    if start < 0:
        return None
    end = start + len(normalized_needle) - 1
    if start >= len(mapping) or end >= len(mapping):
        return None
    raw_start = mapping[start]
    raw_end = mapping[end] + 1
    while raw_end < len(text) and text[raw_end].isalnum():
        raw_end += 1
    return raw_start, raw_end


@dataclass(frozen=True)
class WordRecord:
    text: str
    start_char: int
    end_char: int
    broadcast_start_utc: datetime | None
    broadcast_end_utc: datetime | None
    probability: float | None


class ConversationService:
    """Assemble the complete contiguous speech conversation around a mention.

    This intentionally does not use a fixed number of seconds before/after a
    mention. Neighboring transcript groups are walked in both directions until
    a real speech gap marks a session boundary, then the complete session is
    returned with exact character and timestamp highlights.
    """

    def __init__(self, settings: Settings, s3_client: Any) -> None:
        self._settings = settings
        self._s3 = s3_client

    def build(self, mention: dict[str, Any]) -> dict[str, Any]:
        transcript_key = str(mention.get("transcript_s3_key") or "").strip()
        scope = str(mention.get("conversation_scope") or "session")
        documents = (
            self._load_related_transcripts(transcript_key, scope=scope)
            if transcript_key
            else []
        )
        if not documents:
            fallback = str(mention.get("context") or "").strip()
            highlight = self._fallback_highlight(fallback, mention)
            return {
                "full_transcript": fallback,
                "highlighted_sentence": fallback or None,
                "transcript_segments": [
                    {
                        "id": "fallback",
                        "text": fallback,
                        "start_char": 0,
                        "end_char": len(fallback),
                        "broadcast_start_utc": mention.get("broadcast_start_utc"),
                        "broadcast_end_utc": mention.get("broadcast_end_utc"),
                        "detected_language": mention.get("detected_language"),
                        "source_transcript_key": transcript_key or "",
                    }
                ] if fallback else [],
                "words": [],
                "highlights": [highlight] if highlight else [],
                "transcript_source_keys": [transcript_key] if transcript_key else [],
            }

        text_parts: list[str] = []
        words: list[WordRecord] = []
        segments: list[dict[str, Any]] = []
        source_keys: list[str] = []

        for doc_index, (key, document) in enumerate(documents):
            if doc_index and text_parts:
                text_parts.append("\n")
            source_keys.append(key)
            language = str((document.get("language") or {}).get("detected") or "").strip() or None
            doc_segments = document.get("segments") if isinstance(document.get("segments"), list) else []
            if not doc_segments:
                doc_text = str(document.get("text") or "").strip()
                if doc_text:
                    start_char = sum(len(part) for part in text_parts)
                    text_parts.append(doc_text)
                    segments.append(
                        {
                            "id": f"{doc_index}:0",
                            "text": doc_text,
                            "start_char": start_char,
                            "end_char": start_char + len(doc_text),
                            "broadcast_start_utc": document.get("broadcast_start_utc"),
                            "broadcast_end_utc": None,
                            "detected_language": language,
                            "source_transcript_key": key,
                        }
                    )
                continue

            for segment_index, segment in enumerate(doc_segments):
                if not isinstance(segment, dict):
                    continue
                if segment_index and text_parts and not text_parts[-1].endswith(("\n", " ")):
                    text_parts.append(" ")
                segment_start_char = sum(len(part) for part in text_parts)
                segment_words = segment.get("words") if isinstance(segment.get("words"), list) else []
                if segment_words:
                    for word_index, word in enumerate(segment_words):
                        if not isinstance(word, dict):
                            continue
                        raw = str(word.get("word") or "")
                        if not raw:
                            continue
                        if not text_parts and word_index == 0:
                            raw = raw.lstrip()
                        before = sum(len(part) for part in text_parts)
                        text_parts.append(raw)
                        leading = len(raw) - len(raw.lstrip())
                        trailing = len(raw) - len(raw.rstrip())
                        word_start = before + leading
                        word_end = before + len(raw) - trailing
                        words.append(
                            WordRecord(
                                text=raw.strip(),
                                start_char=word_start,
                                end_char=max(word_start, word_end),
                                broadcast_start_utc=_datetime(word.get("broadcast_start_utc")),
                                broadcast_end_utc=_datetime(word.get("broadcast_end_utc")),
                                probability=_float(word.get("probability")),
                            )
                        )
                else:
                    segment_text = str(segment.get("text") or "").strip()
                    text_parts.append(segment_text)
                segment_end_char = sum(len(part) for part in text_parts)
                segment_text = "".join(text_parts)[segment_start_char:segment_end_char].strip()
                # Trim segment character bounds to its visible content.
                raw_segment = "".join(text_parts)[segment_start_char:segment_end_char]
                left_trim = len(raw_segment) - len(raw_segment.lstrip())
                right_trim = len(raw_segment) - len(raw_segment.rstrip())
                visible_start = segment_start_char + left_trim
                visible_end = segment_end_char - right_trim
                segments.append(
                    {
                        "id": f"{doc_index}:{segment.get('id', segment_index + 1)}",
                        "text": segment_text,
                        "start_char": visible_start,
                        "end_char": max(visible_start, visible_end),
                        "broadcast_start_utc": segment.get("broadcast_start_utc"),
                        "broadcast_end_utc": segment.get("broadcast_end_utc"),
                        "detected_language": language,
                        "source_transcript_key": key,
                    }
                )

        full_transcript = "".join(text_parts).strip()
        if len(full_transcript) > self._settings.RADIO_CONVERSATION_MAX_CHARACTERS:
            full_transcript = full_transcript[: self._settings.RADIO_CONVERSATION_MAX_CHARACTERS]
            segments = [item for item in segments if item["start_char"] < len(full_transcript)]
            for item in segments:
                item["end_char"] = min(item["end_char"], len(full_transcript))
                item["text"] = full_transcript[item["start_char"]:item["end_char"]]
            words = [word for word in words if word.start_char < len(full_transcript)]

        highlight = self._timestamp_highlight(full_transcript, words, mention)
        if highlight is None:
            highlight = self._fallback_highlight(full_transcript, mention)
        highlighted_sentence = None
        if highlight:
            highlighted_sentence = self._sentence_for_span(
                full_transcript, highlight["start_char"], highlight["end_char"]
            )
            if not highlighted_sentence:
                for segment in segments:
                    if segment["start_char"] <= highlight["start_char"] < segment["end_char"]:
                        highlighted_sentence = segment["text"]
                        break
            highlighted_sentence = highlighted_sentence or highlight["text"]

        return {
            "full_transcript": full_transcript,
            "highlighted_sentence": highlighted_sentence,
            "transcript_segments": segments,
            "words": [
                {
                    "text": word.text,
                    "start_char": word.start_char,
                    "end_char": word.end_char,
                    "broadcast_start_utc": word.broadcast_start_utc,
                    "broadcast_end_utc": word.broadcast_end_utc,
                    "probability": word.probability,
                }
                for word in words
            ],
            "highlights": [highlight] if highlight else [],
            "transcript_source_keys": source_keys,
        }

    def _load_related_transcripts(
        self,
        transcript_key: str,
        *,
        scope: str = "session",
    ) -> list[tuple[str, dict[str, Any]]]:
        clean_key = transcript_key.strip().lstrip("/")
        if not clean_key.startswith(self._settings.RADIO_TRANSCRIPTS_PREFIX):
            return []
        path = PurePosixPath(clean_key)
        parent = path.parent.as_posix() + "/"
        if scope == "source_group":
            response = self._s3.list_objects_v2(
                Bucket=self._settings.RADIO_S3_BUCKET,
                Prefix=parent,
                MaxKeys=self._settings.RADIO_CONVERSATION_MAX_TRANSCRIPTS,
            )
            keys = sorted(
                {
                    clean_key,
                    *(
                        str(item.get("Key") or "")
                        for item in response.get("Contents", [])
                        if str(item.get("Key") or "").endswith(".transcript.json")
                    ),
                }
            )
            return self._load_documents(keys)
        prefix_parts = PurePosixPath(
            self._settings.RADIO_TRANSCRIPTS_PREFIX.rstrip("/")
        ).parts
        relative = path.parts[len(prefix_parts):]

        # Normal key structure:
        # transcripts/<station>/<YYYY>/<MM>/<DD>/<source-chunk>/<segment>.transcript.json
        if len(relative) >= 6:
            day_prefix = (
                self._settings.RADIO_TRANSCRIPTS_PREFIX
                + "/".join(relative[:4])
                + "/"
            )
            grouped: dict[str, list[str]] = {}
            paginator = self._s3.get_paginator("list_objects_v2")
            for page in paginator.paginate(
                Bucket=self._settings.RADIO_S3_BUCKET,
                Prefix=day_prefix,
            ):
                for item in page.get("Contents", []):
                    key = str(item.get("Key") or "")
                    if not key.endswith(".transcript.json"):
                        continue
                    group = PurePosixPath(key).parent.as_posix() + "/"
                    grouped.setdefault(group, []).append(key)
            grouped.setdefault(parent, []).append(clean_key)
            ordered_groups = sorted(grouped)
            anchor_group_index = ordered_groups.index(parent)
            scan = self._settings.RADIO_CONVERSATION_SCAN_CHUNKS
            selected_groups = ordered_groups[
                max(0, anchor_group_index - scan):
                anchor_group_index + scan + 1
            ]
            keys = sorted(
                {
                    key
                    for group in selected_groups
                    for key in grouped.get(group, [])
                }
            )[: self._settings.RADIO_CONVERSATION_MAX_TRANSCRIPTS]
        else:
            response = self._s3.list_objects_v2(
                Bucket=self._settings.RADIO_S3_BUCKET,
                Prefix=parent,
                MaxKeys=self._settings.RADIO_CONVERSATION_MAX_TRANSCRIPTS,
            )
            keys = sorted(
                {
                    clean_key,
                    *(
                        str(item.get("Key") or "")
                        for item in response.get("Contents", [])
                        if str(item.get("Key") or "").endswith(".transcript.json")
                    ),
                }
            )

        documents = self._load_documents(keys)
        return self._select_contiguous_session(documents, clean_key)

    def _load_documents(self, keys: list[str]) -> list[tuple[str, dict[str, Any]]]:
        documents: list[tuple[str, dict[str, Any]]] = []
        for key in keys:
            try:
                body = self._s3.get_object(
                    Bucket=self._settings.RADIO_S3_BUCKET,
                    Key=key,
                )["Body"].read()
                document = json.loads(body)
            except Exception:
                continue
            if isinstance(document, dict):
                documents.append((key, document))
        documents.sort(key=lambda item: (self._document_start(item[1]), item[0]))
        return documents

    def _select_contiguous_session(
        self,
        documents: list[tuple[str, dict[str, Any]]],
        anchor_key: str,
    ) -> list[tuple[str, dict[str, Any]]]:
        if not documents:
            return []
        anchor_indexes = [
            index for index, (key, _) in enumerate(documents) if key == anchor_key
        ]
        if not anchor_indexes:
            return documents
        anchor = anchor_indexes[0]
        left = anchor
        right = anchor
        gap_limit = self._settings.RADIO_CONVERSATION_SESSION_GAP_SECONDS
        duration_limit = self._settings.RADIO_CONVERSATION_MAX_DURATION_SECONDS

        def can_include(candidate_left: int, candidate_right: int) -> bool:
            start = self._document_start(documents[candidate_left][1])
            end = self._document_end(documents[candidate_right][1])
            if start == datetime.max.replace(tzinfo=timezone.utc) or end is None:
                return False
            return (end - start).total_seconds() <= duration_limit

        while left > 0:
            previous_end = self._document_end(documents[left - 1][1])
            current_start = self._document_start(documents[left][1])
            if previous_end is None or current_start == datetime.max.replace(tzinfo=timezone.utc):
                break
            if (current_start - previous_end).total_seconds() > gap_limit:
                break
            if not can_include(left - 1, right):
                break
            left -= 1

        while right + 1 < len(documents):
            current_end = self._document_end(documents[right][1])
            next_start = self._document_start(documents[right + 1][1])
            if current_end is None or next_start == datetime.max.replace(tzinfo=timezone.utc):
                break
            if (next_start - current_end).total_seconds() > gap_limit:
                break
            if not can_include(left, right + 1):
                break
            right += 1

        return documents[left:right + 1]

    @staticmethod
    def _document_start(document: dict[str, Any]) -> datetime:
        value = _datetime(document.get("broadcast_start_utc"))
        if value:
            return value
        segments = document.get("segments") if isinstance(document.get("segments"), list) else []
        for segment in segments:
            if isinstance(segment, dict):
                value = _datetime(segment.get("broadcast_start_utc"))
                if value:
                    return value
        return datetime.max.replace(tzinfo=timezone.utc)

    @staticmethod
    def _document_end(document: dict[str, Any]) -> datetime | None:
        direct = _datetime(document.get("broadcast_end_utc"))
        if direct:
            return direct
        segments = document.get("segments") if isinstance(document.get("segments"), list) else []
        values = [
            _datetime(segment.get("broadcast_end_utc"))
            for segment in segments
            if isinstance(segment, dict)
        ]
        values = [value for value in values if value]
        if values:
            return max(values)
        start = ConversationService._document_start(document)
        return None if start == datetime.max.replace(tzinfo=timezone.utc) else start

    @staticmethod
    def _sentence_for_span(text: str, start: int, end: int) -> str | None:
        if not text or start < 0 or end <= start:
            return None
        left_matches = list(re.finditer(r"[.!?\n]+\s*", text[:start]))
        left = left_matches[-1].end() if left_matches else 0
        right_match = re.search(r"[.!?]+(?:\s|$)|\n", text[end:])
        right = end + right_match.end() if right_match else len(text)
        sentence = text[left:right].strip()
        return sentence or None

    @staticmethod
    def _timestamp_highlight(
        full_transcript: str,
        words: list[WordRecord],
        mention: dict[str, Any],
    ) -> dict[str, Any] | None:
        start = _datetime(mention.get("broadcast_start_utc"))
        end = _datetime(mention.get("broadcast_end_utc")) or start
        if not start:
            return None
        overlapping = []
        for word in words:
            word_start = word.broadcast_start_utc
            word_end = word.broadcast_end_utc or word_start
            if not word_start or not word_end:
                continue
            if word_end >= start and (end is None or word_start <= end):
                overlapping.append(word)
        if not overlapping:
            return None
        char_start = min(word.start_char for word in overlapping)
        char_end = max(word.end_char for word in overlapping)
        return {
            "start_char": char_start,
            "end_char": char_end,
            "text": full_transcript[char_start:char_end],
            "keyword": str(mention.get("keyword_value") or mention.get("display_name") or ""),
            "matched_alias": mention.get("matched_alias"),
            "method": "timestamp",
            "broadcast_start_utc": start,
            "broadcast_end_utc": end,
        }

    @staticmethod
    def _fallback_highlight(full_transcript: str, mention: dict[str, Any]) -> dict[str, Any] | None:
        for candidate, method in (
            (mention.get("matched_alias"), "exact"),
            (mention.get("keyword_value"), "normalized"),
            (mention.get("display_name"), "normalized"),
        ):
            value = str(candidate or "").strip()
            if not value:
                continue
            direct = re.search(re.escape(value), full_transcript, flags=re.IGNORECASE)
            span = direct.span() if direct else find_normalized_span(full_transcript, value)
            if span:
                start, end = span
                return {
                    "start_char": start,
                    "end_char": end,
                    "text": full_transcript[start:end],
                    "keyword": str(mention.get("keyword_value") or value),
                    "matched_alias": mention.get("matched_alias"),
                    "method": method if direct else "normalized",
                    "broadcast_start_utc": _datetime(mention.get("broadcast_start_utc")),
                    "broadcast_end_utc": _datetime(mention.get("broadcast_end_utc")),
                }
        return None
