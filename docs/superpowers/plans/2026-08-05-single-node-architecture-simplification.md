# TextProcessor Single-Node Architecture Simplification Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将生产栈收敛为独立 Backend API、单容器 Task Runner、四项统一 task API 和单容器重型能力服务，并保持现有任务可靠性。

**Architecture:** Backend API 只创建和查询任务；Task Runner 在一个容器内监管 Celery Worker 与 Beat。Structured Extraction、Global Deduplication、Classification 和 Markdown Cleaning 共用 Task Kernel、PostgreSQL 权威状态与 Redis DB 0 Celery broker，重型算法继续通过 adapter 调用独立能力容器。

**Tech Stack:** Python 3.14、FastAPI、Celery、Redis、PostgreSQL、SQLModel/SQLAlchemy、Alembic、Docker Compose、Pytest、PowerShell、GitHub Actions

## Global Constraints

- 当前按单机、单实例设计，每个实际服务使用一个容器，不考虑横向扩容。
- API 与 Task Runner 必须独立运行；Task Runner 在一个容器内运行 Celery Worker 与 Celery Beat。
- 目标耗时不超过 500ms 的轻型非处理接口使用 `async/await` 在当前请求内返回；执行模式由 API 契约固定，不按单次耗时动态切换。
- 业务 Route 不实现业务逻辑，不得使用 FastAPI `BackgroundTasks`。
- Structured Extraction、Global Deduplication、Classification 和 Markdown Cleaning 均使用 `POST 202 + task_id` 与 GET 查询协议。
- Celery 消息只携带 `task_id`、`task_type` 和 `schema_version`；PostgreSQL 是权威状态来源。
- 第一阶段保留 Docling 与 Data-Juicer 已有内部队列，不实施第二阶段队列重写。
- 只通过内部网络暴露能力服务；只有 Frontend 与 Backend API 接入 Traefik。
- 每项任务按 TDD 完成并独立提交；未运行的真实模型或外部服务验证不得声称通过。

---

## File Structure

### New shared runtime files

- `backend/app/tasking/envelope.py`：统一 Celery 消息解析与序列化。
- `backend/app/tasking/state.py`：共享任务状态枚举和转换校验。
- `backend/app/tasking/contracts.py`：Task Kernel repository、dispatcher 与 recovery 协议。
- `backend/app/tasking/recovery.py`：通用遗漏入队恢复循环。
- `backend/app/task_runner/process_manager.py`：监管 Celery Worker 与 Beat。
- `backend/app/task_runner/healthcheck.py`：验证两个子进程和 broker 可达。

### New classification task feature

- `backend/app/features/text_classification/`：任务模型、迁移、API、Celery 编排和 Classification adapter。
- `backend/tests/features/text_classification/`：契约、状态、幂等、adapter 和 Celery 单元测试。
- `backend/tests/integration/text_classification/`：真实 PostgreSQL/Redis/Celery 边界测试。

### Deployment files

- `services/datajuicer_service/Dockerfile`：Data-Juicer 单容器镜像。
- `services/datajuicer_service/process_manager.py`：监管 Data-Juicer API、Worker 与 Beat。
- `compose.yml`：目标生产服务定义。
- `compose.override.yml`：开发/测试差异。
- `scripts/verify-task-runner.ps1`：Task Runner 进程和恢复验证。
- `scripts/verify-single-node-stack.ps1`：八容器生产栈验收。

---

### Task 1: Reconcile the Markdown Cleaning Feature Branch

**Files:**
- Merge source: `feat/markdown-cleaning-api`
- Modify on conflict: `backend/app/api/main.py`
- Modify on conflict: `backend/app/core/celery_app.py`
- Modify on conflict: `backend/app/core/config.py`
- Modify on conflict: `backend/app/alembic/versions/`
- Test: `backend/tests/features/markdown_cleaning/`
- Test: `backend/tests/integration/markdown_cleaning/`

**Interfaces:**
- Consumes: current `dev` task APIs and Celery application.
- Produces: registered Markdown Cleaning `POST /api/v1/markdown-cleaning/tasks`, GET by task ID, Celery tasks and Alembic revisions `20260803_01` through `20260803_03`.

- [x] **Step 1: Create the execution worktree and record the baseline**

Run:

```powershell
git status --short --untracked-files=all
git log --oneline dev..feat/markdown-cleaning-api
uv run --directory backend pytest tests/features/structured_extraction tests/features/global_deduplication -q
```

Expected: existing feature tests pass; unrelated untracked files are recorded and left untouched.

