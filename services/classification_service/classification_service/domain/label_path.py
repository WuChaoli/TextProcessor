from dataclasses import dataclass

from classification_service.domain.errors import DomainValidationError


@dataclass(frozen=True)
class TopTriplePath:
    """The exactly three ordered labels produced by the top-triple model."""

    levels: tuple[str, str, str]

    def __post_init__(self) -> None:
        if len(self.levels) != 3 or any(level == "" for level in self.levels):
            raise DomainValidationError(
                "top triple path must contain exactly three non-empty levels"
            )

    @classmethod
    def from_leaf_label(cls, label: str) -> "TopTriplePath":
        levels = label.split(" > ")
        if len(levels) != 3:
            raise DomainValidationError(
                "top triple label must contain exactly three non-empty levels"
            )
        return cls(levels=(levels[0], levels[1], levels[2]))
