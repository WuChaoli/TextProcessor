import uuid
from datetime import UTC, datetime
from pathlib import Path

from tests.features.global_deduplication.test_submit_orchestration import (
    FakeAdapter,
    FakeScheduler,
    build_orchestrator,
    build_session,
    build_task,
)

NOW = datetime(2026, 7, 31, 12, 0, tzinfo=UTC)


def test_recovery_only_dispatches_due_work(tmp_path: Path) -> None:
    session = build_session()
    input_root = tmp_path / "input"
    input_root.mkdir()
    submit_task = build_task(
        session,
        manifest_path=input_root / "manifest.json",
        target_path=tmp_path / "published" / "submit.json",
    )
    poll_task = build_task(
        session,
        manifest_path=input_root / "manifest2.json",
        target_path=tmp_path / "published" / "poll.json",
    )
    poll_task.status = "running"
    poll_task.external_job_id = uuid.uuid7()
    poll_task.next_poll_at = NOW
    poll_task.lease_expires_at = None
    session.add(poll_task)
    session.commit()
    scheduler = FakeScheduler()
    orchestrator = build_orchestrator(
        session,
        input_root=input_root,
        staging_root=tmp_path / "staging",
        adapter=FakeAdapter(),
        scheduler=scheduler,
        now=NOW,
    )

    summary = orchestrator.recover()

    assert summary.submit_dispatched == 1
    assert summary.poll_dispatched == 1
    assert scheduler.submits == [(submit_task.id, 0)]
    assert scheduler.polls == [(poll_task.id, 0)]
