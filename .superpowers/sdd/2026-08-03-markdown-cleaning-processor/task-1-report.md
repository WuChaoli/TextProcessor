# Task 1 执行报告（Markdown Cleaning Processor 契约）

## 红灯（先前失败）
- 命令：
  - `uv run --project backend pytest backend/tests/features/markdown_cleaning/processors/test_protocol.py backend/tests/features/markdown_cleaning/processors/test_dependency_compatibility.py -q`
- 现象：测试在加载 `backend/tests/conftest.py` 时失败，`pydantic_settings` 校验缺少必填项。
- 关键报错：
  - `ValidationError: 5 validation errors for Settings`
  - `PROJECT_NAME`, `POSTGRES_SERVER`, `POSTGRES_USER`, `FIRST_SUPERUSER`, `FIRST_SUPERUSER_PASSWORD` 均为 missing
- 结论：环境未注入必填配置，不是契约代码本身语法或断言问题。
- 另有首次运行 Ruff 时失败：`UP043/I001/F401/C416`（后续详见后续修复）。

## 修复与实现动作
- 新建/补齐契约文件：
  - `backend/app/features/markdown_cleaning/processors/__init__.py`
  - `backend/app/features/markdown_cleaning/processors/protocol.py`
  - `backend/app/features/markdown_cleaning/processors/models.py`
  - `backend/app/features/markdown_cleaning/processors/errors.py`
- 新建契约测试：
  - `backend/tests/features/markdown_cleaning/processors/conftest.py`
  - `backend/tests/features/markdown_cleaning/processors/test_protocol.py`
  - `backend/tests/features/markdown_cleaning/processors/test_dependency_compatibility.py`
- 新增依赖约束（Python 3.14 兼容）：
  - `markdown-it-py>=4.2,<5`
  - `presidio-analyzer>=2.2.363,<3`
  - `mdformat>=1,<2`
  - `mdformat-gfm>=1,<2`
  - 体现在 `backend/pyproject.toml` 与 `uv.lock`
- `backend/tests/features/markdown_cleaning/processors/conftest.py` 覆盖同名 `db` fixture（`scope=session, autouse=True`），使目标测试在该目录下不触发 `backend/tests/conftest.py` 的全局数据库初始化路径。
- 调整测试 import 与注释告警，修复 Ruff 指出项。

## 验证命令与结果
- 命令：
  - `uv run --project backend pytest backend/tests/features/markdown_cleaning/processors/test_protocol.py backend/tests/features/markdown_cleaning/processors/test_dependency_compatibility.py -q`
- 结果：`4 passed, 1 warning in 1.28s`
- 命令：
  - `uv run --project backend ruff check backend/app/features/markdown_cleaning/processors backend/tests/features/markdown_cleaning/processors`
- 结果：`All checks passed!`
- 命令：
  - `uv run --project backend mypy backend/app/features/markdown_cleaning/processors`
- 结果：`Success: no issues found in 4 source files`
- 命令：
  - `uv run --project backend pyright backend/app/features/markdown_cleaning/processors`
- 结果：`0 errors, 0 warnings, 0 informations`
- 命令：
  - `uv run --project backend ty check backend/app/features/markdown_cleaning/processors`
- 结果：`All checks passed!`
- 说明：`test_dependency_compatibility.py` 已验证 `markdown-it-py`/`mdformat`/`mdformat-gfm`/`presidio_analyzer` 在当前 Python 3.14 环境可导入。

## 结论
Task 1 已完成绿灯：契约代码、测试与依赖兼容性路径全部可执行并通过。

## Fix Round 1（审查反馈闭环）
- 已确认：`map_processing_exception` 接受可选 `error_code`，用于显式回退到固定稳定 code；默认分支仍保留超时、输入、输出、内部错误推断。
- 已确认：`MarkdownCleaningSummary` 与 `ProcessorResult` 增加不变性校验，防止负数计数、非法 sha256 与负字节数。
- 已修正文档措辞：该目录 `conftest.py` 通过定义同名 `db` fixture 覆盖（shadow）全局 `db` fixture，未修改 `backend/tests/conftest.py` 全局实现或其 side-effect。

### 追加验证（Fix Round 1）
- 首轮修复后失败案例：
  - `uv run --project backend pytest backend/tests/features/markdown_cleaning/processors/test_protocol.py -q`
  - 现象：`self.__dict__` 访问在 `slots=True` dataclass 上失败。
- 再次修复后绿灯结果：
  - `uv run --project backend pytest backend/tests/features/markdown_cleaning/processors/test_protocol.py -q`
  - 结果：`4 passed, 1 warning in 0.04s`
  - 该 warning 为 `fastapi.testclient` 运行时 `StarletteDeprecationWarning`（与本次变更无关）。
  - `uv run --project backend ruff check backend/app/features/markdown_cleaning/processors backend/tests/features/markdown_cleaning/processors`
  - 结果：`All checks passed!`
  - `uv run --project backend mypy backend/app/features/markdown_cleaning/processors`
  - 结果：`Success: no issues found in 4 source files`
  - `uv run --project backend pyright backend/app/features/markdown_cleaning/processors`
  - 结果：`0 errors, 0 warnings, 0 informations`
  - `uv run --project backend ty check backend/app/features/markdown_cleaning/processors`
  - 结果：`All checks passed!`

## Fix Round 2（审查反馈闭环）
- 已补齐 `map_processing_exception` 默认路径覆盖：`ValueError("secret")` 不携带 phase 参数时命中 `INVALID_MARKDOWN_INPUT`。
- 已补齐非法结果测试：
  - 非法 `output_sha256`（长度/非十六进制）触发 `ValueError`。
  - `output_bytes=-1` 触发 `ValueError`。
- 已直接修正文档第29行误述，改为准确描述：该目录 `conftest.py` 覆盖同名 `db` fixture，而未改动全局 `backend/tests/conftest.py`。

### 追加验证（Fix Round 2）
- `uv run --project backend pytest backend/tests/features/markdown_cleaning/processors/test_protocol.py -q`
  - `4 passed, 1 warning in 0.05s`
- `uv run --project backend ruff check backend/app/features/markdown_cleaning/processors backend/tests/features/markdown_cleaning/processors`
  - `All checks passed!`
- `uv run --project backend mypy backend/app/features/markdown_cleaning/processors`
  - `Success: no issues found in 4 source files`
- `uv run --project backend pyright backend/app/features/markdown_cleaning/processors`
  - `0 errors, 0 warnings, 0 informations`
- `uv run --project backend ty check backend/app/features/markdown_cleaning/processors`
  - `All checks passed!`
