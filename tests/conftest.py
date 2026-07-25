from __future__ import annotations

import io
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from app.config import Settings
from app.db import Database


class FakeBody(io.BytesIO):
    def close(self) -> None:
        super().close()


class FakePaginator:
    def __init__(self, client: "FakeS3") -> None:
        self.client = client

    def paginate(self, *, Bucket: str, Prefix: str):
        rows = []
        for key, value in self.client.objects.items():
            if key.startswith(Prefix):
                rows.append(
                    {
                        "Key": key,
                        "LastModified": value.get("LastModified", datetime.now(timezone.utc)),
                        "ETag": value.get("ETag", '"etag"'),
                    }
                )
        yield {"Contents": rows}


class FakeS3:
    class exceptions:
        class NoSuchKey(Exception):
            pass

    def __init__(self) -> None:
        self.objects: dict[str, dict[str, Any]] = {}

    def put_json(self, key: str, value: dict[str, Any], *, etag: str = "etag") -> None:
        self.objects[key] = {
            "Body": json.dumps(value).encode(),
            "ContentType": "application/json",
            "ETag": f'"{etag}"',
            "LastModified": datetime.now(timezone.utc),
        }

    def put_bytes(self, key: str, value: bytes, *, content_type: str = "audio/wav") -> None:
        self.objects[key] = {
            "Body": value,
            "ContentType": content_type,
            "ETag": '"audio"',
            "LastModified": datetime.now(timezone.utc),
        }

    def get_paginator(self, name: str) -> FakePaginator:
        assert name == "list_objects_v2"
        return FakePaginator(self)

    def list_objects_v2(self, *, Bucket: str, Prefix: str, Delimiter: str | None = None, MaxKeys: int = 1000):
        del Bucket
        if Delimiter == "/":
            prefixes = set()
            for key in self.objects:
                if not key.startswith(Prefix):
                    continue
                remainder = key[len(Prefix):]
                head = remainder.split("/", 1)[0]
                if head:
                    prefixes.add(f"{Prefix}{head}/")
            return {"CommonPrefixes": [{"Prefix": value} for value in sorted(prefixes)]}
        rows = [{"Key": key} for key in sorted(self.objects) if key.startswith(Prefix)]
        return {"Contents": rows[:MaxKeys]}

    def get_object(self, *, Bucket: str, Key: str, Range: str | None = None):
        del Bucket
        if Key not in self.objects:
            raise self.exceptions.NoSuchKey(Key)
        item = self.objects[Key]
        raw = bytes(item["Body"])
        if Range:
            first, last = Range.removeprefix("bytes=").split("-", 1)
            raw = raw[int(first): int(last) + 1]
        return {"Body": FakeBody(raw), "ContentType": item.get("ContentType")}

    def head_object(self, *, Bucket: str, Key: str):
        del Bucket
        item = self.objects[Key]
        return {"ContentLength": len(item["Body"]), "ContentType": item.get("ContentType")}

    def put_object(self, *, Bucket: str, Key: str, Body: bytes, ContentType: str, **kwargs):
        del Bucket, kwargs
        self.objects[Key] = {
            "Body": bytes(Body),
            "ContentType": ContentType,
            "ETag": '"put"',
            "LastModified": datetime.now(timezone.utc),
        }
        return {"ETag": '"put"'}


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    station_dir = tmp_path / "stations"
    station_dir.mkdir()
    metadata = tmp_path / "stations.json"
    metadata.write_text(
        json.dumps(
            {
                "stations": [
                    {
                        "id": "hertz879",
                        "name": "Hertz 87.9",
                        "country_code": "DE",
                        "language_codes": ["de", "en"],
                    }
                ]
            }
        )
    )
    return Settings(
        RADIO_S3_BUCKET="bucket",
        RADIO_DATABASE_PATH=tmp_path / "radio.db",
        RADIO_AUDIO_TOKEN_SECRET="x" * 48,
        RADIO_STATION_CONFIG_DIR=station_dir,
        RADIO_STATION_METADATA_PATH=metadata,
        RADIO_SYNC_ENABLED=False,
    )


@pytest.fixture
def database(settings: Settings) -> Database:
    db = Database(settings.RADIO_DATABASE_PATH)
    db.connect()
    try:
        yield db
    finally:
        db.close()


@pytest.fixture
def fake_s3() -> FakeS3:
    return FakeS3()
