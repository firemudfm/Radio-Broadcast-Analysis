"""Migration registry for the shared-station pipeline.

Versions 0001 and 0002 are reserved for the pre-existing v0.3 core schema
(`app/db.py`) and the v0.4 catalogue schema (`app/db_catalog.py`); the runner
adopts them rather than re-creating them. New work starts at 0003.

Manual DOWN for 0003-0005 (emergency only, forward-only by policy):
    DROP TABLE IF EXISTS processing_failures;
    DROP TABLE IF EXISTS worker_heartbeats;
    DROP TABLE IF EXISTS inbox_messages;
    DROP TABLE IF EXISTS outbox_events;
    DROP TABLE IF EXISTS analysis_results;
    DROP TABLE IF EXISTS analysis_jobs;
    DROP TABLE IF EXISTS mention_keywords;
    DROP TABLE IF EXISTS mention_campaigns;
    DROP TABLE IF EXISTS mention_events;
    DROP TABLE IF EXISTS conversation_sessions;
    DROP TABLE IF EXISTS transcripts;
    DROP TABLE IF EXISTS transcription_jobs;
    DROP TABLE IF EXISTS audio_segments;
    DROP TABLE IF EXISTS station_sessions;
    DROP TABLE IF EXISTS station_keyword_index_versions;
    DROP TABLE IF EXISTS station_keyword_bindings;
    DROP TABLE IF EXISTS station_subscriptions;
    DELETE FROM schema_migrations WHERE version >= 3;
"""
from __future__ import annotations

from . import Migration

# --- 0003: planner ------------------------------------------------------------
#
# One row per DISTINCT station, never per (campaign, station). That uniqueness
# constraint is the mechanism that makes station sharing structural rather than
# a property some code path has to remember to enforce.

_M0003 = Migration(
    version=3,
    name="station_subscriptions_and_keyword_index",
    statements=(
        """
        CREATE TABLE IF NOT EXISTS station_subscriptions (
          station_id TEXT PRIMARY KEY,
          station_uuid TEXT,
          display_name TEXT NOT NULL DEFAULT '',
          stream_url TEXT,
          language_codes_json TEXT NOT NULL DEFAULT '[]',
          country_code TEXT,
          reference_count INTEGER NOT NULL DEFAULT 0,
          state TEXT NOT NULL DEFAULT 'desired'
            CHECK (state IN ('desired','pending_capacity','starting','active',
                             'degraded','winding_down','stopped')),
          state_reason TEXT,
          shard_index INTEGER NOT NULL DEFAULT 0,
          keyword_index_version INTEGER NOT NULL DEFAULT 0,
          winddown_after_utc TEXT,
          last_error TEXT,
          created_at_utc TEXT NOT NULL,
          updated_at_utc TEXT NOT NULL
        )
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_station_subscriptions_state
          ON station_subscriptions(state, shard_index)
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_station_subscriptions_shard
          ON station_subscriptions(shard_index, state)
        """,
        # The combined index: which campaign keywords apply to which station.
        # Mapping rows preserve keyword_id and campaign_id so a match can be
        # attributed back to every campaign that asked for it.
        """
        CREATE TABLE IF NOT EXISTS station_keyword_bindings (
          station_id TEXT NOT NULL,
          keyword_id TEXT NOT NULL,
          campaign_id TEXT NOT NULL,
          entity_id TEXT NOT NULL,
          canonical_value TEXT NOT NULL,
          keyword_type TEXT NOT NULL DEFAULT 'brand',
          match_mode TEXT NOT NULL DEFAULT 'tokens',
          semantic_matching INTEGER NOT NULL DEFAULT 0,
          semantic_threshold REAL NOT NULL DEFAULT 0.74,
          aliases_json TEXT NOT NULL DEFAULT '[]',
          languages_json TEXT NOT NULL DEFAULT '[]',
          content_policy_json TEXT NOT NULL DEFAULT '{}',
          created_at_utc TEXT NOT NULL,
          PRIMARY KEY (station_id, keyword_id, campaign_id)
        )
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_station_keyword_bindings_station
          ON station_keyword_bindings(station_id)
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_station_keyword_bindings_campaign
          ON station_keyword_bindings(campaign_id)
        """,
        # Versions are content-addressed: the index is republished only when
        # the fingerprint changes, so an edit that does not alter effective
        # content does not churn every listener.
        """
        CREATE TABLE IF NOT EXISTS station_keyword_index_versions (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          station_id TEXT NOT NULL,
          version INTEGER NOT NULL,
          fingerprint TEXT NOT NULL,
          keyword_count INTEGER NOT NULL DEFAULT 0,
          alias_count INTEGER NOT NULL DEFAULT 0,
          campaign_count INTEGER NOT NULL DEFAULT 0,
          payload_json TEXT NOT NULL,
          published_at_utc TEXT NOT NULL,
          UNIQUE (station_id, version)
        )
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_keyword_index_versions_station
          ON station_keyword_index_versions(station_id, version DESC)
        """,
    ),
)

