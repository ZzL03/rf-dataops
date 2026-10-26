import os
import uuid
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from sqlalchemy import event, func, select, text
from sqlalchemy.exc import IntegrityError

from app.core.errors import DomainError
from app.core.identity import content_key
from app.db.session import build_session_factory
from app.models import FileObservation, ProcessingConfig, RawFile, SignalSegment
from app.schemas.iq import SegmentConfig
from app.services.ingestion import IngestionService, inspect_file
from app.services.segments import index_segments
from tests.auth_helpers import user_client
from tests.iq_helpers import make_source, write_metadata

pytestmark = pytest.mark.integration


@pytest.fixture
def asset_env(postgres_database, tmp_path):
    settings, engine = postgres_database
    local = settings.model_copy(
        update={"data_root": tmp_path, "data_root_key": "test_" + uuid.uuid4().hex}
    )
    factory = build_session_factory(engine)
    return local, engine, factory, IngestionService(local, factory)


def test_scan_idempotent_and_content_revision_with_unchanged_size_mtime(asset_env) -> None:
    settings, _, factory, service = asset_env
    path, _, _ = make_source(settings.data_root)
    original = path.read_bytes()
    before = path.stat()
    first = service.scan()
    again = service.scan()
    assert (first.registered, again.registered, again.unchanged) == (1, 0, 1)
    assert first.files[0]["id"] == again.files[0]["id"]
    changed = bytearray(original)
    changed[0] ^= 1
    path.write_bytes(changed)
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
    next_report = service.scan()
    assert next_report.registered == 1
    assert next_report.files[0]["sha256"] != first.files[0]["sha256"]
    with factory() as session:
        assert session.get(RawFile, uuid.UUID(first.files[0]["id"])).status == "STALE"
    path.write_bytes(original)
    restored = service.scan()
    assert restored.files[0]["id"] == first.files[0]["id"]
    with factory() as session:
        events = set(
            session.scalars(
                select(FileObservation.event).where(
                    FileObservation.raw_file_id == uuid.UUID(first.files[0]["id"])
                )
            )
        )
        assert {"REGISTERED", "VERIFIED", "CHANGED", "REACTIVATED"} <= events


def test_metadata_change_has_distinct_identity(asset_env) -> None:
    settings, _, factory, service = asset_env
    path, metadata, _ = make_source(settings.data_root)
    first = service.scan()
    metadata["sample_rate_hz"] = 2000.0
    write_metadata(path, metadata)
    second = service.scan()
    assert first.files[0]["sha256"] == second.files[0]["sha256"]
    assert first.files[0]["id"] != second.files[0]["id"]
    with factory() as session:
        assert session.get(RawFile, uuid.UUID(first.files[0]["id"])).status == "STALE"


def test_incomplete_invalid_and_missing_sources(asset_env) -> None:
    settings, _, factory, service = asset_env
    incomplete, _, _ = make_source(settings.data_root, "incomplete.bin")
    incomplete.with_name(incomplete.name + ".done").unlink()
    invalid, metadata, _ = make_source(settings.data_root, "invalid.bin")
    metadata["sample_rate_hz"] = 0
    write_metadata(invalid, metadata)
    valid, _, _ = make_source(settings.data_root, "valid.bin")
    result = service.scan()
    assert (result.registered, result.skipped, len(result.errors)) == (1, 1, 1)
    assert result.errors[0]["code"] == "METADATA_INVALID"
    file_id = uuid.UUID(result.files[0]["id"])
    valid.unlink()
    assert service.scan().invalidated == 1
    with factory() as session:
        assert session.get(RawFile, file_id).status == "INVALID"


def test_scan_limits_and_invalid_bin_length(asset_env) -> None:
    settings, _, _, service = asset_env
    path, _, _ = make_source(settings.data_root)
    path.write_bytes(b"not whole IQ")
    assert service.scan().errors[0]["code"] == "INVALID_BIN_SIZE"
    make_source(settings.data_root, "another.bin")
    limited = IngestionService(settings.model_copy(update={"max_scan_files": 1}), service.factory)
    with pytest.raises(DomainError, match="too many"):
        limited.scan()


def test_concurrent_registration_is_one_asset(asset_env) -> None:
    settings, _, factory, service = asset_env
    path, _, _ = make_source(settings.data_root)
    observed = inspect_file(service.root, path.name)
    barrier = Barrier(2)

    def register():
        barrier.wait(timeout=10)
        return service.register(observed)

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(register) for _ in range(2)]
        results = [future.result(timeout=20) for future in futures]
    assert results[0][0] == results[1][0]
    assert sorted(created for _, created in results) == [False, True]
    with factory() as session:
        assert (
            session.scalar(
                select(func.count())
                .select_from(RawFile)
                .where(RawFile.root_key == settings.data_root_key)
            )
            == 1
        )


