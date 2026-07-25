from __future__ import annotations

import argparse
import json

import boto3

from .config import get_settings
from .db import Database
from .services.keywords import KeywordConfigService


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["init-db", "publish-keywords"])
    args = parser.parse_args()
    settings = get_settings()
    database = Database(settings.RADIO_DATABASE_PATH)
    database.connect()
    try:
        if args.command == "init-db":
            print(json.dumps({"status": "PASS", "database": str(settings.RADIO_DATABASE_PATH)}))
            return
        s3 = boto3.client("s3", region_name=settings.effective_aws_region)
        result = KeywordConfigService(settings, database, s3).publish()
        print(json.dumps({"status": "PASS", **result}, indent=2))
    finally:
        database.close()


if __name__ == "__main__":
    main()
