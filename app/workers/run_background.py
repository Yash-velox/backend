"""Background processing + publish workers, separate from the HTTP API process.

Run on UAT/prod as `aone-backend-worker` so uvicorn API workers stay free for
health and Admin traffic.

    python -m app.workers.run_background
"""

from __future__ import annotations

import asyncio
import logging
import signal

from app.logging_setup import setup_logging
from app.workers.processing_worker import processing_worker
from app.workers.publish_worker import publish_worker

logger = logging.getLogger("app.workers.run_background")


async def _main() -> None:
    setup_logging()
    await processing_worker.start()
    await publish_worker.start()
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            pass
    logger.info("Background workers running | waiting for SIGTERM")
    await stop.wait()
    await publish_worker.stop()
    await processing_worker.stop()


if __name__ == "__main__":
    asyncio.run(_main())
