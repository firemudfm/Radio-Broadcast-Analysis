from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from ..config import Settings

MANAGED_BY = "firemud-radio-api-open"


class KeywordConfigService:
    def __init__(self, settings: Settings, database: Any, s3_client: Any) -> None:
        self._settings = settings
        self._database = database
        self._s3 = s3_client

    def publish(self) -> dict[str, Any]:
        document = self._load_existing()
        existing = document.get("entities")
        preserved = [
            item
            for item in existing if isinstance(item, dict) and item.get("managed_by") != MANAGED_BY
        ]
        managed = self._managed_entities(self._database.active_bindings())
        now = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        output = {
            **document,
            "schema_version": "1.0",
            "config_version": f"api-open-{now}",
            "description": "Manual entities plus active no-auth pilot campaigns.",
            "entities": [*preserved, *managed],
        }
        body = json.dumps(output, indent=2, sort_keys=True, ensure_ascii=False).encode("utf-8")
        self._s3.put_object(
            Bucket=self._settings.RADIO_S3_BUCKET,
            Key=self._settings.RADIO_KEYWORDS_KEY,
            Body=body,
            ContentType="application/json; charset=utf-8",
            ServerSideEncryption="AES256",
        )
        return {
            "key": self._settings.RADIO_KEYWORDS_KEY,
            "manual_entities": len(preserved),
            "managed_entities": len(managed),
        }

    def _load_existing(self) -> dict[str, Any]:
        try:
            response = self._s3.get_object(
                Bucket=self._settings.RADIO_S3_BUCKET,
                Key=self._settings.RADIO_KEYWORDS_KEY,
            )
        except self._s3.exceptions.NoSuchKey:
            return {"schema_version": "1.0", "entities": []}
        except Exception as error:
            code = getattr(error, "response", {}).get("Error", {}).get("Code")
            if code in {"NoSuchKey", "404"}:
                return {"schema_version": "1.0", "entities": []}
            raise
        loaded = json.loads(response["Body"].read())
        if not isinstance(loaded, dict):
            raise ValueError("Keyword configuration must be a JSON object")
        entities = loaded.get("entities")
        if entities is None:
            loaded["entities"] = []
        elif not isinstance(entities, list):
            raise ValueError("Keyword configuration entities must be a list")
        return loaded

    @staticmethod
    def _managed_entities(bindings: list[dict[str, Any]]) -> list[dict[str, Any]]:
        grouped: dict[str, dict[str, Any]] = {}
        for binding in bindings:
            entity_id = str(binding["entity_id"])
            target = grouped.setdefault(
                entity_id,
                {
                    "id": entity_id,
                    "display_name": binding["display_name"],
                    "entity_type": binding.get("keyword_type") or "brand",
                    "enabled": True,
                    "match_mode": binding["match_mode"],
                    "aliases": {"*": []},
                    "managed_by": MANAGED_BY,
                    "campaign_ids": [],
                    "station_ids": [],
                },
            )
            aliases = [binding["display_name"], *binding.get("aliases", [])]
            for alias in aliases:
                cleaned = str(alias).strip()
                if cleaned and cleaned.casefold() not in {
                    str(item).casefold() for item in target["aliases"]["*"]
                }:
                    target["aliases"]["*"].append(cleaned)
            if binding["campaign_id"] not in target["campaign_ids"]:
                target["campaign_ids"].append(binding["campaign_id"])
            for station_id in binding["station_ids"]:
                if station_id not in target["station_ids"]:
                    target["station_ids"].append(station_id)
        return sorted(grouped.values(), key=lambda item: str(item["display_name"]).casefold())
