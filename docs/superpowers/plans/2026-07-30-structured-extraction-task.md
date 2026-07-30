# Structured Extraction Task API Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 实现结构化提取单条异步任务的 POST/GET API、PostgreSQL 权威任务状态、调用方隔离、幂等创建和 Celery 入队边界。

**Architecture:** 在 `app/features/structured_extraction/` 内建立独立 feature，route 只做认证与协议适配，application service 负责编排，repository 封装 SQLModel/PostgreSQL，dispatcher 隔离 Celery。第一份计划只建立可测试的任务提交与查询闭环；processor、外部解析服务和 Celery worker 执行逻辑由配套 worker 计划实现。

**Tech Stack:** Python 3.14、FastAPI、Pydantic v2、SQLModel、PostgreSQL 18、Alembic、Celery 5、Redis broker、pytest、httpx。

## Global Constraints

- 外部 JSON 使用 camelCase；Python 和数据库使用 snake_case。
- PostgreSQL 是任务状态、参数、结果引用与错误摘要的唯一权威来源；Redis 不保存权威业务状态。
- 幂等键固定为 `(caller_id, session_id, file_id)`；相同键参数不一致返回 `IDEMPOTENCY_CONFLICT`。
- POST 与 GET 都使用现有 bearer token 身份认证；无权访问和任务不存在统一返回 404。
- PostgreSQL、日志和响应都不得保存或返回 Markdown 正文。
- `file_storage_path` 与 `file_oss_url` 至少一个非空；同时存在时只选择本地路径，失败不自动降级。
- `target_path` 是输出 allowlist 内、以 `.md` 结尾的完整绝对路径；默认禁止覆盖。
- 状态转换必须通过显式状态机和带当前状态条件的更新完成。
- 没有实际运行的 processor、Redis/Celery、外部服务或真实格式测试不得声称通过。

---

## File Structure

```text
backend/app/features/structured_extraction/
├── __init__.py                 # feature package
├── errors.py                   # 稳定业务错误码与异常
├── models.py                   # SQLModel 表、状态枚举、内部记录
├── schemas.py                  # POST/GET camelCase API schema
├── state_machine.py            # 合法状态转换
├── repository.py               # PostgreSQL 查询、幂等和条件更新
├── dispatcher.py               # Celery 发送边界
├── service.py                  # 创建与查询 application service
└── routes.py                   # FastAPI route

backend/tests/features/structured_extraction/
├── test_schemas.py
├── test_state_machine.py
├── test_repository.py
└── test_service.py

backend/tests/api/routes/test_structured_extraction.py
backend/app/alembic/versions/20260730_01_add_extraction_tasks.py
```

`models.py` 只描述任务持久化，不放 API response；`schemas.py` 不包含数据库操作；`repository.py` 不发送 Celery；`service.py` 不解析文件；`routes.py` 不包含业务分支。

---

### Task 1: 建立 Feature 契约、错误和请求校验

**Files:**
- Create: `backend/app/features/structured_extraction/__init__.py`
- Create: `backend/app/features/structured_extraction/errors.py`
- Create: `backend/app/features/structured_extraction/schemas.py`
- Modify: `backend/app/core/config.py`
- Test: `backend/tests/features/structured_extraction/test_schemas.py`

**Interfaces:**
- Produces: `ExtractionTaskCreate`, `ExtractionTaskAccepted`, `ExtractionTaskPublic`, `ExtractionResultPublic`, `ExtractionErrorPublic`。
- Produces: `ExtractionErrorCode` 和 `ExtractionDomainError`。
- Produces: `settings.EXTRACTION_INPUT_ROOTS`、`settings.EXTRACTION_OUTPUT_ROOTS`、请求字段长度及 URL allowlist 配置。

- [ ] **Step 1: 写请求 schema 的失败测试**

```python
def test_create_requires_one_input() -> None:
    with pytest.raises(ValidationError):
        ExtractionTaskCreate(
            sessionId="s-1",
            fileId="11",
            targetPath="/data/output/1.md",
        )


def test_create_prefers_local_without_dropping_original_fields() -> None:
    request = ExtractionTaskCreate(
        sessionId="s-1",
        fileId="11",
        fileStoragePath="/data/input/1.txt",
        fileOssUrl="http://files.internal/1.txt",
        targetPath="/data/output/1.md",
    )
    assert request.selected_input_type == "local"
    assert request.model_dump(by_alias=True)["fileOssUrl"].endswith("/1.txt")
```

