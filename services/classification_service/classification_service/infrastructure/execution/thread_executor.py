from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import TypeVar

from classification_service.infrastructure.execution.admission_controller import (
    AdmissionController,
)

T = TypeVar("T")


class ThreadInferenceExecutor:
    """Run inference on one dedicated worker with bounded admission."""

    def __init__(self, *, waiting_limit: int = 8, timeout_seconds: float = 15.0):
        self._executor = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="classification-inference",
        )
        self._admission = AdmissionController(
            waiting_limit=waiting_limit,
            timeout_seconds=timeout_seconds,
        )

    async def run(self, operation: Callable[[], T]) -> T:
        return await self._admission.run(lambda: self._executor.submit(operation))

    def shutdown(self) -> None:
        self._executor.shutdown(wait=True, cancel_futures=False)
