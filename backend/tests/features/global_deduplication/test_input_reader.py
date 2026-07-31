from pathlib import Path

import httpx
import pytest

from app.features.global_deduplication.errors import (
    GlobalDeduplicationErrorCode,
    GlobalDeduplicationProcessingError,
)
from app.features.global_deduplication.input_reader import (
    BoundedLocalReader,
    BoundedUriReader,
    load_documents,
    load_manifest_bytes,
    normalize_document,
)
from app.features.global_deduplication.models import DocumentReference


def assert_error_code(
    error: pytest.ExceptionInfo[GlobalDeduplicationProcessingError],
    code: GlobalDeduplicationErrorCode,
) -> None:
    assert error.value.code is code


def test_manifest_ignores_unknown_fields() -> None:
    documents = load_manifest_bytes(
        b'[{"fileId":"1","fileStoragePath":"a.md","ignored":true}]',
        max_documents=2,
    )

    assert len(documents) == 1
    assert documents[0].file_id == "1"
    assert documents[0].file_storage_path == "a.md"


def test_manifest_rejects_duplicate_file_id() -> None:
    with pytest.raises(GlobalDeduplicationProcessingError) as error:
        load_manifest_bytes(
            b'[{"fileId":"1","fileStoragePath":"a.md"},'
            b'{"fileId":"1","fileStoragePath":"b.txt"}]',
            max_documents=2,
        )

    assert_error_code(error, GlobalDeduplicationErrorCode.DUPLICATE_FILE_ID)


@pytest.mark.parametrize(
    ("content", "code"),
    [
        (b"", GlobalDeduplicationErrorCode.INVALID_INPUT_MANIFEST),
        (b"{}", GlobalDeduplicationErrorCode.INVALID_INPUT_MANIFEST),
        (b"[]", GlobalDeduplicationErrorCode.EMPTY_DOCUMENT_LIST),
        (b"\xff", GlobalDeduplicationErrorCode.INVALID_INPUT_MANIFEST),
        (
            b'[{"fileId":"","fileStoragePath":"a.md"}]',
            GlobalDeduplicationErrorCode.INVALID_INPUT_MANIFEST,
        ),
        (
            b'[{"fileId":"1","fileStoragePath":" "}]',
            GlobalDeduplicationErrorCode.INVALID_INPUT_MANIFEST,
        ),
    ],
)
def test_manifest_rejects_invalid_content(
    content: bytes,
    code: GlobalDeduplicationErrorCode,
) -> None:
    with pytest.raises(GlobalDeduplicationProcessingError) as error:
        load_manifest_bytes(content, max_documents=10)

    assert_error_code(error, code)


def test_manifest_enforces_document_count() -> None:
    with pytest.raises(GlobalDeduplicationProcessingError) as error:
        load_manifest_bytes(
            b'[{"fileId":"1","fileStoragePath":"a.md"},'
            b'{"fileId":"2","fileStoragePath":"b.txt"}]',
            max_documents=1,
        )

    assert_error_code(error, GlobalDeduplicationErrorCode.INVALID_INPUT_MANIFEST)


@pytest.mark.parametrize("suffix", [".md", ".txt", ".json", ".MD"])
def test_document_normalization_supports_text_formats(suffix: str) -> None:
    raw = b'\xef\xbb\xbf{"b": 2,\r\n"a": 1}\r'

    assert normalize_document(raw, suffix=suffix) == '{"b": 2,\n"a": 1}\n'


def test_document_normalization_rejects_format_and_encoding() -> None:
    with pytest.raises(GlobalDeduplicationProcessingError) as format_error:
        normalize_document(b"text", suffix=".doc")
    assert_error_code(
        format_error,
        GlobalDeduplicationErrorCode.UNSUPPORTED_DOCUMENT_FORMAT,
    )

    with pytest.raises(GlobalDeduplicationProcessingError) as encoding_error:
        normalize_document(b"\xff", suffix=".txt")
    assert_error_code(
        encoding_error,
        GlobalDeduplicationErrorCode.DOCUMENT_DECODE_FAILED,
    )


def test_local_reader_enforces_root_and_byte_limit(tmp_path: Path) -> None:
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    document = allowed / "a.txt"
    document.write_bytes(b"1234")
    reader = BoundedLocalReader(input_roots=(allowed,), chunk_bytes=2)

    assert reader.read(document, max_bytes=4) == b"1234"

    with pytest.raises(GlobalDeduplicationProcessingError) as size_error:
        reader.read(document, max_bytes=3)
    assert_error_code(size_error, GlobalDeduplicationErrorCode.DOCUMENT_TOO_LARGE)

    outside = tmp_path / "outside.txt"
    outside.write_text("x", encoding="utf-8")
    with pytest.raises(GlobalDeduplicationProcessingError) as path_error:
        reader.read(outside, max_bytes=10)
    assert_error_code(
        path_error,
        GlobalDeduplicationErrorCode.DOCUMENT_PATH_NOT_ALLOWED,
    )


def test_document_loading_enforces_batch_limit(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "a.md").write_text("123", encoding="utf-8")
    (source / "b.json").write_text('{"x":1}', encoding="utf-8")
    references = (
        DocumentReference(file_id="1", file_storage_path=str(source / "a.md")),
        DocumentReference(file_id="2", file_storage_path=str(source / "b.json")),
    )

    with pytest.raises(GlobalDeduplicationProcessingError) as error:
        load_documents(
            references,
            reader=BoundedLocalReader(input_roots=(source,), chunk_bytes=2),
            max_document_bytes=10,
            max_total_bytes=9,
        )

    assert_error_code(error, GlobalDeduplicationErrorCode.BATCH_TOO_LARGE)


def test_document_loading_preserves_order_and_normalized_text(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "a.md").write_bytes(b"\xef\xbb\xbfline\r\n")
    references = (
        DocumentReference(file_id="1", file_storage_path=str(source / "a.md")),
    )

    documents = load_documents(
        references,
        reader=BoundedLocalReader(input_roots=(source,), chunk_bytes=2),
        max_document_bytes=100,
        max_total_bytes=100,
    )

    assert documents[0].reference == references[0]
    assert documents[0].text == "line\n"
    assert documents[0].size_bytes == 9


def test_uri_reader_supports_controlled_file_uri(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    document = source / "a.txt"
    document.write_text("content", encoding="utf-8")
    reader = BoundedUriReader(
        input_roots=(source,),
        chunk_bytes=2,
    )

    assert reader.read_document(document.as_uri(), max_bytes=10) == b"content"


def test_uri_reader_validates_http_and_bounds_response(tmp_path: Path) -> None:
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        return httpx.Response(200, content=b"1234")

    reader = BoundedUriReader(
        input_roots=(tmp_path,),
        chunk_bytes=2,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        remote_url_validator=lambda value: value,
    )

    assert reader.read_document("https://internal.example/a.md", max_bytes=4) == b"1234"
    assert requested == ["https://internal.example/a.md"]
    with pytest.raises(GlobalDeduplicationProcessingError) as error:
        reader.read_document("https://internal.example/a.md", max_bytes=3)
    assert_error_code(error, GlobalDeduplicationErrorCode.DOCUMENT_TOO_LARGE)
