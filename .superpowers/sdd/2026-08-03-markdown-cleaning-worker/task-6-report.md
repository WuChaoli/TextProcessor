# Task 6 实施与验证记录

## round1a：自有真实基础设施与 happy path

- 每例创建唯一 `tp_md_<uuid>` PostgreSQL database，仅连接健康 admin `127.0.0.1:5433/postgres` 创建，并在 `finally` 中只终止、删除自有库。
- 不使用 `metadata.create_all`；执行真实 `alembic upgrade head`，并以 `alembic heads` 和数据库 `alembic_version` 双重断言。
- 每例创建唯一 `tp-md-redis-<uuid>` Redis 7 容器和随机宿主端口，API dispatcher 与 Celery worker 使用同一 broker，`finally` 只删除自有容器。
- TestClient 仅固定认证身份；生产 dispatcher POST 入队，真实 Celery `-P solo -Q markdown_cleaning` 消费，GET 轮询至 `succeeded`。
- 删除 API output roots 与 worker output roots 不能重叠的矛盾校验；保留 input、staging 与 output 的危险重叠校验。

## round1b/5：完整真实集成验收

- happy path 使用中文 canonical corpus，并在原始输入前加入 UTF-8 BOM；逐字节对比固定 `expected.md`，覆盖 phone、idCard、bankCard、email、ipv4 五类脱敏、重复段落、GFM 格式化以及代码、链接、HTML 等保护区。
- 独立真实 `InputResolver + MarkdownInputValidator` 断言 `source.original.md` 保留 BOM、原始摘要准确，独立 `source.md` 无 BOM、处理摘要准确；随后调用安全 `StagingLayout.cleanup()` 且仅清理 task root。
- 真实 API 配合故障 dispatcher 验证入队失败：HTTP 503、PostgreSQL `failed/QUEUE_SUBMISSION_FAILED` 稳定持久化、无 staging 字段；异常内部路径不进入 caplog 或响应。
- 真实 Redis 重复投递两条相同 execute 消息，真实 worker 完成一次，目标字节不变且 `attempt_count == 1`。
- 人工构造过期 running lease 后，通过真实 broker 投递生产 `markdown_cleaning.recover` task；真实 solo worker 执行恢复、重投 execute 并完成 takeover，最终 `succeeded` 且 attempt 增至 2。
- 路径逃逸、输出冲突原字节保留、发布后数据库失败恢复继续由真实 PostgreSQL、真实 publisher/repository 测试覆盖；processor 不使用 mock/eager/fake broker。

## RED/GREEN 证据

- RED：旧根 fixture 首先错误连接共享 `localhost:5432/app`；迁移后暴露 API output 与 worker publish root 的矛盾校验；独立 staging 测试首跑暴露协议 fixture 缺失恢复摘要字段。
- GREEN focused：五类 happy path、独立 staging、API enqueue failure、真实重复消息、真实 recover takeover 均已分别通过。
- 完整 integration：`uv run pytest -q tests/integration/markdown_cleaning --maxfail=1`，结果 `11 passed, 4 warnings in 60.99s`。
- Ruff：`uv run ruff check app/core/config.py tests/integration/markdown_cleaning`，最终结果 `All checks passed!`。
- MyPy 探测：`uv run mypy app/features/markdown_cleaning tests/integration/markdown_cleaning` 未通过，报告 37 条测试类型错误（主要为原有 fixture 未标注、Celery/Kombu 无 stubs，以及生产 Protocol 的严格逆变不兼容）；运行时完整 integration 已通过，本轮不以忽略规则掩盖该边界。
- Windows teardown 修复：旧实现只 terminate `uv` launcher，曾遗留本 worktree 的 Celery/Python 子进程；已精确清理，并改为对 fixture 记录的根 PID 执行 `taskkill /PID <pid> /T /F`、等待根进程退出。连续 worker 测试及完整 integration 后精确命令行核验均为 `OWNED_WORKERS_REMAINING=0`。
- 最终资源核验：无 `tp-md-redis-*` 容器、无 `tp_md_*` PostgreSQL database。

## fix round2/5：review Medium/Low 收敛

- 配置语义：API `MARKDOWN_CLEANING_OUTPUT_ROOTS` 与 worker `output_roots` 相同或位于其子目录均允许；output 与 API input、worker staging 相同或父子重叠继续拒绝。相关 config/orchestration/Celery 测试 `37 passed`。
- 真实 crash/takeover：本机 `host.docker.internal:80` 受控 Markdown HTTP 服务仅允许解析出的 LAN 地址 `/32`，首请求阻塞；API POST 后首 worker 已 claim 且 DB 为 `running` 时按 fixture 根 PID 杀完整进程树，确认无目标文件；2 秒 lease 到期后生产 `markdown_cleaning.recover` 经真实 Redis 被新 solo worker 消费并重投 execute，第二次 HTTP 返回后最终 `succeeded`、`attempt_count == 2`、仅一个目标，额外等待后字节不变。
- 严格 envelope：真实 POST 后使用 Kombu 取出消息并逐字段断言仅含 `taskId/taskType/schemaVersion`，随后 requeue 原消息交给真实 worker。
- 入队失败：使用未监听的真实 Redis 端口和独立 `Celery(set_as_current=False)`，生产 dispatcher 返回稳定 503 并在真实 PostgreSQL 持久化失败；每例 API Celery app 独立，避免 producer pool/current app 跨 fixture 污染。
- cleanup：terminal staging cleanup 遇到 `OSError` 记录安全 warning，结构化日志含 `task_id`，不记录异常或宿主路径。
- deadline/decorator：真实 crash 任务断言 `processing_deadline - started_at` 位于 29 到 31 秒，且 Celery decorator `time_limit` 大于 processor soft timeout。
- 完整 integration：`uv run pytest -q tests/integration/markdown_cleaning --maxfail=1`，结果 `12 passed, 4 warnings in 80.06s`。
