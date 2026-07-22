from __future__ import annotations

import logging
import os
import signal
import socket
import threading

import pymysql

from .config import (
    TRAVEL_IMPORT_JOB_STALE_SECONDS,
    TRAVEL_IMPORT_WORKER_POLL_SECONDS,
)
from .connectors import DB_CONFIG, init_db
from .services.import_processor import ImportProcessor
from .services.import_repository import (
    claim_next_job,
    complete_job,
    fail_job,
)
from .services.storage import ensure_bucket

logger = logging.getLogger(__name__)
stop_event = threading.Event()


def main() -> None:
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    init_db()
    ensure_bucket()
    lock_connection = _acquire_single_worker_lock()
    worker_id = f"{socket.gethostname()}:{os.getpid()}"
    processor = ImportProcessor()
    logger.info("Travel import worker started as %s", worker_id)
    try:
        while not stop_event.is_set():
            job = claim_next_job(worker_id, TRAVEL_IMPORT_JOB_STALE_SECONDS)
            if not job:
                stop_event.wait(TRAVEL_IMPORT_WORKER_POLL_SECONDS)
                continue
            logger.info("Claimed import job %s for batch %s", job["id"], job["batch_id"])
            try:
                processor.process(job)
                complete_job(job["id"], job["batch_id"])
            except Exception as exc:
                logger.exception("Import job %s failed", job["id"])
                fail_job(job["id"], job["batch_id"], str(exc))
    finally:
        try:
            with lock_connection.cursor() as cursor:
                cursor.execute("SELECT RELEASE_LOCK('travel-import-worker')")
        finally:
            lock_connection.close()
    logger.info("Travel import worker stopped")


def _acquire_single_worker_lock():
    connection = pymysql.connect(**DB_CONFIG)
    with connection.cursor() as cursor:
        cursor.execute("SELECT GET_LOCK('travel-import-worker', 0) AS acquired")
        if cursor.fetchone()["acquired"] != 1:
            connection.close()
            raise RuntimeError("Another travel import worker already owns the DB lock")
    return connection


def _stop(_signum, _frame) -> None:
    stop_event.set()


if __name__ == "__main__":
    main()
