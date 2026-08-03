# Task 7 实施与验证记录

## 交付

- `scripts/verify-markdown-cleaning-stack.ps1`：每次创建唯一 PostgreSQL 18、Redis 7 容器和随机回环端口，以本地独立进程启动 FastAPI、Celery solo worker 与 beat；真实迁移、创建用户、HTTP 登录、POST 和 GET，不使用 mock、eager 或 TestClient。
- `docs/runbooks/markdown-cleaning.md`：记录前置条件、运行命令、验收项、配置、安全边界、排障和精确残留核验。
- Windows publisher 修复：真实栈发现 `.markdown-cleaning-publish-*.tmp` 残留；改为关闭写入 fd 后，通过 pinned parent handle 和相对名称执行 `NtDeleteFile`，不使用存在路径竞态的绝对路径 unlink。

## RED → GREEN

- RED 1：脚本不存在，PowerShell 报 `The term './scripts/verify-markdown-cleaning-stack.ps1' is not recognized`。
- RED 2：初版真实启动分别暴露 Alembic cwd、单元素 JSON array、Windows GBK FastAPI CLI 编码问题，逐项修正为 backend cwd、显式 JSON array、`PYTHONUTF8=1` 与 uvicorn。
- RED 3：真实 canonical 已成功后，输出目录仍有 `.markdown-cleaning-publish-<uuid>.tmp`；新增 publisher 成功和 link failure 回归均失败。旧 `SetFileInformationByHandle(FileDispositionInfo)` 实测返回 `WinError 5`。
- GREEN publisher：使用 pinned parent + relative name 的 `NtDeleteFile` 后 focused `2 passed`，完整 publisher `22 passed`，Ruff 通过；真实栈严格断言 canonical 时输出目录精确一个文件且就是业务 target。
- RED 4：恢复样本先后触发处理器 block/token/PII 限制，稳定终态 `failed/INVALID_MARKDOWN_INPUT`；改用低于所有限制的单 block、无 PII 大段落，不放宽处理器或断言。
- GREEN stack：`pwsh -NoProfile -File scripts/verify-markdown-cleaning-stack.ps1 -TimeoutSeconds 300`，exit `0`，摘要：`MARKDOWN_CLEANING_STACK_OK runId=5a31d5e38d67 ... elapsedSeconds=33.54 images=[postgres:18, redis:7-alpine]`。

## 真实全栈证据

- canonical 固定中文 fixture 经真实 token/HTTP API 完成，结果与 expected 逐字节一致；重复段落、五类脱敏及格式化全部统计一致；DB 为 `succeeded:1`。
- canonical 输出目录在断言时精确一个文件；重复 POST 返回相同 taskId，真实 Redis 重投 execute 后 attempt count 仍为 1。
- 预存目标返回 `OUTPUT_CONFLICT`，`DO-NOT-OVERWRITE` 原字节不变，失败 API 没有内部 result/staging 路径。
- recovery 任务由 worker1 取得 `running` lease 后真实终止完整进程树；5 秒 lease 过期后 beat 投递真实 recovery，worker2 接管，最终 `succeeded`、attempt count 为 2，只有一个 recovery target，API 始终返回业务 targetPath。

## Cleanup 与边界

- 正常和异常均进入 `finally`；只停止脚本记录的进程树、删除两个精确容器名和本次随机临时目录。
- 强制中断 PowerShell 进程本身无法保证 `finally`，runbook 提供按输出 `runId` 的精确人工清理方式，禁止宽泛删除。
- 不声称或启动 DataJuicer/Docling；fixture 只是经真实 HTTP 提交的输入与 expected oracle，不替代真实服务。

## fix round1/5

- publisher 新增 cleanup failure 回归：目标已 hardlink/fsync 后模拟 Windows `NtDeleteFile`（POSIX 对应 `unlink`）失败，仍返回成功且目标字节存在；只记录 `cleanup_stage=temporary` 的安全 warning，不记录异常或路径。link 提交前失败仍保持原 `PublicationSystemError`。
- 脚本每启动一个 API/worker/beat launcher 就更新本次临时目录中的 `ownership.json`，记录 `runId/name/pid/apiPort`；初始进程全部启动后输出单行 `MARKDOWN_CLEANING_STACK_STARTED`。runbook 使用 manifest 精确 `taskkill /PID /T /F`，并按 PID 与 API port 双重核验，不使用宽泛进程名。
- fresh 验证：publisher `23 passed, 4 warnings in 3.26s`，Ruff `All checks passed!`，PowerShell AST parser 无错误；真实全栈 exit `0`，`runId=8b32e9457546`，启动摘要记录 API port `59935` 与 launcher PIDs `20476,34204,23932`，最终耗时 `34.78s`、镜像仍为 `postgres:18` 与 `redis:7-alpine`。正常 `finally` 后该 runId 的容器、进程、端口、临时目录和 manifest 均无残留。