- [ ] **Step 2: 运行 schema 测试并确认失败**

Run: `cd backend; uv run pytest tests/features/structured_extraction/test_schemas.py -q`

Expected: FAIL，feature package 或 schema 尚不存在。

- [ ] **Step 3: 定义稳定错误码和 domain error**

```python
class ExtractionErrorCode(StrEnum):
    INVALID_REQUEST = "INVALID_REQUEST"
    INPUT_PATH_NOT_ALLOWED = "INPUT_PATH_NOT_ALLOWED"
    INPUT_URL_NOT_ALLOWED = "INPUT_URL_NOT_ALLOWED"
    OUTPUT_PATH_NOT_ALLOWED = "OUTPUT_PATH_NOT_ALLOWED"
    IDEMPOTENCY_CONFLICT = "IDEMPOTENCY_CONFLICT"
    QUEUE_SUBMISSION_FAILED = "QUEUE_SUBMISSION_FAILED"
    OUTPUT_CONFLICT = "OUTPUT_CONFLICT"
    INTERNAL_ERROR = "INTERNAL_ERROR"


class ExtractionDomainError(Exception):
    def __init__(
        self,
        code: ExtractionErrorCode,
        message: str,
        *,
        http_status: int,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.safe_message = message
        self.http_status = http_status
```

- [ ] **Step 4: 定义 camelCase API schema**

使用 `ConfigDict(alias_generator=to_camel, populate_by_name=True)`，并实现：

```python
class ExtractionTaskCreate(CamelModel):
    session_id: str = Field(min_length=1, max_length=128)
    file_id: str = Field(min_length=1, max_length=128)
    file_storage_path: str | None = Field(default=None, max_length=2048)
    file_oss_url: str | None = Field(default=None, max_length=4096)
    target_path: str = Field(min_length=1, max_length=2048)

    @model_validator(mode="after")
    def require_input(self) -> Self:
        if not self.file_storage_path and not self.file_oss_url:
            raise ValueError("fileStoragePath 和 fileOssUrl 至少提供一个")
        return self

    @property
    def selected_input_type(self) -> Literal["local", "remote"]:
        return "local" if self.file_storage_path else "remote"
```

响应 schema 明确 `result` 与 `error` 互斥，并让 `ExtractionTaskPublic` 的 model validator 拒绝同时非空。

- [ ] **Step 5: 增加类型化服务端配置**

在 `Settings` 中增加 JSON 可解析的输入根、输出根、HTTP host/CIDR allowlist、字段大小限制和 broker URL；使用 Pydantic validator 将路径解析为绝对规范路径，并拒绝输入根与输出根重叠。

```python
EXTRACTION_INPUT_ROOTS: list[Path] = []
EXTRACTION_OUTPUT_ROOTS: list[Path] = []
EXTRACTION_HTTP_ALLOWED_HOSTS: list[str] = []
EXTRACTION_HTTP_ALLOWED_CIDRS: list[str] = []
EXTRACTION_MAX_INPUT_BYTES: int = 100 * 1024 * 1024
CELERY_BROKER_URL: str = "redis://redis:6379/0"
```

- [ ] **Step 6: 补齐 schema 与配置测试**

覆盖空白字符串、超长 ID、非 `.md` 目标、camelCase dump、`result/error` 互斥和根目录重叠。

- [ ] **Step 7: 运行测试和类型检查**

Run:

```bash
cd backend
uv run pytest tests/features/structured_extraction/test_schemas.py -q
uv run mypy app/features/structured_extraction/errors.py app/features/structured_extraction/schemas.py app/core/config.py
uv run ruff check app/features/structured_extraction app/core/config.py tests/features/structured_extraction/test_schemas.py
```

Expected: 全部通过。

- [ ] **Step 8: 提交**

```bash
git add backend/app/features/structured_extraction backend/app/core/config.py backend/tests/features/structured_extraction/test_schemas.py
git commit -m "功能：定义结构化提取任务契约"
```

---

### Task 2: 建立任务表与 Alembic 迁移

**Files:**
- Create: `backend/app/features/structured_extraction/models.py`
- Modify: `backend/app/models.py`
- Create: `backend/app/alembic/versions/20260730_01_add_extraction_tasks.py`
- Test: `backend/tests/features/structured_extraction/test_repository.py`
- Modify: `backend/tests/conftest.py`

