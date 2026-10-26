import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy import func, select

from app.api.assets import DbSession, Limit, Offset
from app.api.auth import current_user, require_publisher
from app.api.jobs import RequestKey
from app.api.labeling import columns
from app.core.errors import DomainError
from app.models import Dataset, DatasetVersion, DatasetVersionItem, User
from app.schemas.datasets import DatasetRequest, PublicationRequest, SelectionRequest
from app.schemas.jobs import JobOut, PublishJobRequest
from app.services.datasets import create_dataset, find_dataset, preview_selection
from app.services.job_queue import JobQueue

router = APIRouter(prefix="/api/v1", tags=["datasets"], dependencies=[Depends(current_user)])
Publisher = Annotated[User, Depends(require_publisher)]


def find_version(session, version_id, root_key):
    row = session.scalar(
        select(DatasetVersion)
        .join(Dataset)
        .where(
            DatasetVersion.id == version_id,
            Dataset.root_key == root_key,
            DatasetVersion.sealed.is_(True),
        )
    )
    if row is None:
        raise DomainError("NOT_FOUND", "Dataset version not found", 404)
    return row


def version_out(session, version):
    labels = dict(
        session.execute(
            select(DatasetVersionItem.frozen_label, func.count())
            .where(DatasetVersionItem.dataset_version_id == version.id)
            .group_by(DatasetVersionItem.frozen_label)
        ).all()
    )
    sources = dict(
        session.execute(
            select(DatasetVersionItem.label_source, func.count())
            .where(DatasetVersionItem.dataset_version_id == version.id)
            .group_by(DatasetVersionItem.label_source)
        ).all()
    )
    return {
        key: value
        for key, value in columns(version).items()
        if key not in {"manifest_text", "creation_xid"}
    } | {"label_distribution": labels, "source_counts": sources}


@router.post("/datasets", status_code=201)
def create(body: DatasetRequest, request: Request, user: Publisher):
    return columns(
        create_dataset(
            request.app.state.session_factory, request.app.state.settings.data_root_key, body, user
        )
    )


@router.get("/datasets")
def datasets(request: Request, session: DbSession, limit: Limit = 20, offset: Offset = 0):
    count = (
        select(func.count())
        .where(DatasetVersion.dataset_id == Dataset.id, DatasetVersion.sealed.is_(True))
        .scalar_subquery()
    )
    query = select(Dataset, count).where(
        Dataset.root_key == request.app.state.settings.data_root_key
    )
    rows = session.execute(
        query.order_by(Dataset.created_at.desc(), Dataset.id).limit(limit).offset(offset)
    )
    return {
        "items": [{**columns(row), "version_count": total} for row, total in rows],
        "total": session.scalar(select(func.count()).select_from(query.subquery())),
        "limit": limit,
        "offset": offset,
    }


@router.get("/datasets/{dataset_id}")
def detail(dataset_id: uuid.UUID, request: Request, session: DbSession):
    return columns(find_dataset(session, dataset_id, request.app.state.settings.data_root_key))


@router.post("/datasets/{dataset_id}/selection-preview", dependencies=[Depends(require_publisher)])
def preview(dataset_id: uuid.UUID, body: SelectionRequest, request: Request, session: DbSession):
    dataset = find_dataset(session, dataset_id, request.app.state.settings.data_root_key)
    return preview_selection(session, dataset, body)


@router.post("/datasets/{dataset_id}/versions", status_code=202, response_model=JobOut)
def publish(
    dataset_id: uuid.UUID,
    body: PublicationRequest,
    request: Request,
    response: Response,
    request_key: RequestKey,
    user: Publisher,
):
    job = JobQueue(request.app.state.settings, request.app.state.session_factory).submit(
        PublishJobRequest(dataset_id=dataset_id, publication=body), request_key, f"user:{user.id}"
    )
    response.headers["Location"] = f"/api/v1/jobs/{job.id}"
    return job


@router.get("/datasets/{dataset_id}/versions")
def versions(
    dataset_id: uuid.UUID,
    request: Request,
    session: DbSession,
    limit: Limit = 20,
    offset: Offset = 0,
):
    find_dataset(session, dataset_id, request.app.state.settings.data_root_key)
    query = select(DatasetVersion).where(
        DatasetVersion.dataset_id == dataset_id, DatasetVersion.sealed.is_(True)
    )
    rows = session.scalars(
        query.order_by(DatasetVersion.created_at.desc(), DatasetVersion.id)
        .limit(limit)
        .offset(offset)
    )
    return {
        "items": [version_out(session, row) for row in rows],
        "total": session.scalar(select(func.count()).select_from(query.subquery())),
        "limit": limit,
        "offset": offset,
    }


@router.get("/dataset-versions/{version_id}")
def version_detail(version_id: uuid.UUID, request: Request, session: DbSession):
    return version_out(
        session, find_version(session, version_id, request.app.state.settings.data_root_key)
    )


@router.get("/dataset-versions/{version_id}/items")
def items(
    version_id: uuid.UUID,
    request: Request,
    session: DbSession,
    limit: Limit = 20,
    offset: Offset = 0,
):
    version = find_version(session, version_id, request.app.state.settings.data_root_key)
    rows = session.scalars(
        select(DatasetVersionItem)
        .where(DatasetVersionItem.dataset_version_id == version.id)
        .order_by(DatasetVersionItem.segment_id)
        .limit(limit)
        .offset(offset)
    )
    return {
        "items": [row.snapshot for row in rows],
        "total": version.item_count,
        "limit": limit,
        "offset": offset,
    }


@router.get("/dataset-versions/{version_id}/manifest")
def manifest(version_id: uuid.UUID, request: Request, session: DbSession):
    version = find_version(session, version_id, request.app.state.settings.data_root_key)
    return Response(
        content=version.manifest_text.encode("utf-8"),
        media_type="application/json",
        headers={
            "Content-Disposition": (
                f'attachment; filename="dataset-{version.id}-{version.version}.json"'
            ),
            "ETag": f'"{version.manifest_sha256}"',
            "X-Manifest-SHA256": version.manifest_sha256,
            "Cache-Control": "private, no-cache",
        },
    )
