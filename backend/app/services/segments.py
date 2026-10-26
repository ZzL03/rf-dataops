import uuid
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings
from app.core.errors import DomainError
from app.core.identity import PIPELINE_VERSION, READER_VERSION, content_key
from app.models import ProcessingConfig, RawFile, SignalSegment
from app.schemas.iq import SegmentConfig
from app.services.ingestion import Inspection, inspect_file, lock_path, verify_observation
from app.services.storage import ControlledRoot


@dataclass(frozen=True)
class SegmentPlan:
    file_id: uuid.UUID
    observed: Inspection
    parameters: dict
    start: int
    count: int

    @property
    def config_hash(self) -> str:
        return content_key(self.parameters)


def range_count(
    total: int, config: SegmentConfig, start: int, end: int | None, maximum: int
) -> tuple[int, int]:
    end = total if end is None else end
    if start < 0 or end > total or end - start < config.window_samples:
        raise DomainError(
            "INVALID_INTERVAL", "Index range must contain a complete window inside the file"
        )
    count = (end - start - config.window_samples) // config.stride + 1
    if count > maximum:
        raise DomainError("SEGMENT_LIMIT", "Narrow start/end: at most 1,000 windows per job")
    return end, count


def prepare_segments(
    settings: Settings,
    factory: sessionmaker[Session],
    file_id: uuid.UUID,
    config: SegmentConfig,
    start: int = 0,
    end: int | None = None,
) -> SegmentPlan:
    """Full source verification outside a transaction; produces no business rows."""
    with factory() as session:
        raw = session.get(RawFile, file_id)
        if raw is None or raw.root_key != settings.data_root_key:
            raise DomainError("NOT_FOUND", "File not found", 404)
        if raw.status != "ACTIVE":
            raise DomainError("SOURCE_INACTIVE", "Source is not active; scan it again", 409)
    _, count = range_count(raw.sample_count, config, start, end, settings.max_segments_per_call)
    observed = inspect_file(ControlledRoot(settings.data_root), raw.relative_path)
    if observed.sha256 != raw.sha256 or observed.metadata_hash != raw.metadata_hash:
        raise DomainError(
            "SOURCE_CHANGED", "Source content or metadata changed; scan it again", 409
        )
    return SegmentPlan(file_id, observed, config.normalized(), start, count)


def persist_segments(
    session: Session, settings: Settings, plan: SegmentPlan, job_id: uuid.UUID | None = None
) -> dict:
    """Caller owns the transaction, including the execution fence."""
    lock_path(session, settings.data_root_key, plan.observed.relative_path)
    current = session.get(RawFile, plan.file_id, with_for_update=True)
    if current is None or current.status != "ACTIVE":
        raise DomainError("SOURCE_INACTIVE", "Source was invalidated before indexing", 409)
    if (
        current.sha256 != plan.observed.sha256
        or current.metadata_hash != plan.observed.metadata_hash
    ):
        raise DomainError("SOURCE_CHANGED", "Prepared source does not match asset identity", 409)
    verify_observation(ControlledRoot(settings.data_root), plan.observed)
    session.execute(
        insert(ProcessingConfig)
        .values(
            id=uuid.uuid4(),
            config_hash=plan.config_hash,
            parameters=plan.parameters,
            reader_version=READER_VERSION,
            pipeline_version=PIPELINE_VERSION,
        )
        .on_conflict_do_nothing(index_elements=["config_hash"])
    )
    config_id = session.scalar(
        select(ProcessingConfig.id).where(ProcessingConfig.config_hash == plan.config_hash)
    )
    values = [
        {
            "id": uuid.uuid4(),
            "raw_file_id": plan.file_id,
            "config_id": config_id,
            "start_sample": plan.start + i * plan.parameters["stride_samples"],
            "sample_count": plan.parameters["window_samples"],
            "created_by_job_id": job_id,
        }
        for i in range(plan.count)
    ]
    created = len(
        list(
            session.scalars(
                insert(SignalSegment)
                .values(values)
                .on_conflict_do_nothing(constraint="uq_segment_identity")
                .returning(SignalSegment.id)
            )
        )
    )
    ids = list(
        session.scalars(
            select(SignalSegment.id)
            .where(
                SignalSegment.raw_file_id == plan.file_id,
                SignalSegment.config_id == config_id,
                SignalSegment.start_sample.in_([row["start_sample"] for row in values]),
            )
            .order_by(SignalSegment.start_sample)
        )
    )
    total = session.scalar(
        select(func.count())
        .select_from(SignalSegment)
        .where(SignalSegment.raw_file_id == plan.file_id, SignalSegment.config_id == config_id)
    )
    return {
        "file_id": str(plan.file_id),
        "config_id": str(config_id),
        "config_hash": plan.config_hash,
        "requested": plan.count,
        "created": created,
        "existing": plan.count - created,
        "total": total,
        "segment_ids": [str(value) for value in ids],
    }


def index_segments(
    settings: Settings,
    factory: sessionmaker[Session],
    file_id: uuid.UUID,
    config: SegmentConfig,
    start: int = 0,
    end: int | None = None,
) -> dict:
    """Trusted phase-two service; workers use prepare/persist within their own fence."""
    plan = prepare_segments(settings, factory, file_id, config, start, end)
    with factory.begin() as session:
        return persist_segments(session, settings, plan)
