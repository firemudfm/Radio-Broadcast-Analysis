"""Operator command line.

``python -m app.cli init-db``
``python -m app.cli publish-keywords``
``python -m app.cli.migrate_database``

Kept import-light on purpose. This package is now the parent of
:mod:`app.cli.migrate_database`, which runs during deployment before anything
else is up, so a module-scope ``import boto3`` here would make schema migration
depend on the AWS SDK it has no use for. The heavy imports live inside the
branch that needs them.
"""
from __future__ import annotations

import argparse
import json

from ..config import get_settings
from ..db import Database


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m app.cli")
    parser.add_argument("command", choices=["init-db", "publish-keywords"])
    args = parser.parse_args()
    settings = get_settings()
    database = Database(settings.RADIO_DATABASE_PATH)
    database.connect()
    try:
        if args.command == "init-db":
            print(json.dumps({"status": "PASS", "database": str(settings.RADIO_DATABASE_PATH)}))
            return
        # Imported here, not at module scope: only this branch talks to S3.
        import boto3  # noqa: PLC0415 - keeps the package importable without the SDK

        from ..services.keywords import KeywordConfigService  # noqa: PLC0415

        s3 = boto3.client("s3", region_name=settings.effective_aws_region)
        result = KeywordConfigService(settings, database, s3).publish()
        print(json.dumps({"status": "PASS", **result}, indent=2))
    finally:
        database.close()


if __name__ == "__main__":
    main()
