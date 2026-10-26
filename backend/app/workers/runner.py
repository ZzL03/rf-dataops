import argparse
import logging
import signal
import threading
import time
import uuid

from sqlalchemy.exc import OperationalError, SQLAlchemyError

from app.core.config import Settings
from app.core.errors import DomainError
from app.db.session import build_engine, build_session_factory
from app.services.job_queue import JobQueue, StaleExecution
from app.services.job_work import prepare_work

logger = logging.getLogger(__name__)


class LeaseGuard:
    """Heartbeat/recovery use their own sessions while the main thread computes."""

    def __init__(self, queue: JobQueue, token) -> None:
        self.queue, self.token = queue, token
        self.stop = threading.Event()
        self.lost = threading.Event()
        self.thread = threading.Thread(target=self._maintain, daemon=True)

    def _maintain(self) -> None:
        next_recovery = time.monotonic() + self.queue.settings.worker_recovery_seconds
        while not self.stop.wait(self.queue.settings.worker_heartbeat_seconds):
            try:
                if not self.lost.is_set() and not self.queue.heartbeat(self.token):
                    self.lost.set()
                if time.monotonic() >= next_recovery:
                    self.queue.recover()
                    next_recovery = time.monotonic() + self.queue.settings.worker_recovery_seconds
            except SQLAlchemyError as exc:
                self.lost.set()
                logger.warning(
                    "lease_maintenance_failed job_id=%s error=%s",
                    self.token.job_id,
                    type(exc).__name__,
                )

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *_args):
        self.stop.set()
        self.thread.join(timeout=self.queue.settings.database_connect_timeout + 5)


class Worker:
    def __init__(self, queue: JobQueue, worker_id: str | None = None) -> None:
        self.queue = queue
        self.worker_id = worker_id or str(uuid.uuid4())

    def run_once(self) -> bool:
        self.queue.recover()
        token = self.queue.claim(self.worker_id)
        if token is None:
            return False
        logger.info(
            "job_claimed job_id=%s worker_id=%s execution_version=%s",
            token.job_id,
            self.worker_id,
            token.execution_version,
        )
        with LeaseGuard(self.queue, token) as lease:
            try:
                prepared = prepare_work(self.queue.settings, self.queue.factory, token)
                if lease.lost.is_set():
                    raise StaleExecution()
                self.queue.complete(token, prepared)
                logger.info(
                    "job_succeeded job_id=%s execution_version=%s",
                    token.job_id,
                    token.execution_version,
                )
            except StaleExecution:
                logger.warning(
                    "stale_execution_discarded job_id=%s execution_version=%s",
                    token.job_id,
                    token.execution_version,
                )
            except Exception as exc:
                if isinstance(exc, DomainError):
                    error = f"{exc.code}: {exc.message}"
                    retryable = exc.status_code >= 500 or exc.code in {
                        "SOURCE_UNREADABLE",
                        "SCAN_UNREADABLE",
                    }
                else:
                    error = (
                        "DATABASE_UNAVAILABLE: transient database failure"
                        if isinstance(exc, OperationalError)
                        else "INTERNAL_ERROR: unexpected worker failure"
                    )
                    retryable = isinstance(exc, OperationalError)
                logger.warning(
                    "job_execution_failed job_id=%s error_type=%s retryable=%s",
                    token.job_id,
                    type(exc).__name__,
                    retryable,
                )
                try:
                    self.queue.fail(token, error, retryable)
                except SQLAlchemyError as failure:
                    # If commit acknowledgement is ambiguous, DB state remains authoritative.
                    logger.warning(
                        "failure_record_unavailable job_id=%s error=%s",
                        token.job_id,
                        type(failure).__name__,
                    )
        return True


def main() -> int:
    parser = argparse.ArgumentParser(description="RF-DataOps PostgreSQL Worker")
    parser.add_argument(
        "--once", action="store_true", help="Process at most one available attempt, then exit"
    )
    args = parser.parse_args()
    settings = Settings()
    logging.basicConfig(
        level=settings.log_level, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    engine = build_engine(settings)
    worker = Worker(JobQueue(settings, build_session_factory(engine)))
    stop = threading.Event()
    signal.signal(signal.SIGINT, lambda *_args: stop.set())
    signal.signal(signal.SIGTERM, lambda *_args: stop.set())
    try:
        while not stop.is_set():
            try:
                claimed = worker.run_once()
            except SQLAlchemyError as exc:
                logger.warning("queue_unavailable error=%s", type(exc).__name__)
                if args.once:
                    return 1
                claimed = False
            if args.once:
                return 0
            if not claimed:
                stop.wait(settings.worker_poll_seconds)
    finally:
        engine.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
