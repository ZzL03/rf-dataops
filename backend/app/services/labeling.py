import uuid

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert

from app.core.errors import DomainError
from app.models import Annotation, ModelVersion, Prediction, RawFile, ReviewTask, SignalSegment
from app.schemas.labeling import LABELS
from app.signal_processing.models import PREPROCESS_VERSION


def register_model(factory, name, version, adapter="mock", config=None, artifact_sha256=None):
    from app.services.job_queue import advisory_lock

    config = config or {"labels": list(LABELS), "preprocessing": PREPROCESS_VERSION}
    with factory.begin() as session:
        advisory_lock(session, "model:" + name + ":" + version)
        prior = session.scalar(
            select(ModelVersion).where(ModelVersion.name == name, ModelVersion.version == version)
        )
        if prior:
            if (prior.adapter, prior.config, prior.artifact_sha256) != (
                adapter,
                config,
                artifact_sha256,
            ):
                raise DomainError(
                    "MODEL_VERSION_CONFLICT",
                    "Register changed model content with a new version",
                    409,
                )
            return prior
        model = ModelVersion(
            name=name,
            version=version,
            adapter=adapter,
            config=config,
            artifact_sha256=artifact_sha256,
        )
        session.add(model)
        session.flush()
        return model


def find_review(session, review_id, root_key):
    review = session.scalar(
        select(ReviewTask)
        .join(SignalSegment)
        .join(RawFile)
        .where(ReviewTask.id == review_id, RawFile.root_key == root_key)
    )
    if review is None:
        raise DomainError("NOT_FOUND", "Review not found", 404)
    return review


def annotate(factory, root_key, review_id, body, user):
    with factory.begin() as session:
        review = find_review(session, review_id, root_key)
        prediction = session.get(Prediction, body.prediction_id) if body.prediction_id else None
        if body.prediction_id and (
            prediction is None or prediction.segment_id != review.segment_id
        ):
            raise DomainError(
                "PREDICTION_MISMATCH", "Prediction must belong to this review segment", 409
            )
        if body.action == "CONFIRM" and prediction.label != body.label:
            raise DomainError(
                "LABEL_MISMATCH", "Confirmation must match the referenced prediction", 409
            )
        annotation_id = uuid.uuid4()
        version = session.scalar(
            update(ReviewTask)
            .where(ReviewTask.id == review_id, ReviewTask.label_version == body.expected_version)
            .values(
                label_version=ReviewTask.label_version + 1,
                current_annotation_id=annotation_id,
                status="UNKNOWN" if body.label == "UNKNOWN" else "REVIEWED",
                updated_at=func.clock_timestamp(),
            )
            .returning(ReviewTask.label_version)
            .execution_options(synchronize_session=False)
        )
        if version is None:
            session.expire_all()
            current = session.get(ReviewTask, review_id)
            raise DomainError(
                "REVIEW_VERSION_CONFLICT",
                f"Review changed; current version is {current.label_version}. "
                "Reload before deciding.",
                409,
            )
        record = Annotation(
            id=annotation_id,
            review_task_id=review_id,
            version=version,
            label=body.label,
            action=body.action,
            comment=body.comment,
            reviewer_id=user.id,
            prediction_id=body.prediction_id,
        )
        session.add(record)
        session.flush()
        return record


def ensure_review(session, segment_id):
    session.execute(
        insert(ReviewTask)
        .values(id=uuid.uuid4(), segment_id=segment_id)
        .on_conflict_do_nothing(constraint="uq_review_segment")
    )
