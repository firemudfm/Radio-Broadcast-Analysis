from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import BeforeValidator, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

# Enumerated settings. Declaring them as Literal makes pydantic reject unknown
# values at startup instead of letting a typo select a silent no-op path.
PipelineMode = Literal["legacy", "shared_sqs"]
QueueBackend = Literal["sqs", "memory"]
SegmentStoreBackend = Literal["local", "s3"]
AudioClassifierBackend = Literal["vad_energy", "yamnet", "passthrough"]
ContentClassifierBackend = Literal["rules", "passthrough"]
AsrBackend = Literal["faster_whisper", "fake"]
LogFormat = Literal["text", "json"]


def _split_csv(value: object) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    text = str(value).strip()
    if not text:
        return []
    if text.startswith("["):
        loaded = json.loads(text)
        if not isinstance(loaded, list):
            raise ValueError("Expected a JSON array")
        return [str(item).strip() for item in loaded if str(item).strip()]
    return [item.strip() for item in text.split(",") if item.strip()]


CsvList = Annotated[list[str], NoDecode, BeforeValidator(_split_csv)]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=None, extra="ignore", case_sensitive=True)

    APP_ENV: str = "development"
    LOG_LEVEL: str = "INFO"
    RADIO_API_VERSION: str = "0.4.0"
    RADIO_API_MODE: str = "open-pilot"

    AWS_REGION: str = "eu-north-1"
    AWS_DEFAULT_REGION: str | None = None
    RADIO_S3_BUCKET: str
    RADIO_RESULTS_PREFIX: str = "results/intelligence/"
    RADIO_ANALYSIS_PREFIX: str = "results/conversation-analysis/"
    RADIO_KEYWORDS_KEY: str = "config/keywords/keywords.json"
    RADIO_RAW_PREFIX: str = "raw-audio/"
    RADIO_TRANSCRIPTS_PREFIX: str = "transcripts/"

    RADIO_API_HOST: str = "127.0.0.1"
    RADIO_API_PORT: int = 8788
    RADIO_API_CORS_ORIGINS: CsvList = Field(
        default_factory=lambda: [
            "http://localhost:5173",
            "http://localhost:5175",
            "http://127.0.0.1:5173",
            "http://127.0.0.1:5175",
        ]
    )

    RADIO_DATABASE_PATH: Path = Path("/var/lib/firemud/radio-intelligence-api/radio.db")
    RADIO_AUDIO_TOKEN_SECRET: SecretStr
    RADIO_AUDIO_TOKEN_TTL_SECONDS: int = 600

    RADIO_SYNC_ENABLED: bool = True
    RADIO_SYNC_ON_STARTUP: bool = True
    RADIO_SYNC_INTERVAL_SECONDS: int = 15
    RADIO_SYNC_LOOKBACK_DAYS: int = 14
    RADIO_SYNC_MAX_OBJECTS: int = 1000

    RADIO_STATION_CONFIG_DIR: Path = Path("/etc/radio-pipeline/stations")
    RADIO_STATION_METADATA_PATH: Path = Path("/etc/firemud/radio-stations.json")
    RADIO_STATION_REFRESH_SECONDS: int = 60

    # Dynamic conversation assembly. The service walks neighboring transcript
    # groups and returns the complete contiguous speech session around a mention.
    # Session boundaries come from real speech gaps, not a fixed before/after window.
    RADIO_CONVERSATION_MAX_TRANSCRIPTS: int = 200
    RADIO_CONVERSATION_SCAN_CHUNKS: int = 6
    RADIO_CONVERSATION_SESSION_GAP_SECONDS: float = 30.0
    RADIO_CONVERSATION_MAX_DURATION_SECONDS: int = 1800
    RADIO_CONVERSATION_MAX_CHARACTERS: int = 120_000

    # Local small LLM. It is intentionally bound to localhost and called only by FastAPI.
    RADIO_LLM_ENABLED: bool = True
    RADIO_LLM_BASE_URL: str = "http://127.0.0.1:8790"
    RADIO_LLM_MODEL: str = "qwen3-0.6b-q8"
    RADIO_LLM_TIMEOUT_SECONDS: int = 90
    RADIO_LLM_MAX_INPUT_CHARACTERS: int = 40_000
    RADIO_LLM_MAX_OUTPUT_TOKENS: int = 480
    RADIO_LLM_TEMPERATURE: float = 0.1

    # Shared analysis queue. One bounded worker is used for all campaigns.
    RADIO_ANALYSIS_WORKER_ENABLED: bool = True
    RADIO_ANALYSIS_WORKER_POLL_SECONDS: int = 20
    RADIO_ANALYSIS_WORKER_BATCH_SIZE: int = 2
    RADIO_ANALYSIS_RETRY_LIMIT: int = 3
    RADIO_ANALYSIS_SETTLE_SECONDS: int = 360

    # Cross-language semantic discovery. This is opt-in per keyword and uses the
    # same shared local LLM; it never spawns a model per campaign.
    RADIO_SEMANTIC_DISCOVERY_ENABLED: bool = True
    RADIO_SEMANTIC_SCAN_LOOKBACK_DAYS: int = 7
    RADIO_SEMANTIC_GROUPS_PER_CYCLE: int = 1
    RADIO_SEMANTIC_KEYWORDS_PER_GROUP: int = 10
    RADIO_SEMANTIC_DEFAULT_THRESHOLD: float = 0.74
    RADIO_SEMANTIC_SETTLE_SECONDS: int = 120
    RADIO_SEMANTIC_RESULTS_PREFIX: str = "results/semantic-matches/"

    # Rolling window (in days) for the dashboard sentiment summary and the
    # per-campaign recent-mention counts. 1 means "last 24 hours".
    RADIO_MENTION_WINDOW_DAYS: int = 7

    # Playback padding (seconds) around the matched keyword inside the mention's
    # clean-speech clip. Values >= the clip length mean "play the whole captured
    # discussion segment", not just the keyword moment.
    RADIO_MENTION_AUDIO_PAD_SECONDS: float = 2.0

    @field_validator(
        "RADIO_RESULTS_PREFIX",
        "RADIO_ANALYSIS_PREFIX",
        "RADIO_RAW_PREFIX",
        "RADIO_TRANSCRIPTS_PREFIX",
        "RADIO_SEMANTIC_RESULTS_PREFIX",
    )
    @classmethod
    def normalize_prefix(cls, value: str) -> str:
        cleaned = value.strip().lstrip("/")
        return cleaned if cleaned.endswith("/") else f"{cleaned}/"

    @field_validator("RADIO_KEYWORDS_KEY")
    @classmethod
    def normalize_key(cls, value: str) -> str:
        return value.strip().lstrip("/")

    @field_validator("RADIO_AUDIO_TOKEN_SECRET")
    @classmethod
    def validate_audio_secret(cls, value: SecretStr) -> SecretStr:
        if len(value.get_secret_value()) < 32:
            raise ValueError("RADIO_AUDIO_TOKEN_SECRET must contain at least 32 characters")
        return value

    @field_validator("RADIO_AUDIO_TOKEN_TTL_SECONDS")
    @classmethod
    def validate_audio_ttl(cls, value: int) -> int:
        if not 30 <= value <= 3600:
            raise ValueError("RADIO_AUDIO_TOKEN_TTL_SECONDS must be between 30 and 3600")
        return value

    @field_validator(
        "RADIO_SYNC_INTERVAL_SECONDS",
        "RADIO_STATION_REFRESH_SECONDS",
        "RADIO_ANALYSIS_WORKER_POLL_SECONDS",
    )
    @classmethod
    def validate_interval(cls, value: int) -> int:
        if value < 5:
            raise ValueError("Polling intervals must be at least 5 seconds")
        return value

    @field_validator("RADIO_SYNC_LOOKBACK_DAYS")
    @classmethod
    def validate_lookback(cls, value: int) -> int:
        if not 1 <= value <= 31:
            raise ValueError("RADIO_SYNC_LOOKBACK_DAYS must be between 1 and 31")
        return value

    @field_validator("RADIO_SYNC_MAX_OBJECTS")
    @classmethod
    def validate_max_objects(cls, value: int) -> int:
        if not 1 <= value <= 5000:
            raise ValueError("RADIO_SYNC_MAX_OBJECTS must be between 1 and 5000")
        return value

    @field_validator("RADIO_CONVERSATION_MAX_TRANSCRIPTS")
    @classmethod
    def validate_max_transcripts(cls, value: int) -> int:
        if not 1 <= value <= 500:
            raise ValueError("RADIO_CONVERSATION_MAX_TRANSCRIPTS must be between 1 and 500")
        return value

    @field_validator("RADIO_CONVERSATION_SCAN_CHUNKS")
    @classmethod
    def validate_scan_chunks(cls, value: int) -> int:
        if not 0 <= value <= 24:
            raise ValueError("RADIO_CONVERSATION_SCAN_CHUNKS must be between 0 and 24")
        return value

    @field_validator("RADIO_CONVERSATION_SESSION_GAP_SECONDS")
    @classmethod
    def validate_session_gap(cls, value: float) -> float:
        if not 1.0 <= value <= 300.0:
            raise ValueError("RADIO_CONVERSATION_SESSION_GAP_SECONDS must be between 1 and 300")
        return value

    @field_validator("RADIO_CONVERSATION_MAX_DURATION_SECONDS")
    @classmethod
    def validate_conversation_duration(cls, value: int) -> int:
        if not 60 <= value <= 7200:
            raise ValueError("RADIO_CONVERSATION_MAX_DURATION_SECONDS must be between 60 and 7200")
        return value

    @field_validator("RADIO_CONVERSATION_MAX_CHARACTERS", "RADIO_LLM_MAX_INPUT_CHARACTERS")
    @classmethod
    def validate_character_limit(cls, value: int) -> int:
        if not 1000 <= value <= 500_000:
            raise ValueError("Transcript character limits must be between 1000 and 500000")
        return value

    @field_validator("RADIO_ANALYSIS_SETTLE_SECONDS")
    @classmethod
    def validate_analysis_settle(cls, value: int) -> int:
        if not 0 <= value <= 3600:
            raise ValueError("RADIO_ANALYSIS_SETTLE_SECONDS must be between 0 and 3600")
        return value

    @field_validator("RADIO_LLM_TIMEOUT_SECONDS")
    @classmethod
    def validate_llm_timeout(cls, value: int) -> int:
        if not 5 <= value <= 300:
            raise ValueError("RADIO_LLM_TIMEOUT_SECONDS must be between 5 and 300")
        return value

    @field_validator("RADIO_LLM_MAX_OUTPUT_TOKENS")
    @classmethod
    def validate_llm_tokens(cls, value: int) -> int:
        if not 64 <= value <= 2048:
            raise ValueError("RADIO_LLM_MAX_OUTPUT_TOKENS must be between 64 and 2048")
        return value

    @field_validator("RADIO_LLM_TEMPERATURE")
    @classmethod
    def validate_llm_temperature(cls, value: float) -> float:
        if not 0.0 <= value <= 1.0:
            raise ValueError("RADIO_LLM_TEMPERATURE must be between 0 and 1")
        return value

    @field_validator("RADIO_SEMANTIC_SCAN_LOOKBACK_DAYS")
    @classmethod
    def validate_semantic_lookback(cls, value: int) -> int:
        if not 1 <= value <= 31:
            raise ValueError("RADIO_SEMANTIC_SCAN_LOOKBACK_DAYS must be between 1 and 31")
        return value

    @field_validator("RADIO_MENTION_WINDOW_DAYS")
    @classmethod
    def validate_mention_window(cls, value: int) -> int:
        if not 1 <= value <= 31:
            raise ValueError("RADIO_MENTION_WINDOW_DAYS must be between 1 and 31")
        return value

    @field_validator("RADIO_MENTION_AUDIO_PAD_SECONDS")
    @classmethod
    def validate_mention_audio_pad(cls, value: float) -> float:
        if not 0.0 <= value <= 900.0:
            raise ValueError("RADIO_MENTION_AUDIO_PAD_SECONDS must be between 0 and 900")
        return value

    @field_validator("RADIO_SEMANTIC_GROUPS_PER_CYCLE", "RADIO_SEMANTIC_KEYWORDS_PER_GROUP")
    @classmethod
    def validate_semantic_batch(cls, value: int) -> int:
        if not 1 <= value <= 100:
            raise ValueError("Semantic batch values must be between 1 and 100")
        return value

    @field_validator("RADIO_SEMANTIC_SETTLE_SECONDS")
    @classmethod
    def validate_semantic_settle(cls, value: int) -> int:
        if not 0 <= value <= 3600:
            raise ValueError("RADIO_SEMANTIC_SETTLE_SECONDS must be between 0 and 3600")
        return value

    @field_validator("RADIO_SEMANTIC_DEFAULT_THRESHOLD")
    @classmethod
    def validate_semantic_threshold(cls, value: float) -> float:
        if not 0.55 <= value <= 0.95:
            raise ValueError("RADIO_SEMANTIC_DEFAULT_THRESHOLD must be between 0.55 and 0.95")
        return value


    # --- v0.4 radio catalogue, monitoring, and admission control -------------
    RADIO_BROWSER_USER_AGENT: str = "FireMudRadioMonitor/0.4 (+EC2 pilot)"
    RADIO_BROWSER_MIRROR_REFRESH_SECONDS: int = 1800
    RADIO_BROWSER_COUNTRY_CACHE_SECONDS: int = 21600
    RADIO_BROWSER_SEARCH_CACHE_SECONDS: int = 600
    RADIO_BROWSER_STATION_CACHE_SECONDS: int = 300
    RADIO_BROWSER_REQUEST_TIMEOUT_SECONDS: float = 10.0
    RADIO_BROWSER_MAX_ATTEMPTS: int = 3
    RADIO_DATABASE_OVERRIDE_URL: str = "https://db.radio-browser.info/all.json"
    RADIO_DATABASE_OVERRIDE_REFRESH_SECONDS: int = 21600
    RADIO_MAX_ACTIVE_STATIONS: int = 8
    RADIO_MAX_STATIONS_PER_CAMPAIGN: int = 10
    RADIO_ALLOW_COUNTRY_ALL: bool = False
    RADIO_STATION_STOP_GRACE_SECONDS: int = 300
    RADIO_PREVIEW_TOKEN_TTL_SECONDS: int = 120
    RADIO_PREVIEW_MAX_SECONDS: int = 60
    RADIO_PREVIEW_MAX_CONCURRENT: int = 2
    RADIO_PROBE_SECONDS: int = 20
    RADIO_RECONCILER_POLL_SECONDS: int = 10
    RADIO_LEGACY_PINNED_STATION_IDS: CsvList = Field(default_factory=lambda: ["hertz879"])

    @field_validator("RADIO_MAX_ACTIVE_STATIONS")
    @classmethod
    def validate_max_active(cls, value: int) -> int:
        if not 1 <= value <= 64:
            raise ValueError("RADIO_MAX_ACTIVE_STATIONS must be between 1 and 64")
        return value

    @field_validator("RADIO_MAX_STATIONS_PER_CAMPAIGN")
    @classmethod
    def validate_max_per_campaign(cls, value: int) -> int:
        if not 1 <= value <= 100:
            raise ValueError("RADIO_MAX_STATIONS_PER_CAMPAIGN must be between 1 and 100")
        return value

    @field_validator("RADIO_PREVIEW_MAX_SECONDS")
    @classmethod
    def validate_preview_seconds(cls, value: int) -> int:
        if not 5 <= value <= 300:
            raise ValueError("RADIO_PREVIEW_MAX_SECONDS must be between 5 and 300")
        return value

    @field_validator("RADIO_PROBE_SECONDS")
    @classmethod
    def validate_probe_seconds(cls, value: int) -> int:
        if not 5 <= value <= 120:
            raise ValueError("RADIO_PROBE_SECONDS must be between 5 and 120")
        return value

    # --- v0.5 shared-station SQS pipeline ------------------------------------
    # Everything below is inert while RADIO_PIPELINE_MODE=legacy (the default).
    # See docs/architecture/adr/ADR-001-legacy-and-shared-pipeline-modes.md.

    RADIO_PIPELINE_MODE: PipelineMode = "legacy"
    RADIO_QUEUE_BACKEND: QueueBackend = "memory"
    RADIO_SEGMENT_STORE: SegmentStoreBackend = "local"

    RADIO_TRANSCRIPTION_QUEUE_URL: str = ""
    RADIO_ANALYSIS_QUEUE_URL: str = ""

    RADIO_SPOOL_PATH: Path = Path("/var/lib/radio/spool")
    RADIO_MODEL_PATH: Path = Path("/var/lib/radio/models")
    RADIO_EVIDENCE_PATH: Path = Path("/var/lib/radio/evidence")
    RADIO_LOG_PATH: Path = Path("/var/lib/radio/logs")

    RADIO_TEMP_SPEECH_PREFIX: str = "temp-speech/"
    RADIO_TEMP_TRANSCRIPTS_PREFIX: str = "temp-transcripts/"
    RADIO_MENTIONS_PREFIX: str = "mentions/"
    RADIO_EVIDENCE_PREFIX: str = "evidence/"
    RADIO_PIPELINE_CONFIG_PREFIX: str = "config/"

    # -- capacity -------------------------------------------------------------
    # Unique ACTIVE stations, not campaigns and not keywords. The default is a
    # conservative starting point for 4 vCPU / 8 GiB (see ADR-008); it is not a
    # benchmarked optimum and must be raised only with measurements.
    RADIO_MAX_ACTIVE_UNIQUE_STATIONS: int = 8
    RADIO_LISTENER_MAX_SESSIONS: int = 8
    RADIO_LISTENER_SHARD_COUNT: int = 1
    RADIO_LISTENER_SHARD_INDEX: int = 0
    RADIO_STATION_WINDDOWN_GRACE_SECONDS: int = 300

    # -- listener -------------------------------------------------------------
    RADIO_LISTENER_CONNECT_TIMEOUT_SECONDS: float = 15.0
    RADIO_LISTENER_READ_TIMEOUT_SECONDS: float = 30.0
    RADIO_LISTENER_RECONNECT_MIN_SECONDS: float = 2.0
    RADIO_LISTENER_RECONNECT_MAX_SECONDS: float = 120.0
    RADIO_LISTENER_FFMPEG_BINARY: str = "ffmpeg"
    RADIO_LISTENER_SHUTDOWN_GRACE_SECONDS: float = 10.0

    # -- ring buffer and segmentation -----------------------------------------
    RADIO_SAMPLE_RATE: int = 16_000
    RADIO_RING_BUFFER_SECONDS: int = 60
    RADIO_PRE_KEYWORD_SECONDS: int = 30
    RADIO_SPEECH_CHUNK_SECONDS: int = 20
    RADIO_CHUNK_OVERLAP_SECONDS: float = 1.0
    RADIO_SILENCE_END_SECONDS: int = 12
    RADIO_PURE_MUSIC_DISCARD_SECONDS: int = 8
    RADIO_MAX_CONVERSATION_SECONDS: int = 300
    RADIO_SEGMENT_OPUS_BITRATE: str = "24k"

    # -- audio classification -------------------------------------------------
    RADIO_AUDIO_CLASSIFIER: AudioClassifierBackend = "vad_energy"
    RADIO_TRANSCRIBE_UNCERTAIN_AUDIO: bool = True
    RADIO_CLASSIFIER_WINDOW_SECONDS: float = 3.0
    RADIO_CLASSIFIER_FRAME_MS: int = 32
    RADIO_VAD_SPEECH_THRESHOLD: float = 0.5
    RADIO_VAD_MODEL_FILENAME: str = "silero_vad.onnx"

    # -- ASR ------------------------------------------------------------------
    RADIO_ASR_MODEL: str = "Systran/faster-whisper-small"
    RADIO_ASR_CONFIRMATION_MODEL: str = "Systran/faster-whisper-small"
    RADIO_ASR_DEVICE: str = "cpu"
    RADIO_ASR_COMPUTE_TYPE: str = "int8"
    RADIO_ASR_CPU_THREADS: int = 2
    RADIO_ASR_BEAM_SIZE: int = 1
    RADIO_ASR_CONFIRMATION_BEAM_SIZE: int = 5
    RADIO_ASR_BACKEND: AsrBackend = "faster_whisper"
    RADIO_ASR_PROMPT_MAX_CHARACTERS: int = 400

    # -- content policy defaults ----------------------------------------------
    RADIO_INCLUDE_SONG_LYRICS: bool = False
    RADIO_INCLUDE_LONG_FORM_SINGING: bool = False
    RADIO_INCLUDE_SUNG_ADVERTISING_JINGLES: bool = True
    RADIO_INCLUDE_SPEECH_OVER_MUSIC: bool = True
    RADIO_CONTENT_CLASSIFIER: ContentClassifierBackend = "rules"
    RADIO_CONTENT_CLASSIFIER_LLM: bool = False
    RADIO_JINGLE_MAX_SECONDS: int = 30
    RADIO_JINGLE_ADJACENCY_SECONDS: int = 60

    # -- LLM (pipeline additions to the existing RADIO_LLM_* block) ------------
    RADIO_LLM_MODEL_PATH: Path = Path("/models/qwen/Qwen3-0.6B-Q8_0.gguf")
    RADIO_LLM_STRICT_SCHEMA: bool = True
    RADIO_LLM_REPAIR_RETRIES: int = 1
    RADIO_LLM_CIRCUIT_FAILURE_THRESHOLD: int = 5
    RADIO_LLM_CIRCUIT_RESET_SECONDS: int = 60

    # -- queue reliability ----------------------------------------------------
    RADIO_SQS_VISIBILITY_SECONDS: int = 300
    RADIO_SQS_VISIBILITY_HEARTBEAT_SECONDS: int = 60
    RADIO_SQS_MAX_PROCESSING_SECONDS: int = 1800
    RADIO_SQS_WAIT_TIME_SECONDS: int = 20
    RADIO_SQS_MAX_MESSAGES_PER_RECEIVE: int = 5

    RADIO_OUTBOX_ENABLED: bool = True
    RADIO_OUTBOX_POLL_SECONDS: float = 2.0
    RADIO_OUTBOX_BATCH_SIZE: int = 25
    RADIO_OUTBOX_MAX_ATTEMPTS: int = 10
    RADIO_OUTBOX_LEASE_SECONDS: int = 120
    RADIO_OUTBOX_RETENTION_DAYS: int = 7
    RADIO_INBOX_RETENTION_DAYS: int = 7

    RADIO_JOB_LEASE_SECONDS: int = 300
    RADIO_JOB_MAX_ATTEMPTS: int = 5
    RADIO_HEARTBEAT_INTERVAL_SECONDS: int = 30
    RADIO_HEARTBEAT_STALE_SECONDS: int = 120
    RADIO_SQLITE_BUSY_RETRIES: int = 5

    # -- spool retention and backpressure -------------------------------------
    RADIO_SPOOL_WARNING_PERCENT: int = 70
    RADIO_SPOOL_PAUSE_PERCENT: int = 85
    RADIO_SPOOL_EMERGENCY_PERCENT: int = 90
    RADIO_NO_HIT_RETENTION_MINUTES: int = 10
    RADIO_FAILED_SEGMENT_RETENTION_HOURS: int = 24
    RADIO_EVIDENCE_RETENTION_DAYS: int = 14
    RADIO_CLEANUP_POLL_SECONDS: int = 60

    # -- planner --------------------------------------------------------------
    RADIO_PLANNER_POLL_SECONDS: float = 5.0

    # -- observability --------------------------------------------------------
    RADIO_LOG_FORMAT: LogFormat = "text"
    RADIO_LOG_TRANSCRIPT_BODIES: bool = False

    # --- validators for the v0.5 block ---------------------------------------

    @field_validator(
        "RADIO_TEMP_SPEECH_PREFIX",
        "RADIO_TEMP_TRANSCRIPTS_PREFIX",
        "RADIO_MENTIONS_PREFIX",
        "RADIO_EVIDENCE_PREFIX",
        "RADIO_PIPELINE_CONFIG_PREFIX",
    )
    @classmethod
    def normalize_pipeline_prefix(cls, value: str) -> str:
        cleaned = value.strip().lstrip("/")
        if not cleaned:
            raise ValueError("S3 prefixes must not be empty")
        return cleaned if cleaned.endswith("/") else f"{cleaned}/"

    @field_validator("RADIO_MAX_ACTIVE_UNIQUE_STATIONS", "RADIO_LISTENER_MAX_SESSIONS")
    @classmethod
    def validate_station_capacity(cls, value: int) -> int:
        # Bounded on purpose: an unbounded station count is how a host gets
        # silently oversubscribed until audio starts being dropped.
        if not 1 <= value <= 512:
            raise ValueError("Station capacity limits must be between 1 and 512")
        return value

    @field_validator("RADIO_LISTENER_SHARD_COUNT")
    @classmethod
    def validate_shard_count(cls, value: int) -> int:
        if not 1 <= value <= 64:
            raise ValueError("RADIO_LISTENER_SHARD_COUNT must be between 1 and 64")
        return value

    @field_validator("RADIO_SAMPLE_RATE")
    @classmethod
    def validate_sample_rate(cls, value: int) -> int:
        # Silero VAD supports only 8 kHz and 16 kHz; Whisper resamples to 16 kHz.
        if value not in {8_000, 16_000}:
            raise ValueError("RADIO_SAMPLE_RATE must be 8000 or 16000")
        return value

    @field_validator(
        "RADIO_RING_BUFFER_SECONDS",
        "RADIO_PRE_KEYWORD_SECONDS",
        "RADIO_SPEECH_CHUNK_SECONDS",
        "RADIO_SILENCE_END_SECONDS",
        "RADIO_PURE_MUSIC_DISCARD_SECONDS",
        "RADIO_JINGLE_MAX_SECONDS",
        "RADIO_JINGLE_ADJACENCY_SECONDS",
    )
    @classmethod
    def validate_positive_duration(cls, value: int) -> int:
        if not 1 <= value <= 3600:
            raise ValueError("Durations must be between 1 and 3600 seconds")
        return value

    @field_validator("RADIO_CHUNK_OVERLAP_SECONDS")
    @classmethod
    def validate_overlap(cls, value: float) -> float:
        if not 0.0 <= value <= 10.0:
            raise ValueError("RADIO_CHUNK_OVERLAP_SECONDS must be between 0 and 10")
        return value

    @field_validator("RADIO_MAX_CONVERSATION_SECONDS")
    @classmethod
    def validate_pipeline_conversation_seconds(cls, value: int) -> int:
        if not 30 <= value <= 3600:
            raise ValueError("RADIO_MAX_CONVERSATION_SECONDS must be between 30 and 3600")
        return value

    @field_validator("RADIO_VAD_SPEECH_THRESHOLD")
    @classmethod
    def validate_vad_threshold(cls, value: float) -> float:
        if not 0.05 <= value <= 0.95:
            raise ValueError("RADIO_VAD_SPEECH_THRESHOLD must be between 0.05 and 0.95")
        return value

    @field_validator("RADIO_CLASSIFIER_FRAME_MS")
    @classmethod
    def validate_frame_ms(cls, value: int) -> int:
        if not 10 <= value <= 100:
            raise ValueError("RADIO_CLASSIFIER_FRAME_MS must be between 10 and 100")
        return value

    @field_validator("RADIO_CLASSIFIER_WINDOW_SECONDS")
    @classmethod
    def validate_classifier_window(cls, value: float) -> float:
        if not 0.5 <= value <= 30.0:
            raise ValueError("RADIO_CLASSIFIER_WINDOW_SECONDS must be between 0.5 and 30")
        return value

    @field_validator("RADIO_ASR_COMPUTE_TYPE")
    @classmethod
    def validate_compute_type(cls, value: str) -> str:
        allowed = {"int8", "int8_float16", "int8_float32", "float16", "float32", "auto"}
        cleaned = value.strip().lower()
        if cleaned not in allowed:
            raise ValueError(f"RADIO_ASR_COMPUTE_TYPE must be one of {sorted(allowed)}")
        return cleaned

    @field_validator("RADIO_ASR_DEVICE")
    @classmethod
    def validate_asr_device(cls, value: str) -> str:
        cleaned = value.strip().lower()
        if cleaned not in {"cpu", "cuda", "auto"}:
            raise ValueError("RADIO_ASR_DEVICE must be cpu, cuda or auto")
        return cleaned

    @field_validator("RADIO_ASR_CPU_THREADS")
    @classmethod
    def validate_asr_threads(cls, value: int) -> int:
        if not 1 <= value <= 64:
            raise ValueError("RADIO_ASR_CPU_THREADS must be between 1 and 64")
        return value

    @field_validator("RADIO_ASR_BEAM_SIZE", "RADIO_ASR_CONFIRMATION_BEAM_SIZE")
    @classmethod
    def validate_beam_size(cls, value: int) -> int:
        if not 1 <= value <= 10:
            raise ValueError("Beam sizes must be between 1 and 10")
        return value

    @field_validator("RADIO_ASR_PROMPT_MAX_CHARACTERS")
    @classmethod
    def validate_prompt_budget(cls, value: int) -> int:
        if not 0 <= value <= 2000:
            raise ValueError("RADIO_ASR_PROMPT_MAX_CHARACTERS must be between 0 and 2000")
        return value

    @field_validator("RADIO_SQS_VISIBILITY_SECONDS")
    @classmethod
    def validate_visibility(cls, value: int) -> int:
        # Zero visibility means a message is immediately redelivered while it is
        # still being processed; 43200 is the documented SQS maximum (12 hours).
        if not 30 <= value <= 43_200:
            raise ValueError("RADIO_SQS_VISIBILITY_SECONDS must be between 30 and 43200")
        return value

    @field_validator("RADIO_SQS_VISIBILITY_HEARTBEAT_SECONDS")
    @classmethod
    def validate_visibility_heartbeat(cls, value: int) -> int:
        if not 5 <= value <= 3600:
            raise ValueError("RADIO_SQS_VISIBILITY_HEARTBEAT_SECONDS must be between 5 and 3600")
        return value

    @field_validator("RADIO_SQS_MAX_PROCESSING_SECONDS")
    @classmethod
    def validate_max_processing(cls, value: int) -> int:
        if not 60 <= value <= 43_200:
            raise ValueError("RADIO_SQS_MAX_PROCESSING_SECONDS must be between 60 and 43200")
        return value

    @field_validator("RADIO_SQS_WAIT_TIME_SECONDS")
    @classmethod
    def validate_wait_time(cls, value: int) -> int:
        # 20 seconds is the documented SQS long-polling maximum.
        if not 0 <= value <= 20:
            raise ValueError("RADIO_SQS_WAIT_TIME_SECONDS must be between 0 and 20")
        return value

    @field_validator("RADIO_SQS_MAX_MESSAGES_PER_RECEIVE")
    @classmethod
    def validate_receive_batch(cls, value: int) -> int:
        # SQS returns at most 10 messages per ReceiveMessage call.
        if not 1 <= value <= 10:
            raise ValueError("RADIO_SQS_MAX_MESSAGES_PER_RECEIVE must be between 1 and 10")
        return value

    @field_validator("RADIO_OUTBOX_BATCH_SIZE")
    @classmethod
    def validate_outbox_batch(cls, value: int) -> int:
        if not 1 <= value <= 500:
            raise ValueError("RADIO_OUTBOX_BATCH_SIZE must be between 1 and 500")
        return value

    @field_validator("RADIO_OUTBOX_MAX_ATTEMPTS", "RADIO_JOB_MAX_ATTEMPTS")
    @classmethod
    def validate_attempt_limit(cls, value: int) -> int:
        if not 1 <= value <= 100:
            raise ValueError("Attempt limits must be between 1 and 100")
        return value

    @field_validator("RADIO_OUTBOX_LEASE_SECONDS", "RADIO_JOB_LEASE_SECONDS")
    @classmethod
    def validate_lease(cls, value: int) -> int:
        if not 30 <= value <= 43_200:
            raise ValueError("Lease durations must be between 30 and 43200 seconds")
        return value

    @field_validator("RADIO_OUTBOX_POLL_SECONDS", "RADIO_PLANNER_POLL_SECONDS")
    @classmethod
    def validate_fast_poll(cls, value: float) -> float:
        if not 0.5 <= value <= 300.0:
            raise ValueError("Poll intervals must be between 0.5 and 300 seconds")
        return value

    @field_validator("RADIO_OUTBOX_RETENTION_DAYS", "RADIO_INBOX_RETENTION_DAYS")
    @classmethod
    def validate_message_retention(cls, value: int) -> int:
        if not 1 <= value <= 90:
            raise ValueError("Message-table retention must be between 1 and 90 days")
        return value

    @field_validator("RADIO_HEARTBEAT_INTERVAL_SECONDS", "RADIO_CLEANUP_POLL_SECONDS")
    @classmethod
    def validate_worker_interval(cls, value: int) -> int:
        if not 5 <= value <= 3600:
            raise ValueError("Worker intervals must be between 5 and 3600 seconds")
        return value

    @field_validator("RADIO_HEARTBEAT_STALE_SECONDS")
    @classmethod
    def validate_heartbeat_stale(cls, value: int) -> int:
        if not 10 <= value <= 86_400:
            raise ValueError("RADIO_HEARTBEAT_STALE_SECONDS must be between 10 and 86400")
        return value

    @field_validator("RADIO_SQLITE_BUSY_RETRIES")
    @classmethod
    def validate_busy_retries(cls, value: int) -> int:
        if not 0 <= value <= 20:
            raise ValueError("RADIO_SQLITE_BUSY_RETRIES must be between 0 and 20")
        return value

    @field_validator("RADIO_NO_HIT_RETENTION_MINUTES")
    @classmethod
    def validate_no_hit_retention(cls, value: int) -> int:
        if not 1 <= value <= 10_080:
            raise ValueError("RADIO_NO_HIT_RETENTION_MINUTES must be between 1 and 10080")
        return value

    @field_validator("RADIO_FAILED_SEGMENT_RETENTION_HOURS")
    @classmethod
    def validate_failed_retention(cls, value: int) -> int:
        if not 1 <= value <= 720:
            raise ValueError("RADIO_FAILED_SEGMENT_RETENTION_HOURS must be between 1 and 720")
        return value

    @field_validator("RADIO_EVIDENCE_RETENTION_DAYS")
    @classmethod
    def validate_evidence_retention(cls, value: int) -> int:
        if not 1 <= value <= 3650:
            raise ValueError("RADIO_EVIDENCE_RETENTION_DAYS must be between 1 and 3650")
        return value

    @field_validator("RADIO_LLM_REPAIR_RETRIES")
    @classmethod
    def validate_repair_retries(cls, value: int) -> int:
        if not 0 <= value <= 2:
            raise ValueError("RADIO_LLM_REPAIR_RETRIES must be between 0 and 2")
        return value

    @field_validator("RADIO_LLM_CIRCUIT_FAILURE_THRESHOLD")
    @classmethod
    def validate_circuit_threshold(cls, value: int) -> int:
        if not 1 <= value <= 100:
            raise ValueError("RADIO_LLM_CIRCUIT_FAILURE_THRESHOLD must be between 1 and 100")
        return value

    @field_validator("RADIO_LLM_CIRCUIT_RESET_SECONDS")
    @classmethod
    def validate_circuit_reset(cls, value: int) -> int:
        if not 5 <= value <= 3600:
            raise ValueError("RADIO_LLM_CIRCUIT_RESET_SECONDS must be between 5 and 3600")
        return value

    @field_validator("RADIO_STATION_WINDDOWN_GRACE_SECONDS")
    @classmethod
    def validate_winddown_grace(cls, value: int) -> int:
        if not 0 <= value <= 86_400:
            raise ValueError("RADIO_STATION_WINDDOWN_GRACE_SECONDS must be between 0 and 86400")
        return value

    @field_validator(
        "RADIO_LISTENER_CONNECT_TIMEOUT_SECONDS",
        "RADIO_LISTENER_READ_TIMEOUT_SECONDS",
        "RADIO_LISTENER_SHUTDOWN_GRACE_SECONDS",
    )
    @classmethod
    def validate_listener_timeout(cls, value: float) -> float:
        if not 1.0 <= value <= 600.0:
            raise ValueError("Listener timeouts must be between 1 and 600 seconds")
        return value

    @field_validator("RADIO_LISTENER_RECONNECT_MIN_SECONDS", "RADIO_LISTENER_RECONNECT_MAX_SECONDS")
    @classmethod
    def validate_reconnect_delay(cls, value: float) -> float:
        if not 0.1 <= value <= 3600.0:
            raise ValueError("Reconnect delays must be between 0.1 and 3600 seconds")
        return value

    @field_validator("RADIO_SEGMENT_OPUS_BITRATE")
    @classmethod
    def validate_opus_bitrate(cls, value: str) -> str:
        cleaned = value.strip().lower()
        if not re.fullmatch(r"\d{1,4}k?", cleaned):
            raise ValueError("RADIO_SEGMENT_OPUS_BITRATE must look like '24k' or '24000'")
        return cleaned

    @field_validator("RADIO_LISTENER_FFMPEG_BINARY")
    @classmethod
    def validate_ffmpeg_binary(cls, value: str) -> str:
        cleaned = value.strip()
        # This value becomes argv[0] of a subprocess. Shell metacharacters and
        # whitespace are refused so a misconfigured env var can never become an
        # argument-injection vector, even though shell=True is never used.
        if not cleaned or not re.fullmatch(r"[A-Za-z0-9_./-]+", cleaned):
            raise ValueError("RADIO_LISTENER_FFMPEG_BINARY must be a plain binary name or path")
        return cleaned

    @model_validator(mode="after")
    def validate_pipeline_coherence(self) -> Settings:
        if self.RADIO_LISTENER_SHARD_INDEX < 0:
            raise ValueError("RADIO_LISTENER_SHARD_INDEX must not be negative")
        if self.RADIO_LISTENER_SHARD_INDEX >= self.RADIO_LISTENER_SHARD_COUNT:
            raise ValueError(
                "RADIO_LISTENER_SHARD_INDEX must be less than RADIO_LISTENER_SHARD_COUNT"
            )
        if self.RADIO_PRE_KEYWORD_SECONDS > self.RADIO_RING_BUFFER_SECONDS:
            raise ValueError(
                "RADIO_PRE_KEYWORD_SECONDS cannot exceed RADIO_RING_BUFFER_SECONDS: the "
                "pre-roll must fit inside the buffer it is read from"
            )
        if self.RADIO_SPEECH_CHUNK_SECONDS > self.RADIO_RING_BUFFER_SECONDS:
            raise ValueError(
                "RADIO_SPEECH_CHUNK_SECONDS cannot exceed RADIO_RING_BUFFER_SECONDS"
            )
        if self.RADIO_CHUNK_OVERLAP_SECONDS >= self.RADIO_SPEECH_CHUNK_SECONDS:
            raise ValueError(
                "RADIO_CHUNK_OVERLAP_SECONDS must be smaller than RADIO_SPEECH_CHUNK_SECONDS"
            )
        if self.RADIO_LISTENER_RECONNECT_MIN_SECONDS > self.RADIO_LISTENER_RECONNECT_MAX_SECONDS:
            raise ValueError(
                "RADIO_LISTENER_RECONNECT_MIN_SECONDS must not exceed "
                "RADIO_LISTENER_RECONNECT_MAX_SECONDS"
            )
        if not (
            self.RADIO_SPOOL_WARNING_PERCENT
            < self.RADIO_SPOOL_PAUSE_PERCENT
            < self.RADIO_SPOOL_EMERGENCY_PERCENT
            <= 99
        ):
            raise ValueError(
                "Spool watermarks must satisfy warning < pause < emergency <= 99"
            )
        if self.RADIO_SPOOL_WARNING_PERCENT < 1:
            raise ValueError("RADIO_SPOOL_WARNING_PERCENT must be at least 1")
        if self.RADIO_SQS_VISIBILITY_HEARTBEAT_SECONDS >= self.RADIO_SQS_VISIBILITY_SECONDS:
            raise ValueError(
                "RADIO_SQS_VISIBILITY_HEARTBEAT_SECONDS must be shorter than "
                "RADIO_SQS_VISIBILITY_SECONDS or visibility will expire before it is extended"
            )
        if self.RADIO_LISTENER_MAX_SESSIONS > self.RADIO_MAX_ACTIVE_UNIQUE_STATIONS:
            raise ValueError(
                "RADIO_LISTENER_MAX_SESSIONS cannot exceed RADIO_MAX_ACTIVE_UNIQUE_STATIONS"
            )
        if self.RADIO_PIPELINE_MODE == "shared_sqs":
            self._validate_shared_sqs_requirements()
        return self

    def _validate_shared_sqs_requirements(self) -> None:
        """Fail fast rather than start a half-configured pipeline.

        A shared_sqs deployment that starts without queues silently produces
        segments nobody consumes, which looks healthy and loses every mention.
        """
        if self.RADIO_QUEUE_BACKEND == "sqs":
            for name in ("RADIO_TRANSCRIPTION_QUEUE_URL", "RADIO_ANALYSIS_QUEUE_URL"):
                url = str(getattr(self, name) or "").strip()
                if not url:
                    raise ValueError(
                        f"{name} is required when RADIO_PIPELINE_MODE=shared_sqs "
                        f"and RADIO_QUEUE_BACKEND=sqs"
                    )
                parsed = urlsplit(url)
                if parsed.scheme != "https" or not parsed.netloc:
                    raise ValueError(f"{name} must be an https SQS queue URL")
                if not parsed.path.rstrip("/").endswith(".fifo"):
                    raise ValueError(
                        f"{name} must reference a FIFO queue (path ending in .fifo); "
                        f"ordering per station is a correctness requirement"
                    )
        if self.RADIO_SEGMENT_STORE == "s3" and not self.RADIO_S3_BUCKET.strip():
            raise ValueError("RADIO_S3_BUCKET is required when RADIO_SEGMENT_STORE=s3")

    # --- derived helpers -----------------------------------------------------

    @property
    def effective_aws_region(self) -> str:
        return (self.AWS_DEFAULT_REGION or self.AWS_REGION).strip()

    @property
    def shared_pipeline_enabled(self) -> bool:
        return self.RADIO_PIPELINE_MODE == "shared_sqs"

    @property
    def ring_buffer_bytes_per_station(self) -> int:
        """16-bit mono PCM. Kept as a property so capacity docs cannot drift."""
        return self.RADIO_RING_BUFFER_SECONDS * self.RADIO_SAMPLE_RATE * 2

    @property
    def content_policy_defaults(self) -> dict[str, bool]:
        return {
            "include_news": True,
            "include_interviews": True,
            "include_advertisements": True,
            "include_announcements": True,
            "include_emergency_alerts": True,
            "include_dj_commentary": True,
            "include_speech_over_music": self.RADIO_INCLUDE_SPEECH_OVER_MUSIC,
            "include_song_lyrics": self.RADIO_INCLUDE_SONG_LYRICS,
            "include_long_form_singing": self.RADIO_INCLUDE_LONG_FORM_SINGING,
            "include_sung_advertising_jingles": self.RADIO_INCLUDE_SUNG_ADVERTISING_JINGLES,
        }


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
