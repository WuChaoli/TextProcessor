# Data-Juicer 独立处理服务 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** 实现可从源码启动的独立 Data-Juicer 异步处理服务，完成 `text_exact_minhash_v1`、POST/GET job、Celery 恢复、聚类结果发布以及自动化与真实文本数据测试。

**Architecture:** 服务位于 `services/datajuicer_service/`，使用独立 Python 3.11 环境、FastAPI、SQLAlchemy、Alembic、Celery、PostgreSQL 和 Redis。Data-Juicer v1.5.4 以固定 Git submodule 提供 MinHash 基础能力；wrapper 自己保留精准重复和 MinHash cluster 的完整成员关系，不调用上游最终过滤输出。

**Tech Stack:** Python 3.11, uv, FastAPI, Pydantic v2, SQLAlchemy 2, Alembic, psycopg 3, Celery 5.6, Redis 7, Data-Juicer v1.5.4, SciPy, pytest, Ruff, Mypy, Ty, httpx.

## Global Constraints

- 默认使用简体中文文档和错误信息；Python 标识符、API 字段与文件名使用 English。
- Data-Juicer 固定为 tag `v1.5.4`、commit `7061da6ad06287aa0305eda162429b34361a56a3`，不得跟随 `main`。
- 服务 Python 固定为 `3.11`，独立于 backend 的 Python 3.14 环境。
- 首版不安装 `.[all]`、`.[dist]`、`.[tools]`、Ray、GPU 或多模态依赖。
- 首版唯一 profile 为 `text_exact_minhash_v1`，调用方不能传递 operator 或 profile 参数。
- 首版只从源码启动，不实现 Dockerfile 或 Docker 服务。
- PostgreSQL 和 Redis 可复用物理实例，但使用独立 database/user、queue 与 key namespace。
- Data-Juicer Service 不保存或记录文本正文，不修改上游 submodule 文件。
- 所有 job、Celery 重试和恢复沿用原 `jobId`；所有 POST 幂等由 `requestId` 唯一约束保证。
- 每个输出必须包含全部输入 uid，每个非空 cluster 严格一个 representative。
- 每个任务必须遵循 TDD：先写失败测试，再实现，再运行范围匹配的门禁并提交。

---

### Task 1: 独立 package、源码 pin 与配置骨架

**Files:**
- Create: `.gitmodules`
- Create: `services/datajuicer_service/pyproject.toml`
- Create: `services/datajuicer_service/.python-version`
- Create: `services/datajuicer_service/datajuicer_service/__init__.py`
- Create: `services/datajuicer_service/datajuicer_service/core/config.py`
- Create: `services/datajuicer_service/tests/test_config.py`
- Create: `services/datajuicer_service/README.md`
- Add submodule: `services/datajuicer_service/vendor/data-juicer`

**Interfaces:**
- Produces: `Settings` with `database_url`, `celery_broker_url`, `celery_queue`, `job_timeout_seconds`, `max_attempts`, `recovery_interval_seconds`, `worker_concurrency`, `profile_np`.
- Produces: package commands `datajuicer-api`, `datajuicer-worker`, `datajuicer-beat`.

- [x] **Step 1: Write configuration tests**

```python
from datajuicer_service.core.config import Settings


def test_settings_use_isolated_queue_defaults() -> None:
    settings = Settings(
        database_url="postgresql+psycopg://user:pass@localhost/datajuicer_service",
        celery_broker_url="redis://localhost:6379/0",
    )
    assert settings.celery_queue == "datajuicer.jobs"
    assert settings.max_attempts == 3
    assert settings.worker_concurrency == 1


def test_settings_reject_non_positive_limits() -> None:
    with pytest.raises(ValidationError):
        Settings(
            database_url="postgresql+psycopg://user:pass@localhost/datajuicer_service",
            celery_broker_url="redis://localhost:6379/0",
            max_attempts=0,
        )
```

- [x] **Step 2: Run the test and verify missing package failure**

Run:

```powershell
uv run --project services/datajuicer_service pytest services/datajuicer_service/tests/test_config.py -q
```

Expected: FAIL because the service package and settings do not exist.

