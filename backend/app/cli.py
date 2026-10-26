import argparse
import json
import logging
import uuid

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from app.core.config import Settings
from app.core.errors import DomainError
from app.db.session import build_engine, build_session_factory
from app.models import ProcessingJob
from app.schemas.iq import SegmentConfig
from app.schemas.jobs import (
    InferenceJobRequest,
    JobOut,
    QualityJobRequest,
    ScanJobRequest,
    SegmentJobRequest,
)
from app.services.job_queue import JobQueue


def main() -> int:
    parser = argparse.ArgumentParser(description="RF-DataOps persistent job submission CLI")
    commands = parser.add_subparsers(dest="command", required=True)
    scan = commands.add_parser("scan")
    scan.add_argument("--directory", default=".")
    segment = commands.add_parser("segment")
    segment.add_argument("--file-id", type=uuid.UUID, required=True)
    segment.add_argument("--window", type=int, required=True)
    stride = segment.add_mutually_exclusive_group()
    stride.add_argument("--stride", type=int)
    stride.add_argument("--overlap", type=float)
    segment.add_argument("--start", type=int, default=0)
    segment.add_argument("--end", type=int)
    quality = commands.add_parser("quality")
    quality.add_argument("--segment-id", type=uuid.UUID, required=True)
    infer = commands.add_parser("infer")
    infer.add_argument("--segment-id", type=uuid.UUID, required=True)
    infer.add_argument("--model-id", type=uuid.UUID, required=True)
    for command in (scan, segment, quality, infer):
        command.add_argument("--request-key", default=None)
        command.add_argument("--priority", type=int, default=0)
        command.add_argument("--max-attempts", type=int, default=3)
    jobs = commands.add_parser("jobs")
    jobs.add_argument("--job-id", type=uuid.UUID)
    rerun = commands.add_parser("rerun")
    rerun.add_argument("--job-id", type=uuid.UUID, required=True)
    rerun.add_argument("--request-key", default=None)
    rerun.add_argument("--max-attempts", type=int, choices=range(1, 11))
    args = parser.parse_args()
    engine = None
    try:
        settings = Settings()
        engine = build_engine(settings)
        factory = build_session_factory(engine)
        queue = JobQueue(settings, factory)
        if args.command == "jobs":
            with factory() as session:
                query = select(ProcessingJob).where(
                    ProcessingJob.root_key == settings.data_root_key
                )
                if args.job_id:
                    query = query.where(ProcessingJob.id == args.job_id)
                rows = session.scalars(query.order_by(ProcessingJob.created_at.desc()).limit(20))
                print(
                    json.dumps(
                        {
                            "items": [
                                JobOut.model_validate(row).model_dump(mode="json") for row in rows
                            ]
                        }
                    )
                )
            return 0
        key = args.request_key or str(uuid.uuid4())
        if args.command == "rerun":
            job = queue.rerun(args.job_id, key, args.max_attempts)
            print(JobOut.model_validate(job).model_dump_json())
            return 0
        options = {"priority": args.priority, "max_attempts": args.max_attempts}
        if args.command == "scan":
            submission = ScanJobRequest(directory=args.directory, **options)
        elif args.command == "segment":
            submission = SegmentJobRequest(
                raw_file_id=args.file_id,
                start=args.start,
                end=args.end,
                config=SegmentConfig(
                    window_samples=args.window,
                    stride_samples=args.stride,
                    overlap_ratio=args.overlap,
                ),
                **options,
            )
        elif args.command == "infer":
            submission = InferenceJobRequest(
                segment_id=args.segment_id, model_version_id=args.model_id, **options
            )
        else:
            submission = QualityJobRequest(segment_id=args.segment_id, **options)
        job = queue.submit(submission, key)
        print(JobOut.model_validate(job).model_dump_json())
        return 0
    except DomainError as exc:
        print(json.dumps({"error": {"code": exc.code, "message": exc.message}}))
        return 2
    except ValidationError:
        print(
            json.dumps(
                {
                    "error": {
                        "code": "INVALID_PARAMETERS",
                        "message": "Invalid configuration or IQ parameters",
                    }
                }
            )
        )
        return 2
    except SQLAlchemyError as exc:
        logging.error("Database operation failed: %s", type(exc).__name__)
        print(
            json.dumps(
                {
                    "error": {
                        "code": "DATABASE_ERROR",
                        "message": "Database operation failed; check migrations and connectivity",
                    }
                }
            )
        )
        return 3
    finally:
        if engine is not None:
            engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
