import ipaddress
import secrets
import warnings
from pathlib import Path
from typing import Annotated, Any, Literal, Self

from pydantic import (
    AnyUrl,
    BaseModel,
    BeforeValidator,
    EmailStr,
    Field,
    HttpUrl,
    PostgresDsn,
    computed_field,
    model_validator,
)
from pydantic_settings import BaseSettings, SettingsConfigDict


def parse_cors(v: Any) -> list[str] | str:
    if isinstance(v, str) and not v.startswith("["):
        return [i.strip() for i in v.split(",") if i.strip()]
    elif isinstance(v, list | str):
        return v
    raise ValueError(v)


def _has_parent_or_child_overlap(left: Path, right: Path) -> bool:
    return left == right or left in right.parents or right in left.parents


class MinerUProfile(BaseModel):
    backend: str = "hybrid-engine"
    parse_method: str = "auto"
    lang_list: str = "ch"
    formula_enable: bool = False
    table_enable: bool = True
    return_md: Literal[True] = True
    return_middle_json: bool = False
    return_content_list: bool = True
    return_images: Literal[False] = False
    response_format_zip: Literal[False] = False
    start_page_id: int = Field(default=0, ge=0)
    end_page_id: int = Field(default=99999, ge=0)
    effort: str = "high"


class DoclingProfile(BaseModel):
    to_formats: tuple[Literal["md"], ...] = ("md",)
    image_export_mode: Literal["placeholder"] = "placeholder"
    do_ocr: Literal[False] = False
    table_mode: Literal["fast", "accurate"] = "accurate"


class ExtractionWorkerSettings(BaseModel):
    staging_root: Path = Path("/data/textprocessor/staging")
    output_roots: tuple[Path, ...] = (Path("/data/textprocessor/output"),)
    copy_chunk_bytes: int = Field(default=1024 * 1024, gt=0)
    max_output_bytes: int = Field(default=100 * 1024 * 1024, gt=0)
    connect_timeout_seconds: float = Field(default=10, gt=0)
    read_timeout_seconds: float = Field(default=60, gt=0)
    max_http_redirects: int = Field(default=3, ge=0)
    poll_interval_seconds: int = Field(default=5, gt=0)
    processing_deadline_seconds: int = Field(default=3600, gt=0)
    poll_lease_seconds: int = Field(default=30, gt=0)
    recovery_batch_size: int = Field(default=100, gt=0)
    slot_quarantine_grace_seconds: int = Field(default=300, ge=0)
    failed_staging_retention_seconds: int = Field(default=86400, ge=0)
    mineru_max_in_flight_tasks: int = Field(default=2, gt=0)
    docling_max_in_flight_tasks: int = Field(default=2, gt=0)
    docx_visual_complexity_threshold: int = Field(default=5, ge=0)
    production_formats: tuple[str, ...] = (
        "text",
        "markdown",
        "json",
        "xml",
        "yaml",
        "csv",
        "tsv",
    )
    s3_allowed_buckets: tuple[str, ...] = ()
    s3_endpoint_url: HttpUrl | None = None
    s3_region: str | None = None
    s3_access_key_id: str | None = Field(default=None, repr=False)
    s3_secret_access_key: str | None = Field(default=None, repr=False)
    mineru_base_url: HttpUrl | None = None
    mineru_api_key: str | None = Field(default=None, repr=False)
    mineru_profile_name: str = "default"
    mineru_profile: MinerUProfile = Field(default_factory=MinerUProfile)
    docling_base_url: HttpUrl | None = None
    docling_api_key: str | None = Field(default=None, repr=False)
    docling_profile_name: str = "default"
    docling_profile: DoclingProfile = Field(default_factory=DoclingProfile)

    @model_validator(mode="after")
    def _normalize_and_validate_roots(self) -> Self:
        staging_root = self.staging_root.resolve(strict=False)
        output_roots = tuple(
            sorted(
                {path.resolve(strict=False) for path in self.output_roots},
                key=str,
            )
        )
        if not output_roots:
            raise ValueError("至少配置一个结构化提取输出根目录")
        for output_root in output_roots:
            if (
                staging_root == output_root
                or staging_root in output_root.parents
                or output_root in staging_root.parents
            ):
                raise ValueError("结构化提取 staging 与输出根目录不能重叠")
        self.staging_root = staging_root
        self.output_roots = output_roots
        return self


