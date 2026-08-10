# Global Deduplication Directory Input Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace JSON manifest input/output with a local batch directory that moves duplicate text files from `original/` to flat `duplicate/`.

**Architecture:** Persist one canonical batch directory per task. A scanner produces the existing Data-Juicer input from supported regular files below `original/`; after the processor chooses representatives, a durable staging move-manifest makes individual no-overwrite moves recoverable. PostgreSQL stores only the public summary.

**Tech Stack:** FastAPI, Pydantic v2, SQLModel/Alembic, Celery, PostgreSQL, Python pathlib/os, pytest, Ruff, Mypy, Pyright and ty.

## Global Constraints

- Keep asynchronous authenticated `POST/GET /api/v1/global-deduplication/tasks` and `(callerId, sessionId)` idempotency.
- POST accepts only absolute local or `file://` `inputPath`; reject legacy JSON fields, HTTP/S3, and `targetPath`.
- Require existing `original/` and `duplicate/`; recursively use only regular `.md`, `.txt`, `.json`, silently skipping other types and never following symlinks.
- Keep the existing processor's `keep=false` decision; move to flat `duplicate/<filename>` without overwrite or renaming.
- Individual movement errors enter `moveFailures`; completed scan/dedup always yields `succeeded`.
- Persist decisions and move state in staging until terminal cleanup; recovery must not rescan or recalculate decisions.
- Return only relative paths publicly. Run Apifox schema get/validate before write and read back after synchronization.

---

### Task 1: Replace API contract and persisted task parameters

**Files:**
- Modify: `backend/app/features/global_deduplication/{schemas,request_policy,service,routes,task_models,repository}.py`
- Create: `backend/app/alembic/versions/20260810_01_global_deduplication_directory_input.py`
- Modify: `backend/tests/features/global_deduplication/{test_api_contract,test_request_policy,test_service,test_routes,test_repository}.py`

**Interfaces:** Produce `GlobalDeduplicationTaskCreate(session_id, input_path)`, canonical `ValidatedGlobalDeduplicationRequest`, and public `result={totalFiles,uniqueFiles,movedDuplicates,moveFailures}`.

- [ ] **Step 1: Write failing contract tests**

```python
def test_create_accepts_only_local_batch_input(tmp_path: Path) -> None:
    batch = tmp_path / "batch"
    (batch / "original").mkdir(parents=True)
    (batch / "duplicate").mkdir()
    value = GlobalDeduplicationTaskCreate.model_validate(
        {"sessionId": "batch-1", "inputPath": str(batch)}
    )
    assert value.model_dump(by_alias=True) == {
        "sessionId": "batch-1", "inputPath": str(batch)
    }
    with pytest.raises(ValidationError):
        GlobalDeduplicationTaskCreate.model_validate(
            {"sessionId": "batch-1", "inputJsonPath": str(batch)}
        )
```

Cover missing children, `file://` canonicalization, legacy `422`, different-path idempotency conflict, and safe GET summary.

- [ ] **Step 2: Confirm the tests fail**

Run: `uv run --project backend pytest backend/tests/features/global_deduplication/test_api_contract.py backend/tests/features/global_deduplication/test_request_policy.py backend/tests/features/global_deduplication/test_service.py backend/tests/features/global_deduplication/test_routes.py backend/tests/features/global_deduplication/test_repository.py -q`

Expected: FAIL because the code currently requires `inputJsonPath` and `targetPath`.

- [ ] **Step 3: Implement strict local contract and migration**

```python
class GlobalDeduplicationTaskCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    session_id: NonBlank = Field(alias="sessionId", max_length=128)
    input_path: NonBlank = Field(alias="inputPath", max_length=4096)

def request_fingerprint(*, input_path: str) -> str:
    body = json.dumps({"input_path": input_path}, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(body.encode()).hexdigest()
```

Resolve the batch strictly, require direct non-symlink directory children, persist `input_path`, and type-check `result_metadata` before route serialization. The Alembic revision adds `input_path`, safely fails nonterminal legacy rows, then removes old manifest/output columns. Preserve no absolute path in result metadata.

- [ ] **Step 4: Verify API and migration**

