import uuid
from datetime import UTC, datetime

from sqlalchemy import JSON, DateTime, Enum, UniqueConstraint
from sqlmodel import Field, SQLModel

from app.features.global_deduplication.state_machine import (
    GlobalDeduplicationTaskStatus,
)


def get_datetime_utc() -> datetime:
    return datetime.now(UTC)


class GlobalDeduplicationTask(SQLModel, table=True):
    __tablename__ = "global_deduplication_task"  # pyright: ignore[reportAssignmentType]
    __table_args__ = (
        UniqueConstraint(
            "caller_id",
            "session_id",
            name="uq_global_deduplication_caller_session",
        ),
    )

    id: uuid.UUID = Field(default_factory=uuid.uuid7, primary_key=True)
    caller_id: uuid.UUID = Field(foreign_key="user.id", index=True)
    session_id: str = Field(max_length=128)
    request_fingerprint: str = Field(max_length=64)
    input_path: str = Field(max_length=4096)
    status: GlobalDeduplicationTaskStatus = Field(
        sa_type=Enum(
            GlobalDeduplicationTaskStatus,
            native_enum=False,
            values_callable=lambda values: [value.value for value in values],
            length=16,
        ),  # type: ignore[call-overload]  # ty: ignore[invalid-argument-type]
        index=True,
    )
    processing_phase: str | None = Field(default=None, max_length=64)
    progress_total: int | None = Field(default=None, ge=0)
    progress_processed: int = Field(default=0, ge=0)
    progress_percent: int = Field(default=0, ge=0, le=100)
    attempt_count: int = Field(default=0, ge=0)
    max_attempts: int = Field(default=3, gt=0)
    lease_expires_at: datetime | None = Field(
        default=None,
        sa_type=DateTime(timezone=True),  # type: ignore[call-overload]  # ty: ignore[invalid-argument-type]
        index=True,
    )
    external_job_id: uuid.UUID | None = Field(default=None, index=True)
    external_profile: str | None = Field(default=None, max_length=128)
    external_status: str | None = Field(default=None, max_length=32)
    external_progress: dict[str, object] | None = Field(default=None, sa_type=JSON)
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
        index=True,
    )
    external_recovery_attempted: bool = False
    staging_path: str | None = Field(default=None, max_length=4096)
    input_manifest_sha256: str | None = Field(default=None, max_length=64)
    input_jsonl_sha256: str | None = Field(default=None, max_length=64)
    mapping_sha256: str | None = Field(default=None, max_length=64)
    external_output_sha256: str | None = Field(default=None, max_length=64)
    prepared_output_sha256: str | None = Field(default=None, max_length=64)
    output_sha256: str | None = Field(default=None, max_length=64)
    published_at: datetime | None = Field(
        default=None,
        sa_type=DateTime(timezone=True),  # type: ignore[call-overload]  # ty: ignore[invalid-argument-type]
    )
    result_metadata: dict[str, object] | None = Field(default=None, sa_type=JSON)
    error_code: str | None = Field(default=None, max_length=64)
    error_message: str | None = Field(default=None, max_length=512)
    created_at: datetime = Field(
        default_factory=get_datetime_utc,
        sa_type=DateTime(timezone=True),  # type: ignore[call-overload]  # ty: ignore[invalid-argument-type]
    )
    queued_at: datetime | None = Field(
        default=None,
        sa_type=DateTime(timezone=True),  # type: ignore[call-overload]  # ty: ignore[invalid-argument-type]
        index=True,
    )
    last_dispatched_at: datetime | None = Field(
        default=None,
        sa_type=DateTime(timezone=True),  # type: ignore[call-overload]  # ty: ignore[invalid-argument-type]
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