@pytest.mark.parametrize("suffix", ["bin", "h5"])
def test_ingest_index_query_preview_end_to_end(asset_env, suffix: str) -> None:
    settings, engine, factory, service = asset_env
    _, _, iq = make_source(settings.data_root, f"capture.{suffix}")
    file_id = uuid.UUID(service.scan().files[0]["id"])
    config = SegmentConfig(window_samples=128, overlap_ratio=0.5)
    result = index_segments(settings, factory, file_id, config)
    replay = index_segments(
        settings, factory, file_id, SegmentConfig(window_samples=128, stride_samples=64)
    )
    assert (result["created"], replay["created"], replay["existing"]) == (3, 0, 3)
    assert result["config_id"] == replay["config_id"]
    with user_client(settings, engine) as client:
        files = client.get("/api/v1/files?limit=1&offset=0&status=ACTIVE").json()
        assert files["total"] == 1
        assert files["items"][0]["segment_count"] == 3
        assert "stat_identity" not in files["items"][0]
        assert client.get(f"/api/v1/files/{file_id}").json()["sample_count"] == len(iq)
        segments = client.get(f"/api/v1/segments?raw_file_id={file_id}").json()
        assert segments["total"] == 3
        first = min(segments["items"], key=lambda row: row["start_sample"])
        segment_id = first["id"]
        detail = client.get(f"/api/v1/segments/{segment_id}").json()
        assert detail["processing_config"]["parameters"]["stride_samples"] == 64
        response = client.get(f"/api/v1/segments/{segment_id}/preview?max_points=32")
        assert response.status_code == 200, response.text
        preview = response.json()
        assert len(preview["i"]) == len(preview["q"]) == len(preview["frequency_offset_hz"]) == 32
        assert preview["i"][0] == float(iq[0].real)
        assert preview["reader_version"] == "iq-reader-v1"
        assert client.get(f"/api/v1/files/{file_id}/observations").json()["total"] == 1
        assert client.get("/api/v1/files?limit=101").status_code == 422
        assert client.get("/api/v1/files?status=made-up").status_code == 422
        assert (
            client.get(f"/api/v1/segments/{segment_id}/preview?max_points=2049").status_code == 422
        )
        assert client.get(f"/api/v1/files/{uuid.uuid4()}").status_code == 404
        assert client.get("/api/v1/files/not-a-uuid").status_code == 422


def test_preview_rejects_changed_source_and_metadata(asset_env) -> None:
    settings, engine, factory, service = asset_env
    path, metadata, _ = make_source(settings.data_root)
    file_id = uuid.UUID(service.scan().files[0]["id"])
    index_segments(settings, factory, file_id, SegmentConfig(window_samples=128))
    with user_client(settings, engine) as client:
        segment_id = client.get(f"/api/v1/segments?raw_file_id={file_id}").json()["items"][0]["id"]
        metadata["sample_rate_hz"] = 2.0
        write_metadata(path, metadata)
        response = client.get(f"/api/v1/segments/{segment_id}/preview")
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "SOURCE_CHANGED"
        metadata["sample_rate_hz"] = 1000.0
        write_metadata(path, metadata)
        path.write_bytes(path.read_bytes()[:-8])
        assert client.get(f"/api/v1/segments/{segment_id}/preview").status_code == 409
        assert str(settings.data_root) not in response.text


def test_database_enforces_immutable_identities_and_bounds(asset_env) -> None:
    settings, engine, factory, service = asset_env
    make_source(settings.data_root)
    file_id = uuid.UUID(service.scan().files[0]["id"])
    result = index_segments(settings, factory, file_id, SegmentConfig(window_samples=128))
    config_id = uuid.UUID(result["config_id"])
    statements = [
        ("UPDATE raw_files SET sha256 = :hash WHERE id = :id", {"hash": "a" * 64, "id": file_id}),
        ("DELETE FROM raw_files WHERE id = :id", {"id": file_id}),
        (
            "UPDATE processing_configs SET pipeline_version = 'changed' WHERE id = :id",
            {"id": config_id},
        ),
        ("UPDATE signal_segments SET start_sample = 1 WHERE raw_file_id = :id", {"id": file_id}),
        ("DELETE FROM file_observations WHERE raw_file_id = :id", {"id": file_id}),
        (
            "INSERT INTO signal_segments(id,raw_file_id,config_id,start_sample,sample_count) "
            "VALUES (:id,:raw,:config,250,128)",
            {"id": uuid.uuid4(), "raw": file_id, "config": config_id},
        ),
    ]
    for statement, parameters in statements:
        with pytest.raises(IntegrityError), engine.begin() as connection:
            connection.execute(text(statement), parameters)
    with factory() as session:
        assert session.get(RawFile, file_id).sha256 != "a" * 64
        assert (
            session.scalar(
                select(func.count())
                .select_from(SignalSegment)
                .where(SignalSegment.raw_file_id == file_id)
            )
            == 2
        )


