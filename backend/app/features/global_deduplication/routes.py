import uuid
from typing import Annotated, NoReturn

from fastapi import APIRouter, Depends, HTTPException, status

from app.api.deps import CurrentUser, SessionDep
from app.core.config import settings
from app.features.global_deduplication.api_errors import (
    GlobalDeduplicationApiErrorCode,
    GlobalDeduplicationDomainError,
)
from app.features.global_deduplication.dispatcher import (
    CeleryGlobalDeduplicationTaskDispatcher,
)
from app.features.global_deduplication.repository import (
    GlobalDeduplicationTaskRepository,
)
from app.features.global_deduplication.request_policy import (
    GlobalDeduplicationRequestPolicy,
)
from app.features.global_deduplication.schemas import (
    GlobalDeduplicationErrorPublic,
    GlobalDeduplicationProgressPublic,
    GlobalDeduplicationResultPublic,
    GlobalDeduplicationTaskAccepted,
    GlobalDeduplicationTaskCreate,
    GlobalDeduplicationTaskPublic,
)
from app.features.global_deduplication.service import (
    GlobalDeduplicationTaskService,
)
from app.features.global_deduplication.state_machine import (
    GlobalDeduplicationTaskStatus,
)
from app.features.global_deduplication.task_models import GlobalDeduplicationTask

router = APIRouter(
    prefix="/global-deduplication/tasks",
    tags=["global-deduplication"],
)


def get_global_deduplication_policy() -> GlobalDeduplicationRequestPolicy:
    return GlobalDeduplicationRequestPolicy(
        input_roots=settings.GLOBAL_DEDUP_INPUT_ROOTS,
        output_roots=settings.GLOBAL_DEDUP_WORKER.output_roots,
        allowed_http_hosts=settings.GLOBAL_DEDUP_HTTP_ALLOWED_HOSTS,
        allowed_http_cidrs=settings.GLOBAL_DEDUP_HTTP_ALLOWED_CIDRS,
    )


def get_global_deduplication_dispatcher() -> (
    CeleryGlobalDeduplicationTaskDispatcher
):
    return CeleryGlobalDeduplicationTaskDispatcher()


PolicyDep = Annotated[
    GlobalDeduplicationRequestPolicy,
    Depends(get_global_deduplication_policy),
]
DispatcherDep = Annotated[
    CeleryGlobalDeduplicationTaskDispatcher,
    Depends(get_global_deduplication_dispatcher),
]


def _service(
    session: SessionDep,
    policy: PolicyDep,
    dispatcher: DispatcherDep,
) -> GlobalDeduplicationTaskService:
    return GlobalDeduplicationTaskService(
        GlobalDeduplicationTaskRepository(session),
        policy,
        dispatcher,
    )


def _raise_http_error(error: GlobalDeduplicationDomainError) -> NoReturn:
    raise HTTPException(
        status_code=error.http_status,
        detail={
            "code": error.code,
            "message": error.safe_message,
        },
    ) from error


def task_to_public(
    task: GlobalDeduplicationTask,
) -> GlobalDeduplicationTaskPublic:
    result = (
        GlobalDeduplicationResultPublic(targetPath=task.target_path)
        if task.status is GlobalDeduplicationTaskStatus.SUCCEEDED
        else None
    )
    error = (
        GlobalDeduplicationErrorPublic(
            code=task.error_code,
            message=task.error_message,
        )
        if (
            task.status
            in {
                GlobalDeduplicationTaskStatus.FAILED,
                GlobalDeduplicationTaskStatus.CANCELLED,
            }
            and task.error_code is not None
            and task.error_message is not None
        )
        else None
    )
    return GlobalDeduplicationTaskPublic(
        taskId=task.id,
        sessionId=task.session_id,
        status=task.status,
        createdAt=task.created_at,
        startedAt=task.started_at,
        finishedAt=task.finished_at,
        progress=GlobalDeduplicationProgressPublic(
            phase=task.processing_phase,
            total=task.progress_total,
            processed=task.progress_processed,
            percent=task.progress_percent,
        ),
        result=result,
        error=error,
    )


@router.post(
    "",
    response_model=GlobalDeduplicationTaskAccepted,
    status_code=status.HTTP_202_ACCEPTED,
)
def create_global_deduplication_task(
    request: GlobalDeduplicationTaskCreate,
    current_user: CurrentUser,
    service: Annotated[GlobalDeduplicationTaskService, Depends(_service)],
) -> GlobalDeduplicationTaskAccepted:
    try:
        task = service.create_task(current_user.id, request)
    except GlobalDeduplicationDomainError as error:
        _raise_http_error(error)
    return GlobalDeduplicationTaskAccepted(
        taskId=task.id,
        sessionId=task.session_id,
        status=task.status,
    )


@router.get(
    "/{task_id}",
    response_model=GlobalDeduplicationTaskPublic,
)
def get_global_deduplication_task(
    task_id: uuid.UUID,
    current_user: CurrentUser,
    service: Annotated[GlobalDeduplicationTaskService, Depends(_service)],
) -> GlobalDeduplicationTaskPublic:
    task = service.get_task(current_user.id, task_id)
    if task is None:
        _raise_http_error(
            GlobalDeduplicationDomainError(
                GlobalDeduplicationApiErrorCode.TASK_NOT_FOUND,
                "任务不存在",
                http_status=404,
            )
        )
    return task_to_public(task)