class MarkdownCleaningWorkerSettings(BaseModel):
    staging_root: Path = Path("/data/textprocessor/markdown-cleaning")
    output_roots: tuple[Path, ...] = (Path("/data/textprocessor/output"),)
    allowed_http_hosts: tuple[str, ...] = ()
    allowed_http_cidrs: tuple[str, ...] = ()
    max_input_bytes: int = Field(default=100 * 1024 * 1024, gt=0)
    copy_chunk_bytes: int = Field(default=1024 * 1024, gt=0)
    max_output_bytes: int = Field(default=100 * 1024 * 1024, gt=0)
    connect_timeout_seconds: float = Field(default=10, gt=0)
    read_timeout_seconds: float = Field(default=60, gt=0)
    max_http_redirects: int = Field(default=3, ge=0)
    queue_lease_seconds: int = Field(default=120, gt=0)
    queue_recovery_interval_seconds: int = Field(default=30, gt=0)
    queue_recovery_batch_size: int = Field(default=100, gt=0)
    processing_soft_timeout_seconds: int = Field(default=300, gt=0)
    processing_hard_timeout_seconds: int = Field(default=3600, gt=0)
    max_attempts: int = Field(default=3, gt=0)
    max_in_flight_tasks: int = Field(default=4, gt=0)
    allowed_stale_grace_seconds: int = Field(default=300, ge=0)

    @model_validator(mode="after")
    def _normalize_and_validate_limits(self) -> Self:
        self.staging_root = self.staging_root.resolve(strict=False)
        self.output_roots = tuple(
            sorted({path.resolve(strict=False) for path in self.output_roots}, key=str)
        )
        if not self.output_roots:
            raise ValueError("至少配置一个 Markdown 清洗输出根目录")
        for output_root in self.output_roots:
            if (
                self.staging_root == output_root
                or self.staging_root in output_root.parents
                or output_root in self.staging_root.parents
            ):
                raise ValueError("Markdown 清洗 staging 与输出根目录不能重叠")
        if self.processing_hard_timeout_seconds <= self.processing_soft_timeout_seconds:
            raise ValueError(
                "Markdown 清洗 worker 的硬超时必须大于软超时"
            )
        self.allowed_http_hosts = tuple(
            sorted(
                {
                    host.rstrip(".").lower()
                    for host in self.allowed_http_hosts
                    if isinstance(host, str) and host.strip()
                }
            )
        )
        validated_networks = []
        for cidr in self.allowed_http_cidrs:
            if not isinstance(cidr, str) or not cidr.strip():
                raise ValueError("allowed_http_cidrs 必须是有效 CIDR 字符串")
            try:
                validated_networks.append(ipaddress.ip_network(cidr, strict=False))
            except ValueError as exc:
                raise ValueError("allowed_http_cidrs 必须是有效 CIDR 字符串") from exc
        self.allowed_http_cidrs = tuple(
            sorted({str(network) for network in validated_networks})
        )
        return self


