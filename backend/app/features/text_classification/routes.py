import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status

from app.api.deps import CurrentUser, SessionDep
from app.features.text_classification.dispatcher import (
    CeleryClassificationDispatcher,
)
from app.features.text_classification.models import ClassificationTask
from app.features.text_classification.repository import (
    ClassificationTaskRepository,
)
from app.features.text_classification.schemas import (
    ClassificationTaskAccepted,
    ClassificationTaskCreate,
    ClassificationTaskPublic,
)
from app.features.text_classification.service import ClassificationTaskService

router = APIRouter(prefix="/text-classification/tasks", tags=["text-classification"])


def get_classification_dispatcher() -> CeleryClassificationDispatcher:
    return CeleryClassificationDispatcher()


def _service(
    session: SessionDep,
    dispatcher: Annotated[
        CeleryClassificationDispatcher,
        Depends(get_classification_dispatcher),
    ],
) -> ClassificationTaskService:
    return ClassificationTaskService(ClassificationTaskRepository(session), dispatcher)


def _public(task: ClassificationTask) -> ClassificationTaskPublic:
    error = {"code": task.error_code, "message": task.error_message} if task.error_code and task.error_message else None
    return ClassificationTaskPublic(task_id=task.id, session_id=task.session_id, file_id=task.file_id, status=task.status, created_at=task.created_at, started_at=task.started_at, finished_at=task.finished_at, result=task.result, error=error)


@router.post("", response_model=ClassificationTaskAccepted, status_code=status.HTTP_202_ACCEPTED)
def create_task(request: ClassificationTaskCreate, current_user: CurrentUser, service: Annotated[ClassificationTaskService, Depends(_service)]) -> ClassificationTaskAccepted:
    try:
        task = service.create_task(current_user.id, request)
    except ValueError as error:
        raise HTTPException(409, detail={"code": str(error), "message": "幂等键对应的请求参数不一致"}) from error
    except Exception as error:
        raise HTTPException(503, detail={"code": "QUEUE_SUBMISSION_FAILED", "message": "任务提交失败"}) from error
    return ClassificationTaskAccepted(task_id=task.id, session_id=task.session_id, file_id=task.file_id, status=task.status, created_at=task.created_at)


@router.get("/{task_id}", response_model=ClassificationTaskPublic)
def get_task(task_id: uuid.UUID, current_user: CurrentUser, service: Annotated[ClassificationTaskService, Depends(_service)]) -> ClassificationTaskPublic:
    task = service.get_task(current_user.id, task_id)
    if task is None:
        raise HTTPException(404, detail={"code": "TASK_NOT_FOUND", "message": "任务不存在"})
    return _public(task)
