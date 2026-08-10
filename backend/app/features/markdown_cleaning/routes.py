import uuid
from typing import Annotated, Literal, NoReturn

from fastapi import APIRouter, Depends, HTTPException, status

from app.api.deps import CurrentUser, SessionDep
from app.core.config import settings
from app.features.markdown_cleaning.api_errors import (
    MarkdownCleaningApiErrorCode,
    MarkdownCleaningDomainError,
)
from app.features.markdown_cleaning.dispatcher import (
    CeleryMarkdownCleaningTaskDispatcher,
)
from app.features.markdown_cleaning.repository import MarkdownCleaningTaskRepository
from app.features.markdown_cleaning.request_policy import MarkdownCleaningRequestPolicy
from app.features.markdown_cleaning.schemas import (
    MarkdownCleaningDomainErrorResponse,
    MarkdownCleaningErrorPublic,
    MarkdownCleaningProgressPublic,
    MarkdownCleaningRedactionsPublic,
    MarkdownCleaningResultPublic,
    MarkdownCleaningSummaryPublic,
    MarkdownCleaningTaskAccepted,
    MarkdownCleaningTaskCreate,
    MarkdownCleaningTaskPublic,
)
from app.features.markdown_cleaning.service import MarkdownCleaningTaskService
from app.features.markdown_cleaning.state_machine import MarkdownCleaningTaskStatus
from app.features.markdown_cleaning.task_models import MarkdownCleaningTask

router = APIRouter(
    prefix="/markdown-cleaning/tasks",
    tags=["markdown-cleaning"],
)


def get_markdown_cleaning_request_policy() -> MarkdownCleaningRequestPolicy:
    return MarkdownCleaningRequestPolicy(
        allowed_http_hosts=settings.MARKDOWN_CLEANING_HTTP_ALLOWED_HOSTS,
        allowed_http_cidrs=settings.MARKDOWN_CLEANING_HTTP_ALLOWED_CIDRS,
    )


def get_markdown_cleaning_dispatcher() -> CeleryMarkdownCleaningTaskDispatcher:
    return CeleryMarkdownCleaningTaskDispatcher()


RequestPolicyDep = Annotated[
    MarkdownCleaningRequestPolicy,
    Depends(get_markdown_cleaning_request_policy),
]
DispatcherDep = Annotated[
    CeleryMarkdownCleaningTaskDispatcher,
    Depends(get_markdown_cleaning_dispatcher),
]


def _service(
    session: SessionDep,
    policy: RequestPolicyDep,
    dispatcher: DispatcherDep,
) -> MarkdownCleaningTaskService:
    return MarkdownCleaningTaskService(
        MarkdownCleaningTaskRepository(session),
        policy,
        dispatcher,
    )


def _raise_http_error(error: MarkdownCleaningDomainError) -> NoReturn:
    raise HTTPException(
        status_code=error.http_status,
        detail={
            "code": error.code,
            "message": error.safe_message,
        },
    ) from error


def _safe_error(task: MarkdownCleaningTask) -> MarkdownCleaningErrorPublic | None:
    if task.status not in {
        MarkdownCleaningTaskStatus.FAILED,
        MarkdownCleaningTaskStatus.CANCELLED,
    }:
        return None
    if task.error_code is None or task.error_message is None:
        return None
    return MarkdownCleaningErrorPublic(code=task.error_code, message=task.error_message)


def _safe_result(task: MarkdownCleaningTask) -> MarkdownCleaningResultPublic | None:
    if task.status is not MarkdownCleaningTaskStatus.SUCCEEDED:
        return None
    return MarkdownCleaningResultPublic(
        fileId=task.file_id,
        fileStoragePath=task.file_storage_path,
        fileOssUrl=task.file_oss_url,
        targetPath=task.target_path,
        summary=MarkdownCleaningSummaryPublic(
            duplicateParagraphsRemoved=task.duplicate_paragraphs_removed or 0,
            redactions=MarkdownCleaningRedactionsPublic(
                phone=task.phone_redaction_count or 0,
                idCard=task.id_card_redaction_count or 0,
                bankCard=task.bank_card_redaction_count or 0,
                email=task.email_redaction_count or 0,
                ipv4=task.ipv4_redaction_count or 0,
            ),
            formattingChanges=task.formatting_change_count or 0,
        ),
    )


