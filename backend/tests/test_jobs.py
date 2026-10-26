import multiprocessing as mp
import struct
import threading
import time
import uuid
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, func, select, text, update
from sqlalchemy.exc import IntegrityError

from app.core.errors import DomainError
from app.db.session import build_engine, build_session_factory
from app.main import create_app
from app.models import (
    JobEvent,
    ProcessingConfig,
    ProcessingJob,
    RawFile,
    SegmentQuality,
    SignalSegment,
)
from app.schemas.iq import SegmentConfig
from app.schemas.jobs import QualityJobRequest, ScanJobRequest, SegmentJobRequest
from app.services.ingestion import IngestionService
from app.services.job_queue import JobQueue, StaleExecution
from app.services.job_work import prepare_work
from app.services.segments import index_segments
from app.workers.runner import LeaseGuard, Worker
from tests.auth_helpers import session_header
from tests.iq_helpers import make_source, write_metadata

pytestmark = pytest.mark.integration


@pytest.fixture
def job_env(postgres_database, tmp_path):
    settings, engine = postgres_database
    settings = settings.model_copy(
        update={
            "data_root": tmp_path,
            "data_root_key": "queue_" + uuid.uuid4().hex,
            "retry_base_seconds": 0,
            "worker_lease_seconds": 10,
            "worker_heartbeat_seconds": 0.05,
            "worker_recovery_seconds": 0.1,
        }
    )
    factory = build_session_factory(engine)
    return settings, engine, factory, JobQueue(settings, factory)


def scan_job(queue, **options):
    return queue.submit(ScanJobRequest(**options), str(uuid.uuid4()))


def expire(factory, job_id):
    with factory.begin() as session:
        session.execute(
            update(ProcessingJob)
            .where(ProcessingJob.id == job_id)
            .values(lease_until=func.clock_timestamp() - text("INTERVAL '1 second'"))
        )


def source_job(env, *, max_attempts=3, window=128):
    settings, _, factory, queue = env
    make_source(settings.data_root)
    raw_id = uuid.UUID(IngestionService(settings, factory).scan().files[0]["id"])
    request = SegmentJobRequest(
        raw_file_id=raw_id, config=SegmentConfig(window_samples=window), max_attempts=max_attempts
    )
    return queue.submit(request, str(uuid.uuid4())), raw_id


def claim_in_child(settings, barrier, sender):
    engine = build_engine(settings)
    try:
        queue = JobQueue(settings, build_session_factory(engine))
        barrier.wait(timeout=20)
        sender.send(queue.claim(str(uuid.uuid4())))
    finally:
        sender.close()
        engine.dispose()


def block_real_worker(settings, sender):
    from app.workers import runner

    engine = build_engine(settings)
    try:

        def blocked(_settings, _factory, token):
            sender.send(token)
            threading.Event().wait()

        runner.prepare_work = blocked
        Worker(JobQueue(settings, build_session_factory(engine))).run_once()
    finally:
        sender.close()
        engine.dispose()


def test_two_processes_claim_only_one_job(job_env):
    settings, _, factory, queue = job_env
    job = scan_job(queue)
    context = mp.get_context("spawn")
    barrier = context.Barrier(2)
    readers, children = [], []
    try:
        for _ in range(2):
            receiver, sender = context.Pipe(duplex=False)
            child = context.Process(target=claim_in_child, args=(settings, barrier, sender))
            child.start()
            sender.close()
            readers.append(receiver)
            children.append(child)
        tokens = []
        for receiver in readers:
            assert receiver.poll(30), "Worker did not finish its database claim"
            tokens.append(receiver.recv())
        assert sum(token is not None for token in tokens) == 1
        with factory() as session:
            row = session.get(ProcessingJob, job.id)
            assert row.attempts == row.execution_version == 1
    finally:
        for child in children:
            child.join(timeout=5)
            if child.is_alive():
                child.kill()
                child.join(timeout=5)
            assert child.exitcode == 0
        for receiver in readers:
            receiver.close()


