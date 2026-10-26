import uuid
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator

Label = Literal["BPSK", "QPSK", "8PSK", "16QAM", "64QAM", "UNKNOWN"]
LABELS = ("BPSK", "QPSK", "8PSK", "16QAM", "64QAM")


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    username: str = Field(min_length=1, max_length=64)
    password: SecretStr = Field(min_length=1, max_length=128)


class ModelRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(default="mock-modulation", pattern=r"^[a-zA-Z0-9_.-]{1,64}$")
    version: str = Field(default="mock-v1", pattern=r"^[a-zA-Z0-9_.-]{1,64}$")


class AnnotationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: int = Field(ge=0)
    label: Label
    action: Literal["CONFIRM", "CORRECT", "UNKNOWN"]
    comment: str = Field(default="", max_length=2000)
    prediction_id: uuid.UUID | None = None

    @model_validator(mode="after")
    def consistent_action(self):
        if (self.action == "UNKNOWN") != (self.label == "UNKNOWN"):
            raise ValueError("UNKNOWN action and label must be used together")
        if self.action == "CONFIRM" and self.prediction_id is None:
            raise ValueError("Confirmation requires a prediction reference")
        return self