Run: `uv run --project backend pytest backend/tests/features/global_deduplication/test_api_contract.py backend/tests/features/global_deduplication/test_request_policy.py backend/tests/features/global_deduplication/test_service.py backend/tests/features/global_deduplication/test_routes.py backend/tests/features/global_deduplication/test_repository.py -q && uv run --project backend alembic -c backend/alembic.ini upgrade head && uv run --project backend alembic -c backend/alembic.ini downgrade -1 && uv run --project backend alembic -c backend/alembic.ini upgrade head`

Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add backend/app/features/global_deduplication backend/app/alembic/versions/20260810_01_global_deduplication_directory_input.py backend/tests/features/global_deduplication
git commit -m "feat：全局去重改为目录任务接口"
```

### Task 2: Create scanner, durable move manifest, and mover

**Files:**
- Create: `backend/app/features/global_deduplication/directory_input.py`
- Create: `backend/app/features/global_deduplication/file_mover.py`
- Modify: `backend/app/features/global_deduplication/{staging,models,errors}.py`
- Create: `backend/tests/features/global_deduplication/{test_directory_input,test_file_mover}.py`
- Modify: `backend/tests/features/global_deduplication/test_staging.py`

**Interfaces:** `scan_batch(path: Path) -> ScannedBatch`; `DuplicateFileMover.move_pending(manifest) -> MoveSummary`; staging `move_manifest_json` holds relative path, source SHA-256, destination filename and `pending|moved|failed` states.

- [ ] **Step 1: Write failing scan/move tests**

```python
def test_scanner_recurses_sorts_and_skips_unsupported(batch: Path) -> None:
    write(batch / "original" / "z.txt", "z")
    write(batch / "original" / "nested" / "a.md", "a")
    write(batch / "original" / "image.png", "x")
    assert [d.relative_path.as_posix() for d in scan_batch(batch).documents] == ["nested/a.md", "z.txt"]

def test_existing_flat_destination_keeps_source(batch: Path) -> None:
    write(batch / "original" / "nested" / "same.txt", "duplicate")
    write(batch / "duplicate" / "same.txt", "existing")
    assert mover.move_pending(manifest_for("nested/same.txt")).failures[0].code == "OUTPUT_CONFLICT"
    assert (batch / "original" / "nested" / "same.txt").exists()
```

Also cover symlink skip, source read failure, `EXDEV` fallback, injected error, and same-digest destination recovery.

- [ ] **Step 2: Confirm the primitive tests fail**

Run: `uv run --project backend pytest backend/tests/features/global_deduplication/test_directory_input.py backend/tests/features/global_deduplication/test_file_mover.py backend/tests/features/global_deduplication/test_staging.py -q`

Expected: FAIL because scanner, mover and manifest are absent.

- [ ] **Step 3: Implement safe, recoverable file operations**

Use `lstat()` plus `stat.S_ISREG`, sort POSIX relative paths, and use relative path as internal file ID. Atomically replace the staging manifest after each result. Create destination exclusively; on `EXDEV`, copy to task-unique `.part`, fsync, verify digest, atomically create destination, then delete source. A preexisting equal digest marks moved; any other preexisting destination becomes `OUTPUT_CONFLICT`.

- [ ] **Step 4: Verify primitives**

Run: `uv run --project backend pytest backend/tests/features/global_deduplication/test_directory_input.py backend/tests/features/global_deduplication/test_file_mover.py backend/tests/features/global_deduplication/test_staging.py -q && uv run --project backend ruff check backend/app/features/global_deduplication backend/tests/features/global_deduplication && uv run --project backend mypy backend/app/features/global_deduplication`

Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add backend/app/features/global_deduplication/directory_input.py backend/app/features/global_deduplication/file_mover.py backend/app/features/global_deduplication/staging.py backend/app/features/global_deduplication/models.py backend/app/features/global_deduplication/errors.py backend/tests/features/global_deduplication/test_directory_input.py backend/tests/features/global_deduplication/test_file_mover.py backend/tests/features/global_deduplication/test_staging.py
git commit -m "feat：增加全局去重目录扫描和文件迁移"
```

### Task 3: Rewire worker finalization and recovery

**Files:**
- Modify: `backend/app/features/global_deduplication/{orchestration,celery_tasks}.py`
- Delete: `backend/app/features/global_deduplication/publisher.py`
- Modify: `backend/tests/features/global_deduplication/{test_submit_orchestration,test_poll_orchestration,test_recovery}.py`
- Delete: `backend/tests/features/global_deduplication/test_publisher.py`

**Interfaces:** submission scans `Path(task.input_path)`; finalization validates processor output, creates/loads move manifest, runs mover, then persists compact summary.

- [ ] **Step 1: Write failing lifecycle tests**