def test_skip_locked_and_priority(job_env):
    _, _, factory, queue = job_env
    high, low = scan_job(queue, priority=100), scan_job(queue, priority=-100)
    with factory.begin() as session:
        session.execute(select(ProcessingJob).where(ProcessingJob.id == high.id).with_for_update())
        token = queue.claim("unblocked-worker")
        assert token.job_id == low.id
    assert queue.claim("high-worker").job_id == high.id


def test_killed_worker_recovers_and_old_execution_cannot_publish(job_env):
    settings, _, factory, queue = job_env
    job, raw_id = source_job(job_env)
    context = mp.get_context("spawn")
    receiver, sender = context.Pipe(duplex=False)
    child = context.Process(target=block_real_worker, args=(settings, sender))
    child.start()
    sender.close()
    try:
        assert receiver.poll(30)
        old = receiver.recv()
        assert old.job_id == job.id
        child.kill()
        child.join(timeout=10)
        assert not child.is_alive()
        with factory() as session:
            assert session.get(ProcessingJob, job.id).status == "RUNNING"
        expire(factory, job.id)
        assert queue.heartbeat(old) is False
        assert queue.recover() == 1
        fresh = queue.claim("replacement-worker")
        assert fresh.execution_version == old.execution_version + 1
        prepared = prepare_work(settings, factory, old)
        with pytest.raises(StaleExecution):
            queue.complete(old, prepared)
        assert queue.fail(old, "late failure", True) is False
        with factory() as session:
            assert (
                session.scalar(
                    select(func.count())
                    .select_from(SignalSegment)
                    .where(SignalSegment.raw_file_id == raw_id)
                )
                == 0
            )
        result = queue.complete(fresh, prepare_work(settings, factory, fresh))
        assert result["created"] == 2
        with factory() as session:
            row = session.get(ProcessingJob, job.id)
            assert (row.status, row.attempts, row.execution_version) == ("SUCCEEDED", 2, 2)
    finally:
        if child.is_alive():
            child.kill()
            child.join(timeout=5)
        receiver.close()


def test_heartbeat_renews_lease_and_cannot_revive_expired_token(job_env):
    settings, _, factory, queue = job_env
    job = scan_job(queue)
    token = queue.claim("heartbeat-worker")
    with factory() as session:
        before = session.get(ProcessingJob, job.id).lease_until
    with LeaseGuard(queue, token):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            with factory() as session:
                after = session.get(ProcessingJob, job.id).lease_until
            if after > before:
                break
            time.sleep(0.02)
        assert after > before
    expire(factory, job.id)
    assert not queue.heartbeat(token)
    with pytest.raises(StaleExecution):
        queue.complete(token, prepare_work(settings, factory, token))


@pytest.mark.parametrize("crash", [False, True])
def test_retry_limit_for_error_and_lease_expiry(job_env, crash):
    _, _, factory, queue = job_env
    job = scan_job(queue, max_attempts=2)
    for attempt in (1, 2):
        token = queue.claim("attempt-worker")
        assert token.execution_version == attempt
        if crash:
            expire(factory, job.id)
            assert queue.recover() == 1
        else:
            assert queue.fail(token, "TRANSIENT_IO: simulated temporary failure", True)
    assert queue.claim("extra-worker") is None
    with factory() as session:
        row = session.get(ProcessingJob, job.id)
        assert row.status == "FAILED" and row.attempts == 2


def test_backoff_prevents_early_claim(job_env):
    settings, _, factory, _ = job_env
    queue = JobQueue(settings.model_copy(update={"retry_base_seconds": 2}), factory)
    job = scan_job(queue)
    token = queue.claim("backoff-worker")
    assert queue.fail(token, "TRANSIENT_IO", True)
    assert queue.claim("early-worker") is None
    with factory() as session:
        assert session.get(ProcessingJob, job.id).status == "RETRY_WAIT"


