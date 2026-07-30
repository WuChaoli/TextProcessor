import hashlib
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import func
from sqlalchemy import select as sa_select
from sqlmodel import Session, col, select

from app.features.structured_extraction.models import ProcessorSlot, get_datetime_utc

PROCESSOR_SLOT_ACTIVE = "active"
PROCESSOR_SLOT_QUARANTINED = "quarantined"
PROCESSOR_SLOT_QUARANTINE_EXPIRED = "processor_slot.quarantine_expired"


@dataclass(frozen=True)
class ProcessorSlotReapAlert:
    event: str
    task_id: uuid.UUID
    processor_name: str
    quarantined_at: datetime


def _processor_capacity_lock_key(processor_name: str) -> int:
    digest = hashlib.sha256(f"processor-slot\0{processor_name}".encode()).digest()
    return int.from_bytes(digest[:8], byteorder="big", signed=True)


class ProcessorSlotRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def acquire(
        self,
        *,
        task_id: uuid.UUID,
        processor_name: str,
        max_in_flight: int,
        lease_duration: timedelta,
        now: datetime | None = None,
    ) -> ProcessorSlot | None:
        if max_in_flight < 1:
            raise ValueError("max_in_flight 必须大于零")
        if lease_duration <= timedelta(0):
            raise ValueError("lease_duration 必须大于零")
        acquired_at = now or get_datetime_utc()

        with self._processor_capacity_lock(processor_name):
            existing = self._session.exec(
                select(ProcessorSlot).where(ProcessorSlot.task_id == task_id)
            ).one_or_none()
            if existing is not None:
                self._session.commit()
                return (
                    existing
                    if existing.processor_name == processor_name
                    and existing.state == PROCESSOR_SLOT_ACTIVE
                    else None
                )

            occupied = self._session.exec(
                select(func.count())
                .select_from(ProcessorSlot)
                .where(
                    ProcessorSlot.processor_name == processor_name,
                    col(ProcessorSlot.state).in_(
                        (PROCESSOR_SLOT_ACTIVE, PROCESSOR_SLOT_QUARANTINED)
                    ),
                )
            ).one()
            if occupied >= max_in_flight:
                self._session.commit()
                return None

            slot = ProcessorSlot(
                task_id=task_id,
                processor_name=processor_name,
                state=PROCESSOR_SLOT_ACTIVE,
                acquired_at=acquired_at,
                lease_expires_at=acquired_at + lease_duration,
            )
            self._session.add(slot)
            self._session.commit()
            self._session.refresh(slot)
            return slot

    def refresh(
        self,
        task_id: uuid.UUID,
        *,
        lease_duration: timedelta,
        now: datetime | None = None,
    ) -> ProcessorSlot | None:
        if lease_duration <= timedelta(0):
            raise ValueError("lease_duration 必须大于零")
        slot = self._session.exec(
            select(ProcessorSlot).where(
                ProcessorSlot.task_id == task_id,
                ProcessorSlot.state == PROCESSOR_SLOT_ACTIVE,
            )
        ).one_or_none()
        if slot is None:
            self._session.commit()
            return None
        slot.lease_expires_at = (now or get_datetime_utc()) + lease_duration
        self._session.add(slot)
        self._session.commit()
        self._session.refresh(slot)
        return slot

    def release(self, task_id: uuid.UUID) -> bool:
        slot = self._session.exec(
            select(ProcessorSlot).where(ProcessorSlot.task_id == task_id)
        ).one_or_none()
        if slot is None:
            self._session.commit()
            return False
        self._session.delete(slot)
        self._session.commit()
        return True

    def quarantine(
        self,
        task_id: uuid.UUID,
        *,
        now: datetime | None = None,
    ) -> ProcessorSlot | None:
        slot = self._session.exec(
            select(ProcessorSlot).where(ProcessorSlot.task_id == task_id)
        ).one_or_none()
        if slot is None:
            self._session.commit()
            return None
        if slot.state == PROCESSOR_SLOT_QUARANTINED:
            self._session.commit()
            return slot
        if slot.state != PROCESSOR_SLOT_ACTIVE:
            self._session.commit()
            return None
        slot.state = PROCESSOR_SLOT_QUARANTINED
        slot.quarantined_at = now or get_datetime_utc()
        self._session.add(slot)
        self._session.commit()
        self._session.refresh(slot)
        return slot

    def reap(
        self,
        *,
        quarantine_grace: timedelta,
        now: datetime | None = None,
    ) -> list[ProcessorSlotReapAlert]:
        if quarantine_grace < timedelta(0):
            raise ValueError("quarantine_grace 不能小于零")
        cutoff = (now or get_datetime_utc()) - quarantine_grace
        slots = list(
            self._session.exec(
                select(ProcessorSlot).where(
                    ProcessorSlot.state == PROCESSOR_SLOT_QUARANTINED,
                    col(ProcessorSlot.quarantined_at).is_not(None),
                    col(ProcessorSlot.quarantined_at) <= cutoff,
                )
            ).all()
        )
        alerts: list[ProcessorSlotReapAlert] = []
        for slot in slots:
            quarantined_at = slot.quarantined_at
            if quarantined_at is None:
                continue
            alerts.append(
                ProcessorSlotReapAlert(
                    event=PROCESSOR_SLOT_QUARANTINE_EXPIRED,
                    task_id=slot.task_id,
                    processor_name=slot.processor_name,
                    quarantined_at=quarantined_at,
                )
            )
        for slot in slots:
            self._session.delete(slot)
        self._session.commit()
        return alerts

    @contextmanager
    def _processor_capacity_lock(self, processor_name: str) -> Iterator[None]:
        bind = self._session.get_bind()
        if bind.dialect.name == "postgresql":
            self._session.execute(  # ty: ignore[deprecated]
                sa_select(
                    func.pg_advisory_xact_lock(
                        _processor_capacity_lock_key(processor_name)
                    )
                )
            )
        yield