```python
def test_finalize_succeeds_when_one_move_conflicts(tmp_path: Path) -> None:
    task, job = stage_duplicate_decision(tmp_path)
    write(Path(task.input_path) / "duplicate" / "duplicate.txt", "old")
    orchestrator.poll(task.id)
    saved = repository.get(task.id)
    assert saved.status is GlobalDeduplicationTaskStatus.SUCCEEDED
    assert saved.result_metadata["move_failures"][0]["code"] == "OUTPUT_CONFLICT"
```

Add interruption-after-first-manifest-update coverage; retry must not rescan or resubmit Data-Juicer and must not repeat completed moves.

- [ ] **Step 2: Confirm old worker tests fail**

Run: `uv run --project backend pytest backend/tests/features/global_deduplication/test_submit_orchestration.py backend/tests/features/global_deduplication/test_poll_orchestration.py backend/tests/features/global_deduplication/test_recovery.py -q`

Expected: FAIL because current flow reads a manifest and publishes `final-result.json`.

- [ ] **Step 3: Implement directory finalization**

Replace `read_manifest/load_manifest_bytes/load_documents` with scanner output. In `_finalize`, turn only `keep=false` decisions into one staged manifest, reload it on retry, run mover, and save:

```python
result_metadata = {
    "total_files": total_files,
    "unique_files": total_files - duplicate_count,
    "moved_duplicates": summary.moved_duplicates,
    "move_failures": [item.to_dict() for item in summary.failures],
}
```

Remove `FinalResultPublisher`, final result JSON and global-dedup-only output settings. Retain polling, finite retry, lease and missing-job recovery behavior.

- [ ] **Step 4: Verify full feature checks**

Run: `uv run --project backend pytest backend/tests/features/global_deduplication -q && uv run --project backend ruff check backend/app/features/global_deduplication backend/tests/features/global_deduplication && uv run --project backend mypy backend/app/features/global_deduplication && uv run --project backend pyright backend/app/features/global_deduplication && uv run --project backend ty check backend/app/features/global_deduplication`

Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add backend/app/features/global_deduplication backend/tests/features/global_deduplication
git commit -m "feat：全局去重完成后迁移重复文件"
```

### Task 4: Prove real pipeline behavior and synchronize Apifox

**Files:**
- Modify: `backend/tests/integration/global_deduplication/test_global_deduplication_worker_pipeline.py`
- Modify: `docs/runbooks/datajuicer-service.md`
- Modify: `docs/reports/2026-08-10-four-apis-local-path-137-acceptance.md` if it documents the retired payload

- [ ] **Step 1: Write failing end-to-end tests**

```python
def test_directory_pipeline_moves_duplicate_and_returns_safe_summary(runtime: Runtime) -> None:
    batch = runtime.make_batch({"first.txt": "same", "second.txt": "same", "skip.bin": "x"})
    accepted = runtime.post_global_dedup({"sessionId": "dir-1", "inputPath": str(batch)})
    completed = runtime.wait_for_task(accepted["taskId"])
    assert completed["result"] == {
        "totalFiles": 2, "uniqueFiles": 1, "movedDuplicates": 1, "moveFailures": []
    }
```

Cover legacy payload rejection, flat collision with optimistic success, worker crash during moves, duplicate message recovery, and terminal staging cleanup.

- [ ] **Step 2: Confirm integration failures before wiring**

Run: `uv run --project backend pytest backend/tests/integration/global_deduplication -q`

Expected: FAIL until directory input is complete.

- [ ] **Step 3: Update fixtures, docs and Apifox**

Make fixtures create `batch/original` and `batch/duplicate` and assert physical locations plus safe GET data. Replace runbook JSON examples. Use installed Apifox CLI schema `get` and `validate` for project `8681977`, export FastAPI OpenAPI, synchronize Chinese field descriptions, then read back endpoint and related smoke case without printing credentials.

- [ ] **Step 4: Run final validation and commit**

Run: `uv run --project backend pytest backend/tests/features/global_deduplication backend/tests/integration/global_deduplication -q && uv run --project backend ruff check backend/app/features/global_deduplication backend/tests/features/global_deduplication backend/tests/integration/global_deduplication && uv run --project backend mypy backend/app/features/global_deduplication && uv run --project backend pyright backend/app/features/global_deduplication && uv run --project backend ty check backend/app/features/global_deduplication`

Expected: all pass. Run a dedicated real-stack verifier if present; otherwise retain the real integration output as evidence.

```bash
git add backend/tests/integration/global_deduplication docs/runbooks/datajuicer-service.md docs/reports/2026-08-10-four-apis-local-path-137-acceptance.md
git commit -m "test：验证全局去重目录迁移流程"
```