- [x] **Step 2: Merge the completed Markdown branch without flattening its evidence**

Run:

```powershell
git merge --no-ff feat/markdown-cleaning-api -m "合并：接入Markdown清洗任务能力"
```

Resolve only genuine conflicts. `api_router` must include structured extraction, global deduplication, and Markdown Cleaning; `celery_app.include` and `beat_schedule` must retain all three features. When conflicts occur, stage the resolved files and finish the pending merge with `git commit --no-edit`.

- [x] **Step 3: Verify the merged Markdown contract**

Run:

```powershell
uv run --directory backend pytest tests/features/markdown_cleaning tests/api/routes/test_markdown_cleaning.py -q
```

Expected: all Markdown unit and API tests pass.

- [x] **Step 4: Verify migration continuity**

Run against an isolated PostgreSQL database:

```powershell
uv run --directory backend alembic upgrade head
uv run --directory backend alembic current
```

Expected: head is `20260803_03`; no divergent migration heads.

- [x] **Step 5: Verify the merge boundary**

```powershell
git status --short --untracked-files=all
git show --stat --oneline HEAD
```

Expected: the merge is complete, only approved Markdown files were introduced, and unrelated untracked files remain untouched.

---

### Task 2: Introduce the Shared Task Kernel

**Files:**
- Create: `backend/app/tasking/__init__.py`
- Create: `backend/app/tasking/envelope.py`
- Create: `backend/app/tasking/state.py`
- Create: `backend/app/tasking/contracts.py`
- Create: `backend/app/tasking/recovery.py`
- Modify: `backend/app/features/structured_extraction/celery_tasks.py`
- Modify: `backend/app/features/global_deduplication/messages.py`
- Modify: `backend/app/features/markdown_cleaning/messages.py`
- Test: `backend/tests/tasking/test_envelope.py`
- Test: `backend/tests/tasking/test_state.py`
- Test: `backend/tests/tasking/test_recovery.py`

**Interfaces:**
- Produces: `TaskEnvelope(task_id: UUID, task_type: str, schema_version: int)` with `as_payload() -> dict[str, str | int]` and `parse(payload: object, *, expected_type: str, expected_schema_version: int) -> TaskEnvelope`.
- Produces: `TaskStatus` enum and `ensure_transition(current: TaskStatus, target: TaskStatus) -> None`.
- Produces: `RecoverableTaskRepository` and `TaskDispatcher` protocols consumed by feature-specific recovery adapters.

- [x] **Step 1: Write failing envelope tests**

```python
def test_envelope_round_trip() -> None:
    task_id = uuid4()
    payload = TaskEnvelope(task_id, "structured_extraction", 1).as_payload()
    parsed = TaskEnvelope.parse(
        payload,
        expected_type="structured_extraction",
        expected_schema_version=1,
    )
    assert parsed.task_id == task_id


@pytest.mark.parametrize("payload", [{}, {"task_id": "bad"}, {"schema_version": True}])
def test_envelope_rejects_invalid_payload(payload: object) -> None:
    with pytest.raises(ValueError):
        TaskEnvelope.parse(payload, expected_type="x", expected_schema_version=1)
```

- [x] **Step 2: Run the envelope tests and verify RED**

Run: `uv run --directory backend pytest tests/tasking/test_envelope.py -q`

Expected: collection fails because `app.tasking.envelope` does not exist.

- [x] **Step 3: Implement the immutable envelope**

```python
@dataclass(frozen=True, slots=True)
class TaskEnvelope:
    task_id: UUID
    task_type: str
    schema_version: int

    def as_payload(self) -> dict[str, str | int]:
        return {
            "task_id": str(self.task_id),
            "task_type": self.task_type,
            "schema_version": self.schema_version,
        }
```

`parse` must reject booleans as schema versions, unknown task types, schema mismatch, missing fields and extra fields.

- [x] **Step 4: Write and implement shared state transition tests**

```python
def test_terminal_state_cannot_transition() -> None:
    with pytest.raises(IllegalTaskTransition):
        ensure_transition(TaskStatus.SUCCEEDED, TaskStatus.RUNNING)
```

Allowed transitions are exactly `pending -> queued -> running -> succeeded|failed|cancelled`, plus `pending|queued -> cancelled`.

- [x] **Step 5: Write and implement generic recovery protocol tests**

