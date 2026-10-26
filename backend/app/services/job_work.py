import os
import uuid
from dataclasses import asdict, dataclass

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings
from app.core.errors import DomainError
from app.core.identity import READER_VERSION, content_key
from app.models import (
    ModelVersion,
    Prediction,
    ProcessingJob,
    RawFile,
    ReviewTask,
    SegmentQuality,
    SignalSegment,
)
from app.schemas.iq import SegmentConfig, metadata_adapter
from app.services.datasets import PublicationPlan, apply_publication, prepare_publication
from app.services.ingestion import (
    IngestionService,
    Inspection,
    ScanReport,
    inspect_file,
    lock_path,
    verify_observation,
)
from app.services.labeling import ensure_review
from app.services.segments import SegmentPlan, persist_segments, prepare_segments
from app.services.storage import ControlledRoot, signature
from app.signal_processing.models import adapter_for
from app.signal_processing.preview import build_preview
from app.signal_processing.readers import open_reader

QUALITY_VERSION = "quality-v1"


@dataclass(frozen=True)
class ScanPlan:
    inspections: tuple[Inspection, ...]
    errors: tuple[dict, ...]
    incomplete: tuple[str, ...]
    missing: tuple[str, ...]


@dataclass(frozen=True)
class QualityPlan:
    segment_id: uuid.UUID
    file_id: uuid.UUID
    observed: Inspection
    metrics: dict


@dataclass(frozen=True)
class InferencePlan:
    segment_id: uuid.UUID
    file_id: uuid.UUID
    observed: Inspection
    model_version_id: uuid.UUID
    prediction: dict


@dataclass(frozen=True)
class PreparedWork:
    job_id: uuid.UUID
    input_key: str
    data: ScanPlan | SegmentPlan | QualityPlan | InferencePlan | PublicationPlan

    @property
    def digest(self) -> str:
        def source(item: Inspection) -> dict:
            return {
                "path": item.relative_path,
                "sha256": item.sha256,
                "metadata_hash": item.metadata_hash,
                "samples": item.sample_count,
            }

        data = self.data
        if isinstance(data, PublicationPlan):
            result = {
                "dataset_id": str(data.dataset_id),
                "selection_digest": data.selection_digest,
                "sources": [source(item) for _, item in data.inspections],
            }
        elif isinstance(data, ScanPlan):
            result = {
                "sources": [source(item) for item in data.inspections],
                "errors": data.errors,
                "incomplete": data.incomplete,
                "missing": data.missing,
            }
        elif isinstance(data, SegmentPlan):
            result = {
                "source": source(data.observed),
                "file_id": str(data.file_id),
                "parameters": data.parameters,
                "start": data.start,
                "count": data.count,
            }
        elif isinstance(data, InferencePlan):
            result = {
                "source": source(data.observed),
                "segment_id": str(data.segment_id),
                "model_version_id": str(data.model_version_id),
                "prediction": {
                    key: value for key, value in data.prediction.items() if key != "inference_ms"
                },
            }
        else:
            result = {
                "source": source(data.observed),
                "segment_id": str(data.segment_id),
                "algorithm_version": QUALITY_VERSION,
                "metrics": data.metrics,
            }
        return content_key({"input_key": self.input_key, "prepared_result": result})


