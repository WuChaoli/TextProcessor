from enum import StrEnum


class ExtractionErrorCode(StrEnum):
    INVALID_REQUEST = "INVALID_REQUEST"
    INPUT_PATH_NOT_ALLOWED = "INPUT_PATH_NOT_ALLOWED"
    INPUT_URL_NOT_ALLOWED = "INPUT_URL_NOT_ALLOWED"
    OUTPUT_PATH_NOT_ALLOWED = "OUTPUT_PATH_NOT_ALLOWED"
    IDEMPOTENCY_CONFLICT = "IDEMPOTENCY_CONFLICT"
    QUEUE_SUBMISSION_FAILED = "QUEUE_SUBMISSION_FAILED"
    OUTPUT_CONFLICT = "OUTPUT_CONFLICT"
    INTERNAL_ERROR = "INTERNAL_ERROR"


class ExtractionDomainError(Exception):
    def __init__(
        self,
        code: ExtractionErrorCode,
        message: str,
        *,
        http_status: int,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.safe_message = message
        self.http_status = http_status

