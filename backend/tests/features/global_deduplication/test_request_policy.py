from pathlib import Path

import pytest

from app.features.global_deduplication.api_errors import GlobalDeduplicationDomainError
from app.features.global_deduplication.request_policy import (
    GlobalDeduplicationRequestPolicy,
)
from app.features.global_deduplication.schemas import GlobalDeduplicationTaskCreate


def request(path: str) -> GlobalDeduplicationTaskCreate:
    return GlobalDeduplicationTaskCreate(sessionId="session-1", inputPath=path)


def test_local_batch_path_is_normalized(tmp_path: Path) -> None:
    batch = tmp_path / "batch"
    (batch / "original").mkdir(parents=True)
    (batch / "duplicate").mkdir()

    validated = GlobalDeduplicationRequestPolicy().validate_request(request(str(batch)))

    assert validated.input_path == str(batch.resolve())


def test_batch_requires_expected_directories_and_rejects_remote_uri(
    tmp_path: Path,
) -> None:
    batch = tmp_path / "batch"
    batch.mkdir()
    policy = GlobalDeduplicationRequestPolicy()

    with pytest.raises(GlobalDeduplicationDomainError):
        policy.validate_request(request(str(batch)))
    with pytest.raises(GlobalDeduplicationDomainError):
        policy.validate_request(request("https://files.internal/batch"))