```python
def test_recover_dispatches_each_due_task_once() -> None:
    repository = FakeRecoverableRepository([first_id, second_id])
    dispatcher = FakeDispatcher()
    assert recover_due_tasks(repository, dispatcher) == 2
    assert dispatcher.ids == [first_id, second_id]
```

The helper must continue after one dispatch failure and only mark successfully dispatched tasks.

- [x] **Step 6: Migrate the three existing features to TaskEnvelope**

Replace feature-local payload parsing with `TaskEnvelope.parse`; retain feature-specific task type constants and public API aliases. Do not change database schemas in this step.

- [x] **Step 7: Run shared and affected feature tests**

Run:

```powershell
uv run --directory backend pytest tests/tasking tests/features/structured_extraction/test_celery_tasks.py tests/features/global_deduplication/test_messages.py tests/features/markdown_cleaning/test_messages.py -q
uv run --directory backend ruff check app/tasking app/features
```

Expected: all tests and Ruff pass.

- [x] **Step 8: Commit**

```powershell
git add backend/app/tasking backend/app/features backend/tests/tasking backend/tests/features
git commit -m "重构：建立共享任务可靠性内核"
```

---

### Task 3: Add Classification as a Celery Task API

**Files:**
- Create: `backend/app/features/text_classification/` modules for models, schemas, repository, state machine, service, dispatcher, adapter, orchestration, Celery tasks and routes.
- Create: `backend/app/alembic/versions/20260805_01_add_text_classification_tasks.py`
- Modify: `backend/app/api/main.py`
- Modify: `backend/app/core/celery_app.py`
- Modify: `backend/app/core/config.py`
- Modify: `.env.example`
- Test: `backend/tests/features/text_classification/`
- Test: `backend/tests/integration/text_classification/test_task_pipeline.py`

**Interfaces:**
- Produces: `POST /api/v1/text-classification/tasks` returning `202` and camel-case `taskId`, `status`, `createdAt`.
- Produces: `GET /api/v1/text-classification/tasks/{task_id}` with caller isolation.
- Consumes: Classification `POST /internal/v1/classify` through `ClassificationHttpAdapter.classify(input_uri: str) -> ClassificationResult`.
- Produces: Celery task type `text_classification`, schema version `1`.

- [x] **Step 1: Write failing public API contract tests**

```python
def test_create_classification_task_returns_202(client: TestClient, token: str) -> None:
    response = client.post(
        "/api/v1/text-classification/tasks",
        headers={"Authorization": f"Bearer {token}"},
        json={"callerId": "caller-a", "sessionId": "s-1", "fileId": "f-1", "inputUri": "file:///allowed/input.txt"},
    )
    assert response.status_code == 202
    assert set(response.json()) >= {"taskId", "status", "createdAt"}
```

Also test duplicate idempotency, cross-caller GET denial, input size limits, stable error codes and absence of result text in logs.

- [x] **Step 2: Run contract tests and verify RED**

Run: `uv run --directory backend pytest tests/features/text_classification/test_api_contract.py -q`

Expected: import or route registration failure.

- [x] **Step 3: Add the task table and migration**

Create fields for UUID ID, caller identity, `(caller_id, session_id, file_id)` unique key, validated source URI, input digest, staging URI, status, dispatch markers, attempt count, result JSON/reference, error code/summary and lifecycle timestamps. PostgreSQL does not store the document body.

- [x] **Step 4: Implement repository, state machine and service**

```python
class TextClassificationTaskService:
    def create_task(self, command: CreateClassificationTask) -> TextClassificationTask:
        task, created = self._repository.create_or_get(command)
        if created:
            self._dispatcher.enqueue(task.id)
        return task

    def get_task(self, task_id: UUID, *, caller_id: str) -> TextClassificationTask:
        return self._repository.get_for_caller(task_id, caller_id=caller_id)
```

Use Task Kernel envelope and transitions. Commit the task record before dispatch; on broker failure retain a recoverable pending/queued state and return a stable `QUEUE_SUBMISSION_FAILED` response.

- [x] **Step 5: Implement the Classification adapter contract**

```python
class ClassificationHttpAdapter:
    def classify(self, input_uri: str) -> ClassificationResult:
        response = self._client.post(
            "/internal/v1/classify",
            json={"schemaVersion": "1", "requestId": self._request_id, "inputUri": input_uri},
        )
        response.raise_for_status()
        return ClassificationResult.model_validate(response.json())
```

Task Runner first downloads the validated source through `fsspec` into a task-specific staging directory and passes only its shared `file://` URI. Send the configured internal service token, set connect/read/pool timeouts, reject oversized or malformed responses, and map OOM/unavailable/timeout errors to stable codes. Classification resolves only files below its read-only staging root, enforces byte and UTF-8 limits, and does not persist input or output.

