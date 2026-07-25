from __future__ import annotations

import uvicorn

from .config import get_settings


def main() -> None:
    settings = get_settings()
    uvicorn.run(
        "app.main:app",
        host=settings.RADIO_API_HOST,
        port=settings.RADIO_API_PORT,
        log_level=settings.LOG_LEVEL.lower(),
        proxy_headers=False,
    )


if __name__ == "__main__":
    main()
