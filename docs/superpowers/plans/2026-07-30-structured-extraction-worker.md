# Structured Extraction Worker and Docling Deployment Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 实现结构化提取 worker 的输入 staging、格式路由、文本直通、MinerU/Docling 异步 adapter、非阻塞 Celery 编排、可靠发布、容量恢复，并部署和真实验证独立 Docling 服务。

**Architecture:** Celery worker 只接收 `task_id` 并从 PostgreSQL读取权威参数。轻量文本在本地 processor 内完成；复杂文档通过统一 adapter 提交 MinerU/Docling HTTP 异步任务，使用短 poll task 和 PostgreSQL 租约恢复。Docling 采用独立 API、专用 Redis 和 RQ worker，TextProcessor 不访问其内部队列。

**Tech Stack:** Python 3.14、Celery 5、Redis、PostgreSQL 18、SQLModel、fsspec/s3fs、httpx、Mistune 3、defusedxml、pytest、Docker Compose、Docling Serve v1。

## Global Constraints

- 本计划依赖 `2026-07-30-structured-extraction-task.md` 完成的任务表、repository、状态机、dispatcher 和 API。
- PostgreSQL 是任务、处理阶段、外部 task ID、slot、租约和结果摘要的权威来源。
- Celery 消息只携带 `task_id`、任务类型和 schema version。
- Worker 不在轮询循环中 sleep；每个 poll task 只查询外部服务一次。
- Processor 一旦选定不自动跨引擎降级。
- 文本直通只统一编码为 UTF-8 无 BOM，不 parse 后重组正文。
- 外部 processor 最终只发布一个 Markdown 文件，必须删除图片资源引用并保留可用 alt/图注。
- 最终目标已存在时失败，禁止覆盖；发布前预检和发布时原子冲突保护都必须存在。
- MinerU/Docling 服务 URL、认证、超时和 profile 只来自启动配置，修改后重启生效。
- Docling 必须独立部署并逐格式真实验证；未通过的格式不得进入 production allowlist。
- 真实外部服务测试与默认快速测试分离，未运行不得声称通过。

---

## File Structure

```text
backend/app/features/structured_extraction/
├── worker_models.py             # 外部任务、artifact、路由值对象
├── staging.py                   # staging 生命周期
├── input_resolver.py            # fsspec 流式输入
├── format_detector.py           # 文件签名与文本检测
├── office_inspector.py          # DOCX OOXML 预检
├── router.py                    # production allowlist 与固定路由
├── processors/
│   ├── __init__.py
│   ├── protocol.py
│   ├── plain_text.py
│   ├── markdown_normalizer.py
│   └── publisher.py
├── adapters/
│   ├── __init__.py
│   ├── protocol.py
│   ├── mineru.py
│   └── docling.py
├── slots.py                     # PostgreSQL processor slot
├── orchestration.py             # submit/poll/reconcile use cases
└── celery_tasks.py              # Celery task 薄入口

backend/tests/features/structured_extraction/
├── test_staging.py
├── test_input_resolver.py
├── test_format_detector.py
├── test_office_inspector.py
├── test_router.py
├── test_plain_text.py
├── test_markdown_normalizer.py
├── test_publisher.py
├── test_mineru_adapter.py
├── test_docling_adapter.py
├── test_slots.py
├── test_orchestration.py
└── test_recovery.py

backend/tests/integration/structured_extraction/
├── test_worker_pipeline.py
├── test_mineru_real.py
└── test_docling_real.py

scripts/
├── smoke-mineru.ps1
├── smoke-docling.ps1
└── verify-extraction-stack.ps1

docs/runbooks/
└── structured-extraction.md
```

Feature 文件按职责拆分；Celery task 不直接写 HTTP、文件解析或 SQL 细节。

---

### Task 1: 增加 Worker 依赖、配置和持久化字段

**Files:**
- Modify: `backend/pyproject.toml`
- Modify: `uv.lock`
- Modify: `backend/app/core/config.py`
- Modify: `backend/app/features/structured_extraction/models.py`
- Modify: `backend/app/features/structured_extraction/errors.py`
- Modify: `backend/app/features/structured_extraction/schemas.py`
- Create: `backend/app/features/structured_extraction/worker_models.py`
- Create: `backend/app/alembic/versions/20260730_02_add_extraction_worker_state.py`
- Create: `backend/tests/features/structured_extraction/test_worker_config.py`

**Interfaces:**
- Consumes: `ExtractionTask`。
- Produces: `ProcessorName`, `DetectedFormat`, `ExtractionProcessingPhase`, `ExternalTaskState`, `ProcessingContext`, `ExternalTaskSubmission`, `ExternalTaskStatus`, `ProcessorArtifact`。
- Produces worker/profile settings and persisted external/routing fields。

- [ ] **Step 1: 添加依赖**

Run:

```bash
uv add --package app "fsspec>=2026.0" "s3fs>=2026.0" "mistune>=3,<4" "defusedxml>=0.7,<1"
```

继续使用现有 `httpx`，不新增 `requests`。

- [ ] **Step 2: 写配置校验失败测试**

