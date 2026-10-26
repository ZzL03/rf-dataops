import concurrent.futures
import hashlib
import json
import os
import threading
import uuid
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, func, select, text, update
from sqlalchemy.exc import IntegrityError

from app.core.errors import DomainError
from app.core.identity import canonical_json
from app.main import create_app
from app.models import Annotation, DatasetVersion, DatasetVersionItem, ProcessingJob
from app.schemas.datasets import DatasetRequest, PublicationRequest, Selection, SelectionRequest
from app.schemas.jobs import PublishJobRequest
from app.schemas.labeling import AnnotationRequest
from app.services.datasets import create_dataset, preview_selection
from app.services.job_queue import StaleExecution
from app.services.job_work import prepare_work
from app.services.labeling import annotate
from app.workers.runner import Worker
from tests import test_labeling
from tests.test_labeling import infer

labeling_env = test_labeling.labeling_env

pytestmark = pytest.mark.integration


@pytest.fixture
def pub_env(labeling_env):
    settings, engine, factory, queue, segments, model, actors = labeling_env
    _, prediction, review = infer(labeling_env)
    annotation = annotate(
        factory,
        settings.data_root_key,
        review.id,
        AnnotationRequest(
            expected_version=0, label="QPSK", action="CORRECT", comment="合成样本演示"
        ),
        actors[1][0],
    )
    dataset = create_dataset(
        factory,
        settings.data_root_key,
        DatasetRequest(name="数据集_" + uuid.uuid4().hex),
        actors[-1][0],
    )
    selection = SelectionRequest(selection=Selection(segment_ids=segments[:2], review_status=None))
    return labeling_env, dataset, selection, review, annotation, prediction


def enqueue(env, version="v1", selection=None, digest=None, key=None):
    base, dataset, default, *_ = env
    _, _, factory, queue, _, _, actors = base
    selection = selection or default
    with factory() as session:
        preview = preview_selection(session, dataset, selection)
    request = PublicationRequest(
        **selection.model_dump(),
        version=version,
        expected_selection_digest=digest or preview["selection_digest"],
    )
    job = queue.submit(
        PublishJobRequest(dataset_id=dataset.id, publication=request),
        key or str(uuid.uuid4()),
        f"user:{actors[-1][0].id}",
    )
    return job, request


def publish(env, version="v1", selection=None):
    job, _ = enqueue(env, version, selection)
    settings, _, factory, queue, *_ = env[0]
    assert Worker(queue).run_once()
    with factory() as session:
        done = session.get(ProcessingJob, job.id)
        assert done.status == "SUCCEEDED", done.last_error
        result = session.get(DatasetVersion, uuid.UUID(done.result["dataset_version_id"]))
        return result, done


def draft(session, env, job, request):
    row = DatasetVersion(
        dataset_id=env[1].id,
        version=request.version,
        selection=request.selection.model_dump(mode="json"),
        label_policy=request.label_policy,
        model_version_id=request.model_version_id,
        allow_mock_predictions=request.allow_mock_predictions,
        published_by=env[0][-1][-1][0].id,
        publish_job_id=job.id,
    )
    session.add(row)
    session.flush()
    return row


def one_item(env, version_id, **changes):
    _, _, _, review, annotation, _ = env
    with env[0][2]() as session:
        from app.models import SignalSegment

        raw_id = session.get(SignalSegment, review.segment_id).raw_file_id
    values = dict(
        dataset_version_id=version_id,
        segment_id=review.segment_id,
        raw_file_id=raw_id,
        annotation_id=annotation.id,
        label_source="HUMAN",
        frozen_label=annotation.label,
        snapshot={},
    )
    return DatasetVersionItem(**(values | changes))


