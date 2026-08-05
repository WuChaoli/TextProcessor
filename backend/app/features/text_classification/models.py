import uuid
from datetime import UTC, datetime

from sqlalchemy import JSON, BigInteger, DateTime, Enum, UniqueConstraint
from sqlmodel import Field, SQLModel

from app.tasking.state import TaskStatus


def utc_now() -> datetime:
    return datetime.now(UTC)


class ClassificationTask(SQLModel, table=True):
    __tablename__ = "classification_task"
    __table_args__ = (UniqueConstraint("caller_id", "session_id", "file_id", name="uq_classification_task_caller_session_file"),)

    id: uuid.UUID = Field(default_factory=uuid.uuid7, primary_key=True)
    caller_id: uuid.UUID = Field(foreign_key="user.id", ondelete="CASCADE", index=True)
    session_id: str = Field(max_length=128)
    file_id: str = Field(max_length=128)
    request_fingerprint: str = Field(max_length=64)
    input_uri: str = Field(max_length=4096)
    status: TaskStatus = Field(sa_type=Enum(TaskStatus, native_enum=False, values_callable=lambda values: [value.value for value in values], length=16), index=True)  # type: ignore[call-overload]
    staging_uri: str | None = Field(default=None, max_length=4096)
    input_sha256: str | None = Field(default=None, max_length=64)
    input_size_bytes: int | None = Field(default=None, ge=0, sa_type=BigInteger)  # type: ignore[call-overload]
    result: dict[str, object] | None = Field(default=None, sa_type=JSON)
    error_code: str | None = Field(default=None, max_length=64)
    error_message: str | None = Field(default=None, max_length=512)
    created_at: datetime = Field(default_factory=utc_now, sa_type=DateTime(timezone=True))  # type: ignore[call-overload]
    queued_at: datetime | None = Field(default=None, sa_type=DateTime(timezone=True))  # type: ignore[call-overload]
    last_dispatched_at: datetime | None = Field(default=None, sa_type=DateTime(timezone=True))  # type: ignore[call-overload]
    started_at: datetime | None = Field(default=None, sa_type=DateTime(timezone=True))  # type: ignore[call-overload]
    finished_at: datetime | None = Field(default=None, sa_type=DateTime(timezone=True))  # type: ignore[call-overload]
    updated_at: datetime = Field(default_factory=utc_now, sa_type=DateTime(timezone=True))  # type: ignore[call-overload]
