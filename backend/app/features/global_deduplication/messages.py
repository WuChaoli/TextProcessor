import uuid
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, ValidationError


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
            return cls.model_validate(payload)
        except ValidationError as error:
            raise InvalidGlobalDeduplicationMessage(
                "INVALID_GLOBAL_DEDUPLICATION_MESSAGE"
            ) from error

    def as_payload(self) -> dict[str, str | int]:
        return {
            "taskId": str(self.task_id),
            "taskType": self.task_type,
            "schemaVersion": self.schema_version,
        }