def test_publish_manifest_traceability_and_byte_identity(pub_env):
    base, dataset, _, review, annotation, _ = pub_env
    settings, engine, factory, queue, _, _, actors = base
    version, job = publish(pub_env)
    document = json.loads(version.manifest_text)
    assert version.sealed and version.item_count == 1
    item = document["items"][0]
    assert document["dataset"]["name"] == dataset.name
    assert document["label_policy"] == "HUMAN_ONLY" and not document["contains_mock_predictions"]
    assert document["source_counts"] == {"HUMAN": 1}
    assert item["segment_id"] == str(review.segment_id) and item["label"] == "QPSK"
    assert item["annotation"]["id"] == str(annotation.id) and item["annotation"][
        "reviewer_id"
    ] == str(actors[1][0].id)
    assert len(item["source"]["sha256"]) == 64 and item["source"]["iq_metadata"]
    assert item["interval"]["sample_count"] == 128 and item["processing"]["reader_version"]
    assert canonical_json(document) == version.manifest_text
    with TestClient(create_app(settings, engine=engine), headers=actors[-1][1]) as client:
        response = client.get(f"/api/v1/dataset-versions/{version.id}/manifest")
        assert response.content == version.manifest_text.encode("utf-8")
        assert (
            hashlib.sha256(response.content).hexdigest()
            == response.headers["x-manifest-sha256"]
            == version.manifest_sha256
        )
        assert response.headers["etag"] == f'"{version.manifest_sha256}"'
        assert "attachment" in response.headers["content-disposition"]
        assert client.get(f"/api/v1/dataset-versions/{version.id}/items?limit=1").json()[
            "items"
        ] == [item]
        assert client.get(f"/api/v1/dataset-versions/{version.id}").json()[
            "label_distribution"
        ] == {"QPSK": 1}


def test_new_label_and_new_version_leave_old_manifest_unchanged(pub_env):
    settings, engine, factory, _, _, _, actors = pub_env[0]
    first, _ = publish(pub_env)
    annotate(
        factory,
        settings.data_root_key,
        pub_env[3].id,
        AnnotationRequest(expected_version=1, label="BPSK", action="CORRECT"),
        actors[1][0],
    )
    second, _ = publish(pub_env, "v2")
    assert json.loads(first.manifest_text)["items"][0]["label"] == "QPSK"
    assert json.loads(second.manifest_text)["items"][0]["label"] == "BPSK"
    with TestClient(create_app(settings, engine=engine), headers=actors[0][1]) as client:
        assert (
            client.get(f"/api/v1/dataset-versions/{first.id}/manifest").content
            == first.manifest_text.encode()
        )
        # A lost source must not make historical metadata dynamically change or disappear.
        next(settings.data_root.glob("*.bin")).unlink()
        assert (
            client.get(f"/api/v1/dataset-versions/{first.id}/manifest").content
            == first.manifest_text.encode()
        )


def test_model_fallback_requires_explicit_version_and_mock_ack(pub_env):
    base, dataset, *_ = pub_env
    _, _, factory, _, segments, model, _ = base
    infer(base, 1)
    selection = SelectionRequest(
        selection=Selection(segment_ids=segments[:2], review_status=None),
        label_policy="MODEL_ALLOWED",
        model_version_id=model.id,
    )
    with factory() as session, pytest.raises(DomainError, match="acknowledge"):
        preview_selection(session, dataset, selection)
    selection = selection.model_copy(update={"allow_mock_predictions": True})
    version, _ = publish(pub_env, selection=selection)
    document = json.loads(version.manifest_text)
    assert document["source_counts"] == {"HUMAN": 1, "MODEL": 1}
    assert document["contains_mock_predictions"]
    assert any(
        item["label"] == "QPSK" and item["label_source"] == "HUMAN" for item in document["items"]
    )
    model_item = next(item for item in document["items"] if item["label_source"] == "MODEL")
    assert (
        model_item["prediction"]["model"]["id"] == str(model.id)
        and model_item["prediction"]["is_mock"]
    )
    assert "artifact_path" not in model_item["prediction"]["model"]["config"]


def test_empty_selection_limits_and_unknown_human_priority(pub_env, monkeypatch):
    base, dataset, default, review, *_ = pub_env
    settings, _, factory, _, segments, model, actors = base
    with factory() as session, pytest.raises(DomainError, match="No eligible"):
        preview_selection(
            session, dataset, SelectionRequest(selection=Selection(segment_ids=[segments[1]]))
        )
    annotate(
        factory,
        settings.data_root_key,
        review.id,
        AnnotationRequest(expected_version=1, label="UNKNOWN", action="UNKNOWN"),
        actors[1][0],
    )
    selection = SelectionRequest(
        selection=Selection(segment_ids=[review.segment_id], review_status=None),
        label_policy="MODEL_ALLOWED",
        model_version_id=model.id,
        allow_mock_predictions=True,
    )
    with factory() as session:
        preview = preview_selection(session, dataset, selection)
        assert preview["label_distribution"] == {"UNKNOWN": 1} and preview["source_counts"] == {
            "HUMAN": 1
        }
    monkeypatch.setattr("app.services.datasets.MAX_MEMBERS", 0)
    with factory() as session, pytest.raises(DomainError, match="10,000"):
        preview_selection(session, dataset, selection)


