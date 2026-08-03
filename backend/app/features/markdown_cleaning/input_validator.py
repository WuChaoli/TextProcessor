from __future__ import annotations

import codecs
import hashlib
import os
import re
import tempfile
import unicodedata
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from app.features.markdown_cleaning.staging import StagingLayout

_UTF8_BOM = codecs.BOM_UTF8
_ALLOWED_SUFFIXES = frozenset({".md", ".markdown"})
_FENCE_OPEN = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")


class MarkdownInputErrorCode(StrEnum):
    INPUT_NOT_FOUND = "INPUT_NOT_FOUND"
    INPUT_ACCESS_FAILED = "INPUT_ACCESS_FAILED"
    INPUT_TOO_LARGE = "INPUT_TOO_LARGE"
    UNSUPPORTED_INPUT_FORMAT = "UNSUPPORTED_INPUT_FORMAT"
    EMPTY_INPUT = "EMPTY_INPUT"
    INVALID_UTF8 = "INVALID_UTF8"
    INVALID_CONTROL_CHARACTER = "INVALID_CONTROL_CHARACTER"
    INVALID_MARKDOWN = "INVALID_MARKDOWN"
    INPUT_DIGEST_MISMATCH = "INPUT_DIGEST_MISMATCH"


class MarkdownInputError(RuntimeError):
    def __init__(self, code: MarkdownInputErrorCode, safe_message: str) -> None:
        super().__init__(f"{code}: {safe_message}")
        self.code = code
        self.safe_message = safe_message


class ResolvedInput(Protocol):
    @property
    def path(self) -> Path: ...

    @property
    def size_bytes(self) -> int: ...

    @property
    def sha256(self) -> str: ...

    @property
    def source_suffix(self) -> str: ...


@dataclass(frozen=True, slots=True)
class ValidatedMarkdownInput:
    original_path: Path
    original_size_bytes: int
    original_sha256: str
    processor_path: Path
    processor_size_bytes: int
    processor_sha256: str


