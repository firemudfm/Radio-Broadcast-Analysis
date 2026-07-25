from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator
from uuid import uuid4

from .models import CampaignCreate, CampaignUpdate
from .text import entity_id_for

SCHEMA = """
PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;

CREATE TABLE IF NOT EXISTS app_meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS campaigns (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  objective TEXT NOT NULL,
  business_name TEXT,
  business_description TEXT,
  status TEXT NOT NULL CHECK (status IN ('active', 'paused')),
  monitor_from_utc TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS campaign_stations (
  campaign_id TEXT NOT NULL REFERENCES campaigns(id) ON DELETE CASCADE,
  station_id TEXT NOT NULL,
  PRIMARY KEY (campaign_id, station_id)
);

CREATE TABLE IF NOT EXISTS campaign_keywords (
  id TEXT PRIMARY KEY,
  campaign_id TEXT NOT NULL REFERENCES campaigns(id) ON DELETE CASCADE,
  entity_id TEXT NOT NULL,
  value TEXT NOT NULL,
  aliases_json TEXT NOT NULL,
  match_mode TEXT NOT NULL CHECK (match_mode IN ('tokens', 'substring')),
  keyword_type TEXT NOT NULL DEFAULT 'brand',
  semantic_matching INTEGER NOT NULL DEFAULT 0,
  semantic_threshold REAL NOT NULL DEFAULT 0.74,
  enabled INTEGER NOT NULL DEFAULT 1,
  UNIQUE (campaign_id, entity_id)
);

CREATE TABLE IF NOT EXISTS mentions (
  id TEXT PRIMARY KEY,
  campaign_id TEXT NOT NULL REFERENCES campaigns(id) ON DELETE CASCADE,
  campaign_keyword_id TEXT NOT NULL REFERENCES campaign_keywords(id) ON DELETE CASCADE,
  station_id TEXT NOT NULL,
  station_name TEXT NOT NULL,
  station_country_code TEXT,
  station_language_codes_json TEXT NOT NULL,
  source_result_s3_key TEXT NOT NULL,
  source_mention_id TEXT NOT NULL,
  entity_id TEXT NOT NULL,
  display_name TEXT NOT NULL,
  matched_alias TEXT,
  context TEXT NOT NULL,
  detected_language TEXT,
  language_probability REAL,
  sentiment_label TEXT NOT NULL CHECK (sentiment_label IN ('positive', 'neutral', 'negative')),
  sentiment_score REAL,
  sentiment_margin REAL,
  needs_review INTEGER NOT NULL DEFAULT 0,
  broadcast_start_utc TEXT NOT NULL,
  broadcast_end_utc TEXT,
  audio_clip_start_utc TEXT NOT NULL,
  audio_clip_end_utc TEXT NOT NULL,
  audio_s3_key TEXT NOT NULL,
  raw_audio_s3_key TEXT,
  transcript_s3_key TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE (campaign_id, source_result_s3_key, source_mention_id)
);

CREATE TABLE IF NOT EXISTS mention_analysis (
  mention_id TEXT PRIMARY KEY REFERENCES mentions(id) ON DELETE CASCADE,
  status TEXT NOT NULL CHECK (status IN ('pending', 'ready', 'disabled', 'error')),
  analysis_s3_key TEXT,
  model TEXT,
  summary TEXT,
  error TEXT,
  attempts INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sync_objects (
  result_key TEXT PRIMARY KEY,
  etag TEXT NOT NULL,
  campaign_revision INTEGER NOT NULL,
  processed_at_utc TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS semantic_scan_objects (
  campaign_keyword_id TEXT NOT NULL REFERENCES campaign_keywords(id) ON DELETE CASCADE,
  transcript_group_key TEXT NOT NULL,
  source_fingerprint TEXT NOT NULL,
  campaign_revision INTEGER NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('matched', 'not_matched', 'error')),
  error TEXT,
  processed_at_utc TEXT NOT NULL,
  PRIMARY KEY (campaign_keyword_id, transcript_group_key)
);

CREATE INDEX IF NOT EXISTS idx_campaigns_status ON campaigns(status);
CREATE INDEX IF NOT EXISTS idx_mentions_campaign_time ON mentions(campaign_id, broadcast_start_utc DESC);
CREATE INDEX IF NOT EXISTS idx_mentions_time ON mentions(broadcast_start_utc DESC);
CREATE INDEX IF NOT EXISTS idx_mention_analysis_status ON mention_analysis(status, updated_at);
CREATE INDEX IF NOT EXISTS idx_semantic_scan_status ON semantic_scan_objects(status, processed_at_utc);
"""


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


