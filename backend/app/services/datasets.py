"""Bounded publication plans; official results are written only by fenced completion."""

import hashlib
import uuid
from collections import Counter
from dataclasses import dataclass

from sqlalchemy import and_, func, select, text
from sqlalchemy.exc import IntegrityError

from app.core.errors import DomainError
from app.core.identity import canonical_json, content_key
from app.models import (
    Annotation,
    Dataset,
    DatasetVersion,
    DatasetVersionItem,
    ModelVersion,
    Prediction,
    ProcessingConfig,
    RawFile,
    ReviewTask,
    SignalSegment,
    User,
)
from app.schemas.datasets import PublicationRequest, SelectionRequest
from app.services.ingestion import Inspection, inspect_file, lock_path, verify_observation
from app.services.storage import ControlledRoot

MAX_MEMBERS = 10000


@dataclass(frozen=True)
class Member:
    segment: SignalSegment
    raw: RawFile
    config: ProcessingConfig
    annotation: Annotation | None
    prediction: Prediction | None

    @property
    def source(self):
        return "HUMAN" if self.annotation else "MODEL"

    @property
    def label(self):
        return self.annotation.label if self.annotation else self.prediction.label

    def identity(self):
        return {
            "segment_id": str(self.segment.id),
            "raw_file_id": str(self.raw.id),
            "sha256": self.raw.sha256,
            "metadata_hash": self.raw.metadata_hash,
            "config_hash": self.config.config_hash,
            "annotation_id": str(self.annotation.id) if self.annotation else None,
            "prediction_id": str(self.prediction.id) if not self.annotation else None,
            "label_source": self.source,
            "label": self.label,
        }


@dataclass(frozen=True)
class PublicationPlan:
    dataset_id: uuid.UUID
    selection_digest: str
    inspections: tuple[tuple[uuid.UUID, Inspection], ...]


def find_dataset(session, dataset_id, root_key):
    dataset = session.get(Dataset, dataset_id)
    if dataset is None or dataset.root_key != root_key:
        raise DomainError("NOT_FOUND", "Dataset not found", 404)
    return dataset


def create_dataset(factory, root_key, body, user):
    from app.services.job_queue import advisory_lock

    with factory.begin() as session:
        advisory_lock(session, "dataset:" + root_key + ":" + body.name)
        if session.scalar(
            select(Dataset.id).where(Dataset.root_key == root_key, Dataset.name == body.name)
        ):
            raise DomainError("DATASET_EXISTS", "Dataset name already exists", 409)
        dataset = Dataset(
            root_key=root_key, name=body.name, description=body.description, created_by=user.id
        )
        session.add(dataset)
        session.flush()
        return dataset


def select_members(session, dataset, request: SelectionRequest):
    rule = request.selection
    for ids, entity, query in (
        (
            rule.raw_file_ids,
            RawFile,
            select(RawFile.id).where(RawFile.root_key == dataset.root_key),
        ),
        (
            rule.segment_ids,
            SignalSegment,
            select(SignalSegment.id).join(RawFile).where(RawFile.root_key == dataset.root_key),
        ),
    ):
        if ids and len(set(session.scalars(query.where(entity.id.in_(ids))))) != len(ids):
            raise DomainError("NOT_FOUND", "Selected source does not exist in this data root", 404)
    if request.model_version_id:
        model = session.get(ModelVersion, request.model_version_id)
        if model is None:
            raise DomainError("NOT_FOUND", "Model version not found", 404)
        if model.adapter == "mock" and not request.allow_mock_predictions:
            raise DomainError(
                "MOCK_ACK_REQUIRED",
                "Explicitly acknowledge Mock predictions before using this model",
            )
    effective_label = (
        func.coalesce(Annotation.label, Prediction.label)
        if request.label_policy == "MODEL_ALLOWED"
        else Annotation.label
    )
    query = (
        select(SignalSegment, RawFile, ProcessingConfig, Annotation, Prediction)
        .join(RawFile, SignalSegment.raw_file_id == RawFile.id)
        .join(ProcessingConfig, SignalSegment.config_id == ProcessingConfig.id)
        .outerjoin(ReviewTask, ReviewTask.segment_id == SignalSegment.id)
        .outerjoin(Annotation, Annotation.id == ReviewTask.current_annotation_id)
        .outerjoin(
            Prediction,
            and_(
                Prediction.segment_id == SignalSegment.id,
                Prediction.model_version_id == request.model_version_id,
            ),
        )
        .where(RawFile.root_key == dataset.root_key, effective_label.is_not(None))
    )
    if rule.raw_file_ids:
        query = query.where(RawFile.id.in_(rule.raw_file_ids))
    if rule.segment_ids:
        query = query.where(SignalSegment.id.in_(rule.segment_ids))
    if rule.review_status:
        query = query.where(func.coalesce(ReviewTask.status, "PENDING") == rule.review_status)
    if rule.labels:
        query = query.where(effective_label.in_(rule.labels))
    rows = list(session.execute(query.order_by(SignalSegment.id).limit(MAX_MEMBERS + 1)))
    if not rows:
        raise DomainError("EMPTY_SELECTION", "No eligible samples match the selection")
    if len(rows) > MAX_MEMBERS:
        raise DomainError("DATASET_LIMIT", "Narrow the selection to at most 10,000 samples")
    members = [Member(*row) for row in rows]
    if any(member.raw.status != "ACTIVE" for member in members):
        raise DomainError("SOURCE_INACTIVE", "A selected source is not active", 409)
    if len({member.raw.id for member in members}) > 100:
        raise DomainError("DATASET_FILE_LIMIT", "Narrow the selection to at most 100 source files")
    return members


