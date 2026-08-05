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

## Task4-round3（本次续跑）验证记录
- 统一环境：`POSTGRES_SERVER=127.0.0.1 POSTGRES_PORT=5433 POSTGRES_DB=app POSTGRES_USER=postgres POSTGRES_PASSWORD=changethis PROJECT_NAME=TextProcessor FIRST_SUPERUSER=admin@example.com FIRST_SUPERUSER_PASSWORD=changethis SECRET_KEY=changethis ENVIRONMENT=local`，在 `backend` 目录执行 `uv run --project . ...`
- 1）`pytest tests/features/markdown_cleaning/test_service.py -k idempotency_lock_holds_postgres_session_lock_across_transaction_commits -q`
  - 结果：`1 passed, 6 deselected`
- 2）`pytest tests/features/markdown_cleaning/test_service.py -k concurrent_replay_multiple_rounds_keeps_single_dispatch_and_release_lock -q`
  - 结果：`1 passed, 6 deselected`
- 3）`pytest tests/features/markdown_cleaning/test_repository.py -k concurrent -q`
  - 结果：`1 passed, 8 deselected`
- 4）`pytest tests/features/markdown_cleaning/test_state_machine.py tests/features/markdown_cleaning/test_repository.py -q`
  - 结果：`21 passed`
- 5）`pytest tests/features/markdown_cleaning/test_service.py tests/features/markdown_cleaning/test_messages.py -q`
  - 结果：`10 passed`
- 6）`ruff check app/features/markdown_cleaning tests/features/markdown_cleaning`
  - 结果：失败（`UP043`, `B023` 5 个问题，均在 `tests/features/markdown_cleaning/test_service.py`）
- 7）`mypy app/features/markdown_cleaning tests/features/markdown_cleaning`
  - 结果：失败（`test_messages.py:44` unused type ignore，`test_service.py:129` no-any-return）

## 仓库状态核验
- 主仓库：`git -C C:\\Users\\wuchaoli\\Desktop\\codespace\\TextProcessor status --short` 为 `clean`
- worktree：`git -C C:\\Users\\wuchaoli\\Desktop\\codespace\\TextProcessor\\.worktrees\\markdown-cleaning-api status --short` 有 `backend/tests/features/markdown_cleaning/test_service.py` 的未提交改动（测试修订）

## 测试：稳定Markdown清洗并发锁验证（本轮收官）
- 执行命令与结果（均在 `POSTGRES_SERVER=127.0.0.1 POSTGRES_PORT=5433 POSTGRES_DB=app POSTGRES_USER=postgres POSTGRES_PASSWORD=changethis PROJECT_NAME=TextProcessor FIRST_SUPERUSER=admin@example.com FIRST_SUPERUSER_PASSWORD=changethis SECRET_KEY=changethis ENVIRONMENT=local` 下执行）：
  1. `ruff check app/features/markdown_cleaning tests/features/markdown_cleaning`
     - 结果：`All checks passed!`
  2. `mypy app/features/markdown_cleaning tests/features/markdown_cleaning`
     - 结果：`Success: no issues found in 18 source files`
  3. `pytest tests/features/markdown_cleaning/test_service.py -k "idempotency_lock_holds_postgres_session_lock_across_transaction_commits or concurrent_replay_multiple_rounds_keeps_single_dispatch_and_release_lock" -q`
     - 结果：`2 passed, 5 deselected`
  4. `pytest tests/features/markdown_cleaning/test_service.py tests/features/markdown_cleaning/test_messages.py -q`
     - 结果：`10 passed`
- 说明：`test_service.py` 中新增/变更的 `advisory-lock` 闭包与事务路径已消除 Ruff B023、UP043 与 MyPy `no-any-return`/类型告警；`test_messages.py` 现仅保留显式类型兼容实现，未新增静态告警。
- 最新 worktree 状态：`git -C C:\\Users\\wuchaoli\\Desktop\\codespace\\TextProcessor\\.worktrees\\markdown-cleaning-api status --short`（仅工作区文件为本轮修订，需你决定是否提交）
- 主仓库状态：`git -C C:\\Users\\wuchaoli\\Desktop\\codespace\\TextProcessor status --short` 依旧 `clean`

