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
        ),  # type: ignore[call-overload]  # ty: ignore[invalid-argument-type]
        index=True,
    )
    processing_phase: str | None = Field(default=None, max_length=64)
    attempt_count: int = 0
    max_attempts: int = 3
    lease_expires_at: datetime | None = Field(
        default=None,
        sa_type=DateTime(timezone=True),  # type: ignore[call-overload]  # ty: ignore[invalid-argument-type]
    )
    prepared_output_sha256: str | None = Field(default=None, max_length=64)
    staging_path: str | None = Field(default=None, max_length=2048)
    published_at: datetime | None = Field(
        default=None,
        sa_type=DateTime(timezone=True),  # type: ignore[call-overload]  # ty: ignore[invalid-argument-type]
    )
    result_metadata: dict[str, object] | None = Field(default=None, sa_type=JSON)
    detected_format: str | None = Field(default=None, max_length=32)
    routing_reasons: list[str] | None = Field(default=None, sa_type=JSON)
    processor_name: str | None = Field(default=None, max_length=32)
    processor_version: str | None = Field(default=None, max_length=128)
    profile_name: str | None = Field(default=None, max_length=128)
    profile_sha256: str | None = Field(default=None, max_length=64)
    external_task_id: str | None = Field(default=None, max_length=256)
    next_poll_at: datetime | None = Field(
        default=None,
        sa_type=DateTime(timezone=True),  # type: ignore[call-overload]  # ty: ignore[invalid-argument-type]
        index=True,
    )
    processing_deadline: datetime | None = Field(
        default=None,
        sa_type=DateTime(timezone=True),  # type: ignore[call-overload]  # ty: ignore[invalid-argument-type]
    )
    poll_lease_expires_at: datetime | None = Field(
        default=None,
        sa_type=DateTime(timezone=True),  # type: ignore[call-overload]  # ty: ignore[invalid-argument-type]
    )
    input_sha256: str | None = Field(default=None, max_length=64)
    input_size_bytes: int | None = Field(default=None, ge=0)
    output_sha256: str | None = Field(default=None, max_length=64)
    error_code: str | None = Field(default=None, max_length=64)
    error_message: str | None = Field(default=None, max_length=512)
    created_at: datetime = Field(
        default_factory=get_datetime_utc,
        sa_type=DateTime(timezone=True),  # type: ignore[call-overload]  # ty: ignore[invalid-argument-type]
    )
    queued_at: datetime | None = Field(
        default=None,
        sa_type=DateTime(timezone=True),  # type: ignore[call-overload]  # ty: ignore[invalid-argument-type]
    )
    last_dispatched_at: datetime | None = Field(
        default=None,
        sa_type=DateTime(timezone=True),  # type: ignore[call-overload]  # ty: ignore[invalid-argument-type]
        index=True,
    )
    started_at: datetime | None = Field(
        default=None,
        sa_type=DateTime(timezone=True),  # type: ignore[call-overload]  # ty: ignore[invalid-argument-type]
    )
    finished_at: datetime | None = Field(
        default=None,
        sa_type=DateTime(timezone=True),  # type: ignore[call-overload]  # ty: ignore[invalid-argument-type]
    )
    updated_at: datetime = Field(
        default_factory=get_datetime_utc,
        sa_type=DateTime(timezone=True),  # type: ignore[call-overload]  # ty: ignore[invalid-argument-type]
    )


class ProcessorSlot(SQLModel, table=True):
    __tablename__ = "processor_slot"

    id: uuid.UUID = Field(default_factory=uuid.uuid7, primary_key=True)
    processor_name: str = Field(index=True, max_length=32)
    task_id: uuid.UUID = Field(
        foreign_key="extraction_task.id",
        ondelete="CASCADE",
        unique=True,
    )
    state: str = Field(max_length=16)
    acquired_at: datetime = Field(
        sa_type=DateTime(timezone=True),  # type: ignore[call-overload]  # ty: ignore[invalid-argument-type]
    )
    lease_expires_at: datetime = Field(
        sa_type=DateTime(timezone=True),  # type: ignore[call-overload]  # ty: ignore[invalid-argument-type]
    )
    quarantined_at: datetime | None = Field(
        default=None,
        sa_type=DateTime(timezone=True),  # type: ignore[call-overload]  # ty: ignore[invalid-argument-type]
    )
