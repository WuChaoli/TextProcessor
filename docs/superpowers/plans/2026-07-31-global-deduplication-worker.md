# 全局文档去重 TextProcessor Worker Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在 TextProcessor backend 中实现全局文档去重 worker，完成清单与文档校验、任务 staging、Data-Juicer 异步 job 编排、结果映射、禁止覆盖发布和中断恢复。

**Architecture:** 新能力位于独立的 `app.features.global_deduplication` feature，不复用或污染 `ExtractionTask`。Celery `submit` 负责输入准备和幂等提交，`poll` 负责外部状态同步及快速 finalize，Beat `recover` 只扫描并重投；PostgreSQL 是任务与恢复状态的唯一真相源。

**Tech Stack:** Python 3.14, FastAPI project settings, SQLModel/SQLAlchemy, Alembic, Celery 5.6, PostgreSQL, Redis, fsspec, httpx, pytest, Ruff, Mypy, Ty.

## Global Constraints

- 本计划只实现 TextProcessor worker、内部 task model 和测试；外部 POST/GET route 留给后续 API 计划。
- Celery 消息只含 `taskId`、`taskType=global_deduplication`、`schemaVersion=1`。
- 首版文档格式仅允许 `.md`、`.txt`、`.json`，全部按 UTF-8 原始文本处理。
- 文本只去 UTF-8 BOM，并把 CRLF/CR 统一成 LF；JSON 不解析或重排。
- Data-Juicer profile 固定且可配置为 `text_exact_minhash_v1`，业务请求不能覆盖。
- `input.jsonl` 不含业务 ID/路径，`mapping.json` 不含正文。
- 最终输出只含 `fileId`、`fileStoragePath`、`groupId`、`keep`。
- 发布禁止覆盖；数据库 prepared 摘要必须先于最终文件发布持久化。
- 默认测试不要求真实 Data-Juicer；真实服务契约测试单独标记 `real_integration`。

---

### Task 1: Worker 配置、错误和 Celery 消息契约

**Files:**
- Modify: `backend/app/core/config.py`
- Create: `backend/app/features/global_deduplication/__init__.py`
- Create: `backend/app/features/global_deduplication/errors.py`
- Create: `backend/app/features/global_deduplication/messages.py`
- Test: `backend/tests/features/global_deduplication/test_config.py`
- Test: `backend/tests/features/global_deduplication/test_messages.py`

**Interfaces:**
- Produces: `GlobalDeduplicationWorkerSettings`.
- Produces: `GlobalDeduplicationErrorCode`, `GlobalDeduplicationProcessingError`.
- Produces: `GlobalDeduplicationMessage.parse(payload) -> GlobalDeduplicationMessage` and `.as_payload()`.

- [x] **Step 1: Write failing configuration and message tests**

```python
def test_global_dedup_defaults_are_bounded(tmp_path: Path) -> None:
    value = GlobalDeduplicationWorkerSettings(staging_root=tmp_path)
    assert value.datajuicer_profile == "text_exact_minhash_v1"
    assert value.max_documents > 0
    assert value.max_manifest_bytes > 0
    assert value.max_document_bytes > 0
    assert value.max_total_bytes >= value.max_document_bytes


def test_message_rejects_wrong_type_and_schema() -> None:
    with pytest.raises(InvalidGlobalDeduplicationMessage):
        GlobalDeduplicationMessage.parse(
            {"taskId": str(uuid.uuid7()), "taskType": "other", "schemaVersion": 1}
        )
```

- [x] **Step 2: Run the tests and verify import/behavior failures**

```powershell
uv run --project backend pytest backend/tests/features/global_deduplication/test_config.py backend/tests/features/global_deduplication/test_messages.py -q
```

- [x] **Step 3: Implement bounded settings and strict message parsing**

```python
class GlobalDeduplicationMessage(BaseModel):
    model_config = ConfigDict(extra="forbid", alias_generator=to_camel)
    task_id: uuid.UUID
    task_type: Literal["global_deduplication"]
    schema_version: Literal[1]

    def as_payload(self) -> dict[str, str | int]:
        return self.model_dump(mode="json", by_alias=True)
```

Add all spec configuration fields under `Settings.GLOBAL_DEDUP_WORKER`, with positive numeric validation, resolved staging root, independently configurable connect/read timeouts and fixed profile validation.

