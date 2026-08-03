# Markdown 清洗真实全栈验收

## 用途与边界

`scripts/verify-markdown-cleaning-stack.ps1` 验证 Markdown 清洗 API、PostgreSQL、Redis、Celery worker 与 Celery beat 的真实协作。脚本不使用 mock、eager mode 或 `TestClient`，也不依赖 Docling/DataJuicer；Markdown 清洗由仓库内已验证 processor 执行。

脚本仅面向本地或 CI 验收。它创建带随机后缀的 PostgreSQL/Redis 容器、随机端口、临时输入/输出/staging 根和测试超级用户。`finally` 仅终止本次脚本记录的进程、删除本次容器和临时目录，不操作共享容器、数据库或业务文件。

## 前置条件与运行

- Windows PowerShell、Docker Engine、`uv` 可用。
- Docker 能拉取 `postgres:18` 和 `redis:7-alpine`。
- 本机随机回环端口可用；默认验收超时为 180 秒。

在仓库根目录运行：

```powershell
pwsh -NoProfile -File scripts/verify-markdown-cleaning-stack.ps1
```

慢速机器可提高单阶段超时：

```powershell
pwsh -NoProfile -File scripts/verify-markdown-cleaning-stack.ps1 -TimeoutSeconds 300
```

成功时退出码为 `0`，并输出一行 `MARKDOWN_CLEANING_STACK_OK` 摘要，其中包括随机 `runId`、任务 ID、耗时及实际容器镜像。失败时退出码非 `0`，输出 `MARKDOWN_CLEANING_STACK_FAILED` 和本次日志位置；随后仍执行清理。

## 验收内容

脚本执行真实 Alembic migration 和初始用户创建，通过 `/api/v1/login/access-token` 获取 token，再经 HTTP POST/GET 完成以下断言：

1. 固定中文 canonical fixture 的最终文件与 `expected.md` 逐字节一致，重复段落、phone、idCard、bankCard、email、ipv4 和格式化统计全部一致。
2. PostgreSQL 终态为 `succeeded`、首次执行 `attempt_count=1`，输出根只有一个 canonical 最终文件；API 的 `targetPath` 始终是调用方业务路径。
3. 同一 caller/session/file 的重复 POST 返回相同任务；真实 Redis 重复 execute 消息不会重复处理成功任务。
4. 预存在目标产生稳定 `OUTPUT_CONFLICT`，原字节不变，失败响应不返回内部结果或 staging 路径。
5. worker 取得真实 running lease 后被终止；租约过期后真实 beat 投递 recovery，新 worker 接管并以 `attempt_count=2` 完成，且只有一个恢复输出。

## 配置说明

脚本为子进程设置独立的 `POSTGRES_*`、`CELERY_BROKER_URL`、`SECRET_KEY`、`FIRST_SUPERUSER*`、`MARKDOWN_CLEANING_INPUT_ROOTS`、`MARKDOWN_CLEANING_OUTPUT_ROOTS` 与 `MARKDOWN_CLEANING_WORKER`。输入、输出和 staging 根互不重叠；输出只允许位于本次 output root。恢复验收将 lease 设为 5 秒、beat recovery interval 设为 1 秒，仅用于缩短本地验收时间，不是生产容量建议。

## 排障

- `docker run` 或镜像拉取失败：先运行 `docker info`，检查 Docker Engine 和镜像仓库连通性。
- API 未就绪：检查失败摘要中的 `api.stderr.log`，常见原因是端口策略、依赖未安装或配置校验失败。
- worker/beat 超时：检查 `worker-*.stderr.log`、`beat.stderr.log`，以及 Redis 容器日志；确认没有企业安全软件阻止本地子进程或回环端口。
- migration 或认证失败：检查 PostgreSQL 容器日志和当前 backend Alembic head；脚本不会复用本机 5432 数据库。
- 失败后核验残留：`docker ps -a --filter "name=tp-md-stack-"` 应为空；进程也不应包含该次输出的随机 `runId`。若 PowerShell 被强制杀死而来不及运行 `finally`，按摘要中的精确 `runId` 人工删除对应两个容器，勿使用宽泛清理命令。

## 安全边界

测试凭据和 secret 每次随机生成，只用于临时数据库。请求不会扩大文件、网络或凭据权限；URI allowlist 仍由生产配置校验。脚本不允许任意公网 URL，不覆盖已有目标，不记录原始文档正文，不把 staging 或宿主内部路径作为 API 业务结果。
