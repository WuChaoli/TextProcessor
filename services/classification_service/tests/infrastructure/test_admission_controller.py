import asyncio
import threading
from collections.abc import Callable, Coroutine, Sequence
from concurrent.futures import Future
from typing import Any

import pytest

from classification_service.application.ports.inference_executor import (
    InferenceAdmissionClosed,
    InferenceCapacityExceeded,
)
from classification_service.infrastructure.execution.admission_controller import (
    AdmissionController,
)
from classification_service.infrastructure.execution.thread_executor import (
    ThreadInferenceExecutor,
)


def _run[T](coroutine: Coroutine[Any, Any, T]) -> T:
    return asyncio.run(coroutine)


async def _cancel(tasks: Sequence[asyncio.Task[Any]]) -> None:
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


def test_tenth_request_is_rejected_without_submitting_work() -> None:
    async def scenario() -> None:
        controller = AdmissionController(waiting_limit=8, timeout_seconds=1.0)
        futures = [Future[int]() for _ in range(10)]
        submitted: list[int] = []

        def submit(index: int) -> Callable[[], Future[int]]:
            def controlled_submit() -> Future[int]:
                submitted.append(index)
                return futures[index]

            return controlled_submit

        active = asyncio.create_task(controller.run(submit(0)))
        await asyncio.sleep(0)
        waiting = [
            asyncio.create_task(controller.run(submit(index))) for index in range(1, 9)
        ]
        await asyncio.sleep(0)

        with pytest.raises(InferenceCapacityExceeded):
            await controller.run(submit(9))

        assert submitted == [0]

        await _cancel(waiting)
        futures[0].set_result(0)
        assert await active == 0

    _run(scenario())


def test_active_timeout_does_not_release_slot_until_underlying_future_finishes() -> (
    None
):
    async def scenario() -> None:
        controller = AdmissionController(waiting_limit=8, timeout_seconds=0.02)
        first_future: Future[int] = Future()
        second_future: Future[int] = Future()
        submitted: list[str] = []

        def submit(name: str, future: Future[int]) -> Callable[[], Future[int]]:
            def controlled_submit() -> Future[int]:
                submitted.append(name)
                return future

            return controlled_submit

        with pytest.raises(TimeoutError):
            await controller.run(submit("first", first_future))

        second = asyncio.create_task(controller.run(submit("second", second_future)))
        await asyncio.sleep(0)
        assert submitted == ["first"]

        first_future.set_result(1)
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert submitted == ["first", "second"]

        second_future.set_result(2)
        assert await second == 2

    _run(scenario())


def test_waiting_timeout_is_measured_from_admission_and_never_submits_work() -> None:
    async def scenario() -> None:
        controller = AdmissionController(waiting_limit=8, timeout_seconds=0.02)
        active_future: Future[int] = Future()
        waiting_future: Future[int] = Future()
        submitted: list[str] = []

        def submit(name: str, future: Future[int]) -> Callable[[], Future[int]]:
            def controlled_submit() -> Future[int]:
                submitted.append(name)
                return future

            return controlled_submit

        active = asyncio.create_task(controller.run(submit("active", active_future)))
        await asyncio.sleep(0)

        with pytest.raises(TimeoutError):
            await controller.run(submit("waiting", waiting_future))

        with pytest.raises(TimeoutError):
            await active
        assert submitted == ["active"]

        active_future.set_result(1)
        await asyncio.sleep(0)

    _run(scenario())


def test_cancelling_queued_request_removes_waiter() -> None:
    async def scenario() -> None:
        controller = AdmissionController(waiting_limit=8, timeout_seconds=1.0)
        active_future: Future[int] = Future()
        waiting_futures = [Future[int]() for _ in range(9)]

        active = asyncio.create_task(controller.run(lambda: active_future))
        await asyncio.sleep(0)
        waiting: list[asyncio.Task[object]] = [
            asyncio.create_task(controller.run(lambda future=future: future))
            for future in waiting_futures[:8]
        ]
        await asyncio.sleep(0)

        waiting[3].cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiting[3]

        replacement = asyncio.create_task(controller.run(lambda: waiting_futures[8]))
        await asyncio.sleep(0)
        assert not replacement.done()

        await _cancel([*waiting[:3], *waiting[4:], replacement])
        active_future.set_result(1)
        assert await active == 1

    _run(scenario())


