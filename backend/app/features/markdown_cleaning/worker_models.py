from enum import StrEnum


class MarkdownCleaningProcessingPhase(StrEnum):
    VALIDATING_INPUT = "validating_input"
    CLAIMING_TASK = "claiming_task"
    CLEANING = "cleaning"
    SAVING_PREPARED = "saving_prepared"
    PUBLISHING_RESULT = "publishing_result"
    SUCCEEDED = "succeeded"
    FAILED = "failed"