```python
def test_mineru_profile_requires_markdown_without_images() -> None:
    with pytest.raises(ValidationError):
        MinerUProfile(return_md=False, return_images=True, response_format_zip=True)


def test_processor_roots_must_not_overlap(tmp_path: Path) -> None:
    with pytest.raises(ValidationError):
        ExtractionWorkerSettings(staging_root=tmp_path, output_roots=[tmp_path])
```

- [ ] **Step 3: 定义类型化配置**

增加：

```python
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
    start_page_id: int = 0
    end_page_id: int = 99999
    effort: str = "high"


class DoclingProfile(BaseModel):
    to_formats: tuple[Literal["md"], ...] = ("md",)
    image_export_mode: Literal["placeholder"] = "placeholder"
    do_ocr: Literal[False] = False
    table_mode: Literal["fast", "accurate"] = "accurate"
```

并增加 staging、连接超时、poll、deadline、slot、保留期、allowlist 和 processor base URL/API key 字段。

- [ ] **Step 4: 定义 worker 值对象**

```python
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
    task_id: UUID
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
```

- [ ] **Step 5: 扩展 worker 错误码**

在 `errors.py` 增加 `INPUT_NOT_FOUND`、`INPUT_ACCESS_FAILED`、`INPUT_TOO_LARGE`、`UNSUPPORTED_INPUT_FORMAT`、`PROCESSING_FAILED`、`PROCESSING_TIMEOUT`、`PROCESSOR_SUBMISSION_UNCERTAIN`、`INVALID_PROCESSOR_OUTPUT` 和 `OUTPUT_WRITE_FAILED`。内部异常只携带稳定 code、安全 message、是否瞬时错误和可选外部 task ID。

```python
class ExtractionProcessingError(ExtractionDomainError):
    def __init__(
        self,
        code: ExtractionErrorCode,
        message: str,
        *,
        transient: bool = False,
        external_task_id: str | None = None,
    ) -> None:
        super().__init__(code, message, http_status=500)
        self.transient = transient
        self.external_task_id = external_task_id
```

- [ ] **Step 6: 扩展成功响应 schema**

在 `schemas.py` 定义 `ProcessorPublic`、`RoutingPublic`，并让 `ExtractionResultPublic` 增加 `processor`、`routing`、`input_sha256`、`output_sha256`。这些字段只接受已持久化的非敏感元数据，不包含 URL、token、完整 profile 或 staging 路径。

- [ ] **Step 7: 扩展任务持久化**

增加 `detected_format`、`routing_reasons` JSON、`processor_name`、`processor_version`、`profile_name`、`profile_sha256`、`external_task_id`、`next_poll_at`、`processing_deadline`、`poll_lease_expires_at`、`input_sha256`、`output_sha256`。

- [ ] **Step 8: 生成并检查迁移**

Run:

```bash
cd backend
uv run alembic revision --autogenerate --rev-id 20260730_02 -m "add extraction worker state"
uv run alembic upgrade head
uv run alembic downgrade -1
uv run alembic upgrade head
```

检查 `down_revision == "20260730_01"`，且迁移没有修改模板 User/Item 表。

- [ ] **Step 9: 运行配置、类型和迁移测试**

Run:

```bash
cd backend
uv run pytest tests/features/structured_extraction/test_worker_config.py -q
uv run mypy app/features/structured_extraction/worker_models.py app/core/config.py
uv run ruff check app/features/structured_extraction/worker_models.py app/core/config.py
```

- [ ] **Step 10: 提交**

```bash
git add backend/pyproject.toml uv.lock backend/app/core/config.py backend/app/features/structured_extraction/models.py backend/app/features/structured_extraction/errors.py backend/app/features/structured_extraction/schemas.py backend/app/features/structured_extraction/worker_models.py backend/app/alembic/versions backend/tests/features/structured_extraction/test_worker_config.py
git commit -m "功能：配置结构化提取 Worker 状态"
```

---

### Task 2: 实现 Staging 和受控输入解析

**Files:**
- Create: `backend/app/features/structured_extraction/staging.py`
- Create: `backend/app/features/structured_extraction/input_resolver.py`
- Create: `backend/tests/features/structured_extraction/test_staging.py`
- Create: `backend/tests/features/structured_extraction/test_input_resolver.py`

**Interfaces:**
- Produces: `StagingLayout.for_task(root, task_id) -> StagingLayout`。
- Produces: `InputResolver.resolve(task, layout) -> ResolvedInput`。

- [ ] **Step 1: 写 staging 隔离测试**

```python
def test_layout_is_derived_only_from_task_id(tmp_path: Path) -> None:
    layout = StagingLayout.for_task(tmp_path, UUID("018f0000-0000-7000-8000-000000000001"))
    assert layout.root == tmp_path / "018f0000-0000-7000-8000-000000000001"
    assert layout.source.parent == layout.root / "source"
    assert layout.processor_dir == layout.root / "processor"
    assert layout.output == layout.root / "output" / "result.md"
```

- [ ] **Step 2: 写流式输入和优先级测试**

覆盖本地优先、远程不降级、大小上限、HTTP redirect 重新校验、S3 bucket allowlist、摘要计算、中断后不保留伪完整 source。

- [ ] **Step 3: 运行测试并确认失败**

Run:

```bash
cd backend
uv run pytest tests/features/structured_extraction/test_staging.py tests/features/structured_extraction/test_input_resolver.py -q
```

