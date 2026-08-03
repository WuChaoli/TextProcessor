# Task 3 修复报告：结果校验与无覆盖原子发布

## 修复结论

- Publisher 在 POSIX 通过已验证父目录 `dir_fd` 执行 `O_NOFOLLOW`、`O_EXCL`、同目录 hard-link no-replace 与目录 fsync。
- Publisher 在 Windows 通过 `CreateFileW(...OPEN_REPARSE_POINT)` 固定父目录句柄，随后使用相对该句柄的 `NtCreateFile` 创建临时文件，并以 `NtSetInformationFile(FileLinkInformation)` 禁止覆盖发布。父目录路径在校验后被 junction 替换时，发布仍落入已固定目录，外部目录保持未触碰。
- hard-link/no-replace 不可用时返回独立安全错误，错误不包含内部绝对路径。
- 文件 fsync 失败始终上抛；仅 Windows 已知不支持目录 fsync 的路径不执行目录 fsync，POSIX 目录 fsync 失败不豁免。
- Output validator 以 no-follow 文件描述符读取 source/output，验证精确预期输出路径、真实摘要/长度、UTF-8 无 BOM、LF、恰一个末尾换行、保护区顺序/类型/父级/内容不变，以及无内部临时 token。

## RED → GREEN 证据

- 初始隔离测试：`3 failed, 31 passed`，确认 Windows 多进程发布和文件句柄/fsync 缺陷。
- 增加 reviewer 回归后：`9 failed, 31 passed`，新增失败覆盖 parent junction swap、保护区内容/顺序/父级、内部 token 与非预期 fsync 错误。
- 修复后两组 Task3 测试：`40 passed in 3.36s`。

## 验证命令

- `uv run --project backend pytest --confcutdir=backend/tests/features backend/tests/features/markdown_cleaning/test_output_validator.py backend/tests/features/markdown_cleaning/test_publisher.py -q`
  - `40 passed in 3.36s`
- `uv run --project backend ruff check <Task3 files>`
  - `All checks passed!`
- `uv run --project backend ruff format --check <Task3 files>`
  - `4 files already formatted`
- `uv run --project backend mypy <Task3 production files>`
  - `Success: no issues found in 2 source files`
- `uv run --project backend pyright <Task3 production files>`
  - `0 errors, 0 warnings, 0 informations`
- `uv run --project backend ty check <Task3 production files>`
  - `All checks passed!`

## 环境边界

仓库根 `backend/tests/conftest.py` 会无条件连接 PostgreSQL；当前 `.env` 的本机 PostgreSQL 凭据不可用。因此 Task3 纯文件系统单测使用 `--confcutdir=backend/tests/features` 隔离无关数据库 fixture，未声称数据库集成测试通过。
