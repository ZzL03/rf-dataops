import re
import uuid
from dataclasses import dataclass

from sqlalchemy import func, select, text, update
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings
from app.core.errors import DomainError
from app.core.identity import READER_VERSION, content_key
from app.models import (
    DatasetVersion,
    JobEvent,
    JobRequest,
    ModelVersion,
    ProcessingJob,
    RawFile,
    SignalSegment,
    User,
)
from app.schemas.jobs import (
    InferenceJobRequest,
    PublishJobRequest,
    QualityJobRequest,
    ScanJobRequest,
    SegmentJobRequest,
)
from app.services.job_work import QUALITY_VERSION, PreparedWork, apply_work
from app.services.segments import range_count
from app.services.storage import normalize_path


class StaleExecution(DomainError):
    def __init__(self) -> None:
        super().__init__("STALE_EXECUTION", "Worker no longer owns this execution", 409)


@dataclass(frozen=True)
class ExecutionToken:
    job_id: uuid.UUID
    worker_id: str
    execution_version: int
    job_type: str
    payload: dict
    dedupe_key: str


def add_event(session: Session, job: ProcessingJob, event: str, detail: dict | None = None) -> None:
    session.add(
        JobEvent(
            job_id=job.id,
            event=event,
            worker_id=job.worker_id,
            execution_version=job.execution_version,
            detail=detail or {},
        )
    )


def advisory_lock(session: Session, key: str) -> None:
    session.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:key,0))"), {"key": key})