def test_stop_rejects_new_requests_with_a_stable_error() -> None:
    async def scenario() -> None:
        controller = AdmissionController(waiting_limit=8, timeout_seconds=1.0)
        controller.stop_admission()

        with pytest.raises(InferenceAdmissionClosed) as caught:
            await controller.run(lambda: Future[int]())

        assert caught.value.code == "INFERENCE_ADMISSION_CLOSED"

    _run(scenario())


def test_stop_rejects_queued_requests_without_submitting_them() -> None:
    async def scenario() -> None:
        controller = AdmissionController(waiting_limit=8, timeout_seconds=1.0)
        active_future: Future[int] = Future()
        queued_futures = [Future[int](), Future[int]()]
        submitted: list[str] = []

        def submit(name: str, future: Future[int]) -> Callable[[], Future[int]]:
            def controlled_submit() -> Future[int]:
                submitted.append(name)
                return future

            return controlled_submit

        active = asyncio.create_task(controller.run(submit("active", active_future)))
        await asyncio.sleep(0)
        queued = [
            asyncio.create_task(controller.run(submit(f"queued-{index}", future)))
            for index, future in enumerate(queued_futures)
        ]
        await asyncio.sleep(0)

        controller.stop_admission()

        for request in queued:
            with pytest.raises(InferenceAdmissionClosed):
                await request
        assert submitted == ["active"]

        active_future.set_result(1)
        assert await active == 1

    _run(scenario())


def test_active_request_can_complete_after_stop() -> None:
    async def scenario() -> None:
        controller = AdmissionController(waiting_limit=8, timeout_seconds=1.0)
        active_future: Future[int] = Future()
        active = asyncio.create_task(controller.run(lambda: active_future))
        await asyncio.sleep(0)

        controller.stop_admission()
        active_future.set_result(42)

        assert await active == 42

    _run(scenario())


def test_stop_then_shutdown_never_submits_work_to_the_closed_pool() -> None:
    async def scenario() -> None:
        executor = ThreadInferenceExecutor(waiting_limit=8, timeout_seconds=1.0)
        called = False

        def operation() -> int:
            nonlocal called
            called = True
            return 1

        executor.stop_admission()
        executor.shutdown()

        with pytest.raises(InferenceAdmissionClosed):
            await executor.run(operation)
        assert called is False

    _run(scenario())


def test_thread_executor_keeps_event_loop_responsive_while_inference_runs() -> None:
    async def scenario() -> None:
        executor = ThreadInferenceExecutor(waiting_limit=8, timeout_seconds=1.0)
        started = threading.Event()
        release = threading.Event()

        def blocking_inference() -> str:
            started.set()
            if not release.wait(timeout=1.0):
                raise RuntimeError("test did not release inference")
            return threading.current_thread().name

        inference = asyncio.create_task(executor.run(blocking_inference))
        try:

            async def wait_until_started() -> None:
                while not started.is_set():
                    await asyncio.sleep(0)

            await asyncio.wait_for(wait_until_started(), timeout=1.0)

            event_loop_progressed = False

            async def mark_progress() -> None:
                nonlocal event_loop_progressed
                await asyncio.sleep(0)
                event_loop_progressed = True

            await asyncio.wait_for(mark_progress(), timeout=0.1)
            assert event_loop_progressed is True
            assert not inference.done()

            release.set()
            thread_name = await inference
            assert thread_name.startswith("classification-inference")
        finally:
            release.set()
            if not inference.done():
                await inference
            executor.shutdown()

    _run(scenario())
