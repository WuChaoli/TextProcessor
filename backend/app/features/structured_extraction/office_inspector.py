from dataclasses import dataclass
from pathlib import Path
from zipfile import BadZipFile, ZipFile

from app.features.structured_extraction.errors import ExtractionProcessingError
from app.features.structured_extraction.safe_archive import (
    invalid_archive,
    read_safe_xml,
    validated_archive_entries,
)


@dataclass(frozen=True)
class OfficeInspection:
    text_character_count: int = 0
    image_count: int = 0
    drawing_count: int = 0
    anchored_object_count: int = 0
    text_box_count: int = 0
    column_section_count: int = 0
    chart_count: int = 0
    embedded_object_count: int = 0
    scanned_page_likelihood: float = 0.0
    visual_complexity_score: int = 0
    reasons: tuple[str, ...] = ()


class OfficeDocumentInspector:
    def inspect_docx(self, path: Path) -> OfficeInspection:
        try:
            with ZipFile(path) as package:
                entries = validated_archive_entries(package)
                root = read_safe_xml(package, entries, "word/document.xml")
                tags = [self._local_name(element.tag) for element in root.iter()]
                text_character_count = 0
                for element in root.iter():
                    if self._local_name(element.tag) == "t":
                        text_character_count += len(element.text or "")
                image_count = sum(
                    name.startswith("word/media/") and not name.endswith("/")
                    for name in entries
                )
                drawing_count = tags.count("drawing") + tags.count("pict")
                anchored_object_count = tags.count("anchor")
                text_box_count = tags.count("txbxContent")
                column_section_count = tags.count("cols")
                chart_count = sum(
                    name.startswith("word/charts/") and name.endswith(".xml")
                    for name in entries
                )
                embedded_object_count = sum(
                    name.startswith("word/embeddings/") and not name.endswith("/")
                    for name in entries
                )
        except ExtractionProcessingError:
            raise
        except BadZipFile, OSError:
            raise invalid_archive() from None

        image_dominant = image_count > 0 and text_character_count < image_count * 200
        scanned_page_likelihood = (
            min(1.0, image_count * 200 / max(text_character_count, 1))
            if image_count
            else 0.0
        )
        reasons: list[str] = []
        if image_dominant:
            reasons.append("image_dominant_document")
        self._append_reason(reasons, "drawings", drawing_count)
        self._append_reason(reasons, "anchored_objects", anchored_object_count)
        self._append_reason(reasons, "text_boxes", text_box_count)
        self._append_reason(reasons, "column_sections", column_section_count)
        self._append_reason(reasons, "charts", chart_count)
        self._append_reason(reasons, "embedded_objects", embedded_object_count)
        score = (
            (2 if image_dominant else 0)
            + drawing_count
            + anchored_object_count * 2
            + text_box_count * 2
            + column_section_count
            + chart_count * 2
            + embedded_object_count * 2
        )
        return OfficeInspection(
            text_character_count=text_character_count,
            image_count=image_count,
            drawing_count=drawing_count,
            anchored_object_count=anchored_object_count,
            text_box_count=text_box_count,
            column_section_count=column_section_count,
            chart_count=chart_count,
            embedded_object_count=embedded_object_count,
            scanned_page_likelihood=scanned_page_likelihood,
            visual_complexity_score=score,
            reasons=tuple(reasons),
        )

    @staticmethod
    def _local_name(tag: object) -> str:
        value = str(tag)
        return value.rsplit("}", maxsplit=1)[-1]

    @staticmethod
    def _append_reason(reasons: list[str], name: str, count: int) -> None:
        if count:
            reasons.append(f"{name}={count}")