## Task4-round4（本轮修订）
- 约束：仅修改 `backend/tests/features/markdown_cleaning/test_service.py`（本次同时同步补充本文件的并发验证记录）。
- 处理：
  - 使用 `RepositoryProbe + ProbeMarkdownCleaningTaskRepository` 做测试侧事件钩子：`PENDING->QUEUED` 提交完成、`QUEUED->FAILED` 进入/提交、锁释放三段。
  - 用只读查询 `pg_locks` + `pg_stat_activity` 校验 advisory lock 持有与释放；移除对 `pg_try_advisory_lock` 的直接 acquire/unlock 干预。
  - 并发两线程场景改为“等待首线程完成 `PENDING->QUEUED` 提交后再启动第二线程”，并对 futures/事件全部设 `timeout=10.0`。
  - 通过返回的 `task_id`（或 `dispatcher.task_ids[0]`）按 ID 查询持久化状态断言，不再仅按幂等键查询。
- 当前变更后的并发预期：两线程皆返回 `503` 安全错误、`dispatcher.task_ids` 长度为 1、最终状态为 `FAILED` 且错误信息为 `任务提交失败`，`pg_locks` 无残留。

## Task4-round5（显式5433收官验证）
- 执行环境：`POSTGRES_SERVER=127.0.0.1 POSTGRES_PORT=5433 POSTGRES_DB=app POSTGRES_USER=postgres POSTGRES_PASSWORD=changethis PROJECT_NAME=TextProcessor FIRST_SUPERUSER=admin@example.com FIRST_SUPERUSER_PASSWORD=changethis SECRET_KEY=changethis ENVIRONMENT=local`（`worktree` 下 `backend`）
- 验证命令与结果：
  1. `pytest backend/tests/features/markdown_cleaning/test_service.py -k "idempotency_lock_holds_postgres_session_lock_across_transaction_commits or concurrent_replay_waits_for_lock_and_returns_safe_503 or concurrent_replay_multiple_rounds_keeps_single_dispatch_and_release_lock" -q`
     - 结果：`3 passed, 4 deselected`
  2. `pytest backend/tests/features/markdown_cleaning/test_repository.py -k concurrent -q`
     - 结果：`1 passed, 8 deselected`
  3. `pytest backend/tests/features/markdown_cleaning/test_service.py backend/tests/features/markdown_cleaning/test_messages.py -q`
     - 结果：`10 passed`
  4. `ruff check backend/app/features/markdown_cleaning backend/tests/features/markdown_cleaning`
     - 结果：`All checks passed!`
  5. `mypy backend/app/features/markdown_cleaning backend/tests/features/markdown_cleaning`
     - 结果：`Success: no issues found in 18 source files`
- 修复点：
  - `psycopg` 场景改为顺序参数 (`%s` + tuple) 进行 `pg_locks/pg_stat_activity` 查询，避免 `:name` 占位符报错。
  - `error_code` 持久化断言改为字符串值比较。
  - `_run_create_task_once` dispatcher 类型收敛为 `Protocol`，兼容 `Dispatcher` 与 `BlockingDispatcher`，修复 mypy 三处报错。

## Task4-round6（收敛并发probe，重写真实并发验收）
- 目标：去掉复杂 probe/事件阻塞的并发实现，改成 `Barrier` 驱动的自包含并发回归；保留普通单测。
- 文件：
  - `backend/tests/features/markdown_cleaning/test_service.py`
  - `task-4-report.md`
