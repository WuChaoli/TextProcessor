import pytest

from app.features.structured_extraction.errors import (
    ExtractionErrorCode,
    ExtractionProcessingError,
)
from app.features.structured_extraction.processors.markdown_normalizer import (
    MarkdownNormalizer,
)


def test_removes_images_but_preserves_alt_and_document_structure() -> None:
    markdown = """\
# 标题

![系统架构](images/a(1).png "title")
![][ref]
[ref]: images/a.png

<img src="x.png" alt="图示">

[普通链接](https://example.com)

| 列1 | 列2 |
| --- | --- |
| A | B |

公式 $x^2$。

```python
print("keep")
```
"""

    normalized = MarkdownNormalizer().normalize(markdown)

    assert "系统架构" in normalized
    assert "图示" in normalized
    assert "![系统架构]" not in normalized
    assert "![][ref]" not in normalized
    assert "[ref]:" not in normalized
    assert "<img" not in normalized.lower()
    assert "[普通链接](https://example.com)" in normalized
    assert "| 列1 | 列2 |" in normalized
    assert "$x^2$" in normalized
    assert 'print("keep")' in normalized


def test_removes_data_image_without_rejecting_code_literal() -> None:
    markdown = """\
![inline](data:image/png;base64,AAAA)

```text
data:image/png;base64,example-in-code
```
"""

    normalized = MarkdownNormalizer().normalize(markdown)

    assert "inline" in normalized
    assert "example-in-code" in normalized


def test_rejects_data_image_used_as_normal_link() -> None:
    with pytest.raises(ExtractionProcessingError) as captured:
        MarkdownNormalizer().normalize("[not-an-image](data:image/png;base64,AAAA)")

    assert captured.value.code is ExtractionErrorCode.INVALID_PROCESSOR_OUTPUT
