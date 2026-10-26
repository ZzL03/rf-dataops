import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class RawFile(Base):
    __tablename__ = "raw_files"
    __table_args__ = (
        UniqueConstraint(
            "root_key", "relative_path", "sha256", "metadata_hash", name="uq_raw_identity"
        ),
        CheckConstraint("byte_size > 0 AND sample_count > 0", name="positive_size"),
        CheckConstraint("format IN ('BIN', 'HDF5')", name="valid_format"),
        CheckConstraint("status IN ('ACTIVE', 'STALE', 'INVALID')", name="valid_status"),
        CheckConstraint(
            "sha256 ~ '^[a-f0-9]{64}$' AND metadata_hash ~ '^[a-f0-9]{64}$'", name="valid_hashes"
        ),
        Index("ix_raw_files_path", "root_key", "relative_path"),
        Index("ix_raw_files_sha256", "sha256"),
        Index(
            "uq_raw_active_path",
            "root_key",
            "relative_path",
            unique=True,
            postgresql_where=text("status = 'ACTIVE'"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    root_key: Mapped[str] = mapped_column(String(64), nullable=False)
    relative_path: Mapped[str] = mapped_column(String(512), nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    metadata_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    byte_size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    mtime_ns: Mapped[int] = mapped_column(BigInteger, nullable=False)
    stat_identity: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    format: Mapped[str] = mapped_column(String(8), nullable=False)
    iq_metadata: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    sample_count: Mapped[int] = mapped_column(BigInteger, nullable=False)
    status: Mapped[str] = mapped_column(String(8), nullable=False, server_default="ACTIVE")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    observed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class ProcessingConfig(Base):
    __tablename__ = "processing_configs"
    __table_args__ = (
        UniqueConstraint("config_hash"),
        CheckConstraint("config_hash ~ '^[a-f0-9]{64}$'", name="valid_hash"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    config_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    parameters: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    reader_version: Mapped[str] = mapped_column(String(64), nullable=False)
    pipeline_version: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class SignalSegment(Base):
    __tablename__ = "signal_segments"
    __table_args__ = (
        UniqueConstraint(
            "raw_file_id", "config_id", "start_sample", "sample_count", name="uq_segment_identity"
        ),
        CheckConstraint(
            "start_sample >= 0 AND sample_count > 0 AND sample_count <= 1000000",
            name="valid_interval",
        ),
        Index("ix_signal_segments_file", "raw_file_id", "start_sample", "id"),
        Index("ix_signal_segments_config_id", "config_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    raw_file_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("raw_files.id", ondelete="RESTRICT"))
    config_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("processing_configs.id", ondelete="RESTRICT")
    )
    start_sample: Mapped[int] = mapped_column(BigInteger, nullable=False)
    sample_count: Mapped[int] = mapped_column(nullable=False)
    quality: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    created_by_job_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("processing_jobs.id", ondelete="RESTRICT", use_alter=True)
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class FileObservation(Base):
    """Append-only scanner events preserve invalidation and reactivation history."""

    __tablename__ = "file_observations"
    __table_args__ = (Index("ix_file_observations_file", "raw_file_id", "created_at"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    raw_file_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("raw_files.id", ondelete="RESTRICT"))
    event: Mapped[str] = mapped_column(String(40), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