- [x] **Step 4: Run focused tests and static checks**

```powershell
uv run --project backend pytest backend/tests/features/global_deduplication/test_config.py backend/tests/features/global_deduplication/test_messages.py -q
uv run --project backend ruff check backend/app/core/config.py backend/app/features/global_deduplication backend/tests/features/global_deduplication
uv run --project backend mypy backend/app/features/global_deduplication
```

- [x] **Step 5: Commit**

```powershell
git add backend/app/core/config.py backend/app/features/global_deduplication backend/tests/features/global_deduplication
git commit -m "功能：定义全局去重worker契约"
```

---

### Task 2: 输入清单、文档读取与 staging

**Files:**
- Create: `backend/app/features/global_deduplication/models.py`
- Create: `backend/app/features/global_deduplication/input_reader.py`
- Create: `backend/app/features/global_deduplication/staging.py`
- Test: `backend/tests/features/global_deduplication/test_input_reader.py`
- Test: `backend/tests/features/global_deduplication/test_staging.py`

**Interfaces:**
- Produces: `DocumentReference(file_id: str, file_storage_path: str)`.
- Produces: `BoundedUriReader.read(uri: str, max_bytes: int) -> bytes`.
- Produces: `load_manifest(...) -> tuple[DocumentReference, ...]`.
- Produces: `load_document(...) -> NormalizedDocument`.
- Produces: `GlobalDeduplicationStaging.prepare(...) -> PreparedInput`.

- [x] **Step 1: Write failing manifest/document tests**

```python
def test_manifest_ignores_unknown_fields_and_rejects_duplicate_file_id() -> None:
    valid = b'[{"fileId":"1","fileStoragePath":"a.md","ignored":true}]'
    assert load_manifest_bytes(valid, max_documents=2)[0].file_id == "1"
    with pytest.raises(GlobalDeduplicationProcessingError, match="DUPLICATE_FILE_ID"):
        load_manifest_bytes(
            b'[{"fileId":"1","fileStoragePath":"a.md"},'
            b'{"fileId":"1","fileStoragePath":"b.txt"}]',
            max_documents=2,
        )


def test_document_normalization_preserves_json_and_normalizes_newlines() -> None:
    raw = b'\xef\xbb\xbf{"b": 2,\r\n"a": 1}\r'
    assert normalize_document(raw, suffix=".json") == '{"b": 2,\n"a": 1}\n'
```

Cover empty/top-level non-array/invalid UTF-8, missing/blank fields, unknown field discard, document count, `.md/.txt/.json`, unsupported suffix, per-file and cumulative limits.

- [x] **Step 2: Run tests and verify missing implementation failures**

```powershell
uv run --project backend pytest backend/tests/features/global_deduplication/test_input_reader.py backend/tests/features/global_deduplication/test_staging.py -q
```

- [x] **Step 3: Implement bounded URI reads and deterministic staging**

```python
@dataclass(frozen=True)
class GlobalDeduplicationStagingLayout:
    root: Path
    input_jsonl: Path
    mapping_json: Path
    datajuicer_result: Path
    final_result: Path
    manifest: Path

    @classmethod
    def for_task(cls, staging_root: Path, task_id: uuid.UUID) -> Self:
        task_root = staging_root.resolve(strict=False) / str(task_id)
        return cls(
            root=task_root,
            input_jsonl=task_root / "input.jsonl",
            mapping_json=task_root / "mapping.json",
            datajuicer_result=task_root / "datajuicer-result.jsonl",
            final_result=task_root / "final-result.json",
            manifest=task_root / "manifest.json",
        )
```

Use fsspec-backed controlled local/file/http(s)/s3 adapters, bounded chunk reads, atomic `.part` writes, `uid` assignment by manifest order, compact JSONL, and SHA-256 metadata. Reuse existing complete staging only when manifest/input/mapping digests all match.

- [x] **Step 4: Verify information separation and limits**

```powershell
uv run --project backend pytest backend/tests/features/global_deduplication/test_input_reader.py backend/tests/features/global_deduplication/test_staging.py -q
uv run --project backend mypy backend/app/features/global_deduplication/input_reader.py backend/app/features/global_deduplication/staging.py
```

