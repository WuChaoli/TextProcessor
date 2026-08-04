from dataclasses import dataclass
from math import isfinite

from classification_service.domain.errors import DomainValidationError

TOP_TRIPLE_CLASSIFIER_NAME = "top-triple-classifier"
END_DOC_CLASSIFIER_NAME = "end-doc-classifier"
CLASSIFIER_NAMES = frozenset({TOP_TRIPLE_CLASSIFIER_NAME, END_DOC_CLASSIFIER_NAME})


@dataclass(frozen=True)
class ModelPrediction:
    """A validated label and confidence emitted by one classifier."""

    label: str
    confidence: float

    def __post_init__(self) -> None:
        if not self.label:
            raise DomainValidationError("prediction label must not be empty")
        try:
            valid_confidence = (
                isfinite(self.confidence) and 0.0 <= self.confidence <= 1.0
            )
        except TypeError as error:
            raise DomainValidationError(
                "prediction confidence must be a finite number"
            ) from error
        if not valid_confidence:
            raise DomainValidationError(
                "prediction confidence must be finite and in [0, 1]"
            )


@dataclass(frozen=True)
class ModelIdentity:
    """The stable public identity of a classifier in a model release."""

    name: str
    release_id: str

    def __post_init__(self) -> None:
        if self.name not in CLASSIFIER_NAMES:
            raise DomainValidationError(
                "model identity name is not a supported classifier"
            )
        if not self.release_id:
            raise DomainValidationError("model identity fields must not be empty")
