from dataclasses import dataclass
from math import isfinite

from classification_service.domain.errors import DomainValidationError


@dataclass(frozen=True)
class ModelPrediction:
    """A validated label and confidence emitted by one classifier."""

    label: str
    confidence: float

    def __post_init__(self) -> None:
        if not self.label:
            raise DomainValidationError("prediction label must not be empty")
        try:
            valid_confidence = isfinite(self.confidence) and 0.0 <= self.confidence <= 1.0
        except TypeError as error:
            raise DomainValidationError("prediction confidence must be a finite number") from error
        if not valid_confidence:
            raise DomainValidationError("prediction confidence must be finite and in [0, 1]")


@dataclass(frozen=True)
class ModelIdentity:
    """The stable public identity of a classifier in a model release."""

    name: str
    release_id: str

    def __post_init__(self) -> None:
        if not self.name or not self.release_id:
            raise DomainValidationError("model identity fields must not be empty")