def test_large_batch_rejected_without_partial_config_or_segments(asset_env) -> None:
    settings, _, factory, service = asset_env
    make_source(settings.data_root, samples=2002)
    file_id = uuid.UUID(service.scan().files[0]["id"])
    with pytest.raises(DomainError, match="1,000"):
        index_segments(settings, factory, file_id, SegmentConfig(window_samples=1))
    with factory() as session:
        assert (
            session.scalar(
                select(func.count())
                .select_from(SignalSegment)
                .where(SignalSegment.raw_file_id == file_id)
            )
            == 0
        )


def test_stale_hashed_observation_cannot_register(asset_env) -> None:
    settings, _, factory, service = asset_env
    path, _, _ = make_source(settings.data_root)
    observed = inspect_file(service.root, path.name)
    path.write_bytes(b"changed after hash")
    with pytest.raises(DomainError, match="identity changed"):
        service.register(observed)
    with factory() as session:
        assert (
            session.scalar(
                select(func.count())
                .select_from(RawFile)
                .where(RawFile.root_key == settings.data_root_key)
            )
            == 0
        )


def test_index_failure_rolls_back_config_and_all_segments(asset_env) -> None:
    settings, engine, factory, service = asset_env
    make_source(settings.data_root)
    file_id = uuid.UUID(service.scan().files[0]["id"])
    config = SegmentConfig(window_samples=123)
    config_hash = content_key(config.normalized())

    def interrupt(_connection, _cursor, statement, _parameters, _context, _executemany):
        if statement.startswith("INSERT INTO signal_segments"):
            raise RuntimeError("Injected failure after config insert, before segment commit")

    event.listen(engine, "before_cursor_execute", interrupt)
    try:
        with pytest.raises(RuntimeError, match="Injected failure"):
            index_segments(settings, factory, file_id, config)
    finally:
        event.remove(engine, "before_cursor_execute", interrupt)
    with factory() as session:
        assert (
            session.scalar(
                select(ProcessingConfig).where(ProcessingConfig.config_hash == config_hash)
            )
            is None
        )
        assert (
            session.scalar(
                select(func.count())
                .select_from(SignalSegment)
                .where(SignalSegment.raw_file_id == file_id)
            )
            == 0
        )


def test_removing_completion_marker_invalidates_registered_asset(asset_env) -> None:
    settings, _, factory, service = asset_env
    path, _, _ = make_source(settings.data_root)
    file_id = uuid.UUID(service.scan().files[0]["id"])
    path.with_name(path.name + ".done").unlink()
    report = service.scan()
    assert (report.skipped, report.invalidated) == (1, 1)
    with factory() as session:
        assert session.get(RawFile, file_id).status == "INVALID"


def test_database_allows_only_one_active_revision_per_path(asset_env) -> None:
    settings, engine, factory, service = asset_env
    make_source(settings.data_root)
    file_id = uuid.UUID(service.scan().files[0]["id"])
    with factory() as session:
        raw = session.get(RawFile, file_id)
        values = {column.name: getattr(raw, column.name) for column in RawFile.__table__.columns}
    values.update(id=uuid.uuid4(), sha256="f" * 64)
    with pytest.raises(IntegrityError), engine.begin() as connection:
        connection.execute(RawFile.__table__.insert().values(**values))


def test_preview_supports_same_file_through_another_os_mount(asset_env) -> None:
    settings, engine, factory, service = asset_env
    make_source(settings.data_root)
    file_id = uuid.UUID(service.scan().files[0]["id"])
    index_segments(settings, factory, file_id, SegmentConfig(window_samples=128))
    with factory.begin() as session:
        raw = session.get(RawFile, file_id)
        raw.stat_identity = {
            **raw.stat_identity,
            "st_dev": "other-device",
            "st_ino": "other-inode",
            "st_ctime_ns": "other-clock",
        }
    with user_client(settings, engine) as client:
        segment_id = client.get(f"/api/v1/segments?raw_file_id={file_id}").json()["items"][0]["id"]
        response = client.get(f"/api/v1/segments/{segment_id}/preview")
        assert response.status_code == 200, response.text