- [x] **Step 6: Implement Celery execution and recovery**

The Worker loads input from PostgreSQL, marks the task running, calls the adapter once per attempt, validates labels/scores, persists the result and marks succeeded. Configure finite retry and Beat recovery for undispatched or expired running tasks.

- [x] **Step 7: Register routes and Celery tasks**

Add the router to `backend/app/api/main.py`, the task module to `celery_app.include`, and a recovery schedule to `beat_schedule`.

- [x] **Step 8: Run unit and real-boundary integration tests**

Run:

```powershell
uv run --directory backend pytest tests/features/text_classification -q
uv run --directory backend pytest tests/integration/text_classification/test_task_pipeline.py -q
uv run --directory backend ruff check app/features/text_classification tests/features/text_classification
```

Expected: duplicate messages produce one result, worker loss is recoverable, and cross-caller GET is rejected.

- [x] **Step 9: Commit**

```powershell
git add backend/app/features/text_classification backend/app/alembic backend/app/api/main.py backend/app/core backend/tests .env.example
git commit -m "功能：将文本分类接入后台任务接口"
```

---

### Task 4: Merge Celery Worker and Beat into Task Runner

**Files:**
- Create: `backend/app/task_runner/__init__.py`
- Create: `backend/app/task_runner/process_manager.py`
- Create: `backend/app/task_runner/healthcheck.py`
- Modify: `backend/Dockerfile`
- Modify: `compose.yml`
- Modify: `compose.override.yml`
- Test: `backend/tests/task_runner/test_process_manager.py`
- Test: `backend/tests/task_runner/test_healthcheck.py`
- Test: `backend/tests/features/structured_extraction/test_deployment_stack.py`

**Interfaces:**
- Produces: `python -m app.task_runner.process_manager` as Task Runner entrypoint.
- Produces state file `/var/run/textprocessor/task-runner.json` containing exact keys `worker` and `beat` with positive PIDs.
- Produces health command `python -m app.task_runner.healthcheck`.

- [x] **Step 1: Write failing supervisor tests**

```python
def test_child_failure_terminates_sibling_and_returns_nonzero(tmp_path: Path) -> None:
    code = run_supervisor((fast_failure(), sleeping_process()), state_path=tmp_path / "state.json")
    assert code != 0
```

Also cover SIGTERM forwarding, exact state keys, atomic state writes and clean startup failure.

- [x] **Step 2: Run tests and verify RED**

Run: `uv run --directory backend pytest tests/task_runner/test_process_manager.py -q`

Expected: module not found.

- [x] **Step 3: Implement the Python PID 1 supervisor**

Start these independent commands:

```text
celery -A app.core.celery_app:celery_app worker --loglevel=INFO
celery -A app.core.celery_app:celery_app beat --loglevel=INFO --pidfile=/var/run/celery/beat.pid --schedule=/var/lib/celery/beat-schedule
```

Forward SIGTERM/SIGINT, terminate the sibling on unexpected exit, wait with a bounded grace period, and return nonzero on child failure.

- [x] **Step 4: Implement health checks**

Health must verify both PIDs are alive, Redis DB 0 responds, and the Beat schedule path exists after startup. Reject missing, malformed or extra state keys.

- [x] **Step 5: Replace Compose services**

Remove `extraction-worker` and `extraction-beat`; add `task-runner` using the backend image, supervisor command, Redis/PostgreSQL dependencies, the Beat volume and the new healthcheck. Update override files and all contract tests to reject the removed service names.

- [x] **Step 6: Build and exercise fault recovery**

Run:

```powershell
docker compose config --services
docker compose build backend-api task-runner
docker compose up -d db redis task-runner
```

Kill the Worker child, assert the container restart count increases and health returns; repeat for Beat.

- [x] **Step 7: Run tests and commit**

```powershell
uv run --directory backend pytest tests/task_runner tests/features/structured_extraction/test_deployment_stack.py -q
git add backend/app/task_runner backend/tests/task_runner backend/Dockerfile compose.yml compose.override.yml
git commit -m "部署：合并Celery Worker与Beat容器"
```

---

### Task 5: Package Data-Juicer as One Capability Container