- [ ] **Step 4: 实现 StagingLayout**

所有目录创建使用 `mode=0o700`；临时 source 写入 `.source.{task_id}.part`，完整复制并校验后再同目录 rename。

- [ ] **Step 5: 实现 fsspec InputResolver**

本地使用受控 `file` filesystem，远程按 URL scheme 创建只读 filesystem。调用方 URL 不携带 storage options 或凭据；S3/MinIO credential 只从服务端配置注入。

复制循环：

```python
while chunk := source.read(settings.EXTRACTION_COPY_CHUNK_BYTES):
    total += len(chunk)
    if total > settings.EXTRACTION_MAX_INPUT_BYTES:
        raise ExtractionProcessingError(INPUT_TOO_LARGE)
    digest.update(chunk)
    destination.write(chunk)
```

- [ ] **Step 6: 实现幂等复用和安全清理**

已有 staging source 只有在数据库记录大小和 SHA-256 均匹配时复用。清理函数验证解析后的目录仍位于 staging root 且 task ID 目录完全匹配。

- [ ] **Step 7: 运行测试**

Run:

```bash
cd backend
uv run pytest tests/features/structured_extraction/test_staging.py tests/features/structured_extraction/test_input_resolver.py -q
uv run mypy app/features/structured_extraction/staging.py app/features/structured_extraction/input_resolver.py
```

- [ ] **Step 8: 提交**

```bash
git add backend/app/features/structured_extraction/staging.py backend/app/features/structured_extraction/input_resolver.py backend/tests/features/structured_extraction/test_staging.py backend/tests/features/structured_extraction/test_input_resolver.py
git commit -m "功能：实现提取任务受控输入暂存"
```

---

### Task 3: 实现格式检测、DOCX 预检与确定路由

**Files:**
- Create: `backend/app/features/structured_extraction/format_detector.py`
- Create: `backend/app/features/structured_extraction/office_inspector.py`
- Create: `backend/app/features/structured_extraction/router.py`
- Create: `backend/tests/features/structured_extraction/test_format_detector.py`
- Create: `backend/tests/features/structured_extraction/test_office_inspector.py`
- Create: `backend/tests/features/structured_extraction/test_router.py`
- Create: `backend/tests/fixtures/structured_extraction/`

**Interfaces:**
- Produces: `FormatDetector.detect(path) -> DetectedDocument`。
- Produces: `OfficeDocumentInspector.inspect_docx(path) -> OfficeInspection`。
- Produces: `ProcessorRouter.route(document, inspection) -> RoutingDecision`。

- [ ] **Step 1: 创建最小安全 fixture**

用测试代码生成不含业务内容的 TXT、伪装 PDF、DOCX ZIP、PPTX ZIP、XLSX ZIP 和未知文本 fixture；不要提交真实业务文档。

- [ ] **Step 2: 写格式检测测试**

覆盖 PDF magic、PNG/JPEG、OLE、OOXML `[Content_Types].xml`、EPUB mimetype、HTML、未知文本、NUL 二进制、扩展名与签名冲突。

- [ ] **Step 3: 写 DOCX inspector 测试**

构造包含 `w:drawing`、`wp:anchor`、`w:txbxContent`、`w:cols`、chart、OLE 和 media 的最小 OOXML，断言统计字段和 reasons。

- [ ] **Step 4: 写路由矩阵测试**

```python
@pytest.mark.parametrize(
    ("format", "processor"),
    [
        ("pdf", "mineru"),
        ("png", "mineru"),
        ("ppt", "mineru"),
        ("pptx", "mineru"),
        ("doc", "mineru"),
        ("xls", "docling"),
        ("xlsx", "docling"),
        ("html", "docling"),
        ("epub", "docling"),
        ("json", "plain_text"),
        ("unknown_text", "plain_text"),
    ],
)
```

普通 DOCX 断言 Docling，超过配置阈值断言 MinerU。

- [ ] **Step 5: 运行测试并确认失败**

Run:

```bash
cd backend
uv run pytest tests/features/structured_extraction/test_format_detector.py tests/features/structured_extraction/test_office_inspector.py tests/features/structured_extraction/test_router.py -q
```

- [ ] **Step 6: 实现安全检测**

使用固定上限读取文件头；ZIP 只读取中央目录和受控的小型 XML，限制 entry 数量、单 entry 解压大小和总解压大小，拒绝路径逃逸。XML 通过 `defusedxml` 解析。

- [ ] **Step 7: 实现可配置 DOCX 评分**

每个触发项产生稳定 reason，例如 `image_dominant_document`、`anchored_objects=12`。Router 返回：

```python
@dataclass(frozen=True)
class RoutingDecision:
    processor: ProcessorName
    detected_format: str
    reasons: tuple[str, ...]
```

- [ ] **Step 8: 实现 production allowlist**

格式必须同时满足“有路由规则”和“位于当前配置 allowlist”。`.wps/.et/.dps/ofd` 明确拒绝。

- [ ] **Step 9: 运行测试并提交**

Run:

```bash
cd backend
uv run pytest tests/features/structured_extraction/test_format_detector.py tests/features/structured_extraction/test_office_inspector.py tests/features/structured_extraction/test_router.py -q
uv run mypy app/features/structured_extraction/format_detector.py app/features/structured_extraction/office_inspector.py app/features/structured_extraction/router.py
```

