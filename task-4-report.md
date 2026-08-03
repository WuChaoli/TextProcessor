# Task 4 实施与验证记录（round 1）

## 定位与结论
- 复核当前 diff 后，`create_task` 已将 `idempotency_lock` 上移到 service 层，覆盖 `create_or_get`、`PENDING->QUEUED`、`enqueue_execute`、`mark_dispatched`/失败持久化路径。
- 并发死锁现象在当前实现与本地回归验证中未复现；`idempotency_lock` 对并发 `create_task` 可形成串行化，避免两个会话同时进入队列提交逻辑。

## 变更文件
- `backend/app/features/markdown_cleaning/repository.py`
- `backend/app/features/markdown_cleaning/service.py`
- `backend/tests/features/markdown_cleaning/test_service.py`

## 验证执行（每条命令独立、单测颗粒）
1. `uv run --project backend pytest backend/tests/features/markdown_cleaning/test_service.py -q --maxfail=1`
   - 结果：`5 passed`
   - 说明：非并发 service tests 全量通过
2. `uv run --project backend pytest backend/tests/features/markdown_cleaning/test_service.py -q -k concurrent --maxfail=1`
   - 结果：`1 passed`
   - 说明：并发场景（`test_concurrent_replay_waits_for_lock_and_returns_safe_503`）通过，无阻塞挂起
3. `uv run --project backend pytest backend/tests/features/markdown_cleaning/test_repository.py backend/tests/features/markdown_cleaning/test_state_machine.py -q`
   - 结果：`21 passed`
4. `uv run --project backend ruff check backend/app/features/markdown_cleaning backend/tests/features/markdown_cleaning`
   - 结果：`All checks passed!`
5. `uv run --project backend mypy backend/app/features/markdown_cleaning backend/tests/features/markdown_cleaning`
   - 结果：失败（非本次改动引入）
   - 明细：`backend/tests/features/markdown_cleaning/test_api_contract.py` 3 条类型错误（`None` 赋值给 `str`）

## 行为校验（Thread/Event）
- 已在并发测试中对 `Event.wait` 使用显式超时与断言：
  - `dispatcher.blocked_event.wait(timeout=5.0)`
  - `release_event.wait(timeout=8.0)`
- `future.result(timeout=10.0)` 使用超时收敛防止卡死。

## 备注与风险
- 以上命令均在 `ENVIRONMENT=local` 且显式 PostgreSQL 环境变量下执行。
- 任务4 当前轮次未再发现新的阻塞/死锁回归；遗留 `mypy` 报错位于 `test_api_contract.py`，与本轮逻辑修改不直接相关。

## Task4 round 2 修复说明
- 根因：`conftest.py` 的全局 `db` fixture 在本文件场景也会提前连接 `PostgreSQL`，导致 PostgreSQL 不可达/凭据错误时报错，而非按并发测试逻辑 `skip`。
- 修复点：
  - 在 `backend/tests/features/markdown_cleaning/test_service.py` 覆写会话级 autouse `db` fixture 为 noop，避免本文件的其它测试提前触发全局 DB 初始化失败。
  - 新增 `_skip_if_postgres_unreachable()`：`engine.connect()` 探测返回 `OperationalError` 时 `pytest.skip`，并在并发测试中优先执行，防止逻辑误判。
  - 保留并发等待/断言路径（`Event.wait` 与 `future.result(timeout=...)`）不变。

## 对照验证（focused service）
- 默认不可达配置（示例：`POSTGRES_PORT=5432`）：
  - 命令：`uv run --project backend pytest backend/tests/features/markdown_cleaning/test_service.py -q --maxfail=1`
  - 结果：`4 passed, 1 skipped`
- 显式可达配置（`127.0.0.1:5433`）：
  - 命令：`uv run --project backend pytest backend/tests/features/markdown_cleaning/test_service.py -q --maxfail=1`
  - 结果：`5 passed`
- 同步命令：
  - `uv run --project backend ruff check backend/app/features/markdown_cleaning`
  - `uv run --project backend mypy backend/app/features/markdown_cleaning`
  - 结果：`All checks passed` / `Success: no issues found`
