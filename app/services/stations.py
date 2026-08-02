from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path
from typing import Any

from botocore.exceptions import ClientError

from ..config import Settings

logger = logging.getLogger(__name__)

# S3 error codes that mean "you are not allowed to list this prefix", as opposed
# to "S3 is unreachable / misconfigured". raw-audio/ is a legacy prefix that the
# production instance role deliberately does not grant s3:ListBucket on, so a
# denied listing there is expected and must not take a request-time route down.
_LIST_DENIED_CODES = {"AccessDenied", "AllAccessDisabled", "AccessDeniedException"}

_ENV_PATTERN = re.compile(r"^([A-Z0-9_]+)=(.*)$")


def _unquote(value: str) -> str:
    text = value.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in {'"', "'"}:
        return text[1:-1]
    return text


def parse_station_env(path: Path) -> dict[str, str]:
    output: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        match = _ENV_PATTERN.match(line)
        if match:
            output[match.group(1)] = _unquote(match.group(2))
    return output


class StationService:
    def __init__(self, settings: Settings, s3_client: Any) -> None:
        self._settings = settings
        self._s3 = s3_client
        self._cache: list[dict[str, Any]] = []
        self._cache_time = 0.0

    def list_stations(self, *, force: bool = False) -> list[dict[str, Any]]:
        now = time.monotonic()
        if not force and self._cache and now - self._cache_time < self._settings.RADIO_STATION_REFRESH_SECONDS:
            return [dict(item) for item in self._cache]
        metadata = self._metadata()
        stations: dict[str, dict[str, Any]] = {}
        config_dir = self._settings.RADIO_STATION_CONFIG_DIR
        if config_dir.exists():
            for path in sorted(config_dir.glob("*.env")):
                try:
                    values = parse_station_env(path)
                except OSError:
                    continue
                station_id = values.get("STATION_ID", path.stem).strip()
                if not station_id:
                    continue
                item = metadata.get(station_id, {})
                language = values.get("STATION_LANGUAGE", "").strip()
                stations[station_id] = {
                    "id": station_id,
                    "name": values.get("STATION_NAME") or item.get("name") or station_id,
                    "country_code": item.get("country_code"),
                    "language_codes": item.get("language_codes") or ([language] if language else []),
                    "connected": True,
                    "enabled": True,
                }
        for station_id in self._s3_station_ids():
            if station_id in stations:
                continue
            item = metadata.get(station_id, {})
            stations[station_id] = {
                "id": station_id,
                "name": item.get("name") or station_id,
                "country_code": item.get("country_code"),
                "language_codes": item.get("language_codes") or [],
                "connected": True,
                "enabled": True,
            }
        output = sorted(stations.values(), key=lambda item: str(item["name"]).casefold())
        self._cache = output
        self._cache_time = now
        return [dict(item) for item in output]

    def station_map(self) -> dict[str, dict[str, Any]]:
        return {item["id"]: item for item in self.list_stations()}

    def _metadata(self) -> dict[str, dict[str, Any]]:
        path = self._settings.RADIO_STATION_METADATA_PATH
        if not path.exists():
            return {}
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        rows = loaded.get("stations") if isinstance(loaded, dict) else loaded
        if not isinstance(rows, list):
            return {}
        output: dict[str, dict[str, Any]] = {}
        for row in rows:
            if not isinstance(row, dict):
                continue
            station_id = str(row.get("id") or "").strip()
            if not station_id:
                continue
            output[station_id] = {
                "name": str(row.get("name") or station_id),
                "country_code": str(row.get("country_code") or "").strip() or None,
                "language_codes": [
                    str(value).strip()
                    for value in row.get("language_codes", [])
                    if str(value).strip()
                ],
            }
        return output

    def _s3_station_ids(self) -> list[str]:
        try:
            response = self._s3.list_objects_v2(
                Bucket=self._settings.RADIO_S3_BUCKET,
                Prefix=self._settings.RADIO_RAW_PREFIX,
                Delimiter="/",
                MaxKeys=1000,
            )
        except ClientError as error:
            # raw-audio/ is a legacy prefix: the current pipeline stores segments
            # locally and publishes results under mentions/, so nothing writes
            # raw-audio/ anymore and the production role scopes s3:ListBucket to
            # the active prefixes only. Treat a denied listing as "no
            # S3-discovered stations" and fall back to the locally configured
            # ones -- _hydrate already tolerates a station missing from the map.
            # A real outage (absent credentials, unreachable endpoint) raises
            # BotoCoreError rather than ClientError and still propagates, so
            # routes that genuinely depend on S3 keep failing loudly.
            code = error.response.get("Error", {}).get("Code", "")
            if code in _LIST_DENIED_CODES:
                logger.warning(
                    "S3 station discovery skipped (%s on prefix %r); "
                    "using local station config only",
                    code,
                    self._settings.RADIO_RAW_PREFIX,
                )
                return []
            raise
        output: list[str] = []
        for item in response.get("CommonPrefixes", []):
            prefix = str(item.get("Prefix") or "")
            relative = prefix.removeprefix(self._settings.RADIO_RAW_PREFIX).strip("/")
            if relative:
                output.append(relative)
        return output
