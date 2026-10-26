import uuid
from datetime import datetime
from typing import Generic, Literal, TypeVar

from pydantic import BaseModel, ConfigDict

T = TypeVar("T")


class Page(BaseModel, Generic[T]):
    items: list[T]
    total: int
    limit: int
    offset: int


class FileOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    root_key: str
    relative_path: str
    sha256: str
    metadata_hash: str
    byte_size: int
    format: Literal["BIN", "HDF5"]
    iq_metadata: dict
    sample_count: int
    status: Literal["ACTIVE", "STALE", "INVALID"]
    segment_count: int = 0
    created_at: datetime
    observed_at: datetime


class ConfigOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    config_hash: str
    parameters: dict
    reader_version: str
    pipeline_version: str
    created_at: datetime


class SegmentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    raw_file_id: uuid.UUID
    config_id: uuid.UUID
    start_sample: int
    sample_count: int
    quality: dict | None
    created_at: datetime


class SegmentDetail(SegmentOut):
    processing_config: ConfigOut


class ObservationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    raw_file_id: uuid.UUID
    event: str
    created_at: datetime


class PreviewOut(BaseModel):
    model_config = ConfigDict(allow_inf_nan=False)

    segment_id: uuid.UUID
    start_sample: int
    sample_count: int
    sample_rate_hz: float
    center_frequency_hz: float
    reader_version: str
    sample_indices: list[int]
    i: list[float]
    q: list[float]
    frequency_offset_hz: list[float]
    power_db: list[float]
    stats: dict[str, float]
    fft_samples: int
    power_reference: str
