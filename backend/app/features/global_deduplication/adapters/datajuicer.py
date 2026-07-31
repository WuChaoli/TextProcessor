import re
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal, Self

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from app.features.global_deduplication.errors import (
    GlobalDeduplicationErrorCode,
    GlobalDeduplicationProcessingError,
)

DataJuicerProfile = Literal["text_exact_minhash_v1"]
DataJuicerStatus = Literal[
    "pending",
    "queued",
    "running",
    "succeeded",
    "failed",
    "cancelled",
]
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True, slots=True)
class DataJuicerSubmitRequest:
    request_id: uuid.UUID
    input_path: Path
    output_path: Path
    profile: DataJuicerProfile

    def to_public_json(self) -> dict[str, str]:
        return {
            "requestId": str(self.request_id),
            "profile": self.profile,
            "inputPath": str(self.input_path),
            "outputPath": str(self.output_path),
        }


@dataclass(frozen=True, slots=True)
class DataJuicerSubmission:
    job_id: uuid.UUID
    request_id: uuid.UUID
    profile: DataJuicerProfile
    status: Literal["pending", "queued"]


@dataclass(frozen=True, slots=True)
class DataJuicerProgress:
    phase: str
    total: int | None
    processed: int
    percent: int


@dataclass(frozen=True, slots=True)
class DataJuicerResult:
    output_path: Path
    output_sha256: str


@dataclass(frozen=True, slots=True)
class DataJuicerError:
    code: str
    message: str


@dataclass(frozen=True, slots=True)
class DataJuicerJob:
    job_id: uuid.UUID
    request_id: uuid.UUID
    profile: DataJuicerProfile
    status: DataJuicerStatus
    progress: DataJuicerProgress
    result: DataJuicerResult | None
    error: DataJuicerError | None


class _SubmissionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    job_id: uuid.UUID = Field(alias="jobId")
    request_id: uuid.UUID = Field(alias="requestId")
    profile: DataJuicerProfile
    status: Literal["pending", "queued"]


class _ProgressResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    phase: str = Field(min_length=1, max_length=64)
    total: int | None = Field(ge=0)
    processed: int = Field(ge=0)
    percent: int = Field(ge=0, le=100)

    @model_validator(mode="after")
    def _validate_total(self) -> Self:
        if self.total is not None and self.processed > self.total:
            raise ValueError("processed cannot exceed total")
        return self


class _ResultResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    output_path: str = Field(alias="outputPath", min_length=1, max_length=4096)
    output_sha256: str = Field(alias="outputSha256")

    @model_validator(mode="after")
    def _validate_sha256(self) -> Self:
        if not _SHA256.fullmatch(self.output_sha256):
            raise ValueError("invalid sha256")
        return self


class _ErrorResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str = Field(min_length=1, max_length=128)
    message: str = Field(min_length=1, max_length=512)


class _JobResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    job_id: uuid.UUID = Field(alias="jobId")
    request_id: uuid.UUID = Field(alias="requestId")
    profile: DataJuicerProfile
    status: DataJuicerStatus
    progress: _ProgressResponse
    result: _ResultResponse | None
    error: _ErrorResponse | None
    created_at: datetime | None = Field(default=None, alias="createdAt")
    started_at: datetime | None = Field(default=None, alias="startedAt")
    finished_at: datetime | None = Field(default=None, alias="finishedAt")

    @model_validator(mode="after")
    def _validate_terminal_payload(self) -> Self:
        if self.result is not None and self.error is not None:
            raise ValueError("result and error are mutually exclusive")
        if self.status == "succeeded" and self.result is None:
            raise ValueError("succeeded requires result")
        if self.status == "failed" and self.error is None:
            raise ValueError("failed requires error")
        if self.status == "cancelled" and self.result is not None:
            raise ValueError("cancelled cannot expose result")
        if self.status in {"pending", "queued", "running"} and (
            self.result is not None or self.error is not None
        ):
            raise ValueError("nonterminal job cannot expose result or error")
        return self


