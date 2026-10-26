import uuid
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Request
from sqlalchemy import func, select

from app.api.assets import DbSession, Limit, Offset, get_segment
from app.api.auth import current_user, require_admin, require_reviewer
from app.core.errors import DomainError
from app.models import (
    Annotation,
    ModelVersion,
    Prediction,
    RawFile,
    ReviewTask,
    SignalSegment,
    User,
)
from app.schemas.labeling import AnnotationRequest, ModelRequest
from app.services.labeling import annotate, find_review, register_model

router = APIRouter(prefix="/api/v1", tags=["labeling"], dependencies=[Depends(current_user)])


def columns(row):
    return {column.name: getattr(row, column.name) for column in row.__table__.columns}


def model_out(model):
    # Local artifact path stays server-side.
    return {
        **{key: value for key, value in columns(model).items() if key != "config"},
        "config": {key: value for key, value in model.config.items() if key != "artifact_path"},
        "is_mock": model.adapter == "mock",
    }


@router.get("/models")
def models(session: DbSession, limit: Limit = 20, offset: Offset = 0):
    rows = session.scalars(
        select(ModelVersion)
        .order_by(ModelVersion.created_at, ModelVersion.id)
        .limit(limit)
        .offset(offset)
    )
    return {
        "items": [model_out(row) for row in rows],
        "total": session.scalar(select(func.count()).select_from(ModelVersion)),
        "limit": limit,
        "offset": offset,
    }


@router.post("/models", status_code=201, dependencies=[Depends(require_admin)])
def create_model(body: ModelRequest, request: Request):
    return model_out(register_model(request.app.state.session_factory, body.name, body.version))


@router.get("/segments/{segment_id}/predictions")
def predictions(
    segment_id: uuid.UUID,
    request: Request,
    session: DbSession,
    limit: Limit = 20,
    offset: Offset = 0,
):
    get_segment(session, segment_id, request.app.state.settings.data_root_key)
    query = (
        select(Prediction, ModelVersion)
        .join(ModelVersion)
        .where(Prediction.segment_id == segment_id)
    )
    rows = session.execute(
        query.order_by(Prediction.created_at, Prediction.id).limit(limit).offset(offset)
    )
    return {
        "items": [{**columns(prediction), "model": model_out(model)} for prediction, model in rows],
        "total": session.scalar(select(func.count()).select_from(query.subquery())),
        "limit": limit,
        "offset": offset,
    }


def review_out(session, review):
    annotation = (
        session.get(Annotation, review.current_annotation_id)
        if review.current_annotation_id
        else None
    )
    return {**columns(review), "current_annotation": columns(annotation) if annotation else None}


@router.get("/reviews")
def reviews(
    request: Request,
    session: DbSession,
    limit: Limit = 20,
    offset: Offset = 0,
    status: Literal["PENDING", "REVIEWED", "UNKNOWN"] | None = None,
    max_confidence: float | None = None,
):
    if max_confidence is not None and not 0 <= max_confidence <= 1:
        raise DomainError("INVALID_CONFIDENCE", "Confidence must be between 0 and 1")
    # Minimum available model confidence is a review-ordering hint, never a human label.
    confidence = (
        select(func.min(Prediction.confidence))
        .where(Prediction.segment_id == ReviewTask.segment_id)
        .correlate(ReviewTask)
        .scalar_subquery()
    )
    query = (
        select(ReviewTask, confidence.label("confidence"))
        .join(SignalSegment)
        .join(RawFile)
        .where(RawFile.root_key == request.app.state.settings.data_root_key)
    )
    if status:
        query = query.where(ReviewTask.status == status)
    if max_confidence is not None:
        query = query.where(confidence <= max_confidence)
    total = session.scalar(select(func.count()).select_from(query.subquery()))
    rows = session.execute(
        query.order_by(confidence.asc().nullsfirst(), ReviewTask.created_at, ReviewTask.id)
        .limit(limit)
        .offset(offset)
    )
    return {
        "items": [
            {**review_out(session, review), "min_model_confidence": score} for review, score in rows
        ],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


@router.get("/reviews/{review_id}")
def review_detail(review_id: uuid.UUID, request: Request, session: DbSession):
    return review_out(
        session, find_review(session, review_id, request.app.state.settings.data_root_key)
    )


@router.get("/reviews/{review_id}/annotations")
def history(
    review_id: uuid.UUID,
    request: Request,
    session: DbSession,
    limit: Limit = 20,
    offset: Offset = 0,
):
    find_review(session, review_id, request.app.state.settings.data_root_key)
    query = (
        select(Annotation, User.username)
        .join(User, Annotation.reviewer_id == User.id)
        .where(Annotation.review_task_id == review_id)
    )
    rows = session.execute(query.order_by(Annotation.version.desc()).limit(limit).offset(offset))
    return {
        "items": [
            {**columns(annotation), "reviewer_username": username} for annotation, username in rows
        ],
        "total": session.scalar(select(func.count()).select_from(query.subquery())),
        "limit": limit,
        "offset": offset,
    }


@router.post("/reviews/{review_id}/annotations", status_code=201)
def annotation(
    review_id: uuid.UUID,
    body: AnnotationRequest,
    request: Request,
    user: Annotated[User, Depends(require_reviewer)],
):
    return columns(
        annotate(
            request.app.state.session_factory,
            request.app.state.settings.data_root_key,
            review_id,
            body,
            user,
        )
    )