- 本轮改动：
  - 删除 `RepositoryProbe`、`ProbeMarkdownCleaningTaskRepository`、`BlockingDispatcher`、`DispatcherProtocol`、3 个旧并发测试与事件/尝试锁侧的辅助状态机。
  - `Dispatcher` 改为：每次入参 dispatch 都计数，失败场景不写入 `task_ids`，成功场景写入 `task_ids`，便于区分“尝试次数”和“成功入队”。
  - `_run_create_task_once` 改为接收 `threading.Barrier`，两个线程在同一轮并发中同时进入 `create_task`。
  - 新增 `test_concurrent_replay_returns_safe_503_and_releases_advisory_lock_in_10_rounds`：10 轮循环，每轮 `caller/session/file/paths` 唯一；两线程并发提交，`future` 超时 10 秒；断言两者返回 `QUEUE_SUBMISSION_FAILED`/503；断言 `dispatcher.call_count == 1` 与 `dispatcher.task_ids == []`；按实际 task id 断言最终 FAILED 与错误字段；用 `pg_locks` 校验对应 advisory key 无 `granted` 锁残留。
  - 保留 `skip` 规则：仅在 PostgreSQL 可达时运行并发用例；连接失败则 `skip`，不吞掉真实逻辑失败。
- 当前状态：尚未在本轮新增执行命令（仅按要求先完成并发测试收敛与报告更新，若你要我再跑 5433 全量可继续）。

## Task4-round7（flaky 收尾清理）
- 结论：按要求执行 `test_service.py` 回退，恢复到 `aa652dd` 的稳定测试形态，并删除本轮未提交内容中的复杂测试辅助（`fd22` 及此前 50/50 诊断相关引入的）：
  - 复杂锁生命周期/多轮重放实现
  - probe 与 `Barrier`/事件辅助路径
  - 额外 helper 与状态收集函数
  - `idempotency_lock`/`concurrent` 并发的超复杂覆盖路径
- 保留项：`backend/tests/features/markdown_cleaning/test_messages.py` 中 `fd22f10` 的静态修复保持不变（不回退）。
- 验证结果（本次）：
  - 默认环境（`POSTGRES_PORT=5432`）：`test_service` `4 passed, 1 skipped`
  - 显式 `5433`：`test_service` `5 passed`
  - 仓库并发回归：`backend/tests/features/markdown_cleaning/test_repository.py -k concurrent` `1 passed`
  - 路由相关：`backend/tests/features/markdown_cleaning/test_api_contract.py` `1 passed, 8 deselected`
  - `ruff check backend/app/features/markdown_cleaning backend/tests/features/markdown_cleaning`：`UP043` 1 条，建议移除 Generator 默认类型参数（可修复）
  - `mypy backend/app/features/markdown_cleaning backend/tests/features/markdown_cleaning`：`Success: no issues found in 18 source files`
- 说明：未重新声明 50/50 反复轮次运行；仅保留“并发与连接诊断”为证据边界说明，不夸大为 50 次重复执行。

## 测试：移除Markdown清洗并发假阴性用例
- 处理：最小修复 `backend/tests/features/markdown_cleaning/test_service.py` 中 `UP043`（`Generator` 注解默认参数，改为 `Generator[None]`），不恢复任何并发假阴性测试逻辑，继续保留 `aa652dd` 的稳定用例边界。
- 验证命令与结果：
  - `ruff check backend/app/features/markdown_cleaning backend/tests/features/markdown_cleaning`
    - `All checks passed!`
  - `mypy backend/app/features/markdown_cleaning backend/tests/features/markdown_cleaning`
    - `Success: no issues found in 18 source files`
  - `POSTGRES_SERVER=127.0.0.1 POSTGRES_DB=app POSTGRES_USER=postgres POSTGRES_PASSWORD=changethis PROJECT_NAME=TextProcessor FIRST_SUPERUSER=admin@example.com FIRST_SUPERUSER_PASSWORD=changethis SECRET_KEY=changethis POSTGRES_PORT=5432 uv run --project backend pytest backend/tests/features/markdown_cleaning/test_service.py -q --maxfail=1`
    - `4 passed, 1 skipped`
  - `POSTGRES_SERVER=127.0.0.1 POSTGRES_DB=app POSTGRES_USER=postgres POSTGRES_PASSWORD=changethis PROJECT_NAME=TextProcessor FIRST_SUPERUSER=admin@example.com FIRST_SUPERUSER_PASSWORD=changethis SECRET_KEY=changethis POSTGRES_PORT=5433 uv run --project backend pytest backend/tests/features/markdown_cleaning/test_service.py -q --maxfail=1`
    - `5 passed`
- 结论：`test_service.py` 质量门禁恢复通过；`test_messages.py (fd22f10)` 保持不变。
