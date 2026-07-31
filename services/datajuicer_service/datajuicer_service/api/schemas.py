from datetime import datetime
from pathlib import PurePosixPath, PureWindowsPath
from typing import Literal
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from datajuicer_service.jobs.models import DataJuicerJob
from datajuicer_service.jobs.state_machine import JobStatus


def to_camel(value: str) -> str:
    first, *rest = value.split("_")
    return first + "".join(part.capitalize() for part in rest)


class ApiModel(BaseModel):
    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        extra="forbid",
    )


class JobCreatePublic(ApiModel):
    request_id: str = Field(min_length=1, max_length=255)
    profile: Literal["text_exact_minhash_v1"]
    input_path: str = Field(min_length=1, max_length=4096)
    output_path: str = Field(min_length=1, max_length=4096)

    @field_validator("request_id", "input_path", "output_path")
    @classmethod
    def reject_outer_whitespace(cls, value: str) -> str:
        if value != value.strip():
            raise ValueError("value must not contain outer whitespace")
        return value

    @field_validator("input_path", "output_path")
    @classmethod
    def require_absolute_local_path(cls, value: str) -> str:
        if not (
            PurePosixPath(value).is_absolute()
            or PureWindowsPath(value).is_absolute()
        ):
            raise ValueError("path must be absolute")
        return value

    @model_validator(mode="after")
    def require_distinct_paths(self) -> "JobCreatePublic":
        if self.input_path == self.output_path:
            raise ValueError("inputPath and outputPath must differ")
        return self


class JobAccepted(ApiModel):
    job_id: UUID
    request_id: str
    profile: str
    status: JobStatus


class JobProgressPublic(ApiModel):
    phase: str
    total: int | None = Field(ge=0)
    processed: int = Field(ge=0)
    percent: int = Field(ge=0, le=100)


class JobResultPublic(ApiModel):
    output_path: str
    output_sha256: str


class JobErrorPublic(ApiModel):
    code: str
    message: str


class JobPublic(ApiModel):
    job_id: UUID
    request_id: str
    profile: str
    status: JobStatus
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    progress: JobProgressPublic
    result: JobResultPublic | None
    error: JobErrorPublic | None

    @model_validator(mode="after")
    def validate_terminal_payload(self) -> "JobPublic":
        if self.result is not None and self.error is not None:
            raise ValueError("result and error are mutually exclusive")
        if self.status is JobStatus.SUCCEEDED and self.result is None:
            raise ValueError("succeeded job requires result")
        if self.status is JobStatus.FAILED and self.error is None:
            raise ValueError("failed job requires error")
        if self.status not in {JobStatus.SUCCEEDED, JobStatus.FAILED} and (
            self.result is not None or self.error is not None
        ):
            raise ValueError("non-terminal job cannot expose result or error")
        return self


def accepted_from_job(job: DataJuicerJob) -> JobAccepted:
    return JobAccepted(
        job_id=job.job_id,
        request_id=job.request_id,
        profile=job.profile,
        status=job.status,
    )


def public_from_job(job: DataJuicerJob) -> JobPublic:
    result = None
    if job.status is JobStatus.SUCCEEDED and job.output_sha256 is not None:
        result = JobResultPublic(
            output_path=job.output_path,
            output_sha256=job.output_sha256,
        )
    error = None
    if (
        job.status is JobStatus.FAILED
        and job.error_code is not None
        and job.error_message is not None
    ):
        error = JobErrorPublic(code=job.error_code, message=job.error_message)
    return JobPublic(
        job_id=job.job_id,
        request_id=job.request_id,
        profile=job.profile,
        status=job.status,
        created_at=job.created_at,
        started_at=job.started_at,
        finished_at=job.finished_at,
        progress=JobProgressPublic(
            phase=job.processing_phase,
            total=job.progress_total,
            processed=job.progress_processed,
            percent=job.progress_percent,
        ),
        result=result,
        error=error,
    )
