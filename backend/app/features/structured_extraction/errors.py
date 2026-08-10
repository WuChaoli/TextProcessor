from enum import StrEnum


class ExtractionErrorCode(StrEnum):
    INVALID_REQUEST = "INVALID_REQUEST"
    INPUT_PATH_NOT_ALLOWED = "INPUT_PATH_NOT_ALLOWED"
    INPUT_URL_NOT_ALLOWED = "INPUT_URL_NOT_ALLOWED"
    INPUT_NOT_FOUND = "INPUT_NOT_FOUND"
    INPUT_ACCESS_FAILED = "INPUT_ACCESS_FAILED"
    INPUT_TOO_LARGE = "INPUT_TOO_LARGE"
    UNSUPPORTED_INPUT_FORMAT = "UNSUPPORTED_INPUT_FORMAT"
    OUTPUT_PATH_NOT_ALLOWED = "OUTPUT_PATH_NOT_ALLOWED"
    OUTPUT_ACCESS_FAILED = "OUTPUT_ACCESS_FAILED"
    IDEMPOTENCY_CONFLICT = "IDEMPOTENCY_CONFLICT"
    QUEUE_SUBMISSION_FAILED = "QUEUE_SUBMISSION_FAILED"
    OUTPUT_CONFLICT = "OUTPUT_CONFLICT"
    TASK_NOT_FOUND = "TASK_NOT_FOUND"
    PROCESSING_FAILED = "PROCESSING_FAILED"
    PROCESSING_TIMEOUT = "PROCESSING_TIMEOUT"
    PROCESSOR_SUBMISSION_UNCERTAIN = "PROCESSOR_SUBMISSION_UNCERTAIN"
    INVALID_PROCESSOR_OUTPUT = "INVALID_PROCESSOR_OUTPUT"
    OUTPUT_WRITE_FAILED = "OUTPUT_WRITE_FAILED"
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


class ExtractionProcessingError(ExtractionDomainError):
    def __init__(
        self,
        code: ExtractionErrorCode,
        message: str,
        *,
        transient: bool = False,
        external_task_id: str | None = None,
    ) -> None:
        super().__init__(code, message, http_status=500)
        self.transient = transient
        self.external_task_id = external_task_id
