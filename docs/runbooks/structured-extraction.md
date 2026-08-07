# 结构化提取运行手册

结构化提取通过 Backend API 创建和查询任务，由独立的 `task-runner` 容器执行 Celery 任务。Docling 是内部能力容器，不发布宿主机端口；其 API 与 RQ Worker 位于同一容器，使用共享 Redis DB 1。Celery 只使用 Redis DB 0。

## 启动与验证

仓库默认使用统一 Compose 部署。137 当前采用 systemd 运行 TextProcessor、外部
MinerU 和本机 Docling 的混合形态，其上线、开放格式与回滚步骤见
`docs/runbooks/structured-extraction-137-production.md`，不得直接用下面的全栈命令
替换正在运行的 systemd 服务。

部署迁移完成后启动八个生产服务：

```powershell
docker compose -f compose.yml -f compose.docling.yml up -d
pwsh -NoProfile -File scripts/verify-single-node-stack.ps1
```

只复核 Docling 内部能力边界时运行：

```powershell
pwsh -NoProfile -File scripts/verify-docling-deployment.ps1
```

旧入口 `verify-extraction-stack.ps1` 保留为统一验证器的兼容包装，不再启动独立 extraction worker/beat。

## 故障与恢复

- Backend API 停止不会终止已经入队或运行中的任务；任务状态仍由 PostgreSQL 持久化。
- `task-runner` 内任一 Worker/Beat 子进程退出时，PID 1 会终止另一个子进程并以非零码退出，由容器重启策略恢复两者。
- Docling API 或 RQ Worker 任一退出时，Docling 容器按同样原则整体重启；已进入 Redis DB 1 的任务可恢复。
- 排障依次检查 `docker compose ps`、`task-runner`/`docling` 日志、Redis DB 0/1、PostgreSQL 任务状态和结果 manifest。日志不得输出正文、令牌或宿主机绝对路径。

## 真实能力边界

统一验证器使用小型本地文本证明 API/Task Runner 的独立性，不证明真实 Docling 模型质量。大型 PDF、模型缓存、GPU/CPU 资源和外部处理器验收必须在目标主机使用明确 fixture 单独执行；未运行时记录为 `real_integration not run`。
