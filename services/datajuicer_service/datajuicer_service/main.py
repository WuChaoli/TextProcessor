from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime

from fastapi import FastAPI, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from datajuicer_service.api.routes import create_router, error_detail
from datajuicer_service.core.celery_app import create_celery_app
from datajuicer_service.core.config import get_settings
from datajuicer_service.core.database import (
    create_database_engine,
    create_session_factory,
)
from datajuicer_service.jobs.dispatcher import CeleryJobDispatcher
from datajuicer_service.jobs.repository import JobRepository
from datajuicer_service.jobs.service import JobRepositoryProtocol, JobService
from datajuicer_service.profiles.compatibility import verify_datajuicer_runtime


def create_app(
    service: JobService,
    *,
    readiness_check: Callable[[], bool],
) -> FastAPI:
    app = FastAPI(title="Data-Juicer Service", version="0.1.0")

    @app.exception_handler(RequestValidationError)
    async def invalid_request_handler(
        _request: object,
        _error: RequestValidationError,
    ) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            content={
                "detail": error_detail("INVALID_REQUEST", "请求参数不正确")
            },
        )

    app.include_router(create_router(service, readiness_check))
    return app


def create_application() -> FastAPI:
    settings = get_settings()
    verify_datajuicer_runtime()
    engine = create_database_engine(settings.database_url)
    session_factory = create_session_factory(engine)

    @contextmanager
    def repository_factory() -> Iterator[JobRepositoryProtocol]:
        with session_factory() as session:
            yield JobRepository(session)

    celery_client = create_celery_app(
        broker_url=settings.celery_broker_url,
        queue=settings.celery_queue,
        recovery_interval_seconds=settings.recovery_interval_seconds,
    )
    dispatcher = CeleryJobDispatcher(celery_client, queue=settings.celery_queue)
    service = JobService(
        repository_factory=repository_factory,
        dispatcher=dispatcher,
        max_attempts=settings.max_attempts,
        job_timeout_seconds=settings.job_timeout_seconds,
        now=lambda: datetime.now(UTC),
    )

    def readiness_check() -> bool:
        with Session(engine) as session:
            return session.scalar(select(1)) == 1

    app = create_app(service, readiness_check=readiness_check)
    app.state.database_engine = engine
    return app
