from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from classification_service.bootstrap import create_app
from classification_service.domain.classification_result import ModelPrediction
from classification_service.infrastructure.config import Settings
from classification_service.infrastructure.model.runtime import (
    LoadedClassificationRuntime,
)
from classification_service.presentation.error_mapping import CudaOutOfMemoryError


class FakeChunker:
    def chunk(self, text: str) -> tuple[str, ...]:
        return (text,)


class FakeTopClassifier:
    def predict(self, chunks: tuple[str, ...]) -> ModelPrediction:
        marker = chunks[0]
        if marker == "capacity":
            from classification_service.application.ports.inference_executor import (
                InferenceCapacityExceeded,
            )

            raise InferenceCapacityExceeded
        if marker == "timeout":
            raise TimeoutError
        if marker == "oom":
            raise CudaOutOfMemoryError
        if marker == "torch-oom":
            torch_error = type(
                "OutOfMemoryError", (RuntimeError,), {"__module__": "torch.cuda"}
            )
            raise torch_error
        if marker == "secret C:\\models\\release":
            raise RuntimeError(marker)
        return ModelPrediction("应急 > 安全生产 > 危化品", 0.72)


class FakeEndClassifier:
    def predict(self, chunks: tuple[str, ...]) -> ModelPrediction:
        return ModelPrediction("法规标准类", 0.81)


def settings(tmp_path: Path) -> Settings:
    release = tmp_path / "release"
    release.mkdir()
    input_root = tmp_path / "staging"
    input_root.mkdir()
    return Settings(
        environment="development",
        internal_service_token=SecretStr("token"),
        model_root=tmp_path,
        model_release=release,
        model_release_sha256="a" * 64,
        release_quality_status="experimental",
        input_root=input_root,
    )


@pytest.fixture
def client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[TestClient, list[str], list[bool]]:
    stages: list[str] = []
    exited: list[bool] = []
    monkeypatch.setattr(
        "classification_service.bootstrap.validate_release", lambda _: SimpleNamespace()
    )

    def loader(_: object, **kwargs: Any) -> LoadedClassificationRuntime:
        callback = kwargs["stage_changed"]
        for stage in (
            "loading_tokenizer",
            "loading_top_triple_classifier",
            "loading_end_doc_classifier",
            "smoke_testing",
        ):
            stages.append(stage)
            callback(stage)
        return LoadedClassificationRuntime(
            release_id="release-1",
            device="cuda:0",
            chunker=FakeChunker(),
            top_triple_classifier=FakeTopClassifier(),
            end_doc_classifier=FakeEndClassifier(),
        )  # type: ignore[arg-type]

    app = create_app(
        settings=settings(tmp_path),
        runtime_loader=loader,
        exit_hook=lambda: exited.append(True),
    )
    with TestClient(app) as test_client:
        yield test_client, stages, exited


def request(client: TestClient, text: str = "text", **overrides: object):
    input_root = Path(client.app.state.classification_input_root)
    source = input_root / "request.txt"
    source.write_text(text, encoding="utf-8")
    body = {
        "schemaVersion": "1",
        "requestId": "req-1",
        "inputUri": source.as_uri(),
        **overrides,
    }
    return client.post(
        "/internal/v1/classify",
        headers={"Authorization": "Bearer token", "X-Request-ID": "req-1"},
        json=body,
    )


def test_success_contract_and_health(
    client: tuple[TestClient, list[str], list[bool]],
) -> None:
    http, stages, _ = client
    assert http.get("/health/live").status_code == 200
    assert http.get("/health/ready").json() == {"status": "ready"}
    response = request(http)
    assert response.status_code == 200
    assert response.json() == {
        "schemaVersion": "1",
        "requestId": "req-1",
        "tags": ["应急", "安全生产", "危化品", "法规标准类"],
        "confidence": {"topTriple": 0.72, "endDoc": 0.81},
        "releaseId": "release-1",
    }
    assert stages == [
        "loading_tokenizer",
        "loading_top_triple_classifier",
        "loading_end_doc_classifier",
        "smoke_testing",
    ]


@pytest.mark.parametrize(
    "headers", [{}, {"Authorization": "Bearer wrong", "X-Request-ID": "req-1"}]
)
def test_authentication_is_required(
    client: tuple[TestClient, list[str], list[bool]], headers: dict[str, str]
) -> None:
    response = client[0].post(
        "/internal/v1/classify",
        headers=headers,
        json={"schemaVersion": "1", "requestId": "req-1", "inputUri": "file:///forbidden.txt"},
    )
    assert response.status_code == 401


@pytest.mark.parametrize(
    ("payload", "status"),
    [
        ({"schemaVersion": "2", "requestId": "req-1", "inputUri": "file:///x"}, 400),
        ({"schemaVersion": "1", "requestId": "req-1", "inputUri": "file:///x", "extra": True}, 400),
        ({"schemaVersion": "1", "requestId": "req-1", "inputUri": ""}, 400),
    ],
)
def test_request_validation(
    client: tuple[TestClient, list[str], list[bool]],
    payload: dict[str, object],
    status: int,
) -> None:
    response = client[0].post(
        "/internal/v1/classify",
        headers={"Authorization": "Bearer token", "X-Request-ID": "req-1"},
        json=payload,
    )
    assert response.status_code == status


def test_size_and_request_id_limits(
    client: tuple[TestClient, list[str], list[bool]],
) -> None:
    assert request(client[0], "x" * 500_001).status_code == 413
    response = client[0].post(
        "/internal/v1/classify",
        headers={"Authorization": "Bearer token", "X-Request-ID": "other"},
        json={"schemaVersion": "1", "requestId": "req-1", "inputUri": "file:///x"},
    )
    assert response.status_code == 400


@pytest.mark.parametrize(
    ("text", "status"),
    [("capacity", 429), ("timeout", 504), ("secret C:\\models\\release", 500)],
)
def test_stable_error_mapping_has_no_sensitive_detail(
    client: tuple[TestClient, list[str], list[bool]], text: str, status: int
) -> None:
    response = request(client[0], text)
    assert response.status_code == status
    serialized = response.text
    if "secret" in text:
        assert text not in serialized
        assert "C:\\models" not in serialized


def test_cuda_oom_marks_unready_and_triggers_exit(
    client: tuple[TestClient, list[str], list[bool]],
) -> None:
    http, _, exited = client
    assert request(http, "torch-oom").status_code == 503
    assert http.get("/health/ready").status_code == 503
    assert exited == [True]
