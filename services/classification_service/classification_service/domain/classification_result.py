from dataclasses import dataclass
from typing import Self

from classification_service.domain.errors import DomainValidationError
from classification_service.domain.label_path import TopTriplePath
from classification_service.domain.model_identity import ModelPrediction


@dataclass(frozen=True)
class ClassificationResult:
    """The fixed four-tag result composed from the two model predictions."""

    tags: tuple[str, str, str, str]
    top_triple_confidence: float
    end_doc_confidence: float
    release_id: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "tags", tuple(self.tags))
        if len(self.tags) != 4 or any(tag == "" for tag in self.tags):
            raise DomainValidationError("classification result must contain four non-empty tags")
        if not self.release_id:
            raise DomainValidationError("release id must not be empty")
        ModelPrediction(self.tags[0], self.top_triple_confidence)
        ModelPrediction(self.tags[3], self.end_doc_confidence)

    @classmethod
    def compose(
        cls,
        *,
        top_triple: ModelPrediction,
        end_doc: ModelPrediction,
        release_id: str,
    ) -> Self:
        path = TopTriplePath.from_leaf_label(top_triple.label)
        return cls(
            tags=(*path.levels, end_doc.label),
            top_triple_confidence=top_triple.confidence,
            end_doc_confidence=end_doc.confidence,
            release_id=release_id,
        )
