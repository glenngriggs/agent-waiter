"""Start the platform: `python -m den`.

One uvicorn worker only: running turns, the event bus and the scheduler live
in this process's memory, so a second worker would split them in two.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

import uvicorn

from .app import create_app
from .config import load_dotenv, load_settings

LOCAL_BANNER = (
    "LOCAL RUNTIME: agents run on this machine without containers. "
    "Development and tests only."
)


def main() -> None:
    # .env in the working directory fills in variables that are not already
    # set (it is git-ignored, so keys can live there).
    load_dotenv(Path.cwd() / ".env")
    settings = load_settings()
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    if settings.runtime == "local":
        rule = "!" * len(LOCAL_BANNER)
        print(f"{rule}\n{LOCAL_BANNER}\n{rule}", file=sys.stderr, flush=True)
    uvicorn.run(
        create_app(settings),
        host=settings.host,
        port=settings.port,
        workers=1,
        log_level="info",
        # Open SSE streams never end on their own; without this, Ctrl+C would
        # wait for every browser tab to close before shutting down.
        timeout_graceful_shutdown=5,
    )


if __name__ == "__main__":
    main()