- [x] **Step 3: Add the pinned Data-Juicer submodule**

Run:

```powershell
git submodule add https://github.com/datajuicer/data-juicer.git services/datajuicer_service/vendor/data-juicer
git -C services/datajuicer_service/vendor/data-juicer checkout 7061da6ad06287aa0305eda162429b34361a56a3
git submodule status
```

Expected: the submodule line starts with commit `7061da6ad06287aa0305eda162429b34361a56a3`.

- [x] **Step 4: Create the Python 3.11 package**

`pyproject.toml` must declare:

```toml
[project]
name = "datajuicer-service"
version = "0.1.0"
requires-python = ">=3.11,<3.12"
dependencies = [
  "alembic>=1.16,<2",
  "celery[redis]>=5.6,<6",
  "fastapi>=0.116,<1",
  "httpx>=0.28,<1",
  "psycopg[binary]>=3.2,<4",
  "pydantic>=2.11,<3",
  "pydantic-settings>=2.10,<3",
  "scipy>=1.16,<2",
  "sqlalchemy>=2.0,<3",
  "uvicorn>=0.35,<1",
]

[dependency-groups]
dev = [
  "mypy>=1.17,<3",
  "pytest>=8.4,<10",
  "ruff>=0.12,<1",
  "ty>=0.0.25",
]
```

安装固定版本的官方 `py-data-juicer==1.5.4` wheel 以提供依赖和包元数据。由于
v1.5.4 的 Hatch build hook 在 Windows editable install 时无条件编译 Cython/C++
扩展，源码运行由启动脚本把固定的 `vendor/data-juicer` 放在 `PYTHONPATH`
首位；兼容性测试必须断言实际 `data_juicer.__file__` 位于该 submodule。

```powershell
uv lock --project services/datajuicer_service
uv sync --project services/datajuicer_service --locked
```

- [x] **Step 5: Implement validated settings**

```python
class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="DATAJUICER_",
        env_file=".env",
        extra="ignore",
    )

    database_url: str
    celery_broker_url: str
    celery_queue: str = "datajuicer.jobs"
    job_timeout_seconds: int = Field(default=3600, gt=0)
    max_attempts: int = Field(default=3, gt=0)
    recovery_interval_seconds: int = Field(default=30, gt=0)
    worker_concurrency: int = Field(default=1, gt=0)
    profile_np: int = Field(default=1, gt=0)
```

- [x] **Step 6: Verify package and pinned upstream**

Run:

```powershell
$env:PYTHONPATH=(Resolve-Path services/datajuicer_service/vendor/data-juicer)
uv run --project services/datajuicer_service python -c "import data_juicer; print(data_juicer.__version__); print(data_juicer.__file__)"
uv run --project services/datajuicer_service pytest services/datajuicer_service/tests/test_config.py -q
uv run --project services/datajuicer_service ruff check services/datajuicer_service
```

Expected: Data-Juicer reports `1.5.4`; tests and Ruff pass.

- [x] **Step 7: Commit**

```powershell
git add .gitmodules services/datajuicer_service
git commit -m "构建：初始化Data-Juicer独立服务"
```

---

### Task 2: Profile 输入输出模型与精准分组

**Files:**
- Create: `services/datajuicer_service/datajuicer_service/profiles/models.py`
- Create: `services/datajuicer_service/datajuicer_service/profiles/exact.py`
- Create: `services/datajuicer_service/datajuicer_service/profiles/io.py`
- Create: `services/datajuicer_service/tests/profiles/test_exact.py`
- Create: `services/datajuicer_service/tests/profiles/test_io.py`

**Interfaces:**
- Produces: `InputSample(uid: int, text: str)`.
- Produces: `ExactGroup(member_uids: tuple[int, ...], representative_uid: int)`.
- Produces: `load_input_jsonl(path: Path, limits: InputLimits) -> list[InputSample]`.
- Produces: `group_exact(samples: Sequence[InputSample]) -> ExactGroupingResult`.

- [x] **Step 1: Write failing input and exact grouping tests**