- [x] **Step 5: Commit**

```powershell
git add backend/app/features/global_deduplication backend/tests/features/global_deduplication
git commit -m "功能：准备全局去重输入staging"
```

---

### Task 3: Data-Juicer HTTP adapter

**Files:**
- Create: `backend/app/features/global_deduplication/adapters/__init__.py`
- Create: `backend/app/features/global_deduplication/adapters/datajuicer.py`
- Test: `backend/tests/features/global_deduplication/test_datajuicer_adapter.py`

**Interfaces:**
- Produces: `DataJuicerAdapter.submit(request: DataJuicerSubmitRequest) -> DataJuicerSubmission`.
- Produces: `DataJuicerAdapter.get_job(job_id: UUID) -> DataJuicerJob`.

- [x] **Step 1: Write failing MockTransport contract tests**

```python
def test_submit_uses_task_id_as_request_id() -> None:
    submission = adapter.submit(
        DataJuicerSubmitRequest(
            request_id=task_id,
            input_path=input_path,
            output_path=output_path,
            profile="text_exact_minhash_v1",
        )
    )
    assert submission.request_id == str(task_id)
    assert submission.status in {"pending", "queued"}
```

Test idempotent response, 409, unsupported profile, clear 4xx, connect failure, pre-response timeout as uncertain, malformed JSON, mismatched request/job/profile/output path, invalid progress and invalid SHA-256.

- [x] **Step 2: Run tests and verify missing adapter failures**

```powershell
uv run --project backend pytest backend/tests/features/global_deduplication/test_datajuicer_adapter.py -q
```

- [x] **Step 3: Implement strict Pydantic response parsing and error mapping**

```python
class DataJuicerAdapter:
    def submit(self, request: DataJuicerSubmitRequest) -> DataJuicerSubmission:
        response = self._client.post("/v1/jobs", json=request.to_public_json())
        return self._parse_submission(response, request)

    def get_job(self, job_id: uuid.UUID) -> DataJuicerJob:
        response = self._client.get(f"/v1/jobs/{job_id}")
        return self._parse_job(response, expected_job_id=job_id)
```

Never expose response bodies in `safe_message`; configure connect and read timeout separately.

- [x] **Step 4: Run focused tests and static checks**

```powershell
uv run --project backend pytest backend/tests/features/global_deduplication/test_datajuicer_adapter.py -q
uv run --project backend ruff check backend/app/features/global_deduplication/adapters backend/tests/features/global_deduplication/test_datajuicer_adapter.py
uv run --project backend mypy backend/app/features/global_deduplication/adapters
```

- [x] **Step 5: Commit**

```powershell
git add backend/app/features/global_deduplication/adapters backend/tests/features/global_deduplication/test_datajuicer_adapter.py
git commit -m "功能：接入Data-Juicer任务接口"
```

---

### Task 4: 外部结果校验、业务映射与禁止覆盖发布

**Files:**
- Create: `backend/app/features/global_deduplication/result_mapper.py`
- Create: `backend/app/features/global_deduplication/publisher.py`
- Test: `backend/tests/features/global_deduplication/test_result_mapper.py`
- Test: `backend/tests/features/global_deduplication/test_publisher.py`

**Interfaces:**
- Produces: `validate_processor_output(path, expected_uids, expected_sha256) -> tuple[ClusterDecision, ...]`.
- Produces: `map_business_result(task_id, mapping, decisions) -> list[BusinessResult]`.
- Produces: `FinalResultPublisher.prepare(...) -> PreparedFinalResult`.
- Produces: `FinalResultPublisher.publish(prepared, target, allow_recovery) -> PublishedFinalResult`.

- [ ] **Step 1: Write failing invariant and mapping tests**

```python
def test_group_id_is_task_scoped_and_stable_under_result_reordering() -> None:
    first = map_business_result(task_id, mapping, decisions)
    repeated = map_business_result(task_id, mapping, tuple(reversed(decisions)))
    assert first == repeated
    assert first[0].group_id == first[1].group_id
    assert first[0].keep is True
    assert first[1].keep is False
    assert first[2].group_id is None and first[2].keep is True
```

