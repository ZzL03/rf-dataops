import os
import uuid
from collections.abc import Iterator
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.auth import current_user
from app.core.errors import DomainError
from app.core.identity import READER_VERSION, content_key
from app.models import FileObservation, ProcessingConfig, RawFile, SignalSegment
from app.schemas.assets import (
    ConfigOut,
    FileOut,
    ObservationOut,
    Page,
    PreviewOut,
    SegmentDetail,
    SegmentOut,
)
from app.services.ingestion import ensure_complete, read_metadata
from app.services.storage import ControlledRoot, signature
from app.signal_processing.preview import build_preview
from app.signal_processing.readers import open_reader

router = APIRouter(prefix="/api/v1", tags=["assets"], dependencies=[Depends(current_user)])
Limit = Annotated[int, Query(ge=1, le=100)]
Offset = Annotated[int, Query(ge=0)]


def get_session(request: Request) -> Iterator[Session]:
    with request.app.state.session_factory() as session:
        yield session


DbSession = Annotated[Session, Depends(get_session)]


def get_file(session: Session, file_id: uuid.UUID, root_key: str) -> RawFile:
    raw = session.get(RawFile, file_id)
    if raw is None or raw.root_key != root_key:
        raise DomainError("NOT_FOUND", "File not found", 404)
    return raw


def get_segment(session: Session, segment_id: uuid.UUID, root_key: str) -> SignalSegment:
    segment = session.scalar(
        select(SignalSegment)
        .join(RawFile)
        .where(SignalSegment.id == segment_id, RawFile.root_key == root_key)
    )
    if segment is None:
        raise DomainError("NOT_FOUND", "Segment not found", 404)
    return segment


@router.get("/files", response_model=Page[FileOut])
def files(
    request: Request,
    session: DbSession,
    limit: Limit = 20,
    offset: Offset = 0,
    status: Literal["ACTIVE", "STALE", "INVALID"] | None = None,
    format: Literal["BIN", "HDF5"] | None = None,
    q: Annotated[str | None, Query(max_length=512)] = None,
) -> dict:
    filters = [RawFile.root_key == request.app.state.settings.data_root_key]
    if status:
        filters.append(RawFile.status == status)
    if format:
        filters.append(RawFile.format == format)
    if q:
        filters.append(RawFile.relative_path.contains(q, autoescape=True))
    count = select(func.count()).where(SignalSegment.raw_file_id == RawFile.id).scalar_subquery()
    rows = session.execute(
        select(RawFile, count)
        .where(*filters)
        .order_by(RawFile.created_at, RawFile.id)
        .limit(limit)
        .offset(offset)
    )
    items = [FileOut.model_validate(raw).model_copy(update={"segment_count": n}) for raw, n in rows]
    total = session.scalar(select(func.count()).select_from(RawFile).where(*filters))
    return {"items": items, "total": total, "limit": limit, "offset": offset}


@router.get("/files/{file_id}", response_model=FileOut)
def file_detail(file_id: uuid.UUID, request: Request, session: DbSession) -> FileOut:
    raw = get_file(session, file_id, request.app.state.settings.data_root_key)
    count = session.scalar(
        select(func.count()).select_from(SignalSegment).where(SignalSegment.raw_file_id == file_id)
    )
    return FileOut.model_validate(raw).model_copy(update={"segment_count": count})


@router.get("/files/{file_id}/observations", response_model=Page[ObservationOut])
def observations(
    file_id: uuid.UUID, request: Request, session: DbSession, limit: Limit = 20, offset: Offset = 0
) -> dict:
    get_file(session, file_id, request.app.state.settings.data_root_key)
    query = select(FileObservation).where(FileObservation.raw_file_id == file_id)
    rows = session.scalars(
        query.order_by(FileObservation.created_at, FileObservation.id).limit(limit).offset(offset)
    )
    total = session.scalar(select(func.count()).select_from(query.subquery()))
    return {"items": list(rows), "total": total, "limit": limit, "offset": offset}


