from __future__ import annotations

import hashlib
from pathlib import Path

from sqlmodel import Session

from app.features.markdown_cleaning.repository import MarkdownCleaningTaskRepository
from app.features.markdown_cleaning.state_machine import MarkdownCleaningTaskStatus
from tests.integration.markdown_cleaning.conftest import (
    build_real_orchestrator,
    persist_queued_task,
)


def test_real_postgres_processor_pipeline_preserves_bom_truth_and_statistics(
    pg_session: Session, caller, pipeline_roots: dict[str, Path]
) -> None:
    source = pipeline_roots["input"] / "中文样本.md"
    target = pipeline_roots["output"] / "result.md"
    raw = (
        "\ufeff#标题\n\n重复段落\n\n重复段落\n\n"
        "手机 13800138000，身份证 11010519491231002X，银行卡 4111111111111111，"
        "邮箱 a@example.com，IP 192.168.1.1。\n\n"
        "`13800138000`\n\n```text\na@example.com\n```\n"
    ).encode()
    source.write_bytes(raw)
    task = persist_queued_task(pg_session, caller, source=source, target=target)

    build_real_orchestrator(pg_session, pipeline_roots).execute(task.id)

    saved = MarkdownCleaningTaskRepository(pg_session).get(task.id)
    assert saved is not None and saved.status is MarkdownCleaningTaskStatus.SUCCEEDED
    output = target.read_bytes()
    assert output == target.read_text(encoding="utf-8").encode("utf-8")
    assert saved.output_sha256 == hashlib.sha256(output).hexdigest()
    assert saved.duplicate_paragraphs_removed == 1
    assert (saved.phone_redaction_count, saved.id_card_redaction_count) == (1, 1)
    assert (saved.bank_card_redaction_count, saved.email_redaction_count) == (1, 1)
    assert saved.ipv4_redaction_count == 1
    assert b"`13800138000`" in output and b"a@example.com\n```" in output
    assert saved.processing_deadline is not None


def test_duplicate_broker_delivery_is_terminally_idempotent(
    pg_session: Session, caller, pipeline_roots: dict[str, Path]
) -> None:
    source = pipeline_roots["input"] / "once.md"
    target = pipeline_roots["output"] / "once-result.md"
    source.write_text("# Once\n", encoding="utf-8")
    task = persist_queued_task(pg_session, caller, source=source, target=target)
    worker = build_real_orchestrator(pg_session, pipeline_roots)
    worker.execute(task.id)
    first = target.read_bytes()
    worker.execute(task.id)
    assert target.read_bytes() == first
    assert MarkdownCleaningTaskRepository(pg_session).get(task.id).attempt_count == 1


def test_path_escape_and_output_conflict_are_safe_terminal_failures(
    pg_session: Session, caller, pipeline_roots: dict[str, Path], tmp_path: Path
) -> None:
    outside = tmp_path / "outside.md"
    outside.write_text("secret\n", encoding="utf-8")
    escaped = persist_queued_task(
        pg_session, caller, source=outside, target=pipeline_roots["output"] / "x.md"
    )
    build_real_orchestrator(pg_session, pipeline_roots).execute(escaped.id)
    assert (
        MarkdownCleaningTaskRepository(pg_session).get(escaped.id).status
        is MarkdownCleaningTaskStatus.FAILED
    )

    source = pipeline_roots["input"] / "conflict.md"
    target = pipeline_roots["output"] / "exists.md"
    source.write_text("safe\n", encoding="utf-8")
    target.write_bytes(b"owner")
    conflict = persist_queued_task(pg_session, caller, source=source, target=target)
    build_real_orchestrator(pg_session, pipeline_roots).execute(conflict.id)
    saved = MarkdownCleaningTaskRepository(pg_session).get(conflict.id)
    assert saved.error_code == "OUTPUT_CONFLICT"
    assert target.read_bytes() == b"owner"
