import concurrent.futures
import sys
import uuid
from dataclasses import replace
from datetime import timedelta

import numpy as np
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, func, select, text, update
from sqlalchemy.exc import IntegrityError

from app.core.errors import DomainError
from app.db.session import build_session_factory
from app.main import create_app
from app.models import (
    Annotation,
    AuthSession,
    ModelVersion,
    Prediction,
    ProcessingJob,
    ReviewTask,
    User,
)
from app.schemas.iq import SegmentConfig
from app.schemas.jobs import InferenceJobRequest
from app.schemas.labeling import AnnotationRequest
from app.services.authentication import (
    create_user,
    hash_password,
    issue_session,
    token_hash,
    verify_password,
)
from app.services.ingestion import IngestionService
from app.services.job_queue import JobQueue, StaleExecution
from app.services.job_work import prepare_work
from app.services.labeling import annotate, register_model
from app.services.segments import index_segments
from app.signal_processing.models import MockModel, TorchScriptModel, normalize_iq
from app.workers.runner import Worker
from tests.iq_helpers import make_source


def test_mock_is_deterministic_normalized_and_explicit():
    iq = np.exp(1j * np.arange(128) / 10).astype(np.complex64)
    first, second = MockModel().predict(iq), MockModel().predict(iq.copy())
    assert first.semantic() == second.semantic()
    assert first.is_mock and sum(first.scores.values()) == pytest.approx(1)
    assert first.label == max(first.scores, key=first.scores.get)
    assert normalize_iq(iq).shape == (1, 2, 128)
    assert np.all(normalize_iq(np.zeros(8, dtype=np.complex64)) == 0)
    with pytest.raises(DomainError):
        MockModel().predict(np.array([np.nan + 1j]))


@pytest.fixture
def labeling_env(postgres_database, tmp_path):
    settings, engine = postgres_database
    settings = settings.model_copy(
        update={
            "data_root": tmp_path,
            "model_root": tmp_path,
            "data_root_key": "label_" + uuid.uuid4().hex,
            "retry_base_seconds": 0,
        }
    )
    factory = build_session_factory(engine)
    make_source(tmp_path)
    raw_id = uuid.UUID(IngestionService(settings, factory).scan().files[0]["id"])
    refs = index_segments(settings, factory, raw_id, SegmentConfig(window_samples=128))
    model = register_model(factory, "mock_" + uuid.uuid4().hex, "v1")
    actors = []
    with factory.begin() as session:
        for role in ("ADMIN", "REVIEWER", "REVIEWER", "RESEARCHER"):
            user = User(username="actor_" + uuid.uuid4().hex, password_hash="not-used", role=role)
            session.add(user)
            session.flush()
            actors.append((user, {"Authorization": "Bearer " + issue_session(session, user.id)}))
    return (
        settings,
        engine,
        factory,
        JobQueue(settings, factory),
        list(map(uuid.UUID, refs["segment_ids"])),
        model,
        actors,
    )


def infer(env, segment_index=0, model=None):
    settings, _, factory, queue, segments, default, _ = env
    request = InferenceJobRequest(
        segment_id=segments[segment_index], model_version_id=(model or default).id
    )
    job = queue.submit(request, str(uuid.uuid4()))
    assert Worker(queue).run_once()
    with factory() as session:
        done = session.get(ProcessingJob, job.id)
        assert done.status == "SUCCEEDED", done.last_error
        return (
            done,
            session.get(Prediction, uuid.UUID(done.result["prediction_id"])),
            session.get(ReviewTask, uuid.UUID(done.result["review_id"])),
        )


