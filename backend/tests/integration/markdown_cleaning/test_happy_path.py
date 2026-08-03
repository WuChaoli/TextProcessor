from __future__ import annotations

import hashlib
import json
import time

from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlmodel import Session

from app.features.markdown_cleaning.task_models import MarkdownCleaningTask


def test_api_and_real_worker_clean_markdown_end_to_end(
    markdown_cleaning_runtime,
) -> None:
    runtime = markdown_cleaning_runtime
    source_bytes = (
        b"\xef\xbb\xbf#\xe6\xa0\x87\xe9\xa2\x98\n\n"
        b"\xe9\x87\x8d\xe5\xa4\x8d\xe6\xae\xb5\xe8\x90\xbd\n\n"
        b"\xe9\x87\x8d\xe5\xa4\x8d\xe6\xae\xb5\xe8\x90\xbd\n\n"
        b"\xe6\x89\x8b\xe6\x9c\xba 13800138000\xef\xbc\x8c\xe9\x82\xae\xe7\xae\xb1 a@example.com\xe3\x80\x82\n"
    )
    expected = "#标题\n\n重复段落\n\n手机 [PHONE]，邮箱 [EMAIL]。\n".encode()
    runtime.source.write_bytes(source_bytes)

    with TestClient(runtime.app) as client:
        response = client.post(
            "/api/v1/markdown-cleaning/tasks",
            json={
                "sessionId": runtime.session_id,
                "fileId": "中文样本.md",
                "fileStoragePath": str(runtime.source),
                "targetPath": str(runtime.target),
            },
        )
        assert response.status_code == 202, response.text
        accepted = response.json()
        assert accepted["status"] == "queued"
        assert runtime.redis.llen("markdown_cleaning") == 1

        with runtime.worker():
            deadline = time.monotonic() + 45
            while True:
                response = client.get(
                    f"/api/v1/markdown-cleaning/tasks/{accepted['taskId']}"
                )
                assert response.status_code == 200, response.text
                payload = response.json()
                if payload["status"] == "succeeded":
                    break
                assert payload["status"] in {"queued", "running"}, payload
                assert time.monotonic() < deadline, payload
                time.sleep(0.2)

    assert runtime.source.read_bytes() == source_bytes
    assert runtime.source.read_bytes().startswith(b"\xef\xbb\xbf")
    assert runtime.target.read_bytes() == expected
    assert not runtime.target.read_bytes().startswith(b"\xef\xbb\xbf")
    result = payload["result"]
    assert result["targetPath"] == str(runtime.target)
    assert result["summary"] == {
        "duplicateParagraphsRemoved": 1,
        "redactions": {
            "phone": 1,
            "idCard": 0,
            "bankCard": 0,
            "email": 1,
            "ipv4": 0,
        },
        "formattingChanges": 0,
    }
    assert "staging" not in json.dumps(payload, ensure_ascii=False).lower()
    assert str(runtime.staging_root) not in json.dumps(payload, ensure_ascii=False)

    with Session(runtime.engine) as session:
        task = session.get(MarkdownCleaningTask, accepted["taskId"])
        assert task is not None and task.processing_deadline is not None
        assert task.input_sha256 == hashlib.sha256(source_bytes[3:]).hexdigest()
        assert task.prepared_output_sha256 == hashlib.sha256(expected).hexdigest()
        assert task.output_sha256 == hashlib.sha256(expected).hexdigest()
        version = session.exec(text("select version_num from alembic_version")).one()[0]
        assert version == runtime.alembic_head
