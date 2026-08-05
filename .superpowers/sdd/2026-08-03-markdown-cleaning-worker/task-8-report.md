# Task 8 最终修复报告（round 1/5）

## 已完成

- 将 Markdown 清洗 GET 响应补齐 `progress: {phase, percent}`。
- 公共 phase 仅允许 `validating_input`、`cleaning`、`publishing`、`completed`：
  - queued/running 根据内部阶段映射；
  - succeeded 强制 `completed/100`；
  - failed 若内部阶段已变为 `failed` 或未知值，则按已持久 `progress_percent` 推导失败前阶段，百分比保持不变。
- policy/allowlist 请求拒绝统一为 HTTP 422；幂等冲突仍为 409，队列失败仍为 503；OpenAPI 与路由测试同步更新。
- `FakeCelery` 支持并断言 `queue="markdown_cleaning"`。
- Python 3.14/PEP 758 语法本身有效，但按审查要求将三个多异常 `except` 明确加括号，并使用 `# fmt: skip` 防止 Ruff 在 Python 3.14 目标下移除括号。
- 对指定 Markdown 清洗生产代码、feature/API/integration tests 执行 Ruff 格式化，首轮实际重排 29 个文件。

## 已验证

- `uv run ruff format ...`：最终 69 files left unchanged。
- `uv run ruff check ...`：All checks passed。
- `mypy app/features/markdown_cleaning`：32 source files，0 issues。
- `ty check --python ..\\.venv\\Scripts\\python.exe app/features/markdown_cleaning`：All checks passed。
- `pyright --pythonpath ..\\.venv\\Scripts\\python.exe app/features/markdown_cleaning`：0 errors / 0 warnings。
- `python -m py_compile` 覆盖 `input_resolver.py`、`pipeline.py`、`schemas.py`、`routes.py`，并真实 import 成功。
- `git diff --check`：通过。

## 当前环境阻塞与未验证项

- 默认 `.env` 的 PostgreSQL 5432 凭据与本机实例不匹配；现有测试 PostgreSQL 在 5433。
- 切换 5433 后，精确 8 个目标 pytest node 两次均在 120 秒内没有进入收集/没有断言输出；进程 CPU 约为 0，PostgreSQL `pg_stat_activity` 无对应连接。按主会话指令已精确终止本轮 pytest 父子进程，不再重复启动。
- 因 runner hang，本轮没有可声称的 pytest GREEN 计数，也未执行全量 349、integration 12、Alembic 往返或 fresh-stack；这些必须由主会话在独立运行环境复验。

## 主会话独立复验

- API/契约定向测试：`56 passed`。
- Markdown feature 与 API 路由全量：`361 passed`。
- 真实 PostgreSQL/Redis/Celery integration：`12 passed`。
- 独立临时 PostgreSQL 数据库执行 Alembic `upgrade head -> downgrade -1 -> upgrade head`，最终为 `20260803_03 (head)`，随后删除该测试数据库。
- Ruff format/check、Mypy、Pyright、ty 均通过；生产类型检查覆盖 32 个 Markdown 清洗源文件。
- fresh 全栈脚本成功：`runId=e53e37843f9f`，`exit 0`，`37.97s`；真实 API、worker、beat、崩溃恢复、冲突和幂等均通过。
- 验收后 `tp-md-*` 容器、Celery 进程、`textprocessor-md-*` 临时目录和 `tp_md_*` 测试数据库均为 0。