```bash
git add backend/app/features/structured_extraction/format_detector.py backend/app/features/structured_extraction/office_inspector.py backend/app/features/structured_extraction/router.py backend/tests/features/structured_extraction
git commit -m "功能：实现文档格式检测与处理器路由"
```

---

### Task 4: 实现文本直通、Markdown 清理和原子发布

**Files:**
- Create: `backend/app/features/structured_extraction/processors/__init__.py`
- Create: `backend/app/features/structured_extraction/processors/protocol.py`
- Create: `backend/app/features/structured_extraction/processors/plain_text.py`
- Create: `backend/app/features/structured_extraction/processors/markdown_normalizer.py`
- Create: `backend/app/features/structured_extraction/processors/publisher.py`
- Create: `backend/tests/features/structured_extraction/test_plain_text.py`
- Create: `backend/tests/features/structured_extraction/test_markdown_normalizer.py`
- Create: `backend/tests/features/structured_extraction/test_publisher.py`

**Interfaces:**
- Produces: `PlainTextPassThroughProcessor.process(source, destination) -> ProcessorArtifact`。
- Produces: `MarkdownNormalizer.normalize(markdown) -> str`。
- Produces: `AtomicPublisher.prepare()` 与 `publish()`。

- [ ] **Step 1: 写文本直通 golden tests**

覆盖 UTF-8、UTF-8 BOM、GB18030、CRLF/LF、JSON 缩进、XML 字段顺序和解码失败。断言只移除 BOM并统一输出编码，不重新序列化。

- [ ] **Step 2: 写 Markdown 图片清理 golden tests**

覆盖：

```markdown
![系统架构](images/a(1).png "title")
![][ref]
[ref]: images/a.png
<img src="x.png" alt="图示">
[普通链接](https://example.com)
```

断言保留 alt、普通链接、表格、公式和代码块，并移除 Markdown image、HTML img 和 `data:image`。

- [ ] **Step 3: 写发布冲突和恢复测试**

覆盖处理前目标存在、两个线程同时发布、摘要相同恢复成功、摘要不同冲突、临时文件不跨目录。

- [ ] **Step 4: 运行测试并确认失败**

Run:

```bash
cd backend
uv run pytest tests/features/structured_extraction/test_plain_text.py tests/features/structured_extraction/test_markdown_normalizer.py tests/features/structured_extraction/test_publisher.py -q
```

- [ ] **Step 5: 实现文本解码**

按配置编码顺序尝试严格解码，默认 `utf-8-sig` 后 `gb18030`；每次使用 `errors="strict"`。写出使用 `encoding="utf-8", newline=""`。

- [ ] **Step 6: 实现语法感知 normalizer**

使用 Mistune AST/Markdown renderer 自定义 image token；HTML token 使用标准库 `html.parser` 只处理 `<img>`。输出后再次 parse，若仍存在图片节点或 `data:image` 则抛 `INVALID_PROCESSOR_OUTPUT`。

- [ ] **Step 7: 实现禁止覆盖的 publisher**

发布前 repository 保存 `prepared_output_sha256` 与 `processing_phase=publishing`。文件层使用同目录 exclusive create/link/rename 组合，确保目标存在时绝不替换；不要使用会覆盖目标的 `Path.replace()`。

- [ ] **Step 8: 运行测试和提交**

Run:

```bash
cd backend
uv run pytest tests/features/structured_extraction/test_plain_text.py tests/features/structured_extraction/test_markdown_normalizer.py tests/features/structured_extraction/test_publisher.py -q
uv run mypy app/features/structured_extraction/processors
```

```bash
git add backend/app/features/structured_extraction/processors backend/tests/features/structured_extraction/test_plain_text.py backend/tests/features/structured_extraction/test_markdown_normalizer.py backend/tests/features/structured_extraction/test_publisher.py
git commit -m "功能：实现 Markdown 生成与原子发布"
```

---

### Task 5: 实现统一 Adapter 协议与 MinerU HTTP Adapter

**Files:**
- Create: `backend/app/features/structured_extraction/adapters/__init__.py`
- Create: `backend/app/features/structured_extraction/adapters/protocol.py`
- Create: `backend/app/features/structured_extraction/adapters/mineru.py`
- Create: `backend/tests/features/structured_extraction/test_mineru_adapter.py`

**Interfaces:**
- Produces: `ExternalProcessorAdapter`。
- Produces: `MinerUHttpAdapter.submit()`, `get_status()`, `fetch_result()`。

- [ ] **Step 1: 写 MinerU 契约测试**

使用 `httpx.MockTransport` 覆盖：

- `POST /tasks` multipart 字段与配置序列化；
- HTTP 202 `task_id`；
- `queued/pending/processing/running/completed/failed`；
- 未知状态；
- `GET /tasks/{id}/result`；
- `results` 唯一项、不依赖 stem；
- 空 Markdown、错误 JSON、超大结果；
- 上传后 read timeout 映射 `PROCESSOR_SUBMISSION_UNCERTAIN`。

- [ ] **Step 2: 运行测试并确认失败**

Run: `cd backend; uv run pytest tests/features/structured_extraction/test_mineru_adapter.py -q`

- [ ] **Step 3: 定义 adapter protocol**