def test_completion_replay_and_result_mismatch(job_env):
    settings, _, factory, queue = job_env
    job, raw_id = source_job(job_env)
    token = queue.claim("result-worker")
    prepared = prepare_work(settings, factory, token)
    result = queue.complete(token, prepared)
    assert queue.complete(token, prepared) == result
    wrong = replace(prepared, data=replace(prepared.data, start=1))
    with pytest.raises(DomainError, match="different result"):
        queue.complete(token, wrong)
    with factory() as session:
        assert (
            session.scalar(
                select(func.count())
                .select_from(SignalSegment)
                .where(SignalSegment.raw_file_id == raw_id)
            )
            == 2
        )
        assert (
            session.scalar(
                select(func.count())
                .select_from(JobEvent)
                .where(JobEvent.job_id == job.id, JobEvent.event == "SUCCEEDED")
            )
            == 1
        )
        assert set(
            session.scalars(
                select(SignalSegment.created_by_job_id).where(SignalSegment.raw_file_id == raw_id)
            )
        ) == {job.id}


def test_result_transaction_rolls_back_all_business_rows(job_env):
    settings, engine, factory, queue = job_env
    job, raw_id = source_job(job_env, window=117)
    token = queue.claim("atomic-worker")
    prepared = prepare_work(settings, factory, token)

    def interrupt(_connection, _cursor, statement, parameters, _context, _many):
        if (
            statement.startswith("UPDATE processing_jobs")
            and parameters.get("status") == "SUCCEEDED"
        ):
            raise RuntimeError("fault after business writes, before success update")

    event.listen(engine, "before_cursor_execute", interrupt)
    try:
        with pytest.raises(RuntimeError, match="fault after business"):
            queue.complete(token, prepared)
    finally:
        event.remove(engine, "before_cursor_execute", interrupt)
    with factory() as session:
        assert session.get(ProcessingJob, job.id).status == "RUNNING"
        assert (
            session.scalar(
                select(func.count())
                .select_from(SignalSegment)
                .where(SignalSegment.raw_file_id == raw_id)
            )
            == 0
        )
        assert (
            session.scalar(
                select(ProcessingConfig).where(
                    ProcessingConfig.config_hash == prepared.data.config_hash
                )
            )
            is None
        )
    assert queue.complete(token, prepared)["created"] == 2


def test_final_lease_fence_rolls_back_already_inserted_rows(job_env):
    settings, engine, factory, _ = job_env
    settings = settings.model_copy(
        update={"worker_lease_seconds": 1.0, "worker_heartbeat_seconds": 0.1}
    )
    env = settings, engine, factory, JobQueue(settings, factory)
    job, raw_id = source_job(env)
    queue = env[3]
    token = queue.claim("deadline-worker")
    prepared = prepare_work(settings, factory, token)
    delayed = []

    def delay_commit(connection, _cursor, statement, parameters, _context, _many):
        if (
            statement.startswith("UPDATE processing_jobs")
            and parameters.get("status") == "SUCCEEDED"
        ):
            delayed.append(True)
            connection.execute(text("SELECT pg_sleep(1.1)"))

    event.listen(engine, "before_cursor_execute", delay_commit)
    try:
        with pytest.raises(StaleExecution):
            queue.complete(token, prepared)
    finally:
        event.remove(engine, "before_cursor_execute", delay_commit)
    assert delayed
    with factory() as session:
        assert (
            session.scalar(
                select(func.count())
                .select_from(SignalSegment)
                .where(SignalSegment.raw_file_id == raw_id)
            )
            == 0
        )
        assert session.get(ProcessingJob, job.id).status == "RUNNING"


