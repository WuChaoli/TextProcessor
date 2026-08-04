from typing import Protocol

from classification_service.domain.model_identity import ModelPrediction


class Classifier(Protocol):
    """Classify a fixed sequence of document chunks with one release model."""

    def predict(self, chunks: tuple[str, ...]) -> ModelPrediction: ...
