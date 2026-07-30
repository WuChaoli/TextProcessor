import uuid
from typing import Annotated, NoReturn

from fastapi import APIRouter, Depends, HTTPException, status

from app.api.deps import CurrentUser, SessionDep
from app.core.config import settings
from app.features.structured_extraction.dispatcher import (
    CeleryExtractionTaskDispatcher,
)
from app.features.structured_extraction.errors import (
    ExtractionDomainError,
    ExtractionErrorCode,
)
from app.features.structured_extraction.models import (
    ExtractionTask,
    ExtractionTaskStatus,
)
from app.features.structured_extraction.repository import ExtractionTaskRepository
from app.features.structured_extraction.request_policy import RequestPolicy
from app.features.structured_extraction.schemas import (
    ExtractionErrorPublic,
    ExtractionResultPublic,
    ExtractionTaskAccepted,
    ExtractionTaskCreate,
    ExtractionTaskPublic,
)
from app.features.structured_extraction.service import ExtractionTaskService

router = APIRouter(
    prefix="/structured-extraction/tasks",
    tags=["structured-extraction"],
)


def get_request_policy() -> RequestPolicy:
    return RequestPolicy(
        input_roots=settings.EXTRACTION_INPUT_ROOTS,
        output_roots=settings.EXTRACTION_OUTPUT_ROOTS,
        allowed_http_hosts=settings.EXTRACTION_HTTP_ALLOWED_HOSTS,
        allowed_http_cidrs=settings.EXTRACTION_HTTP_ALLOWED_CIDRS,
        max_input_bytes=settings.EXTRACTION_MAX_INPUT_BYTES,
    )


def get_extraction_dispatcher() -> CeleryExtractionTaskDispatcher:
    return CeleryExtractionTaskDispatcher()


RequestPolicyDep = Annotated[RequestPolicy, Depends(get_request_policy)]
DispatcherDep = Annotated[
    CeleryExtractionTaskDispatcher,
    Depends(get_extraction_dispatcher),
]


def _service(
    session: SessionDep,
    policy: RequestPolicyDep,
    dispatcher: DispatcherDep,
) -> ExtractionTaskService:
    return ExtractionTaskService(
        ExtractionTaskRepository(session),
        policy,
        dispatcher,
    )


def _raise_http_error(error: ExtractionDomainError) -> NoReturn:
    raise HTTPException(
        status_code=error.http_status,
        detail={
            "code": error.code,
            "message": error.safe_message,
        },
    ) from error


def task_to_public(task: ExtractionTask) -> ExtractionTaskPublic:
    result = None
    if task.status == ExtractionTaskStatus.SUCCEEDED:
        result = ExtractionResultPublic(
            file_storage_path=task.file_storage_path,
            file_oss_url=task.file_oss_url,
            target_path=task.target_path,
        )
    error = None
    if task.error_code and task.error_message:
        error = ExtractionErrorPublic(
            code=task.error_code,
            message=task.error_message,
        )
    return ExtractionTaskPublic(
        task_id=task.id,
        session_id=task.session_id,
        file_id=task.file_id,
        status=task.status,
        created_at=task.created_at,
        started_at=task.started_at,
        finished_at=task.finished_at,
        result=result,
        error=error,
    )


@router.post(
    "",
    response_model=ExtractionTaskAccepted,
    status_code=status.HTTP_202_ACCEPTED,
)
def create_extraction_task(
    request: ExtractionTaskCreate,
    current_user: CurrentUser,
    service: Annotated[ExtractionTaskService, Depends(_service)],
) -> ExtractionTaskAccepted:
    try:
        task = service.create_task(current_user.id, request)
    except ExtractionDomainError as error:
        _raise_http_error(error)
    return ExtractionTaskAccepted(
        task_id=task.id,
        session_id=task.session_id,
        file_id=task.file_id,
        status=task.status,
    )


@router.get("/{task_id}", response_model=ExtractionTaskPublic)
def get_extraction_task(
    task_id: uuid.UUID,
    current_user: CurrentUser,
    service: Annotated[ExtractionTaskService, Depends(_service)],
) -> ExtractionTaskPublic:
    task = service.get_task(current_user.id, task_id)
    if task is None:
        _raise_http_error(
            ExtractionDomainError(
                ExtractionErrorCode.TASK_NOT_FOUND,
                "任务不存在",
                http_status=404,
            )
        )
    return task_to_public(task)
