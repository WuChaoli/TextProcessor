from dataclasses import dataclass

from classification_service.application.ports.inference_executor import (
    InferenceAdmissionClosed,
    InferenceCapacityExceeded,
)
from classification_service.domain.errors import DomainValidationError


class CudaOutOfMemoryError(RuntimeError):
    """Stable infrastructure-neutral signal for a fatal CUDA OOM."""


def is_cuda_out_of_memory(error: BaseException) -> bool:
    error_type = type(error)
    return isinstance(error, CudaOutOfMemoryError) or (
        error_type.__name__ == "OutOfMemoryError"
        and error_type.__module__.startswith("torch")
    )


@dataclass(frozen=True)
class PublicError:
    status_code: int
    code: str
    message: str


def map_public_error(error: BaseException) -> PublicError:
    if isinstance(error, InferenceCapacityExceeded):
        return PublicError(429, "CAPACITY_EXCEEDED", "service capacity exceeded")
    if isinstance(error, TimeoutError):
        return PublicError(504, "INFERENCE_TIMEOUT", "inference timed out")
    if isinstance(error, InferenceAdmissionClosed) or is_cuda_out_of_memory(error):
        return PublicError(503, "SERVICE_UNAVAILABLE", "service unavailable")
    if isinstance(error, DomainValidationError):
        return PublicError(400, "INVALID_REQUEST", "request is invalid")
    return PublicError(500, "INTERNAL_ERROR", "internal service error")
