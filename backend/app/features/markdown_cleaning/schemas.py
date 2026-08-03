import uuid
from datetime import datetime
from typing import Annotated, Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    computed_field,
    field_validator,
    model_validator,
)

from app.features.markdown_cleaning.state_machine import MarkdownCleaningTaskStatus

NonBlank = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class MarkdownCleaningTaskCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    session_id: NonBlank = Field(alias="sessionId", max_length=128)
    file_id: NonBlank = Field(alias="fileId", max_length=255)
    file_storage_path: NonBlank | None = Field(
        default=None, alias="fileStoragePath", max_length=4096
    )
    file_oss_url: NonBlank | None = Field(
        default=None, alias="fileOssUrl", max_length=4096
    )
    target_path: NonBlank = Field(alias="targetPath", max_length=4096)

    @field_validator("file_storage_path", "file_oss_url", "target_path")
    @classmethod
    def _must_be_markdown(cls, value: str | None) -> str | None:
        if value is not None and not value.lower().endswith((".md", ".markdown")):
            raise ValueError("Markdown 文件路径必须以 .md 或 .markdown 结尾")
        return value

    @model_validator(mode="after")
    def _requires_an_input(self) -> Self:
        if self.file_storage_path is None and self.file_oss_url is None:
            raise ValueError("fileStoragePath 和 fileOssUrl 至少提供一个")
        return self

    @computed_field  # type: ignore[prop-decorator]
    @property
    def selected_input_type(self) -> Literal["local", "remote"]:
        return "local" if self.file_storage_path is not None else "remote"


class MarkdownCleaningTaskAccepted(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    task_id: uuid.UUID = Field(alias="taskId")
    file_id: str = Field(alias="fileId")
    session_id: str = Field(alias="sessionId")
    status: MarkdownCleaningTaskStatus


class MarkdownCleaningRedactionsPublic(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    phone: int = Field(ge=0)
    id_card: int = Field(alias="idCard", ge=0)
    bank_card: int = Field(alias="bankCard", ge=0)
    email: int = Field(ge=0)
    ipv4: int = Field(ge=0)


class MarkdownCleaningSummaryPublic(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    duplicate_paragraphs_removed: int = Field(
        alias="duplicateParagraphsRemoved", ge=0
    )
    redactions: MarkdownCleaningRedactionsPublic
    formatting_changes: int = Field(alias="formattingChanges", ge=0)


class MarkdownCleaningResultPublic(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    file_id: str = Field(alias="fileId")
    file_storage_path: str | None = Field(default=None, alias="fileStoragePath")
    file_oss_url: str | None = Field(default=None, alias="fileOssUrl")
    target_path: str = Field(alias="targetPath")
    summary: MarkdownCleaningSummaryPublic


class MarkdownCleaningErrorPublic(BaseModel):
    code: str
    message: str


class MarkdownCleaningDomainErrorResponse(BaseModel):
    detail: MarkdownCleaningErrorPublic | str


class MarkdownCleaningTaskPublic(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    task_id: uuid.UUID = Field(alias="taskId")
    session_id: str = Field(alias="sessionId")
    file_id: str = Field(alias="fileId")
    status: MarkdownCleaningTaskStatus
    created_at: datetime = Field(alias="createdAt")
    started_at: datetime | None = Field(alias="startedAt")
    finished_at: datetime | None = Field(alias="finishedAt")
    result: MarkdownCleaningResultPublic | None
    error: MarkdownCleaningErrorPublic | None

    @model_validator(mode="after")
    def _result_and_error_match_status(self) -> Self:
        if self.result is not None and self.error is not None:
            raise ValueError("result 和 error 不能同时存在")
        if (
            self.status is MarkdownCleaningTaskStatus.SUCCEEDED
            and self.result is None
        ):
            raise ValueError("成功任务必须包含 result")
        if (
            self.status is not MarkdownCleaningTaskStatus.SUCCEEDED
            and self.result is not None
        ):
            raise ValueError("非成功任务不能包含 result")
        return self
