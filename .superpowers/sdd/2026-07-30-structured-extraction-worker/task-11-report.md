# Task 11 报告：结构化提取端到端故障、并发与安全验证

## 完成内容

- 新增真实 PostgreSQL（`127.0.0.1:5433`）集成测试
  `backend/tests/integration/structured_extraction/test_worker_pipeline.py`。
- 覆盖 API POST 本地 TXT、实际 `ExtractionOrchestrator.submit` 和 API GET：
  输出唯一 Markdown、UTF-8 无 BOM、原文结构、路由、processor 元数据以及输入/输出
  SHA-256 均被断言。
- 覆盖两个任务竞争同一 target（仅一个成功）、外部结果发布后数据库成功状态写入前
  worker 崩溃后的同摘要恢复、重复 submit/poll 的外部调用幂等，以及丢失 poll 后
  recover 重调度并完成。
- 覆盖本地符号链接逃逸、HTTP redirect 到 loopback/link-local/metadata、越权 S3 bucket
  与 URL 调用方凭据；请求或输入解析均在访问前拒绝。
- 覆盖 processor slot 容量等待、deadline 超时后的 quarantine、grace 到期 reap，以及
  外部成功终态释放 slot；成功 poll 后以真实 PostgreSQL 查询断言该任务的
  `ProcessorSlot` 已不存在。
- 测试 session 结束时先清理 `processor_slot` 和 `extraction_task`，再清理 user，避免
  结构化任务外键残留污染默认全量套件。

## RED 与根因核验

- 首轮 RED 先暴露新 API 测试 fixture 使用已提交且脱离 Session 的 `User` 实例；改为保存
  固定 UUID 后消除，该问题仅在测试夹具中。
- 初次全量套件的 target race 曾出现失败任务 `INTERNAL_ERROR`，而单文件是
  `OUTPUT_CONFLICT`。没有放宽断言：恢复并保留 `OUTPUT_CONFLICT`；以临时
  `_internal_error()` trap 配合完整套件验证未进入 generic exception，按前置
  structured-extraction 收集顺序的 187 项验证也通过。移除 trap 后完整套件再次通过。
- 没有发现需要改变运行时语义的生产缺陷。质量门禁发现 repository 的 SQLModel
  `order_by` 字段缺少 `col(...)`，导致 Mypy/Ty 失败；已作等价类型修正。slot 的
  PostgreSQL advisory-lock 调用保留 SQLAlchemy `execute`，并添加 Ty 的精确弃用豁免，
  因 `Session.exec` 对该 scalar select 没有匹配 overload。
- Review fix round 1：临时移除 `poll()` 的 slot release 后，terminal-release 用例以
  `ProcessorSlot(state='active') is None` 失败（RED）；恢复 release 后该真实 PG
  断言通过（GREEN）。这证明用例不会仅凭任务成功而掩盖 slot release 回归。

## 验证证据

在 `backend` 目录、显式环境变量
`POSTGRES_SERVER=127.0.0.1 POSTGRES_PORT=5433` 下执行：

- `uv run pytest tests/integration/structured_extraction/test_worker_pipeline.py -q`：11 passed。
- `uv run ruff format app tests --check`：97 files already formatted。
- `uv run ruff check app tests`：All checks passed。
- `uv run mypy app`：Success: no issues found in 52 source files。
- `uv run ty check app`：All checks passed。
- `uv run pytest -m "not real_integration" -q`：261 passed，2 deselected，54 warnings。

`real_integration` 的 2 项未计入以上默认测试结果；warnings 为既有 FastAPI TestClient
弃用提示和测试环境中的 JWT 短密钥警告。

## 未覆盖边界

- 未调用真实 MinerU、Docling、Redis 或 MinIO；外部 processor 与 HTTP transport 均为
  受控 fake/MockTransport，以保证默认测试可重复且不将真实集成纳入默认结果。