```python
class ExternalProcessorAdapter(Protocol):
    def submit(self, source: Path, context: ProcessingContext) -> ExternalTaskSubmission: ...
    def get_status(self, external_task_id: str) -> ExternalTaskStatus: ...
    def fetch_result(self, external_task_id: str, destination: Path) -> ProcessorArtifact: ...
```

- [ ] **Step 4: 实现 MinerU profile multipart 编码**

布尔值按当前 MinerU 实例已验证协议编码为小写字符串；profile SHA-256 使用规范化 JSON，不包含 base URL 或 token。

- [ ] **Step 5: 实现状态和结果解析**

所有响应先检查 HTTP 状态、content type、大小和 JSON object。外部错误压缩空白并截断到安全长度，完整响应不进入业务异常。

- [ ] **Step 6: 运行测试和提交**

Run:

```bash
cd backend
uv run pytest tests/features/structured_extraction/test_mineru_adapter.py -q
uv run mypy app/features/structured_extraction/adapters
```

```bash
git add backend/app/features/structured_extraction/adapters backend/tests/features/structured_extraction/test_mineru_adapter.py
git commit -m "功能：接入 MinerU 异步解析服务"
```

---

### Task 6: 部署 Docling API、RQ Worker 与专用 Redis

**Files:**
- Modify: `compose.yml`
- Modify: `compose.override.yml`
- Create: `.env.example`
- Create: `compose.docling.yml`
- Create: `scripts/verify-docling-deployment.ps1`
- Create: `docs/runbooks/structured-extraction.md`

**Interfaces:**
- Produces internal endpoint `http://docling-api:5001`。
- Produces authenticated Docling v1 async API。
- Later tasks consume pinned image and actual OpenAPI contract。

- [ ] **Step 1: 核验官方镜像命令和 digest**

Run:

```powershell
docker pull quay.io/docling-project/docling-serve:v1.21.0
docker image inspect quay.io/docling-project/docling-serve:v1.21.0 --format '{{index .RepoDigests 0}}'
docker run --rm quay.io/docling-project/docling-serve:v1.21.0 docling-serve --help
docker run --rm quay.io/docling-project/docling-serve:v1.21.0 docling-serve rq-worker --help
```

将实际 digest 写入 `.env.example` 的 `DOCLING_IMAGE` 示例并在 compose 使用 `${DOCLING_IMAGE?Variable not set}`。如果该 tag 或命令与真实镜像不一致，停止本任务并依据官方镜像实际 help 修正文档和 compose，不能猜测启动命令。

- [ ] **Step 2: 写 compose 配置**

`compose.docling.yml` 定义：

- `docling-redis`：专用认证、持久化 volume、healthcheck；
- `docling-api`：固定 image、API key、UI 关闭、远程服务关闭、文件/页数/超时限制、模型 cache volume；
- `docling-worker`：同 image、同 profile、依赖 Redis 健康；
- 仅 default 内部网络，不配置 Traefik labels 和生产 host port。

- [ ] **Step 3: 配置 secrets 与本地 override**

`.env.example` 只放变量名和非敏感示例，不放真实 API key。`compose.override.yml` 可为本地调试映射 Docling 5001 端口，但生产 compose 不暴露。

- [ ] **Step 4: 启动并读取真实 OpenAPI**

Run:

```powershell
docker compose -f compose.yml -f compose.docling.yml up -d docling-redis docling-api docling-worker
Invoke-RestMethod -Uri http://localhost:5001/openapi.json -Headers @{"X-API-Key"=$env:DOCLING_SERVE_API_KEY} | ConvertTo-Json -Depth 100 | Set-Content -Encoding utf8 .\tmp-docling-openapi.json
```

若实际版本使用其他 OpenAPI 路径，从 `/docs` 页面引用中读取真实路径。OpenAPI 只作为验证临时产物，不提交生成快照，契约测试提取必要字段。

- [ ] **Step 5: 验证认证、队列和健康**

验证无 key 返回 401/403、正确 key 可访问；检查 RQ worker 已注册和消费。重启 API 后提交任务仍可查询。

- [ ] **Step 6: 编写部署验证脚本**

脚本检查三个容器健康、镜像 digest 一致、API/RQ worker 版本一致、未暴露生产端口、API key 生效和 Redis 不与 Celery URL相同。

- [ ] **Step 7: 记录 runbook**

写明启动、停止、升级、secret 注入、模型 cache、Redis 持久化、OpenAPI 回读、故障检查和禁止使用 `latest`。

- [ ] **Step 8: 提交**

```bash
git add compose.yml compose.override.yml compose.docling.yml .env.example scripts/verify-docling-deployment.ps1 docs/runbooks/structured-extraction.md
git commit -m "部署：增加独立 Docling 解析服务"
```

---

### Task 7: 依据真实实例实现 Docling Adapter

**Files:**
- Create: `backend/app/features/structured_extraction/adapters/docling.py`
- Create: `backend/tests/features/structured_extraction/test_docling_adapter.py`
- Create: `backend/tests/integration/structured_extraction/test_docling_real.py`

**Interfaces:**
- Consumes: Task 6 实例的真实 OpenAPI。
- Produces: `DoclingHttpAdapter.submit()`, `get_status()`, `fetch_result()`。