**Files:**
- Create: `services/datajuicer_service/Dockerfile`
- Create: `services/datajuicer_service/process_manager.py`
- Create: `services/datajuicer_service/healthcheck.py`
- Create: `services/datajuicer_service/tests/test_container_contract.py`
- Create: `services/datajuicer_service/tests/test_process_manager.py`
- Modify: `compose.yml`
- Modify: `.env.example`

**Interfaces:**
- Produces one `datajuicer` Compose service containing API, existing Celery Worker and Beat.
- Consumes shared Redis using an isolated DB/prefix and Data-Juicer's existing database configuration.
- Exposes only the internal `/v1/jobs`, `/v1/jobs/{job_id}`, `/health` and `/ready` API.

- [x] **Step 1: Write failing container contract tests**

```python
def test_compose_has_one_datajuicer_service() -> None:
    content = Path("compose.yml").read_text(encoding="utf-8")
    assert "  datajuicer:" in content
    assert "  datajuicer-worker:" not in content
    assert "  datajuicer-beat:" not in content
```

Also assert there is no public Traefik label or production host port.

- [x] **Step 2: Implement a three-process supervisor**

Supervise Uvicorn API, Data-Juicer Celery Worker and Beat using the same signal, atomic state and sibling-termination rules as Task Runner. State keys are exactly `api`, `worker`, `beat`.

- [x] **Step 3: Build the Data-Juicer image**

Install the locked service dependencies and vendored Data-Juicer runtime, run as a non-root user, use the supervisor as entrypoint, and provide a healthcheck that verifies all three processes plus `/ready` and Redis connectivity.

- [x] **Step 4: Add the production Compose service**

Use one service, internal network only, explicit model/data/cache volumes, shared Redis isolation, PostgreSQL readiness, finite restart policy and no published port.

- [x] **Step 5: Verify current Data-Juicer behavior is preserved**

Run:

```powershell
uv run --directory services/datajuicer_service pytest tests -m "not real_integration" -q
docker compose build datajuicer
docker compose up -d db redis datajuicer
```

Submit one authorized small real job only when its configured fixture exists; otherwise report real capability validation as incomplete.

- [x] **Step 6: Commit**

```powershell
git add services/datajuicer_service/Dockerfile services/datajuicer_service/process_manager.py services/datajuicer_service/healthcheck.py services/datajuicer_service/tests compose.yml .env.example
git commit -m "部署：将Data-Juicer封装为单容器服务"
```

---

### Task 6: Simplify Production Profiles and Migration Flow

**Files:**
- Modify: `compose.yml`
- Modify: `compose.override.yml`
- Modify: `compose.traefik.yml`
- Modify: `.github/workflows/deploy-staging.yml`
- Modify: `.github/workflows/deploy-production.yml`
- Modify: `.github/workflows/test-docker-compose.yml`
- Modify: `deployment.md`
- Test: `backend/tests/features/structured_extraction/test_deployment_stack.py`
- Create: `backend/tests/architecture/test_execution_boundaries.py`

**Interfaces:**
- Produces default services: `frontend`, `backend-api`, `task-runner`, `docling`, `classification`, `datajuicer`, `redis`, `db`.
- Produces `debug` profile for Adminer and development/test-only services in override files.
- Produces release ordering: pull/build -> migrate once -> start/update -> verify.

- [x] **Step 1: Write failing Compose contract assertions**

Assert default config excludes `adminer`, `prestart`, `extraction-worker` and `extraction-beat`; assert exactly the eight target services; assert only `frontend` and `backend-api` join `traefik-public`. Rename existing Compose service keys `backend`, `docling-api` and `classification-service` to `backend-api`, `docling` and `classification`, then update internal URLs, dependencies, labels, scripts and workflow service lists atomically.

Add an AST-based architecture test that fails when a production Route imports `BackgroundTasks` or directly imports a processor/algorithm implementation. The allowed dependency direction is `routes -> service/dependencies -> processor|adapter`.

- [x] **Step 2: Move Adminer and auxiliary services to profiles**

Set `profiles: [debug]` for Adminer. Keep Mailcatcher, Playwright and local Proxy in development/test overrides; do not duplicate production service definitions.

- [x] **Step 3: Remove the prestart service**

Remove Compose dependency on `prestart`. Deployment workflows must run the backend image once with `bash scripts/prestart.sh` after database health and before application services are updated.

- [x] **Step 4: Update deployment workflows**

Both staging and production workflows must load the same base Compose and capability overlays, run migration once, start all eight services and execute the single-node verifier. Keep shell interpolation in environment variables rather than GitHub expression interpolation inside scripts.

- [x] **Step 5: Validate Compose and workflows**

