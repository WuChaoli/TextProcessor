from pathlib import Path
from typing import Protocol

from app.features.structured_extraction.worker_models import (
    ExternalTaskStatus,
    ExternalTaskSubmission,
    ProcessingContext,
    ProcessorArtifact,
)


class ExternalProcessorAdapter(Protocol):
    def submit(
        self,
        source: Path,
        context: ProcessingContext,
    ) -> ExternalTaskSubmission: ...

    def get_status(self, external_task_id: str) -> ExternalTaskStatus: ...

    def fetch_result(
        self,
        external_task_id: str,
        destination: Path,
    ) -> ProcessorArtifact: ...
