# Markdown Cleaning Worker Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development` to implement this plan task-by-task. Every task follows RED → GREEN → REFACTOR and ends with an isolated commit.

**Goal:** 将 Markdown 清洗 API 任务接入本地 Celery Worker，可靠完成受控输入下载、staging Processor 执行、无覆盖原子发布、租约恢复和真实全栈验收。

**Architecture:** PostgreSQL 保存完整参数和权威状态，Celery envelope 仅含 `task_id/type/schema_version`。Worker 以带 token 的租约 claim 任务，按服务端配置解析输入至 task staging，调用本地 Processor，再把校验后的结果原子发布到数据库中的业务 `target_path`。API 始终返回 `targetPath`，不得暴露 staging；beat 恢复 queued、过期 running 及已发布但状态未收口的任务。

**Tech Stack:** FastAPI、Celery 5、Redis、PostgreSQL、SQLModel、fsspec/httpx、Python pathlib/os primitives、pytest、Docker Compose。

## Global Constraints

- 契约以 `docs/superpowers/specs/2026-08-03-markdown-cleaning-worker-design.md` 为准，并依赖 Processor 计划完成。
- Worker 不接受消息中的路径/正文/配置；所有权威参数从 PostgreSQL 读取。
- 本地输入失败不能切换 OSS；请求选择的输入源不可在 Worker 自动改变。
- staging 根必须由 `task_id` 派生并做 containment 校验；清理不得信任数据库中的任意路径。
- 目标已存在必须失败，不能覆盖；发布需要跨进程安全的 `O_EXCL`/hard-link 或等价原子原语，不能只用进程内锁。
- 每次续租、进度、成功或失败落库均校验 lease token，旧 Worker 不得覆盖新 Worker。
- 真实验收必须启动 API、Worker、beat、PostgreSQL、Redis，使用真实 Processor 和固定中文输入逐字节验证。

### Task 1: Worker 配置、错误与租约 Repository

**Files:**
- Modify: `backend/app/core/config.py`
- Modify: `backend/app/features/markdown_cleaning/{repository.py,api_errors.py,task_models.py}`
- Create: `backend/app/features/markdown_cleaning/worker_models.py`
- Create: `backend/app/alembic/versions/20260803_03_add_markdown_cleaning_worker_state.py`（仅当现有表缺少实现所需字段）
- Create: `backend/tests/features/markdown_cleaning/{test_worker_config.py,test_worker_repository.py}`

**Steps:**
- [ ] RED：覆盖 staging/input/output roots、HTTP host/CIDR、字节/超时/attempt/lease/recovery/concurrency 校验，以及 staging/output 根不重叠。
- [ ] RED：覆盖 queued 原子 claim、token 续租、阶段进度、prepared/finished/failure、旧 token 拒绝、attempt 上限、recoverable 分页和 dispatch 节流。
- [ ] 实现嵌套 Markdown Worker settings 和值对象；Repository 所有 Worker 写操作使用 `status + lease_token` 条件更新。
- [ ] GREEN：运行目标测试和 Alembic upgrade/downgrade（如有迁移）。
- [ ] Commit: `功能：建立Markdown清洗Worker租约契约`

### Task 2: 安全 staging、受控输入与输入校验

**Files:**
- Create: `backend/app/features/markdown_cleaning/{staging.py,input_resolver.py,input_validator.py}`
- Create: `backend/tests/features/markdown_cleaning/{test_staging.py,test_input_resolver.py,test_input_validator.py}`

**Steps:**
- [ ] RED：覆盖本地 allowlist/path escape/symlink、HTTP credentials/redirect SSRF/host CIDR/timeout/size、选定源失败不 fallback、`.part` 清理、hash 复用。
- [ ] RED：覆盖空文件、BOM、非 UTF-8、NUL、超限和未闭合 fence。
- [ ] 复用结构化提取中已验证的算法模式，但建立 markdown_cleaning 自有类型；流式复制并计算 SHA-256，完成后原子进入 staging。
- [ ] task staging 权限收紧，所有删除路径由配置 root + task_id 重新计算并验证 containment。
- [ ] GREEN：运行三个目标测试。
- [ ] Commit: `功能：实现Markdown清洗安全输入暂存`

### Task 3: 结果校验与无覆盖原子发布

**Files:**
- Create: `backend/app/features/markdown_cleaning/{output_validator.py,publisher.py}`
- Create: `backend/tests/features/markdown_cleaning/{test_output_validator.py,test_publisher.py}`

**Steps:**
- [ ] RED：覆盖 UTF-8、大小/hash/统计、保护区与 Processor result 一致性、目标预存在冲突、并发发布仅一个成功、崩溃后 prepared hash 对账。
- [ ] 在目标父目录创建 `O_EXCL` 临时文件并 fsync；使用 hard-link/no-replace 原语提交，任何路径均不得覆盖现有目标。
- [ ] target 仅允许配置的本地输出根；错误持久化前脱敏绝对路径。
- [ ] GREEN：运行目标测试和多进程并发用例。
- [ ] Commit: `功能：实现Markdown清洗原子发布`

