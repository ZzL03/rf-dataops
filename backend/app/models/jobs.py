import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class ProcessingJob(Base):
    __tablename__ = "processing_jobs"
    __table_args__ = (
        UniqueConstraint("dedupe_key", "generation", name="uq_job_generation"),
        CheckConstraint(
            "job_type IN ('SCAN','SEGMENT','QUALITY','INFER','PUBLISH')", name="valid_type"
        ),
        CheckConstraint(
            "status IN ('PENDING','RUNNING','RETRY_WAIT','SUCCEEDED','FAILED')", name="valid_status"
        ),
        CheckConstraint(
            "attempts >= 0 AND attempts <= max_attempts AND max_attempts BETWEEN "
            "1 AND 10 AND execution_version >= 0 AND generation >= 0",
            name="valid_attempts",
        ),
        CheckConstraint("priority BETWEEN -100 AND 100", name="valid_priority"),
        CheckConstraint(
            "(status = 'RUNNING' AND worker_id IS NOT NULL AND lease_until IS NOT"
            " NULL) OR (status <> 'RUNNING' AND lease_until IS NULL)",
            name="valid_lease",
        ),
        CheckConstraint(
            "(job_type='SCAN' AND raw_file_id IS NULL AND segment_id IS NULL) OR "
            "(job_type='SEGMENT' AND raw_file_id IS NOT NULL AND segment_id IS "
            "NULL) OR (job_type='QUALITY' AND raw_file_id IS NULL AND segment_id "
            "IS NOT NULL) OR (job_type='INFER' AND raw_file_id IS NULL AND segment_id IS NOT NULL) "
            "OR (job_type='PUBLISH' AND raw_file_id IS NULL AND segment_id IS NULL)",
            name="valid_target",
        ),
        CheckConstraint("(job_type='INFER') = (model_version_id IS NOT NULL)", name="valid_model"),
        CheckConstraint("(job_type='PUBLISH') = (dataset_id IS NOT NULL)", name="valid_dataset"),
        CheckConstraint(
            "(status='SUCCEEDED') = (result IS NOT NULL AND result_digest IS NOT NULL)",
            name="valid_result",
        ),
        Index(
            "ix_jobs_claim",
            "root_key",
            text("priority DESC"),
            "next_run_at",
            "created_at",
            "id",
            postgresql_where=text("status IN ('PENDING','RETRY_WAIT')"),
        ),
        Index(
            "ix_jobs_expired",
            "root_key",
            "lease_until",
            "id",
            postgresql_where=text("status = 'RUNNING'"),
        ),
        Index("ix_jobs_created", "root_key", "created_at", "id"),
        Index("ix_jobs_raw_file", "raw_file_id"),
        Index("ix_jobs_segment", "segment_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    job_type: Mapped[str] = mapped_column(String(16))
    root_key: Mapped[str] = mapped_column(String(64))
    target_identity: Mapped[str] = mapped_column(String(512))
    raw_file_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("raw_files.id", ondelete="RESTRICT")
    )
    segment_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("signal_segments.id", ondelete="RESTRICT")
    )
    payload: Mapped[dict] = mapped_column(JSONB)
    dataset_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("datasets.id", ondelete="RESTRICT", use_alter=True)
    )
    model_version_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("model_versions.id", ondelete="RESTRICT", use_alter=True)
    )
    dedupe_key: Mapped[str] = mapped_column(String(64))
    generation: Mapped[int] = mapped_column(server_default="0")
    parent_job_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("processing_jobs.id", ondelete="RESTRICT")
    )
    status: Mapped[str] = mapped_column(String(16), server_default="PENDING")
    priority: Mapped[int] = mapped_column(server_default="0")
    attempts: Mapped[int] = mapped_column(server_default="0")
    max_attempts: Mapped[int] = mapped_column(server_default="3")
    worker_id: Mapped[str | None] = mapped_column(String(64))
    execution_version: Mapped[int] = mapped_column(server_default="0")
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_run_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    last_error: Mapped[str | None] = mapped_column(Text)
    result: Mapped[dict | None] = mapped_column(JSONB)
    result_digest: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class JobRequest(Base):
    """Many client idempotency keys may refer to one logical job."""

    __tablename__ = "job_requests"

    request_key: Mapped[str] = mapped_column(String(128), primary_key=True)
    request_hash: Mapped[str] = mapped_column(String(64))
    job_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("processing_jobs.id", ondelete="RESTRICT"), index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class JobEvent(Base):
    __tablename__ = "job_events"
    __table_args__ = (Index("ix_job_events_history", "job_id", "created_at", "id"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    job_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("processing_jobs.id", ondelete="RESTRICT"))
    event: Mapped[str] = mapped_column(String(32))
    worker_id: Mapped[str | None] = mapped_column(String(64))
    execution_version: Mapped[int] = mapped_column()
    detail: Mapped[dict] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class SegmentQuality(Base):
    __tablename__ = "segment_quality_results"
    __table_args__ = (
        UniqueConstraint("segment_id", "algorithm_version", name="uq_segment_quality"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    segment_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("signal_segments.id", ondelete="RESTRICT")
    )
    algorithm_version: Mapped[str] = mapped_column(String(64))
    metrics: Mapped[dict] = mapped_column(JSONB)
    digest: Mapped[str] = mapped_column(String(64))
    created_by_job_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("processing_jobs.id", ondelete="RESTRICT")
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