```python
def test_load_input_rejects_duplicate_uid(tmp_path: Path) -> None:
    path = tmp_path / "input.jsonl"
    path.write_text('{"uid":0,"text":"a"}\n{"uid":0,"text":"b"}\n', encoding="utf-8")
    with pytest.raises(ProfileInputError, match="DUPLICATE_UID"):
        load_input_jsonl(path, InputLimits(max_records=10, max_bytes=1024, max_text_chars=1024))


def test_exact_grouping_ignores_outer_whitespace_only() -> None:
    samples = [
        InputSample(uid=0, text="  A 1!\\n"),
        InputSample(uid=1, text="A 1!"),
        InputSample(uid=2, text="a 1!"),
        InputSample(uid=3, text="A1!"),
    ]
    result = group_exact(samples)
    assert result.groups == (ExactGroup(member_uids=(0, 1), representative_uid=0),)
    assert result.independent_uids == (2, 3)
```

- [x] **Step 2: Run tests and verify failure**

Run:

```powershell
uv run --project services/datajuicer_service pytest services/datajuicer_service/tests/profiles/test_io.py services/datajuicer_service/tests/profiles/test_exact.py -q
```

Expected: FAIL because profile models and functions do not exist.

- [x] **Step 3: Implement strict JSONL loading**

Implement:

```python
def load_input_jsonl(path: Path, limits: InputLimits) -> list[InputSample]:
    # Count real file records; reject blank lines, unknown fields, invalid JSON,
    # non-integer/negative/duplicate uid, non-string text, empty input and limits.
```

The loader must stream lines and track bytes/characters without loading an unbounded file first.

- [x] **Step 4: Implement collision-safe exact grouping**

Use normalized text `sample.text.strip()`. Hash it for bucket lookup, but compare the normalized string inside each hash bucket before grouping. Select the temporary representative by:

```text
normalized length DESC
uid ASC
```

- [x] **Step 5: Run profile tests and type gates**

```powershell
uv run --project services/datajuicer_service pytest services/datajuicer_service/tests/profiles/test_io.py services/datajuicer_service/tests/profiles/test_exact.py -q
uv run --project services/datajuicer_service mypy services/datajuicer_service/datajuicer_service/profiles
uv run --project services/datajuicer_service ty check services/datajuicer_service/datajuicer_service/profiles
```

Expected: all pass.

- [x] **Step 6: Commit**

```powershell
git add services/datajuicer_service/datajuicer_service/profiles services/datajuicer_service/tests/profiles
git commit -m "功能：实现文本精准分组"
```

---

### Task 3: Data-Juicer MinHash adapter 与完整聚类

**Files:**
- Create: `services/datajuicer_service/datajuicer_service/profiles/minhash.py`
- Create: `services/datajuicer_service/datajuicer_service/profiles/compatibility.py`
- Create: `services/datajuicer_service/tests/profiles/test_minhash.py`
- Create: `services/datajuicer_service/tests/profiles/test_datajuicer_compatibility.py`

**Interfaces:**
- Consumes: `InputSample`, exact temporary representatives.
- Produces: `MinHashCluster(member_uids: tuple[int, ...])`.
- Produces: `cluster_minhash(samples: Sequence[InputSample], config: MinHashConfig) -> tuple[MinHashCluster, ...]`.
- Produces: `verify_datajuicer_runtime() -> DataJuicerRuntime`.

- [x] **Step 1: Write compatibility and Chinese near-duplicate tests**

```python
def test_runtime_is_pinned_datajuicer() -> None:
    runtime = verify_datajuicer_runtime()
    assert runtime.version == "1.5.4"
    assert runtime.commit == "7061da6ad06287aa0305eda162429b34361a56a3"


def test_minhash_clusters_chinese_near_duplicates() -> None:
    samples = [
        InputSample(uid=0, text="这是用于测试的中文长文档，包含完整的主体内容和结论。" * 20),
        InputSample(uid=1, text="这是用于测试的中文长文档，包含完整主体内容以及结论。" * 20),
        InputSample(uid=2, text="完全不同的技术说明。" * 20),
    ]
    clusters = cluster_minhash(samples, MinHashConfig.v1())
    assert clusters == (MinHashCluster(member_uids=(0, 1)),)
```

