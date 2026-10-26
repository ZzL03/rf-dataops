import hashlib
import os
import uuid
from dataclasses import dataclass, field
from pathlib import PurePosixPath

from pydantic import ValidationError
from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings
from app.core.errors import DomainError
from app.core.identity import content_key
from app.models import FileObservation, RawFile
from app.schemas.iq import Metadata, metadata_adapter
from app.services.storage import ControlledRoot, normalize_path, signature
from app.signal_processing.readers import open_reader


@dataclass(frozen=True)
class Inspection:
    relative_path: str
    metadata: Metadata
    sha256: str
    byte_size: int
    sample_count: int
    stat_identity: dict[str, str]

    @property
    def metadata_hash(self) -> str:
        return content_key(self.metadata.model_dump(mode="json"))


def ensure_complete(root: ControlledRoot, path: str) -> None:
    try:
        root.read_small(path + ".done", 1024)
    except DomainError as exc:
        if exc.code == "SOURCE_MISSING":
            raise DomainError("INCOMPLETE", "Completion marker is missing", 409) from exc
        raise


def read_metadata(root: ControlledRoot, path: str) -> Metadata:
    raw = root.read_small(path + ".iq.json")
    try:
        metadata = metadata_adapter.validate_json(raw)
    except ValidationError as exc:
        raise DomainError(
            "METADATA_INVALID", "Explicit IQ metadata is invalid or unsupported"
        ) from exc
    suffix = PurePosixPath(path).suffix.lower()
    if (metadata.format == "BIN" and suffix != ".bin") or (
        metadata.format == "HDF5" and suffix not in {".h5", ".hdf5"}
    ):
        raise DomainError("FORMAT_MISMATCH", "Explicit format does not match the file extension")
    return metadata


def inspect_file(root: ControlledRoot, path: str) -> Inspection:
    """Hash in bounded blocks and validate layout outside any database transaction."""
    path = normalize_path(path)
    ensure_complete(root, path)
    metadata = read_metadata(root, path)
    with root.open_read(path) as stream:
        before = os.fstat(stream.fileno())
        digest = hashlib.sha256()
        while block := stream.read(8 * 1024 * 1024):
            digest.update(block)
        with open_reader(stream, metadata) as reader:
            samples = reader.sample_count
        if signature(before) != signature(os.fstat(stream.fileno())):
            raise DomainError("SOURCE_CHANGED", "Source changed during inspection", 409)
    result = Inspection(
        path, metadata, digest.hexdigest(), before.st_size, samples, signature(before)
    )
    verify_observation(root, result)
    return result


def verify_observation(root: ControlledRoot, observed: Inspection) -> None:
    """Cheap final guard; the acquisition-side immutability contract is still required."""
    ensure_complete(root, observed.relative_path)
    if read_metadata(root, observed.relative_path) != observed.metadata:
        raise DomainError("SOURCE_CHANGED", "IQ interpretation changed during inspection", 409)
    with root.open_read(observed.relative_path) as stream:
        if signature(os.fstat(stream.fileno())) != observed.stat_identity:
            raise DomainError("SOURCE_CHANGED", "Source identity changed during inspection", 409)


def lock_path(session: Session, root_key: str, path: str) -> None:
    key = content_key({"root": root_key, "path": path})
    session.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"), {"key": key})


@dataclass
class ScanReport:
    registered: int = 0
    unchanged: int = 0
    skipped: int = 0
    invalidated: int = 0
    files: list[dict] = field(default_factory=list)
    errors: list[dict] = field(default_factory=list)


