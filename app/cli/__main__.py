"""Keep ``python -m app.cli`` working now that ``cli`` is a package."""
from __future__ import annotations

from . import main

if __name__ == "__main__":
    main()
