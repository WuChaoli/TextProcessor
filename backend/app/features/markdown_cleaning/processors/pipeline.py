"""Markdown cleaning processor pipeline implementation."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Final

from markdown_it import MarkdownIt

from app.features.markdown_cleaning.processors.errors import (
    MarkdownCleaningErrorCode,
    MarkdownCleaningProcessorError,
    map_processing_exception,
)
from app.features.markdown_cleaning.processors.markdown_formatter import (
    MarkdownFormatterAdapter,
    MarkdownFormatterResult,
)
from app.features.markdown_cleaning.processors.markdown_parser import (
    MarkdownParserAdapter,
    MarkdownParserError,
    MarkdownParserErrorCode,
    MarkdownParseResult,
)
from app.features.markdown_cleaning.processors.models import (
    MarkdownCleaningSummary,
    ProcessorResult,
    SourceSpan,
)
from app.features.markdown_cleaning.processors.paragraph_dedup import (
    ParagraphDeduplicator,
)
from app.features.markdown_cleaning.processors.presidio_adapter import (
    PresidioMarkdownRedactor,
    SensitiveRedactionResult,
    SensitiveRedactionSummary,
)

_BOM: Final = "\ufeff"

_EMAIL_CANDIDATE_PATTERN: Final = re.compile(
    r"(?<![\w.+-])[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,63}(?![\w.+-])"
)
_CN_MOBILE_CANDIDATE_PATTERN: Final = re.compile(r"(?<!\d)1[3-9](?:[ -]?\d){9}(?!\d)")
_ID_CARD_CANDIDATE_PATTERN: Final = re.compile(r"(?<!\d)(\d{17}[Xx\d])(?!\d)")
_CREDIT_CARD_CANDIDATE_PATTERN: Final = re.compile(r"(?<!\d)(\d{12,19})(?!\d)")
_IPV4_CANDIDATE_PATTERN: Final = re.compile(r"(?<!\d)(?:\d{1,3}\.){3}\d{1,3}(?!\d)")
_RUNTIME_ENV_ALLOWLIST: Final = (
    "COMSPEC",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "PATH",
    "PATHEXT",
    "SYSTEMDRIVE",
    "SYSTEMROOT",
    "TEMP",
    "TMP",
    "TMPDIR",
    "WINDIR",
)


@dataclass(frozen=True, slots=True)
class MarkdownCleaningPipelineLimits:
    max_input_bytes: int = 4 * 1024 * 1024
    max_output_bytes: int = 4 * 1024 * 1024
    max_block_count: int = 2000
    max_protected_span_count: int = 2000
    max_block_char_span: int = 1_000_000
    max_token_count: int = 10_000
    max_pii_candidate_count: int = 2000
    processing_timeout_seconds: float = 8.0

    def __post_init__(self) -> None:
        integer_limits = (
            "max_input_bytes",
            "max_output_bytes",
            "max_block_count",
            "max_protected_span_count",
            "max_block_char_span",
            "max_token_count",
            "max_pii_candidate_count",
        )
        for name in integer_limits:
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        if (
            not isinstance(self.processing_timeout_seconds, (int, float))
            or isinstance(self.processing_timeout_seconds, bool)
            or self.processing_timeout_seconds <= 0
            or not math.isfinite(self.processing_timeout_seconds)
        ):
            raise ValueError("processing_timeout_seconds must be positive")


@dataclass(frozen=True, slots=True)
class _ProcessingDeadline:
    started_at: float
    timeout_seconds: float
    time_fn: Callable[[], float]

    def check(self) -> None:
        if self.time_fn() - self.started_at > self.timeout_seconds:
            raise map_processing_exception(
                TimeoutError("processing deadline exceeded"),
                MarkdownCleaningErrorCode.PROCESSING_TIMEOUT,
            )

    def remaining_seconds(self) -> float:
        return max(0.0, self.timeout_seconds - (self.time_fn() - self.started_at))


@dataclass(frozen=True, slots=True)
class _PipelineTransformResult:
    text: str
    duplicate_count: int
    redaction_summary: SensitiveRedactionSummary
    formatting_changes: int


class MarkdownCleaningPipeline:
    """Run deterministic markdown cleaning stages in fixed order."""

    def __init__(
        self,
        *,
        staging_root: Path,
        parser: MarkdownParserAdapter | None = None,
        deduplicator: ParagraphDeduplicator | None = None,
        redactor: PresidioMarkdownRedactor | None = None,
        formatter: MarkdownFormatterAdapter | None = None,
        limits: MarkdownCleaningPipelineLimits | None = None,
        time_fn: Callable[[], float] = time.perf_counter,
        _runtime_command: tuple[str, ...] | None = None,
        _run_inline: bool = False,
    ) -> None:
        if self._is_link_or_junction(staging_root):
            raise ValueError("staging_root cannot be a link")
        try:
            resolved_staging_root = staging_root.resolve(strict=True)
        except OSError as exc:
            raise ValueError("staging_root must exist") from exc
        if not resolved_staging_root.is_dir():
            raise ValueError("staging_root must be a directory")
        has_custom_components = any(
            component is not None
            for component in (parser, deduplicator, redactor, formatter)
        )
        if (
            (has_custom_components or time_fn is not time.perf_counter)
            and not _run_inline
            and _runtime_command is None
        ):
            raise ValueError("custom stages and clocks require explicit inline execution")
        self._staging_root = resolved_staging_root
        self._parser = parser or MarkdownParserAdapter()
        self._deduplicator = deduplicator or ParagraphDeduplicator(parser=self._parser)
        self._redactor = redactor or PresidioMarkdownRedactor()
        self._formatter = formatter or MarkdownFormatterAdapter(parser=self._parser)
        self._limits = limits or MarkdownCleaningPipelineLimits()
        self._time_fn = time_fn
        self._token_parser = MarkdownIt("commonmark").enable("table")
        if _run_inline:
            self._runtime_command = None
        else:
            self._runtime_command = _runtime_command or (
                sys.executable,
                "-m",
                "app.features.markdown_cleaning.processors.pipeline_runtime",
            )

    def process(self, source_path: Path, destination_path: Path) -> ProcessorResult:
        try:
            source_path, destination_path = self._validate_staging_paths(
                source_path,
                destination_path,
            )
            deadline = _ProcessingDeadline(
                started_at=self._time_fn(),
                timeout_seconds=self._limits.processing_timeout_seconds,
                time_fn=self._time_fn,
            )
            raw_input = self._read_source(source_path, deadline)

            source_text = self._decode_utf8_no_bom(raw_input)
            transformed = (
                self._run_transform_isolated(source_text, deadline)
                if self._runtime_command is not None
                else self._transform_text(source_text, deadline)
            )

            output_bytes = transformed.text.encode("utf-8")
            self._enforce_output_invariants(transformed.text, output_bytes, deadline)
            output_sha256 = self._sha256(output_bytes)
            input_sha256 = self._sha256(raw_input)
            summary = self._build_summary(
                duplicate_count=transformed.duplicate_count,
                redaction_summary=transformed.redaction_summary,
                formatting_changes=transformed.formatting_changes,
            )
            result = ProcessorResult(
                output_path=destination_path,
                input_sha256=input_sha256,
                output_sha256=output_sha256,
                contract_version="markdown_cleaning_v1",
                summary=summary,
                input_bytes=len(raw_input),
                output_bytes=len(output_bytes),
            )
            self._write_staging_atomic(destination_path, output_bytes, deadline)
            return result
        except MarkdownCleaningProcessorError:
            raise
        except Exception as exc:
            raise map_processing_exception(exc) from exc

    def _transform_text(
        self,
        source_text: str,
        deadline: _ProcessingDeadline,
    ) -> _PipelineTransformResult:
        parsed_input = self._parse(source_text, deadline)
        self._enforce_parse_invariants(parsed_input, deadline)

        deduped_text, duplicate_count = self._deduplicate(source_text, deadline)
        deduped_parse = self._parse(deduped_text, deadline)
        self._enforce_parse_invariants(deduped_parse, deadline)

        self._enforce_pii_candidate_limit(
            deduped_text,
            deduped_parse.protected_spans,
            deadline,
        )

        redaction = self._redact(
            deduped_text,
            deduped_parse.protected_spans,
            deadline,
        )
        redacted_parse = self._parse(redaction.text, deadline)
        self._enforce_parse_invariants(redacted_parse, deadline)

        formatted = self._format(redaction.text, deadline)
        formatted_parse = self._parse(formatted.text, deadline)
        self._enforce_parse_invariants(formatted_parse, deadline)
        return _PipelineTransformResult(
            text=formatted.text,
            duplicate_count=duplicate_count,
            redaction_summary=redaction.summary,
            formatting_changes=formatted.formatting_changes,
        )

    def _run_transform_isolated(
        self,
        source_text: str,
        deadline: _ProcessingDeadline,
    ) -> _PipelineTransformResult:
        deadline.check()
        runtime_command = self._runtime_command
        if runtime_command is None:
            raise map_processing_exception(
                RuntimeError("isolated runtime command missing"),
                MarkdownCleaningErrorCode.INTERNAL_ERROR,
            )
        request = json.dumps(
            {
                "markdown": source_text,
                "limits": asdict(self._limits),
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        try:
            process = subprocess.Popen(
                runtime_command,
                cwd=Path(__file__).resolve().parents[4],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                env=self._runtime_environment(),
            )
        except OSError as exc:
            raise map_processing_exception(
                exc,
                MarkdownCleaningErrorCode.INTERNAL_ERROR,
            ) from exc

        try:
            stdout, _stderr = process.communicate(
                input=request,
                timeout=deadline.remaining_seconds(),
            )
        except subprocess.TimeoutExpired as exc:
            self._terminate_runtime(process)
            raise map_processing_exception(
                TimeoutError("isolated transform timeout"),
                MarkdownCleaningErrorCode.PROCESSING_TIMEOUT,
            ) from exc

        deadline.check()
        if process.returncode != 0:
            raise map_processing_exception(
                RuntimeError("isolated transform failed"),
                MarkdownCleaningErrorCode.INTERNAL_ERROR,
            )
        try:
            response = json.loads(stdout.decode("utf-8"))
            if not isinstance(response, dict):
                raise ValueError("runtime response must be an object")
            if response.get("ok") is not True:
                code = MarkdownCleaningErrorCode(response["errorCode"])
                safe_message = str(response["safeMessage"])
                raise MarkdownCleaningProcessorError(code, safe_message)
            redaction = response["redactionSummary"]
            if not isinstance(redaction, dict):
                raise ValueError("runtime redaction summary must be an object")
            return _PipelineTransformResult(
                text=str(response["text"]),
                duplicate_count=int(response["duplicateCount"]),
                redaction_summary=SensitiveRedactionSummary(
                    phone=int(redaction["phone"]),
                    id_card=int(redaction["idCard"]),
                    bank_card=int(redaction["bankCard"]),
                    email=int(redaction["email"]),
                    ipv4=int(redaction["ipv4"]),
                ),
                formatting_changes=int(response["formattingChanges"]),
            )
        except MarkdownCleaningProcessorError:
            raise
        except (KeyError, TypeError, ValueError, UnicodeDecodeError) as exc:
            raise map_processing_exception(
                exc,
                MarkdownCleaningErrorCode.INTERNAL_ERROR,
            ) from exc

    @staticmethod
    def _terminate_runtime(process: subprocess.Popen[bytes]) -> None:
        try:
            process.kill()
        except OSError:
            return
        try:
            process.communicate(timeout=0.5)
        except subprocess.TimeoutExpired:
            if process.stdin is not None:
                process.stdin.close()
            if process.stdout is not None:
                process.stdout.close()

    @staticmethod
    def _runtime_environment() -> dict[str, str]:
        environment = {
            key: os.environ[key]
            for key in _RUNTIME_ENV_ALLOWLIST
            if key in os.environ
        }
        environment["PYTHONIOENCODING"] = "utf-8"
        environment["PYTHONUTF8"] = "1"
        return environment

    def _validate_staging_paths(
        self,
        source: Path,
        destination: Path,
    ) -> tuple[Path, Path]:
        if self._has_link_component(source):
            raise map_processing_exception(
                ValueError("source cannot be a link"),
                MarkdownCleaningErrorCode.INVALID_MARKDOWN_INPUT,
            )
        if self._has_link_component(destination):
            raise map_processing_exception(
                ValueError("destination cannot use links"),
                MarkdownCleaningErrorCode.INVALID_PROCESSOR_OUTPUT,
            )
        try:
            resolved_source = source.resolve(strict=True)
        except OSError as exc:
            raise map_processing_exception(
                exc,
                MarkdownCleaningErrorCode.INVALID_MARKDOWN_INPUT,
            ) from exc
        if not resolved_source.is_file() or not resolved_source.is_relative_to(
            self._staging_root
        ):
            raise map_processing_exception(
                ValueError("source outside staging root"),
                MarkdownCleaningErrorCode.INVALID_MARKDOWN_INPUT,
            )

        try:
            resolved_destination_parent = destination.parent.resolve(strict=True)
            resolved_destination = (
                destination.resolve(strict=True)
                if destination.exists()
                else resolved_destination_parent / destination.name
            )
        except OSError as exc:
            raise map_processing_exception(
                exc,
                MarkdownCleaningErrorCode.INVALID_PROCESSOR_OUTPUT,
            ) from exc
        if not resolved_destination.is_relative_to(self._staging_root):
            raise map_processing_exception(
                ValueError("destination outside staging root"),
                MarkdownCleaningErrorCode.INVALID_PROCESSOR_OUTPUT,
            )
        if resolved_source == resolved_destination:
            raise map_processing_exception(
                ValueError("source and destination must differ"),
                MarkdownCleaningErrorCode.INVALID_PROCESSOR_OUTPUT,
            )
        return resolved_source, resolved_destination

    @staticmethod
    def _is_link_or_junction(path: Path) -> bool:
        return path.is_symlink() or (
            hasattr(os.path, "isjunction") and os.path.isjunction(path)
        )

    def _has_link_component(self, path: Path) -> bool:
        lexical_path = Path(os.path.abspath(path))
        try:
            relative_path = lexical_path.relative_to(self._staging_root)
        except ValueError:
            return False

        candidate = self._staging_root
        for part in relative_path.parts:
            candidate /= part
            if self._is_link_or_junction(candidate):
                return True
        return False

    @staticmethod
    def _sha256(content: bytes) -> str:
        return hashlib.sha256(content).hexdigest()

    def _read_source(self, source: Path, deadline: _ProcessingDeadline) -> bytes:
        deadline.check()
        try:
            with source.open("rb") as stream:
                content = stream.read(self._limits.max_input_bytes + 1)
        except OSError as exc:
            raise map_processing_exception(
                exc,
                MarkdownCleaningErrorCode.INVALID_MARKDOWN_INPUT,
            ) from exc
        deadline.check()
        if len(content) > self._limits.max_input_bytes:
            raise map_processing_exception(
                ValueError("input exceeds configured size"),
                MarkdownCleaningErrorCode.INVALID_MARKDOWN_INPUT,
            )
        return content

    def _decode_utf8_no_bom(self, content: bytes) -> str:
        if b"\x00" in content:
            raise map_processing_exception(
                ValueError("input contains NUL byte"),
                MarkdownCleaningErrorCode.INVALID_MARKDOWN_INPUT,
            )
        try:
            text = content.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise map_processing_exception(
                ValueError("input not valid UTF-8"),
                MarkdownCleaningErrorCode.INVALID_MARKDOWN_INPUT,
            ) from exc
        if text.startswith(_BOM):
            raise map_processing_exception(
                ValueError("input contains BOM"),
                MarkdownCleaningErrorCode.INVALID_MARKDOWN_INPUT,
            )
        return text

    def _parse(
        self,
        markdown: str,
        deadline: _ProcessingDeadline,
    ) -> MarkdownParseResult:
        deadline.check()
        try:
            self._check_token_count(markdown)
            parsed = self._parser.parse(markdown)
        except MarkdownCleaningProcessorError:
            raise
        except MarkdownParserError as exc:
            error_code = (
                MarkdownCleaningErrorCode.INVALID_MARKDOWN_INPUT
                if exc.code is MarkdownParserErrorCode.INVALID_MARKDOWN_INPUT
                else MarkdownCleaningErrorCode.MARKDOWN_PARSE_FAILED
            )
            raise map_processing_exception(exc, error_code) from exc
        except Exception as exc:
            raise map_processing_exception(
                exc,
                MarkdownCleaningErrorCode.MARKDOWN_PARSE_FAILED,
            ) from exc
        deadline.check()
        return parsed

    def _deduplicate(
        self,
        markdown: str,
        deadline: _ProcessingDeadline,
    ) -> tuple[str, int]:
        deadline.check()
        try:
            result = self._deduplicator.deduplicate(markdown)
        except Exception as exc:
            raise map_processing_exception(
                exc,
                MarkdownCleaningErrorCode.PARAGRAPH_DEDUPLICATION_FAILED,
            ) from exc
        deadline.check()
        return result

    def _redact(
        self,
        markdown: str,
        protected_spans: tuple[SourceSpan, ...],
        deadline: _ProcessingDeadline,
    ) -> SensitiveRedactionResult:
        deadline.check()
        try:
            result = self._redactor.redact(markdown, protected_spans=protected_spans)
        except Exception as exc:
            raise map_processing_exception(
                exc,
                MarkdownCleaningErrorCode.SENSITIVE_DATA_REDACTION_FAILED,
            ) from exc
        deadline.check()
        return result

    def _format(
        self,
        markdown: str,
        deadline: _ProcessingDeadline,
    ) -> MarkdownFormatterResult:
        deadline.check()
        try:
            result = self._formatter.format(markdown)
        except Exception as exc:
            if isinstance(exc, MarkdownCleaningProcessorError):
                raise
            raise map_processing_exception(
                exc,
                MarkdownCleaningErrorCode.MARKDOWN_NORMALIZATION_FAILED,
            ) from exc
        deadline.check()
        return result

    def _check_token_count(self, markdown: str) -> None:
        tokens = self._token_parser.parse(markdown)
        if len(tokens) > self._limits.max_token_count:
            raise map_processing_exception(
                ValueError("token count exceeds limit"),
                MarkdownCleaningErrorCode.INVALID_MARKDOWN_INPUT,
            )

    def _enforce_parse_invariants(
        self,
        parse_result: MarkdownParseResult,
        deadline: _ProcessingDeadline,
    ) -> None:
        deadline.check()
        blocks = parse_result.blocks
        protected_spans = parse_result.protected_spans
        if len(blocks) > self._limits.max_block_count:
            raise map_processing_exception(
                ValueError("too many blocks"),
                MarkdownCleaningErrorCode.INVALID_MARKDOWN_INPUT,
            )

        if len(protected_spans) > self._limits.max_protected_span_count:
            raise map_processing_exception(
                ValueError("too many protected spans"),
                MarkdownCleaningErrorCode.INVALID_MARKDOWN_INPUT,
            )

        for block in blocks:
            if block.source_span.end - block.source_span.start > self._limits.max_block_char_span:
                raise map_processing_exception(
                    ValueError("single block size exceeds limit"),
                    MarkdownCleaningErrorCode.INVALID_MARKDOWN_INPUT,
                )

    def _enforce_output_invariants(
        self,
        text: str,
        content: bytes,
        deadline: _ProcessingDeadline,
    ) -> None:
        deadline.check()
        if len(content) > self._limits.max_output_bytes:
            raise map_processing_exception(
                ValueError("output exceeds configured size"),
                MarkdownCleaningErrorCode.INVALID_PROCESSOR_OUTPUT,
            )
        if not content:
            raise map_processing_exception(
                ValueError("output is empty"),
                MarkdownCleaningErrorCode.INVALID_PROCESSOR_OUTPUT,
            )
        if "\x00" in text or text.startswith(_BOM) or "\r" in text:
            raise map_processing_exception(
                ValueError("output encoding invariants failed"),
                MarkdownCleaningErrorCode.INVALID_PROCESSOR_OUTPUT,
            )
        if not text.endswith("\n") or text.endswith("\n\n"):
            raise map_processing_exception(
                ValueError("output terminal newline invariant failed"),
                MarkdownCleaningErrorCode.INVALID_PROCESSOR_OUTPUT,
            )

    def _write_staging_atomic(
        self,
        destination: Path,
        content: bytes,
        deadline: _ProcessingDeadline,
    ) -> None:
        deadline.check()
        destination_parent = destination.parent
        fd = None
        handle = None
        tmp_path = None
        try:
            fd, tmp_name = tempfile.mkstemp(
                prefix="markdown-cleaning-",
                suffix=".tmp",
                dir=destination_parent,
            )
            tmp_path = Path(tmp_name)
            handle = os.fdopen(fd, "wb")
            fd = None
            with handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            deadline.check()
            os.replace(tmp_path, destination)
        except Exception as exc:
            if tmp_path is not None and tmp_path.exists():
                try:
                    tmp_path.unlink()
                except Exception:
                    pass
            if isinstance(exc, MarkdownCleaningProcessorError):
                raise
            if isinstance(exc, OSError):
                raise map_processing_exception(
                    exc,
                    MarkdownCleaningErrorCode.INVALID_PROCESSOR_OUTPUT,
                ) from exc
            raise map_processing_exception(
                exc,
                MarkdownCleaningErrorCode.INTERNAL_ERROR,
            ) from exc
        finally:
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
            if handle is not None:
                try:
                    handle.close()
                except OSError:
                    pass

    @staticmethod
    def _build_summary(
        *,
        duplicate_count: int,
        redaction_summary: SensitiveRedactionSummary,
        formatting_changes: int,
    ) -> MarkdownCleaningSummary:
        try:
            return MarkdownCleaningSummary(
                duplicate_paragraphs_removed=duplicate_count,
                phone_redactions=redaction_summary.phone,
                id_card_redactions=redaction_summary.id_card,
                bank_card_redactions=redaction_summary.bank_card,
                email_redactions=redaction_summary.email,
                ipv4_redactions=redaction_summary.ipv4,
                formatting_changes=formatting_changes,
            )
        except (TypeError, ValueError) as exc:
            raise map_processing_exception(
                exc,
                MarkdownCleaningErrorCode.INVALID_PROCESSOR_OUTPUT,
            ) from exc

    @staticmethod
    def _span_overlaps(span_start: int, span_end: int, other: SourceSpan) -> bool:
        return span_start < other.end and span_end > other.start

    def _enforce_pii_candidate_limit(
        self,
        markdown: str,
        protected_spans: tuple[SourceSpan, ...],
        deadline: _ProcessingDeadline,
    ) -> None:
        count = 0
        patterns = (
            _EMAIL_CANDIDATE_PATTERN,
            _CN_MOBILE_CANDIDATE_PATTERN,
            _ID_CARD_CANDIDATE_PATTERN,
            _CREDIT_CARD_CANDIDATE_PATTERN,
            _IPV4_CANDIDATE_PATTERN,
        )
        for pattern in patterns:
            for match in pattern.finditer(markdown):
                if self._is_unprotected(match.start(), match.end(), protected_spans):
                    count += 1
                    if count > self._limits.max_pii_candidate_count:
                        raise map_processing_exception(
                            ValueError("pii candidates exceed limit"),
                            MarkdownCleaningErrorCode.INVALID_MARKDOWN_INPUT,
                        )
            deadline.check()

    def _is_unprotected(
        self,
        start: int,
        end: int,
        protected_spans: tuple[SourceSpan, ...],
    ) -> bool:
        for span in protected_spans:
            if self._span_overlaps(start, end, span):
                return False
        return True