def test_scan_has_no_official_writes_until_fenced_completion(job_env):
    settings, _, factory, queue = job_env
    make_source(settings.data_root)
    job = scan_job(queue)
    old = queue.claim("scan-old")
    prepared = prepare_work(settings, factory, old)
    with factory() as session:
        assert (
            session.scalar(
                select(func.count())
                .select_from(RawFile)
                .where(RawFile.root_key == settings.data_root_key)
            )
            == 0
        )
    expire(factory, job.id)
    queue.recover()
    fresh = queue.claim("scan-fresh")
    with pytest.raises(StaleExecution):
        queue.complete(old, prepared)
    report = queue.complete(fresh, prepare_work(settings, factory, fresh))
    assert report["registered"] == 1


def test_full_worker_quality_and_idempotent_requests(job_env):
    settings, _, factory, queue = job_env
    make_source(settings.data_root)
    raw_id = uuid.UUID(IngestionService(settings, factory).scan().files[0]["id"])
    refs = index_segments(settings, factory, raw_id, SegmentConfig(window_samples=128))
    request = QualityJobRequest(segment_id=uuid.UUID(refs["segment_ids"][0]))
    key = str(uuid.uuid4())
    job = queue.submit(request, key)
    assert queue.submit(request, key).id == job.id
    assert queue.submit(request, str(uuid.uuid4())).id == job.id
    assert Worker(queue).run_once()
    with factory() as session:
        done = session.get(ProcessingJob, job.id)
        assert done.status == "SUCCEEDED"
        assert done.result["metrics"]["sample_count"] == 128
        assert (
            session.scalar(
                select(func.count())
                .select_from(SegmentQuality)
                .where(SegmentQuality.segment_id == request.segment_id)
            )
            == 1
        )
        assert session.get(SignalSegment, request.segment_id).quality["finite"]


def test_scan_reports_per_file_errors_and_allows_a_later_scan(job_env):
    settings, _, factory, queue = job_env
    path, metadata, _ = make_source(settings.data_root, "bad.bin")
    metadata["sample_rate_hz"] = 0
    write_metadata(path, metadata)
    make_source(settings.data_root, "good.bin")
    first = scan_job(queue)
    Worker(queue).run_once()
    with factory() as session:
        done = session.get(ProcessingJob, first.id)
        assert done.status == "SUCCEEDED" and len(done.result["errors"]) == 1
        assert done.result["registered"] == 1
    second = scan_job(queue)
    assert second.id != first.id


def test_failed_job_rerun_is_a_new_audited_generation(job_env):
    _, _, factory, queue = job_env
    parent = scan_job(queue, max_attempts=1)
    token = queue.claim("failed-worker")
    queue.fail(token, "INVALID_INPUT", False)
    key = str(uuid.uuid4())
    rerun = queue.rerun(parent.id, key)
    assert queue.rerun(parent.id, key).id == rerun.id
    assert rerun.parent_job_id == parent.id and rerun.generation == 1 and rerun.attempts == 0
    with pytest.raises(DomainError, match="latest failed"):
        queue.rerun(parent.id, str(uuid.uuid4()))
    with factory() as session:
        assert session.get(ProcessingJob, parent.id).status == "FAILED"


def test_api_auth_submission_conflict_and_job_queries(job_env):
    settings, engine, factory, queue = job_env
    headers = {**session_header(engine), "Idempotency-Key": str(uuid.uuid4())}
    with TestClient(create_app(settings, engine=engine)) as client:
        assert client.post("/api/v1/scans", json={"directory": "."}).status_code == 401
        client.headers.update({"Authorization": headers["Authorization"]})
        response = client.post("/api/v1/scans", json={"directory": "."}, headers=headers)
        assert response.status_code == 202, response.text
        job_id = response.json()["id"]
        assert response.headers["location"] == f"/api/v1/jobs/{job_id}"
        assert (
            client.post("/api/v1/scans", json={"directory": "."}, headers=headers).json()["id"]
            == job_id
        )
        assert (
            client.post("/api/v1/scans", json={"directory": "another"}, headers=headers).status_code
            == 409
        )
        assert client.get("/api/v1/jobs?status=PENDING").json()["total"] == 1
        assert client.get(f"/api/v1/jobs/{job_id}/events").json()["items"][0]["event"] == "ENQUEUED"
        assert (
            client.post(
                "/api/v1/scans",
                json={"directory": "../outside"},
                headers={**headers, "Idempotency-Key": str(uuid.uuid4())},
            ).status_code
            == 403
        )
        assert client.get(f"/api/v1/jobs/{uuid.uuid4()}").status_code == 404
        assert "test-admin-token" not in response.text
        assert Worker(queue).run_once()
        assert client.get(f"/api/v1/jobs/{job_id}").json()["status"] == "SUCCEEDED"