class JobQueue:
    def __init__(self, settings: Settings, factory: sessionmaker[Session]) -> None:
        self.settings, self.factory = settings, factory

    def _request(self, session: Session, key: str, digest: str) -> ProcessingJob | None:
        if not re.fullmatch(r"[A-Za-z0-9_.-]{8,128}", key):
            raise DomainError(
                "INVALID_IDEMPOTENCY_KEY", "Use an 8-128 character ASCII idempotency key"
            )
        advisory_lock(session, "request:" + key)
        prior = session.get(JobRequest, key)
        if prior:
            if prior.request_hash != digest:
                raise DomainError(
                    "IDEMPOTENCY_CONFLICT", "Idempotency key was used with a different request", 409
                )
            job = session.get(ProcessingJob, prior.job_id)
            if job.root_key != self.settings.data_root_key:
                raise DomainError(
                    "IDEMPOTENCY_CONFLICT", "Idempotency key belongs to another root", 409
                )
            return job
        return None

    def submit(
        self,
        request: ScanJobRequest
        | SegmentJobRequest
        | QualityJobRequest
        | InferenceJobRequest
        | PublishJobRequest,
        request_key: str,
        origin: str = "cli",
    ) -> ProcessingJob:
        root = self.settings.data_root_key
        # HTTP replay checks the original body; logical identity normalizes processing params.
        request_hash = content_key({"root": root, "body": request.model_dump(mode="json")})
        with self.factory.begin() as session:
            prior = self._request(session, request_key, request_hash)
            if prior:
                return prior
            raw_id = segment_id = model_id = dataset_id = None
            payload = {"reader_version": READER_VERSION}
            if isinstance(request, ScanJobRequest):
                target = normalize_path(request.directory, directory=True)
                payload.update(directory=target, scan_request_key=request_key)
            elif isinstance(request, PublishJobRequest):
                from app.services.datasets import find_dataset

                dataset = find_dataset(session, request.dataset_id, root)
                try:
                    actor = session.get(User, uuid.UUID(origin.removeprefix("user:")))
                except ValueError:
                    actor = None
                if (
                    actor is None
                    or not actor.is_active
                    or actor.role not in {"ADMIN", "RESEARCHER"}
                ):
                    raise DomainError(
                        "FORBIDDEN", "Publication requires an authenticated publisher", 403
                    )
                if session.scalar(
                    select(DatasetVersion.id).where(
                        DatasetVersion.dataset_id == dataset.id,
                        DatasetVersion.version == request.publication.version,
                    )
                ):
                    raise DomainError("VERSION_EXISTS", "Dataset version already exists", 409)
                dataset_id, target = dataset.id, str(dataset.id)
                payload.update(
                    dataset_id=str(dataset.id),
                    publisher_id=str(actor.id),
                    publication=request.publication.model_dump(mode="json"),
                )
            elif isinstance(request, SegmentJobRequest):
                raw = session.get(RawFile, request.raw_file_id)
                if raw is None or raw.root_key != root:
                    raise DomainError("NOT_FOUND", "File not found", 404)
                if raw.status != "ACTIVE":
                    raise DomainError("SOURCE_INACTIVE", "Source is not active", 409)
                end, _ = range_count(
                    raw.sample_count,
                    request.config,
                    request.start,
                    request.end,
                    self.settings.max_segments_per_call,
                )
                raw_id, target = raw.id, str(raw.id)
                payload.update(
                    file_id=str(raw.id),
                    config=request.config.normalized(),
                    start=request.start,
                    end=end,
                )
            else:
                segment = session.get(SignalSegment, request.segment_id)
                raw = session.get(RawFile, segment.raw_file_id) if segment else None
                if raw is None or raw.root_key != root:
                    raise DomainError("NOT_FOUND", "Segment not found", 404)
                if raw.status != "ACTIVE":
                    raise DomainError("SOURCE_INACTIVE", "Source is not active", 409)
                segment_id, target = segment.id, str(segment.id)
                payload.update(segment_id=str(segment_id))
                if isinstance(request, InferenceJobRequest):
                    model = session.get(ModelVersion, request.model_version_id)
                    if model is None:
                        raise DomainError("NOT_FOUND", "Model version not found", 404)
                    model_id = model.id
                    payload.update(model_version_id=str(model_id))
                else:
                    payload.update(algorithm_version=QUALITY_VERSION)
            key = content_key({"root": root, "type": request.job_type, "payload": payload})
            advisory_lock(session, "job:" + key)
            job = session.scalar(
                select(ProcessingJob)
                .where(ProcessingJob.dedupe_key == key)
                .order_by(ProcessingJob.generation.desc())
                .limit(1)
            )
            if job is None:
                job = ProcessingJob(
                    job_type=request.job_type,
                    root_key=root,
                    target_identity=target,
                    raw_file_id=raw_id,
                    segment_id=segment_id,
                    model_version_id=model_id,
                    dataset_id=dataset_id,
                    payload=payload,
                    dedupe_key=key,
                    priority=request.priority,
                    max_attempts=request.max_attempts,
                )
                session.add(job)
                session.flush()
                add_event(session, job, "ENQUEUED", {"origin": origin})
            session.add(
                JobRequest(request_key=request_key, request_hash=request_hash, job_id=job.id)
            )
            return job

    def rerun(
        self,
        job_id: uuid.UUID,
        request_key: str,
        max_attempts: int | None = None,
        origin: str = "cli",
    ) -> ProcessingJob:
        digest = content_key(
            {
                "root": self.settings.data_root_key,
                "rerun": str(job_id),
                "max_attempts": max_attempts,
            }
        )
        with self.factory.begin() as session:
            prior = self._request(session, request_key, digest)
            if prior:
                return prior
            parent = session.get(ProcessingJob, job_id)
            if parent is None or parent.root_key != self.settings.data_root_key:
                raise DomainError("NOT_FOUND", "Job not found", 404)
            advisory_lock(session, "job:" + parent.dedupe_key)
            session.refresh(parent, with_for_update=True)
            latest = session.scalar(
                select(func.max(ProcessingJob.generation)).where(
                    ProcessingJob.dedupe_key == parent.dedupe_key
                )
            )
            if parent.status != "FAILED" or parent.generation != latest:
                raise DomainError(
                    "RERUN_CONFLICT", "Only the latest failed generation may be rerun", 409
                )
            job = ProcessingJob(
                job_type=parent.job_type,
                root_key=parent.root_key,
                target_identity=parent.target_identity,
                raw_file_id=parent.raw_file_id,
                segment_id=parent.segment_id,
                model_version_id=parent.model_version_id,
                dataset_id=parent.dataset_id,
                payload=parent.payload,
                dedupe_key=parent.dedupe_key,
                generation=latest + 1,
                parent_job_id=parent.id,
                priority=parent.priority,
                max_attempts=max_attempts or parent.max_attempts,
            )
            session.add(job)
            session.flush()
            session.add(JobRequest(request_key=request_key, request_hash=digest, job_id=job.id))
            add_event(session, job, "ENQUEUED", {"origin": origin, "parent_job_id": str(parent.id)})
            add_event(
                session, parent, "RERUN_REQUESTED", {"new_job_id": str(job.id), "origin": origin}
            )
            return job

    def claim(self, worker_id: str) -> ExecutionToken | None:
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", worker_id):
            raise ValueError("Invalid worker identity")
        with self.factory.begin() as session:
            candidate = (
                select(ProcessingJob.id)
                .where(
                    ProcessingJob.root_key == self.settings.data_root_key,
                    ProcessingJob.status.in_(["PENDING", "RETRY_WAIT"]),
                    ProcessingJob.next_run_at <= func.clock_timestamp(),
                    ProcessingJob.attempts < ProcessingJob.max_attempts,
                )
                .order_by(
                    ProcessingJob.priority.desc(),
                    ProcessingJob.next_run_at,
                    ProcessingJob.created_at,
                    ProcessingJob.id,
                )
                .limit(1)
                .with_for_update(skip_locked=True)
                .cte("candidate")
            )
            statement = (
                update(ProcessingJob)
                .where(ProcessingJob.id == candidate.c.id)
                .values(
                    status="RUNNING",
                    worker_id=worker_id,
                    attempts=ProcessingJob.attempts + 1,
                    execution_version=ProcessingJob.execution_version + 1,
                    lease_until=func.clock_timestamp()
                    + func.make_interval(0, 0, 0, 0, 0, 0, self.settings.worker_lease_seconds),
                    updated_at=func.clock_timestamp(),
                )
                .returning(ProcessingJob)
            )
            job = session.scalar(statement)
            if job is None:
                return None
            add_event(session, job, "CLAIMED")
            return ExecutionToken(
                job.id, worker_id, job.execution_version, job.job_type, job.payload, job.dedupe_key
            )

    def _conditions(self, token: ExecutionToken) -> tuple:
        return (
            ProcessingJob.id == token.job_id,
            ProcessingJob.root_key == self.settings.data_root_key,
            ProcessingJob.status == "RUNNING",
            ProcessingJob.worker_id == token.worker_id,
            ProcessingJob.execution_version == token.execution_version,
            ProcessingJob.lease_until > func.clock_timestamp(),
        )

    def heartbeat(self, token: ExecutionToken) -> bool:
        with self.factory.begin() as session:
            renewed = session.scalar(
                update(ProcessingJob)
                .where(*self._conditions(token))
                .values(
                    lease_until=func.clock_timestamp()
                    + func.make_interval(0, 0, 0, 0, 0, 0, self.settings.worker_lease_seconds),
                    updated_at=func.clock_timestamp(),
                )
                .returning(ProcessingJob.id)
            )
            return renewed is not None

    def complete(self, token: ExecutionToken, prepared: PreparedWork) -> dict:
        if prepared.job_id != token.job_id or prepared.input_key != token.dedupe_key:
            raise DomainError("RESULT_MISMATCH", "Prepared result belongs to another job", 409)
        digest = prepared.digest
        with self.factory.begin() as session:
            if token.job_type == "PUBLISH":
                session.connection(execution_options={"isolation_level": "REPEATABLE READ"})
            job = session.scalar(
                select(ProcessingJob)
                .where(
                    ProcessingJob.id == token.job_id,
                    ProcessingJob.root_key == self.settings.data_root_key,
                )
                .with_for_update()
            )
            if (
                job is None
                or job.worker_id != token.worker_id
                or job.execution_version != token.execution_version
            ):
                raise StaleExecution()
            if (
                job.dedupe_key != token.dedupe_key
                or job.job_type != token.job_type
                or job.payload != token.payload
            ):
                raise DomainError(
                    "RESULT_MISMATCH", "Execution inputs do not match the persisted job", 409
                )
            if job.status == "SUCCEEDED":
                if job.result_digest != digest:
                    raise DomainError(
                        "RESULT_CONFLICT", "Execution replay has different result content", 409
                    )
                return job.result
            if session.scalar(select(ProcessingJob.id).where(*self._conditions(token))) is None:
                raise StaleExecution()
            result = apply_work(session, self.settings, job, prepared)
            session.flush()
            finished = session.scalar(
                update(ProcessingJob)
                .where(*self._conditions(token))
                .values(
                    status="SUCCEEDED",
                    result=result,
                    result_digest=digest,
                    lease_until=None,
                    last_error=None,
                    updated_at=func.clock_timestamp(),
                )
                .returning(ProcessingJob.id)
                .execution_options(synchronize_session=False)
            )
            if finished is None:
                raise StaleExecution()
            add_event(session, job, "SUCCEEDED", {"result_digest": digest})
            return result

    def _transition_failure(
        self, session: Session, job: ProcessingJob, error: str, retryable: bool, event: str
    ) -> None:
        retry = retryable and job.attempts < job.max_attempts
        delay = min(self.settings.retry_base_seconds * 2 ** (job.attempts - 1), 60) if retry else 0
        add_event(
            session,
            job,
            event,
            {"error": error[:1000], "retryable": retryable, "retry_delay_seconds": delay},
        )
        job.status = "RETRY_WAIT" if retry else "FAILED"
        job.last_error, job.worker_id, job.lease_until = error[:1000], None, None
        job.next_run_at = func.clock_timestamp() + func.make_interval(0, 0, 0, 0, 0, 0, delay)
        job.updated_at = func.clock_timestamp()

    def fail(self, token: ExecutionToken, error: str, retryable: bool) -> bool:
        with self.factory.begin() as session:
            job = session.scalar(
                select(ProcessingJob).where(*self._conditions(token)).with_for_update()
            )
            if job is None:
                return False
            self._transition_failure(session, job, error, retryable, "EXECUTION_FAILED")
            return True

    def recover(self, limit: int = 100) -> int:
        with self.factory.begin() as session:
            jobs = list(
                session.scalars(
                    select(ProcessingJob)
                    .where(
                        ProcessingJob.root_key == self.settings.data_root_key,
                        ProcessingJob.status == "RUNNING",
                        ProcessingJob.lease_until <= func.clock_timestamp(),
                    )
                    .order_by(ProcessingJob.lease_until, ProcessingJob.id)
                    .limit(limit)
                    .with_for_update(skip_locked=True)
                )
            )
            for job in jobs:
                self._transition_failure(
                    session, job, "LEASE_EXPIRED: execution lease expired", True, "LEASE_EXPIRED"
                )
            return len(jobs)
