# Task 4 实施报告：Markdown 清洗 Worker 编排与恢复

## 交付范围

- 新增 `MarkdownCleaningOrchestrator`，按 `claim -> stage original -> validate source -> process -> validate output -> prepare -> publish -> terminal update -> cleanup` 顺序执行。
- 所有运行态进度、prepared、publishing、成功和失败写入均携带 claim 返回的 lease token；条件写失败立即抛出 `LeaseLostError`，旧 worker 不继续处理或发布。
- 到期检查覆盖输入访问前和处理完成后、发布前；任务 deadline 原样传给 processor。
- 输入、processor 与非法输出等确定性错误落终态；文件系统/数据库等临时错误仅在 attempt 未耗尽时抛出 `RetryableWorkerError`，attempt 耗尽后失败。
- 发布成功而终态数据库写入失败时保留 staging/prepared 信息并请求有限重试；恢复仅使用 `allow_recovery=True` 的摘要对账，匹配则成功，不匹配则 `OUTPUT_CONFLICT`。
- queued、无 prepared 的 expired running、recoverable prepared 使用独立查询和逐项异常隔离；expired running 接管同时校验旧 lease token。
- 新增测试模块自己的 autouse `db` fixture 覆盖，纯编排测试不启动全局 PostgreSQL fixture；Repository 恢复条件使用独立内存 SQLite。

## RED 证据

1. 首次运行：

   `uv run pytest tests/features/markdown_cleaning/test_orchestration.py tests/features/markdown_cleaning/test_recovery.py -q`

   结果：2 个 collection error，均为预期的 `ModuleNotFoundError: app.features.markdown_cleaning.orchestration`。

2. Repository 恢复回归新增后：

   `uv run pytest tests/features/markdown_cleaning/test_recovery.py -q`

   结果：`2 failed, 1 passed`。失败分别证明 prepared 被错误混入普通 expired-running 列表，以及缺少 `reconcile_prepared`。

## GREEN 与质量门禁

- 初次交付目标测试：`13 passed`；review fix round 1 后目标数量见本轮提交验证。
- Ruff check：通过。
- Ruff format check：通过。
- Mypy（Task4 生产文件及最小兼容 Repository）：通过。
- Pyright（同范围）：`0 errors, 0 warnings`。
- ty（同范围）：通过。

测试运行仍报告项目既有的 4 个 warning：1 个 Starlette/httpx deprecation，以及默认开发密钥/密码的 3 个配置 warning；不是 Task4 新增失败。

## 明确边界与后续接线要求

- Task5 必须用独立 DB session 组装 `lease_renewer` callback；禁止将 execute 使用的 Repository/Session 直接交给 heartbeat 线程。
- 本任务没有实现 Celery task、注册或 beat schedule；它们属于 Task5。

## Review fix round 1

- 新增 `processing_deadline` 持久列与 `20260803_03` 迁移；首次 claim 使用 `coalesce(existing_deadline, now + timeout)`，恢复重试不会延长 deadline。真实 SQLite Repository-backed execute 验证持久 deadline 被传给 processor。
- 新增 `LeaseHeartbeat`：进入长同步 processor 前立即续租，随后后台周期续租；续租回调由 Task5 使用独立 DB session 组装，Task4 不在线程间共享 SQLAlchemy Session。任一续租失败均在 processor 返回后阻止校验和发布。
- Publisher 细分 `OutputConflictError`、`InvalidPreparedOutputError` 与 `PublicationSystemError`。execute/recovery 仅将目标已存在或恢复摘要不匹配记为 `OUTPUT_CONFLICT`；prepared 无效记为 `INVALID_PROCESSOR_OUTPUT`；文件系统/能力错误保留为可重试异常。
- Alembic offline upgrade SQL 已确认新增 timezone-aware 列与索引；downgrade SQL 已确认先删除索引再删除列。
- Review fix round 1 的 Task1–4 隔离测试：`145 passed`；补充错误分类用例后最终目标数量以提交前 fresh run 为准。
- 变更范围 Ruff、format、Mypy、Pyright、ty 均通过。全量 `app` 静态检查另暴露 structured extraction 的既有 suppressions/Celery typing，以及仓库既有 26 个 format drift；未越权修改。