### Task 4: Worker 编排、失败分类与恢复

**Files:**
- Create: `backend/app/features/markdown_cleaning/orchestration.py`
- Create: `backend/tests/features/markdown_cleaning/{test_orchestration.py,test_recovery.py}`

**Steps:**
- [ ] RED：覆盖完整成功流、确定性输入/Processor/冲突失败、临时系统错误有限重试、每阶段进度、worker 中断、租约过期被新 worker 接管、旧 worker 收尾被拒绝。
- [ ] 编排严格执行 claim → stage → validate → process → validate output → prepare → publish → terminal update → safe cleanup。
- [ ] 若发布成功后数据库更新失败，恢复逻辑仅在目标 hash 等于 prepared hash 时收口成功；不匹配则输出冲突。
- [ ] queued/expired running/recoverable prepared 批量恢复互相隔离，单任务异常不阻断本批次。
- [ ] GREEN：运行目标测试。
- [ ] Commit: `功能：实现Markdown清洗Worker编排恢复`

### Task 5: Celery task、注册与 beat 恢复

**Files:**
- Create: `backend/app/features/markdown_cleaning/celery_tasks.py`
- Modify: `backend/app/core/celery_app.py`
- Create: `backend/tests/features/markdown_cleaning/{test_celery_tasks.py,test_deployment_stack.py}`

**Steps:**
- [ ] RED：验证 task 名 `markdown_cleaning.execute`/`markdown_cleaning.recover`、严格 envelope、每次 task 独立 DB session、acks-late/reject-on-worker-lost、include 与 beat schedule。
- [ ] Celery entrypoint 只做 message validation、依赖组装、调用 orchestration 和安全日志；不得实现文件/SQL/Processor 细节。
- [ ] recover task 使用 Repository 列表与现有 dispatcher 重投 envelope，逐项记录失败但不中断。
- [ ] GREEN：运行目标测试并启动 Celery app 检查 task registry。
- [ ] Commit: `功能：接入Markdown清洗Celery任务`

### Task 6: API/Worker/Processor 集成测试

**Files:**
- Create: `backend/tests/integration/markdown_cleaning/{conftest.py,test_worker_pipeline.py,test_recovery_pipeline.py,test_api_worker_contract.py}`

**Steps:**
- [ ] 使用真实 PostgreSQL 和 Redis；通过 API POST 创建任务并捕获真实 broker envelope，Worker 从 DB 读取路径而非消息。
- [ ] 运行固定中文输入，覆盖段落去重、五类脱敏、格式化、保护区、最终 DB 统计与目标文件逐字节一致。
- [ ] 断言 POST/GET 始终返回业务 `targetPath`，响应、错误、日志均无 `staging_path`/宿主机内部路径。
- [ ] 覆盖重复消息、入队失败、worker 中断、租约接管、路径逃逸、输出冲突和发布后 DB 失败恢复。
- [ ] 运行 `uv run --project backend pytest backend/tests/integration/markdown_cleaning -q`。
- [ ] Commit: `测试：覆盖Markdown清洗接口与Worker集成`

### Task 7: 真实全栈脚本与运行手册

**Files:**
- Modify: `docker-compose.yml` 或仓库当前权威 compose 文件
- Create: `scripts/verify-markdown-cleaning-stack.ps1`
- Create: `docs/runbooks/markdown-cleaning.md`

**Steps:**
- [ ] 以唯一 compose project/queue 启动 PostgreSQL、Redis、API、Celery worker、beat；不得 mock broker、DB 或 Processor。
- [ ] 创建受控 input/output/staging 根和固定中文 sample，经 HTTP API 提交并轮询至终态。
- [ ] 逐字节验证业务 `targetPath` 文件、全部统计、DB 状态和只产生一个最终输出；重复投递保持幂等。
- [ ] 模拟一次 worker 终止/租约过期并验证 beat 恢复；验证目标预存在时不覆盖。
- [ ] 脚本使用 `try/finally` 清理仅属于本次运行的容器/临时目录，输出可复核摘要。
- [ ] Commit: `测试：增加Markdown清洗真实全栈验收`

### Task 8: 最终审查与质量门禁

- [ ] fresh reviewer 对照 API、Worker、Processor 三份 spec 审查端到端语义、安全、恢复和测试证据。
- [ ] findings 按 RED → GREEN 修复，并由原 reviewer 复核。
- [ ] 运行 markdown_cleaning 全部 unit/integration、Alembic、Ruff、Mypy、Pyright、ty 及真实全栈脚本。
- [ ] 区分本变更失败与仓库既有失败；不得把未通过或未运行写成通过。
- [ ] Commit: `修复：完成Markdown清洗端到端收口`
