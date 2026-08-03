from __future__ import annotations

import json
import logging

from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.features.markdown_cleaning import routes
from app.features.markdown_cleaning.task_models import MarkdownCleaningTask


class FailingDispatcher:
    def enqueue_execute(self, task_id) -> None:
        raise RuntimeError("broker unavailable at C:/internal/redis")


def test_real_api_persists_safe_failure_when_enqueue_fails(
    markdown_cleaning_runtime, caplog
) -> None:
    runtime = markdown_cleaning_runtime
    runtime.source.write_text("# safe\n", encoding="utf-8")
    runtime.app.dependency_overrides[routes.get_markdown_cleaning_dispatcher] = (
        FailingDispatcher
    )
    try:
        with caplog.at_level(logging.WARNING), TestClient(runtime.app) as client:
            response = client.post(
                "/api/v1/markdown-cleaning/tasks",
                json={
                    "sessionId": runtime.session_id,
                    "fileId": "enqueue-failure.md",
                    "fileStoragePath": str(runtime.source),
                    "targetPath": str(runtime.target),
                },
            )
        assert response.status_code == 503
        assert response.json() == {
            "detail": {
                "code": "QUEUE_SUBMISSION_FAILED",
                "message": "任务提交失败，请稍后重试",
            }
        }
        with Session(runtime.engine) as session:
            task = session.exec(
                select(MarkdownCleaningTask).where(
                    MarkdownCleaningTask.session_id == runtime.session_id
                )
            ).one()
            assert task.status == "failed"
            assert task.error_code == "QUEUE_SUBMISSION_FAILED"
            assert task.error_message == "任务提交失败"
            assert task.staging_path is None
        exposed = json.dumps(response.json(), ensure_ascii=False) + caplog.text
        assert str(runtime.staging_root) not in exposed
        assert "C:/internal/redis" not in exposed
    finally:
        runtime.app.dependency_overrides.pop(
            routes.get_markdown_cleaning_dispatcher, None
        )