def test_database_guards_job_contract_and_append_only_audit(job_env):
    _, engine, factory, queue = job_env
    job = scan_job(queue)
    for sql in [
        "UPDATE processing_jobs SET attempts=9 WHERE id=:id",
        "DELETE FROM processing_jobs WHERE id=:id",
        "UPDATE processing_jobs SET payload='{}'::jsonb WHERE id=:id",
        "DELETE FROM job_events WHERE job_id=:id",
    ]:
        with pytest.raises(IntegrityError), engine.begin() as connection:
            connection.execute(text(sql), {"id": job.id})
    with factory() as session:
        assert session.get(ProcessingJob, job.id).attempts == 0


@pytest.mark.parametrize("field", ["worker_id", "execution_version"])
def test_each_execution_identity_component_is_checked(job_env, field):
    settings, _, factory, queue = job_env
    job, raw_id = source_job(job_env)
    token = queue.claim("valid-worker")
    forged = replace(
        token, **{field: "other-worker" if field == "worker_id" else token.execution_version + 1}
    )
    assert not queue.heartbeat(forged)
    assert not queue.fail(forged, "wrong owner", True)
    with pytest.raises(StaleExecution):
        queue.complete(forged, prepare_work(settings, factory, token))
    with factory() as session:
        assert session.get(ProcessingJob, job.id).status == "RUNNING"
        assert (
            session.scalar(
                select(func.count())
                .select_from(SignalSegment)
                .where(SignalSegment.raw_file_id == raw_id)
            )
            == 0
        )


def test_nonretryable_quality_failure_has_no_official_result(job_env):
    settings, _, factory, queue = job_env
    path, _, _ = make_source(settings.data_root)
    with path.open("r+b") as stream:
        stream.write(struct.pack("<f", float("nan")))
    raw_id = uuid.UUID(IngestionService(settings, factory).scan().files[0]["id"])
    refs = index_segments(settings, factory, raw_id, SegmentConfig(window_samples=128))
    segment_id = uuid.UUID(refs["segment_ids"][0])
    job = queue.submit(QualityJobRequest(segment_id=segment_id), str(uuid.uuid4()))
    assert Worker(queue).run_once()
    with factory() as session:
        failed = session.get(ProcessingJob, job.id)
        assert failed.status == "FAILED" and failed.attempts == 1
        assert "NONFINITE_IQ" in failed.last_error
        assert failed.result is None
        assert (
            session.scalar(select(SegmentQuality).where(SegmentQuality.segment_id == segment_id))
            is None
        )
    assert queue.claim("retry-forbidden") is None


def test_static_admin_token_cannot_replace_user_login(job_env):
    settings, engine, _, _ = job_env
    with TestClient(
        create_app(settings.model_copy(update={"admin_token": None}), engine=engine)
    ) as client:
        response = client.post(
            "/api/v1/scans",
            json={},
            headers={
                "Authorization": "Bearer retired-static-token",
                "Idempotency-Key": str(uuid.uuid4()),
            },
        )
        assert response.status_code == 401
        assert response.json()["error"]["code"] == "UNAUTHORIZED"
