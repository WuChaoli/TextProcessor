# 结构化提取运行手册

## Docling 独立服务

Docling 作为独立的 HTTP 服务部署。TextProcessor 只调用其 v1 API，不读取
Docling 专用 Redis，也不向 Docling 传入业务侧 URL。

首次启动前，从 `.env.example` 复制 Docling 变量到部署环境，替换两个 secret。
镜像必须使用明确版本和 digest，禁止使用 `latest`。

```powershell
docker compose -f compose.yml -f compose.docling.yml up -d docling-redis docling-api docling-worker
.\scripts\verify-docling-deployment.ps1 -ComposeFiles @("compose.yml", "compose.docling.yml")
```

本地调试需要端口映射时额外加载 `compose.override.yml`。生产环境不加载该
override，因此 Docling API 不暴露宿主机端口。

```powershell
docker compose -f compose.yml -f compose.docling.yml -f compose.override.yml up -d docling-redis docling-api docling-worker
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
`textprocessor-redis-data`；它与 `docling-redis` 的 service、volume 和 URL
完全独立。启动 API、worker 和 beat 时使用同一 Compose project：

```powershell
docker compose -f compose.yml -f compose.docling.yml -f compose.override.yml up -d `
  db redis prestart backend extraction-worker extraction-beat `
  docling-redis docling-api docling-worker
.\scripts\verify-extraction-stack.ps1
```

验证器逐阶段报告 DB、两套 Redis、backend、worker、beat、MinerU 和 Docling
的状态。MinerU 是外部服务，必须在启动环境中提供
`EXTRACTION_WORKER__MINERU_BASE_URL`。默认只验证 worker 运行时的消息身份
契约及 `acks_late`/recovery 配置；在专用验收环境可增加
`-ExerciseWorkerLossRecovery`，它会向 worker 发送 `SIGKILL` 并等待同一服务
恢复健康。不要在生产运行该选项。

停止服务：

```powershell
docker compose -f compose.yml -f compose.docling.yml down
```

不要在正常停止时增加 `--volumes`。`docling-model-cache` 保存模型缓存，
`docling-redis-data` 保存异步任务队列和结果。删除 volume 会丢失这些数据。

## 升级

1. 从 Docling Serve 官方发布页选择明确版本。
2. 拉取镜像并记录多架构 manifest digest。
3. 更新 `DOCLING_IMAGE` 的版本和 digest。
4. 先在隔离环境回读 `/openapi.json`，运行部署验证和真实格式 smoke。
5. 只有逐格式验证通过的格式才能加入 production allowlist。

## 故障检查

- API 不健康：检查 `docling-api` 日志、API key、模型缓存和内存。
- 任务不推进：检查 `docling-worker` 是否运行，并确认它与 API 使用相同的
  `DOCLING_SERVE_ENG_RQ_REDIS_URL`。
- Redis 不健康：检查 password 是否在服务、API、worker 三处一致；不得改用
  TextProcessor 的 Celery Redis。
- 结果查询失败：确认 Redis volume 未被删除，且任务未超过结果保留期。

日志、错误响应和验证产物中不得记录 API key、Redis password 或原始文档正文。
