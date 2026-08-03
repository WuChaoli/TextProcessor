# Markdown 组合清洗 API Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 实现 Markdown 组合清洗异步任务的 POST/GET 接口、PostgreSQL 幂等任务记录和最小 Celery 投递，为后续本地 Worker Processor 提供稳定边界。

**Architecture:** 新能力位于独立 `app.features.markdown_cleaning` package。FastAPI route 只做鉴权和响应映射，service 编排幂等创建与入队，repository 以 PostgreSQL advisory lock 和唯一约束收敛并发；dispatcher 只向 `markdown_cleaning.execute` 发送最小消息，本阶段不实现文件读取或清洗算法。

**Tech Stack:** Python 3.14, FastAPI, Pydantic v2, SQLModel, SQLAlchemy 2, PostgreSQL, Alembic, Celery 5.6, Redis, pytest, Ruff, Mypy, Pyright, Ty.

## Global Constraints

- 外部 API 固定为 `POST /api/v1/markdown-cleaning/tasks` 与 `GET /api/v1/markdown-cleaning/tasks/{taskId}`。
- 请求字段固定为 `sessionId`、`fileId`、`fileStoragePath`、`fileOssUrl`、`targetPath`；调用方不能传入 processor 版本、处理步骤、规则或掩码。
- `fileStoragePath` 与 `fileOssUrl` 至少提供一个；同时存在时本地路径优先，后续 Worker 读取失败时不得降级。
- 输入和输出后缀只允许 `.md`、`.markdown`；本地输入与目标路径不得指向同一文件。
- 幂等键固定为 `(callerId, sessionId, fileId)`，参数冲突返回 `409 IDEMPOTENCY_CONFLICT`。
- PostgreSQL 是任务状态权威来源；Celery 消息只含 `taskId`、`taskType`、`schemaVersion`。
- API 不读取文档、不运行 processor、不返回正文、敏感值、匹配位置或宿主机 staging 路径。
- 本阶段不添加 `markdown-it-py`、Presidio、`mdformat-gfm`，不实现 Worker、Beat、staging 或发布逻辑。

---

## File Structure

```text
backend/app/features/markdown_cleaning/
├── __init__.py          # feature package
├── api_errors.py        # 稳定 API/domain 错误
├── dispatcher.py        # 最小 Celery 消息投递
├── messages.py          # 消息 schema version 1
├── repository.py        # 幂等、查询和条件状态转换
├── request_policy.py    # 输入/输出地址规范化与 allowlist
├── routes.py            # POST/GET 与 public response mapping
├── schemas.py           # camelCase 请求和响应 schema
├── service.py           # 创建、幂等命中、入队失败编排
├── state_machine.py     # 状态枚举和合法转换
└── task_models.py       # SQLModel 权威任务记录

backend/app/alembic/versions/20260803_01_add_markdown_cleaning_tasks.py
backend/tests/features/markdown_cleaning/
├── __init__.py
├── test_api_contract.py
├── test_messages.py
├── test_repository.py
├── test_request_policy.py
├── test_service.py
└── test_state_machine.py
backend/tests/api/routes/test_markdown_cleaning.py
```

---

### Task 1: 请求响应契约、状态机与稳定错误

**Files:**
- Create: `backend/app/features/markdown_cleaning/__init__.py`
- Create: `backend/app/features/markdown_cleaning/schemas.py`
- Create: `backend/app/features/markdown_cleaning/state_machine.py`
- Create: `backend/app/features/markdown_cleaning/api_errors.py`
- Create: `backend/tests/features/markdown_cleaning/__init__.py`
- Create: `backend/tests/features/markdown_cleaning/test_api_contract.py`
- Create: `backend/tests/features/markdown_cleaning/test_state_machine.py`

**Interfaces:**
- Produces: `MarkdownCleaningTaskStatus`, `MarkdownCleaningTaskCreate`, `MarkdownCleaningTaskAccepted`, `MarkdownCleaningTaskPublic`, `MarkdownCleaningSummaryPublic`, `MarkdownCleaningDomainError`.
- Consumes: only Pydantic and standard-library types; no database or Celery dependency.

- [ ] **Step 1: Write failing schema and state-machine tests**

Create tests covering camelCase mapping, `extra="forbid"`, blank values, two-input rule, Markdown suffix, result/error exclusivity, safe summary shape and legal transitions:

```python
def test_create_schema_selects_local_input_and_rejects_unknown_fields() -> None:
    request = MarkdownCleaningTaskCreate.model_validate({
        "sessionId": " session-1 ",
        "fileId": " 11 ",
        "fileStoragePath": " C:/input/source.md ",
        "fileOssUrl": "https://files.internal/source.md",
        "targetPath": " C:/output/result.md ",
    })
    assert request.session_id == "session-1"
    assert request.file_id == "11"
    assert request.selected_input_type == "local"
    with pytest.raises(ValidationError):
        MarkdownCleaningTaskCreate.model_validate({
            **request.model_dump(by_alias=True),
            "processor": "caller-controlled",
        })


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (MarkdownCleaningTaskStatus.PENDING, MarkdownCleaningTaskStatus.QUEUED),
        (MarkdownCleaningTaskStatus.QUEUED, MarkdownCleaningTaskStatus.RUNNING),
        (MarkdownCleaningTaskStatus.RUNNING, MarkdownCleaningTaskStatus.SUCCEEDED),
        (MarkdownCleaningTaskStatus.RUNNING, MarkdownCleaningTaskStatus.FAILED),
        (MarkdownCleaningTaskStatus.RUNNING, MarkdownCleaningTaskStatus.CANCELLED),
    ],
)
def test_legal_transitions(current, target) -> None:
    assert_transition(current, target)
```

- [ ] **Step 2: Run focused tests and verify import failures**

Run:

```powershell
uv run --project backend pytest backend/tests/features/markdown_cleaning/test_api_contract.py backend/tests/features/markdown_cleaning/test_state_machine.py -q
```

Expected: collection fails because `app.features.markdown_cleaning.schemas` and `state_machine` do not exist.

- [ ] **Step 3: Implement schemas, errors and state machine**

Implement these exact public structures:

```python
class MarkdownCleaningTaskStatus(StrEnum):
    PENDING = "pending"
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class MarkdownCleaningTaskCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    session_id: NonBlank = Field(alias="sessionId", max_length=128)
    file_id: NonBlank = Field(alias="fileId", max_length=255)
    file_storage_path: NonBlank | None = Field(
        default=None, alias="fileStoragePath", max_length=4096
    )
    file_oss_url: NonBlank | None = Field(
        default=None, alias="fileOssUrl", max_length=4096
    )
    target_path: NonBlank = Field(alias="targetPath", max_length=4096)


class MarkdownCleaningSummaryPublic(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    duplicate_paragraphs_removed: int = Field(
        alias="duplicateParagraphsRemoved", ge=0
    )
    redactions: MarkdownCleaningRedactionsPublic
    formatting_changes: int = Field(alias="formattingChanges", ge=0)
```

Expose a computed `selected_input_type: Literal["local", "remote"]`; it returns `local` when both are present. Validate both input and target suffix case-insensitively. Define `MarkdownCleaningApiErrorCode` values used by this phase: `IDEMPOTENCY_CONFLICT`, `INPUT_PATH_NOT_ALLOWED`, `INPUT_URL_NOT_ALLOWED`, `OUTPUT_PATH_NOT_ALLOWED`, `QUEUE_SUBMISSION_FAILED`, `TASK_NOT_FOUND`.

- [ ] **Step 4: Run focused tests and static checks**

Run:

```powershell
uv run --project backend pytest backend/tests/features/markdown_cleaning/test_api_contract.py backend/tests/features/markdown_cleaning/test_state_machine.py -q
uv run --project backend ruff check backend/app/features/markdown_cleaning backend/tests/features/markdown_cleaning
uv run --project backend mypy backend/app/features/markdown_cleaning
```

Expected: all pass.

- [ ] **Step 5: Commit Task 1**

```powershell
git add backend/app/features/markdown_cleaning backend/tests/features/markdown_cleaning
git commit -m "功能：定义Markdown清洗接口契约"
```

---

### Task 2: 地址策略与规范化指纹

**Files:**
- Create: `backend/app/features/markdown_cleaning/request_policy.py`
- Create: `backend/tests/features/markdown_cleaning/test_request_policy.py`
- Modify: `backend/app/core/config.py`