@pytest.mark.integration
def test_login_hash_session_expiry_logout_and_disabled_user(postgres_database):
    settings, engine = postgres_database
    factory = build_session_factory(engine)
    password = "Private-test-password-28"
    user = create_user(factory, "login_" + uuid.uuid4().hex, password, "REVIEWER")
    assert password not in user.password_hash and verify_password(password, user.password_hash)
    assert hash_password(password) != user.password_hash
    with TestClient(create_app(settings, engine=engine)) as client:
        body = {"username": user.username, "password": password}
        response = client.post("/api/v1/auth/token", json=body)
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        token = response.json()["access_token"]
        headers = {"Authorization": "Bearer " + token}
        with factory() as session:
            stored = session.get(AuthSession, token_hash(token))
            assert stored and stored.token_hash != token
        assert client.get("/api/v1/auth/me", headers=headers).json()["id"] == str(user.id)
        assert client.post("/api/v1/auth/logout", headers=headers).status_code == 204
        assert client.get("/api/v1/auth/me", headers=headers).status_code == 401
        token = client.post("/api/v1/auth/token", json=body).json()["access_token"]
        headers = {"Authorization": "Bearer " + token}
        with factory.begin() as session:
            session.execute(
                update(AuthSession)
                .where(AuthSession.token_hash == token_hash(token))
                .values(expires_at=func.clock_timestamp() - timedelta(seconds=1))
            )
        assert client.get("/api/v1/files", headers=headers).status_code == 401
        token = client.post("/api/v1/auth/token", json=body).json()["access_token"]
        with factory.begin() as session:
            session.get(User, user.id).is_active = False
        assert (
            client.get("/api/v1/files", headers={"Authorization": "Bearer " + token}).status_code
            == 401
        )
        bad = client.post("/api/v1/auth/token", json=body)
        unknown = client.post(
            "/api/v1/auth/token", json={**body, "username": "unknown_" + uuid.uuid4().hex}
        )
        assert bad.status_code == unknown.status_code == 401 and bad.json() == unknown.json()
        response = client.post("/api/v1/auth/token", json={**body, "password": password * 20})
        assert response.status_code == 422 and password not in response.text


@pytest.mark.integration
def test_shared_login_rate_limit(postgres_database):
    settings, engine = postgres_database
    body = {"username": "unknown_" + uuid.uuid4().hex, "password": "bad-password"}
    for index in range(6):
        with TestClient(create_app(settings, engine=engine)) as client:
            response = client.post("/api/v1/auth/token", json=body)
            assert response.status_code == (401 if index < 5 else 429)


@pytest.mark.integration
def test_roles_and_private_reads(labeling_env):
    settings, engine, _, _, _, _, actors = labeling_env
    with TestClient(create_app(settings, engine=engine)) as client:
        for path in ("/api/v1/files", "/api/v1/jobs", "/api/v1/models", "/api/v1/reviews"):
            assert client.get(path).status_code == 401
            for _, headers in actors:
                assert client.get(path, headers=headers).status_code == 200
        for user, headers in actors:
            response = client.post(
                "/api/v1/scans", json={}, headers={**headers, "Idempotency-Key": str(uuid.uuid4())}
            )
            assert response.status_code == (202 if user.role == "ADMIN" else 403)
        assert client.post("/api/v1/models", json={}, headers=actors[-1][1]).status_code == 403


@pytest.mark.integration
def test_inference_replay_creates_one_prediction_and_pending_review(labeling_env):
    settings, _, factory, queue, segments, model, _ = labeling_env
    request = InferenceJobRequest(segment_id=segments[0], model_version_id=model.id)
    job = queue.submit(request, str(uuid.uuid4()))
    assert queue.submit(request, str(uuid.uuid4())).id == job.id
    token = queue.claim("model-worker")
    prepared = prepare_work(settings, factory, token)
    result = queue.complete(token, prepared)
    replay = replace(
        prepared,
        data=replace(prepared.data, prediction={**prepared.data.prediction, "inference_ms": 999}),
    )
    assert queue.complete(token, replay) == result
    with factory() as session:
        assert (
            session.scalar(
                select(func.count())
                .select_from(Prediction)
                .where(Prediction.segment_id == segments[0])
            )
            == 1
        )
        prediction = session.get(Prediction, uuid.UUID(result["prediction_id"]))
        review = session.get(ReviewTask, uuid.UUID(result["review_id"]))
        assert prediction.is_mock and prediction.created_by_job_id == job.id
        assert (
            review.status == "PENDING"
            and review.current_annotation_id is None
            and review.label_version == 0
        )
        assert (
            session.scalar(
                select(func.count())
                .select_from(Annotation)
                .where(Annotation.review_task_id == review.id)
            )
            == 0
        )


