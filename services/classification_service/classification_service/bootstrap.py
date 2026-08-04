import os
import signal
from collections.abc import Callable
from contextlib import asynccontextmanager
from typing import cast

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from classification_service.application.classify_text import ClassifyTextHandler
from classification_service.infrastructure.config import Settings, get_settings
from classification_service.infrastructure.execution.thread_executor import (
    ThreadInferenceExecutor,
)
from classification_service.infrastructure.model.runtime import (
    LoadedClassificationRuntime,
    load_classification_runtime,
)
from classification_service.infrastructure.release.validator import validate_release
from classification_service.presentation.health import HealthState, StartupStage
from classification_service.presentation.routes import create_router
from classification_service.presentation.schemas import ErrorDetail, ErrorResponse

RuntimeLoader = Callable[..., LoadedClassificationRuntime]


def terminate_process() -> None:
    os.kill(os.getpid(), signal.SIGTERM)


def create_app(
    *,
    settings: Settings | None = None,
    runtime_loader: RuntimeLoader = load_classification_runtime,
    exit_hook: Callable[[], None] = terminate_process,
) -> FastAPI:
    configured = settings or get_settings()
    health = HealthState()

    def transition(stage: str) -> None:
        health.transition(cast(StartupStage, stage))

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        executor: ThreadInferenceExecutor | None = None
        try:
            transition("validating_release")
            release = validate_release(configured)
            runtime = runtime_loader(
                release,
                minimum_free_gpu_mib=configured.minimum_free_gpu_mib,
                stage_changed=transition,
            )
            executor = ThreadInferenceExecutor(
                waiting_limit=configured.waiting_queue_limit,
                timeout_seconds=configured.inference_timeout_seconds,
            )
            handler = ClassifyTextHandler(
                chunker=runtime.chunker,
                top_triple_classifier=runtime.top_triple_classifier,
                end_doc_classifier=runtime.end_doc_classifier,
                release_id=runtime.release_id,
                executor=executor,
            )
            app.include_router(
                create_router(
                    handler=handler,
                    token=configured.internal_service_token.get_secret_value(),
                    max_text_chars=configured.max_text_chars,
                    mark_unready=lambda: transition("failed"),
                    exit_hook=exit_hook,
                )
            )
            app.state.executor = executor
            transition("ready")
            yield
        except BaseException:
            transition("failed")
            raise
        finally:
            transition("stopping")
            if executor is not None:
                executor.stop_admission()
                executor.shutdown()

    app = FastAPI(lifespan=lifespan)

    @app.exception_handler(RequestValidationError)
    async def validation_error(
        _request: Request, _error: RequestValidationError
    ) -> JSONResponse:
        body = ErrorResponse(
            error=ErrorDetail(code="INVALID_REQUEST", message="request is invalid")
        )
        return JSONResponse(status_code=400, content=body.model_dump())

    @app.exception_handler(HTTPException)
    async def http_error(_request: Request, error: HTTPException) -> JSONResponse:
        if error.status_code == 401:
            body = ErrorResponse(
                error=ErrorDetail(
                    code="UNAUTHORIZED", message="authentication required"
                )
            )
            return JSONResponse(status_code=401, content=body.model_dump())
        body = ErrorResponse(
            error=ErrorDetail(code="HTTP_ERROR", message="request failed")
        )
        return JSONResponse(status_code=error.status_code, content=body.model_dump())

    @app.get("/health/live")
    async def live() -> dict[str, str]:
        return {"status": "live"}

    @app.get("/health/ready")
    async def ready() -> JSONResponse:
        return JSONResponse(
            status_code=200 if health.ready else 503, content={"status": health.stage}
        )

    app.state.health = health
    return app