Reject digest mismatch, unknown fields, uid mismatch/duplicate/negative, singleton invariant, one-member cluster, multiple/no representative and mixed/unknown method. Assert serialized objects have exactly four public fields.

- [ ] **Step 2: Run tests and verify failures**

```powershell
uv run --project backend pytest backend/tests/features/global_deduplication/test_result_mapper.py backend/tests/features/global_deduplication/test_publisher.py -q
```

- [ ] **Step 3: Implement UUIDv5 mapping and crash-safe publication**

```python
def business_group_id(task_id: uuid.UUID, member_uids: Collection[int]) -> uuid.UUID:
    name = ",".join(str(uid) for uid in sorted(member_uids))
    return uuid.uuid5(task_id, name)
```

Prepare compact UTF-8 JSON at `final-result.json.part`, fsync, parse back, compute SHA-256, then publish with hard-link/create-exclusive semantics. Recovery may accept an existing target only when its SHA-256 equals the persisted prepared digest.

- [ ] **Step 4: Run focused tests and static checks**

```powershell
uv run --project backend pytest backend/tests/features/global_deduplication/test_result_mapper.py backend/tests/features/global_deduplication/test_publisher.py -q
uv run --project backend mypy backend/app/features/global_deduplication/result_mapper.py backend/app/features/global_deduplication/publisher.py
```

- [ ] **Step 5: Commit**

```powershell
git add backend/app/features/global_deduplication backend/tests/features/global_deduplication
git commit -m "功能：映射并发布全局去重结果"
```

---

### Task 5: 独立任务表、状态机、repository 和 migration

**Files:**
- Create: `backend/app/features/global_deduplication/task_models.py`
- Create: `backend/app/features/global_deduplication/state_machine.py`
- Create: `backend/app/features/global_deduplication/repository.py`
- Create: `backend/app/alembic/versions/20260731_01_add_global_deduplication_tasks.py`
- Test: `backend/tests/features/global_deduplication/test_state_machine.py`
- Test: `backend/tests/features/global_deduplication/test_repository.py`

**Interfaces:**
- Produces: `GlobalDeduplicationTask` with unique `(caller_id, session_id)` and all worker/recovery fields from the spec.
- Produces: conditional methods `acquire_submit`, `acquire_poll`, `save_prepared_input`, `save_external_job`, `save_prepared_output`, `mark_succeeded`, `mark_failed`, and recovery queries.

- [ ] **Step 1: Write failing state and real PostgreSQL repository tests**

```python
def test_only_one_poll_worker_acquires_lease(repository, running_task) -> None:
    first = repository.acquire_poll(running_task.id, now=NOW, lease_seconds=30)
    second = repository.acquire_poll(running_task.id, now=NOW, lease_seconds=30)
    assert first is not None
    assert second is None
```

Test legal transitions, idempotent terminal messages, submit lease expiry, prepared/external/final metadata persistence, due-poll and published-before-DB recovery selection.

- [ ] **Step 2: Run tests and verify missing model/migration failures**

```powershell
uv run --project backend pytest backend/tests/features/global_deduplication/test_state_machine.py backend/tests/features/global_deduplication/test_repository.py -q
```

- [ ] **Step 3: Implement model, conditional updates and migration**

Use native timezone-aware timestamps, JSON only for bounded external progress/result metadata, indexed status/lease/next-poll columns, and compare-and-update SQL so stale lease holders cannot overwrite new state.

- [ ] **Step 4: Run migration and repository gates**

```powershell
uv run --project backend alembic upgrade head
uv run --project backend alembic check
uv run --project backend pytest backend/tests/features/global_deduplication/test_state_machine.py backend/tests/features/global_deduplication/test_repository.py -q
```

- [ ] **Step 5: Commit**

```powershell
git add backend/app/features/global_deduplication backend/app/alembic/versions/20260731_01_add_global_deduplication_tasks.py backend/tests/features/global_deduplication
git commit -m "功能：持久化全局去重任务状态"
```

---

### Task 6: Submit orchestration

**Files:**
- Create: `backend/app/features/global_deduplication/orchestration.py`
- Test: `backend/tests/features/global_deduplication/test_submit_orchestration.py`

