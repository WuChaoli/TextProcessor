import zipfile
from pathlib import Path

from app.features.structured_extraction.office_inspector import (
    OfficeDocumentInspector,
)

FIXTURE_ROOT = Path(__file__).parents[2] / "fixtures" / "structured_extraction"


def test_docx_inspector_counts_visual_structure_and_reasons(tmp_path: Path) -> None:
    path = tmp_path / "complex.docx"
    document_xml = """\
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"
 xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing">
  <w:body>
    <w:p><w:r><w:t>short text</w:t><w:drawing><wp:anchor/></w:drawing></w:r></w:p>
    <w:txbxContent><w:p><w:r><w:t>box</w:t></w:r></w:p></w:txbxContent>
    <w:sectPr><w:cols w:num="2"/></w:sectPr>
  </w:body>
</w:document>
"""
    with zipfile.ZipFile(path, "w") as package:
        package.writestr("[Content_Types].xml", "<Types/>")
        package.writestr("word/document.xml", document_xml)
        package.writestr("word/media/image1.png", b"image")
        package.writestr("word/charts/chart1.xml", "<chart/>")
        package.writestr("word/embeddings/object1.bin", b"object")

    inspection = OfficeDocumentInspector().inspect_docx(path)

    assert inspection.text_character_count == 13
    assert inspection.image_count == 1
    assert inspection.drawing_count == 1
    assert inspection.anchored_object_count == 1
    assert inspection.text_box_count == 1
    assert inspection.column_section_count == 1
    assert inspection.chart_count == 1
    assert inspection.embedded_object_count == 1
    assert inspection.scanned_page_likelihood > 0
    assert inspection.visual_complexity_score >= 6
    assert "anchored_objects=1" in inspection.reasons
    assert "text_boxes=1" in inspection.reasons
    assert "column_sections=1" in inspection.reasons


def test_plain_docx_has_zero_visual_complexity(tmp_path: Path) -> None:
    path = tmp_path / "plain.docx"
    document_xml = """\
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:body><w:p><w:r><w:t>ordinary paragraph</w:t></w:r></w:p></w:body>
</w:document>
"""
    with zipfile.ZipFile(path, "w") as package:
        package.writestr("[Content_Types].xml", "<Types/>")
        package.writestr("word/document.xml", document_xml)

    inspection = OfficeDocumentInspector().inspect_docx(path)

    assert inspection.visual_complexity_score == 0
    assert inspection.reasons == ()


def test_synthetic_complex_docx_crosses_production_routing_threshold() -> None:
    inspection = OfficeDocumentInspector().inspect_docx(
        FIXTURE_ROOT / "synthetic-complex.docx"
    )

    assert inspection.visual_complexity_score >= 5
    assert inspection.reasons == (
        "drawings=1",
        "anchored_objects=1",
        "text_boxes=1",
        "column_sections=1",
    )
