# 结构化提取私有 Manifest 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 取消输出目录中的共享 `manifest.json`，改为任务私有 staging manifest，并在终态后安全清理，使同目录不同 `targetPath` 可连续或并发发布。

**Architecture:** `StagingLayout` 为每个 task ID 提供独立 `manifest` 路径；发布器只处理 Markdown 文件级冲突。编排器在发布前写入私有 manifest，以 PostgreSQL 为状态权威，成功或失败转为终态后清理 staging；恢复流程回收终态残留 staging。

**Tech Stack:** Python 3.14、FastAPI、SQLModel/PostgreSQL、pytest、fsspec、现有 `AtomicPublisher` 与 `ExtractionOrchestrator`。

## Global Constraints

- API 请求和响应结构保持不变。
- 最终输出目录只包含调用方指定的 Markdown，不发布 manifest、隐藏 sidecar 或临时文件。
- 目标 Markdown 本身已存在且不满足本任务摘要恢复时，继续返回 `OUTPUT_CONFLICT`。
- PostgreSQL 是任务状态权威；非终态 staging 可用于恢复，终态 staging 必须清理。
- 清理只能针对校验后的 `{staging_root}/{task_id}`，失败只记录告警，不改写业务终态。
- 不迁移或删除历史输出中的旧版 `manifest.json`，新任务直接忽略它。

---

### Task 1: 文件级冲突与任务私有 Manifest 布局

**Files:**
- Modify: `backend/app/features/structured_extraction/staging.py`
- Modify: `backend/app/features/structured_extraction/processors/publisher.py`
- Test: `backend/tests/features/structured_extraction/test_staging.py`
- Test: `backend/tests/features/structured_extraction/test_publisher.py`

**Interfaces:**
- Produces: `StagingLayout.manifest: Path`，固定为 `{staging_root}/{task_id}/manifest.json`。
- Produces: `AtomicPublisher.ensure_target_available(target: Path) -> Path`，只以目标 Markdown 是否存在判断冲突。
- Removes: 对外 `AtomicPublisher.publish_manifest(...)`，manifest 不再经过输出发布器。

- [ ] **Step 1: 写入失败测试**

  在 staging 测试中断言两个 task ID 的 `manifest` 不同且均位于各自 task root；在 publisher 测试中把旧版 `manifest.json` 放进输出目录，断言 `ensure_target_available(output / "another.md")` 成功，同时保留已有目标 Markdown 冲突测试。

- [ ] **Step 2: 验证 RED**

  运行：

  ```powershell
  uv run --project backend pytest backend/tests/features/structured_extraction/test_staging.py backend/tests/features/structured_extraction/test_publisher.py -q
  ```

  预期：私有 manifest 属性缺失，旧版 manifest 仍触发 `OUTPUT_CONFLICT`。

- [ ] **Step 3: 最小实现**

  给 `StagingLayout` 增加 `manifest` 字段并由 `for_task()` 设置为 `task_root / "manifest.json"`。把 `ensure_target_available()` 改为只检查 `normalized_target.exists()`；删除只为公开 JSON 发布服务的 `publish_manifest()` 和 `_validate_target(..., manifest=True)` 分支，使发布器只接受 `.md`。

- [ ] **Step 4: 验证 GREEN**

  重跑 Task 1 测试，并执行：

  ```powershell
  uv run --project backend ruff check backend/app/features/structured_extraction/staging.py backend/app/features/structured_extraction/processors/publisher.py backend/tests/features/structured_extraction/test_staging.py backend/tests/features/structured_extraction/test_publisher.py
  ```

- [ ] **Step 5: 提交**

  ```powershell
  git add backend/app/features/structured_extraction/staging.py backend/app/features/structured_extraction/processors/publisher.py backend/tests/features/structured_extraction/test_staging.py backend/tests/features/structured_extraction/test_publisher.py
  git commit -m "修复：按目标文件判断结构化提取输出冲突"
  ```

### Task 2: 编排器私有 Manifest 与终态清理

**Files:**
- Modify: `backend/app/features/structured_extraction/orchestration.py`
- Modify: `backend/app/features/structured_extraction/repository.py`
- Test: `backend/tests/features/structured_extraction/test_orchestration.py`
- Test: `backend/tests/features/structured_extraction/test_repository.py`
- Test: `backend/tests/integration/structured_extraction/test_worker_pipeline.py`

**Interfaces:**
- Produces: `ExtractionTaskRepository.list_terminal_with_staging(limit: int) -> list[ExtractionTask]`。
- Produces: `ExtractionTaskRepository.clear_terminal_staging(task_id: UUID) -> bool`，仅对终态且 `staging_path` 非空的任务清空数据库引用。
- Produces: `ExtractionOrchestrator._cleanup_terminal_staging(task: ExtractionTask) -> bool`，安全删除 task staging，并在成功后清空数据库引用。

