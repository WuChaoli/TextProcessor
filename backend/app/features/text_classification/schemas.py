import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

from app.tasking.state import TaskStatus


class CamelModel(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, str_strip_whitespace=True)


class ClassificationTaskCreate(CamelModel):
    session_id: str = Field(min_length=1, max_length=128)
    file_id: str = Field(min_length=1, max_length=128)
    input_uri: str = Field(min_length=1, max_length=4096)


class ClassificationTaskAccepted(CamelModel):
    task_id: uuid.UUID
    session_id: str
    file_id: str
    status: TaskStatus
    created_at: datetime


class ClassificationTaskPublic(ClassificationTaskAccepted):
    started_at: datetime | None = None
    finished_at: datetime | None = None
    result: dict[str, object] | None = None
    error: dict[str, str] | None = None
