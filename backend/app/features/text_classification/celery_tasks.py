import uuid

from sqlmodel import Session

from app.core.celery_app import celery_app
from app.core.config import settings
from app.core.db import engine
from app.features.text_classification.adapter import ClassificationClient
from app.features.text_classification.input_preparer import ClassificationInputPreparer
from app.features.text_classification.models import utc_now
from app.features.text_classification.repository import ClassificationTaskRepository
from app.tasking.envelope import TaskEnvelope
from app.tasking.state import TaskStatus


def execute(session: Session, task_id: uuid.UUID) -> None:
    repository = ClassificationTaskRepository(session)
    task = repository.get(task_id)
    if task is None or task.status in {TaskStatus.SUCCEEDED, TaskStatus.FAILED, TaskStatus.CANCELLED}:
        return
    if task.status is TaskStatus.QUEUED:
        task = repository.transition(task.id, expected=TaskStatus.QUEUED, target=TaskStatus.RUNNING, started_at=utc_now())
    try:
        prepared = ClassificationInputPreparer(staging_root=settings.CLASSIFICATION_STAGING_ROOT, input_roots=tuple(settings.CLASSIFICATION_INPUT_ROOTS), max_input_bytes=settings.CLASSIFICATION_MAX_INPUT_BYTES).prepare(str(task.id), task.input_uri)
        result = ClassificationClient(
            base_url=settings.CLASSIFICATION_BASE_URL,
            api_token=settings.CLASSIFICATION_API_TOKEN,
            timeout=settings.CLASSIFICATION_TIMEOUT_SECONDS,
        ).classify(str(task.id), prepared.local_uri)
        repository.transition(task.id, expected=TaskStatus.RUNNING, target=TaskStatus.SUCCEEDED, staging_uri=prepared.local_uri, input_sha256=prepared.input_sha256, input_size_bytes=prepared.size_bytes, result=result, finished_at=utc_now())
    except Exception:
        repository.transition(task.id, expected=TaskStatus.RUNNING, target=TaskStatus.FAILED, error_code="CLASSIFICATION_FAILED", error_message="分类处理失败", finished_at=utc_now())
        raise


@celery_app.task(name="text_classification.execute")  # type: ignore[untyped-decorator]
def execute_task(*, task_id: str, task_type: str, schema_version: int) -> None:
    envelope = TaskEnvelope.parse({"task_id": task_id, "task_type": task_type, "schema_version": schema_version}, expected_type="text_classification", expected_schema_version=1)
    with Session(engine) as session:
        execute(session, envelope.task_id)
