import uuid
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path


class ProcessorName(StrEnum):
    PLAIN_TEXT = "plain_text"
    MINERU = "mineru"
    DOCLING = "docling"


class ExtractionProcessingPhase(StrEnum):
    STAGING = "staging"
    WAITING_CAPACITY = "waiting_capacity"
    SUBMITTING = "submitting"
    SUBMITTED = "submitted"
    POLLING = "polling"
    DOWNLOADING = "downloading"
    NORMALIZING = "normalizing"
    PUBLISHING = "publishing"


class DetectedFormat(StrEnum):
    PDF = "pdf"
    DOC = "doc"
    DOCX = "docx"
    PPT = "ppt"
    PPTX = "pptx"
    XLS = "xls"
    XLSX = "xlsx"
    HTML = "html"
    EPUB = "epub"
    JSON = "json"
    XML = "xml"
    YAML = "yaml"
    CSV = "csv"
    TSV = "tsv"
    MARKDOWN = "markdown"
    IMAGE = "image"
    TEXT = "text"
    UNKNOWN_TEXT = "unknown_text"


class ExternalTaskState(StrEnum):
    PROCESSING = "processing"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


@dataclass(frozen=True)
class ProcessingContext:
    task_id: uuid.UUID
    detected_format: DetectedFormat
    profile_name: str
    profile_sha256: str


@dataclass(frozen=True)
class ExternalTaskSubmission:
    external_task_id: str
    processor_name: ProcessorName
    processor_version: str | None


@dataclass(frozen=True)
class ExternalTaskStatus:
    state: ExternalTaskState
    safe_error_code: str | None = None
    safe_error_message: str | None = None


@dataclass(frozen=True)
class ProcessorArtifact:
    markdown_path: Path
    processor_name: ProcessorName
    processor_version: str | None
    profile_name: str
    profile_sha256: str
