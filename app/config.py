from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Annotated

from pydantic import BeforeValidator, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


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
    RADIO_MAX_ACTIVE_STATIONS: int = 2
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

    @property
    def effective_aws_region(self) -> str:
        return (self.AWS_DEFAULT_REGION or self.AWS_REGION).strip()


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
