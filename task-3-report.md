# Task 3 实施与验证记录

## 代码改动
- 新增 `backend/app/features/markdown_cleaning/task_models.py`（任务模型定义）
- 新增 `backend/app/features/markdown_cleaning/repository.py`（仓储层逻辑）
- 新增 Alembic 迁移 `backend/app/alembic/versions/20260803_01_add_markdown_cleaning_tasks.py`
- 调整 `backend/app/models.py`（按工作区现状对模型声明进行了必要同步）
- 新增 `backend/tests/features/markdown_cleaning/test_repository.py`（仓储行为测试）
- 修订 `backend/app/features/markdown_cleaning/state_machine.py` 与 `backend/tests/features/markdown_cleaning/test_state_machine.py`，支持 `QUEUED -> FAILED` 直接转移

## 变更范围对应状态
- 仅在 `.worktrees/markdown-cleaning-api` 内完成开发与修改
- 已清理原始 checkout 的误改：`backend/app/models.py` 与 Task3 新增文件不在原始 checkout 保留

## 验证执行记录
- `uv run --project . alembic heads`（worktree/backend，环境变量 `POSTGRES_SERVER=127.0.0.1 POSTGRES_PORT=5433 POSTGRES_DB=app POSTGRES_USER=postgres`）通过，显示 `20260803_01`
- `uv run --project . alembic upgrade head`（同上环境）通过；目标库 `app` 已有完整基线表
- `uv run --project backend pytest backend/tests/features/markdown_cleaning/test_state_machine.py backend/tests/features/markdown_cleaning/test_repository.py -q`（同上环境）通过，`21 passed`
- `uv run --project backend ruff check backend/app/features/markdown_cleaning backend/tests/features/markdown_cleaning` 通过
- `uv run --project backend mypy backend/app/features/markdown_cleaning backend/tests/features/markdown_cleaning`：剩余 3 个已存在问题，位于 `backend/tests/features/markdown_cleaning/test_api_contract.py`（`None` 赋值与 `str` 类型不匹配），与本次改动无关

## 风险与待确认
- 继续保留 `.env` 定位的数据库配置说明（本路径仅在 `backend/app/core/config.py` 的 `env_file` 指向 `backend/.env`，未设置时会导致连接空库）；建议在新环境运行前统一设置 `POSTGRES_DB`

## 追加（补丁 20260803_02）
- 新增 Alembic 迁移 `backend/app/alembic/versions/20260803_02_set_markdown_cleaning_task_defaults.py`（`down_revision=20260803_01`），对已执行 `20260803_01` 的旧库补齐 `processor_contract_version` 与 `max_attempts` 的 `server_default`。
- 调整 `backend/tests/features/markdown_cleaning/test_repository.py` 中 `test_task_columns_have_database_defaults` 的 migration 文件路径，改为 `Path(__file__)` 相对计算，避免依赖进程 cwd。

## 追加验证结果（已执行）
- 已在 `.venv` 环境下执行：
  - `Set-Location backend; $env:POSTGRES_SERVER='127.0.0.1'; $env:POSTGRES_PORT='5433'; $env:POSTGRES_DB='app'; $env:POSTGRES_USER='postgres'; $env:POSTGRES_PASSWORD='changethis'; $env:PROJECT_NAME='TextProcessor'; $env:FIRST_SUPERUSER='admin@example.com'; $env:FIRST_SUPERUSER_PASSWORD='changethis'; $env:SECRET_KEY='changethis'; $env:ENVIRONMENT='local'; uv run --project . alembic upgrade head`  
    - 升级到 `20260803_02` 成功。
  - `uv run --project backend pytest backend/tests/features/markdown_cleaning/test_state_machine.py backend/tests/features/markdown_cleaning/test_repository.py -q`  
    - 通过：`21 passed`
  - `Set-Location backend; uv run --project . pytest tests/features/markdown_cleaning/test_state_machine.py tests/features/markdown_cleaning/test_repository.py -q`  
    - 通过：`21 passed`
  - `uv run --project backend python -c ...`（查询 `information_schema.columns`）  
    - `processor_contract_version` -> `"'markdown_cleaning_v1'::character varying"`  
    - `max_attempts` -> `'3'`
  - 全量新库校验（临时库）：
    - `CREATE DATABASE task3_tmp_default` + `alembic upgrade head` 成功升级到 `20260803_02`
    - `information_schema.columns` 查询返回 `[('max_attempts', '3'), ('processor_contract_version', "'markdown_cleaning_v1'::character varying")]`
    - 临时库已清理（`DROP DATABASE task3_tmp_default`）
