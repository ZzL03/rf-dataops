from decimal import Decimal
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator, model_validator

from app.core.identity import PIPELINE_VERSION, READER_VERSION


class IQMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False, strict=True)

    sample_rate_hz: float = Field(gt=0)
    center_frequency_hz: float = Field(ge=0)


class BinMetadata(IQMetadata):
    format: Literal["BIN"]
    dtype: Literal["int16", "float32"]
    byte_order: Literal["little", "big"]
    interleaving: Literal["IQ"]


class Hdf5Metadata(IQMetadata):
    format: Literal["HDF5"]
    dataset_path: str = Field(min_length=2, max_length=512)

    @field_validator("dataset_path")
    @classmethod
    def validate_dataset_path(cls, value: str) -> str:
        parts = value.split("/")[1:]
        if not value.startswith("/") or any(p in {"", ".", ".."} for p in parts):
            raise ValueError("Use a canonical absolute HDF5 dataset path such as /iq")
        return value


Metadata = Annotated[BinMetadata | Hdf5Metadata, Field(discriminator="format")]
metadata_adapter = TypeAdapter(Metadata)


class SegmentConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False, strict=True)

    window_samples: int = Field(gt=0, le=1_000_000)
    stride_samples: int | None = Field(default=None, gt=0)
    overlap_ratio: float | None = Field(default=None, ge=0, lt=1)

    @model_validator(mode="after")
    def validate_stride(self) -> "SegmentConfig":
        if self.stride_samples is not None and self.overlap_ratio is not None:
            raise ValueError("Provide stride_samples or overlap_ratio, not both")
        if self.stride > self.window_samples:
            raise ValueError("stride_samples must not exceed window_samples")
        return self

    @property
    def stride(self) -> int:
        if self.stride_samples is not None:
            return self.stride_samples
        if self.overlap_ratio is None:
            return self.window_samples
        stride = Decimal(self.window_samples) * (1 - Decimal(str(self.overlap_ratio)))
        if stride < 1 or stride != stride.to_integral_value():
            raise ValueError("overlap_ratio must produce an integral, positive sample stride")
        return int(stride)

    def normalized(self) -> dict:
        return {
            "window_samples": self.window_samples,
            "stride_samples": self.stride,
            "reader_version": READER_VERSION,
            "pipeline_version": PIPELINE_VERSION,
            "tail_policy": "drop",
        }