@router.get("/segments", response_model=Page[SegmentOut])
def segments(
    request: Request,
    session: DbSession,
    limit: Limit = 20,
    offset: Offset = 0,
    raw_file_id: uuid.UUID | None = None,
    config_id: uuid.UUID | None = None,
) -> dict:
    query = (
        select(SignalSegment)
        .join(RawFile)
        .where(RawFile.root_key == request.app.state.settings.data_root_key)
    )
    if raw_file_id is not None:
        query = query.where(SignalSegment.raw_file_id == raw_file_id)
    if config_id is not None:
        query = query.where(SignalSegment.config_id == config_id)
    rows = session.scalars(
        query.order_by(SignalSegment.created_at, SignalSegment.id).limit(limit).offset(offset)
    )
    total = session.scalar(select(func.count()).select_from(query.subquery()))
    return {"items": list(rows), "total": total, "limit": limit, "offset": offset}


@router.get("/segments/{segment_id}", response_model=SegmentDetail)
def segment_detail(segment_id: uuid.UUID, request: Request, session: DbSession) -> SegmentDetail:
    segment = get_segment(session, segment_id, request.app.state.settings.data_root_key)
    config = session.get(ProcessingConfig, segment.config_id)
    return SegmentDetail(
        **SegmentOut.model_validate(segment).model_dump(),
        processing_config=ConfigOut.model_validate(config),
    )


@router.get(
    "/segments/{segment_id}/preview",
    response_model=PreviewOut,
    responses={409: {"description": "Source integrity check failed"}},
)
def preview(
    segment_id: uuid.UUID,
    request: Request,
    session: DbSession,
    max_points: Annotated[int, Query(ge=32, le=2048)] = 512,
) -> dict:
    segment = get_segment(session, segment_id, request.app.state.settings.data_root_key)
    raw = get_file(session, segment.raw_file_id, request.app.state.settings.data_root_key)
    if raw.status != "ACTIVE":
        raise DomainError("SOURCE_INACTIVE", "Source is not active; scan it again", 409)
    # Copy values and release the read transaction before filesystem I/O or FFT.
    path, expected = raw.relative_path, raw.stat_identity
    metadata_hash = raw.metadata_hash
    start, count = segment.start_sample, segment.sample_count
    config = session.get(ProcessingConfig, segment.config_id)
    if config.reader_version != READER_VERSION:
        raise DomainError("READER_VERSION_MISMATCH", "This reader version is unavailable", 409)
    session.rollback()
    root = ControlledRoot(request.app.state.settings.data_root)
    ensure_complete(root, path)
    metadata = read_metadata(root, path)
    if content_key(metadata.model_dump(mode="json")) != metadata_hash:
        raise DomainError("SOURCE_CHANGED", "IQ interpretation changed; scan it again", 409)
    with root.open_read(path) as stream:
        local_identity = signature(os.fstat(stream.fileno()))
        # Device/inode/ctime are not portable across Windows and a Linux bind mount.
        # This cheap guard is intentionally weaker than a full content verification.
        if any(local_identity[key] != expected[key] for key in ("st_size", "st_mtime_ns")):
            raise DomainError("SOURCE_CHANGED", "Source identity changed; scan it again", 409)
        with open_reader(stream, metadata) as reader:
            iq = reader.read(start, count)
        if signature(os.fstat(stream.fileno())) != local_identity:
            raise DomainError("SOURCE_CHANGED", "Source changed while reading preview", 409)
    # Reopen the path once to detect replacement of a file that was still held by our FD.
    with root.open_read(path) as stream:
        if signature(os.fstat(stream.fileno())) != local_identity:
            raise DomainError("SOURCE_CHANGED", "Source path changed while reading preview", 409)
    return {
        "segment_id": segment_id,
        "start_sample": start,
        "sample_count": count,
        "sample_rate_hz": metadata.sample_rate_hz,
        "center_frequency_hz": metadata.center_frequency_hz,
        "reader_version": READER_VERSION,
        **build_preview(iq, start, metadata.sample_rate_hz, max_points),
    }
