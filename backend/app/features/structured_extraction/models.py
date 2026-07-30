import uuid
from datetime import UTC, datetime
from enum import StrEnum

from sqlalchemy import JSON, DateTime, Enum, UniqueConstraint
from sqlmodel import Field, SQLModel


def get_datetime_utc() -> datetime:
    return datetime.now(UTC)


class ExtractionTaskStatus(StrEnum):
    PENDING = "pending"
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ExtractionTask(SQLModel, table=True):
    __tablename__ = "extraction_task"
    __table_args__ = (
        UniqueConstraint(
            "caller_id",
            "session_id",
            "file_id",
            name="uq_extraction_task_caller_session_file",
        ),
    )

    id: uuid.UUID = Field(default_factory=uuid.uuid7, primary_key=True)
    caller_id: uuid.UUID = Field(foreign_key="user.id", index=True)
    session_id: str = Field(max_length=128)
    file_id: str = Field(max_length=128)
    request_fingerprint: str = Field(max_length=64)
    file_storage_path: str | None = Field(default=None, max_length=2048)
    file_oss_url: str | None = Field(default=None, max_length=4096)
    selected_input_type: str = Field(max_length=16)
    target_path: str = Field(max_length=2048)
    status: ExtractionTaskStatus = Field(
        sa_type=Enum(
            ExtractionTaskStatus,
            native_enum=False,
            values_callable=lambda values: [value.value for value in values],
            length=16,
        ),  # type: ignore[call-overload]
        index=True,
    )
    processing_phase: str | None = Field(default=None, max_length=64)
    attempt_count: int = 0
    max_attempts: int = 3
    lease_expires_at: datetime | None = Field(
        default=None,
        sa_type=DateTime(timezone=True),  # type: ignore[call-overload]
    )
    prepared_output_sha256: str | None = Field(default=None, max_length=64)
    staging_path: str | None = Field(default=None, max_length=2048)
    published_at: datetime | None = Field(
        default=None,
        sa_type=DateTime(timezone=True),  # type: ignore[call-overload]
    )
    result_metadata: dict[str, object] | None = Field(default=None, sa_type=JSON)
    error_code: str | None = Field(default=None, max_length=64)
    error_message: str | None = Field(default=None, max_length=512)
    created_at: datetime = Field(
        default_factory=get_datetime_utc,
        sa_type=DateTime(timezone=True),  # type: ignore[call-overload]
    )
    queued_at: datetime | None = Field(
        default=None,
        sa_type=DateTime(timezone=True),  # type: ignore[call-overload]
    )
    started_at: datetime | None = Field(
        default=None,
        sa_type=DateTime(timezone=True),  # type: ignore[call-overload]
    )
    finished_at: datetime | None = Field(
        default=None,
        sa_type=DateTime(timezone=True),  # type: ignore[call-overload]
    )
    updated_at: datetime = Field(
        default_factory=get_datetime_utc,
        sa_type=DateTime(timezone=True),  # type: ignore[call-overload]
    )
