from enum import StrEnum


class MarkdownCleaningApiErrorCode(StrEnum):
    IDEMPOTENCY_CONFLICT = "IDEMPOTENCY_CONFLICT"
    INPUT_PATH_NOT_ALLOWED = "INPUT_PATH_NOT_ALLOWED"
    INPUT_URL_NOT_ALLOWED = "INPUT_URL_NOT_ALLOWED"
    OUTPUT_PATH_NOT_ALLOWED = "OUTPUT_PATH_NOT_ALLOWED"
    QUEUE_SUBMISSION_FAILED = "QUEUE_SUBMISSION_FAILED"
    TASK_NOT_FOUND = "TASK_NOT_FOUND"


class MarkdownCleaningDomainError(RuntimeError):
    def __init__(
        self,
        code: MarkdownCleaningApiErrorCode,
        safe_message: str,
        *,
        http_status: int,
    ) -> None:
        super().__init__(f"{code}: {safe_message}")
        self.code = code
        self.safe_message = safe_message
        self.http_status = http_status
