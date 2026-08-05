# 结构化提取运行手册

## Docling 合并服务

Docling 作为独立的 HTTP 服务部署。API 与 RQ Worker 是同一容器内的两个进程，
由 Python PID 1 监管器统一启动、停止和监视。TextProcessor 只调用其 v1 API，
不读取 Docling RQ 数据，也不向 Docling 传入业务侧 URL。

首次启动前，从 `.env.example` 复制 Docling 变量到部署环境，替换 API key。
镜像必须使用明确版本和 digest，禁止使用 `latest`。

```powershell
docker compose -f compose.yml -f compose.docling.yml up -d redis docling-api
.\scripts\verify-docling-deployment.ps1 -ComposeFiles @("compose.yml", "compose.docling.yml")
```

本地调试需要端口映射时额外加载 `compose.override.yml`。生产环境不加载该
override，因此 Docling API 不暴露宿主机端口。

```powershell
docker compose -f compose.yml -f compose.docling.yml -f compose.override.yml up -d redis docling-api
.\scripts\verify-docling-deployment.ps1 `
  -ComposeFiles @("compose.yml", "compose.docling.yml", "compose.override.yml") `
  -BaseUrl "http://localhost:5001" `
  -AllowPublishedPort
```

生产验证默认不允许 `docling-api` 发布宿主机端口；不传 `BaseUrl` 时，验证器会
从 API 容器内检查认证与 OpenAPI。使用隔离 Compose project 时，传入相同的
`-ComposeProjectName` 和 `-ComposeFiles`，例如
`-ComposeProjectName textprocessor-docling-smoke`。

## TextProcessor Worker 运行栈

TextProcessor 的 `redis` 是 Celery broker，持久化到
`textprocessor-redis-data`。Celery 使用 DB 0，Docling RQ 使用同一实例的 DB 1。
logical DB 只隔离键空间，不隔离内存、CPU、连接、持久化或故障域。启动 API、
worker 和 beat 时使用同一 Compose project：

```powershell
docker compose -f compose.yml -f compose.docling.yml -f compose.override.yml up -d `
  db redis prestart backend extraction-worker extraction-beat `
  docling-api
.\scripts\verify-extraction-stack.ps1
```

验证器逐阶段报告 DB、共享 Redis、backend、worker、beat、MinerU 和 Docling
的状态。MinerU 是外部服务，必须在启动环境中提供
`EXTRACTION_WORKER__MINERU_BASE_URL`。默认只验证 worker 运行时的消息身份
契约及 `acks_late`/recovery 配置。Redis 的
`CELERY_BROKER_VISIBILITY_TIMEOUT_SECONDS` 默认值为 3660 秒，部署值必须覆盖最长的
正常任务；这是容器突然终止后迟确认消息重新可见的上限。在专用验收环境可增加
`-ExerciseWorkerLossRecovery`：验证器会使用临时 MinerU stub、短 visibility timeout，
仅在外部 task id 已持久化且任务进入 polling 后向 worker 发送 `SIGKILL`，重启后断言
同一任务成功且仅发布一个 Markdown 文件。不要在生产运行该选项。

## 真实格式验收

真实外部服务验收与默认测试集分离。只有获得上传授权的脱敏样本可以提交给
MinerU 或 Docling；业务样本不得提交到仓库、测试输出或日志。每次验收使用临时
目录，并只保存格式、服务版本（如响应提供）、耗时、状态、结果大小和摘要，不
保存 API key、样本绝对路径、原始文档或 Markdown 正文。

MinerU 需要当前可达的服务 URL，以及 PDF、图片、PPTX 各一个授权样本。
legacy `.doc` 和 `.ppt` 在首版明确禁用，不得路由到处理器或提交到 smoke；调用方
应先转换为 `.docx` 或 `.pptx`。API key 是否需要取决于部署配置：

```powershell
.\scripts\smoke-mineru.ps1 `
  -BaseUrl $env:EXTRACTION_WORKER__MINERU_BASE_URL `
  -ApiKey $env:EXTRACTION_WORKER__MINERU_API_KEY `
  -SamplePath @(
  $env:MINERU_SMOKE_PDF,
  $env:MINERU_SMOKE_IMAGE,
  $env:MINERU_SMOKE_PPTX
)
```

Docling 需要当前可达的服务 URL、API key，以及普通 DOCX、XLSX、HTML、EPUB
各一个授权样本：

```powershell
.\scripts\smoke-docling.ps1 `
  -BaseUrl $env:EXTRACTION_WORKER__DOCLING_BASE_URL `
  -ApiKey $env:EXTRACTION_WORKER__DOCLING_API_KEY `
  -SamplePath @(
  $env:DOCLING_SMOKE_DOCX,
  $env:DOCLING_SMOKE_XLSX,
  $env:DOCLING_SMOKE_HTML,
  $env:DOCLING_SMOKE_EPUB
)
```

两个脚本均会临时设置 `*_REAL_INTEGRATION=1` 和样本映射，随后运行带
`real_integration` marker 的测试；它们不打印 secret、样本路径或文档正文。也可由
受控环境直接调用，但必须先设置对应的 `MINERU_REAL_INTEGRATION=1`、
`DOCLING_REAL_INTEGRATION=1`、连接配置和完整样本映射；缺少 opt-in 时测试会按
设计 skip，因此不能把命令退出码单独作为通过证据：

```powershell
Set-Location backend
uv run pytest -m real_integration tests/integration/structured_extraction -q
```

一次完整的 Docling 重启恢复验收必须在 pending 与 started 状态分别重启合并的
`docling-api` 容器和共享 `redis`，并记录任务恢复或明确失败的结果；不要将单纯
healthcheck 当作恢复证据。容量基线同样需要在独立环境测量每个 processor 的
单任务与并发资源使用。

当前源码和 smoke 脚本本身不构成任何外部格式“已通过”的证据。仅在对应的真实
命令成功执行并保留脱敏摘要后，才可将格式加入 production allowlist；`.wps`、`.et`、
`.dps` 和 `.ofd` 保持禁用。

停止服务：

```powershell
docker compose -f compose.yml -f compose.docling.yml down
```

不要在正常停止时增加 `--volumes`。`docling-model-cache` 保存模型缓存，
`textprocessor-redis-data` 同时保存 Celery DB 0 与 Docling RQ DB 1。删除共享
Redis volume 会同时丢失两套队列状态。

## 升级

1. 从 Docling Serve 官方发布页选择明确版本。
2. 拉取镜像并记录多架构 manifest digest。
3. 更新 `DOCLING_BASE_IMAGE` 的版本和 digest，并为包装镜像使用应用 release tag。
4. 先在隔离环境回读 `/openapi.json`，运行部署验证和真实格式 smoke。
5. 只有逐格式验证通过的格式才能加入 production allowlist。

## 故障检查

- API 不健康：检查 `docling-api` 日志、API key、模型缓存和内存。
- 任务不推进：读取容器内 `/run/textprocessor-docling/processes.json`，确认 `api`
  与 `worker` PID 存活，再用 `redis-cli -n 1` 检查 Docling RQ；不要输出 job payload。
- Redis 不健康：检查共享 Redis 容器、DB 0/DB 1 和整体资源使用。不要执行
  `FLUSHALL`，也不要跨 logical DB 清理键。
- 结果查询失败：确认 Redis volume 未被删除，且任务未超过结果保留期。

日志、错误响应和验证产物中不得记录 API key、Redis job payload 或原始文档正文。