**Interfaces:**
- Consumes: `ExtractionErrorCode`。
- Produces: `ExtractionTaskStatus`, `ExtractionTask`。
- Produces database unique constraint `uq_extraction_task_caller_session_file`。

- [ ] **Step 1: 写模型约束测试**

```python
def test_idempotency_key_is_unique(db: Session, extraction_task_factory: Callable[..., ExtractionTask]) -> None:
    first = extraction_task_factory(caller_id=user_id, session_id="s", file_id="f")
    db.add(first)
    db.commit()
    duplicate = extraction_task_factory(caller_id=user_id, session_id="s", file_id="f")
    db.add(duplicate)
    with pytest.raises(IntegrityError):
        db.commit()
```

- [ ] **Step 2: 运行测试并确认失败**

Run: `cd backend; uv run pytest tests/features/structured_extraction/test_repository.py::test_idempotency_key_is_unique -q`

Expected: FAIL，表模型尚不存在。

- [ ] **Step 3: 定义状态和 SQLModel 表**

```python
class ExtractionTaskStatus(StrEnum):
    PENDING = "pending"
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ExtractionTask(SQLModel, table=True):
    __tablename__ = "extraction_task"
    __table_args__ = (
        UniqueConstraint(
            "caller_id",
            "session_id",
            "file_id",
            name="uq_extraction_task_caller_session_file",
        ),
    )

    id: uuid.UUID = Field(default_factory=uuid.uuid7, primary_key=True)
    caller_id: uuid.UUID = Field(foreign_key="user.id", index=True)
    session_id: str = Field(max_length=128)
    file_id: str = Field(max_length=128)
    request_fingerprint: str = Field(max_length=64)
    file_storage_path: str | None = Field(default=None, max_length=2048)
    file_oss_url: str | None = Field(default=None, max_length=4096)
    selected_input_type: str = Field(max_length=16)
    target_path: str = Field(max_length=2048)
    status: ExtractionTaskStatus = Field(index=True)
    processing_phase: str | None = Field(default=None, max_length=64)
    attempt_count: int = 0
    max_attempts: int = 3
    lease_expires_at: datetime | None = Field(default=None, sa_type=DateTime(timezone=True))
    prepared_output_sha256: str | None = Field(default=None, max_length=64)
    staging_path: str | None = Field(default=None, max_length=2048)
    published_at: datetime | None = Field(default=None, sa_type=DateTime(timezone=True))
    result_metadata: dict[str, object] | None = Field(default=None, sa_type=JSON)
    error_code: str | None = Field(default=None, max_length=64)
    error_message: str | None = Field(default=None, max_length=512)
    created_at: datetime = Field(default_factory=get_datetime_utc, sa_type=DateTime(timezone=True))
    queued_at: datetime | None = Field(default=None, sa_type=DateTime(timezone=True))
    started_at: datetime | None = Field(default=None, sa_type=DateTime(timezone=True))
    finished_at: datetime | None = Field(default=None, sa_type=DateTime(timezone=True))
    updated_at: datetime = Field(default_factory=get_datetime_utc, sa_type=DateTime(timezone=True))
```

使用 PostgreSQL JSON 类型保存非正文结果元数据；不得添加正文列。

- [ ] **Step 4: 注册模型并生成迁移**

在 `app/models.py` 底部显式导入 `ExtractionTask` 以注册 metadata，然后运行：

```bash
cd backend
uv run alembic revision --autogenerate --rev-id 20260730_01 -m "add extraction tasks"
```

检查迁移的 `down_revision` 为当前真实 head `fe56fa70289e`，并且只包含表、外键、唯一约束和索引，不包含无关 schema 变化。

- [ ] **Step 5: 增加测试清理**

在 `tests/conftest.py` 中先删除 `ExtractionTask`，再删除 `User`，避免外键清理失败。

- [ ] **Step 6: 执行迁移和模型测试**

Run:

```bash
cd backend
uv run alembic upgrade head
uv run pytest tests/features/structured_extraction/test_repository.py::test_idempotency_key_is_unique -q
uv run alembic downgrade -1
uv run alembic upgrade head
```

Expected: 正向、回退和再次升级均成功。

- [ ] **Step 7: 提交**

```bash
git add backend/app/features/structured_extraction/models.py backend/app/models.py backend/app/alembic/versions backend/tests/conftest.py backend/tests/features/structured_extraction/test_repository.py
git commit -m "功能：持久化结构化提取任务"
```

