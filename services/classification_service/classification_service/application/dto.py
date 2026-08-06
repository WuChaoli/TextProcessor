from dataclasses import dataclass


@dataclass(frozen=True)
class ClassifyTextCommand:
    """Validated caller context passed from the HTTP adapter to the use case."""

    request_id: str
    text: str