def prepare_work(settings: Settings, factory: sessionmaker[Session], token) -> PreparedWork:
    """Prepare in bounded memory with no official result writes or long database transaction."""
    payload = token.payload
    if payload.get("reader_version") != READER_VERSION:
        raise DomainError("READER_VERSION_MISMATCH", "Unsupported job reader version", 409)
    root = ControlledRoot(settings.data_root)
    if token.job_type == "PUBLISH":
        data = prepare_publication(settings, factory, payload)
    elif token.job_type == "SCAN":
        directory = payload["directory"]
        maximum = min(settings.max_scan_files, 100)
        paths = root.candidates(directory, maximum)
        with factory() as session:
            query = select(RawFile.relative_path).where(
                RawFile.root_key == settings.data_root_key, RawFile.status == "ACTIVE"
            )
            if directory != ".":
                query = query.where(
                    RawFile.relative_path.startswith(directory + "/", autoescape=True)
                )
            known = set(session.scalars(query.limit(maximum + 1)))
        missing = known.difference(paths)
        if len(paths) + len(missing) > maximum:
            raise DomainError("SCAN_LIMIT", "Narrow the scan directory: at most 100 paths per job")
        observed, errors, incomplete = [], [], []
        for path in paths:
            try:
                observed.append(inspect_file(root, path))
            except DomainError as exc:
                if exc.code == "INCOMPLETE":
                    incomplete.append(path)
                else:
                    errors.append({"path": path, "code": exc.code, "message": exc.message})
        data = ScanPlan(tuple(observed), tuple(errors), tuple(incomplete), tuple(sorted(missing)))
    elif token.job_type == "SEGMENT":
        config = SegmentConfig(
            window_samples=payload["config"]["window_samples"],
            stride_samples=payload["config"]["stride_samples"],
        )
        if config.normalized() != payload["config"]:
            raise DomainError(
                "CONFIG_VERSION_MISMATCH", "Unsupported processing configuration", 409
            )
        data = prepare_segments(
            settings,
            factory,
            uuid.UUID(payload["file_id"]),
            config,
            payload["start"],
            payload["end"],
        )
    elif token.job_type == "INFER":
        segment_id = uuid.UUID(payload["segment_id"])
        with factory() as session:
            segment = session.get(SignalSegment, segment_id)
            raw = session.get(RawFile, segment.raw_file_id) if segment else None
            model = session.get(ModelVersion, uuid.UUID(payload["model_version_id"]))
            if raw is None or raw.root_key != settings.data_root_key or raw.status != "ACTIVE":
                raise DomainError("SOURCE_INACTIVE", "Inference source is not active", 409)
            if model is None:
                raise DomainError("NOT_FOUND", "Model version not found", 404)
        observed = inspect_file(root, raw.relative_path)
        if observed.sha256 != raw.sha256 or observed.metadata_hash != raw.metadata_hash:
            raise DomainError("SOURCE_CHANGED", "Inference source content changed", 409)
        metadata = metadata_adapter.validate_python(raw.iq_metadata)
        with root.open_read(raw.relative_path) as stream, open_reader(stream, metadata) as reader:
            before = signature(os.fstat(stream.fileno()))
            iq = reader.read(segment.start_sample, segment.sample_count)
            if signature(os.fstat(stream.fileno())) != before:
                raise DomainError("SOURCE_CHANGED", "Source changed during inference", 409)
        prediction = adapter_for(settings, model).predict(iq)
        verify_observation(root, observed)
        data = InferencePlan(
            segment_id,
            raw.id,
            observed,
            model.id,
            {**prediction.semantic(), "inference_ms": prediction.inference_ms},
        )
    else:
        if payload.get("algorithm_version") != QUALITY_VERSION:
            raise DomainError("QUALITY_VERSION_MISMATCH", "Unsupported quality algorithm", 409)
        segment_id = uuid.UUID(payload["segment_id"])
        with factory() as session:
            segment = session.get(SignalSegment, segment_id)
            raw = session.get(RawFile, segment.raw_file_id) if segment else None
            if raw is None or raw.root_key != settings.data_root_key or raw.status != "ACTIVE":
                raise DomainError("SOURCE_INACTIVE", "Quality source is not active", 409)
        observed = inspect_file(root, raw.relative_path)
        if observed.sha256 != raw.sha256 or observed.metadata_hash != raw.metadata_hash:
            raise DomainError("SOURCE_CHANGED", "Quality source content changed", 409)
        metadata = metadata_adapter.validate_python(raw.iq_metadata)
        with root.open_read(raw.relative_path) as stream, open_reader(stream, metadata) as reader:
            before = signature(os.fstat(stream.fileno()))
            iq = reader.read(segment.start_sample, segment.sample_count)
            if signature(os.fstat(stream.fileno())) != before:
                raise DomainError(
                    "SOURCE_CHANGED", "Source changed during quality calculation", 409
                )
        metrics = build_preview(iq, segment.start_sample, metadata.sample_rate_hz, 32)["stats"]
        metrics.update(
            sample_count=segment.sample_count,
            finite=True,
            zero_power=metrics["mean_power"] == 0,
            algorithm_version=QUALITY_VERSION,
        )
        verify_observation(root, observed)
        data = QualityPlan(segment_id, raw.id, observed, metrics)
    return PreparedWork(token.job_id, token.dedupe_key, data)


