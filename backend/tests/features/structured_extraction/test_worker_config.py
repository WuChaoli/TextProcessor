import uuid
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.core.config import DoclingProfile, ExtractionWorkerSettings, MinerUProfile
from app.features.structured_extraction.errors import (
    ExtractionErrorCode,
    ExtractionProcessingError,
)
from app.features.structured_extraction.schemas import ExtractionResultPublic
from app.features.structured_extraction.worker_models import (
    DetectedFormat,
    ExternalTaskState,
    ExtractionProcessingPhase,
    ProcessingContext,
    ProcessorName,
)


def test_mineru_profile_requires_markdown_without_images() -> None:
    with pytest.raises(ValidationError):
        MinerUProfile(
            return_md=False,
            return_images=True,
            response_format_zip=True,
        )


def test_docling_profile_only_produces_markdown_placeholders_without_ocr() -> None:
    with pytest.raises(ValidationError):
        DoclingProfile(
            to_formats=("html",),
            image_export_mode="embedded",
            do_ocr=True,
        )


def test_processor_roots_must_not_overlap(tmp_path: Path) -> None:
    with pytest.raises(ValidationError):
        ExtractionWorkerSettings(
            staging_root=tmp_path,
            output_roots=(tmp_path,),
        )


def test_worker_settings_normalize_roots_and_require_positive_limits(
    tmp_path: Path,
) -> None:
    staging_root = tmp_path / "staging"
    output_root = tmp_path / "output"

    configured = ExtractionWorkerSettings(
        staging_root=staging_root,
        output_roots=(output_root,),
        production_formats=("text", "json"),
    )

    assert configured.staging_root == staging_root.resolve()
    assert configured.output_roots == (output_root.resolve(),)
    with pytest.raises(ValidationError):
        ExtractionWorkerSettings(
            staging_root=staging_root,
            output_roots=(output_root,),
            copy_chunk_bytes=0,
        )


def test_docx_visual_complexity_threshold_cannot_be_negative(
    tmp_path: Path,
) -> None:
    with pytest.raises(ValidationError):
        ExtractionWorkerSettings(
            staging_root=tmp_path / "staging",
            output_roots=(tmp_path / "output",),
            docx_visual_complexity_threshold=-1,
        )


def test_worker_value_objects_preserve_routing_context() -> None:
    context = ProcessingContext(
        task_id=uuid.UUID("018f0000-0000-7000-8000-000000000001"),
        detected_format=DetectedFormat.DOCX,
        profile_name="office-default",
        profile_sha256="a" * 64,
    )

    assert context.detected_format is DetectedFormat.DOCX
    assert ProcessorName.MINERU.value == "mineru"
    assert ExtractionProcessingPhase.PUBLISHING.value == "publishing"
    assert ExternalTaskState.PROCESSING.value == "processing"


def test_processing_error_retains_only_retry_metadata() -> None:
    error = ExtractionProcessingError(
        ExtractionErrorCode.INPUT_ACCESS_FAILED,
        "输入读取失败",
        transient=True,
        external_task_id="external-1",
    )

    assert error.code is ExtractionErrorCode.INPUT_ACCESS_FAILED
    assert error.safe_message == "输入读取失败"
    assert error.transient is True
    assert error.external_task_id == "external-1"


def test_success_result_serializes_non_sensitive_processor_metadata() -> None:
    result = ExtractionResultPublic.model_validate(
        {
            "fileStoragePath": "/data/input/sample.docx",
            "fileOssUrl": None,
            "targetPath": "/data/output/sample.md",
            "processor": {
                "name": "docling",
                "version": "1.2.3",
                "profile": "office-default",
                "profileSha256": "b" * 64,
            },
            "routing": {
                "detectedFormat": "docx",
                "reasons": ["ordinary_docx"],
            },
            "inputSha256": "c" * 64,
            "outputSha256": "d" * 64,
        }
    )

    payload = result.model_dump(by_alias=True)
    assert payload["processor"]["name"] == "docling"
    assert payload["routing"] == {
        "detectedFormat": "docx",
        "reasons": ["ordinary_docx"],
    }
    assert "baseUrl" not in payload["processor"]
    assert "apiKey" not in payload["processor"]