- [x] **Step 2: Run tests and verify failure**

```powershell
uv run --project services/datajuicer_service pytest services/datajuicer_service/tests/profiles/test_datajuicer_compatibility.py services/datajuicer_service/tests/profiles/test_minhash.py -q
```

Expected: FAIL because the adapter does not exist.

- [x] **Step 3: Implement runtime pin verification**

Verify without importing the upstream operator registry (which triggers the
Data-Juicer LazyLoader and installs Ray):

- `data_juicer.__version__ == "1.5.4"`;
- submodule HEAD equals the pinned commit;
- the pinned operator source exposes the constructor fields used by v1;
- character tokenization and 256-permutation signature shape match expectations.

The API process and worker must fail startup if compatibility validation fails.

- [x] **Step 4: Implement MinHash config and signature computation**

Use fixed v1 values:

```python
@dataclass(frozen=True)
class MinHashConfig:
    tokenization: str = "character"
    window_size: int = 5
    lowercase: bool = True
    ignore_pattern: str | None = None
    num_permutations: int = 256
    jaccard_threshold: float = 0.7
```

Mirror the pinned Data-Juicer v1.5.4 tokenization, shingling, SHA1-32,
seed-42 permutation and optimal bands/rows logic inside the adapter. Verify the
complete signature digest against a literal captured from the pinned operator.
Do not import the upstream operator registry or invoke its final
`dataset.filter`, because that import path activates the LazyLoader and adds
Ray outside the lock file.

- [x] **Step 5: Implement full-membership LSH and union-find clustering**

Build LSH buckets from signatures, union candidate uid values, and return sorted member uid tuples for every component of size greater than one. Do not return or filter singleton components.

- [x] **Step 6: Verify deterministic behavior**

Run the tests twice with reversed sample iteration and assert identical sorted clusters:

```powershell
uv run --project services/datajuicer_service pytest services/datajuicer_service/tests/profiles/test_minhash.py -q --count=2
```

If `pytest-repeat` is not installed, run the same pytest command twice explicitly.

- [x] **Step 7: Run gates and commit**

```powershell
uv run --project services/datajuicer_service pytest services/datajuicer_service/tests/profiles -q
uv run --project services/datajuicer_service ruff check services/datajuicer_service/datajuicer_service/profiles services/datajuicer_service/tests/profiles
uv run --project services/datajuicer_service mypy services/datajuicer_service/datajuicer_service/profiles
git add services/datajuicer_service/datajuicer_service/profiles services/datajuicer_service/tests/profiles
git commit -m "功能：接入Data-Juicer MinHash聚类"
```

---

### Task 4: `text_exact_minhash_v1` 合并、代表与输出

**Files:**
- Create: `services/datajuicer_service/datajuicer_service/profiles/text_exact_minhash_v1.py`
- Create: `services/datajuicer_service/datajuicer_service/profiles/registry.py`
- Modify: `services/datajuicer_service/datajuicer_service/profiles/io.py`
- Create: `services/datajuicer_service/tests/profiles/test_text_exact_minhash_v1.py`

**Interfaces:**
- Produces: `ClusterDecision(uid, cluster_id, representative, method)`.
- Produces: `TextExactMinhashV1.execute(input_path, output_path, progress) -> ProfileResult`.
- Produces: `get_profile(name: str, limits: InputLimits) -> ProfileExecutor`.

- [x] **Step 1: Write failing exact-plus-minhash expansion tests**

```python
def test_profile_expands_exact_group_into_minhash_cluster(tmp_path: Path) -> None:
    samples = [
        InputSample(uid=0, text=LONG_FULL_TEXT),
        InputSample(uid=1, text=f"  {LONG_FULL_TEXT}\\n"),
        InputSample(uid=2, text=LONG_NEAR_DUPLICATE),
        InputSample(uid=3, text=UNRELATED_TEXT),
    ]
    decisions = execute_samples(samples, request_id=REQUEST_ID)
    grouped = [item for item in decisions if item.cluster_id is not None]
    assert {item.uid for item in grouped} == {0, 1, 2}
    assert {item.method for item in grouped} == {"exact_minhash"}
    assert sum(item.representative for item in grouped) == 1
    assert next(item.uid for item in grouped if item.representative) == 0
    assert decisions[-1].cluster_id is None
    assert decisions[-1].representative is True
```

