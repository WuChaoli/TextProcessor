from collections.abc import Callable
from uuid import UUID

from fastapi import APIRouter, HTTPException, status

from datajuicer_service.api.schemas import (
    JobAccepted,
    JobCreatePublic,
    JobPublic,
    accepted_from_job,
    public_from_job,
)
from datajuicer_service.jobs.repository import IdempotencyConflict
from datajuicer_service.jobs.service import (
    CreateJobCommand,
    JobService,
    QueueSubmissionError,
)


def error_detail(code: str, message: str) -> dict[str, str]:
    return {"code": code, "message": message}


def create_router(
    service: JobService,
    readiness_check: Callable[[], bool],
) -> APIRouter:
    router = APIRouter()

    @router.post(
        "/v1/jobs",
        response_model=JobAccepted,
        response_model_by_alias=True,
        status_code=status.HTTP_202_ACCEPTED,
    )
    def create_job(request: JobCreatePublic) -> JobAccepted:
        try:
            job = service.create_job(
                CreateJobCommand(
                    request_id=request.request_id,
                    profile=request.profile,
                    input_path=request.input_path,
                    output_path=request.output_path,
                )
            )
        except IdempotencyConflict as error:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=error_detail("IDEMPOTENCY_CONFLICT", "幂等请求参数冲突"),
            ) from error
        except QueueSubmissionError as error:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=error_detail("QUEUE_SUBMISSION_FAILED", "任务入队失败"),
            ) from error
        return accepted_from_job(job)

    @router.get(
        "/v1/jobs/{job_id}",
        response_model=JobPublic,
        response_model_by_alias=True,
    )
    def get_job(job_id: UUID) -> JobPublic:
        job = service.get_job(job_id)
        if job is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=error_detail("JOB_NOT_FOUND", "任务不存在"),
            )
        return public_from_job(job)

    @router.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @router.get("/ready")
    def ready() -> dict[str, str]:
        try:
            is_ready = readiness_check()
        except Exception as error:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=error_detail("SERVICE_NOT_READY", "服务尚未就绪"),
            ) from error
        if not is_ready:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=error_detail("SERVICE_NOT_READY", "服务尚未就绪"),
            )
        return {"status": "ready"}

    return router