---

### Task 3: 实现显式状态机和 Repository

**Files:**
- Create: `backend/app/features/structured_extraction/state_machine.py`
- Create: `backend/app/features/structured_extraction/repository.py`
- Modify: `backend/tests/features/structured_extraction/test_state_machine.py`
- Modify: `backend/tests/features/structured_extraction/test_repository.py`

**Interfaces:**
- Consumes: `ExtractionTask`, `ExtractionTaskStatus`。
- Produces: `assert_transition(current, target) -> None`。
- Produces: `ExtractionTaskRepository.create_or_get()`, `get_for_caller()`, `transition()`。

- [ ] **Step 1: 写合法与非法转换测试**

```python
@pytest.mark.parametrize(
    ("current", "target"),
    [
        (PENDING, QUEUED),
        (PENDING, FAILED),
        (QUEUED, RUNNING),
        (QUEUED, FAILED),
        (RUNNING, SUCCEEDED),
        (RUNNING, FAILED),
        (RUNNING, CANCELLED),
    ],
)
def test_allowed_transitions(current: ExtractionTaskStatus, target: ExtractionTaskStatus) -> None:
    assert_transition(current, target)


def test_terminal_status_cannot_transition() -> None:
    with pytest.raises(InvalidStateTransition):
        assert_transition(SUCCEEDED, RUNNING)
```

- [ ] **Step 2: 运行状态机测试并确认失败**

Run: `cd backend; uv run pytest tests/features/structured_extraction/test_state_machine.py -q`

Expected: FAIL，状态机尚不存在。

- [ ] **Step 3: 实现无副作用状态机**

```python
ALLOWED_TRANSITIONS: Final = {
    ExtractionTaskStatus.PENDING: frozenset({QUEUED, FAILED}),
    ExtractionTaskStatus.QUEUED: frozenset({RUNNING, FAILED, CANCELLED}),
    ExtractionTaskStatus.RUNNING: frozenset({SUCCEEDED, FAILED, CANCELLED}),
    ExtractionTaskStatus.SUCCEEDED: frozenset(),
    ExtractionTaskStatus.FAILED: frozenset(),
    ExtractionTaskStatus.CANCELLED: frozenset(),
}
```

- [ ] **Step 4: 写 Repository 并发幂等测试**

覆盖：

- 首次创建返回 `(task, True)`；
- 参数一致返回原任务 `(task, False)`；
- 参数不同抛出 HTTP 409 domain error；
- 两个数据库 session 并发插入时唯一约束收敛到同一任务；
- `get_for_caller()` 对其他 caller 返回 `None`；
- 条件状态更新失败时不覆盖其他 worker 的状态。

- [ ] **Step 5: 实现规范化 fingerprint**

```python
def request_fingerprint(request: ExtractionTaskCreate) -> str:
    payload = request.model_dump(mode="json", by_alias=False)
    normalized = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()
```

路径先按 API policy 规范化，再参与 fingerprint。

- [ ] **Step 6: 实现 Repository**

`create_or_get()` 使用数据库唯一约束作为最终并发保障；捕获 `IntegrityError` 后 rollback 并重新查询，绝不生成第二个任务。

`transition()` 使用：

```python
update(ExtractionTask)
.where(
    ExtractionTask.id == task_id,
    ExtractionTask.status == expected,
)
.values(status=target, updated_at=now, **fields)
```

并检查 `rowcount == 1`。

- [ ] **Step 7: 运行状态机与 Repository 测试**

Run:

```bash
cd backend
uv run pytest tests/features/structured_extraction/test_state_machine.py tests/features/structured_extraction/test_repository.py -q
uv run mypy app/features/structured_extraction/state_machine.py app/features/structured_extraction/repository.py
```

Expected: 全部通过。

- [ ] **Step 8: 提交**

```bash
git add backend/app/features/structured_extraction/state_machine.py backend/app/features/structured_extraction/repository.py backend/tests/features/structured_extraction
git commit -m "功能：实现提取任务幂等与状态机"
```

---

### Task 4: 实现路径与 URL 请求策略

**Files:**
- Create: `backend/app/features/structured_extraction/request_policy.py`
- Modify: `backend/app/features/structured_extraction/service.py`
- Create: `backend/tests/features/structured_extraction/test_request_policy.py`

