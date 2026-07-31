from enum import StrEnum


class GlobalDeduplicationApiErrorCode(StrEnum):
    INVALID_REQUEST = "INVALID_REQUEST"
    IDEMPOTENCY_CONFLICT = "IDEMPOTENCY_CONFLICT"
    TASK_NOT_FOUND = "TASK_NOT_FOUND"
    QUEUE_SUBMISSION_FAILED = "QUEUE_SUBMISSION_FAILED"
    INPUT_PATH_NOT_ALLOWED = "INPUT_PATH_NOT_ALLOWED"
    INPUT_URL_NOT_ALLOWED = "INPUT_URL_NOT_ALLOWED"
    OUTPUT_PATH_NOT_ALLOWED = "OUTPUT_PATH_NOT_ALLOWED"
    INTERNAL_ERROR = "INTERNAL_ERROR"


class GlobalDeduplicationDomainError(RuntimeError):
    def __init__(
        self,
        code: GlobalDeduplicationApiErrorCode,
        safe_message: str,
        *,
        http_status: int,
    ) -> None:
        super().__init__(f"{code}: {safe_message}")
        self.code = code
        self.safe_message = safe_message
        self.http_status = http_status