def selection_digest(dataset, request, members):
    values = request.model_dump(mode="json")
    policy = {name: values[name] for name in SelectionRequest.model_fields}
    return content_key(
        {
            "dataset_id": str(dataset.id),
            "root_key": dataset.root_key,
            "request": policy,
            "members": [member.identity() for member in members],
        }
    )


def preview_selection(session, dataset, request):
    members = select_members(session, dataset, request)
    return {
        "item_count": len(members),
        "file_count": len({item.raw.id for item in members}),
        "selection_digest": selection_digest(dataset, request, members),
        "label_distribution": dict(Counter(item.label for item in members)),
        "source_counts": dict(Counter(item.source for item in members)),
        "contains_mock_predictions": any(
            item.source == "MODEL" and item.prediction.is_mock for item in members
        ),
        "items": [
            {
                **item.identity(),
                "relative_path": item.raw.relative_path,
                "start_sample": item.segment.start_sample,
                "sample_count": item.segment.sample_count,
            }
            for item in members[:20]
        ],
        "truncated": len(members) > 20,
    }


def prepare_publication(settings, factory, payload):
    request = PublicationRequest.model_validate(payload["publication"])
    with factory() as session:
        dataset = find_dataset(session, uuid.UUID(payload["dataset_id"]), settings.data_root_key)
        members = select_members(session, dataset, request)
        digest = selection_digest(dataset, request, members)
        if digest != request.expected_selection_digest:
            raise DomainError(
                "SELECTION_CHANGED",
                "Selection changed; preview and submit a new publication request",
                409,
            )
        files = {item.raw.id: item.raw for item in members}
    observations = []
    for file_id, raw in sorted(files.items(), key=lambda pair: pair[1].relative_path):
        inspected = inspect_file(ControlledRoot(settings.data_root), raw.relative_path)
        if (raw.sha256, raw.metadata_hash, raw.sample_count, raw.byte_size) != (
            inspected.sha256,
            inspected.metadata_hash,
            inspected.sample_count,
            inspected.byte_size,
        ):
            raise DomainError(
                "SOURCE_CHANGED", "Selected source content or interpretation changed", 409
            )
        observations.append((file_id, inspected))
    return PublicationPlan(dataset.id, digest, tuple(observations))


def apply_publication(session, settings, job, plan):
    if job.dataset_id != plan.dataset_id:
        raise DomainError("RESULT_MISMATCH", "Publication belongs to another dataset", 409)
    if session.scalar(text("SHOW transaction_isolation")) != "repeatable read":
        raise RuntimeError("Publication requires a repeatable-read completion transaction")
    request = PublicationRequest.model_validate(job.payload["publication"])
    dataset = find_dataset(session, job.dataset_id, settings.data_root_key)
    members = select_members(session, dataset, request)
    if selection_digest(dataset, request, members) != plan.selection_digest:
        raise DomainError(
            "SELECTION_CHANGED",
            "Selection changed; preview and submit a new publication request",
            409,
        )
    actor = session.get(User, uuid.UUID(job.payload["publisher_id"]))
    if actor is None or not actor.is_active or actor.role not in {"ADMIN", "RESEARCHER"}:
        raise DomainError("FORBIDDEN", "Publisher is no longer authorized", 403)
    observed = dict(plan.inspections)
    for raw in sorted(
        {item.raw.id: item.raw for item in members}.values(), key=lambda row: row.relative_path
    ):
        lock_path(session, settings.data_root_key, raw.relative_path)
        session.refresh(raw, with_for_update=True)
        if (
            raw.status != "ACTIVE"
            or raw.id not in observed
            or raw.sha256 != observed[raw.id].sha256
            or raw.metadata_hash != observed[raw.id].metadata_hash
        ):
            raise DomainError("SOURCE_CHANGED", "Source changed before publication commit", 409)
        verify_observation(ControlledRoot(settings.data_root), observed[raw.id])
    version = DatasetVersion(
        dataset_id=dataset.id,
        version=request.version,
        selection=request.selection.model_dump(mode="json"),
        label_policy=request.label_policy,
        model_version_id=request.model_version_id,
        allow_mock_predictions=request.allow_mock_predictions,
        published_by=actor.id,
        publish_job_id=job.id,
    )
    session.add(version)
    try:
        session.flush()
    except IntegrityError as exc:
        if getattr(exc.orig.diag, "constraint_name", None) == "uq_dataset_version":
            raise DomainError("VERSION_EXISTS", "Dataset version already exists", 409) from exc
        raise
    for member in members:
        session.add(
            DatasetVersionItem(
                dataset_version_id=version.id,
                segment_id=member.segment.id,
                raw_file_id=member.raw.id,
                annotation_id=member.annotation.id if member.annotation else None,
                prediction_id=member.prediction.id if not member.annotation else None,
                label_source=member.source,
                frozen_label=member.label,
                snapshot={},
            )
        )
    session.flush()
    manifest = session.scalar(text("SELECT rf_dataset_manifest(:id)"), {"id": version.id})
    encoded = canonical_json(manifest)
    if len(encoded.encode("utf-8")) > 16 * 1024 * 1024:
        raise DomainError("MANIFEST_LIMIT", "Manifest exceeds the 16 MiB publication limit")
    version.manifest_text = encoded
    version.manifest_sha256 = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
    version.item_count, version.sealed = len(members), True
    session.flush()
    return {
        "dataset_version_id": str(version.id),
        "dataset_id": str(dataset.id),
        "version": version.version,
        "item_count": version.item_count,
        "manifest_sha256": version.manifest_sha256,
        "label_distribution": manifest["label_distribution"],
        "source_counts": manifest["source_counts"],
        "contains_mock_predictions": manifest["contains_mock_predictions"],
    }
