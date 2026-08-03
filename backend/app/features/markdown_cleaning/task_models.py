import uuid
from datetime import UTC, datetime

from sqlalchemy import Column, DateTime, Enum, Integer, String, UniqueConstraint
from sqlmodel import Field, SQLModel

from app.features.markdown_cleaning.state_machine import MarkdownCleaningTaskStatus


def get_datetime_utc() -> datetime:
    return datetime.now(UTC)


class MarkdownCleaningTask(SQLModel, table=True):
    __tablename__ = "markdown_cleaning_task"  # pyright: ignore[reportAssignmentType]
    __table_args__ = (
        UniqueConstraint(
            "caller_id",
            "session_id",
            "file_id",
            name="uq_markdown_cleaning_caller_session_file",
        ),
    )

    id: uuid.UUID = Field(default_factory=uuid.uuid7, primary_key=True)
    caller_id: uuid.UUID = Field(
        foreign_key="user.id",
        ondelete="CASCADE",
        index=True,
    )
    session_id: str = Field(max_length=128)
    file_id: str = Field(max_length=255)
    request_fingerprint: str = Field(max_length=64)
    file_storage_path: str | None = Field(default=None, max_length=4096)
    file_oss_url: str | None = Field(default=None, max_length=4096)
    selected_input_type: str = Field(max_length=16)
    target_path: str = Field(max_length=4096)
    processor_contract_version: str = Field(
        default="markdown_cleaning_v1",
        max_length=64,
        sa_column=Column(
            String(length=64),
            nullable=False,
            server_default="'markdown_cleaning_v1'",
        ),
    )
    status: MarkdownCleaningTaskStatus = Field(
        sa_type=Enum(
            MarkdownCleaningTaskStatus,
            native_enum=False,
            values_callable=lambda values: [value.value for value in values],
            length=16,
        ),  # type: ignore[call-overload]  # ty: ignore[invalid-argument-type]
        index=True,
    )
    processing_phase: str | None = Field(default=None, max_length=64)
    progress_percent: int = Field(default=0, ge=0, le=100)
    attempt_count: int = Field(default=0, ge=0)
    max_attempts: int = Field(
        default=3,
        gt=0,
        sa_column=Column(
            Integer,
            nullable=False,
            server_default="3",
        ),
    )
    lease_token: str | None = Field(default=None, max_length=128)
    lease_expires_at: datetime | None = Field(
        default=None,
        sa_type=DateTime(timezone=True),  # type: ignore[call-overload]  # ty: ignore[invalid-argument-type]
        index=True,
    )
    processing_deadline: datetime | None = Field(
        default=None,
        sa_type=DateTime(timezone=True),  # type: ignore[call-overload]  # ty: ignore[invalid-argument-type]
        index=True,
    )
    staging_path: str | None = Field(default=None, max_length=4096)
    input_sha256: str | None = Field(default=None, max_length=64)
    prepared_output_sha256: str | None = Field(default=None, max_length=64)
    output_sha256: str | None = Field(default=None, max_length=64)
    published_at: datetime | None = Field(
        default=None,
        sa_type=DateTime(timezone=True),  # type: ignore[call-overload]  # ty: ignore[invalid-argument-type]
    )
    duplicate_paragraphs_removed: int | None = Field(default=None, ge=0)
    phone_redaction_count: int | None = Field(default=None, ge=0)
    id_card_redaction_count: int | None = Field(default=None, ge=0)
    bank_card_redaction_count: int | None = Field(default=None, ge=0)
    email_redaction_count: int | None = Field(default=None, ge=0)
    ipv4_redaction_count: int | None = Field(default=None, ge=0)
    formatting_change_count: int | None = Field(default=None, ge=0)
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
