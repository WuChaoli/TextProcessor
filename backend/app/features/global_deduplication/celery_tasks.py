import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import httpx
from celery import Task  # type: ignore[import-untyped]
from sqlmodel import Session

from app.core.celery_app import celery_app
from app.core.config import GlobalDeduplicationWorkerSettings, settings
from app.core.db import engine
from app.features.global_deduplication.adapters.datajuicer import (
    DataJuicerAdapter,
)
from app.features.global_deduplication.errors import (
    GlobalDeduplicationProcessingError,
)
from app.features.global_deduplication.input_reader import BoundedUriReader
from app.features.global_deduplication.messages import GlobalDeduplicationMessage
from app.features.global_deduplication.orchestration import (
    GlobalDeduplicationOrchestrator,
    GlobalDeduplicationScheduler,
)
from app.features.global_deduplication.repository import (
    GlobalDeduplicationTaskRepository,
)
from app.features.global_deduplication.staging import GlobalDeduplicationStaging

_TRANSIENT_RETRY_LIMIT = 3


class CeleryGlobalDeduplicationScheduler(GlobalDeduplicationScheduler):
    def enqueue_submit(self, task_id: uuid.UUID, *, countdown: int) -> None:
        submit_global_deduplication_task.apply_async(  # pyright: ignore[reportFunctionMemberAccess]
            kwargs=_message(task_id),
            countdown=countdown,
        )

    def enqueue_poll(self, task_id: uuid.UUID, *, countdown: int) -> None:
        poll_global_deduplication_task.apply_async(  # pyright: ignore[reportFunctionMemberAccess]
            kwargs=_message(task_id),
            countdown=countdown,
        )


def handle_submit_task(
    payload: object,
    *,
    orchestrator: GlobalDeduplicationOrchestrator,
) -> None:
    message = GlobalDeduplicationMessage.parse(payload)
    orchestrator.submit(message.task_id)


def handle_poll_task(
    payload: object,
    *,
    orchestrator: GlobalDeduplicationOrchestrator,
) -> None:
    message = GlobalDeduplicationMessage.parse(payload)
    orchestrator.poll(message.task_id)


def handle_recover_task(
    *,
    orchestrator: GlobalDeduplicationOrchestrator,
) -> dict[str, int]:
    summary = orchestrator.recover()
    return {
        "submitDispatched": summary.submit_dispatched,
        "pollDispatched": summary.poll_dispatched,
    }


@celery_app.task(  # type: ignore[untyped-decorator]
    name="global_deduplication.submit",
    bind=True,
    max_retries=_TRANSIENT_RETRY_LIMIT,
)
def submit_global_deduplication_task(self: Task, **payload: object) -> None:
    try:
        _run_with_orchestrator(handle_submit_task, payload)
    except GlobalDeduplicationProcessingError as error:
        if not error.transient:
            raise
        max_retries = self.max_retries
        if max_retries is not None and self.request.retries >= max_retries:
            message = GlobalDeduplicationMessage.parse(payload)
            with Session(engine) as session, _http_client(
                settings.GLOBAL_DEDUP_WORKER
            ) as client:
                build_orchestrator(
                    session,
                    http_client=client,
                ).fail_exhausted_submission_retry(message.task_id, error)
            return
        raise self.retry(
            exc=error,
            countdown=settings.GLOBAL_DEDUP_WORKER.datajuicer_poll_initial_delay_seconds,
        ) from error


@celery_app.task(  # type: ignore[untyped-decorator]
    name="global_deduplication.poll",
)
def poll_global_deduplication_task(**payload: object) -> None:
    _run_with_orchestrator(handle_poll_task, payload)


@celery_app.task(  # type: ignore[untyped-decorator]
    name="global_deduplication.recover",
)
def recover_global_deduplication_tasks() -> dict[str, int]:
    with Session(engine) as session, _http_client(
        settings.GLOBAL_DEDUP_WORKER
    ) as client:
        return handle_recover_task(
            orchestrator=build_orchestrator(session, http_client=client)
        )


def build_orchestrator(
    session: Session,
    *,
    http_client: httpx.Client,
    worker_settings: GlobalDeduplicationWorkerSettings | None = None,
    input_roots: tuple[Path, ...] | None = None,
    scheduler: GlobalDeduplicationScheduler | None = None,
) -> GlobalDeduplicationOrchestrator:
    configured = worker_settings or settings.GLOBAL_DEDUP_WORKER
    if configured.datajuicer_base_url is None:
        raise RuntimeError("未配置 Data-Juicer 服务地址")
    roots = input_roots or tuple(settings.GLOBAL_DEDUP_INPUT_ROOTS)
    return GlobalDeduplicationOrchestrator(
        repository=GlobalDeduplicationTaskRepository(session),
        reader=BoundedUriReader(
            input_roots=roots,
            chunk_bytes=configured.copy_chunk_bytes,
        ),
        staging=GlobalDeduplicationStaging(configured.staging_root),
        adapter=DataJuicerAdapter(
            base_url=str(configured.datajuicer_base_url),
            client=http_client,
        ),
        scheduler=scheduler or CeleryGlobalDeduplicationScheduler(),
        settings=configured,
        now=lambda: datetime.now(UTC),
    )


def _run_with_orchestrator(
    handler: Callable[..., None],
    payload: object,
) -> None:
    with Session(engine) as session, _http_client(
        settings.GLOBAL_DEDUP_WORKER
    ) as client:
        orchestrator = build_orchestrator(session, http_client=client)
        handler(payload, orchestrator=orchestrator)


def _http_client(
    configured: GlobalDeduplicationWorkerSettings,
) -> httpx.Client:
    return httpx.Client(
        timeout=httpx.Timeout(
            connect=configured.datajuicer_connect_timeout_seconds,
            read=max(
                configured.datajuicer_submit_timeout_seconds,
                configured.datajuicer_poll_timeout_seconds,
            ),
            write=configured.datajuicer_submit_timeout_seconds,
            pool=configured.datajuicer_connect_timeout_seconds,
        )
    )


def _message(task_id: uuid.UUID) -> dict[str, str | int]:
    return GlobalDeduplicationMessage(
        taskId=task_id,
        taskType="global_deduplication",
        schemaVersion=1,
    ).as_payload()
