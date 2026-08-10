import uuid
from datetime import datetime
from typing import Annotated, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    model_validator,
)

from app.features.global_deduplication.state_machine import (
    GlobalDeduplicationTaskStatus,
)

NonBlank = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class GlobalDeduplicationTaskCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    session_id: NonBlank = Field(alias="sessionId", max_length=128)
    input_path: NonBlank = Field(alias="inputPath", max_length=4096)


class GlobalDeduplicationTaskAccepted(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    task_id: uuid.UUID = Field(alias="taskId")
    session_id: str = Field(alias="sessionId")
    status: GlobalDeduplicationTaskStatus


class GlobalDeduplicationProgressPublic(BaseModel):
    phase: str | None
    total: int | None
    processed: int
    percent: int


class GlobalDeduplicationMoveFailurePublic(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    relative_path: str = Field(alias="relativePath")
    code: str


class GlobalDeduplicationResultPublic(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    total_files: int = Field(alias="totalFiles", ge=0)
    unique_files: int = Field(alias="uniqueFiles", ge=0)
    moved_duplicates: int = Field(alias="movedDuplicates", ge=0)
    move_failures: list[GlobalDeduplicationMoveFailurePublic] = Field(
        alias="moveFailures"
    )


class GlobalDeduplicationErrorPublic(BaseModel):
    code: str
    message: str


class GlobalDeduplicationTaskPublic(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    task_id: uuid.UUID = Field(alias="taskId")
    session_id: str = Field(alias="sessionId")
    status: GlobalDeduplicationTaskStatus
    created_at: datetime = Field(alias="createdAt")
    started_at: datetime | None = Field(alias="startedAt")
    finished_at: datetime | None = Field(alias="finishedAt")
    progress: GlobalDeduplicationProgressPublic
    result: GlobalDeduplicationResultPublic | None
    error: GlobalDeduplicationErrorPublic | None

    @model_validator(mode="after")
    def _result_and_error_match_status(self) -> Self:
        if self.result is not None and self.error is not None:
            raise ValueError("result 和 error 不能同时存在")
        if (
            self.status is GlobalDeduplicationTaskStatus.SUCCEEDED
            and self.result is None
        ):
            raise ValueError("成功任务必须包含 result")
        if (
            self.status is not GlobalDeduplicationTaskStatus.SUCCEEDED
            and self.result is not None
        ):
            raise ValueError("非成功任务不能包含 result")
        return self
