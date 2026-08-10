"""Evidence clip capture: give every pipeline mention a playable recording.

The pipeline keeps the audio a mention came from -- matched segments get
``disposition='retained'`` and the cleanup worker never deletes them -- but
until now nothing turned those spool segments into a clip the API can stream.
``mention_events.evidence_storage_key`` stayed NULL, so the audio-token route
answered 409 for every pipeline mention.

This service closes that gap. For one mention it:

1. resolves the conversation's segments (transcripts are stamped with their
   conversation at close; older rows fall back to the mention's transcript_id);
2. reads the retained bytes from the segment store (digest-verified);
3. combines them into a single clip -- a lone segment passes through untouched,
   several same-format segments are losslessly concatenated, and mixed formats
   are re-encoded to Ogg/Opus (the pipeline's own speech settings);
4. uploads the clip to S3 under ``RADIO_EVIDENCE_PREFIX`` as
   ``evidence/YYYY/MM/DD/<mention_id>.<ext>`` (the layout the target
   architecture documents, and the prefix the instance role can Get/Put);
5. records the key on ``mention_events``, which flips ``audio_available`` in
   every mention view and makes the existing audio-token + stream routes work.

Failures never lose the mention: the caller logs and moves on, and the
analysis worker retries via its idle-time backlog sweep, which is also what
backfills mentions created before this feature existed.
"""
from __future__ import annotations

import logging
import subprocess  # nosec B404 - fixed binary, argument arrays, never shell=True
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..observability import log_fields
from ..pipeline.contracts import StorageDescriptor

logger = logging.getLogger(__name__)

#: Generous: concatenation is stream copy in the common case, and clips are a
#: few minutes of 24 kbit/s mono speech, not albums.
COMBINE_TIMEOUT_SECONDS = 120.0

_CONTENT_TYPES = {"opus": "audio/ogg", "wav": "audio/wav", "flac": "audio/flac"}


class EvidenceCaptureError(RuntimeError):
    """The clip could not be produced; the mention itself is unaffected."""


def _content_type(extension: str) -> str:
    return _CONTENT_TYPES.get(extension, "application/octet-stream")


