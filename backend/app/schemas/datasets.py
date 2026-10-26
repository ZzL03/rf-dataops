import uuid
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.schemas.labeling import Label


class DatasetRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=100)
    description: str = Field(default="", max_length=2000)

    @field_validator("name")
    @classmethod
    def clean_name(cls, value):
        value = value.strip()
        if not value or any(ord(char) < 32 for char in value):
            raise ValueError("Dataset name must be nonempty without control characters")
        return value


class Selection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    raw_file_ids: list[uuid.UUID] = Field(default_factory=list, max_length=100)
    segment_ids: list[uuid.UUID] = Field(default_factory=list, max_length=10000)
    labels: list[Label] = Field(default_factory=list, max_length=6)
    review_status: Literal["REVIEWED", "PENDING", "UNKNOWN"] | None = "REVIEWED"

    @field_validator("raw_file_ids", "segment_ids", "labels")
    @classmethod
    def unique_sorted(cls, values):
        return sorted(set(values))


class SelectionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    selection: Selection = Field(default_factory=Selection)
    label_policy: Literal["HUMAN_ONLY", "MODEL_ALLOWED"] = "HUMAN_ONLY"
    model_version_id: uuid.UUID | None = None
    allow_mock_predictions: bool = False

    @model_validator(mode="after")
    def explicit_model(self):
        if (self.label_policy == "MODEL_ALLOWED") != (self.model_version_id is not None):
            raise ValueError("MODEL_ALLOWED requires a model version; HUMAN_ONLY has none")
        if self.allow_mock_predictions and self.label_policy != "MODEL_ALLOWED":
            raise ValueError("Mock acknowledgement only applies to MODEL_ALLOWED")
        return self


class PublicationRequest(SelectionRequest):
    version: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
    expected_selection_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