**Interfaces:**
- Consumes: `ExtractionTaskCreate`, `Settings`。
- Produces: `ValidatedExtractionRequest`。
- Produces: `validate_request_policy(request, settings) -> ValidatedExtractionRequest`。

- [ ] **Step 1: 写路径逃逸和 URL 安全测试**

```python
def test_output_must_stay_under_allowed_root(policy: RequestPolicy) -> None:
    with pytest.raises(ExtractionDomainError) as raised:
        policy.validate_output_path("/data/output/../private/result.md")
    assert raised.value.code is ExtractionErrorCode.OUTPUT_PATH_NOT_ALLOWED


def test_url_rejects_embedded_credentials(policy: RequestPolicy) -> None:
    with pytest.raises(ExtractionDomainError):
        policy.validate_remote_url("http://user:pass@files.internal/a.txt")
```

覆盖绝对路径、`.md`、符号链接逃逸、loopback、link-local、云元数据地址、未授权 host/CIDR、端口和重定向目标重新校验。

- [ ] **Step 2: 运行策略测试并确认失败**

Run: `cd backend; uv run pytest tests/features/structured_extraction/test_request_policy.py -q`

Expected: FAIL，policy 尚不存在。

- [ ] **Step 3: 实现本地路径规范化**

使用 `Path.resolve(strict=False)` 做词法规范化；已有输入路径必须使用 `resolve(strict=True)` 并检查符号链接后的最终路径。输出父目录使用最近存在父级解析后再拼接文件名，防止不存在目标绕过符号链接检查。

- [ ] **Step 4: 实现 URL policy**

只允许 `http`、`https`；拒绝 userinfo、fragment、非 allowlist host/port。解析所有 DNS 地址并要求每个地址均属于 allowlist CIDR，防止 DNS rebinding 的单次解析绕过。每次重定向重新执行相同检查。

- [ ] **Step 5: 返回不可变规范化请求**

```python
@dataclass(frozen=True)
class ValidatedExtractionRequest:
    session_id: str
    file_id: str
    file_storage_path: str | None
    file_oss_url: str | None
    selected_input_type: Literal["local", "remote"]
    target_path: str
```

- [ ] **Step 6: 运行安全策略测试**

Run:

```bash
cd backend
uv run pytest tests/features/structured_extraction/test_request_policy.py -q
uv run ruff check app/features/structured_extraction/request_policy.py tests/features/structured_extraction/test_request_policy.py
```

Expected: 全部通过。

- [ ] **Step 7: 提交**

```bash
git add backend/app/features/structured_extraction/request_policy.py backend/tests/features/structured_extraction/test_request_policy.py
git commit -m "安全：限制结构化提取输入输出位置"
```

---

### Task 5: 建立 Celery Dispatcher 与任务 Application Service

**Files:**
- Modify: `backend/pyproject.toml`
- Modify: `uv.lock`
- Create: `backend/app/core/celery_app.py`
- Create: `backend/app/features/structured_extraction/dispatcher.py`
- Create: `backend/app/features/structured_extraction/service.py`
- Modify: `backend/tests/features/structured_extraction/test_service.py`

**Interfaces:**
- Consumes: `ValidatedExtractionRequest`, `ExtractionTaskRepository`。
- Produces: `ExtractionTaskDispatcher.enqueue_submit(task_id: UUID) -> None`。
- Produces: `ExtractionTaskService.create_task()` 与 `get_task()`。
- Worker plan consumes Celery task name `structured_extraction.submit` and message kwargs `task_id`, `task_type`, `schema_version` only.

- [ ] **Step 1: 添加 Celery/Redis 依赖**

Run:

```bash
uv add --package app "celery[redis]>=5.6,<6"
```

确认只修改 `backend/pyproject.toml` 与根 `uv.lock` 中相关解析结果。

- [ ] **Step 2: 写 application service 失败测试**

```python
def test_create_enqueues_only_task_identity(service: ExtractionTaskService, dispatcher: FakeDispatcher) -> None:
    accepted = service.create_task(caller_id, request)
    assert dispatcher.calls == [
        {
            "task_id": str(accepted.id),
            "task_type": "structured_extraction",
            "schema_version": 1,
        }
    ]


def test_enqueue_failure_marks_task_failed(service: ExtractionTaskService, dispatcher: FailingDispatcher) -> None:
    with pytest.raises(ExtractionDomainError) as raised:
        service.create_task(caller_id, request)
    assert raised.value.code is ExtractionErrorCode.QUEUE_SUBMISSION_FAILED
    assert repository.get_by_key(caller_id, "s-1", "11").status is FAILED
```

