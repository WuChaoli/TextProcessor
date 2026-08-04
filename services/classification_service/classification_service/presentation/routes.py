from collections.abc import Callable

from fastapi import APIRouter, Header
from fastapi.responses import JSONResponse

from classification_service.application.classify_text import ClassifyTextHandler
from classification_service.application.dto import ClassifyTextCommand
from classification_service.presentation.authentication import authenticate_bearer
from classification_service.presentation.error_mapping import (
    is_cuda_out_of_memory,
    map_public_error,
)
from classification_service.presentation.schemas import (
    ClassifyRequest,
    ClassifyResponse,
    ConfidenceResponse,
    ErrorDetail,
    ErrorResponse,
)


def create_router(
    *,
    handler: ClassifyTextHandler,
    token: str,
    max_text_chars: int,
    mark_unready: Callable[[], None],
    exit_hook: Callable[[], None],
) -> APIRouter:
    router = APIRouter()

    @router.post("/internal/v1/classify", response_model=ClassifyResponse)
    async def classify(
        payload: ClassifyRequest,
        authorization: str | None = Header(default=None),
        x_request_id: str | None = Header(default=None, alias="X-Request-ID"),
    ) -> ClassifyResponse | JSONResponse:
        authenticate_bearer(authorization, token)
        if x_request_id != payload.requestId:
            return _error(
                400, payload.requestId, "REQUEST_ID_MISMATCH", "request id mismatch"
            )
        if not payload.text.strip():
            return _error(
                400, payload.requestId, "INVALID_TEXT", "text must not be empty"
            )
        if len(payload.text) > max_text_chars:
            return _error(413, payload.requestId, "TEXT_TOO_LARGE", "text is too large")
        try:
            result = await handler.execute(
                ClassifyTextCommand(request_id=payload.requestId, text=payload.text)
            )
        except Exception as caught:
            public = map_public_error(caught)
            if is_cuda_out_of_memory(caught):
                mark_unready()
                exit_hook()
            return _error(
                public.status_code, payload.requestId, public.code, public.message
            )
        return ClassifyResponse(
            requestId=payload.requestId,
            tags=result.tags,
            confidence=ConfidenceResponse(
                topTriple=result.top_triple_confidence, endDoc=result.end_doc_confidence
            ),
            releaseId=result.release_id,
        )

    return router


def _error(
    status: int, request_id: str | None, code: str, message: str
) -> JSONResponse:
    body = ErrorResponse(
        requestId=request_id, error=ErrorDetail(code=code, message=message)
    )
    return JSONResponse(status_code=status, content=body.model_dump())
