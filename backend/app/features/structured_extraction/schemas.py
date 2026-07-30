import uuid
from datetime import datetime
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic.alias_generators import to_camel

TaskStatus = Literal[
    "pending",
    "queued",
    "running",
    "succeeded",
    "failed",
    "cancelled",
]


class CamelModel(BaseModel):
    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        str_strip_whitespace=True,
    )


class ExtractionTaskCreate(CamelModel):
    session_id: str = Field(min_length=1, max_length=128)
    file_id: str = Field(min_length=1, max_length=128)
    file_storage_path: str | None = Field(default=None, min_length=1, max_length=2048)
    file_oss_url: str | None = Field(default=None, min_length=1, max_length=4096)
    target_path: str = Field(min_length=1, max_length=2048)

    @model_validator(mode="after")
    def require_input(self) -> Self:
        if not self.file_storage_path and not self.file_oss_url:
            raise ValueError("fileStoragePath 和 fileOssUrl 至少提供一个")
        return self

    @property
    def selected_input_type(self) -> Literal["local", "remote"]:
        return "local" if self.file_storage_path else "remote"


class ExtractionTaskAccepted(CamelModel):
    task_id: uuid.UUID
    session_id: str
    file_id: str
    status: TaskStatus


class ExtractionResultPublic(CamelModel):
    file_storage_path: str | None
    file_oss_url: str | None
    target_path: str


class ExtractionErrorPublic(CamelModel):
    code: str
    message: str


class ExtractionTaskPublic(CamelModel):
    task_id: uuid.UUID
    session_id: str
    file_id: str
    status: TaskStatus
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    result: ExtractionResultPublic | None = None
    error: ExtractionErrorPublic | None = None

    @model_validator(mode="after")
    def result_and_error_are_mutually_exclusive(self) -> Self:
        if self.result is not None and self.error is not None:
            raise ValueError("result 和 error 不能同时存在")
        return self