# --- 0004: listener, ASR, conversations, mentions -----------------------------
#
# mention_events is the physical broadcast moment. It carries NO campaign_id and
# NO keyword_id column: attribution lives entirely in the mapping tables, which
# is what makes "one transcription, one analysis, many campaigns" true by
# construction rather than by discipline.

_M0004 = Migration(
    version=4,
    name="segments_transcripts_conversations_mentions",
    statements=(
        """
        CREATE TABLE IF NOT EXISTS station_sessions (
          station_session_id TEXT PRIMARY KEY,
          station_id TEXT NOT NULL,
          generation INTEGER NOT NULL DEFAULT 1,
          shard_index INTEGER NOT NULL DEFAULT 0,
          worker_id TEXT,
          stream_url_hash TEXT,
          codec TEXT,
          bitrate_kbps INTEGER,
          sample_rate INTEGER,
          status TEXT NOT NULL DEFAULT 'connecting'
            CHECK (status IN ('connecting','streaming','reconnecting','stopped','failed')),
          last_audio_at_utc TEXT,
          last_error TEXT,
          started_at_utc TEXT NOT NULL,
          ended_at_utc TEXT
        )
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_station_sessions_station
          ON station_sessions(station_id, started_at_utc DESC)
        """,
        """
        CREATE TABLE IF NOT EXISTS audio_segments (
          segment_id TEXT PRIMARY KEY,
          station_id TEXT NOT NULL,
          station_session_id TEXT NOT NULL,
          sequence_number INTEGER NOT NULL,
          started_at_utc TEXT NOT NULL,
          ended_at_utc TEXT NOT NULL,
          duration_ms INTEGER NOT NULL,
          content_class TEXT NOT NULL,
          content_class_confidence REAL NOT NULL DEFAULT 0.0,
          classifier_signals_json TEXT NOT NULL DEFAULT '{}',
          storage_backend TEXT NOT NULL CHECK (storage_backend IN ('local','s3')),
          storage_path TEXT,
          storage_bucket TEXT,
          storage_key TEXT,
          sha256 TEXT NOT NULL,
          size_bytes INTEGER NOT NULL,
          disposition TEXT NOT NULL DEFAULT 'pending'
            CHECK (disposition IN ('pending','retained','disposable','deleted','failed')),
          keyword_index_version INTEGER NOT NULL DEFAULT 0,
          trace_id TEXT NOT NULL,
          created_at_utc TEXT NOT NULL,
          updated_at_utc TEXT NOT NULL,
          UNIQUE (station_session_id, sequence_number)
        )
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_audio_segments_disposition
          ON audio_segments(disposition, updated_at_utc)
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_audio_segments_station_time
          ON audio_segments(station_id, started_at_utc)
        """,
        """
        CREATE TABLE IF NOT EXISTS transcription_jobs (
          transcription_job_id TEXT PRIMARY KEY,
          segment_id TEXT NOT NULL UNIQUE
            REFERENCES audio_segments(segment_id) ON DELETE CASCADE,
          station_id TEXT NOT NULL,
          status TEXT NOT NULL DEFAULT 'pending'
            CHECK (status IN ('pending','running','succeeded','failed','abandoned')),
          attempts INTEGER NOT NULL DEFAULT 0,
          lease_expires_at_utc TEXT,
          worker_id TEXT,
          last_error_code TEXT,
          last_error TEXT,
          trace_id TEXT NOT NULL,
          created_at_utc TEXT NOT NULL,
          updated_at_utc TEXT NOT NULL
        )
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_transcription_jobs_status
          ON transcription_jobs(status, lease_expires_at_utc)
        """,
        """
        CREATE TABLE IF NOT EXISTS transcripts (
          transcript_id TEXT PRIMARY KEY,
          segment_id TEXT NOT NULL
            REFERENCES audio_segments(segment_id) ON DELETE CASCADE,
          station_id TEXT NOT NULL,
          conversation_id TEXT,
          asr_pass TEXT NOT NULL DEFAULT 'a' CHECK (asr_pass IN ('a','b')),
          text TEXT NOT NULL,
          detected_language TEXT,
          language_probability REAL,
          segments_json TEXT NOT NULL DEFAULT '[]',
          words_json TEXT NOT NULL DEFAULT '[]',
          model_name TEXT NOT NULL,
          model_revision TEXT,
          compute_type TEXT,
          beam_size INTEGER,
          duration_ms INTEGER NOT NULL DEFAULT 0,
          created_at_utc TEXT NOT NULL,
          UNIQUE (segment_id, asr_pass)
        )
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_transcripts_conversation
          ON transcripts(conversation_id, created_at_utc)
        """,
        """
        CREATE TABLE IF NOT EXISTS conversation_sessions (
          conversation_id TEXT PRIMARY KEY,
          station_id TEXT NOT NULL,
          station_session_id TEXT NOT NULL,
          state TEXT NOT NULL DEFAULT 'candidate'
            CHECK (state IN ('idle','candidate','open','closing','closed','failed')),
          close_reason TEXT,
          first_sequence_number INTEGER NOT NULL,
          last_sequence_number INTEGER NOT NULL,
          started_at_utc TEXT NOT NULL,
          ended_at_utc TEXT,
          duration_ms INTEGER NOT NULL DEFAULT 0,
          transcript_text TEXT NOT NULL DEFAULT '',
          detected_language TEXT,
          content_type TEXT NOT NULL DEFAULT 'unknown',
          content_type_confidence REAL NOT NULL DEFAULT 0.0,
          missing_sequences_json TEXT NOT NULL DEFAULT '[]',
          trace_id TEXT NOT NULL,
          created_at_utc TEXT NOT NULL,
          updated_at_utc TEXT NOT NULL
        )
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_conversation_sessions_state
          ON conversation_sessions(state, updated_at_utc)
        """,
        """
        CREATE TABLE IF NOT EXISTS mention_events (
          mention_id TEXT PRIMARY KEY,
          conversation_id TEXT NOT NULL UNIQUE
            REFERENCES conversation_sessions(conversation_id) ON DELETE CASCADE,
          station_id TEXT NOT NULL,
          station_name TEXT NOT NULL DEFAULT '',
          content_type TEXT NOT NULL DEFAULT 'unknown',
          detected_language TEXT,
          language_probability REAL,
          broadcast_start_utc TEXT NOT NULL,
          broadcast_end_utc TEXT,
          transcript_id TEXT,
          evidence_storage_key TEXT,
          evidence_available INTEGER NOT NULL DEFAULT 0,
          result_s3_key TEXT,
          trace_id TEXT NOT NULL,
          created_at_utc TEXT NOT NULL,
          updated_at_utc TEXT NOT NULL
        )
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_mention_events_station_time
          ON mention_events(station_id, broadcast_start_utc DESC)
        """,
        """
        CREATE TABLE IF NOT EXISTS mention_campaigns (
          mention_id TEXT NOT NULL
            REFERENCES mention_events(mention_id) ON DELETE CASCADE,
          campaign_id TEXT NOT NULL,
          included INTEGER NOT NULL DEFAULT 1,
          exclusion_reason TEXT,
          created_at_utc TEXT NOT NULL,
          PRIMARY KEY (mention_id, campaign_id)
        )
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_mention_campaigns_campaign
          ON mention_campaigns(campaign_id, created_at_utc DESC)
        """,
        """
        CREATE TABLE IF NOT EXISTS mention_keywords (
          mention_id TEXT NOT NULL
            REFERENCES mention_events(mention_id) ON DELETE CASCADE,
          keyword_id TEXT NOT NULL,
          campaign_id TEXT NOT NULL,
          canonical_value TEXT NOT NULL,
          matched_text TEXT NOT NULL,
          match_level TEXT NOT NULL,
          confirmed INTEGER NOT NULL DEFAULT 0,
          start_ms INTEGER NOT NULL DEFAULT 0,
          end_ms INTEGER NOT NULL DEFAULT 0,
          start_char INTEGER NOT NULL DEFAULT 0,
          end_char INTEGER NOT NULL DEFAULT 0,
          confidence REAL NOT NULL DEFAULT 1.0,
          created_at_utc TEXT NOT NULL,
          PRIMARY KEY (mention_id, keyword_id)
        )
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_mention_keywords_keyword
          ON mention_keywords(keyword_id)
        """,
        """
        CREATE TABLE IF NOT EXISTS analysis_jobs (
          analysis_job_id TEXT PRIMARY KEY,
          mention_id TEXT NOT NULL UNIQUE
            REFERENCES mention_events(mention_id) ON DELETE CASCADE,
          conversation_id TEXT NOT NULL,
          station_id TEXT NOT NULL,
          status TEXT NOT NULL DEFAULT 'pending'
            CHECK (status IN ('pending','running','succeeded','failed','abandoned')),
          attempts INTEGER NOT NULL DEFAULT 0,
          lease_expires_at_utc TEXT,
          worker_id TEXT,
          last_error_code TEXT,
          last_error TEXT,
          trace_id TEXT NOT NULL,
          created_at_utc TEXT NOT NULL,
          updated_at_utc TEXT NOT NULL
        )
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_analysis_jobs_status
          ON analysis_jobs(status, lease_expires_at_utc)
        """,
        """
        CREATE TABLE IF NOT EXISTS analysis_results (
          mention_id TEXT PRIMARY KEY
            REFERENCES mention_events(mention_id) ON DELETE CASCADE,
          analysis_job_id TEXT NOT NULL,
          schema_version TEXT NOT NULL DEFAULT '1',
          status TEXT NOT NULL DEFAULT 'ready'
            CHECK (status IN ('ready','fallback','disabled','error')),
          model TEXT,
          content_type TEXT,
          language TEXT,
          relevant INTEGER NOT NULL DEFAULT 0,
          summary TEXT,
          translated_summary TEXT,
          main_topic TEXT,
          sentiment TEXT,
          speaker_stance TEXT,
          urgency TEXT,
          entities_json TEXT NOT NULL DEFAULT '[]',
          key_points_json TEXT NOT NULL DEFAULT '[]',
          evidence_json TEXT NOT NULL DEFAULT '[]',
          confidence REAL,
          needs_review INTEGER NOT NULL DEFAULT 0,
          error TEXT,
          result_s3_key TEXT,
          created_at_utc TEXT NOT NULL,
          updated_at_utc TEXT NOT NULL
        )
        """,
    ),
)

