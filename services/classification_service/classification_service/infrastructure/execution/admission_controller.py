import asyncio
from collections import deque
from collections.abc import Callable
from concurrent.futures import Future
from dataclasses import dataclass
from typing import Literal

from classification_service.application.ports.inference_executor import (
    InferenceCapacityExceeded,
)

RequestState = Literal["waiting", "active", "finished"]


@dataclass
class _AdmissionRequest[T]:
    submit: Callable[[], Future[T]]
    response: asyncio.Future[T]
    deadline: float
    loop: asyncio.AbstractEventLoop
    state: RequestState = "waiting"


class AdmissionController:
    """Allow one active inference plus a bounded FIFO waiting queue."""

    def __init__(self, *, waiting_limit: int = 8, timeout_seconds: float = 15.0):
        self._waiting_limit = waiting_limit
        self._timeout_seconds = timeout_seconds
        self._active: _AdmissionRequest[object] | None = None
        self._waiting: deque[_AdmissionRequest[object]] = deque()

    async def run[T](self, submit: Callable[[], Future[T]]) -> T:
        loop = asyncio.get_running_loop()
        request = _AdmissionRequest(
            submit=submit,
            response=loop.create_future(),
            deadline=loop.time() + self._timeout_seconds,
            loop=loop,
        )
        erased_request: _AdmissionRequest[object] = request

        if self._active is None:
            self._start(erased_request)
        elif len(self._waiting) < self._waiting_limit:
            self._waiting.append(erased_request)
        else:
            raise InferenceCapacityExceeded

        try:
            remaining = max(0.0, request.deadline - loop.time())
            return await asyncio.wait_for(
                asyncio.shield(request.response), timeout=remaining
            )
        except (TimeoutError, asyncio.CancelledError):
            self._abandon(erased_request)
            raise

    def _start(self, request: _AdmissionRequest[object]) -> None:
        if request.loop.time() >= request.deadline:
            request.state = "finished"
            if not request.response.done():
                request.response.set_exception(TimeoutError())
            self._start_next()
            return

        request.state = "active"
        self._active = request
        try:
            running = request.submit()
        except BaseException as error:
            if not request.response.done():
                request.response.set_exception(error)
            self._finish(request)
            return

        def notify(finished: Future[object]) -> None:
            try:
                request.loop.call_soon_threadsafe(self._complete, request, finished)
            except RuntimeError:
                return

        running.add_done_callback(notify)

    def _complete(
        self, request: _AdmissionRequest[object], finished: Future[object]
    ) -> None:
        try:
            result = finished.result()
        except BaseException as error:
            if not request.response.done():
                request.response.set_exception(error)
        else:
            if not request.response.done():
                request.response.set_result(result)
        finally:
            self._finish(request)

    def _finish(self, request: _AdmissionRequest[object]) -> None:
        if self._active is not request:
            return
        request.state = "finished"
        self._active = None
        self._start_next()

    def _start_next(self) -> None:
        while self._active is None and self._waiting:
            request = self._waiting.popleft()
            if request.state == "waiting":
                self._start(request)

    def _abandon(self, request: _AdmissionRequest[object]) -> None:
        if request.state == "waiting":
            try:
                self._waiting.remove(request)
            except ValueError:
                pass
            request.state = "finished"
        if not request.response.done():
            request.response.cancel()