- [x] **Step 2: Run test and verify failure**

```powershell
uv run --project services/datajuicer_service pytest services/datajuicer_service/tests/profiles/test_text_exact_minhash_v1.py -q
```

Expected: FAIL because profile orchestration does not exist.

- [x] **Step 3: Implement two-stage expansion**

Create exact groups, select exact representatives, run MinHash only on representatives and original independent samples, then expand every MinHash component back to all exact members.

Assign method:

- exact-only group: `exact`;
- MinHash-only group: `minhash`;
- final group containing an exact group and a MinHash edge: `exact_minhash`;
- singleton: `None`.

- [x] **Step 4: Implement representative and cluster ID**

Select exactly one final representative by normalized length descending and uid ascending. Generate internal UUIDv5 using a fixed service namespace and:

```text
requestId + "\0" + ",".join(sorted member uid)
```

- [x] **Step 5: Implement atomic JSONL profile output**

Write all uid decisions to a job-owned `.part`, validate full uid equality and cluster invariants, compute SHA-256, then publish with no overwrite.

- [x] **Step 6: Verify profile output**

```powershell
uv run --project services/datajuicer_service pytest services/datajuicer_service/tests/profiles -q
uv run --project services/datajuicer_service ruff check services/datajuicer_service/datajuicer_service/profiles services/datajuicer_service/tests/profiles
uv run --project services/datajuicer_service mypy services/datajuicer_service/datajuicer_service/profiles
```

- [x] **Step 7: Commit**

```powershell
git add services/datajuicer_service/datajuicer_service/profiles services/datajuicer_service/tests/profiles
git commit -m "功能：实现精准与MinHash两阶段去重"
```

---

### Task 5: Job 持久化、状态机与 migration

**Files:**
- Create: `services/datajuicer_service/datajuicer_service/core/database.py`
- Create: `services/datajuicer_service/datajuicer_service/jobs/models.py`
- Create: `services/datajuicer_service/datajuicer_service/jobs/state_machine.py`
- Create: `services/datajuicer_service/datajuicer_service/jobs/repository.py`
- Create: `services/datajuicer_service/alembic.ini`
- Create: `services/datajuicer_service/migrations/env.py`
- Create: `services/datajuicer_service/migrations/versions/20260731_01_create_datajuicer_job.py`
- Create: `services/datajuicer_service/tests/jobs/test_state_machine.py`
- Create: `services/datajuicer_service/tests/jobs/test_repository.py`

**Interfaces:**
- Produces: `JobStatus`, `DataJuicerJob`, `JobRepository`.
- Produces: conditional state transitions and lease acquisition.

- [x] **Step 1: Write failing state and idempotency tests**

```python
def test_illegal_terminal_transition_is_rejected() -> None:
    with pytest.raises(InvalidTransition):
        require_transition(JobStatus.SUCCEEDED, JobStatus.RUNNING)


def test_concurrent_request_id_has_one_job(session_factory) -> None:
    # Insert the same request_id from two independent sessions.
    # Assert one row exists and both callers resolve to the same job id.
```

- [x] **Step 2: Run tests and verify failure**

```powershell
uv run --project services/datajuicer_service pytest services/datajuicer_service/tests/jobs -q
```

- [x] **Step 3: Implement model and migration**

Implement all logical fields from the service spec, including unique `request_id`, request fingerprint, progress, lease, attempts, prepared/output digests and lifecycle timestamps.

- [x] **Step 4: Implement repository**

Required methods:

```python
create_or_get(request: JobCreate) -> CreateJobResult
get(job_id: UUID) -> DataJuicerJob | None
acquire_execution(job_id: UUID, now: datetime) -> ExecutionLease | None
mark_queued(job_id: UUID, now: datetime) -> None
update_progress(job_id: UUID, lease_token: UUID, progress: JobProgress) -> None
mark_succeeded(job_id: UUID, lease_token: UUID, result: JobResult) -> None
mark_failed(job_id: UUID, lease_token: UUID | None, error: JobError) -> None
find_recoverable(now: datetime, limit: int) -> list[DataJuicerJob]
```

