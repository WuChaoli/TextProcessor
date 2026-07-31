from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime

from celery import Celery  # type: ignore[import-untyped]

from datajuicer_service.core.celery_app import create_celery_app
from datajuicer_service.core.config import Settings, get_settings
from datajuicer_service.core.database import (
    create_database_engine,
    create_session_factory,
)
from datajuicer_service.jobs.dispatcher import CeleryJobDispatcher
from datajuicer_service.jobs.orchestration import (
    JobOrchestrator,
    OrchestrationRepository,
    ProfileExecutor,
)
from datajuicer_service.jobs.repository import JobRepository
from datajuicer_service.jobs.tasks import TaskOrchestrator, register_tasks
from datajuicer_service.profiles.compatibility import verify_datajuicer_runtime
from datajuicer_service.profiles.io import InputLimits
from datajuicer_service.profiles.registry import get_profile


def create_worker_application(settings: Settings | None = None) -> Celery:
    resolved_settings = settings or get_settings()
    verify_datajuicer_runtime()
    engine = create_database_engine(resolved_settings.database_url)
    session_factory = create_session_factory(engine)
    celery_app = create_celery_app(
        broker_url=resolved_settings.celery_broker_url,
        queue=resolved_settings.celery_queue,
        recovery_interval_seconds=resolved_settings.recovery_interval_seconds,
    )
    celery_app.conf.worker_concurrency = resolved_settings.worker_concurrency
    dispatcher = CeleryJobDispatcher(
        celery_app,
        queue=resolved_settings.celery_queue,
    )
    limits = InputLimits(
        max_records=resolved_settings.input_max_records,
        max_bytes=resolved_settings.input_max_bytes,
        max_text_chars=resolved_settings.input_max_text_chars,
    )

    @contextmanager
    def repository_factory() -> Iterator[OrchestrationRepository]:
        with session_factory() as session:
            yield JobRepository(
                session,
                lease_seconds=resolved_settings.lease_seconds,
                recovery_age_seconds=resolved_settings.recovery_interval_seconds,
            )

    def profile_resolver(name: str) -> ProfileExecutor:
        return get_profile(name, limits)

    def orchestrator_factory() -> TaskOrchestrator:
        return JobOrchestrator(
            repository_factory=repository_factory,
            profile_resolver=profile_resolver,
            dispatcher=dispatcher,
            now=lambda: datetime.now(UTC),
            recovery_batch_size=resolved_settings.recovery_batch_size,
        )

    register_tasks(
        celery_app,
        orchestrator_factory,
        max_attempts=resolved_settings.max_attempts,
    )
    return celery_app
