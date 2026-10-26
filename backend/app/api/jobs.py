import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Header, Request, Response
from sqlalchemy import func, select

from app.api.assets import DbSession, Limit, Offset
from app.api.auth import CurrentUser, current_user, require_admin
from app.core.errors import DomainError
from app.models import JobEvent, ProcessingJob, SegmentQuality
from app.schemas.assets import Page
from app.schemas.jobs import (
    JobEventOut,
    JobOut,
    JobStatus,
    JobSubmission,
    JobType,
    RerunRequest,
    ScanJobRequest,
)
from app.services.job_queue import JobQueue

router = APIRouter(prefix="/api/v1", tags=["jobs"], dependencies=[Depends(current_user)])
RequestKey = Annotated[str, Header(alias="Idempotency-Key", min_length=8, max_length=128)]


def queue_for(request: Request) -> JobQueue:
    return JobQueue(request.app.state.settings, request.app.state.session_factory)


def find_job(session, job_id, root_key):
    job = session.get(ProcessingJob, job_id)
    if job is None or job.root_key != root_key:
        raise DomainError("NOT_FOUND", "Job not found", 404)
    return job


@router.post("/jobs", response_model=JobOut, status_code=202)
def create_job(
    body: JobSubmission,
    request: Request,
    response: Response,
    request_key: RequestKey,
    user: CurrentUser,
) -> ProcessingJob:
    if user.role != "ADMIN" and not (body.job_type == "PUBLISH" and user.role == "RESEARCHER"):
        raise DomainError("FORBIDDEN", "This role cannot submit this job type", 403)
    job = queue_for(request).submit(body, request_key, f"user:{user.id}")
    response.headers["Location"] = f"/api/v1/jobs/{job.id}"
    return job


@router.post(
    "/scans", response_model=JobOut, status_code=202, dependencies=[Depends(require_admin)]
)
def create_scan(
    body: ScanJobRequest,
    request: Request,
    response: Response,
    request_key: RequestKey,
    user: CurrentUser,
) -> ProcessingJob:
    return create_job(body, request, response, request_key, user)


@router.get("/jobs", response_model=Page[JobOut])
def jobs(
    request: Request,
    session: DbSession,
    limit: Limit = 20,
    offset: Offset = 0,
    status: JobStatus | None = None,
    job_type: JobType | None = None,
) -> dict:
    query = select(ProcessingJob).where(
        ProcessingJob.root_key == request.app.state.settings.data_root_key
    )
    if status:
        query = query.where(ProcessingJob.status == status)
    if job_type:
        query = query.where(ProcessingJob.job_type == job_type)
    rows = session.scalars(
        query.order_by(ProcessingJob.created_at.desc(), ProcessingJob.id)
        .limit(limit)
        .offset(offset)
    )
    total = session.scalar(select(func.count()).select_from(query.subquery()))
    return {"items": list(rows), "total": total, "limit": limit, "offset": offset}


@router.get("/jobs/{job_id}", response_model=JobOut)
def job_detail(job_id: uuid.UUID, request: Request, session: DbSession) -> ProcessingJob:
    return find_job(session, job_id, request.app.state.settings.data_root_key)


@router.get("/jobs/{job_id}/events", response_model=Page[JobEventOut])
def events(
    job_id: uuid.UUID, request: Request, session: DbSession, limit: Limit = 20, offset: Offset = 0
) -> dict:
    find_job(session, job_id, request.app.state.settings.data_root_key)
    query = select(JobEvent).where(JobEvent.job_id == job_id)
    rows = session.scalars(
        query.order_by(JobEvent.created_at, JobEvent.id).limit(limit).offset(offset)
    )
    total = session.scalar(select(func.count()).select_from(query.subquery()))
    return {"items": list(rows), "total": total, "limit": limit, "offset": offset}


@router.post(
    "/jobs/{job_id}/reruns",
    response_model=JobOut,
    status_code=202,
)
def rerun_job(
    job_id: uuid.UUID,
    body: RerunRequest,
    request: Request,
    response: Response,
    request_key: RequestKey,
    user: CurrentUser,
) -> ProcessingJob:
    with request.app.state.session_factory() as session:
        prior = find_job(session, job_id, request.app.state.settings.data_root_key)
        if user.role != "ADMIN" and not (prior.job_type == "PUBLISH" and user.role == "RESEARCHER"):
            raise DomainError("FORBIDDEN", "This role cannot rerun this job type", 403)
    job = queue_for(request).rerun(job_id, request_key, body.max_attempts, f"user:{user.id}")
    response.headers["Location"] = f"/api/v1/jobs/{job.id}"
    return job


@router.get("/segments/{segment_id}/quality")
def qualities(segment_id: uuid.UUID, request: Request, session: DbSession) -> dict:
    from app.api.assets import get_segment

    get_segment(session, segment_id, request.app.state.settings.data_root_key)
    rows = session.scalars(
        select(SegmentQuality)
        .where(SegmentQuality.segment_id == segment_id)
        .order_by(SegmentQuality.created_at)
    )
    return {
        "items": [
            {
                "id": row.id,
                "algorithm_version": row.algorithm_version,
                "metrics": row.metrics,
                "created_by_job_id": row.created_by_job_id,
                "created_at": row.created_at,
            }
            for row in rows
        ]
    }
