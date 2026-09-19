"""Entry point cho `python -m app.cli` (Requirement 15.1).

KHÔNG import `app.main` — CLI không mở HTTP socket, không load FastAPI
(Requirement 15.9). Chỉ delegate về `app.cli.main.main(argv)` — hàm async
orchestrator sẽ được implement ở task 42.1.
"""

from __future__ import annotations

import asyncio
import sys

from app.cli.main import main

if __name__ == "__main__":
    sys.exit(asyncio.run(main(sys.argv[1:])))