**Interfaces:**
- Consumes: `MarkdownCleaningTaskCreate`, `MarkdownCleaningDomainError`.
- Produces: `ValidatedMarkdownCleaningRequest(session_id, file_id, file_storage_path, file_oss_url, selected_input_type, target_path)` and `MarkdownCleaningRequestPolicy.validate_request()`.

- [ ] **Step 1: Write failing policy tests**

Cover:

- local input inside/outside allowlist;
- `.md` and `.markdown` case-insensitive suffixes;
- symlink/junction escape;
- controlled `http(s)` host, CIDR, port, credentials, fragment and DNS rebinding protection;
- both inputs selecting local without probing remote;
- remote-only normalization;
- absolute target inside output root;
- existing target accepted by API policy because conflict is a Worker concern;
- input and target resolving to the same file rejected;
- relative path and non-Markdown path rejected.

Representative test:

```python
def test_policy_prefers_local_and_does_not_resolve_remote(
    tmp_path: Path,
) -> None:
    source = tmp_path / "input" / "source.md"
    source.parent.mkdir()
    source.write_text("text", encoding="utf-8")
    output = tmp_path / "output"
    output.mkdir()
    resolver = Mock(side_effect=AssertionError("remote must not be resolved"))
    policy = MarkdownCleaningRequestPolicy(
        input_roots=(source.parent,),
        output_roots=(output,),
        allowed_http_hosts=("files.internal",),
        allowed_http_cidrs=("10.0.0.0/8",),
        resolver=resolver,
    )
    validated = policy.validate_request(MarkdownCleaningTaskCreate(
        sessionId="s1", fileId="11",
        fileStoragePath=str(source),
        fileOssUrl="https://files.internal/source.md",
        targetPath=str(output / "result.md"),
    ))
    assert validated.selected_input_type == "local"
    resolver.assert_not_called()
```

- [ ] **Step 2: Run tests and verify failure**

```powershell
uv run --project backend pytest backend/tests/features/markdown_cleaning/test_request_policy.py -q
```

Expected: FAIL because the policy is not implemented.

- [ ] **Step 3: Implement policy and settings**

Add settings with empty/deny-by-default production-safe defaults:

```text
MARKDOWN_CLEANING_INPUT_ROOTS
MARKDOWN_CLEANING_OUTPUT_ROOTS
MARKDOWN_CLEANING_HTTP_ALLOWED_HOSTS
MARKDOWN_CLEANING_HTTP_ALLOWED_CIDRS
```

Reuse the established URL security behavior without importing the global-deduplication feature. Keep a feature-local implementation or extract only a genuinely generic URI policy in a separately reviewed change; do not create cross-feature imports.

- [ ] **Step 4: Run policy and config tests**

```powershell
uv run --project backend pytest backend/tests/features/markdown_cleaning/test_request_policy.py backend/tests/features/global_deduplication/test_config.py backend/tests/features/structured_extraction/test_worker_config.py -q
uv run --project backend ruff check backend/app/core/config.py backend/app/features/markdown_cleaning/request_policy.py backend/tests/features/markdown_cleaning/test_request_policy.py
```

Expected: all pass; existing settings remain compatible.

- [ ] **Step 5: Commit Task 2**

```powershell
git add backend/app/core/config.py backend/app/features/markdown_cleaning/request_policy.py backend/tests/features/markdown_cleaning/test_request_policy.py
git commit -m "功能：限制Markdown清洗输入输出路径"
```

---

### Task 3: PostgreSQL 任务模型、迁移与 repository 幂等

**Files:**
- Create: `backend/app/features/markdown_cleaning/task_models.py`
- Create: `backend/app/features/markdown_cleaning/repository.py`
- Create: `backend/app/alembic/versions/20260803_01_add_markdown_cleaning_tasks.py`
- Create: `backend/tests/features/markdown_cleaning/test_repository.py`
- Modify: `backend/app/models.py`

**Interfaces:**
- Consumes: `ValidatedMarkdownCleaningRequest`, `MarkdownCleaningTaskStatus`.
- Produces: `MarkdownCleaningTask`, `MarkdownCleaningTaskRepository.create_or_get()`, `get_for_caller()`, `transition()`, `mark_dispatched()`.

- [ ] **Step 1: Write failing repository tests against PostgreSQL when available**

Cover create, consistent replay, changed path conflict, key scope including `fileId`, caller isolation, concurrent unique constraint convergence, conditional transition, queue-failure terminal state and summary fields remaining null before Worker completion.

