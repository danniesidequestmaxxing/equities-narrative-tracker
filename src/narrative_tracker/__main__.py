"""Process dispatcher so one image serves two Railway services:

    NT_PROCESS=web   -> the dashboard (uvicorn)
    (unset / worker) -> the ingestion worker
"""

from __future__ import annotations

import os


def _run() -> None:
    if os.environ.get("NT_PROCESS", "worker").lower() == "web":
        import uvicorn

        uvicorn.run(
            "narrative_tracker.api.dashboard:app",
            host="0.0.0.0",
            port=int(os.environ.get("PORT", "8000")),
        )
    else:
        import asyncio

        from .worker import main

        asyncio.run(main())


if __name__ == "__main__":
    _run()