Run:

```powershell
docker compose config --services
docker compose --profile debug config --services
zizmor .github/workflows
uv run --directory backend pytest tests/features/structured_extraction/test_deployment_stack.py -q
```

Expected: default output has eight target services; debug adds Adminer; no workflow findings beyond documented suppressions.

- [x] **Step 6: Commit**

```powershell
git add compose.yml compose.override.yml compose.traefik.yml .github/workflows deployment.md backend/tests/features/structured_extraction/test_deployment_stack.py
git commit -m "发布：收敛单机生产服务与迁移流程"
```

---

### Task 7: Add Unified Runtime and Failure Verification

**Files:**
- Create: `scripts/verify-task-runner.ps1`
- Create: `scripts/verify-single-node-stack.ps1`
- Modify: `scripts/verify-extraction-stack.ps1`
- Modify: `scripts/verify-classification-service.ps1`
- Modify: `scripts/verify-markdown-cleaning-stack.ps1`
- Modify: `docs/runbooks/structured-extraction.md`
- Modify: `docs/runbooks/markdown-cleaning.md`
- Create: `docs/runbooks/single-node-deployment.md`

**Interfaces:**
- Produces one release verifier that checks service count, health, network exposure, Redis isolation, API/Task Runner independent failure and cleanup.
- Preserves capability-specific real verification scripts for Docling, Classification and Data-Juicer.

- [x] **Step 1: Write static verifier contract tests**

Tests must assert the verifier names all eight services, rejects removed service names, inspects child PIDs, checks no capability host ports, and performs cleanup in `finally`.

- [x] **Step 2: Implement Task Runner verification**

Verify exact `worker` and `beat` state keys, Redis DB 0 ping, Beat schedule, Worker ping, container health and restart count changes after separately killing each child.

- [x] **Step 3: Implement full-stack failure scenarios**

Create one task, kill Backend API and confirm the task completes; create another task, kill Task Runner and confirm API can still create/query tasks while Redis/PostgreSQL are healthy, then confirm recovery after Task Runner restart.

- [x] **Step 4: Verify capability isolation**

Assert Docling, Classification and Data-Juicer have no published production ports and remain healthy across Backend API restart. Run real samples only when their explicit environment variables are present.

- [x] **Step 5: Run the complete quality gate**

```powershell
$env:PYTHONPATH="$(Get-Location);$(Get-Location)\backend"
uv run --directory backend pytest tests -m "not real_integration" -q
uv run --directory backend ruff check app tests
uv run --directory services/classification_service pytest tests -m "not real_integration" -q
uv run --directory services/datajuicer_service pytest tests -m "not real_integration" -q
pwsh -NoProfile -File scripts/verify-single-node-stack.ps1
```

Expected: all fast suites pass; verifier exits 0 and removes its temporary containers, networks, volumes and files. Missing real fixtures remain explicitly incomplete.

- [x] **Step 6: Update runbooks and commit**

```powershell
git add scripts docs/runbooks
git commit -m "验证：覆盖单机架构与独立故障恢复"
```

---

### Task 8: Final Architecture Reconciliation

**Files:**
- Modify if evidence requires: `docs/superpowers/specs/2026-08-05-single-node-architecture-simplification-design.md`
- Modify: `deployment.md`
- Verify: all files changed by Tasks 1-7.

**Interfaces:**
- Consumes all prior task deliverables.
- Produces a clean, reviewable branch whose Compose topology, API contracts, tests and documentation agree with ADR-0001.

- [x] **Step 1: Reconcile every Spec requirement with current evidence**

Check eight default services, four task APIs, Task Kernel usage, API/Task Runner isolation, profiles, migration ordering, stable error handling and real-test boundaries. Any unmet item remains an open implementation item; do not convert it into documentation-only success.

- [x] **Step 2: Run final repository checks**

```powershell
git diff --check
git status --short --untracked-files=all
git log --oneline --decorate -12
```

Review staged and unstaged changes, preserve unrelated user files and ensure each commit contains one approved task boundary.

- [x] **Step 3: Run the release gate again from the final tree**

Run the full fast test, lint, Compose, workflow and single-node verifier commands from Task 7. Record exact pass/deselect/warning counts and list every unrun real integration test.

- [x] **Step 4: Commit evidence-only corrections**

```powershell
git add docs deployment.md
git commit -m "文档：对齐单机架构发布证据"
```

Create this commit only when documentation needed evidence-based correction; otherwise leave the verified tree unchanged.
