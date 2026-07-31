from pathlib import Path

import pytest

from app.features.structured_extraction.errors import (
    ExtractionErrorCode,
    ExtractionProcessingError,
)
from app.features.structured_extraction.format_detector import DetectedDocument
from app.features.structured_extraction.office_inspector import OfficeInspection
from app.features.structured_extraction.router import ProcessorRouter
from app.features.structured_extraction.worker_models import (
    DetectedFormat,
    ProcessorName,
)


@pytest.mark.parametrize(
    ("detected_format", "processor"),
    [
        (DetectedFormat.PDF, ProcessorName.MINERU),
        (DetectedFormat.IMAGE, ProcessorName.MINERU),
        (DetectedFormat.PPTX, ProcessorName.MINERU),
        (DetectedFormat.XLS, ProcessorName.DOCLING),
        (DetectedFormat.XLSX, ProcessorName.DOCLING),
        (DetectedFormat.HTML, ProcessorName.DOCLING),
        (DetectedFormat.EPUB, ProcessorName.DOCLING),
        (DetectedFormat.JSON, ProcessorName.PLAIN_TEXT),
        (DetectedFormat.UNKNOWN_TEXT, ProcessorName.PLAIN_TEXT),
    ],
)
def test_routes_fixed_format_matrix(
    detected_format: DetectedFormat,
    processor: ProcessorName,
) -> None:
    router = ProcessorRouter(
        production_formats=(detected_format.value,),
        docx_visual_complexity_threshold=5,
    )
    document = DetectedDocument(
        path=Path(f"sample.{detected_format.value}"),
        detected_format=detected_format,
        extension=f".{detected_format.value}",
    )

    decision = router.route(document)

    assert decision.processor is processor
    assert decision.detected_format == detected_format.value


def test_routes_plain_docx_to_docling() -> None:
    router = ProcessorRouter(
        production_formats=("docx",),
        docx_visual_complexity_threshold=5,
    )
    document = DetectedDocument(
        path=Path("sample.docx"),
        detected_format=DetectedFormat.DOCX,
        extension=".docx",
    )

    decision = router.route(
        document,
        OfficeInspection(text_character_count=100),
    )

    assert decision.processor is ProcessorName.DOCLING
    assert decision.reasons == ("ordinary_docx",)


def test_routes_complex_docx_to_mineru_with_stable_reasons() -> None:
    router = ProcessorRouter(
        production_formats=("docx",),
        docx_visual_complexity_threshold=5,
    )
    document = DetectedDocument(
        path=Path("sample.docx"),
        detected_format=DetectedFormat.DOCX,
        extension=".docx",
    )
    inspection = OfficeInspection(
        text_character_count=20,
        image_count=3,
        visual_complexity_score=7,
        reasons=("image_dominant_document", "anchored_objects=2"),
    )

    decision = router.route(document, inspection)

    assert decision.processor is ProcessorName.MINERU
    assert decision.reasons == (
        "image_dominant_document",
        "anchored_objects=2",
    )


def test_route_rejects_format_not_in_production_allowlist() -> None:
    router = ProcessorRouter(
        production_formats=("text",),
        docx_visual_complexity_threshold=5,
    )
    document = DetectedDocument(
        path=Path("sample.pdf"),
        detected_format=DetectedFormat.PDF,
        extension=".pdf",
    )

    with pytest.raises(ExtractionProcessingError) as captured:
        router.route(document)

    assert captured.value.code is ExtractionErrorCode.UNSUPPORTED_INPUT_FORMAT


@pytest.mark.parametrize("detected_format", [DetectedFormat.DOC, DetectedFormat.PPT])
def test_route_rejects_legacy_office_even_if_configured_in_allowlist(
    detected_format: DetectedFormat,
) -> None:
    router = ProcessorRouter(
        production_formats=(detected_format.value,),
        docx_visual_complexity_threshold=5,
    )
    document = DetectedDocument(
        path=Path(f"sample.{detected_format.value}"),
        detected_format=detected_format,
        extension=f".{detected_format.value}",
    )

    with pytest.raises(ExtractionProcessingError) as captured:
        router.route(document)

    assert captured.value.code is ExtractionErrorCode.UNSUPPORTED_INPUT_FORMAT


@pytest.mark.parametrize("extension", [".wps", ".et", ".dps", ".ofd"])
def test_route_rejects_explicitly_unsupported_extensions(extension: str) -> None:
    router = ProcessorRouter(
        production_formats=("unknown_text",),
        docx_visual_complexity_threshold=5,
    )
    document = DetectedDocument(
        path=Path(f"sample{extension}"),
        detected_format=DetectedFormat.UNKNOWN_TEXT,
        extension=extension,
    )

    with pytest.raises(ExtractionProcessingError):
        router.route(document)
