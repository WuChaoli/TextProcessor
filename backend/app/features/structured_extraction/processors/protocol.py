from pathlib import Path
from typing import Protocol

from app.features.structured_extraction.worker_models import ProcessorArtifact


class DocumentProcessor(Protocol):
    def process(self, source: Path, destination: Path) -> ProcessorArtifact: ...
