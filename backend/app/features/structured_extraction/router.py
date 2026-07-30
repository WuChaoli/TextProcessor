from dataclasses import dataclass

from app.features.structured_extraction.errors import (
    ExtractionErrorCode,
    ExtractionProcessingError,
)
from app.features.structured_extraction.format_detector import DetectedDocument
from app.features.structured_extraction.office_inspector import OfficeInspection
from app.features.structured_extraction.worker_models import (
    DetectedFormat,
    ProcessorName,
)

_MINERU_FORMATS = {
    DetectedFormat.PDF,
    DetectedFormat.IMAGE,
    DetectedFormat.PPT,
    DetectedFormat.PPTX,
    DetectedFormat.DOC,
}
_DOCLING_FORMATS = {
    DetectedFormat.XLS,
    DetectedFormat.XLSX,
    DetectedFormat.HTML,
    DetectedFormat.EPUB,
}
_PLAIN_TEXT_FORMATS = {
    DetectedFormat.JSON,
    DetectedFormat.XML,
    DetectedFormat.YAML,
    DetectedFormat.CSV,
    DetectedFormat.TSV,
    DetectedFormat.MARKDOWN,
    DetectedFormat.TEXT,
    DetectedFormat.UNKNOWN_TEXT,
}
_EXPLICITLY_UNSUPPORTED = {".wps", ".et", ".dps", ".ofd"}


@dataclass(frozen=True)
class RoutingDecision:
    processor: ProcessorName
    detected_format: str
    reasons: tuple[str, ...]


class ProcessorRouter:
    def __init__(
        self,
        *,
        production_formats: tuple[str, ...],
        docx_visual_complexity_threshold: int,
    ) -> None:
        if docx_visual_complexity_threshold < 0:
            raise ValueError("DOCX 视觉复杂度阈值不能为负数")
        self._production_formats = frozenset(production_formats)
        self._docx_visual_complexity_threshold = docx_visual_complexity_threshold

    def route(
        self,
        document: DetectedDocument,
        inspection: OfficeInspection | None = None,
    ) -> RoutingDecision:
        if (
            document.extension in _EXPLICITLY_UNSUPPORTED
            or document.detected_format.value not in self._production_formats
        ):
            raise unsupported_route()
        detected_format = document.detected_format
        processor: ProcessorName
        reasons: tuple[str, ...]
        if detected_format in _MINERU_FORMATS:
            processor = ProcessorName.MINERU
            reasons = (f"fixed_route={detected_format.value}",)
        elif detected_format in _DOCLING_FORMATS:
            processor = ProcessorName.DOCLING
            reasons = (f"fixed_route={detected_format.value}",)
        elif detected_format in _PLAIN_TEXT_FORMATS:
            processor = ProcessorName.PLAIN_TEXT
            reasons = (f"fixed_route={detected_format.value}",)
        elif detected_format is DetectedFormat.DOCX:
            if inspection is None:
                raise unsupported_route()
            if (
                inspection.visual_complexity_score
                >= self._docx_visual_complexity_threshold
            ):
                processor = ProcessorName.MINERU
                reasons = inspection.reasons or (
                    f"visual_complexity_score={inspection.visual_complexity_score}",
                )
            else:
                processor = ProcessorName.DOCLING
                reasons = ("ordinary_docx",)
        else:
            raise unsupported_route()
        return RoutingDecision(
            processor=processor,
            detected_format=detected_format.value,
            reasons=reasons,
        )


def unsupported_route() -> ExtractionProcessingError:
    return ExtractionProcessingError(
        ExtractionErrorCode.UNSUPPORTED_INPUT_FORMAT,
        "输入格式未进入生产处理 allowlist",
    )
