from collections.abc import Callable
from typing import Protocol, TypeVar

T = TypeVar("T")


class InferenceCapacityExceeded(RuntimeError):
    """Raised when the active request and bounded waiting queue are full."""

    code = "INFERENCE_CAPACITY_EXCEEDED"


class InferenceExecutor(Protocol):
    """Run one complete blocking inference pipeline outside the event loop."""

    async def run(self, operation: Callable[[], T]) -> T: ...