class MarkdownInputValidator:
    def __init__(
        self, *, max_input_bytes: int, read_chunk_bytes: int = 64 * 1024
    ) -> None:
        if max_input_bytes <= 0 or read_chunk_bytes <= 0:
            raise ValueError("输入限制必须是正整数")
        self._max_input_bytes = max_input_bytes
        self._read_chunk_bytes = max(4, read_chunk_bytes)

    def validate(
        self,
        resolved: ResolvedInput,
        layout: StagingLayout,
        *,
        expected_processor_sha256: str | None = None,
        expected_processor_size_bytes: int | None = None,
    ) -> ValidatedMarkdownInput:
        if resolved.source_suffix.lower() not in _ALLOWED_SUFFIXES:
            raise self._error(
                MarkdownInputErrorCode.UNSUPPORTED_INPUT_FORMAT,
                "输入文件格式不受支持",
            )
        try:
            original_path = layout.assert_safe_path(resolved.path, must_exist=True)
            expected_original = layout.assert_safe_path(
                layout.original_source, must_exist=True
            )
        except ValueError:
            raise self._error(
                MarkdownInputErrorCode.INPUT_ACCESS_FAILED,
                "输入文件访问失败",
            ) from None
        if original_path != expected_original or not original_path.is_file():
            raise self._error(
                MarkdownInputErrorCode.INPUT_ACCESS_FAILED,
                "输入文件访问失败",
            )

        layout.prepare()
        part_path = layout.input_dir / ".source.md.part"
        self._remove_part(part_path)
        original_digest = hashlib.sha256()
        processor_digest = hashlib.sha256()
        original_size = 0
        processor_size = 0
        decoder = codecs.getincrementaldecoder("utf-8")(errors="strict")
        text_parts: list[str] = []
        first_chunk = True
        file_descriptor: int | None = None
        handle = None
        try:
            file_descriptor, temporary_name = tempfile.mkstemp(
                prefix=".source.", suffix=".part", dir=layout.input_dir
            )
            generated_part = Path(temporary_name)
            handle = os.fdopen(file_descriptor, "wb")
            file_descriptor = None
            with original_path.open("rb") as source, handle:
                while chunk := source.read(self._read_chunk_bytes):
                    original_size += len(chunk)
                    if original_size > self._max_input_bytes:
                        raise self._error(
                            MarkdownInputErrorCode.INPUT_TOO_LARGE,
                            "输入文件超过大小限制",
                        )
                    original_digest.update(chunk)
                    normalized = (
                        chunk[len(_UTF8_BOM) :]
                        if first_chunk and chunk.startswith(_UTF8_BOM)
                        else chunk
                    )
                    first_chunk = False
                    processor_size += len(normalized)
                    processor_digest.update(normalized)
                    handle.write(normalized)
                    try:
                        decoded = decoder.decode(normalized, final=False)
                    except UnicodeDecodeError:
                        raise self._error(
                            MarkdownInputErrorCode.INVALID_UTF8,
                            "输入文件不是有效 UTF-8",
                        ) from None
                    self._validate_characters(decoded)
                    text_parts.append(decoded)
                try:
                    tail = decoder.decode(b"", final=True)
                except UnicodeDecodeError:
                    raise self._error(
                        MarkdownInputErrorCode.INVALID_UTF8,
                        "输入文件不是有效 UTF-8",
                    ) from None
                self._validate_characters(tail)
                text_parts.append(tail)
                if original_size == 0 or processor_size == 0:
                    raise self._error(
                        MarkdownInputErrorCode.EMPTY_INPUT,
                        "输入文件不能为空",
                    )
                actual_original_sha256 = original_digest.hexdigest()
                if (
                    original_size != resolved.size_bytes
                    or actual_original_sha256 != resolved.sha256
                ):
                    raise self._error(
                        MarkdownInputErrorCode.INPUT_DIGEST_MISMATCH,
                        "输入文件摘要不匹配",
                    )
                self._validate_fences("".join(text_parts))
                handle.flush()
                os.fsync(handle.fileno())

            processor_sha256 = processor_digest.hexdigest()
            if self._can_reuse_processor_source(
                layout,
                expected_sha256=expected_processor_sha256,
                expected_size=expected_processor_size_bytes,
                actual_sha256=processor_sha256,
                actual_size=processor_size,
            ):
                generated_part.unlink(missing_ok=True)
            else:
                processor_path = layout.assert_safe_path(
                    layout.processor_source, must_exist=False
                )
                os.replace(generated_part, processor_path)
            return ValidatedMarkdownInput(
                original_path=original_path,
                original_size_bytes=original_size,
                original_sha256=actual_original_sha256,
                processor_path=layout.processor_source,
                processor_size_bytes=processor_size,
                processor_sha256=processor_sha256,
            )
        except MarkdownInputError:
            self._remove_generated_parts(layout)
            layout.processor_source.unlink(missing_ok=True)
            raise
        except OSError:
            self._remove_generated_parts(layout)
            layout.processor_source.unlink(missing_ok=True)
            raise self._error(
                MarkdownInputErrorCode.INPUT_ACCESS_FAILED,
                "输入文件访问失败",
            ) from None
        finally:
            if file_descriptor is not None:
                os.close(file_descriptor)
            if handle is not None and not handle.closed:
                handle.close()

    @staticmethod
    def _validate_characters(text: str) -> None:
        for character in text:
            if character == "\ufeff":
                raise MarkdownInputValidator._error(
                    MarkdownInputErrorCode.INVALID_UTF8,
                    "输入文件包含无效字节序标记",
                )
            if (
                character not in {"\t", "\n", "\r"}
                and unicodedata.category(character) == "Cc"
            ):
                raise MarkdownInputValidator._error(
                    MarkdownInputErrorCode.INVALID_CONTROL_CHARACTER,
                    "输入文件包含禁止的控制字符",
                )

    @staticmethod
    def _validate_fences(markdown: str) -> None:
        open_character: str | None = None
        open_length = 0
        for line in markdown.splitlines():
            if open_character is None:
                match = _FENCE_OPEN.match(line)
                if match is None:
                    continue
                marker, info = match.groups()
                if marker[0] == "`" and "`" in info:
                    continue
                open_character = marker[0]
                open_length = len(marker)
                continue
            if re.fullmatch(
                rf" {{0,3}}{re.escape(open_character)}{{{open_length},}}[ \t]*",
                line,
            ):
                open_character = None
                open_length = 0
        if open_character is not None:
            raise MarkdownInputValidator._error(
                MarkdownInputErrorCode.INVALID_MARKDOWN,
                "Markdown 围栏未闭合",
            )

    @staticmethod
    def _can_reuse_processor_source(
        layout: StagingLayout,
        *,
        expected_sha256: str | None,
        expected_size: int | None,
        actual_sha256: str,
        actual_size: int,
    ) -> bool:
        if (
            expected_sha256 is None
            or expected_size is None
            or expected_sha256 != actual_sha256
            or expected_size != actual_size
            or not layout.processor_source.is_file()
            or layout.processor_source.is_symlink()
        ):
            return False
        try:
            if layout.processor_source.samefile(layout.original_source):
                return False
            digest = hashlib.sha256()
            size = 0
            with layout.processor_source.open("rb") as source:
                while chunk := source.read(64 * 1024):
                    size += len(chunk)
                    digest.update(chunk)
            return size == expected_size and digest.hexdigest() == expected_sha256
        except OSError:
            return False

    @staticmethod
    def _remove_part(path: Path) -> None:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass

    @classmethod
    def _remove_generated_parts(cls, layout: StagingLayout) -> None:
        try:
            for path in layout.input_dir.glob(".source.*.part"):
                cls._remove_part(path)
        except OSError:
            pass

    @staticmethod
    def _error(
        code: MarkdownInputErrorCode,
        message: str,
    ) -> MarkdownInputError:
        return MarkdownInputError(code, message)