- [x] **Step 5: Run PostgreSQL-backed tests**

Use a dedicated test database URL:

```powershell
$env:DATAJUICER_DATABASE_URL='postgresql+psycopg://postgres:postgres@localhost:5432/datajuicer_test'
uv run --project services/datajuicer_service alembic upgrade head
uv run --project services/datajuicer_service pytest services/datajuicer_service/tests/jobs -q
```

- [x] **Step 6: Run type gates and commit**

```powershell
uv run --project services/datajuicer_service mypy services/datajuicer_service/datajuicer_service/jobs
uv run --project services/datajuicer_service ty check services/datajuicer_service/datajuicer_service/jobs
git add services/datajuicer_service/datajuicer_service/core/database.py services/datajuicer_service/datajuicer_service/jobs services/datajuicer_service/migrations services/datajuicer_service/alembic.ini services/datajuicer_service/tests/jobs
git commit -m "功能：持久化Data-Juicer任务状态"
```

---

### Task 6: POST/GET API 与幂等入队

**Files:**
- Create: `services/datajuicer_service/datajuicer_service/api/schemas.py`
- Create: `services/datajuicer_service/datajuicer_service/api/routes.py`
- Create: `services/datajuicer_service/datajuicer_service/jobs/service.py`
- Create: `services/datajuicer_service/datajuicer_service/jobs/dispatcher.py`
- Create: `services/datajuicer_service/datajuicer_service/main.py`
- Create: `services/datajuicer_service/tests/api/test_jobs.py`

**Interfaces:**
- Produces: `POST /v1/jobs`, `GET /v1/jobs/{jobId}`.
- Produces: `JobService.create_job()` and `CeleryJobDispatcher.enqueue()`.

- [x] **Step 1: Write API contract tests**

```python
def test_create_job_returns_202(client: TestClient) -> None:
    response = client.post("/v1/jobs", json=VALID_REQUEST)
    assert response.status_code == 202
    assert response.json()["requestId"] == VALID_REQUEST["requestId"]
    assert response.json()["status"] == "queued"


def test_same_request_id_with_different_path_returns_409(client: TestClient) -> None:
    assert client.post("/v1/jobs", json=VALID_REQUEST).status_code == 202
    changed = {**VALID_REQUEST, "outputPath": "/tmp/changed.jsonl"}
    response = client.post("/v1/jobs", json=changed)
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "IDEMPOTENCY_CONFLICT"
```

- [x] **Step 2: Run tests and verify failure**

```powershell
uv run --project services/datajuicer_service pytest services/datajuicer_service/tests/api/test_jobs.py -q
```

- [x] **Step 3: Implement camelCase schemas and response invariants**

Implement `JobCreate`, `JobAccepted`, `JobPublic`, `JobProgressPublic`, `JobResultPublic`, and `JobErrorPublic`. Enforce result/error mutual exclusion.

- [x] **Step 4: Implement create service and dispatch boundary**

On a new job:

1. persist `pending`;
2. dispatch only `{jobId, taskType, schemaVersion}`;
3. mark `queued`;
4. return 202.

On enqueue failure, mark failed with `QUEUE_SUBMISSION_FAILED` and return 503.

- [x] **Step 5: Implement GET mapping and health check**

Add `/health` that verifies process health only; database readiness belongs to a separate `/ready` check that performs a lightweight query.

- [x] **Step 6: Run API tests and commit**

```powershell
uv run --project services/datajuicer_service pytest services/datajuicer_service/tests/api -q
uv run --project services/datajuicer_service ruff check services/datajuicer_service/datajuicer_service/api services/datajuicer_service/tests/api
git add services/datajuicer_service/datajuicer_service/api services/datajuicer_service/datajuicer_service/jobs/service.py services/datajuicer_service/datajuicer_service/jobs/dispatcher.py services/datajuicer_service/datajuicer_service/main.py services/datajuicer_service/tests/api
git commit -m "功能：提供Data-Juicer异步任务接口"
```

