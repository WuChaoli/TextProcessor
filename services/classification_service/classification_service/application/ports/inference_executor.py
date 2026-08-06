from collections.abc import Callable
from typing import Protocol, TypeVar

T = TypeVar("T")


class InferenceCapacityExceeded(RuntimeError):
    """Raised when the active request and bounded waiting queue are full."""

    code = "INFERENCE_CAPACITY_EXCEEDED"


class InferenceAdmissionClosed(RuntimeError):
    """Raised when inference admission has stopped for service shutdown."""

    code = "INFERENCE_ADMISSION_CLOSED"


class InferenceExecutor(Protocol):
    """Run one complete blocking inference pipeline outside the event loop."""

    async def run(self, operation: Callable[[], T]) -> T: ...