- [ ] **Step 3: 运行 service 测试并确认失败**

Run: `cd backend; uv run pytest tests/features/structured_extraction/test_service.py -q`

Expected: FAIL，dispatcher/service 尚不存在。

- [ ] **Step 4: 创建 Celery app**

```python
celery_app = Celery(
    "text_processor",
    broker=settings.CELERY_BROKER_URL,
)
celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_backend=None,
    task_acks_late=True,
    task_reject_on_worker_lost=True,
)
```

API 不使用 Celery result backend。

- [ ] **Step 5: 实现字符串任务名 dispatcher**

```python
class CeleryExtractionTaskDispatcher:
    def enqueue_submit(self, task_id: uuid.UUID) -> None:
        celery_app.send_task(
            "structured_extraction.submit",
            kwargs={
                "task_id": str(task_id),
                "task_type": "structured_extraction",
                "schema_version": 1,
            },
        )
```

- [ ] **Step 6: 实现 create/get application service**

流程：

1. policy 校验并规范化请求；
2. repository `create_or_get()`；
3. 幂等命中时不重复入队，直接返回原任务；
4. 新任务先提交数据库状态 `pending -> queued`；
5. 再提交 Celery，使 worker 看到任务时数据库已经可执行；
6. 发送失败则 `queued -> failed`，记录脱敏错误并抛出 503 domain error；
7. 进程若在 queued 提交后、发送消息前崩溃，由 worker 计划中的 queued 恢复扫描补发。

不得把 broker 异常文本返回调用方。

- [ ] **Step 7: 运行 service 测试和静态检查**

Run:

```bash
cd backend
uv run pytest tests/features/structured_extraction/test_service.py -q
uv run mypy app/core/celery_app.py app/features/structured_extraction/dispatcher.py app/features/structured_extraction/service.py
uv run ruff check app/core/celery_app.py app/features/structured_extraction
```

Expected: 全部通过。

- [ ] **Step 8: 提交**

```bash
git add backend/pyproject.toml uv.lock backend/app/core/celery_app.py backend/app/features/structured_extraction/dispatcher.py backend/app/features/structured_extraction/service.py backend/tests/features/structured_extraction/test_service.py
git commit -m "功能：接入结构化提取任务队列"
```

---

### Task 6: 实现 POST/GET Route 与错误映射

**Files:**
- Create: `backend/app/features/structured_extraction/routes.py`
- Modify: `backend/app/api/main.py`
- Create: `backend/tests/api/routes/test_structured_extraction.py`

**Interfaces:**
- Consumes: `CurrentUser`, `SessionDep`, `ExtractionTaskService`。
- Produces:
  - `POST /api/v1/structured-extraction/tasks`
  - `GET /api/v1/structured-extraction/tasks/{taskId}`

- [ ] **Step 1: 写 API 契约测试**

覆盖：

```python
def test_create_returns_202_and_camel_case(client: TestClient, normal_user_token_headers: dict[str, str]) -> None:
    response = client.post(
        "/api/v1/structured-extraction/tasks",
        headers=normal_user_token_headers,
        json={
            "sessionId": "session-001",
            "fileId": "11",
            "fileStoragePath": "/allowed/input/1.txt",
            "fileOssUrl": None,
            "targetPath": "/allowed/output/1.md",
        },
    )
    assert response.status_code == 202
    assert set(response.json()) == {"taskId", "sessionId", "fileId", "status"}
```

另覆盖 422、幂等 202 同 task ID、参数冲突 409、入队失败 503、GET 状态快照和无权访问 404。

- [ ] **Step 2: 运行 API 测试并确认失败**

Run: `cd backend; uv run pytest tests/api/routes/test_structured_extraction.py -q`

Expected: FAIL，route 尚未注册。

- [ ] **Step 3: 实现依赖注入和 domain error 映射**

Route 内将 `ExtractionDomainError` 映射为：

```json
{
  "detail": {
    "code": "IDEMPOTENCY_CONFLICT",
    "message": "相同幂等键对应了不同请求参数"
  }
}
```

不得返回 traceback、broker URL、宿主机内部路径或外部响应正文。

- [ ] **Step 4: 实现 POST**