class IngestionService:
    def __init__(self, settings: Settings, factory: sessionmaker[Session]) -> None:
        self.settings = settings
        self.factory = factory
        self.root = ControlledRoot(settings.data_root)

    def register(self, observed: Inspection) -> tuple[uuid.UUID, bool]:
        with self.factory.begin() as session:
            return self.register_in_session(session, observed)

    def register_in_session(self, session: Session, observed: Inspection) -> tuple[uuid.UUID, bool]:
        """Caller owns the transaction, including any job execution fence."""
        key = self.settings.data_root_key
        lock_path(session, key, observed.relative_path)
        verify_observation(self.root, observed)
        prior = list(
            session.scalars(
                select(RawFile).where(
                    RawFile.root_key == key, RawFile.relative_path == observed.relative_path
                )
            )
        )
        existing = next(
            (
                row
                for row in prior
                if row.sha256 == observed.sha256 and row.metadata_hash == observed.metadata_hash
            ),
            None,
        )
        event = (
            "REGISTERED"
            if existing is None
            else ("VERIFIED" if existing.status == "ACTIVE" else "REACTIVATED")
        )
        for row in prior:
            if row.status == "ACTIVE" and row is not existing:
                row.status = "STALE"
                session.add(FileObservation(raw_file_id=row.id, event="CHANGED"))
        # Explicit flush because the session factory intentionally disables autoflush.
        session.flush()
        statement = insert(RawFile).values(
            id=uuid.uuid4(),
            root_key=key,
            relative_path=observed.relative_path,
            sha256=observed.sha256,
            metadata_hash=observed.metadata_hash,
            byte_size=observed.byte_size,
            mtime_ns=int(observed.stat_identity["st_mtime_ns"]),
            stat_identity=observed.stat_identity,
            format=observed.metadata.format,
            iq_metadata=observed.metadata.model_dump(mode="json"),
            sample_count=observed.sample_count,
            status="ACTIVE",
        )
        statement = statement.on_conflict_do_update(
            constraint="uq_raw_identity",
            set_={
                "mtime_ns": statement.excluded.mtime_ns,
                "stat_identity": statement.excluded.stat_identity,
                "status": "ACTIVE",
                "observed_at": func.clock_timestamp(),
            },
        ).returning(RawFile.id)
        file_id = session.scalar(statement)
        session.add(FileObservation(raw_file_id=file_id, event=event))
        return file_id, existing is None

    def invalidate(self, path: str, reason: str, *, missing_only: bool = False) -> int:
        with self.factory.begin() as session:
            return self.invalidate_in_session(session, path, reason, missing_only=missing_only)

    def invalidate_in_session(
        self, session: Session, path: str, reason: str, *, missing_only: bool = False
    ) -> int:
        """Append invalidation observations in the caller's transaction."""
        lock_path(session, self.settings.data_root_key, path)
        if missing_only:
            try:
                with self.root.open_read(path):
                    return 0
            except DomainError as exc:
                if exc.code != "SOURCE_MISSING":
                    return 0
        rows = list(
            session.scalars(
                select(RawFile).where(
                    RawFile.root_key == self.settings.data_root_key,
                    RawFile.relative_path == path,
                    RawFile.status == "ACTIVE",
                )
            )
        )
        for row in rows:
            row.status = "INVALID"
            session.add(FileObservation(raw_file_id=row.id, event=reason[:40]))
        return len(rows)

    def scan(self, directory: str = ".") -> ScanReport:
        directory = normalize_path(directory, directory=True)
        paths = self.root.candidates(directory, self.settings.max_scan_files)
        report = ScanReport()
        for path in paths:
            try:
                observed = inspect_file(self.root, path)
                file_id, created = self.register(observed)
                report.registered += int(created)
                report.unchanged += int(not created)
                report.files.append({"path": path, "id": str(file_id), "sha256": observed.sha256})
            except DomainError as exc:
                report.invalidated += self.invalidate(path, exc.code)
                if exc.code == "INCOMPLETE":
                    report.skipped += 1
                else:
                    report.errors.append({"path": path, "code": exc.code, "message": exc.message})
        # Only a fully enumerated subtree is eligible for missing-file reconciliation.
        with self.factory() as session:
            query = select(RawFile.relative_path).where(
                RawFile.root_key == self.settings.data_root_key, RawFile.status == "ACTIVE"
            )
            if directory != ".":
                query = query.where(
                    RawFile.relative_path.startswith(directory + "/", autoescape=True)
                )
            known = set(session.scalars(query))
        for path in known.difference(paths):
            report.invalidated += self.invalidate(path, "SOURCE_MISSING", missing_only=True)
        return report