```python
def test_idempotency_key_includes_file_id(session: Session) -> None:
    repo = MarkdownCleaningTaskRepository(session)
    first, first_created = repo.create_or_get(
        caller_id=CALLER_ID, session_id="batch-1", file_id="11",
        file_storage_path="C:/input/1.md", file_oss_url=None,
        selected_input_type="local", target_path="C:/output/1.md",
    )
    second, second_created = repo.create_or_get(
        caller_id=CALLER_ID, session_id="batch-1", file_id="12",
        file_storage_path="C:/input/2.md", file_oss_url=None,
        selected_input_type="local", target_path="C:/output/2.md",
    )
    assert first_created and second_created
    assert first.id != second.id
```

- [ ] **Step 2: Run repository tests and verify failure**

```powershell
uv run --project backend pytest backend/tests/features/markdown_cleaning/test_repository.py -q
```

Expected: FAIL because model, migration and repository do not exist.

- [ ] **Step 3: Implement the complete task model**

Define database fields required by both API and later Worker:

```text
id, caller_id, session_id, file_id, request_fingerprint,
file_storage_path, file_oss_url, selected_input_type, target_path,
processor_contract_version, status, processing_phase, progress_percent,
attempt_count, max_attempts, lease_token, lease_expires_at,
staging_path, input_sha256, prepared_output_sha256, output_sha256,
published_at, duplicate_paragraphs_removed,
phone_redaction_count, id_card_redaction_count,
bank_card_redaction_count, email_redaction_count, ipv4_redaction_count,
formatting_change_count, error_code, error_message,
created_at, queued_at, last_dispatched_at, started_at,
finished_at, updated_at
```

Use UUIDv7 IDs and a unique constraint named `uq_markdown_cleaning_caller_session_file` on `(caller_id, session_id, file_id)`. Set `processor_contract_version="markdown_cleaning_v1"` and `max_attempts=3` server-side.

- [ ] **Step 4: Implement migration and repository**

Migration revision is `20260803_01`, down revision `20260731_01`. Add the table, foreign key to `user.id`, unique constraint and indexes for caller, status, lease, queued time and last dispatch.

Fingerprint exactly these normalized values:

```python
payload = {
    "file_storage_path": file_storage_path,
    "file_oss_url": file_oss_url,
    "selected_input_type": selected_input_type,
    "target_path": target_path,
}
```

Repository advisory lock key includes `caller_id + session_id + file_id`; do not reuse the global-deduplication two-field key.

- [ ] **Step 5: Run migration and repository verification**

```powershell
Push-Location backend
try {
    uv run alembic heads
    uv run alembic upgrade head
} finally {
    Pop-Location
}
uv run --project backend pytest backend/tests/features/markdown_cleaning/test_repository.py -q
uv run --project backend ruff check backend/app/features/markdown_cleaning backend/app/alembic/versions/20260803_01_add_markdown_cleaning_tasks.py backend/tests/features/markdown_cleaning
uv run --project backend mypy backend/app/features/markdown_cleaning
```

Expected: one Alembic head, migration succeeds, repository tests and checks pass.

- [ ] **Step 6: Commit Task 3**

```powershell
git add backend/app/models.py backend/app/alembic/versions/20260803_01_add_markdown_cleaning_tasks.py backend/app/features/markdown_cleaning/task_models.py backend/app/features/markdown_cleaning/repository.py backend/tests/features/markdown_cleaning/test_repository.py
git commit -m "功能：持久化Markdown清洗任务"
```

---

### Task 4: 最小消息、dispatcher 与创建 service

**Files:**
- Create: `backend/app/features/markdown_cleaning/messages.py`
- Create: `backend/app/features/markdown_cleaning/dispatcher.py`
- Create: `backend/app/features/markdown_cleaning/service.py`
- Create: `backend/tests/features/markdown_cleaning/test_messages.py`
- Create: `backend/tests/features/markdown_cleaning/test_service.py`

**Interfaces:**
- Consumes: repository, validated request and `celery_app.send_task()`.
- Produces: `MarkdownCleaningMessage`, `CeleryMarkdownCleaningTaskDispatcher.enqueue_execute()`, `MarkdownCleaningTaskService.create_task()` and `get_task()`.

- [ ] **Step 1: Write failing message and service tests**