- [ ] **Step 1: 写入失败测试**

  修改 plain-text 编排测试，断言输出父目录没有 `manifest.json`、任务 staging 已删除、`result_metadata` 只保留 `target_path` 和 `output_size_bytes`。增加同目录两个不同目标连续成功测试。增加失败任务转终态后 staging 被清理的测试。repository 测试覆盖只枚举终态且有 staging 引用的记录，以及只清空终态记录。

- [ ] **Step 2: 验证 RED**

  运行：

  ```powershell
  uv run --project backend pytest backend/tests/features/structured_extraction/test_orchestration.py backend/tests/features/structured_extraction/test_repository.py -q
  ```

  预期：现有代码仍公开 manifest、结果元数据仍含 manifest 字段，且 repository 清理接口不存在。

- [ ] **Step 3: 实现私有 manifest 和清理顺序**

  `_publish_artifact()` 在 `layout.manifest` 写入恢复元数据，不再调用 `publish_manifest()`；publication 只记录原子文件发布，不写公开 manifest 路径。成功 transition 的 `result_metadata` 仅保存目标路径与输出大小，随后调用统一终态清理。

  `_fail_queued()` 和 `_fail_running()` 仅在 transition 成功后调用统一终态清理。清理方法由服务端配置重新推导 `StagingLayout`，校验数据库 `staging_path` 与推导路径一致后删除；`OSError` 或安全校验失败只使用 module logger 记录 warning。删除成功后调用 `clear_terminal_staging()`。

- [ ] **Step 4: 实现终态残留恢复**

  repository 使用明确终态集合查询 `staging_path IS NOT NULL` 的任务；`recover()` 在既有非终态恢复之后遍历这些任务并调用统一清理。这样覆盖“数据库终态已提交但进程在文件清理前崩溃”的窗口。

- [ ] **Step 5: 验证 GREEN 和恢复行为**

  重跑 Task 2 单元测试。数据库测试环境可用时再运行：

  ```powershell
  uv run --project backend pytest backend/tests/integration/structured_extraction/test_worker_pipeline.py -q
  ```

  重点确认同目标并发仍只有一个成功、发布后数据库 transition 崩溃仍按摘要恢复、不同目标互不冲突。

- [ ] **Step 6: 提交**

  ```powershell
  git add backend/app/features/structured_extraction/orchestration.py backend/app/features/structured_extraction/repository.py backend/tests/features/structured_extraction/test_orchestration.py backend/tests/features/structured_extraction/test_repository.py backend/tests/integration/structured_extraction/test_worker_pipeline.py
  git commit -m "修复：将结构化提取 manifest 限制在任务 staging"
  ```

### Task 3: 契约文档与完整质量验证

**Files:**
- Modify: `AGENTS.md`
- Modify: `docs/superpowers/specs/2026-08-07-structured-extraction-production-formats-design.md`
- Modify: `docs/runbooks/structured-extraction-137-production.md`
- Modify: `docs/reports/2026-08-07-structured-extraction-137-acceptance.md`

**Interfaces:**
- Consumes: Task 1 和 Task 2 已验证的最终行为。
- Produces: 统一的项目规则、生产运行手册与历史验收说明，明确新版本不再发布 manifest。

- [ ] **Step 1: 更新长期契约**

  将根 `AGENTS.md` 的输出规则改为：后台任务使用独立 staging，manifest 仅在非终态期间作为内部恢复数据，最终输出不包含 manifest。生产格式设计与 runbook 同步删除公开 manifest 验收要求。

- [ ] **Step 2: 标注历史报告边界**

  保留 2026-08-07 报告的历史事实，但增加说明：该报告记录旧版本公开 manifest 行为，自本次版本起验收改为仅检查 Markdown、数据库摘要和终态 staging 清理。

- [ ] **Step 3: 运行质量门禁**

  ```powershell
  uv run --project backend ruff check backend/app/features/structured_extraction backend/tests/features/structured_extraction backend/tests/integration/structured_extraction
  uv run --project backend ty check backend/app/features/structured_extraction
  uv run --project backend pytest backend/tests/features/structured_extraction -q
  git diff --check
  ```

  PostgreSQL 测试栈可用时追加完整 integration 测试；若仍不可用，最终报告必须明确 fixture 初始化阻塞及已通过的非数据库门禁，不得声称集成测试通过。

- [ ] **Step 4: 提交**

  ```powershell
  git add AGENTS.md docs/superpowers/specs/2026-08-07-structured-extraction-production-formats-design.md docs/runbooks/structured-extraction-137-production.md docs/reports/2026-08-07-structured-extraction-137-acceptance.md
  git commit -m "文档：更新结构化提取 manifest 生产契约"
  ```

- [ ] **Step 5: 最终核验**

  检查 `git status --short`、`git log --oneline master..HEAD` 和 `git diff --stat master...HEAD`，确认分支只包含本次计划、实现、测试和文档，没有主工作区既有未提交内容。
