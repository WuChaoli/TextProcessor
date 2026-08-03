import uuid
from datetime import datetime
from enum import StrEnum
from typing import Protocol


class MarkdownCleaningProcessingPhase(StrEnum):
    VALIDATING_INPUT = "validating_input"
    CLAIMING_TASK = "claiming_task"
    CLEANING = "cleaning"
    SAVING_PREPARED = "saving_prepared"
    PUBLISHING_RESULT = "publishing_result"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class MarkdownCleaningWorkerTask(Protocol):
    id: uuid.UUID
    lease_token: str | None
    processing_deadline: datetime | None
    target_path: str
    attempt_count: int
    max_attempts: int
    input_sha256: str | None