def test_preview_change_rejects_stale_publication_without_results(pub_env):
    settings, _, factory, queue, _, _, actors = pub_env[0]
    job, _ = enqueue(pub_env)
    annotate(
        factory,
        settings.data_root_key,
        pub_env[3].id,
        AnnotationRequest(expected_version=1, label="BPSK", action="CORRECT"),
        actors[1][0],
    )
    assert Worker(queue).run_once()
    with factory() as session:
        done = session.get(ProcessingJob, job.id)
        assert done.status == "FAILED" and "SELECTION_CHANGED" in done.last_error
        assert (
            session.scalar(
                select(DatasetVersion.id).where(DatasetVersion.dataset_id == pub_env[1].id)
            )
            is None
        )


def test_complete_rechecks_selection_after_hashing(pub_env):
    settings, _, factory, queue, _, _, actors = pub_env[0]
    job, _ = enqueue(pub_env)
    token = queue.claim("publisher")
    prepared = prepare_work(settings, factory, token)
    annotate(
        factory,
        settings.data_root_key,
        pub_env[3].id,
        AnnotationRequest(expected_version=1, label="BPSK", action="CORRECT"),
        actors[1][0],
    )
    with pytest.raises(DomainError, match="Selection changed"):
        queue.complete(token, prepared)


def test_repeatable_read_keeps_one_label_snapshot_during_concurrent_review(pub_env):
    settings, engine, factory, queue, _, _, actors = pub_env[0]
    _, _ = enqueue(pub_env)
    token = queue.claim("snapshot-publisher")
    prepared = prepare_work(settings, factory, token)
    changed = []

    def change_label(_c, _cu, statement, _p, _ctx, _many):
        if statement.startswith("INSERT INTO dataset_versions") and not changed:
            changed.append(True)
            annotate(
                factory,
                settings.data_root_key,
                pub_env[3].id,
                AnnotationRequest(expected_version=1, label="BPSK", action="CORRECT"),
                actors[1][0],
            )

    event.listen(engine, "before_cursor_execute", change_label)
    try:
        result = queue.complete(token, prepared)
    finally:
        event.remove(engine, "before_cursor_execute", change_label)
    with factory() as session:
        stored = session.get(DatasetVersion, uuid.UUID(result["dataset_version_id"]))
        assert changed and json.loads(stored.manifest_text)["items"][0]["label"] == "QPSK"
        assert (
            session.scalar(
                select(Annotation.label)
                .where(Annotation.review_task_id == pub_env[3].id)
                .order_by(Annotation.version.desc())
                .limit(1)
            )
            == "BPSK"
        )


def test_source_full_hash_detects_same_stat_tampering(pub_env):
    settings, _, factory, queue, *_ = pub_env[0]
    job, _ = enqueue(pub_env)
    path = next(settings.data_root.glob("*.bin"))
    before = path.stat()
    content = bytearray(path.read_bytes())
    content[0] ^= 1
    path.write_bytes(content)
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
    assert Worker(queue).run_once()
    with factory() as session:
        done = session.get(ProcessingJob, job.id)
        assert done.status == "FAILED" and "SOURCE_CHANGED" in done.last_error
        assert (
            session.scalar(select(DatasetVersion.id).where(DatasetVersion.publish_job_id == job.id))
            is None
        )


def test_partial_publication_rolls_back_then_replay_is_idempotent(pub_env):
    settings, engine, factory, queue, *_ = pub_env[0]
    job, _ = enqueue(pub_env)
    token = queue.claim("rollback-publisher")
    prepared = prepare_work(settings, factory, token)

    def interrupt(_c, _cu, statement, _p, _ctx, _many):
        if statement.startswith("SELECT rf_dataset_manifest"):
            raise RuntimeError("Injected after all member inserts, before seal")

    event.listen(engine, "before_cursor_execute", interrupt)
    try:
        with pytest.raises(RuntimeError):
            queue.complete(token, prepared)
    finally:
        event.remove(engine, "before_cursor_execute", interrupt)
    with factory() as session:
        assert (
            session.scalar(select(DatasetVersion.id).where(DatasetVersion.publish_job_id == job.id))
            is None
        )
        assert session.get(ProcessingJob, job.id).status == "RUNNING"
    result = queue.complete(token, prepared)
    assert queue.complete(token, prepared) == result
    with factory() as session:
        assert (
            session.scalar(
                select(func.count())
                .select_from(DatasetVersionItem)
                .where(
                    DatasetVersionItem.dataset_version_id == uuid.UUID(result["dataset_version_id"])
                )
            )
            == 1
        )