Assert the serialized message is exactly:

```json
{
  "taskId": "019fb000-0000-7000-8000-000000000001",
  "taskType": "markdown_cleaning",
  "schemaVersion": 1
}
```

Service tests must cover one dispatch on new task, no dispatch on consistent replay, 409 conflict, `pending -> queued`, dispatch timestamp, queue exception mapped to failed + 503, broker exception text not persisted, and replay of a queue-failed task returning the same safe 503.

- [ ] **Step 2: Run tests and verify failure**

```powershell
uv run --project backend pytest backend/tests/features/markdown_cleaning/test_messages.py backend/tests/features/markdown_cleaning/test_service.py -q
```

Expected: FAIL because messages, dispatcher and service do not exist.

- [ ] **Step 3: Implement minimal message and dispatcher**

Use a protocol:

```python
class MarkdownCleaningTaskDispatcher(Protocol):
    def enqueue_execute(self, task_id: uuid.UUID) -> None: ...
```

The concrete dispatcher calls:

```python
celery_app.send_task(
    "markdown_cleaning.execute",
    kwargs=MarkdownCleaningMessage(
        taskId=task_id,
        taskType="markdown_cleaning",
        schemaVersion=1,
    ).as_payload(),
)
```

Do not add a placeholder `celery_tasks.py`, do not add the task to Celery `include`, and do not claim a worker can consume it in this phase. Worker implementation will register the named task before end-to-end execution.

- [ ] **Step 4: Implement service transaction behavior**

Within the feature-specific advisory lock:

1. validate request;
2. create-or-get;
3. return consistent existing task without dispatch;
4. transition new task `pending -> queued` with `processing_phase="validating_input"`;
5. dispatch minimal message;
6. record `last_dispatched_at` best-effort;
7. on dispatch exception, store only `QUEUE_SUBMISSION_FAILED / 任务提交失败`, transition to failed and raise safe 503.

- [ ] **Step 5: Run focused tests and static checks**

```powershell
uv run --project backend pytest backend/tests/features/markdown_cleaning/test_messages.py backend/tests/features/markdown_cleaning/test_service.py -q
uv run --project backend ruff check backend/app/features/markdown_cleaning backend/tests/features/markdown_cleaning
uv run --project backend mypy backend/app/features/markdown_cleaning
```

Expected: all pass.

- [ ] **Step 6: Commit Task 4**

```powershell
git add backend/app/features/markdown_cleaning backend/tests/features/markdown_cleaning
git commit -m "功能：投递Markdown清洗任务"
```

---

### Task 5: FastAPI POST/GET 与路由注册

**Files:**
- Create: `backend/app/features/markdown_cleaning/routes.py`
- Create: `backend/tests/api/routes/test_markdown_cleaning.py`
- Modify: `backend/app/api/main.py`

**Interfaces:**
- Consumes: service, schemas, authenticated `CurrentUser`, `SessionDep`, policy and dispatcher dependencies.
- Produces: authenticated POST/GET endpoints and `task_to_public()` response mapping.

- [ ] **Step 1: Write failing route tests**

Build a TestClient fixture with temporary input/output roots, SQLite session and dependency-overridden recording dispatcher. Cover:

- POST returns 202 and exactly `taskId/sessionId/fileId/status`;
- consistent POST returns same task and dispatches once;
- changed source or target under same key returns 409;
- empty/unknown fields return 422;
- policy rejection returns safe error;
- queue failure returns 503 without broker credential leakage;
- GET queued/running has null result/error;
- GET succeeded maps the four business fields and safe summary counts;
- GET failed maps safe error only;
- other caller and missing task return identical 404 payload;
- unauthenticated POST/GET retain existing authentication behavior.

```python
def test_create_returns_202_and_dispatches_once(api_context) -> None:
    client, dispatcher, payload = api_context
    first = client.post("/api/v1/markdown-cleaning/tasks", json=payload)
    second = client.post("/api/v1/markdown-cleaning/tasks", json=payload)
    assert first.status_code == second.status_code == 202
    assert first.json() == second.json()
    assert set(first.json()) == {"taskId", "sessionId", "fileId", "status"}
    assert len(dispatcher.task_ids) == 1
```

- [ ] **Step 2: Run route tests and verify 404/import failure**

```powershell
uv run --project backend pytest backend/tests/api/routes/test_markdown_cleaning.py -q
```

