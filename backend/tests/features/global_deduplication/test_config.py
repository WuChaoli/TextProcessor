from pathlib import Path

import pytest
from pydantic import ValidationError

from app.core.config import GlobalDeduplicationWorkerSettings


def test_global_dedup_defaults_are_bounded(tmp_path: Path) -> None:
    value = GlobalDeduplicationWorkerSettings(staging_root=tmp_path)

    assert value.datajuicer_profile == "text_exact_minhash_v1"
    assert value.max_documents > 0
    assert value.max_manifest_bytes > 0
    assert value.max_document_bytes > 0
    assert value.max_total_bytes >= value.max_document_bytes
    assert value.staging_root == tmp_path.resolve(strict=False)


def test_global_dedup_rejects_unknown_profile(tmp_path: Path) -> None:
    with pytest.raises(ValidationError):
        GlobalDeduplicationWorkerSettings(
            staging_root=tmp_path,
            datajuicer_profile="moving-profile",
        )


def test_global_dedup_rejects_total_smaller_than_document_limit(
    tmp_path: Path,
) -> None:
    with pytest.raises(ValidationError, match="批次累计限制"):
        GlobalDeduplicationWorkerSettings(
            staging_root=tmp_path,
            max_document_bytes=10,
            max_total_bytes=9,
        )
