# Task 6 实施与验证记录

## round1a：自有真实基础设施与 happy path

- RED 1：`pytest tests/integration/markdown_cleaning/test_happy_path.py -q` 首次失败于根级 autouse fixture 连接共享 `localhost:5432/app`，证实旧测试依赖共享数据库。
- RED 2：隔离根 fixture 后，真实 `alembic upgrade head` 因 Markdown API 输出根与 worker 发布根不得重叠而失败；该限制使合法业务 `targetPath` 无法同时通过请求策略与发布器，已删除这项矛盾校验，仍保留输入/staging/output 的安全隔离。
- 测试 fixture 每例创建唯一 `tp_md_<uuid>` PostgreSQL database，仅通过 `127.0.0.1:5433/postgres` 管理连接创建，并在 `finally` 中终止该库连接、删除该库；不调用 `metadata.create_all`。
- fixture 对唯一数据库执行真实 `uv run alembic upgrade head`，并同时用 Alembic heads 输出和 `alembic_version.version_num` 断言迁移至 head。
- fixture 创建唯一 `tp-md-redis-<uuid>` Redis 7 容器和随机宿主端口；API dispatcher 与 worker 共享该 broker URL，`finally` 仅删除自有容器。
- TestClient 仅 override `get_current_user`；数据库通过应用 `get_db` 所引用的真实 engine 切换，POST 使用生产 `CeleryMarkdownCleaningTaskDispatcher` 写入 Redis。
- 启动真实 Celery subprocess：`worker -P solo -Q markdown_cleaning`，轮询真实 GET 至 `succeeded`。
- happy path 固定逐字节中文输出，断言全部统计、业务 `targetPath`、响应无 staging/internal staging root、原始 BOM 文件未变、输出无 BOM、规范化输入摘要、prepared/output 摘要以及数据库 `processing_deadline`。
- 故障、恢复与重复投递场景保留给后续轮次；round1a 只收敛真实 happy path。

## round1a 验证

- `uv run pytest -q tests/integration/markdown_cleaning/test_happy_path.py tests/integration/markdown_cleaning/test_api_worker_contract.py`
  - 结果：`3 passed`（4 条既有依赖/本地默认密钥 warning）。
- `uv run ruff check app/core/config.py tests/conftest.py tests/integration/markdown_cleaning`
  - 结果：`All checks passed!`。
- 清理核验：`docker ps -a` 无 `tp-md-redis-*`；PostgreSQL `pg_database` 无 `tp_md_*`。
