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

## Round 2 安全复核修复

- `prepare()` 后 source 若被修改，Publisher 会在复制完成后从临时文件描述符重新计算摘要和长度，并在 hard-link 前与 `PreparedMarkdownResult` 精确比对；不一致时删除本任务临时文件且不发布目标。
- 输出根必须预先存在并在 Publisher 初始化时记录文件系统 identity；发布时重新固定 root handle 并核对 identity，防止 root 自身被替换。
- 不再对完整目标路径调用 `mkdir(parents=True)` 或完整路径打开 parent。从 root handle 开始逐级处理每个祖先：POSIX 使用 `openat/mkdirat + O_DIRECTORY + O_NOFOLLOW`，Windows 使用相对 root/child handle 的 `NtCreateFile(FILE_DIRECTORY_FILE)` 创建或打开，随后才创建结果临时文件。
- 新增中间祖先 `root/a/b` 的 `a` 在下钻期间被 junction 替换回归：结果仍写入已固定的原目录对象，outside 目录为空。
- source 的 open/stat/read/decode 错误统一映射为安全的 `INVALID_MARKDOWN_INPUT`；missing、symlink/junction 不泄露真实路径。

Round 2 RED：新增四项回归初次运行 `3 failed, 1 passed`；失败分别对应 source 篡改、中间祖先未固定、missing source 原生异常泄漏。Round 2 GREEN：Task3 两组完整测试 `44 passed in 3.65s`，Windows 多进程与两类 junction 竞态专项 `3 passed in 3.23s`。
