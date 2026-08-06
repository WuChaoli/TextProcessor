import uuid
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field

from app.tasking.envelope import TaskEnvelope


class InvalidGlobalDeduplicationMessage(ValueError):
    pass


class GlobalDeduplicationMessage(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    task_id: uuid.UUID = Field(alias="taskId")
    task_type: Literal["global_deduplication"] = Field(alias="taskType")
    schema_version: Literal[1] = Field(alias="schemaVersion")

    @classmethod
    def parse(cls, payload: object) -> Self:
        try:
            envelope = TaskEnvelope.parse(
                payload,
                expected_type="global_deduplication",
                expected_schema_version=1,
            )
            return cls(
                taskId=envelope.task_id,
                taskType="global_deduplication",
                schemaVersion=1,
            )
        except ValueError as error:
            raise InvalidGlobalDeduplicationMessage(
                "INVALID_GLOBAL_DEDUPLICATION_MESSAGE"
            ) from error

    def as_payload(self) -> dict[str, str | int]:
        return {
            "taskId": str(self.task_id),
            "taskType": self.task_type,
            "schemaVersion": self.schema_version,
        }