class GlobalDeduplicationWorkerSettings(BaseModel):
    staging_root: Path = Path("/data/textprocessor/global-deduplication")
    output_roots: tuple[Path, ...] = (Path("/data/textprocessor/output"),)
    max_documents: int = Field(default=100_000, gt=0)
    max_manifest_bytes: int = Field(default=50 * 1024 * 1024, gt=0)
    max_document_bytes: int = Field(default=100 * 1024 * 1024, gt=0)
    max_total_bytes: int = Field(default=10 * 1024 * 1024 * 1024, gt=0)
    copy_chunk_bytes: int = Field(default=1024 * 1024, gt=0)
    max_http_redirects: int = Field(default=3, ge=0)
    s3_allowed_buckets: tuple[str, ...] = ()
    s3_endpoint_url: HttpUrl | None = None
    s3_region: str | None = None
    s3_access_key_id: str | None = Field(default=None, repr=False)
    s3_secret_access_key: str | None = Field(default=None, repr=False)
    datajuicer_base_url: HttpUrl | None = None
    datajuicer_profile: Literal["text_exact_minhash_v1"] = "text_exact_minhash_v1"
    datajuicer_connect_timeout_seconds: float = Field(default=10, gt=0)
    datajuicer_submit_timeout_seconds: float = Field(default=30, gt=0)
    datajuicer_poll_timeout_seconds: float = Field(default=10, gt=0)
    datajuicer_poll_initial_delay_seconds: int = Field(default=5, gt=0)
    datajuicer_poll_max_delay_seconds: int = Field(default=60, gt=0)
    datajuicer_processing_timeout_seconds: int = Field(default=3600, gt=0)
    submit_lease_seconds: int = Field(default=300, gt=0)
    poll_lease_seconds: int = Field(default=30, gt=0)
    recovery_interval_seconds: int = Field(default=30, gt=0)
    recovery_batch_size: int = Field(default=100, gt=0)
    staging_retention_seconds: int = Field(default=86400, ge=0)

    @model_validator(mode="after")
    def _normalize_and_validate_limits(self) -> Self:
        if self.max_total_bytes < self.max_document_bytes:
            raise ValueError("批次累计限制不能小于单文档限制")
        if (
            self.datajuicer_poll_max_delay_seconds
            < self.datajuicer_poll_initial_delay_seconds
        ):
            raise ValueError("最大轮询间隔不能小于初始轮询间隔")
        self.staging_root = self.staging_root.resolve(strict=False)
        self.output_roots = tuple(
            sorted(
                {path.resolve(strict=False) for path in self.output_roots},
                key=str,
            )
        )
        if not self.output_roots:
            raise ValueError("至少配置一个全局去重输出根目录")
        if any(
            self.staging_root == root
            or self.staging_root in root.parents
            or root in self.staging_root.parents
            for root in self.output_roots
        ):
            raise ValueError("全局去重 staging 与输出根目录不能重叠")
        return self


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        # Use top level .env file (one level above ./backend/)
        env_file="../.env",
        env_nested_delimiter="__",
        env_ignore_empty=True,
        extra="ignore",
    )
    API_V1_STR: str = "/api/v1"
    SECRET_KEY: str = secrets.token_urlsafe(32)
    # 60 minutes * 24 hours * 8 days = 8 days
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 60 * 24 * 8
    FRONTEND_HOST: str = "http://localhost:5173"
    ENVIRONMENT: Literal["local", "staging", "production"] = "local"

    BACKEND_CORS_ORIGINS: Annotated[
        list[AnyUrl] | str, BeforeValidator(parse_cors)
    ] = []

    @computed_field  # type: ignore[prop-decorator]
    @property
    def all_cors_origins(self) -> list[str]:
        return [str(origin).rstrip("/") for origin in self.BACKEND_CORS_ORIGINS] + [
            self.FRONTEND_HOST
        ]

    PROJECT_NAME: str
    SENTRY_DSN: HttpUrl | None = None
    POSTGRES_SERVER: str
    POSTGRES_PORT: int = 5432
    POSTGRES_USER: str
    POSTGRES_PASSWORD: str = ""
    POSTGRES_DB: str = ""
    EXTRACTION_INPUT_ROOTS: list[Path] = []
    EXTRACTION_OUTPUT_ROOTS: list[Path] = []
    EXTRACTION_HTTP_ALLOWED_HOSTS: list[str] = []
    EXTRACTION_HTTP_ALLOWED_CIDRS: list[str] = []
    EXTRACTION_MAX_INPUT_BYTES: int = 100 * 1024 * 1024
    EXTRACTION_QUEUE_RECOVERY_AFTER_SECONDS: int = 60
    EXTRACTION_QUEUE_RECOVERY_INTERVAL_SECONDS: int = 30
    GLOBAL_DEDUP_INPUT_ROOTS: list[Path] = []
    GLOBAL_DEDUP_HTTP_ALLOWED_HOSTS: list[str] = []
    GLOBAL_DEDUP_HTTP_ALLOWED_CIDRS: list[str] = []
    MARKDOWN_CLEANING_INPUT_ROOTS: list[Path] = []
    MARKDOWN_CLEANING_OUTPUT_ROOTS: list[Path] = []
    MARKDOWN_CLEANING_HTTP_ALLOWED_HOSTS: list[str] = []
    MARKDOWN_CLEANING_HTTP_ALLOWED_CIDRS: list[str] = []
    MARKDOWN_CLEANING_WORKER: MarkdownCleaningWorkerSettings = Field(
        default_factory=MarkdownCleaningWorkerSettings
    )
    CELERY_BROKER_VISIBILITY_TIMEOUT_SECONDS: int = Field(default=3660, gt=0)
    EXTRACTION_WORKER: ExtractionWorkerSettings = Field(
        default_factory=ExtractionWorkerSettings
    )
    GLOBAL_DEDUP_WORKER: GlobalDeduplicationWorkerSettings = Field(
        default_factory=GlobalDeduplicationWorkerSettings
    )
    CELERY_BROKER_URL: str = "redis://redis:6379/0"
    CLASSIFICATION_INPUT_ROOTS: list[Path] = []
    CLASSIFICATION_STAGING_ROOT: Path = Path("/data/textprocessor/classification")
    CLASSIFICATION_MAX_INPUT_BYTES: int = Field(default=2_000_000, gt=0)
    CLASSIFICATION_BASE_URL: str = "http://classification:8000"
    CLASSIFICATION_API_TOKEN: str | None = Field(default=None, repr=False)
    CLASSIFICATION_TIMEOUT_SECONDS: float = Field(default=300, gt=0)
    CLASSIFICATION_RECOVERY_INTERVAL_SECONDS: int = Field(default=30, gt=0)
    CLASSIFICATION_RECOVERY_BATCH_SIZE: int = Field(default=100, gt=0)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def SQLALCHEMY_DATABASE_URI(self) -> PostgresDsn:
        return PostgresDsn.build(
            scheme="postgresql+psycopg",
            username=self.POSTGRES_USER,
            password=self.POSTGRES_PASSWORD,
            host=self.POSTGRES_SERVER,
            port=self.POSTGRES_PORT,
            path=self.POSTGRES_DB,
        )

    SMTP_TLS: bool = True
    SMTP_SSL: bool = False
    SMTP_PORT: int = 587
    SMTP_HOST: str | None = None
    SMTP_USER: str | None = None
    SMTP_PASSWORD: str | None = None
    EMAILS_FROM_EMAIL: EmailStr | None = None
    EMAILS_FROM_NAME: str | None = None

    @model_validator(mode="after")
    def _set_default_emails_from(self) -> Self:
        if not self.EMAILS_FROM_NAME:
            self.EMAILS_FROM_NAME = self.PROJECT_NAME
        return self

    EMAIL_RESET_TOKEN_EXPIRE_HOURS: int = 48

    @computed_field  # type: ignore[prop-decorator]
    @property
    def emails_enabled(self) -> bool:
        return bool(self.SMTP_HOST and self.EMAILS_FROM_EMAIL)

    EMAIL_TEST_USER: EmailStr = "test@example.com"
    FIRST_SUPERUSER: EmailStr
    FIRST_SUPERUSER_PASSWORD: str

    def _check_default_secret(self, var_name: str, value: str | None) -> None:
        if value == "changethis":
            message = (
                f'The value of {var_name} is "changethis", '
                "for security, please change it, at least for deployments."
            )
            if self.ENVIRONMENT == "local":
                warnings.warn(message, stacklevel=1)
            else:
                raise ValueError(message)

    @model_validator(mode="after")
    def _enforce_non_default_secrets(self) -> Self:
        self._check_default_secret("SECRET_KEY", self.SECRET_KEY)
        self._check_default_secret("POSTGRES_PASSWORD", self.POSTGRES_PASSWORD)
        self._check_default_secret(
            "FIRST_SUPERUSER_PASSWORD", self.FIRST_SUPERUSER_PASSWORD
        )

        return self

    @model_validator(mode="after")
    def _validate_extraction_roots(self) -> Self:
        input_roots = {
            path.resolve(strict=False) for path in self.EXTRACTION_INPUT_ROOTS
        }
        output_roots = {
            path.resolve(strict=False) for path in self.EXTRACTION_OUTPUT_ROOTS
        }
        if input_roots & output_roots:
            raise ValueError("结构化提取输入根目录和输出根目录不能重叠")
        self.EXTRACTION_INPUT_ROOTS = sorted(input_roots, key=str)
        self.EXTRACTION_OUTPUT_ROOTS = sorted(output_roots, key=str)
        global_input_roots = {
            path.resolve(strict=False) for path in self.GLOBAL_DEDUP_INPUT_ROOTS
        }
        if global_input_roots & set(self.GLOBAL_DEDUP_WORKER.output_roots):
            raise ValueError("全局去重输入根目录和输出根目录不能重叠")
        self.GLOBAL_DEDUP_INPUT_ROOTS = sorted(global_input_roots, key=str)
        markdown_input_roots = {
            path.resolve(strict=False) for path in self.MARKDOWN_CLEANING_INPUT_ROOTS
        }
        markdown_output_roots = {
            path.resolve(strict=False) for path in self.MARKDOWN_CLEANING_OUTPUT_ROOTS
        }
        if markdown_input_roots & markdown_output_roots:
            raise ValueError("Markdown 清洗输入根目录和输出根目录不能重叠")
        if any(
            _has_parent_or_child_overlap(root, self.MARKDOWN_CLEANING_WORKER.staging_root)
            for root in markdown_input_roots
        ):
            raise ValueError("Markdown 清洗输入根目录和 worker 根目录不能重叠")
        if any(
            _has_parent_or_child_overlap(root, self.MARKDOWN_CLEANING_WORKER.staging_root)
            for root in markdown_output_roots
        ):
            raise ValueError("Markdown 清洗输出根目录和 worker 根目录不能重叠")
        if any(
            _has_parent_or_child_overlap(
                root, worker_output_root
            )
            for root in markdown_input_roots
            for worker_output_root in self.MARKDOWN_CLEANING_WORKER.output_roots
        ):
            raise ValueError("Markdown 清洗输入根目录和输出任务根目录不能重叠")
        self.MARKDOWN_CLEANING_INPUT_ROOTS = sorted(markdown_input_roots, key=str)
        self.MARKDOWN_CLEANING_OUTPUT_ROOTS = sorted(markdown_output_roots, key=str)
        return self


settings = Settings()  # type: ignore
