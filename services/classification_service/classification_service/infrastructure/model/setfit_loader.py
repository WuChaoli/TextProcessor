from collections.abc import Sequence
from pathlib import Path
from typing import Protocol, cast, runtime_checkable

import numpy as np

from classification_service.domain.model_identity import ModelPrediction
from classification_service.infrastructure.model.score_aggregator import (
    aggregate_scores,
)


class ModelLoadError(RuntimeError):
    """Stable startup error raised when a model runtime cannot become ready."""

    code = "MODEL_LOAD_FAILED"

    def __init__(self, *, stage: str, release_id: str) -> None:
        self.stage = stage
        self.release_id = release_id
        super().__init__(f"model runtime load failed during {stage}")


class SetFitModel(Protocol):
    def predict_proba(
        self, chunks: Sequence[str], *, as_numpy: bool = True
    ) -> object: ...


class SetFitModelFactory(Protocol):
    def from_pretrained(
        self,
        path: str,
        *,
        device: str,
        local_files_only: bool,
    ) -> object: ...


class SetFitModule(Protocol):
    SetFitModel: SetFitModelFactory


@runtime_checkable
class Detachable(Protocol):
    def detach(self) -> object: ...


@runtime_checkable
class CpuTransferable(Protocol):
    def cpu(self) -> object: ...


@runtime_checkable
class NumpyConvertible(Protocol):
    def numpy(self) -> object: ...


def load_setfit_model(module: object, path: Path, *, device: str) -> SetFitModel:
    """Load one trusted local SetFit model onto the required CUDA device."""
    factory = cast(SetFitModule, module).SetFitModel
    model = factory.from_pretrained(str(path), device=device, local_files_only=True)
    return cast(SetFitModel, model)


def _as_probability_matrix(value: object) -> np.ndarray:
    if isinstance(value, Detachable):
        value = value.detach()
    if isinstance(value, CpuTransferable):
        value = value.cpu()
    if isinstance(value, NumpyConvertible):
        value = value.numpy()
    return np.asarray(value, dtype=np.float64)


class SetFitClassifierAdapter:
    """Validate SetFit probability output before document-level aggregation."""

    def __init__(
        self,
        model: SetFitModel,
        labels: tuple[str, ...],
        *,
        expected_label_count: int,
    ) -> None:
        normalized_labels = tuple(labels)
        if len(normalized_labels) != expected_label_count:
            raise ValueError("labels do not match the classifier output size")
        self._model = model
        self._labels = normalized_labels
        self._expected_label_count = expected_label_count

    def predict(self, chunks: tuple[str, ...]) -> ModelPrediction:
        normalized_chunks = tuple(chunks)
        if not normalized_chunks:
            raise ValueError("classifier requires at least one chunk")
        raw_scores = self._model.predict_proba(list(normalized_chunks), as_numpy=True)
        scores = _as_probability_matrix(raw_scores)
        expected_shape = (len(normalized_chunks), self._expected_label_count)
        if scores.shape != expected_shape:
            raise ValueError("scores shape must match chunks and classifier labels")
        if not np.isfinite(scores).all():
            raise ValueError("scores must contain only finite values")
        if (scores < 0.0).any() or (scores > 1.0).any():
            raise ValueError("scores must be probabilities in [0, 1]")
        if not np.allclose(
            np.sum(scores, axis=1, dtype=np.float64),
            1.0,
            rtol=1e-5,
            atol=1e-6,
        ):
            raise ValueError("each probability row must sum to one")
        return aggregate_scores(scores, self._labels)
