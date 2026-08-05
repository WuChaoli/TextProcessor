import uuid
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field

from app.tasking.envelope import TaskEnvelope


class InvalidMarkdownCleaningMessage(ValueError):
    pass


class MarkdownCleaningMessage(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    task_id: uuid.UUID = Field(alias="taskId")
    task_type: Literal["markdown_cleaning"] = Field(alias="taskType")
    schema_version: Literal[1] = Field(alias="schemaVersion")

    @classmethod
    def parse(cls, payload: object) -> Self:
        try:
            envelope = TaskEnvelope.parse(
                payload,
                expected_type="markdown_cleaning",
                expected_schema_version=1,
            )
            return cls(
                taskId=envelope.task_id,
                taskType=envelope.task_type,
                schemaVersion=envelope.schema_version,
            )
        except ValueError as error:
            raise InvalidMarkdownCleaningMessage(
                "INVALID_MARKDOWN_CLEANING_MESSAGE"
            ) from error

    def as_payload(self) -> dict[str, str | int]:
        return {
            "taskId": str(self.task_id),
            "taskType": self.task_type,
            "schemaVersion": self.schema_version,
        }