---

### Task 7: Celery execute、原子发布与 recovery

**Files:**
- Create: `services/datajuicer_service/datajuicer_service/core/celery_app.py`
- Create: `services/datajuicer_service/datajuicer_service/jobs/tasks.py`
- Create: `services/datajuicer_service/datajuicer_service/jobs/orchestration.py`
- Create: `services/datajuicer_service/datajuicer_service/worker.py`
- Create: `services/datajuicer_service/tests/jobs/test_orchestration.py`
- Create: `services/datajuicer_service/tests/jobs/test_recovery.py`

**Interfaces:**
- Produces: Celery tasks `datajuicer.execute`, `datajuicer.recover`.
- Produces: `JobOrchestrator.execute(job_id)` and `recover(now)`.

- [x] **Step 1: Write duplicate message and crash-window tests**

```python
def test_duplicate_execute_message_runs_profile_once(orchestrator, profile_spy, job) -> None:
    orchestrator.execute(job.id)
    orchestrator.execute(job.id)
    assert profile_spy.calls == 1


def test_published_digest_recovers_success(orchestrator, job, published_output) -> None:
    job.prepared_output_sha256 = sha256_file(published_output)
    orchestrator.execute(job.id)
    assert repository.get(job.id).status is JobStatus.SUCCEEDED
```

- [x] **Step 2: Run tests and verify failure**

```powershell
uv run --project services/datajuicer_service pytest services/datajuicer_service/tests/jobs/test_orchestration.py services/datajuicer_service/tests/jobs/test_recovery.py -q
```

- [x] **Step 3: Configure isolated Celery**

Use queue `datajuicer.jobs`, task namespace `datajuicer.*`, JSON serialization, late ack, reject-on-worker-lost, prefetch 1, no Celery result backend and Beat recovery schedule.

- [x] **Step 4: Implement execute orchestration**

Acquire a lease, verify deadline and output state, call the profile with throttled progress persistence, save prepared digest before publish and only mark success after publication.

Map deterministic profile/input/output errors without retry. Retry transient infrastructure errors with bounded exponential backoff using the same job id.

- [x] **Step 5: Implement recovery**

Recover pending/queued dispatch gaps, expired running leases and published-output/database crash windows. Recovery must enqueue work, not execute profile code inside Beat.

- [x] **Step 6: Run orchestration tests and commit**

```powershell
uv run --project services/datajuicer_service pytest services/datajuicer_service/tests/jobs -q
uv run --project services/datajuicer_service ruff check services/datajuicer_service/datajuicer_service/jobs services/datajuicer_service/datajuicer_service/core/celery_app.py
git add services/datajuicer_service/datajuicer_service/core/celery_app.py services/datajuicer_service/datajuicer_service/jobs services/datajuicer_service/datajuicer_service/worker.py services/datajuicer_service/tests/jobs
git commit -m "功能：编排Data-Juicer后台任务"
```

---

### Task 8: 源码启动脚本与完整自动化门禁

**Files:**
- Create: `services/datajuicer_service/scripts/run_api.ps1`
- Create: `services/datajuicer_service/scripts/run_worker.ps1`
- Create: `services/datajuicer_service/scripts/run_beat.ps1`
- Create: `services/datajuicer_service/scripts/prestart.ps1`
- Create: `services/datajuicer_service/datajuicer_service/worker_app.py`
- Modify: `services/datajuicer_service/README.md`
- Create: `services/datajuicer_service/tests/integration/test_job_flow.py`

**Interfaces:**
- Produces: documented source commands for migration, API, worker and Beat.

- [x] **Step 1: Write a PostgreSQL/Celery integration test**

The test must:

1. POST a job through FastAPI;
2. assert one PostgreSQL row;
3. execute the real Celery task eagerly or through a test worker;
4. read the output JSONL;
5. GET the succeeded job;
6. verify output SHA-256 and complete uid set.

- [x] **Step 2: Run integration test and verify failure**

```powershell
uv run --project services/datajuicer_service pytest services/datajuicer_service/tests/integration/test_job_flow.py -q
```