**Interfaces:**
- Consumes: repository, input/staging and `DataJuicerAdapter`.
- Produces: `GlobalDeduplicationOrchestrator.submit(task_id: UUID) -> None`.
- Consumes scheduler method `enqueue_poll(task_id, countdown)`.

- [ ] **Step 1: Write failing submit orchestration tests**

```python
def test_submit_prepares_all_documents_before_external_job(orchestrator, fake_adapter):
    orchestrator.submit(task_id)
    request = fake_adapter.submissions.single()
    assert request.request_id == task_id
    assert request.input_path.name == "input.jsonl"
    assert request.output_path.name == "datajuicer-result.jsonl"
```

Cover duplicate submit, deterministic input failure/no external call, output preflight conflict, complete staging reuse, submission uncertain, idempotent retry and lost lease.

- [ ] **Step 2: Run tests and verify missing orchestration failures**

```powershell
uv run --project backend pytest backend/tests/features/global_deduplication/test_submit_orchestration.py -q
```

- [ ] **Step 3: Implement acquire → prepare → submit → persist → schedule**

```python
def submit(self, task_id: uuid.UUID) -> None:
    task = self._repository.acquire_submit(task_id, now=self._now())
    if task is None:
        return
    prepared = self._prepare_or_reuse(task)
    submission = self._adapter.submit(self._request(task, prepared))
    if self._repository.save_external_job(task.id, submission):
        self._scheduler.enqueue_poll(task.id, countdown=self._initial_poll_delay)
```

Persist progress phases `validating_input`, `loading_documents`, then `deduplicating`; deterministic errors fail without retry, uncertain submissions remain recoverable with the same task ID.

- [ ] **Step 4: Run focused and static gates**

```powershell
uv run --project backend pytest backend/tests/features/global_deduplication/test_submit_orchestration.py -q
uv run --project backend mypy backend/app/features/global_deduplication/orchestration.py
```

- [ ] **Step 5: Commit**

```powershell
git add backend/app/features/global_deduplication/orchestration.py backend/tests/features/global_deduplication/test_submit_orchestration.py
git commit -m "功能：编排全局去重任务提交"
```

---

### Task 7: Poll、finalize 和恢复扫描

**Files:**
- Modify: `backend/app/features/global_deduplication/orchestration.py`
- Test: `backend/tests/features/global_deduplication/test_poll_orchestration.py`
- Test: `backend/tests/features/global_deduplication/test_recovery.py`

**Interfaces:**
- Produces: `poll(task_id: UUID) -> None`.
- Produces: `recover() -> RecoverySummary`.
- Consumes scheduler methods `enqueue_submit` and `enqueue_poll`.

- [ ] **Step 1: Write failing poll/finalize/recovery tests**

```python
def test_successful_poll_finalizes_in_same_task(orchestrator, target_path) -> None:
    orchestrator.poll(task_id)
    assert target_path.is_file()
    assert repository.get(task_id).status == GlobalDeduplicationTaskStatus.SUCCEEDED


def test_recovery_only_redispatches_due_work(orchestrator, scheduler) -> None:
    summary = orchestrator.recover()
    assert summary.poll_dispatched == 1
    assert scheduler.poll_ids == [task_id]
```

Cover pending/queued lost dispatch, expired submit lease, due poll, external running/failed/cancelled/not-found/timeout, transient poll error, invalid output, competing polls, publish conflict, published-before-DB digest recovery.

- [ ] **Step 2: Run tests and verify behavior failures**

```powershell
uv run --project backend pytest backend/tests/features/global_deduplication/test_poll_orchestration.py backend/tests/features/global_deduplication/test_recovery.py -q
```

- [ ] **Step 3: Implement poll lease, configurable backoff and inline finalize**

Keep public phase `deduplicating` for all nonterminal external states. On success update `publishing_result`, validate external response/path/digest, map output, persist prepared digest, publish, persist result metadata and transition to `completed`.

- [ ] **Step 4: Implement recovery as dispatch-only scanning**

Recovery must never read documents, call Data-Juicer or publish files inside Beat. Re-submit uncertain jobs with `requestId=taskId`; only fail `PROCESSOR_JOB_NOT_FOUND` after the specified single idempotent recovery attempt.

- [ ] **Step 5: Run focused and feature gates**