@pytest.mark.integration
def test_two_reviewers_conflict_then_append_history(labeling_env):
    settings, engine, factory, _, _, _, actors = labeling_env
    _, prediction, review = infer(labeling_env)
    body = {
        "expected_version": 0,
        "label": prediction.label,
        "action": "CONFIRM",
        "prediction_id": str(prediction.id),
        "comment": "human reviewed",
    }

    def submit(headers):
        with TestClient(create_app(settings, engine=engine)) as client:
            return client.post(
                f"/api/v1/reviews/{review.id}/annotations", json=body, headers=headers
            )

    with concurrent.futures.ThreadPoolExecutor(2) as executor:
        responses = list(executor.map(submit, [actors[1][1], actors[2][1]]))
    assert sorted(response.status_code for response in responses) == [201, 409]
    with TestClient(create_app(settings, engine=engine)) as client:
        client.headers.update(actors[1][1])
        current = client.get(f"/api/v1/reviews/{review.id}").json()
        assert current["label_version"] == 1
        unknown = {
            "expected_version": 1,
            "label": "UNKNOWN",
            "action": "UNKNOWN",
            "comment": "uncertain",
        }
        assert (
            client.post(f"/api/v1/reviews/{review.id}/annotations", json=unknown).status_code == 201
        )
        history = client.get(f"/api/v1/reviews/{review.id}/annotations").json()
        assert history["total"] == 2 and [item["version"] for item in history["items"]] == [2, 1]
        assert history["items"][0]["reviewer_id"] == str(actors[1][0].id)
        assert client.get(f"/api/v1/reviews/{review.id}").json()["status"] == "UNKNOWN"
        assert (
            client.post(
                f"/api/v1/reviews/{review.id}/annotations",
                json={**unknown, "reviewer_id": str(actors[0][0].id)},
            ).status_code
            == 422
        )
        assert (
            client.post(
                f"/api/v1/reviews/{review.id}/annotations",
                json={**unknown, "expected_version": 2},
                headers=actors[-1][1],
            ).status_code
            == 403
        )
    with factory() as session:
        assert session.get(Prediction, prediction.id).label == prediction.label


@pytest.mark.integration
def test_new_model_prediction_never_overwrites_human_label(labeling_env):
    settings, engine, factory, _, _, _, actors = labeling_env
    _, _, review = infer(labeling_env)
    annotation = annotate(
        factory,
        settings.data_root_key,
        review.id,
        AnnotationRequest(expected_version=0, label="BPSK", action="CORRECT"),
        actors[1][0],
    )
    model = register_model(factory, "new_model_" + uuid.uuid4().hex, "v2")
    infer(labeling_env, model=model)
    with factory() as session:
        current = session.get(ReviewTask, review.id)
        assert current.current_annotation_id == annotation.id and current.label_version == 1
        assert session.get(Annotation, annotation.id).label == "BPSK"
        assert (
            session.scalar(
                select(func.count())
                .select_from(Prediction)
                .where(Prediction.segment_id == review.segment_id)
            )
            == 2
        )
    with TestClient(create_app(settings, engine=engine), headers=actors[0][1]) as client:
        other = settings.model_copy(update={"data_root_key": "other"})
        with TestClient(create_app(other, engine=engine), headers=actors[0][1]) as outside:
            assert outside.get(f"/api/v1/reviews/{review.id}").status_code == 404
            assert (
                outside.get(f"/api/v1/segments/{review.segment_id}/predictions").status_code == 404
            )
            assert outside.get("/api/v1/reviews").json()["total"] == 0
        assert client.get("/api/v1/reviews?status=REVIEWED&max_confidence=1").json()["total"] == 1


@pytest.mark.integration
def test_old_inference_execution_cannot_publish(labeling_env):
    settings, _, factory, queue, segments, model, _ = labeling_env
    job = queue.submit(
        InferenceJobRequest(segment_id=segments[0], model_version_id=model.id), str(uuid.uuid4())
    )
    token = queue.claim("old-model-worker")
    prepared = prepare_work(settings, factory, token)
    with factory.begin() as session:
        session.execute(
            update(ProcessingJob)
            .where(ProcessingJob.id == job.id)
            .values(lease_until=func.clock_timestamp() - timedelta(seconds=1))
        )
    queue.recover()
    fresh = queue.claim("new-model-worker")
    with pytest.raises(StaleExecution):
        queue.complete(token, prepared)
    with factory() as session:
        assert (
            session.scalar(select(Prediction).where(Prediction.segment_id == segments[0])) is None
        )
    queue.complete(fresh, prepare_work(settings, factory, fresh))