- [x] **Step 3: Implement source startup scripts**

Scripts must use `uv run --project services/datajuicer_service` and fail if:

- Python is not 3.11;
- submodule commit differs;
- lock file is stale;
- migration fails.

No script starts Docker.

- [x] **Step 4: Run all service gates**

```powershell
uv run --project services/datajuicer_service ruff check services/datajuicer_service
uv run --project services/datajuicer_service mypy services/datajuicer_service/datajuicer_service
uv run --project services/datajuicer_service ty check services/datajuicer_service/datajuicer_service
uv run --project services/datajuicer_service pytest services/datajuicer_service/tests -q
```

Expected: all pass; tests requiring real external Hugging Face access remain separately marked.

- [x] **Step 5: Commit**

```powershell
git add services/datajuicer_service
git commit -m "测试：完善Data-Juicer源码运行门禁"
```

---

### Task 9: Hugging Face 文本数据真实验收

**Files:**
- Create: `services/datajuicer_service/scripts/run_real_text_validation.py`
- Create: `services/datajuicer_service/tests/real_integration/test_huggingface_text.py`
- Create: `docs/runbooks/datajuicer-service.md`

**Interfaces:**
- Produces: reproducible real-data validation command and evidence report.

- [x] **Step 1: Select and pin a small public text dataset slice**

Use an explicit dataset name, revision and bounded split/slice. The script must cache the selected text locally and write the exact dataset revision and sample count into its report. Do not depend on an unpinned moving dataset revision.

- [x] **Step 2: Construct deterministic duplicate fixtures**

From real text samples, generate:

- one exact duplicate with BOM/newline/outer-space variation;
- one Chinese or multilingual near-duplicate by controlled deletion/substitution;
- one unrelated sample;
- one longer/shorter near-duplicate pair to verify representative selection.

Store only generated test staging under a temporary directory, not in Git.

- [x] **Step 3: Run the profile against real data**

```powershell
uv run --project services/datajuicer_service pytest -m real_integration services/datajuicer_service/tests/real_integration/test_huggingface_text.py -q
```

Assert exact expected cluster membership, method and representative; do not only assert process success.

- [x] **Step 4: Run the real source service**

Start PostgreSQL/Redis if already available, then use the source scripts to start API, worker and Beat. Submit the generated input through `POST /v1/jobs`, poll GET to `succeeded`, and verify the file digest and content.

Capture:

```text
Data-Juicer version and commit
dataset name/revision/slice
jobId
input count
cluster decisions
output SHA-256
API/worker/Beat status
```

- [x] **Step 5: Document and rerun final gates**

Document exact commands and expected results in `docs/runbooks/datajuicer-service.md`, then run:

```powershell
uv run --project services/datajuicer_service ruff check services/datajuicer_service
uv run --project services/datajuicer_service mypy services/datajuicer_service/datajuicer_service
uv run --project services/datajuicer_service ty check services/datajuicer_service/datajuicer_service
uv run --project services/datajuicer_service pytest services/datajuicer_service/tests -q
uv run --project services/datajuicer_service pytest -m real_integration services/datajuicer_service/tests/real_integration -q
```

- [x] **Step 6: Commit**

```powershell
git add services/datajuicer_service docs/runbooks/datajuicer-service.md
git commit -m "测试：验证Data-Juicer真实文本去重"
```

---

## Completion Gate

Data-Juicer Service 第一阶段只有在以下证据全部成立时才完成：

- 源码 submodule 精确固定 v1.5.4 commit；
- 独立 Python 3.11 lock 可重建；
- POST/GET、幂等、状态机、Celery execute/recover 和 PostgreSQL migration 均有测试；
- 精准、MinHash、两阶段展开、method 和 representative 有确定性断言；
- 输出包含全部 uid 并通过原子发布与摘要恢复测试；
- Ruff、Mypy、Ty、默认 pytest 全通过；
- 固定 Hugging Face 数据切片的真实测试通过；
- API、worker、Beat、PostgreSQL、Redis 的源码端到端链路真实运行通过；
- 没有 Dockerfile 或 Docker 打包工作进入本阶段。
