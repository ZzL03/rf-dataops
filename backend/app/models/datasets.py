import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
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
from app.models.labeling import LABEL_SQL


class Dataset(Base):
    __tablename__ = "datasets"
    __table_args__ = (UniqueConstraint("root_key", "name", name="uq_dataset_name"),)
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    root_key: Mapped[str] = mapped_column(String(64))
    name: Mapped[str] = mapped_column(String(100))
    description: Mapped[str] = mapped_column(Text)
    created_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class DatasetVersion(Base):
    __tablename__ = "dataset_versions"
    __table_args__ = (
        UniqueConstraint("dataset_id", "version", name="uq_dataset_version"),
        UniqueConstraint("publish_job_id", name="uq_dataset_publish_job"),
        CheckConstraint("label_policy IN ('HUMAN_ONLY','MODEL_ALLOWED')", name="valid_policy"),
        CheckConstraint(
            "(label_policy='MODEL_ALLOWED') = (model_version_id IS NOT NULL) "
            "AND (NOT allow_mock_predictions OR label_policy='MODEL_ALLOWED')",
            name="valid_model",
        ),
        CheckConstraint(
            "item_count BETWEEN 0 AND 10000 AND octet_length(manifest_text)<=16777216",
            name="bounded_manifest",
        ),
        CheckConstraint(
            "NOT sealed OR (item_count>0 AND manifest_sha256 ~ '^[a-f0-9]{64}$')", name="valid_seal"
        ),
        Index("ix_dataset_versions_created", "dataset_id", "created_at", "id"),
    )
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    dataset_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("datasets.id", ondelete="RESTRICT"))
    version: Mapped[str] = mapped_column(String(64))
    selection: Mapped[dict] = mapped_column(JSONB)
    label_policy: Mapped[str] = mapped_column(String(16))
    model_version_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("model_versions.id", ondelete="RESTRICT")
    )
    allow_mock_predictions: Mapped[bool] = mapped_column(Boolean, server_default="false")
    published_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    publish_job_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("processing_jobs.id", ondelete="RESTRICT", use_alter=True)
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    creation_xid: Mapped[str] = mapped_column(
        Text, server_default=text("pg_current_xact_id()::text")
    )
    sealed: Mapped[bool] = mapped_column(Boolean, server_default="false")
    item_count: Mapped[int] = mapped_column(server_default="0")
    manifest_text: Mapped[str] = mapped_column(Text, server_default="")
    manifest_sha256: Mapped[str] = mapped_column(String(64), server_default="")


class DatasetVersionItem(Base):
    __tablename__ = "dataset_version_items"
    __table_args__ = (
        UniqueConstraint("dataset_version_id", "segment_id", name="uq_dataset_member"),
        CheckConstraint(
            "(label_source='HUMAN' AND annotation_id IS NOT NULL AND prediction_id IS NULL) OR "
            "(label_source='MODEL' AND annotation_id IS NULL AND prediction_id IS NOT NULL)",
            name="valid_source",
        ),
        CheckConstraint(f"frozen_label IN ({LABEL_SQL})", name="valid_label"),
    )
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    dataset_version_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("dataset_versions.id", ondelete="RESTRICT")
    )
    segment_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("signal_segments.id", ondelete="RESTRICT")
    )
    raw_file_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("raw_files.id", ondelete="RESTRICT"))
    annotation_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("annotations.id", ondelete="RESTRICT")
    )
    prediction_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("predictions.id", ondelete="RESTRICT")
    )
    label_source: Mapped[str] = mapped_column(String(8))
    frozen_label: Mapped[str] = mapped_column(String(16))
    snapshot: Mapped[dict] = mapped_column(JSONB)