@pytest.mark.integration
def test_inference_and_annotation_partial_writes_roll_back(labeling_env):
    settings, engine, factory, queue, segments, model, actors = labeling_env
    job = queue.submit(
        InferenceJobRequest(segment_id=segments[0], model_version_id=model.id), str(uuid.uuid4())
    )
    token = queue.claim("rollback-model-worker")
    prepared = prepare_work(settings, factory, token)

    def interrupt(_c, _cu, statement, _p, _ctx, _many):
        if statement.startswith("INSERT INTO review_tasks"):
            raise RuntimeError("Injected after prediction insert")

    event.listen(engine, "before_cursor_execute", interrupt)
    try:
        with pytest.raises(RuntimeError):
            queue.complete(token, prepared)
    finally:
        event.remove(engine, "before_cursor_execute", interrupt)
    with factory() as session:
        assert (
            session.scalar(select(Prediction).where(Prediction.segment_id == segments[0])) is None
        )
        assert session.get(ProcessingJob, job.id).status == "RUNNING"
    result = queue.complete(token, prepared)
    review_id = uuid.UUID(result["review_id"])

    def interrupt_annotation(_c, _cu, statement, _p, _ctx, _many):
        if statement.startswith("INSERT INTO annotations"):
            raise RuntimeError("Injected after review CAS")

    event.listen(engine, "before_cursor_execute", interrupt_annotation)
    try:
        with pytest.raises(RuntimeError):
            annotate(
                factory,
                settings.data_root_key,
                review_id,
                AnnotationRequest(expected_version=0, label="QPSK", action="CORRECT"),
                actors[1][0],
            )
    finally:
        event.remove(engine, "before_cursor_execute", interrupt_annotation)
    with factory() as session:
        assert session.get(ReviewTask, review_id).label_version == 0
        assert (
            session.scalar(select(Annotation).where(Annotation.review_task_id == review_id)) is None
        )


@pytest.mark.integration
def test_database_guards_label_history_and_model_versions(labeling_env):
    settings, engine, factory, _, _, model, actors = labeling_env
    _, prediction, review = infer(labeling_env)
    annotation = annotate(
        factory,
        settings.data_root_key,
        review.id,
        AnnotationRequest(expected_version=0, label="16QAM", action="CORRECT"),
        actors[1][0],
    )
    for statement, identity in [
        ("UPDATE annotations SET label='BPSK' WHERE id=:id", annotation.id),
        ("DELETE FROM annotations WHERE id=:id", annotation.id),
        ("UPDATE predictions SET label='BPSK' WHERE id=:id", prediction.id),
        ("UPDATE model_versions SET config='{}' WHERE id=:id", model.id),
        ("DELETE FROM review_tasks WHERE id=:id", review.id),
        ("UPDATE review_tasks SET label_version=label_version+1 WHERE id=:id", review.id),
    ]:
        with pytest.raises(IntegrityError), engine.begin() as connection:
            connection.execute(text(statement), {"id": identity})
    with pytest.raises(DomainError):
        register_model(factory, model.name, model.version, config={"changed": True})
    _, another_prediction, _ = infer(labeling_env, 1)
    with pytest.raises(DomainError, match="belong"):
        annotate(
            factory,
            settings.data_root_key,
            review.id,
            AnnotationRequest(
                expected_version=1,
                label=another_prediction.label,
                action="CONFIRM",
                prediction_id=another_prediction.id,
            ),
            actors[1][0],
        )


@pytest.mark.integration
def test_torch_adapter_requires_matching_input_and_optional_runtime(labeling_env, monkeypatch):
    settings, _, _, _, _, _, _ = labeling_env
    model = ModelVersion(
        adapter="torchscript",
        config={
            "sample_count": 128,
            "preprocessing": "iq-rms-v1",
            "labels": ["BPSK", "QPSK"],
            "artifact_path": "model.pt",
        },
        artifact_sha256="0" * 64,
    )
    adapter = TorchScriptModel(settings, model)
    with pytest.raises(DomainError, match="sample length"):
        adapter.predict(np.zeros(64, dtype=np.complex64))
    monkeypatch.setitem(sys.modules, "torch", None)
    with pytest.raises(DomainError, match="optional PyTorch"):
        adapter.predict(np.zeros(128, dtype=np.complex64))