def test_expired_publication_cannot_leave_sealed_results(pub_env):
    settings, engine, factory, queue, *_ = pub_env[0]
    queue.settings = settings.model_copy(update={"worker_lease_seconds": 1})
    job, _ = enqueue(pub_env)
    old = queue.claim("old-publisher")
    prepared = prepare_work(settings, factory, old)
    with factory.begin() as session:
        session.execute(
            update(ProcessingJob)
            .where(ProcessingJob.id == job.id)
            .values(lease_until=func.clock_timestamp() - timedelta(seconds=1))
        )
    queue.recover()
    fresh = queue.claim("fresh-publisher")
    with pytest.raises(StaleExecution):
        queue.complete(old, prepared)

    def delay(connection, _cu, statement, _p, _ctx, _many):
        if statement.startswith("UPDATE processing_jobs SET status="):
            connection.exec_driver_sql("SELECT pg_sleep(1.1)")

    event.listen(engine, "before_cursor_execute", delay)
    try:
        with pytest.raises(StaleExecution):
            queue.complete(fresh, prepare_work(settings, factory, fresh))
    finally:
        event.remove(engine, "before_cursor_execute", delay)
    with factory() as session:
        assert (
            session.scalar(
                select(DatasetVersion.id).where(DatasetVersion.dataset_id == pub_env[1].id)
            )
            is None
        )


@pytest.mark.parametrize(
    "action", ["version_update", "version_delete", "item_update", "item_delete", "item_append"]
)
def test_sealed_database_history_rejects_direct_changes(pub_env, action):
    _, engine, factory, *_ = pub_env[0]
    version, _ = publish(pub_env)
    with pytest.raises(IntegrityError), factory.begin() as session:
        if action == "version_update":
            session.execute(
                text("UPDATE dataset_versions SET item_count=2 WHERE id=:id"), {"id": version.id}
            )
        elif action == "version_delete":
            session.execute(text("DELETE FROM dataset_versions WHERE id=:id"), {"id": version.id})
        elif action == "item_update":
            session.execute(
                text(
                    "UPDATE dataset_version_items SET frozen_label='BPSK' "
                    "WHERE dataset_version_id=:id"
                ),
                {"id": version.id},
            )
        elif action == "item_delete":
            session.execute(
                text("DELETE FROM dataset_version_items WHERE dataset_version_id=:id"),
                {"id": version.id},
            )
        else:
            session.add(one_item(pub_env, version.id))


def test_unsealed_version_cannot_commit(pub_env):
    _, _, factory, queue, *_ = pub_env[0]
    job, request = enqueue(pub_env)
    queue.claim("unsealed-publisher")
    with pytest.raises(IntegrityError, match="unsealed"), factory.begin() as session:
        draft(session, pub_env, job, request)


@pytest.mark.parametrize("tamper", ["manifest", "source", "snapshot"])
def test_database_rejects_forged_manifest_or_member(pub_env, tamper):
    _, _, factory, queue, segments, *_ = pub_env[0]
    job, request = enqueue(pub_env)
    queue.claim("forgery-publisher")
    with pytest.raises(IntegrityError), factory.begin() as session:
        version = draft(session, pub_env, job, request)
        item = one_item(
            pub_env,
            version.id,
            **(
                {"segment_id": segments[1]}
                if tamper == "source"
                else {"snapshot": {"forged": True}}
                if tamper == "snapshot"
                else {}
            ),
        )
        session.add(item)
        session.flush()
        manifest = session.scalar(text("SELECT rf_dataset_manifest(:id)"), {"id": version.id})
        manifest["items"][0]["label"] = "BPSK"
        version.manifest_text = canonical_json(manifest)
        version.manifest_sha256 = hashlib.sha256(version.manifest_text.encode()).hexdigest()
        version.item_count, version.sealed = 1, True