- [ ] **Step 1: 从 OpenAPI 提取真实契约断言**

测试必须断言存在：

```text
POST /v1/convert/file/async
GET /v1/status/poll/{task_id}
GET /v1/result/{task_id}
```

并固定 multipart 文件字段、profile 参数、task ID、状态和 Markdown 结果实际字段。

- [ ] **Step 2: 写 adapter fake HTTP 测试**

覆盖 pending/started/success/failure、401、404 task、结果过期、空 Markdown、错误 schema、响应过大和 API key header。

- [ ] **Step 3: 运行测试并确认失败**

Run: `cd backend; uv run pytest tests/features/structured_extraction/test_docling_adapter.py -q`

- [ ] **Step 4: 实现 Docling adapter**

只使用 multipart 上传，不向 Docling 传业务 URL。`X-API-Key` 从 secret settings 读取。profile 固定 `to_formats=md`、`image_export_mode=placeholder`、`do_ocr=false`。

- [ ] **Step 5: 编写 real marker 测试**

```python
@pytest.mark.real_integration
def test_docling_async_round_trip(docling_client: DoclingHttpAdapter, sample_docx: Path) -> None:
    submission = docling_client.submit(sample_docx, context)
    status = wait_with_test_deadline(docling_client, submission.external_task_id)
    assert status.state is ExternalTaskState.SUCCEEDED
```

默认测试集通过 `-m "not real_integration"` 排除。

- [ ] **Step 6: 运行 fake 和真实契约测试**

Run:

```bash
cd backend
uv run pytest tests/features/structured_extraction/test_docling_adapter.py -q
uv run pytest -m real_integration tests/integration/structured_extraction/test_docling_real.py -q
```

第二条只有 Docling 实例健康时才执行并记录真实结果。

- [ ] **Step 7: 提交**

```bash
git add backend/app/features/structured_extraction/adapters/docling.py backend/tests/features/structured_extraction/test_docling_adapter.py backend/tests/integration/structured_extraction/test_docling_real.py
git commit -m "功能：接入 Docling 异步解析服务"
```

---

### Task 8: 实现 PostgreSQL Processor Slot

**Files:**
- Create: `backend/app/features/structured_extraction/slots.py`
- Modify: `backend/app/features/structured_extraction/models.py`
- Create: `backend/app/alembic/versions/20260730_03_add_processor_slots.py`
- Create: `backend/tests/features/structured_extraction/test_slots.py`

**Interfaces:**
- Produces: `ProcessorSlot` table。
- Produces: `ProcessorSlotRepository.acquire()`, `refresh()`, `release()`, `quarantine()`, `reap()`。

- [ ] **Step 1: 写并发容量测试**

两个 session 同时争抢 `max_in_flight=1`，断言仅一个成功。覆盖同 task 幂等获取、租约刷新、终态释放和隔离不计为空闲。

- [ ] **Step 2: 运行测试并确认失败**

Run: `cd backend; uv run pytest tests/features/structured_extraction/test_slots.py -q`

- [ ] **Step 3: 定义 Slot 表**

```python
class ProcessorSlot(SQLModel, table=True):
    __tablename__ = "processor_slot"
    id: uuid.UUID = Field(default_factory=uuid.uuid7, primary_key=True)
    processor_name: str = Field(index=True, max_length=32)
    task_id: uuid.UUID = Field(foreign_key="extraction_task.id", unique=True)
    state: str = Field(max_length=16)
    acquired_at: datetime
    lease_expires_at: datetime
    quarantined_at: datetime | None
```

- [ ] **Step 4: 实现事务型 acquire**

使用 PostgreSQL advisory transaction lock 或锁定 processor capacity row，在同一事务内检查占用并插入，禁止 `count -> insert` 无锁竞态。

- [ ] **Step 5: 实现隔离与回收**

任务超时后业务状态转 failed，slot 设为 quarantined；外部终态/不存在时释放；超过 grace 时释放并生成结构化告警事件。

- [ ] **Step 6: 迁移与测试**

Run:

```bash
cd backend
uv run alembic revision --autogenerate --rev-id 20260730_03 -m "add processor slots"
uv run alembic upgrade head
uv run pytest tests/features/structured_extraction/test_slots.py -q
```

检查 `down_revision == "20260730_02"`，并执行一次 downgrade/upgrade 往返。

- [ ] **Step 7: 提交**

```bash
git add backend/app/features/structured_extraction/slots.py backend/app/features/structured_extraction/models.py backend/app/alembic/versions backend/tests/features/structured_extraction/test_slots.py
git commit -m "功能：限制外部解析任务容量"
```

---

### Task 9: 实现 Submit/Poll 编排与 Celery Tasks

**Files:**
- Create: `backend/app/features/structured_extraction/orchestration.py`
- Create: `backend/app/features/structured_extraction/celery_tasks.py`
- Modify: `backend/app/core/celery_app.py`
- Create: `backend/tests/features/structured_extraction/test_orchestration.py`
- Create: `backend/tests/features/structured_extraction/test_recovery.py`

**Interfaces:**
- Produces Celery task names:
  - `structured_extraction.submit`
  - `structured_extraction.poll`
  - `structured_extraction.recover`
- Produces: `ExtractionOrchestrator.submit(task_id)`, `poll(task_id)`, `recover(now)`。