使用 `status_code=status.HTTP_202_ACCEPTED`，调用 `service.create_task(current_user.id, request)`，不读取文件、不检查最终目标是否已存在、不调用 processor。

- [ ] **Step 5: 实现 GET**

查询条件必须同时包含 `task_id` 与 `current_user.id`。成功状态映射 `result_metadata`；失败状态映射稳定 code/message；其他状态 `result/error` 均为 `null`。

- [ ] **Step 6: 注册 router**

在 `app/api/main.py` 增加：

```python
from app.features.structured_extraction import routes as structured_extraction

api_router.include_router(structured_extraction.router)
```

- [ ] **Step 7: 运行 API 测试**

Run:

```bash
cd backend
uv run pytest tests/api/routes/test_structured_extraction.py -q
uv run mypy app/features/structured_extraction/routes.py
uv run ruff check app/features/structured_extraction/routes.py tests/api/routes/test_structured_extraction.py
```

Expected: 全部通过。

- [ ] **Step 8: 提交**

```bash
git add backend/app/features/structured_extraction/routes.py backend/app/api/main.py backend/tests/api/routes/test_structured_extraction.py
git commit -m "功能：提供结构化提取任务接口"
```

---

### Task 7: 完成并发、隔离与响应回归验证

**Files:**
- Modify: `backend/tests/api/routes/test_structured_extraction.py`
- Modify: `backend/tests/features/structured_extraction/test_repository.py`
- Create: `backend/tests/features/structured_extraction/test_response_mapping.py`

**Interfaces:**
- Consumes: Task 1-6 的公开接口。
- Produces: 接口 spec 的完整自动化回归证据。

- [ ] **Step 1: 增加并发幂等集成测试**

使用两个独立 SQLModel session 和 barrier 同时创建相同 `(caller, session, file)`，断言数据库只有一行、两个结果使用同一 task ID、dispatcher 只产生一次首次入队。

- [ ] **Step 2: 增加调用方隔离测试**

创建用户 A 的任务，使用用户 B token 查询，断言 HTTP 404 且响应与随机不存在 task ID 一致。

- [ ] **Step 3: 增加所有状态的 response matrix**

```python
@pytest.mark.parametrize(
    ("status", "has_result", "has_error"),
    [
        ("pending", False, False),
        ("queued", False, False),
        ("running", False, False),
        ("succeeded", True, False),
        ("failed", False, True),
        ("cancelled", False, True),
    ],
)
```

成功结果不得包含 `content` 或 Markdown 正文键。

- [ ] **Step 4: 增加日志脱敏测试**

用 `caplog` 触发入队失败和参数冲突，断言 token、原始正文、完整内部路径和 broker credential 不在日志中；保留 `task_id`、`caller_id` 和错误码。

- [ ] **Step 5: 运行接口计划的完整验证**

Run:

```bash
cd backend
uv run ruff format app tests --check
uv run ruff check app tests
uv run mypy app
uv run ty check app
uv run pytest tests/features/structured_extraction tests/api/routes/test_structured_extraction.py -q
uv run alembic upgrade head
```

Expected: 全部通过；没有真实 worker/processor 测试声明。

- [ ] **Step 6: 检查 OpenAPI**

Run:

```bash
cd backend
uv run python -c "from app.main import app; import json; schema=app.openapi(); print(json.dumps(schema['paths']['/api/v1/structured-extraction/tasks'], ensure_ascii=False))"
```

确认 POST 为 202、字段 camelCase、GET 不含 Markdown 正文。

- [ ] **Step 7: 提交**

```bash
git add backend/tests/features/structured_extraction backend/tests/api/routes/test_structured_extraction.py
git commit -m "测试：覆盖结构化提取接口可靠性"
```

---

## Completion Gate

- [ ] POST 首次创建和一致幂等命中均返回 202。
- [ ] 并发相同幂等键只产生一个任务和一次首次入队。
- [ ] 同键不同参数返回 409 `IDEMPOTENCY_CONFLICT`。
- [ ] 入队失败任务落为 failed，POST 返回 503。
- [ ] GET 使用 caller 隔离，无权与不存在统一 404。
- [ ] 所有状态的 `result/error` 互斥。
- [ ] 数据库、响应和日志不包含 Markdown 正文。
- [ ] 迁移 upgrade/downgrade 已真实运行。
- [ ] Ruff、Mypy、Ty 和目标 pytest 全部通过。
- [ ] Worker 与真实 processor 能力只在第二份计划完成后声明。
