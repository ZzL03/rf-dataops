"""Explicit inference contract. Mock scores are synthetic and carry no accuracy claim."""

import hashlib
import time
from dataclasses import dataclass
from typing import Protocol

import numpy as np

from app.core.errors import DomainError
from app.schemas.labeling import LABELS
from app.services.storage import ControlledRoot

PREPROCESS_VERSION = "iq-rms-v1"


@dataclass(frozen=True)
class InferenceResult:
    label: str
    scores: dict[str, float]
    confidence: float
    inference_ms: float
    is_mock: bool

    def semantic(self) -> dict:
        return {
            "label": self.label,
            "scores": self.scores,
            "confidence": self.confidence,
            "is_mock": self.is_mock,
        }


class ModelAdapter(Protocol):
    def predict(self, iq: np.ndarray) -> InferenceResult: ...


def normalize_iq(iq):
    if iq.ndim != 1 or len(iq) == 0 or len(iq) > 1_000_000 or not np.isfinite(iq).all():
        raise DomainError(
            "INVALID_MODEL_INPUT", "Inference requires bounded finite complex IQ", 409
        )
    values = np.stack((iq.real, iq.imag)).astype(np.float32)
    rms = float(np.sqrt(np.mean(np.abs(iq.astype(np.complex128)) ** 2)))
    if rms > 0:
        values = (values.astype(np.float64) / rms).astype(np.float32)
    if not np.isfinite(values).all():
        raise DomainError("INVALID_MODEL_INPUT", "Normalized IQ is not finite", 409)
    return values[None, :, :]


def finish(scores, elapsed, is_mock, labels):
    values = np.asarray(scores, dtype=np.float64)
    if (
        values.shape != (len(labels),)
        or not np.isfinite(values).all()
        or np.any(values < 0)
        or not np.isclose(values.sum(), 1)
    ):
        raise DomainError(
            "INVALID_MODEL_OUTPUT", "Model must return finite normalized class scores", 409
        )
    index = int(np.argmax(values))
    return InferenceResult(
        labels[index],
        dict(zip(labels, map(float, values), strict=True)),
        float(values[index]),
        elapsed,
        is_mock,
    )


class MockModel:
    def predict(self, iq):
        start = time.perf_counter()
        values = normalize_iq(iq)
        seed = hashlib.sha256(values.astype("<f4").tobytes()).digest()
        scores = np.frombuffer(seed[: len(LABELS)], dtype=np.uint8).astype(np.float64) + 1
        scores /= scores.sum()
        return finish(scores, (time.perf_counter() - start) * 1000, True, LABELS)


class TorchScriptModel:
    """CPU TorchScript logits [1,C], input float32 [1,2,N]. Operator-trusted artifacts only."""

    def __init__(self, settings, model):
        self.root, self.model = ControlledRoot(settings.model_root), model

    def predict(self, iq):
        config = self.model.config
        if config.get("preprocessing") != PREPROCESS_VERSION or len(iq) != config.get(
            "sample_count"
        ):
            raise DomainError(
                "MODEL_INPUT_MISMATCH", "Model preprocessing or sample length differs", 409
            )
        try:
            import torch
        except ImportError as exc:
            raise DomainError(
                "MODEL_RUNTIME_UNAVAILABLE",
                "Install the optional PyTorch CPU runtime for this adapter",
                409,
            ) from exc
        start = time.perf_counter()
        inputs = normalize_iq(iq)
        # Hash and load the SAME open artifact, avoiding path replacement between the two.
        with self.root.open_read(config["artifact_path"]) as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
            if digest != self.model.artifact_sha256:
                raise DomainError("MODEL_ARTIFACT_CHANGED", "Model artifact hash changed", 409)
            stream.seek(0)
            network = torch.jit.load(stream, map_location="cpu").eval()
        with torch.inference_mode():
            logits = network(torch.from_numpy(inputs))
            if not isinstance(logits, torch.Tensor) or tuple(logits.shape) != (
                1,
                len(config["labels"]),
            ):
                raise DomainError(
                    "INVALID_MODEL_OUTPUT", "Expected logits with shape [1,classes]", 409
                )
            scores = torch.softmax(logits.to(dtype=torch.float64), dim=1)[0].cpu().numpy()
        return finish(scores, (time.perf_counter() - start) * 1000, False, config["labels"])


def adapter_for(settings, model) -> ModelAdapter:
    if model.adapter == "mock" and model.config == {
        "labels": list(LABELS),
        "preprocessing": PREPROCESS_VERSION,
    }:
        return MockModel()
    if model.adapter == "torchscript":
        return TorchScriptModel(settings, model)
    raise DomainError(
        "MODEL_ADAPTER_UNAVAILABLE", "Unsupported model adapter or configuration", 409
    )