def apply_work(
    session: Session, settings: Settings, job: ProcessingJob, prepared: PreparedWork
) -> dict:
    """Called exclusively inside the queue's fenced completion transaction."""
    data = prepared.data
    if isinstance(data, PublicationPlan):
        if job.job_type != "PUBLISH":
            raise DomainError("RESULT_MISMATCH", "Expected publication job", 409)
        return apply_publication(session, settings, job, data)
    if isinstance(data, SegmentPlan):
        if job.job_type != "SEGMENT" or job.raw_file_id != data.file_id:
            raise DomainError("RESULT_MISMATCH", "Prepared result does not match job", 409)
        if (
            data.parameters != job.payload["config"]
            or data.start != job.payload["start"]
            or data.count
            != (job.payload["end"] - job.payload["start"] - data.parameters["window_samples"])
            // data.parameters["stride_samples"]
            + 1
        ):
            raise DomainError(
                "RESULT_MISMATCH", "Prepared intervals do not match job parameters", 409
            )
        return persist_segments(session, settings, data, job.id)
    if isinstance(data, ScanPlan):
        if job.job_type != "SCAN":
            raise DomainError("RESULT_MISMATCH", "Prepared result does not match job", 409)
        service = IngestionService(settings, None)
        report = ScanReport(skipped=len(data.incomplete), errors=list(data.errors))
        operations = {item.relative_path: ("source", item) for item in data.inspections}
        operations.update({item["path"]: ("error", item["code"]) for item in data.errors})
        operations.update({path: ("error", "INCOMPLETE") for path in data.incomplete})
        operations.update({path: ("missing", "SOURCE_MISSING") for path in data.missing})
        for path, (kind, value) in sorted(operations.items()):
            directory = job.payload["directory"]
            if directory != "." and not path.startswith(directory + "/"):
                raise DomainError(
                    "RESULT_MISMATCH", "Scan result is outside requested directory", 409
                )
            if kind == "source":
                file_id, created = service.register_in_session(session, value)
                report.registered += int(created)
                report.unchanged += int(not created)
                report.files.append({"path": path, "id": str(file_id), "sha256": value.sha256})
            else:
                report.invalidated += service.invalidate_in_session(
                    session, path, value, missing_only=kind == "missing"
                )
        return asdict(report)
    if job.job_type not in {"QUALITY", "INFER"} or job.segment_id != data.segment_id:
        raise DomainError("RESULT_MISMATCH", "Prepared result does not match job", 409)
    lock_path(session, settings.data_root_key, data.observed.relative_path)
    raw = session.get(RawFile, data.file_id, with_for_update=True)
    if raw is None or raw.status != "ACTIVE":
        raise DomainError("SOURCE_INACTIVE", "Source was invalidated before quality commit", 409)
    if raw.sha256 != data.observed.sha256 or raw.metadata_hash != data.observed.metadata_hash:
        raise DomainError(
            "SOURCE_CHANGED", "Prepared quality source does not match asset identity", 409
        )
    verify_observation(ControlledRoot(settings.data_root), data.observed)
    if isinstance(data, InferencePlan):
        if job.model_version_id != data.model_version_id or job.job_type != "INFER":
            raise DomainError("RESULT_MISMATCH", "Prediction model does not match job", 409)
        semantic = {key: value for key, value in data.prediction.items() if key != "inference_ms"}
        digest = content_key(semantic)
        session.execute(
            insert(Prediction)
            .values(
                id=uuid.uuid4(),
                segment_id=data.segment_id,
                model_version_id=data.model_version_id,
                **data.prediction,
                digest=digest,
                created_by_job_id=job.id,
            )
            .on_conflict_do_nothing(constraint="uq_prediction_identity")
        )
        prediction = session.scalar(
            select(Prediction).where(
                Prediction.segment_id == data.segment_id,
                Prediction.model_version_id == data.model_version_id,
            )
        )
        if prediction.digest != digest:
            raise DomainError(
                "RESULT_CONFLICT", "Prediction differs for the same model version", 409
            )
        ensure_review(session, data.segment_id)
        review = session.scalar(select(ReviewTask).where(ReviewTask.segment_id == data.segment_id))
        return {
            "prediction_id": str(prediction.id),
            "review_id": str(review.id),
            "segment_id": str(data.segment_id),
            "model_version_id": str(data.model_version_id),
            **data.prediction,
        }
    if job.job_type != "QUALITY":
        raise DomainError("RESULT_MISMATCH", "Expected quality result", 409)
    digest = content_key(data.metrics)
    session.execute(
        insert(SegmentQuality)
        .values(
            id=uuid.uuid4(),
            segment_id=data.segment_id,
            algorithm_version=QUALITY_VERSION,
            metrics=data.metrics,
            digest=digest,
            created_by_job_id=job.id,
        )
        .on_conflict_do_nothing(constraint="uq_segment_quality")
    )
    quality = session.scalar(
        select(SegmentQuality).where(
            SegmentQuality.segment_id == data.segment_id,
            SegmentQuality.algorithm_version == QUALITY_VERSION,
        )
    )
    if quality.digest != digest:
        raise DomainError(
            "RESULT_CONFLICT", "Quality result differs for the same algorithm version", 409
        )
    segment = session.get(SignalSegment, data.segment_id)
    segment.quality = data.metrics
    return {
        "quality_id": str(quality.id),
        "segment_id": str(data.segment_id),
        "algorithm_version": QUALITY_VERSION,
        "metrics": data.metrics,
    }
