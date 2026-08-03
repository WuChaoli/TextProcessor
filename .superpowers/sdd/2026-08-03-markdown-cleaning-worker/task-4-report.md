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

- 目标测试：`13 passed`。
- Ruff check：通过。
- Ruff format check：通过。
- Mypy（Task4 生产文件及最小兼容 Repository）：通过。
- Pyright（同范围）：`0 errors, 0 warnings`。
- ty（同范围）：通过。

测试运行仍报告项目既有的 4 个 warning：1 个 Starlette/httpx deprecation，以及默认开发密钥/密码的 3 个配置 warning；不是 Task4 新增失败。

## 明确边界与后续接线要求

- Task1 当前 `MarkdownCleaningTask` 表模型没有 `processing_deadline` 字段。Task4 通过 `MarkdownCleaningWorkerTask` 协议明确该权威字段，并用测试证明原样传入 processor，但没有越权修改 Task1 的表模型或迁移。Task5 接线前必须由 Task1/集成收口补齐持久字段、创建时赋值和迁移；否则真实 Repository 返回对象不能满足 Task4 协议。
- 本任务没有实现 Celery task、注册或 beat schedule；它们属于 Task5。
- worktree 根存在非本任务所有的 untracked `result.md`，本提交不纳入也不删除。
