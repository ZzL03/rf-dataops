import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

LABEL_SQL = "'BPSK','QPSK','8PSK','16QAM','64QAM','UNKNOWN'"


class AuthSession(Base):
    __tablename__ = "auth_sessions"
    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), index=True
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class LoginBucket(Base):
    __tablename__ = "login_buckets"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    window_start: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    attempts: Mapped[int] = mapped_column()


class ModelVersion(Base):
    __tablename__ = "model_versions"
    __table_args__ = (
        UniqueConstraint("name", "version", name="uq_model_version"),
        CheckConstraint("adapter IN ('mock','torchscript')", name="valid_adapter"),
        CheckConstraint(
            "(adapter='mock' AND artifact_sha256 IS NULL) OR "
            "(adapter='torchscript' AND artifact_sha256 IS NOT NULL)",
            name="valid_artifact",
        ),
    )
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(64))
    version: Mapped[str] = mapped_column(String(64))
    adapter: Mapped[str] = mapped_column(String(16))
    artifact_sha256: Mapped[str | None] = mapped_column(String(64))
    config: Mapped[dict] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Prediction(Base):
    __tablename__ = "predictions"
    __table_args__ = (
        UniqueConstraint("segment_id", "model_version_id", name="uq_prediction_identity"),
        CheckConstraint(f"label IN ({LABEL_SQL})", name="valid_label"),
        CheckConstraint(
            "confidence >= 0 AND confidence <= 1 AND inference_ms >= 0 "
            "AND inference_ms < 'Infinity'::float8",
            name="valid_numbers",
        ),
        Index("ix_predictions_segment", "segment_id", "created_at", "id"),
    )
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    segment_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("signal_segments.id", ondelete="RESTRICT")
    )
    model_version_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("model_versions.id", ondelete="RESTRICT")
    )
    label: Mapped[str] = mapped_column(String(16))
    scores: Mapped[dict] = mapped_column(JSONB)
    confidence: Mapped[float] = mapped_column(Float)
    inference_ms: Mapped[float] = mapped_column(Float)
    is_mock: Mapped[bool] = mapped_column()
    digest: Mapped[str] = mapped_column(String(64))
    created_by_job_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("processing_jobs.id", ondelete="RESTRICT")
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ReviewTask(Base):
    __tablename__ = "review_tasks"
    __table_args__ = (
        UniqueConstraint("segment_id", name="uq_review_segment"),
        CheckConstraint("status IN ('PENDING','REVIEWED','UNKNOWN')", name="valid_status"),
        CheckConstraint(
            "(label_version=0 AND current_annotation_id IS NULL AND status='PENDING') OR "
            "(label_version>0 AND current_annotation_id IS NOT NULL "
            "AND status IN ('REVIEWED','UNKNOWN'))",
            name="valid_current",
        ),
        Index("ix_reviews_status", "status", "updated_at", "id"),
    )
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    segment_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("signal_segments.id", ondelete="RESTRICT")
    )
    status: Mapped[str] = mapped_column(String(16), server_default="PENDING")
    label_version: Mapped[int] = mapped_column(server_default="0")
    current_annotation_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey(
            "annotations.id",
            ondelete="RESTRICT",
            use_alter=True,
            deferrable=True,
            initially="DEFERRED",
        )
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Annotation(Base):
    __tablename__ = "annotations"
    __table_args__ = (
        UniqueConstraint("review_task_id", "version", name="uq_annotation_version"),
        CheckConstraint("version>0", name="positive_version"),
        CheckConstraint(f"label IN ({LABEL_SQL})", name="valid_label"),
        CheckConstraint(
            "action IN ('CONFIRM','CORRECT','UNKNOWN') "
            "AND ((action='UNKNOWN') = (label='UNKNOWN'))",
            name="valid_action",
        ),
    )
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    review_task_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("review_tasks.id", ondelete="RESTRICT")
    )
    version: Mapped[int] = mapped_column()
    label: Mapped[str] = mapped_column(String(16))
    action: Mapped[str] = mapped_column(String(16))
    comment: Mapped[str] = mapped_column(Text, default="")
    reviewer_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    prediction_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("predictions.id", ondelete="RESTRICT")
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