def test_api_permissions_pagination_root_isolation_and_idempotency(pub_env):
    settings, engine, factory, queue, _, _, actors = pub_env[0]
    dataset = pub_env[1]
    job, body = enqueue(pub_env)
    with TestClient(create_app(settings, engine=engine)) as client:
        assert client.get("/api/v1/datasets").status_code == 401
        client.headers.update(actors[-1][1])
        assert client.get("/api/v1/datasets?limit=1").json()["total"] == 1
        assert client.post("/api/v1/datasets", json={"name": dataset.name}).status_code == 409
        assert (
            client.post(
                "/api/v1/datasets", json={"name": "reviewer-forbidden"}, headers=actors[1][1]
            ).status_code
            == 403
        )
        preview = client.post(
            f"/api/v1/datasets/{dataset.id}/selection-preview",
            json=pub_env[2].model_dump(mode="json"),
        )
        assert preview.status_code == 200 and preview.json()["item_count"] == 1
        key = str(uuid.uuid4())
        response = client.post(
            f"/api/v1/datasets/{dataset.id}/versions",
            json=body.model_dump(mode="json"),
            headers={"Idempotency-Key": key},
        )
        assert response.status_code == 202 and response.json()["id"] == str(job.id)
        assert Worker(queue).run_once()
        replay = client.post(
            f"/api/v1/datasets/{dataset.id}/versions",
            json=body.model_dump(mode="json"),
            headers={"Idempotency-Key": key},
        )
        assert replay.status_code == 202 and replay.json()["id"] == str(job.id)
        assert (
            client.post(
                f"/api/v1/datasets/{dataset.id}/versions",
                json=body.model_dump(mode="json"),
                headers={"Idempotency-Key": str(uuid.uuid4())},
            ).status_code
            == 409
        )
        assert client.get(f"/api/v1/datasets/{dataset.id}/versions?limit=1").json()["total"] == 1
        other = settings.model_copy(update={"data_root_key": "other"})
        with TestClient(create_app(other, engine=engine), headers=actors[-1][1]) as outsider:
            assert outsider.get(f"/api/v1/datasets/{dataset.id}").status_code == 404
            assert outsider.get("/api/v1/datasets").json()["total"] == 0
            version_id = client.get(f"/api/v1/jobs/{job.id}").json()["result"]["dataset_version_id"]
            assert (
                outsider.get(f"/api/v1/dataset-versions/{version_id}/manifest").status_code == 404
            )


def test_concurrent_publishers_cannot_publish_two_copies_of_a_version(pub_env):
    settings, _, factory, queue, _, _, actors = pub_env[0]
    first, body = enqueue(pub_env)
    second = queue.submit(
        PublishJobRequest(dataset_id=pub_env[1].id, publication=body),
        str(uuid.uuid4()),
        f"user:{actors[0][0].id}",
    )
    assert first.id != second.id
    barrier = threading.Barrier(2)

    def complete(worker):
        token = queue.claim(worker)
        plan = prepare_work(settings, factory, token)
        barrier.wait(timeout=5)
        try:
            queue.complete(token, plan)
            return "SUCCEEDED"
        except DomainError as exc:
            assert exc.code == "VERSION_EXISTS"
            queue.fail(token, exc.code, False)
            return "FAILED"

    with concurrent.futures.ThreadPoolExecutor(2) as executor:
        results = list(executor.map(complete, ["publisher-a", "publisher-b"]))
    assert sorted(results) == ["FAILED", "SUCCEEDED"]
    with factory() as session:
        assert (
            session.scalar(
                select(func.count())
                .select_from(DatasetVersion)
                .where(DatasetVersion.dataset_id == pub_env[1].id)
            )
            == 1
        )


def test_concurrent_direct_append_locks_parent_then_rejects_sealed_version(pub_env):
    _, engine, factory, *_ = pub_env[0]
    version, _ = publish(pub_env)
    attempting = threading.Event()

    def observe(_c, _cu, statement, _p, _ctx, _many):
        if statement.startswith("INSERT INTO dataset_version_items"):
            attempting.set()

    def append():
        with pytest.raises(IntegrityError, match="cannot append"), factory.begin() as session:
            session.add(one_item(pub_env, version.id))

    event.listen(engine, "before_cursor_execute", observe)
    try:
        with engine.connect() as connection, concurrent.futures.ThreadPoolExecutor(1) as executor:
            transaction = connection.begin()
            connection.execute(
                text("SELECT id FROM dataset_versions WHERE id=:id FOR UPDATE"), {"id": version.id}
            )
            future = executor.submit(append)
            try:
                assert attempting.wait(timeout=5)
                with pytest.raises(concurrent.futures.TimeoutError):
                    future.result(timeout=0.05)
            finally:
                transaction.commit()
            future.result(timeout=5)
    finally:
        event.remove(engine, "before_cursor_execute", observe)
