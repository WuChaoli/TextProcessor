# 结构化提取运行手册

## Docling 独立服务

Docling 作为独立的 HTTP 服务部署。TextProcessor 只调用其 v1 API，不读取
Docling 专用 Redis，也不向 Docling 传入业务侧 URL。

首次启动前，从 `.env.example` 复制 Docling 变量到部署环境，替换两个 secret。
镜像必须使用明确版本和 digest，禁止使用 `latest`。

```powershell
docker compose -f compose.yml -f compose.docling.yml up -d docling-redis docling-api docling-worker
.\scripts\verify-docling-deployment.ps1
```

本地调试需要端口映射时额外加载 `compose.override.yml`。生产环境不加载该
override，因此 Docling API 不暴露宿主机端口。

```powershell
docker compose -f compose.yml -f compose.docling.yml -f compose.override.yml up -d docling-redis docling-api docling-worker
```

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