class DataJuicerAdapter:
    def __init__(
        self,
        *,
        base_url: str,
        client: httpx.Client,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._client = client

    def submit(
        self,
        request: DataJuicerSubmitRequest,
    ) -> DataJuicerSubmission:
        try:
            response = self._client.post(
                f"{self._base_url}/v1/jobs",
                json=request.to_public_json(),
            )
        except (httpx.ReadTimeout, httpx.WriteTimeout):
            raise self._error(
                GlobalDeduplicationErrorCode.PROCESSOR_SUBMISSION_UNCERTAIN,
                "处理器提交结果不确定",
                transient=True,
            ) from None
        except (httpx.ConnectError, httpx.ConnectTimeout):
            raise self._error(
                GlobalDeduplicationErrorCode.PROCESSOR_UNAVAILABLE,
                "处理器当前不可用",
                transient=True,
            ) from None
        except httpx.HTTPError:
            raise self._error(
                GlobalDeduplicationErrorCode.PROCESSOR_SUBMISSION_FAILED,
                "处理器任务提交失败",
                transient=True,
            ) from None
        if response.status_code != 202:
            raise self._submit_http_error(response)
        try:
            parsed = _SubmissionResponse.model_validate(response.json())
        except (ValueError, ValidationError):
            raise self._invalid_response() from None
        if (
            parsed.request_id != request.request_id
            or parsed.profile != request.profile
        ):
            raise self._invalid_response()
        return DataJuicerSubmission(
            job_id=parsed.job_id,
            request_id=parsed.request_id,
            profile=parsed.profile,
            status=parsed.status,
        )

    def get_job(
        self,
        job_id: uuid.UUID,
        *,
        expected_request_id: uuid.UUID,
        expected_profile: DataJuicerProfile,
        expected_output_path: Path,
    ) -> DataJuicerJob:
        try:
            response = self._client.get(f"{self._base_url}/v1/jobs/{job_id}")
        except httpx.HTTPError:
            raise self._error(
                GlobalDeduplicationErrorCode.PROCESSOR_POLL_FAILED,
                "处理器任务查询失败",
                transient=True,
            ) from None
        if response.status_code == 404:
            raise self._error(
                GlobalDeduplicationErrorCode.PROCESSOR_JOB_NOT_FOUND,
                "处理器任务不存在",
            )
        if response.status_code != 200:
            raise self._error(
                GlobalDeduplicationErrorCode.PROCESSOR_POLL_FAILED,
                "处理器任务查询失败",
                transient=response.status_code >= 500
                or response.status_code == 429,
            )
        try:
            parsed = _JobResponse.model_validate(response.json())
        except (ValueError, ValidationError):
            raise self._invalid_response() from None
        if (
            parsed.job_id != job_id
            or parsed.request_id != expected_request_id
            or parsed.profile != expected_profile
        ):
            raise self._invalid_response()
        result = None
        if parsed.result is not None:
            output_path = Path(parsed.result.output_path)
            if output_path != expected_output_path:
                raise self._invalid_response()
            result = DataJuicerResult(
                output_path=output_path,
                output_sha256=parsed.result.output_sha256,
            )
        error = (
            None
            if parsed.error is None
            else DataJuicerError(
                code=parsed.error.code,
                message=parsed.error.message,
            )
        )
        return DataJuicerJob(
            job_id=parsed.job_id,
            request_id=parsed.request_id,
            profile=parsed.profile,
            status=parsed.status,
            progress=DataJuicerProgress(
                phase=parsed.progress.phase,
                total=parsed.progress.total,
                processed=parsed.progress.processed,
                percent=parsed.progress.percent,
            ),
            result=result,
            error=error,
        )

    def _submit_http_error(
        self,
        response: httpx.Response,
    ) -> GlobalDeduplicationProcessingError:
        if response.status_code == 409:
            return self._error(
                GlobalDeduplicationErrorCode.PROCESSOR_IDEMPOTENCY_CONFLICT,
                "处理器幂等参数冲突",
            )
        if response.status_code in {429, 500, 502, 503, 504}:
            return self._error(
                GlobalDeduplicationErrorCode.PROCESSOR_UNAVAILABLE,
                "处理器当前不可用",
                transient=True,
            )
        return self._error(
            GlobalDeduplicationErrorCode.PROCESSOR_REQUEST_REJECTED,
            "处理器拒绝了任务请求",
        )

    @staticmethod
    def _invalid_response() -> GlobalDeduplicationProcessingError:
        return GlobalDeduplicationProcessingError(
            GlobalDeduplicationErrorCode.INVALID_PROCESSOR_RESPONSE,
            "处理器返回了不符合契约的响应",
        )

    @staticmethod
    def _error(
        code: GlobalDeduplicationErrorCode,
        message: str,
        *,
        transient: bool = False,
    ) -> GlobalDeduplicationProcessingError:
        return GlobalDeduplicationProcessingError(
            code,
            message,
            transient=transient,
        )
