from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import url2pathname

import httpx
import pytest

from app.features.text_classification.adapter import ClassificationClient
from app.features.text_classification.input_preparer import ClassificationInputPreparer
from app.features.text_classification.schemas import ClassificationTaskCreate


def test_create_contract_uses_input_uri_and_camel_case() -> None:
    request = ClassificationTaskCreate.model_validate(
        {"sessionId": "session-1", "fileId": "file-1", "inputUri": "file:///input/a.txt"}
    )

    assert request.input_uri == "file:///input/a.txt"
    assert "text" not in request.model_dump()


def test_preparer_copies_to_task_scoped_staging(tmp_path: Path) -> None:
    source_root = tmp_path / "input"
    source_root.mkdir()
    source = source_root / "document.txt"
    source.write_text("待分类文本", encoding="utf-8")
    staging_root = tmp_path / "staging"
    preparer = ClassificationInputPreparer(
        staging_root=staging_root,
        input_roots=(source_root,),
        max_input_bytes=1024,
    )

    prepared = preparer.prepare("task-1", source.as_uri())

    assert prepared.input_sha256
    assert prepared.size_bytes == len("待分类文本".encode())
    prepared_path = Path(url2pathname(urlsplit(prepared.local_uri).path))
    assert prepared_path.read_text(encoding="utf-8") == "待分类文本"


def test_preparer_rejects_local_path_outside_allowlist(tmp_path: Path) -> None:
    source = tmp_path / "secret.txt"
    source.write_text("secret", encoding="utf-8")
    preparer = ClassificationInputPreparer(
        staging_root=tmp_path / "staging",
        input_roots=(tmp_path / "allowed",),
        max_input_bytes=1024,
    )

    with pytest.raises(ValueError, match="not allowed"):
        preparer.prepare("task-1", source.as_uri())


def test_adapter_sends_only_internal_uri_contract() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["json"] = request.read().decode()
        captured["authorization"] = request.headers.get("Authorization")
        return httpx.Response(
            200,
            json={
                "schemaVersion": "1",
                "requestId": "task-1",
                "tags": ["a", "b", "c", "d"],
                "confidence": {"topTriple": 0.9, "endDoc": 0.8},
                "releaseId": "release-1",
            },
        )

    client = ClassificationClient(
        base_url="http://classification:8000",
        api_token="internal-token",
        transport=httpx.MockTransport(handler),
    )
    result = client.classify("task-1", "file:///staging/task-1/input.txt")

    assert result["tags"] == ["a", "b", "c", "d"]
    assert captured["authorization"] == "Bearer internal-token"
    assert captured["json"] == (
        '{"schemaVersion":"1","requestId":"task-1",'
        '"inputUri":"file:///staging/task-1/input.txt"}'
    )
