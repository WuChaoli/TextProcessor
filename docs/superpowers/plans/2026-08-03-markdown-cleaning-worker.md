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
- Worker 必须把服务端 staging root 与 processor limits 显式注入 `MarkdownCleaningPipeline`；不得把数据库 `targetPath` 传给 Processor。
- Worker 必须保留外部原始 `source.original.md`，再由 validator 原子生成独立 strict UTF-8/no-BOM `source.md` 供 Processor 使用；即使原件无 BOM 也禁止两者共用同一文件。
- Worker 必须把任务记录中的 timezone-aware UTC deadline 显式传给 Processor；Processor 与内部 max seconds 取较早者。纯文本变换运行在可终止子进程中，子进程无 destination 访问权且 timeout 使用剩余时间；Celery `time_limit` 作为第二道硬上限，必须大于 pipeline timeout 与终止宽限之和，防止父进程或操作系统级终止异常长期占用 worker。
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
- [ ] RED：覆盖 staging/input/output roots、HTTP host/CIDR、字节/超时/attempt/lease/recovery/concurrency 校验，以及 staging/output 根不重叠；校验 `processor timeout < Celery hard time limit` 并保留终止宽限。
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
- [ ] RED：覆盖空文件、起始 BOM、内部 BOM、非 UTF-8、NUL、超限和未闭合 fence；断言保留带 BOM 原始字节与摘要，并另行原子生成无 BOM Processor source 与独立摘要。
- [ ] 复用结构化提取中已验证的算法模式，但建立 markdown_cleaning 自有类型；流式复制并计算 SHA-256，完成后原子进入 staging。
- [ ] staging 固定包含 `input/source.original.md` 与 `input/source.md`：前者是外部输入/复用真相，后者只由 validator 移除起始单个 UTF-8 BOM 后生成；不得做换行、正文或 Markdown 结构归一化。
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
- [ ] 编排严格执行 claim → stage original → validate/normalize processor source → `process(..., deadline=task.processing_deadline)` → validate output → prepare → publish → terminal update → safe cleanup；已过期任务不得访问 Processor source 或发布 destination。
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
- [ ] RED：验证 task 名 `markdown_cleaning.execute`/`markdown_cleaning.recover`、严格 envelope、每次 task 独立 DB session、acks-late/reject-on-worker-lost、显式 soft/hard time limit、include 与 beat schedule。
- [ ] execute task 的 hard time limit 必须来自服务端配置且大于配置的整体 task timeout + 子进程终止宽限；它是 Processor deadline 之后的第二道兜底，超时后不得发布 destination，Worker 中断按现有租约恢复。
- [ ] 低风险跟踪：当前可信 Processor child 的业务输出受 `max_output_bytes` 约束，但父进程 `communicate()` 尚无独立 stdout 协议硬上限；Worker 上线前须补充有界流式读取/受控 spool，或配置容器/作业级内存硬限与受控并发，避免异常 child 以超大协议响应占满 Worker 内存。
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
- [ ] 以 UTF-8 BOM 原件覆盖 `source.original.md` 摘要、无 BOM `source.md` 摘要、Processor strict no-BOM 与任务 deadline 传播；Celery hard limit 仅作为第二道兜底。
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