def _public_progress(task: MarkdownCleaningTask) -> MarkdownCleaningProgressPublic:
    if task.status is MarkdownCleaningTaskStatus.SUCCEEDED:
        return MarkdownCleaningProgressPublic(phase="completed", percent=100)

    percent = task.progress_percent
    phase_by_internal: dict[
        str, Literal["validating_input", "cleaning", "publishing"]
    ] = {
        "validating_input": "validating_input",
        "claiming_task": "validating_input",
        "cleaning": "cleaning",
        "saving_prepared": "publishing",
        "publishing_result": "publishing",
    }
    phase = phase_by_internal.get(task.processing_phase or "")
    if phase is None:
        if percent >= 90:
            phase = "publishing"
        elif percent >= 20:
            phase = "cleaning"
        else:
            phase = "validating_input"
    return MarkdownCleaningProgressPublic(phase=phase, percent=percent)


def task_to_public(task: MarkdownCleaningTask) -> MarkdownCleaningTaskPublic:
    return MarkdownCleaningTaskPublic(
        taskId=task.id,
        sessionId=task.session_id,
        fileId=task.file_id,
        status=task.status,
        createdAt=task.created_at,
        startedAt=task.started_at,
        finishedAt=task.finished_at,
        progress=_public_progress(task),
        result=_safe_result(task),
        error=_safe_error(task),
    )


@router.post(
    "",
    response_model=MarkdownCleaningTaskAccepted,
    status_code=status.HTTP_202_ACCEPTED,
    responses={
        status.HTTP_422_UNPROCESSABLE_CONTENT: {
            "model": MarkdownCleaningDomainErrorResponse,
            "description": "Input not allowed by request policy",
        },
        status.HTTP_401_UNAUTHORIZED: {
            "model": MarkdownCleaningDomainErrorResponse,
            "description": "Missing or invalid credentials",
        },
        status.HTTP_409_CONFLICT: {
            "model": MarkdownCleaningDomainErrorResponse,
            "description": "Idempotency conflict for the same caller/session/file",
        },
        status.HTTP_503_SERVICE_UNAVAILABLE: {
            "model": MarkdownCleaningDomainErrorResponse,
            "description": "Queue temporarily unavailable",
        },
    },
)
def create_markdown_cleaning_task(
    request: MarkdownCleaningTaskCreate,
    current_user: CurrentUser,
    service: Annotated[MarkdownCleaningTaskService, Depends(_service)],
) -> MarkdownCleaningTaskAccepted:
    try:
        task = service.create_task(current_user.id, request)
    except MarkdownCleaningDomainError as error:
        _raise_http_error(error)
    return MarkdownCleaningTaskAccepted(
        taskId=task.id,
        sessionId=task.session_id,
        fileId=task.file_id,
        status=task.status,
    )


@router.get(
    "/{task_id}",
    response_model=MarkdownCleaningTaskPublic,
    responses={
        status.HTTP_401_UNAUTHORIZED: {
            "model": MarkdownCleaningDomainErrorResponse,
            "description": "Missing or invalid credentials",
        },
        status.HTTP_404_NOT_FOUND: {
            "model": MarkdownCleaningDomainErrorResponse,
            "description": "Task not found for caller",
        },
    },
)
def get_markdown_cleaning_task(
    task_id: uuid.UUID,
    current_user: CurrentUser,
    service: Annotated[MarkdownCleaningTaskService, Depends(_service)],
) -> MarkdownCleaningTaskPublic:
    task = service.get_task(current_user.id, task_id)
    if task is None:
        _raise_http_error(
            MarkdownCleaningDomainError(
                MarkdownCleaningApiErrorCode.TASK_NOT_FOUND,
                "任务不存在",
                http_status=404,
            )
        )
    return task_to_public(task)
