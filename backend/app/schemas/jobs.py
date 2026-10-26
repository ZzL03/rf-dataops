import uuid
from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.datasets import PublicationRequest
from app.schemas.iq import SegmentConfig

JobStatus = Literal["PENDING", "RUNNING", "RETRY_WAIT", "SUCCEEDED", "FAILED"]
JobType = Literal["SCAN", "SEGMENT", "QUALITY", "INFER", "PUBLISH"]


class JobOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")
    priority: int = Field(default=0, ge=-100, le=100)
    max_attempts: int = Field(default=3, ge=1, le=10)


class ScanJobRequest(JobOptions):
    job_type: Literal["SCAN"] = "SCAN"
    directory: str = "."


class SegmentJobRequest(JobOptions):
    job_type: Literal["SEGMENT"] = "SEGMENT"
    raw_file_id: uuid.UUID
    config: SegmentConfig
    start: int = Field(default=0, ge=0)
    end: int | None = Field(default=None, gt=0)


class QualityJobRequest(JobOptions):
    job_type: Literal["QUALITY"] = "QUALITY"
    segment_id: uuid.UUID


class InferenceJobRequest(JobOptions):
    job_type: Literal["INFER"] = "INFER"
    segment_id: uuid.UUID
    model_version_id: uuid.UUID


class PublishJobRequest(JobOptions):
    job_type: Literal["PUBLISH"] = "PUBLISH"
    dataset_id: uuid.UUID
    publication: PublicationRequest


JobSubmission = Annotated[
    ScanJobRequest
    | SegmentJobRequest
    | QualityJobRequest
    | InferenceJobRequest
    | PublishJobRequest,
    Field(discriminator="job_type"),
]


class RerunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    max_attempts: int | None = Field(default=None, ge=1, le=10)


class JobOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    job_type: JobType
    root_key: str
    target_identity: str
    raw_file_id: uuid.UUID | None
    segment_id: uuid.UUID | None
    model_version_id: uuid.UUID | None
    dataset_id: uuid.UUID | None
    payload: dict
    generation: int
    parent_job_id: uuid.UUID | None
    status: JobStatus
    priority: int
    attempts: int
    max_attempts: int
    worker_id: str | None
    execution_version: int
    lease_until: datetime | None
    next_run_at: datetime
    last_error: str | None
    result: dict | None
    result_digest: str | None
    created_at: datetime
    updated_at: datetime


class JobEventOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    job_id: uuid.UUID
    event: str
    worker_id: str | None
    execution_version: int
    detail: dict
    created_at: datetime
