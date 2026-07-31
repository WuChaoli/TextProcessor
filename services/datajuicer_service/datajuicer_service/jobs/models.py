from datetime import datetime
from uuid import UUID

from sqlalchemy import CheckConstraint, DateTime, Enum, Index, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from datajuicer_service.jobs.state_machine import JobStatus


class Base(DeclarativeBase):
    pass


job_status_type = Enum(
    JobStatus,
    values_callable=lambda members: [member.value for member in members],
    native_enum=False,
    length=16,
)


class DataJuicerJob(Base):
    __tablename__ = "datajuicer_jobs"
    __table_args__ = (
        CheckConstraint(
            "NOT (output_sha256 IS NOT NULL AND error_code IS NOT NULL)",
            name="ck_datajuicer_job_result_error_exclusive",
        ),
        Index("ix_datajuicer_jobs_status_updated", "status", "updated_at"),
        Index("ix_datajuicer_jobs_lease_expires", "lease_expires_at"),
    )

    job_id: Mapped[UUID] = mapped_column(primary_key=True)
    request_id: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    request_fingerprint: Mapped[str] = mapped_column(String(64))
    profile: Mapped[str] = mapped_column(String(64))
    input_path: Mapped[str] = mapped_column(Text)
    output_path: Mapped[str] = mapped_column(Text)
    status: Mapped[JobStatus] = mapped_column(job_status_type)
    processing_phase: Mapped[str] = mapped_column(String(64))
    progress_total: Mapped[int | None] = mapped_column(Integer, nullable=True)
    progress_processed: Mapped[int] = mapped_column(Integer)
    progress_percent: Mapped[int] = mapped_column(Integer)
    attempt_count: Mapped[int] = mapped_column(Integer)
    max_attempts: Mapped[int] = mapped_column(Integer)
    lease_token: Mapped[UUID | None] = mapped_column(nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    processing_deadline: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    input_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    input_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    prepared_output_sha256: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
    )
    staging_output_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    output_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(String(512), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    queued_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
