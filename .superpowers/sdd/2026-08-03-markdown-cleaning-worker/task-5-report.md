# Task 5 实施报告：Markdown 清洗 Celery Worker 接线

## 交付范围

- 注册 `markdown_cleaning.execute` 与 `markdown_cleaning.recover`；消息由 `MarkdownCleaningMessage` 严格校验，仅允许 `taskId`、`taskType`、`schemaVersion`。
- execute/recover 每次调用均创建独立 SQLModel Session；heartbeat 的 `renew_lease` 每次续期再次创建独立 Session，并由 Repository 以 task/status/lease token/未过期条件续租。
- 真实装配包含 Repository、InputResolver、MarkdownInputValidator、MarkdownCleaningPipeline、MarkdownCleaningOutputValidator、Publisher、staging/settings；Processor 与 orchestration 的 timeout 均注入 `processing_soft_timeout_seconds`。
- execute 显式启用 `acks_late`、`reject_on_worker_lost`、soft/hard time limit 和 `markdown_cleaning` queue；仅 `RetryableWorkerError` 触发最多 `max_attempts - 1` 次 Celery retry。
- Celery include、beat 恢复周期与队列已接线；恢复沿用 Task 4 的逐项异常隔离，并经现有 dispatcher 发严格 envelope。
- Processor child stdout 改为 `TemporaryFile` 受控 spool；父进程仅读取 `max_output_bytes + 64 KiB + 1`，超限映射 `INVALID_PROCESSOR_OUTPUT` 且不发布，消除 `communicate()` 将异常响应无界保存在内存的问题。

## RED 证据

1. 首次目标测试：2 个 collection error，预期缺少 `app.features.markdown_cleaning.celery_tasks`。
2. stdout 风险测试：异常 child 输出 1 MB；旧实现完整读取后报 `INTERNAL_ERROR`，断言期望 `INVALID_PROCESSOR_OUTPUT` 失败。

## GREEN 与门禁

- Celery/部署/完整 pipeline 目标：`59 passed`。
- pipeline 全量：`50 passed`；Celery/部署：`9 passed`。
- task registry：精确得到 `['markdown_cleaning.execute', 'markdown_cleaning.recover']`。
- Ruff check 与 format check：通过。
- Mypy：3 个生产文件通过。
- Pyright：`0 errors, 0 warnings`。
- ty：通过。
- `git diff --check`：通过。

测试仍显示项目既有 4 个 warning：Starlette/httpx deprecation 与 3 个默认开发凭据配置 warning。

## 回归边界

- 尝试运行整个 `tests/features/markdown_cleaning` 时，大量旧模块受全局 autouse PostgreSQL fixture 阻断，本机 localhost PostgreSQL 凭据不匹配；这属于计划明确留给 Task 6 的真实 PG/Redis 集成边界。
- 从仓库根运行 Processor/Worker 组合测试时，所有不依赖真实 PostgreSQL的用例执行完成；需要全局 fixture 的旧用例仍因同一 PostgreSQL 环境错误未执行。未声称 Task 6 集成通过。