- [ ] **Step 1: 写文本直通编排测试**

断言 queued task 取得 dispatch lease、staging、路由 plain text、转 running、发布、转 succeeded，且不调用外部 adapter 或 processor slot。

- [ ] **Step 2: 写外部任务编排测试**

覆盖：

- 无 slot 时保持 queued/waiting_capacity 并延迟重新 submit；
- 有 slot 时 queued→running、提交、保存 external ID并安排 poll；
- poll processing 只安排下一次，不 sleep；
- poll success 在同一 task 内 fetch、normalize、publish；
- poll failure 释放 slot并失败；
- submission uncertain 不重提；
- 重复消息不重复提交。

- [ ] **Step 3: 写恢复测试**

覆盖 queued 丢消息、running 过期 poll、有效租约不补发、终态不补发、孤儿 slot 对账。

- [ ] **Step 4: 运行测试并确认失败**

Run:

```bash
cd backend
uv run pytest tests/features/structured_extraction/test_orchestration.py tests/features/structured_extraction/test_recovery.py -q
```

- [ ] **Step 5: 实现 application orchestrator**

Celery task 只做 UUID/schema 校验、创建 session、调用 orchestrator。业务逻辑全部位于普通 Python service。

- [ ] **Step 6: 实现延迟调度**

```python
poll_extraction_task.apply_async(
    kwargs={"task_id": str(task.id), "task_type": "structured_extraction", "schema_version": 1},
    countdown=delay_seconds,
)
```

正常 processing 不调用 `self.retry()`；HTTP 瞬时错误才使用有限 Celery retry。

- [ ] **Step 7: 配置 Beat 恢复任务**

`structured_extraction.recover` 使用配置周期触发，扫描限制批次和确定排序，避免每轮加载全部任务。

- [ ] **Step 8: 运行测试和提交**

Run:

```bash
cd backend
uv run pytest tests/features/structured_extraction/test_orchestration.py tests/features/structured_extraction/test_recovery.py -q
uv run mypy app/features/structured_extraction/orchestration.py app/features/structured_extraction/celery_tasks.py
```

```bash
git add backend/app/features/structured_extraction/orchestration.py backend/app/features/structured_extraction/celery_tasks.py backend/app/core/celery_app.py backend/tests/features/structured_extraction/test_orchestration.py backend/tests/features/structured_extraction/test_recovery.py
git commit -m "功能：编排结构化提取后台任务"
```

---

### Task 10: 部署 TextProcessor Redis、Worker 和 Beat

**Files:**
- Modify: `compose.yml`
- Modify: `compose.override.yml`
- Modify: `.env.example`
- Modify: `backend/Dockerfile`
- Create: `scripts/verify-extraction-stack.ps1`

**Interfaces:**
- Produces services `redis`, `worker`, `beat`。
- Consumes `app.core.celery_app:celery_app`。

- [ ] **Step 1: 写 Compose 配置**

增加 TextProcessor 专用 Redis broker、Celery worker 和 Beat。Worker/Beat 复用 backend image、环境和挂载，但使用不同 command：

```text
celery -A app.core.celery_app:celery_app worker --loglevel=INFO
celery -A app.core.celery_app:celery_app beat --loglevel=INFO
```

Redis 与 Docling Redis 使用不同 service、volume 和 URL。

- [ ] **Step 2: 配置依赖与健康检查**

Backend、worker、beat 依赖 db/prestart 和 Redis health。Worker 添加 ping healthcheck；Beat 使用 pidfile/state file 的容器内受控路径。

- [ ] **Step 3: 验证消息只含标识**

启动 stack，提交一条测试任务，通过 Celery event/log断言消息 kwargs 只有 task ID、任务类型和 schema version。

- [ ] **Step 4: 验证 worker 丢失重投**

处理测试任务时终止 worker 容器，重启后确认 `acks_late` 和恢复扫描器不会丢任务或重复发布。

- [ ] **Step 5: 编写 stack 验证脚本**

检查 db、Redis、backend、worker、beat、MinerU health、Docling API/RQ worker/Redis，并分别报告每个失败阶段。

- [ ] **Step 6: 提交**

```bash
git add compose.yml compose.override.yml .env.example backend/Dockerfile scripts/verify-extraction-stack.ps1
git commit -m "部署：增加结构化提取 Worker 运行栈"
```

---

### Task 11: 完成端到端故障、并发与安全测试

**Files:**
- Create: `backend/tests/integration/structured_extraction/test_worker_pipeline.py`
- Modify: `backend/tests/api/routes/test_structured_extraction.py`
- Modify: `backend/tests/conftest.py`

**Interfaces:**
- Consumes: Task 1-10 全部接口。
- Produces: 默认可重复集成验证证据。

- [ ] **Step 1: 测试文本端到端**

POST 本地 TXT，运行 submit task，轮询 GET，断言目标单一 `.md`、UTF-8 无 BOM、正文结构不变、processor 元数据和 SHA-256 完整。

- [ ] **Step 2: 测试竞争与发布恢复**

两个不同任务使用同一 target，断言只有一个成功；模拟发布后数据库更新前崩溃，重投后按摘要恢复成功。

- [ ] **Step 3: 测试重复和丢失消息**

