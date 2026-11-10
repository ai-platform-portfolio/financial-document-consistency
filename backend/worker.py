"""Native Procrastinate worker with notification wake-up and bounded recovery."""

import asyncio
import os
from pathlib import Path
import sys
import time

HEALTH = Path(os.environ.get("WORKER_HEALTH_PATH", "/tmp/document-worker.ready"))
if __name__ == "__main__" and "--health" in sys.argv:
    sys.exit(0 if HEALTH.exists() and time.time() - HEALTH.stat().st_mtime < 30 else 1)

import procrastinate  # noqa: E402 — keep the health CLI free of runtime imports

from db import DATABASE_URL  # noqa: E402
from processing import process_review, TransientProcessingError  # noqa: E402
from telemetry import configure  # noqa: E402

queue = procrastinate.App(
    connector=procrastinate.PsycopgConnector(
        conninfo=DATABASE_URL,
        min_size=1,
        max_size=4,
        kwargs={
            "connect_timeout": 3,
            "options": "-c statement_timeout=3000 -c lock_timeout=2000",
        },
    )
)


@queue.task(
    name="process-review",
    retry=procrastinate.RetryStrategy(
        max_attempts=3, exponential_wait=5, retry_exceptions=(TransientProcessingError,)
    ),
)
async def process(review_id):
    await asyncio.to_thread(process_review, review_id)


@queue.periodic(cron="* * * * *")
@queue.task(name="recover-stalled", queueing_lock="recover-stalled", priority=100)
async def recover(timestamp):
    # The library's heartbeat test avoids racing a healthy long-running task.
    for job in await queue.job_manager.get_stalled_jobs(seconds_since_heartbeat=60):
        if job.task_name == "process-review":
            await queue.job_manager.retry_job(job)


async def pulse():
    while True:
        HEALTH.touch()
        await asyncio.sleep(5)


async def main():
    configure()
    async with queue.open_async():
        heartbeat = asyncio.create_task(pulse())
        try:
            await queue.run_worker_async(
                concurrency=1,
                listen_notify=True,
                fetch_job_polling_interval=30,
                shutdown_graceful_timeout=60,
            )
        finally:
            heartbeat.cancel()
            HEALTH.unlink(missing_ok=True)


if __name__ == "__main__":
    asyncio.run(main())