class Database:
    def __init__(
        self,
        path: Path,
        mention_window_days: int = 7,
        mention_audio_pad_seconds: float = 2.0,
    ) -> None:
        self._path = path
        self._connection: sqlite3.Connection | None = None
        self._lock = threading.RLock()
        self._mention_window_days = mention_window_days
        self._mention_audio_pad_seconds = mention_audio_pad_seconds

    def connect(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self._path, check_same_thread=False, timeout=30)
        connection.row_factory = sqlite3.Row
        with self._lock:
            self._connection = connection
            connection.executescript(SCHEMA)
            self._migrate(connection)
            connection.execute(
                "INSERT OR IGNORE INTO app_meta(key, value) VALUES('campaign_revision', '0')"
            )
            connection.commit()

    @staticmethod
    def _migrate(connection: sqlite3.Connection) -> None:
        columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(campaign_keywords)")}
        additions = {
            "keyword_type": "TEXT NOT NULL DEFAULT 'brand'",
            "semantic_matching": "INTEGER NOT NULL DEFAULT 0",
            "semantic_threshold": "REAL NOT NULL DEFAULT 0.74",
        }
        for column, declaration in additions.items():
            if column not in columns:
                connection.execute(
                    f"ALTER TABLE campaign_keywords ADD COLUMN {column} {declaration}"
                )

    def close(self) -> None:
        with self._lock:
            if self._connection is not None:
                self._connection.close()
                self._connection = None

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            if self._connection is None:
                raise RuntimeError("Database is not connected")
            try:
                self._connection.execute("BEGIN IMMEDIATE")
                yield self._connection
                self._connection.commit()
            except Exception:
                self._connection.rollback()
                raise

    def transaction_read(self, sql: str, args: tuple | list = ()) -> list[sqlite3.Row]:
        """Read-only helper for the v0.4 catalog store (see db_catalog.py)."""
        with self._lock:
            return list(self._conn().execute(sql, tuple(args)).fetchall())

    def ping(self) -> bool:
        with self._lock:
            if self._connection is None:
                return False
            return self._connection.execute("SELECT 1").fetchone()[0] == 1

    def campaign_revision(self) -> int:
        with self._lock:
            row = self._conn().execute(
                "SELECT value FROM app_meta WHERE key='campaign_revision'"
            ).fetchone()
            return int(row[0]) if row else 0

    def _increment_revision(self, connection: sqlite3.Connection) -> int:
        current = int(
            connection.execute(
                "SELECT value FROM app_meta WHERE key='campaign_revision'"
            ).fetchone()[0]
        )
        next_value = current + 1
        connection.execute(
            "UPDATE app_meta SET value=? WHERE key='campaign_revision'",
            (str(next_value),),
        )
        return next_value

    def create_campaign(self, payload: CampaignCreate, monitor_from: datetime) -> str:
        campaign_id = str(uuid4())
        now = iso(utc_now())
        with self.transaction() as connection:
            connection.execute(
                """
                INSERT INTO campaigns(
                  id, name, objective, business_name, business_description,
                  status, monitor_from_utc, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, 'active', ?, ?, ?)
                """,
                (
                    campaign_id,
                    payload.name,
                    payload.objective,
                    payload.business_name,
                    payload.business_description,
                    iso(monitor_from),
                    now,
                    now,
                ),
            )
            for station_id in payload.station_ids:
                connection.execute(
                    "INSERT INTO campaign_stations(campaign_id, station_id) VALUES(?, ?)",
                    (campaign_id, station_id),
                )
            for keyword in payload.keywords:
                connection.execute(
                    """
                    INSERT INTO campaign_keywords(
                      id, campaign_id, entity_id, value, aliases_json, match_mode,
                      keyword_type, semantic_matching, semantic_threshold, enabled
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
                    """,
                    (
                        str(uuid4()),
                        campaign_id,
                        entity_id_for(keyword.value),
                        keyword.value,
                        json.dumps(keyword.aliases, ensure_ascii=False),
                        keyword.match_mode,
                        keyword.keyword_type,
                        int(bool(keyword.semantic_matching)),
                        keyword.semantic_threshold,
                    ),
                )
            self._increment_revision(connection)
        return campaign_id

    def update_campaign(self, campaign_id: str, payload: CampaignUpdate) -> bool:
        fields = payload.model_fields_set
        now = iso(utc_now())
        with self.transaction() as connection:
            exists = connection.execute(
                "SELECT 1 FROM campaigns WHERE id=?", (campaign_id,)
            ).fetchone()
            if exists is None:
                return False
            updates: list[str] = []
            values: list[Any] = []
            for field, column in (
                ("name", "name"),
                ("objective", "objective"),
                ("business_name", "business_name"),
                ("business_description", "business_description"),
                ("status", "status"),
            ):
                if field in fields:
                    updates.append(f"{column}=?")
                    values.append(getattr(payload, field))
            if updates:
                updates.append("updated_at=?")
                values.extend([now, campaign_id])
                connection.execute(
                    f"UPDATE campaigns SET {', '.join(updates)} WHERE id=?", values
                )
            if payload.station_ids is not None:
                connection.execute(
                    "DELETE FROM campaign_stations WHERE campaign_id=?", (campaign_id,)
                )
                for station_id in payload.station_ids:
                    connection.execute(
                        "INSERT INTO campaign_stations(campaign_id, station_id) VALUES(?, ?)",
                        (campaign_id, station_id),
                    )
            if payload.keywords is not None:
                connection.execute(
                    "DELETE FROM campaign_keywords WHERE campaign_id=?", (campaign_id,)
                )
                for keyword in payload.keywords:
                    connection.execute(
                        """
                        INSERT INTO campaign_keywords(
                          id, campaign_id, entity_id, value, aliases_json, match_mode,
                          keyword_type, semantic_matching, semantic_threshold, enabled
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
                        """,
                        (
                            str(uuid4()),
                            campaign_id,
                            entity_id_for(keyword.value),
                            keyword.value,
                            json.dumps(keyword.aliases, ensure_ascii=False),
                            keyword.match_mode,
                            keyword.keyword_type,
                            int(bool(keyword.semantic_matching)),
                            keyword.semantic_threshold,
                        ),
                    )
            self._increment_revision(connection)
        return True

    def delete_campaign(self, campaign_id: str) -> bool:
        with self.transaction() as connection:
            cursor = connection.execute("DELETE FROM campaigns WHERE id=?", (campaign_id,))
            if cursor.rowcount == 0:
                return False
            self._increment_revision(connection)
        return True

    def get_campaign(self, campaign_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn().execute("SELECT * FROM campaigns WHERE id=?", (campaign_id,)).fetchone()
            return self._campaign(row) if row else None

    def list_campaigns(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn().execute(
                "SELECT * FROM campaigns ORDER BY created_at DESC"
            ).fetchall()
            return [self._campaign(row) for row in rows]

    def active_bindings(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn().execute(
                """
                SELECT c.id AS campaign_id, c.monitor_from_utc,
                       k.id AS keyword_id, k.entity_id, k.value, k.aliases_json,
                       k.match_mode, k.keyword_type, k.semantic_matching, k.semantic_threshold
                FROM campaigns c
                JOIN campaign_keywords k ON k.campaign_id = c.id
                WHERE c.status='active' AND k.enabled=1
                ORDER BY c.created_at, k.value
                """
            ).fetchall()
            output: list[dict[str, Any]] = []
            for row in rows:
                stations = self._conn().execute(
                    "SELECT station_id FROM campaign_stations WHERE campaign_id=?",
                    (row["campaign_id"],),
                ).fetchall()
                output.append(
                    {
                        "campaign_id": str(row["campaign_id"]),
                        "monitor_from_utc": str(row["monitor_from_utc"]),
                        "keyword_id": str(row["keyword_id"]),
                        "entity_id": str(row["entity_id"]),
                        "display_name": str(row["value"]),
                        "aliases": json.loads(row["aliases_json"]),
                        "match_mode": str(row["match_mode"]),
                        "keyword_type": str(row["keyword_type"] or "brand"),
                        "semantic_matching": bool(row["semantic_matching"]),
                        "semantic_threshold": float(row["semantic_threshold"] or 0.74),
                        "station_ids": [str(item["station_id"]) for item in stations],
                    }
                )
            return output

    def result_is_current(self, result_key: str, etag: str, revision: int) -> bool:
        with self._lock:
            row = self._conn().execute(
                "SELECT etag, campaign_revision FROM sync_objects WHERE result_key=?",
                (result_key,),
            ).fetchone()
            return bool(row and row["etag"] == etag and row["campaign_revision"] == revision)

    def replace_result_mentions(
        self,
        *,
        result_key: str,
        records: list[dict[str, Any]],
        etag: str,
        revision: int,
    ) -> int:
        now = iso(utc_now())
        with self.transaction() as connection:
            connection.execute(
                "DELETE FROM mentions WHERE source_result_s3_key=?", (result_key,)
            )
            for record in records:
                self._upsert_mention(connection, record, now)
            connection.execute(
                """
                INSERT INTO sync_objects(result_key, etag, campaign_revision, processed_at_utc)
                VALUES(?, ?, ?, ?)
                ON CONFLICT(result_key) DO UPDATE SET
                  etag=excluded.etag,
                  campaign_revision=excluded.campaign_revision,
                  processed_at_utc=excluded.processed_at_utc
                """,
                (result_key, etag, revision, now),
            )
        return len(records)

    def upsert_mention(self, mention: dict[str, Any]) -> str:
        now = iso(utc_now())
        with self.transaction() as connection:
            return self._upsert_mention(connection, mention, now)

    def _upsert_mention(
        self,
        connection: sqlite3.Connection,
        mention: dict[str, Any],
        now: str,
    ) -> str:
        existing = connection.execute(
            """
            SELECT id FROM mentions
            WHERE campaign_id=? AND source_result_s3_key=? AND source_mention_id=?
            """,
            (
                mention["campaign_id"],
                mention["source_result_s3_key"],
                mention["source_mention_id"],
            ),
        ).fetchone()
        mention_id = str(existing["id"]) if existing else str(uuid4())
        connection.execute(
            """
            INSERT INTO mentions(
              id, campaign_id, campaign_keyword_id, station_id, station_name,
              station_country_code, station_language_codes_json,
              source_result_s3_key, source_mention_id, entity_id, display_name,
              matched_alias, context, detected_language, language_probability,
              sentiment_label, sentiment_score, sentiment_margin, needs_review,
              broadcast_start_utc, broadcast_end_utc, audio_clip_start_utc,
              audio_clip_end_utc, audio_s3_key, raw_audio_s3_key,
              transcript_s3_key, created_at, updated_at
            ) VALUES(
              ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
            )
            ON CONFLICT(campaign_id, source_result_s3_key, source_mention_id)
            DO UPDATE SET
              campaign_keyword_id=excluded.campaign_keyword_id,
              station_id=excluded.station_id,
              station_name=excluded.station_name,
              station_country_code=excluded.station_country_code,
              station_language_codes_json=excluded.station_language_codes_json,
              entity_id=excluded.entity_id,
              display_name=excluded.display_name,
              matched_alias=excluded.matched_alias,
              context=excluded.context,
              detected_language=excluded.detected_language,
              language_probability=excluded.language_probability,
              sentiment_label=excluded.sentiment_label,
              sentiment_score=excluded.sentiment_score,
              sentiment_margin=excluded.sentiment_margin,
              needs_review=excluded.needs_review,
              broadcast_start_utc=excluded.broadcast_start_utc,
              broadcast_end_utc=excluded.broadcast_end_utc,
              audio_clip_start_utc=excluded.audio_clip_start_utc,
              audio_clip_end_utc=excluded.audio_clip_end_utc,
              audio_s3_key=excluded.audio_s3_key,
              raw_audio_s3_key=excluded.raw_audio_s3_key,
              transcript_s3_key=excluded.transcript_s3_key,
              updated_at=excluded.updated_at
            """,
            (
                mention_id,
                mention["campaign_id"],
                mention["campaign_keyword_id"],
                mention["station_id"],
                mention["station_name"],
                mention.get("station_country_code"),
                json.dumps(mention.get("station_language_codes", [])),
                mention["source_result_s3_key"],
                mention["source_mention_id"],
                mention["entity_id"],
                mention["display_name"],
                mention.get("matched_alias"),
                mention["context"],
                mention.get("detected_language"),
                mention.get("language_probability"),
                mention.get("sentiment_label", "neutral"),
                mention.get("sentiment_score"),
                mention.get("sentiment_margin"),
                1 if mention.get("needs_review") else 0,
                mention["broadcast_start_utc"],
                mention.get("broadcast_end_utc"),
                mention["audio_clip_start_utc"],
                mention["audio_clip_end_utc"],
                mention["audio_s3_key"],
                mention.get("raw_audio_s3_key"),
                mention.get("transcript_s3_key"),
                now,
                now,
            ),
        )
        return mention_id

    def list_mentions(
        self,
        *,
        campaign_id: str | None = None,
        station_id: str | None = None,
        sentiment: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[dict[str, Any]], int]:
        where = ["1=1"]
        values: list[Any] = []
        if campaign_id:
            where.append("m.campaign_id=?")
            values.append(campaign_id)
        if station_id:
            where.append("m.station_id=?")
            values.append(station_id)
        if sentiment:
            where.append("m.sentiment_label=?")
            values.append(sentiment)
        where_sql = " AND ".join(where)
        with self._lock:
            total = int(
                self._conn().execute(
                    f"SELECT count(*) FROM mentions m WHERE {where_sql}", values
                ).fetchone()[0]
            )
            rows = self._conn().execute(
                f"""
                SELECT m.*, c.name AS campaign_name, k.value AS keyword_value
                FROM mentions m
                JOIN campaigns c ON c.id=m.campaign_id
                JOIN campaign_keywords k ON k.id=m.campaign_keyword_id
                WHERE {where_sql}
                ORDER BY m.broadcast_start_utc DESC, m.id DESC
                LIMIT ? OFFSET ?
                """,
                [*values, limit, offset],
            ).fetchall()
            return [self._mention(row) for row in rows], total

    def mention_view_by_id(self, mention_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn().execute(
                """
                SELECT m.*, c.name AS campaign_name, k.value AS keyword_value
                FROM mentions m
                JOIN campaigns c ON c.id=m.campaign_id
                JOIN campaign_keywords k ON k.id=m.campaign_keyword_id
                WHERE m.id=?
                """,
                (mention_id,),
            ).fetchone()
            return self._mention(row) if row else None

    def sentiment_summary(self) -> dict[str, int]:
        cutoff = iso(utc_now() - timedelta(days=self._mention_window_days))
        summary = {"positive": 0, "neutral": 0, "negative": 0, "needs_review": 0}
        with self._lock:
            rows = self._conn().execute(
                """
                SELECT sentiment_label, count(*) AS count,
                       sum(CASE WHEN needs_review=1 THEN 1 ELSE 0 END) AS review_count
                FROM mentions
                WHERE broadcast_start_utc >= ?
                GROUP BY sentiment_label
                """,
                (cutoff,),
            ).fetchall()
        for row in rows:
            label = str(row["sentiment_label"])
            if label in summary:
                summary[label] = int(row["count"])
            summary["needs_review"] += int(row["review_count"] or 0)
        return summary


    def get_mention_detail_record(self, mention_id: str) -> dict[str, Any] | None:
        """Return the complete internal mention row needed for transcript assembly."""
        with self._lock:
            row = self._conn().execute(
                """
                SELECT m.*, c.name AS campaign_name, k.value AS keyword_value
                FROM mentions m
                JOIN campaigns c ON c.id=m.campaign_id
                JOIN campaign_keywords k ON k.id=m.campaign_keyword_id
                WHERE m.id=?
                """,
                (mention_id,),
            ).fetchone()
            if row is None:
                return None
            record = dict(row)
            record["station_language_codes"] = json.loads(
                str(record.get("station_language_codes_json") or "[]")
            )
            return record

    def analysis_record(self, mention_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn().execute(
                "SELECT * FROM mention_analysis WHERE mention_id=?", (mention_id,)
            ).fetchone()
            return dict(row) if row else None

    def set_analysis_status(
        self,
        mention_id: str,
        *,
        status: str,
        analysis_s3_key: str | None = None,
        model: str | None = None,
        summary: str | None = None,
        error: str | None = None,
        increment_attempts: bool = False,
    ) -> None:
        now = iso(utc_now())
        with self.transaction() as connection:
            existing = connection.execute(
                "SELECT attempts, created_at FROM mention_analysis WHERE mention_id=?",
                (mention_id,),
            ).fetchone()
            attempts = int(existing["attempts"]) if existing else 0
            if increment_attempts:
                attempts += 1
            created_at = str(existing["created_at"]) if existing else now
            connection.execute(
                """
                INSERT INTO mention_analysis(
                  mention_id, status, analysis_s3_key, model, summary, error,
                  attempts, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(mention_id) DO UPDATE SET
                  status=excluded.status,
                  analysis_s3_key=excluded.analysis_s3_key,
                  model=excluded.model,
                  summary=excluded.summary,
                  error=excluded.error,
                  attempts=excluded.attempts,
                  updated_at=excluded.updated_at
                """,
                (
                    mention_id, status, analysis_s3_key, model, summary, error,
                    attempts, created_at, now,
                ),
            )

    def list_pending_analysis(
        self,
        *,
        limit: int,
        retry_limit: int,
        settle_seconds: int = 0,
    ) -> list[str]:
        cutoff = iso(utc_now() - timedelta(seconds=max(0, settle_seconds)))
        with self._lock:
            rows = self._conn().execute(
                """
                SELECT m.id
                FROM mentions m
                LEFT JOIN mention_analysis a ON a.mention_id=m.id
                WHERE m.created_at <= ?
                  AND (
                    a.mention_id IS NULL
                    OR a.status='pending'
                    OR (a.status='error' AND a.attempts < ?)
                  )
                ORDER BY m.broadcast_start_utc ASC
                LIMIT ?
                """,
                (cutoff, retry_limit, limit),
            ).fetchall()
            return [str(row["id"]) for row in rows]

    def analysis_counts(self) -> dict[str, int]:
        counts = {"pending": 0, "ready": 0, "disabled": 0, "error": 0}
        with self._lock:
            rows = self._conn().execute(
                "SELECT status, count(*) AS count FROM mention_analysis GROUP BY status"
            ).fetchall()
        for row in rows:
            status = str(row["status"])
            if status in counts:
                counts[status] = int(row["count"])
        with self._lock:
            untracked = int(
                self._conn().execute(
                    """
                    SELECT count(*) FROM mentions m
                    LEFT JOIN mention_analysis a ON a.mention_id=m.id
                    WHERE a.mention_id IS NULL
                    """
                ).fetchone()[0]
            )
        counts["pending"] += untracked
        return counts

    def mention_exists_for_keyword_transcript(
        self,
        *,
        campaign_keyword_id: str,
        transcript_s3_key: str,
    ) -> bool:
        with self._lock:
            row = self._conn().execute(
                """
                SELECT 1 FROM mentions
                WHERE campaign_keyword_id=? AND transcript_s3_key=?
                LIMIT 1
                """,
                (campaign_keyword_id, transcript_s3_key),
            ).fetchone()
            return row is not None

    def semantic_scan_is_current(
        self,
        *,
        campaign_keyword_id: str,
        transcript_group_key: str,
        source_fingerprint: str,
        campaign_revision: int,
    ) -> bool:
        with self._lock:
            row = self._conn().execute(
                """
                SELECT source_fingerprint, campaign_revision, status
                FROM semantic_scan_objects
                WHERE campaign_keyword_id=? AND transcript_group_key=?
                """,
                (campaign_keyword_id, transcript_group_key),
            ).fetchone()
            return bool(
                row
                and str(row["source_fingerprint"]) == source_fingerprint
                and int(row["campaign_revision"]) == campaign_revision
                and str(row["status"]) != "error"
            )

    def record_semantic_scan(
        self,
        *,
        campaign_keyword_id: str,
        transcript_group_key: str,
        source_fingerprint: str,
        campaign_revision: int,
        status: str,
        error: str | None = None,
    ) -> None:
        now = iso(utc_now())
        with self.transaction() as connection:
            connection.execute(
                """
                INSERT INTO semantic_scan_objects(
                  campaign_keyword_id, transcript_group_key, source_fingerprint,
                  campaign_revision, status, error, processed_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(campaign_keyword_id, transcript_group_key) DO UPDATE SET
                  source_fingerprint=excluded.source_fingerprint,
                  campaign_revision=excluded.campaign_revision,
                  status=excluded.status,
                  error=excluded.error,
                  processed_at_utc=excluded.processed_at_utc
                """,
                (
                    campaign_keyword_id, transcript_group_key, source_fingerprint,
                    campaign_revision, status, error, now,
                ),
            )

    def semantic_counts(self) -> dict[str, int]:
        counts = {"matched": 0, "not_matched": 0, "error": 0}
        with self._lock:
            rows = self._conn().execute(
                "SELECT status, count(*) AS count FROM semantic_scan_objects GROUP BY status"
            ).fetchall()
        for row in rows:
            status = str(row["status"])
            if status in counts:
                counts[status] = int(row["count"])
        return counts

    def update_mention_from_analysis(
        self,
        mention_id: str,
        *,
        sentiment: str | None,
        confidence: float | None,
        needs_review: bool,
    ) -> None:
        # The pilot DB keeps the legacy three-label dashboard schema. A nuanced
        # "mixed" result remains in the analysis document and is shown in detail;
        # the dashboard uses neutral + review so old clients stay compatible.
        label = str(sentiment or "neutral").lower()
        if label == "mixed":
            label = "neutral"
            needs_review = True
        if label not in {"positive", "neutral", "negative"}:
            label = "neutral"
            needs_review = True
        with self.transaction() as connection:
            connection.execute(
                """
                UPDATE mentions
                SET sentiment_label=?, sentiment_score=?, sentiment_margin=NULL,
                    needs_review=?, updated_at=?
                WHERE id=?
                """,
                (label, confidence, int(needs_review), iso(utc_now()), mention_id),
            )

    def mention_audio(self, mention_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn().execute(
                "SELECT id, audio_s3_key FROM mentions WHERE id=?", (mention_id,)
            ).fetchone()
            if row is None:
                return None
            return {"id": str(row["id"]), "audio_s3_key": str(row["audio_s3_key"])}

    def _campaign(self, row: sqlite3.Row) -> dict[str, Any]:
        campaign_id = str(row["id"])
        stations = self._conn().execute(
            "SELECT station_id FROM campaign_stations WHERE campaign_id=? ORDER BY station_id",
            (campaign_id,),
        ).fetchall()
        keywords = self._conn().execute(
            "SELECT * FROM campaign_keywords WHERE campaign_id=? ORDER BY value",
            (campaign_id,),
        ).fetchall()
        window_cutoff = iso(utc_now() - timedelta(days=self._mention_window_days))
        mentions_7d = int(
            self._conn().execute(
                """
                SELECT count(*) FROM mentions
                WHERE campaign_id=? AND broadcast_start_utc >= ?
                """,
                (campaign_id, window_cutoff),
            ).fetchone()[0]
        )
        return {
            "id": campaign_id,
            "name": str(row["name"]),
            "objective": str(row["objective"]),
            "business_name": row["business_name"],
            "business_description": row["business_description"],
            "status": str(row["status"]),
            "monitor_from_utc": str(row["monitor_from_utc"]),
            "station_ids": [str(item["station_id"]) for item in stations],
            "keywords": [
                {
                    "id": str(item["id"]),
                    "entity_id": str(item["entity_id"]),
                    "value": str(item["value"]),
                    "aliases": json.loads(item["aliases_json"]),
                    "match_mode": str(item["match_mode"]),
                    "keyword_type": str(item["keyword_type"] or "brand"),
                    "semantic_matching": bool(item["semantic_matching"]),
                    "semantic_threshold": float(item["semantic_threshold"] or 0.74),
                    "enabled": bool(item["enabled"]),
                }
                for item in keywords
            ],
            "mentions_7d": mentions_7d,
            "created_at": str(row["created_at"]),
            "updated_at": str(row["updated_at"]),
        }

    def _mention(self, row: sqlite3.Row) -> dict[str, Any]:
        start = datetime.fromisoformat(str(row["broadcast_start_utc"]).replace("Z", "+00:00"))
        end_text = row["broadcast_end_utc"]
        end = datetime.fromisoformat(str(end_text).replace("Z", "+00:00")) if end_text else start
        clip_start = datetime.fromisoformat(str(row["audio_clip_start_utc"]).replace("Z", "+00:00"))
        clip_end = datetime.fromisoformat(str(row["audio_clip_end_utc"]).replace("Z", "+00:00"))
        audio_duration = max(0.0, (clip_end - clip_start).total_seconds())
        pad = self._mention_audio_pad_seconds
        playback_start = max(0.0, (start - clip_start).total_seconds() - pad)
        playback_end = min(
            audio_duration,
            max(playback_start + 0.5, (end - clip_start).total_seconds() + pad),
        )
        return {
            "id": str(row["id"]),
            "campaign_id": str(row["campaign_id"]),
            "campaign_name": str(row["campaign_name"]),
            "keyword": str(row["keyword_value"]),
            "matched_alias": row["matched_alias"],
            "station": {
                "id": str(row["station_id"]),
                "name": str(row["station_name"]),
                "country_code": row["station_country_code"],
                "language_codes": json.loads(row["station_language_codes_json"]),
                "connected": True,
                "enabled": True,
            },
            "context": str(row["context"]),
            "detected_language": row["detected_language"],
            "language_probability": row["language_probability"],
            "sentiment": {
                "label": str(row["sentiment_label"]),
                "score": row["sentiment_score"],
                "margin": row["sentiment_margin"],
                "needs_review": bool(row["needs_review"]),
            },
            "broadcast_start_utc": str(row["broadcast_start_utc"]),
            "broadcast_end_utc": row["broadcast_end_utc"],
            "audio_duration_seconds": audio_duration,
            "playback_start_seconds": playback_start,
            "playback_end_seconds": playback_end,
            "audio_available": bool(row["audio_s3_key"]),
        }

    def _conn(self) -> sqlite3.Connection:
        if self._connection is None:
            raise RuntimeError("Database is not connected")
        return self._connection
