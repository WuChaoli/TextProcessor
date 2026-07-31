import hashlib
import os
import time
import uuid
from pathlib import Path

import httpx


def assert_markdown(path: Path) -> tuple[int, str]:
    payload = path.read_bytes()
    assert payload
    assert not payload.startswith(b"\xef\xbb\xbf")
    lowered = payload.lower()
    assert b"<img" not in lowered
    assert b"data:image" not in lowered
    return len(payload), hashlib.sha256(payload).hexdigest()


base_url = "http://127.0.0.1:8000/api/v1"
cases = (
    ("pdf", "/e2e/input/Dify平台+工作流部署手册（ARM64在线版）20251206.pdf"),
    ("xlsx", "/e2e/input/20260108-九小场所图片识患表单.xlsx"),
)

with httpx.Client(base_url=base_url, timeout=30) as client:
    login = client.post(
        "/login/access-token",
        data={
            "username": os.environ["FIRST_SUPERUSER"],
            "password": os.environ["FIRST_SUPERUSER_PASSWORD"],
        },
    )
    login.raise_for_status()
    token = login.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    for format_name, source_path in cases:
        target_path = f"/e2e/output/{format_name}.md"
        accepted = client.post(
            "/structured-extraction/tasks",
            headers=headers,
            json={
                "sessionId": f"e2e-{uuid.uuid4()}",
                "fileId": f"e2e-{format_name}",
                "fileStoragePath": source_path,
                "targetPath": target_path,
            },
        )
        assert accepted.status_code == 202, accepted.text
        task_id = accepted.json()["taskId"]
        deadline = time.monotonic() + 900
        while True:
            response = client.get(
                f"/structured-extraction/tasks/{task_id}",
                headers=headers,
            )
            response.raise_for_status()
            task = response.json()
            if task["status"] in {"succeeded", "failed", "cancelled"}:
                break
            assert time.monotonic() < deadline, f"{format_name} timed out"
            time.sleep(2)

        assert task["status"] == "succeeded", task.get("error")
        result = task["result"]
        assert result["targetPath"] == target_path
        assert result["routing"]["detectedFormat"] == format_name
        assert len(result["inputSha256"]) == 64
        assert len(result["outputSha256"]) == 64
        output = Path(target_path)
        size, digest = assert_markdown(output)
        assert digest == result["outputSha256"]
        print(
            "PASS"
            f" format={format_name}"
            f" processor={result['processor']['name']}"
            f" status={task['status']}"
            f" bytes={size}"
            f" sha256={digest}"
        )
