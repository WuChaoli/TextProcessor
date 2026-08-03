# Task 6 实施报告

## 范围

- 新增真实 PostgreSQL、Redis/Celery broker 与本地 `MarkdownCleaningPipeline` 集成测试。
- 覆盖真实摘要/统计、BOM 输入、deadline、重复投递、路径逃逸、输出冲突、租约接管与发布后数据库恢复。
- 覆盖 broker envelope 仅携带 `taskId/taskType/schemaVersion`，以及公共结果不泄露 staging 路径。
- Windows 真实运行发现 terminal 后 staging 清理可能因目录句柄返回 `WinError 5`；编排改为终态落库后的清理失败不反转业务成功状态，残留由后续安全清理处理。

## RED 证据

1. 首次收集失败：测试环境缺少 Settings 必填变量。
2. 补齐变量后收集失败：Kombu 不从顶层导出 `SimpleQueue`；改用真实 connection 的 `SimpleQueue()`。
3. 真实 PostgreSQL 失败：已有测试库缺少 `processing_deadline` 列；执行 `uv run alembic upgrade head`，应用 `20260803_02 -> 20260803_03`。
4. 真实 Pipeline 失败：Windows terminal cleanup 抛出 `PermissionError [WinError 5]`；以集成回归测试驱动最小编排修复。

## GREEN 证据

```text
uv run --project backend pytest backend/tests/integration/markdown_cleaning -q
8 passed, 3 warnings in 12.20s
```

基础设施：

- 复用既有健康 `textprocessor-test-db`（PostgreSQL 18，宿主 5433），先执行 `pg_isready`。
- 本任务创建 `tp-md-task6-redis`（Redis 7，宿主 6396），`redis-cli ping` 返回 `PONG`；任务结束删除该容器。