重复 submit/poll 消息不重复上传；删除一次 poll 调度并运行 recover，任务继续推进。

- [ ] **Step 4: 测试路径与 SSRF**

覆盖符号链接逃逸、重定向到 loopback/link-local/metadata、S3 bucket 越权和调用方凭据注入。

- [ ] **Step 5: 测试 slot 与孤儿对账**

并发超过容量时多余任务保持 queued；超时任务 failed 且 slot quarantined；外部终态后释放。

- [ ] **Step 6: 运行默认完整测试**

Run:

```bash
cd backend
uv run ruff format app tests --check
uv run ruff check app tests
uv run mypy app
uv run ty check app
uv run pytest -m "not real_integration" -q
```

Expected: 全部通过；不将 real integration 计入此结果。

- [ ] **Step 7: 提交**

```bash
git add backend/tests/integration/structured_extraction backend/tests/api/routes/test_structured_extraction.py backend/tests/conftest.py
git commit -m "测试：覆盖提取 Worker 故障恢复"
```

---

### Task 12: 执行 MinerU/Docling 真实格式、恢复与容量验收

**Files:**
- Create: `backend/tests/integration/structured_extraction/test_mineru_real.py`
- Modify: `backend/tests/integration/structured_extraction/test_docling_real.py`
- Create: `scripts/smoke-mineru.ps1`
- Create: `scripts/smoke-docling.ps1`
- Modify: `docs/runbooks/structured-extraction.md`

**Interfaces:**
- Produces: production format allowlist、资源基线和真实部署验收证据。

- [ ] **Step 1: 准备授权的脱敏 smoke 样本**

准备 TXT、JSON、XML、CSV、自定义文本、普通 DOCX、复杂 DOCX、DOC、PPT/PPTX、XLS/XLSX、HTML、EPUB、PDF 和扫描图片。真实业务样本必须获得上传授权；不可提交的样本放 Git ignored 路径。

- [ ] **Step 2: 重新验证 MinerU**

执行当前 `/health` 和每个 MinerU 路由格式真实提交。历史 cache/report 不作为当前服务证据。记录 processor version、耗时、成功/失败和输出摘要。

- [ ] **Step 3: 逐格式验证 Docling**

至少真实验证普通 DOCX、XLSX、HTML、EPUB；其他拟启用 Docling 格式逐一验证。单个格式成功不能外推其他格式。

- [ ] **Step 4: 验证 Docling 重启恢复**

分别在任务 pending/started 时重启 API、RQ worker 和专用 Redis，记录任务是否恢复或如何明确失败；验证 Redis 持久化符合 runbook。

- [ ] **Step 5: 验证输出契约**

每个成功任务：

- 只发布一个 `.md`；
- UTF-8 无 BOM；
- 不含图片链接、HTML img 或 Base64；
- 表格、代码、公式和普通链接未被破坏；
- GET 返回 processor/profile/routing/input/output 摘要。

- [ ] **Step 6: 执行容量基线**

逐 processor 测量单任务和并发任务的 CPU、内存、临时磁盘、处理时间和排队时间。以不发生 OOM、超时风暴和队列失控的最大稳定值设置 `maxInFlightTasks` 与 RQ worker 数。

- [ ] **Step 7: 固化 production allowlist**

只有真实通过的格式进入配置。`.wps/.et/.dps/ofd` 保持禁用。任何未验证格式在 runbook 明确标记为不支持，而不是计划完成。

- [ ] **Step 8: 运行最终验证**

Run:

```powershell
.\scripts\verify-extraction-stack.ps1
.\scripts\smoke-mineru.ps1
.\scripts\smoke-docling.ps1
cd backend
uv run pytest -m real_integration tests/integration/structured_extraction -q
```

保留命令、时间、服务版本、镜像 digest、格式矩阵和失败详情。

- [ ] **Step 9: 提交可提交证据**

只提交不含业务正文、token、绝对私有路径和 secret 的摘要：

```bash
git add backend/tests/integration/structured_extraction scripts/smoke-mineru.ps1 scripts/smoke-docling.ps1 docs/runbooks/structured-extraction.md
git commit -m "验证：完成解析服务真实格式验收"
```

---

## Completion Gate

- [ ] 默认快速测试、Ruff、Mypy 和 Ty 全部通过。
- [ ] Celery submit/poll/recover 不在 worker 内 sleep，消息只含任务标识。
- [ ] 文本直通保持正文结构并输出 UTF-8 无 BOM。
- [ ] Router 固定且可解释，DOCX 路由理由持久化。
- [ ] MinerU/Docling adapter 通过 fake contract tests。
- [ ] 目标冲突、发布崩溃和重复消息均有自动化恢复测试。
- [ ] PostgreSQL slot 并发容量、隔离和回收真实验证。
- [ ] Docling API、专用 Redis、RQ worker 使用固定镜像版本/digest 并通过认证。
- [ ] Docling API/RQ worker/Redis 重启恢复已真实执行。
- [ ] MinerU 当前 healthcheck 和真实提交已执行。
- [ ] 每个 production allowlist 格式均有独立真实 smoke 证据。
- [ ] 资源基线支持最终 `maxInFlightTasks` 和 RQ worker 数。
- [ ] 最终只发布 Markdown，不含图片引用或正文数据库字段。