Expected: FAIL because the router is not registered.

- [ ] **Step 3: Implement routes and public mapping**

Use:

```python
router = APIRouter(
    prefix="/markdown-cleaning/tasks",
    tags=["markdown-cleaning"],
)
```

The success result must map stored values only:

```python
MarkdownCleaningResultPublic(
    fileId=task.file_id,
    fileStoragePath=task.file_storage_path,
    fileOssUrl=task.file_oss_url,
    targetPath=task.target_path,
    summary=MarkdownCleaningSummaryPublic(...),
)
```

Only build result for `succeeded`; only build error for `failed/cancelled` with both safe fields present. GET must query by `task_id + caller_id` and use the same `TASK_NOT_FOUND` response for missing and unauthorized records.

- [ ] **Step 4: Register the router**

Modify `backend/app/api/main.py` to import and include `markdown_cleaning.router`. Do not modify frontend generated clients in this phase.

- [ ] **Step 5: Run API and feature tests**

```powershell
uv run --project backend pytest backend/tests/api/routes/test_markdown_cleaning.py backend/tests/features/markdown_cleaning -q
uv run --project backend ruff check backend/app/api/main.py backend/app/features/markdown_cleaning backend/tests/api/routes/test_markdown_cleaning.py backend/tests/features/markdown_cleaning
uv run --project backend mypy backend/app/features/markdown_cleaning
```

Expected: all pass.

- [ ] **Step 6: Commit Task 5**

```powershell
git add backend/app/api/main.py backend/app/features/markdown_cleaning/routes.py backend/tests/api/routes/test_markdown_cleaning.py
git commit -m "功能：提供Markdown清洗任务接口"
```

---

### Task 6: 接口层回归、OpenAPI 与边界验收

**Files:**
- Modify only if verification exposes a defect in Task 1–5 files.

**Interfaces:**
- Consumes: completed API feature.
- Produces: evidence that API phase is complete without claiming Worker processing.

- [ ] **Step 1: Run migration integrity checks**

```powershell
Push-Location backend
try {
    uv run alembic heads
    uv run alembic upgrade head
    uv run alembic downgrade 20260731_01
    uv run alembic upgrade head
} finally {
    Pop-Location
}
```

Expected: exactly one head; upgrade, downgrade and re-upgrade succeed without losing unrelated tables.

- [ ] **Step 2: Run backend quality gates**

```powershell
uv run --project backend pytest backend/tests -q
uv run --project backend ruff check backend/app backend/tests
uv run --project backend mypy backend/app
uv run --project backend pyright backend/app backend/tests
uv run --project backend ty check backend/app backend/tests
```

Expected: all pass. If environment-specific real integration markers are excluded by default, report that boundary explicitly.

- [ ] **Step 3: Verify OpenAPI contract**

Use TestClient to inspect `/api/v1/openapi.json` and assert both paths, 202 POST response, authenticated operations, camelCase request fields, and absence of processor configuration fields.

```python
schema = client.get("/api/v1/openapi.json").json()
assert "/api/v1/markdown-cleaning/tasks" in schema["paths"]
assert "/api/v1/markdown-cleaning/tasks/{task_id}" in schema["paths"]
```

- [ ] **Step 4: Verify the intentional Worker boundary**

Assert dispatcher tests prove only the named Celery message. Do not start a worker or poll for success because `markdown_cleaning.execute` is intentionally implemented in the next plan. Record the interface phase as complete only when POST/GET, persistence, idempotency, dispatch and safe errors pass.

- [ ] **Step 5: Review Git scope and commit verification fixes**

```powershell
git status --short
git diff --check
git diff --stat
```

If verification required fixes, stage only Markdown Cleaning API files and commit:

```powershell
git commit -m "测试：验证Markdown清洗接口层"
```

If no files changed, do not create an empty commit.

## Completion Evidence

The API phase is complete only when:

- POST/GET schemas and authentication behavior match the approved spec;
- PostgreSQL migration has a single valid head and round-trips;
- concurrent idempotency is proven with the three-field key;
- queue failure converges to a safe failed task and 503;
- Celery payload contains only the three allowed fields;
- caller isolation and result/error mutual exclusion are tested;
- the full default backend test and static-analysis gates pass;
- no file content is read and no cleaning dependency or fake Worker implementation is added.