# --- 0005: reliability primitives --------------------------------------------

_M0005 = Migration(
    version=5,
    name="outbox_inbox_heartbeats_failures",
    statements=(
        """
        CREATE TABLE IF NOT EXISTS outbox_events (
          event_id TEXT PRIMARY KEY,
          queue_name TEXT NOT NULL,
          message_group_id TEXT NOT NULL,
          message_deduplication_id TEXT NOT NULL,
          payload_json TEXT NOT NULL,
          status TEXT NOT NULL DEFAULT 'pending'
            CHECK (status IN ('pending','sending','sent','failed')),
          attempts INTEGER NOT NULL DEFAULT 0,
          available_at_utc TEXT NOT NULL,
          lease_expires_at_utc TEXT,
          sqs_message_id TEXT,
          sent_at_utc TEXT,
          last_error TEXT,
          trace_id TEXT,
          created_at_utc TEXT NOT NULL,
          updated_at_utc TEXT NOT NULL,
          UNIQUE (queue_name, message_deduplication_id)
        )
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_outbox_dispatch
          ON outbox_events(status, available_at_utc)
        """,
        # The real exactly-once guarantee. SQS deduplication lasts only 5
        # minutes; this table has no expiry inside the retention window.
        """
        CREATE TABLE IF NOT EXISTS inbox_messages (
          queue_name TEXT NOT NULL,
          message_deduplication_id TEXT NOT NULL,
          message_id TEXT,
          status TEXT NOT NULL DEFAULT 'processing'
            CHECK (status IN ('processing','processed','failed')),
          result_reference TEXT,
          error_code TEXT,
          receive_count INTEGER NOT NULL DEFAULT 1,
          trace_id TEXT,
          first_seen_at_utc TEXT NOT NULL,
          processed_at_utc TEXT,
          PRIMARY KEY (queue_name, message_deduplication_id)
        )
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_inbox_processed
          ON inbox_messages(status, processed_at_utc)
        """,
        """
        CREATE TABLE IF NOT EXISTS worker_heartbeats (
          worker_id TEXT PRIMARY KEY,
          role TEXT NOT NULL,
          shard_index INTEGER NOT NULL DEFAULT 0,
          shard_count INTEGER NOT NULL DEFAULT 1,
          status TEXT NOT NULL DEFAULT 'ok',
          detail_json TEXT NOT NULL DEFAULT '{}',
          pipeline_mode TEXT NOT NULL DEFAULT 'legacy',
          started_at_utc TEXT NOT NULL,
          last_seen_utc TEXT NOT NULL
        )
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_worker_heartbeats_role
          ON worker_heartbeats(role, last_seen_utc DESC)
        """,
        """
        CREATE TABLE IF NOT EXISTS processing_failures (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          component TEXT NOT NULL,
          error_code TEXT NOT NULL,
          retryable INTEGER NOT NULL DEFAULT 0,
          station_id TEXT,
          segment_id TEXT,
          conversation_id TEXT,
          mention_id TEXT,
          job_id TEXT,
          queue_name TEXT,
          message_deduplication_id TEXT,
          message TEXT NOT NULL,
          detail TEXT,
          trace_id TEXT,
          created_at_utc TEXT NOT NULL
        )
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_processing_failures_time
          ON processing_failures(created_at_utc DESC)
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_processing_failures_code
          ON processing_failures(error_code, created_at_utc DESC)
        """,
    ),
)

# --- 0006: campaign content policy -------------------------------------------
#
# Additive and nullable: an existing campaign row with NULL policy resolves to
# the global defaults, so behaviour is unchanged until a client sends one.

_M0006 = Migration(
    version=6,
    name="campaign_content_policy",
    statements=(
        """
        CREATE TABLE IF NOT EXISTS campaign_content_policies (
          campaign_id TEXT PRIMARY KEY,
          policy_json TEXT NOT NULL,
          created_at_utc TEXT NOT NULL,
          updated_at_utc TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS campaign_keyword_aliases (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          keyword_id TEXT NOT NULL,
          campaign_id TEXT NOT NULL,
          value TEXT NOT NULL,
          language TEXT,
          alias_kind TEXT NOT NULL DEFAULT 'canonical',
          created_at_utc TEXT NOT NULL,
          UNIQUE (keyword_id, value, alias_kind)
        )
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_campaign_keyword_aliases_keyword
          ON campaign_keyword_aliases(keyword_id)
        """,
    ),
)

MIGRATIONS: tuple[Migration, ...] = (_M0003, _M0004, _M0005, _M0006)

__all__ = ["MIGRATIONS"]