```powershell
uv run --project backend pytest backend/tests/features/global_deduplication -q
uv run --project backend ruff check backend/app/features/global_deduplication backend/tests/features/global_deduplication
uv run --project backend mypy backend/app/features/global_deduplication
uv run --project backend ty check backend/app/features/global_deduplication
```

- [ ] **Step 6: Commit**

```powershell
git add backend/app/features/global_deduplication backend/tests/features/global_deduplication
git commit -m "功能：完成全局去重轮询与恢复"
```

---

### Task 8: Celery task 注册、Beat 与 worker 集成测试

**Files:**
- Create: `backend/app/features/global_deduplication/celery_tasks.py`
- Modify: `backend/app/core/celery_app.py`
- Test: `backend/tests/features/global_deduplication/test_celery_tasks.py`
- Test: `backend/tests/integration/global_deduplication/test_worker_pipeline.py`

**Interfaces:**
- Produces Celery tasks `global_deduplication.submit`, `global_deduplication.poll`, `global_deduplication.recover`.
- Registers feature module in the existing TextProcessor Celery app and adds configurable Beat recovery schedule.

- [ ] **Step 1: Write failing Celery registration and integration tests**

```python
def test_celery_tasks_only_accept_minimal_message() -> None:
    payload = message(task_id)
    submit_global_deduplication_task.run(**payload)
    orchestrator.submit.assert_called_once_with(task_id)


def test_worker_pipeline_stages_submits_polls_and_publishes(
    db_session, fake_datajuicer_server, local_documents, target_path
) -> None:
    submit_global_deduplication_task.run(**message(task_id))
    poll_global_deduplication_task.run(**message(task_id))
    assert json.loads(target_path.read_text(encoding="utf-8")) == expected_result
```

Test malformed messages, finite transient Celery retry, repeated submit/poll convergence and recovery dispatch. Use PostgreSQL plus local files and fake HTTP Data-Juicer, not in-memory state substitutes.

- [ ] **Step 2: Run tests and verify missing task registration**

```powershell
uv run --project backend pytest backend/tests/features/global_deduplication/test_celery_tasks.py backend/tests/integration/global_deduplication/test_worker_pipeline.py -q
```

- [ ] **Step 3: Implement thin Celery entrypoints**

```python
@celery_app.task(
    bind=True,
    name="global_deduplication.submit",
    autoretry_for=(RetryableGlobalDeduplicationError,),
    retry_backoff=True,
    retry_kwargs={"max_retries": 3},
)
def submit_global_deduplication_task(self: Task, **payload: object) -> None:
    message = GlobalDeduplicationMessage.parse(payload)
    with Session(engine) as session:
        build_orchestrator(session).submit(message.task_id)
```

`poll` schedules future polls explicitly; `recover` returns a bounded summary and never carries full task parameters.

- [ ] **Step 4: Run all worker gates**

```powershell
uv run --project backend pytest backend/tests/features/global_deduplication backend/tests/integration/global_deduplication -q
uv run --project backend ruff check backend/app backend/tests/features/global_deduplication backend/tests/integration/global_deduplication
uv run --project backend mypy backend/app
uv run --project backend ty check backend/app
uv run --project backend pytest backend/tests -q
```

- [ ] **Step 5: Reconcile spec coverage**

Verify tests explicitly cover every item in design sections 17.1–17.3. Record real Data-Juicer end-to-end items from 17.4 as pending for the later three-module integration plan; do not claim them from fake-service tests.

- [ ] **Step 6: Commit**

```powershell
git add backend/app backend/tests docs/superpowers/plans/2026-07-31-global-deduplication-worker.md
git commit -m "功能：接入全局去重Celery worker"
```

---

## Completion Evidence

Worker 阶段完成必须同时具备：

- 独立任务表迁移和 PostgreSQL repository 测试；
- 清单、三种文本格式、限制、BOM/换行及 staging 分离测试；
- Data-Juicer fake HTTP 提交、多轮轮询、超时和非法响应测试；
- 完整成员校验、稳定 UUIDv5、四字段结果和禁止覆盖发布测试；
- submit/poll 租约、重复消息、恢复与崩溃窗口测试；
- Ruff、Mypy、Ty、backend 默认 pytest 通过；
- 真实 Data-Juicer 和双 worker 链路明确保留到三模块集成阶段。