class EvidenceClipService:
    """Builds and attaches the durable audio clip for pipeline mentions."""

    def __init__(
        self,
        settings,
        database: Any,
        store: Any,
        s3_client: Any,
        *,
        ffmpeg_binary: str = "ffmpeg",
        timeout: float = COMBINE_TIMEOUT_SECONDS,
    ) -> None:
        self._settings = settings
        self._database = database
        self._store = store
        self._s3 = s3_client
        self._ffmpeg_binary = ffmpeg_binary
        self._timeout = timeout

    def capture(self, mention_id: str) -> bool:
        """Attach a clip to one mention. True when audio is available after.

        Idempotent: a mention that already carries evidence returns True
        without touching storage, so redeliveries and the backlog sweep are
        both safe.
        """
        row = self._database.read_one(
            "SELECT mention_id, conversation_id, transcript_id,"
            " broadcast_start_utc, broadcast_end_utc, evidence_available"
            " FROM mention_events WHERE mention_id=?",
            (mention_id,),
        )
        if row is None:
            raise EvidenceCaptureError(f"Mention {mention_id} not found")
        if int(row["evidence_available"] or 0):
            return True

        segments = self._segment_rows(
            str(row["conversation_id"] or ""),
            str(row["transcript_id"] or ""),
            broadcast_span_ms=self._span_ms(
                str(row["broadcast_start_utc"] or ""),
                str(row["broadcast_end_utc"] or ""),
            ),
        )
        if not segments:
            raise EvidenceCaptureError(
                "No retained audio segments found for the conversation"
            )

        parts: list[tuple[str, bytes]] = []
        for segment in segments:
            descriptor = StorageDescriptor(
                backend=str(segment["storage_backend"]),
                path=segment["storage_path"],
                bucket=segment["storage_bucket"],
                key=segment["storage_key"],
                sha256=str(segment["sha256"]),
                size_bytes=int(segment["size_bytes"]),
            )
            extension = self._extension_of(descriptor)
            parts.append((extension, self._store.read(descriptor)))

        data, extension = self._combine(parts)
        key = self._clip_key(
            mention_id, str(row["broadcast_start_utc"] or ""), extension
        )
        self._s3.put_object(
            Bucket=self._settings.RADIO_S3_BUCKET,
            Key=key,
            Body=data,
            ContentType=_content_type(extension),
            # Same posture as every other durable result: encrypted at rest,
            # no ACL, bucket policy is the only access path.
            ServerSideEncryption="AES256",
        )

        stamp = datetime.now(UTC).isoformat().replace("+00:00", "Z")
        self._database.write(
            lambda connection: connection.execute(
                "UPDATE mention_events SET evidence_storage_key=?,"
                " evidence_available=1, updated_at_utc=? WHERE mention_id=?",
                (key, stamp, mention_id),
            )
        )
        logger.info(
            "Evidence clip attached",
            extra=log_fields(
                mention_id=mention_id,
                evidence_key=key,
                segments=len(parts),
                bytes=len(data),
            ),
        )
        return True

    # -- resolution ------------------------------------------------------------

    def _segment_rows(
        self,
        conversation_id: str,
        transcript_id: str,
        *,
        broadcast_span_ms: int | None,
    ) -> list[Any]:
        """The conversation's segments, oldest first.

        Transcripts written before close-stamping existed carry
        ``conversation_id=NULL``; those mentions still know their final
        transcript. That resolves only the conversation's LAST segment, so it
        is used solely when that one segment covers (almost) the whole
        broadcast window. A tail clip presented as the mention's recording
        would be wrong evidence, and wrong evidence is worse than none.
        """
        if conversation_id:
            rows = self._database.read_all(
                """
                SELECT s.segment_id, s.storage_backend, s.storage_path,
                       s.storage_bucket, s.storage_key, s.sha256, s.size_bytes
                FROM transcripts t
                JOIN audio_segments s ON s.segment_id = t.segment_id
                WHERE t.conversation_id=? AND s.disposition != 'deleted'
                GROUP BY s.segment_id
                ORDER BY s.started_at_utc, s.segment_id
                """,
                (conversation_id,),
            )
            if rows:
                return rows
        if not transcript_id:
            return []
        rows = self._database.read_all(
            """
            SELECT s.segment_id, s.storage_backend, s.storage_path,
                   s.storage_bucket, s.storage_key, s.sha256, s.size_bytes,
                   s.duration_ms
            FROM transcripts t
            JOIN audio_segments s ON s.segment_id = t.segment_id
            WHERE t.transcript_id=? AND s.disposition != 'deleted'
            """,
            (transcript_id,),
        )
        if not rows:
            return []
        if broadcast_span_ms is None:
            return rows
        segment_ms = int(rows[0]["duration_ms"] or 0)
        if segment_ms and segment_ms >= broadcast_span_ms * 0.9:
            return rows
        raise EvidenceCaptureError(
            "The conversation spans several segments and only the last is"
            " resolvable; refusing to attach a partial clip as evidence"
        )

    @staticmethod
    def _span_ms(start: str, end: str) -> int | None:
        if not start or not end:
            return None
        try:
            opened = datetime.fromisoformat(start.replace("Z", "+00:00"))
            closed = datetime.fromisoformat(end.replace("Z", "+00:00"))
        except ValueError:
            return None
        span = (closed - opened).total_seconds() * 1000
        return int(span) if span > 0 else None

    @staticmethod
    def _extension_of(descriptor: StorageDescriptor) -> str:
        source = descriptor.path or descriptor.key or ""
        suffix = Path(str(source)).suffix.lstrip(".").lower()
        return suffix or "opus"

    def _clip_key(self, mention_id: str, broadcast_start: str, extension: str) -> str:
        moment = self._parse_time(broadcast_start)
        prefix = self._settings.RADIO_EVIDENCE_PREFIX
        # Deterministic and date-partitioned, like the mention documents:
        # recapturing overwrites the same object instead of forking a second.
        return f"{prefix}{moment:%Y/%m/%d}/{mention_id}.{extension}"

    @staticmethod
    def _parse_time(value: str) -> datetime:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return datetime.now(UTC)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)

    # -- combination -----------------------------------------------------------

    def _combine(self, parts: list[tuple[str, bytes]]) -> tuple[bytes, str]:
        """One clip from N segments, plus the extension the key will carry."""
        if len(parts) == 1:
            extension, data = parts[0]
            return data, extension
        extensions = {extension for extension, _ in parts}
        if len(extensions) == 1:
            extension = next(iter(extensions))
            return self._concat_copy(parts, extension), extension
        # Mixed formats only occur across an encoder fallback boundary; decode
        # and re-encode with the pipeline's own speech settings.
        return self._concat_reencode(parts)

    def _concat_copy(self, parts: list[tuple[str, bytes]], extension: str) -> bytes:
        """Lossless concatenation of same-format segments via the demuxer.

        The output is a file, never a pipe: WAV (and other seekable
        containers) need FFmpeg to rewrite size headers on close, which is
        impossible on a pipe and silently produces 0xFFFFFFFF sizes. The
        listing carries bare filenames resolved against ``cwd``, so no path
        from any environment ever needs concat-demuxer quoting.
        """
        with tempfile.TemporaryDirectory(prefix="evidence-") as workdir:
            root = Path(workdir)
            listing = root / "segments.txt"
            lines = []
            for index, (_, data) in enumerate(parts):
                name = f"segment-{index:04d}.{extension}"
                (root / name).write_bytes(data)
                lines.append(f"file '{name}'")
            listing.write_text("\n".join(lines), encoding="utf-8")
            output = f"clip.{extension}"
            command = [
                self._ffmpeg_binary,
                "-nostdin",
                "-hide_banner",
                "-loglevel", "error",
                "-f", "concat",
                "-i", "segments.txt",
                "-c", "copy",
                output,
            ]
            self._run_ffmpeg(command, cwd=root, output=root / output)
            return (root / output).read_bytes()

    def _concat_reencode(self, parts: list[tuple[str, bytes]]) -> tuple[bytes, str]:
        """Concatenate mixed-format segments by decoding through the filter.

        Tries Opus first; a spool only holds mixed formats after an Opus
        encoder fault, so the very box that needs this path may lack libopus.
        Lossless WAV is the fallback, exactly like the segment encoder's own.
        """
        with tempfile.TemporaryDirectory(prefix="evidence-") as workdir:
            root = Path(workdir)
            inputs: list[str] = []
            for index, (extension, data) in enumerate(parts):
                name = f"segment-{index:04d}.{extension}"
                (root / name).write_bytes(data)
                inputs.extend(["-i", name])
            base = [self._ffmpeg_binary, "-nostdin", "-hide_banner", "-loglevel", "error"]
            filtergraph = ["-filter_complex", f"concat=n={len(parts)}:v=0:a=1", "-vn"]
            try:
                opus_output = "clip.opus"
                self._run_ffmpeg(
                    [
                        *base, *inputs, *filtergraph,
                        "-c:a", "libopus", "-b:a", "24k", "-application", "voip",
                        opus_output,
                    ],
                    cwd=root,
                    output=root / opus_output,
                )
                return (root / opus_output).read_bytes(), "opus"
            except EvidenceCaptureError:
                wav_output = "clip.wav"
                self._run_ffmpeg(
                    [*base, *inputs, *filtergraph, "-c:a", "pcm_s16le", wav_output],
                    cwd=root,
                    output=root / wav_output,
                )
                return (root / wav_output).read_bytes(), "wav"

    def _run_ffmpeg(self, command: list[str], *, cwd: Path, output: Path) -> None:
        try:
            completed = subprocess.run(  # noqa: S603 - fixed binary, validated args, no shell
                command,
                cwd=str(cwd),
                capture_output=True,
                timeout=self._timeout,
                check=False,
            )
        except FileNotFoundError as error:
            raise EvidenceCaptureError(
                f"{self._ffmpeg_binary!r} is not installed"
            ) from error
        except subprocess.TimeoutExpired as error:
            raise EvidenceCaptureError("Clip concatenation timed out") from error
        if completed.returncode != 0:
            detail = completed.stderr.decode("utf-8", "replace")[:300]
            raise EvidenceCaptureError(
                f"FFmpeg exited {completed.returncode}: {detail}"
            )
        if not output.exists() or output.stat().st_size == 0:
            raise EvidenceCaptureError("FFmpeg produced no output file")
