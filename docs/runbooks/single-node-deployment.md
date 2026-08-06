# 单机八容器部署与故障验收

## 生产拓扑

默认 Compose 配置必须恰好包含 `frontend`、`backend-api`、`task-runner`、`docling`、`classification`、`datajuicer`、`redis`、`db`。只有 Frontend 和 Backend API 加入 `traefik-public`；其余服务只在默认内部网络通信。Adminer 仅属于 `debug` profile。

`backend-api` 负责认证、校验、任务创建与查询；`task-runner` 在一个容器中监管 Celery Worker 和 Beat。四项后台能力——结构化提取、全局去重、文本分类、Markdown 清洗——均使用 `POST 202 + taskId` 和 GET 查询，PostgreSQL 是状态权威，Redis DB 0 是 Celery broker。Docling 使用 Redis DB 1，Data-Juicer 使用 Redis DB 2。

## 发布顺序

1. 拉取代码、递归初始化 submodule，并构建目标镜像。
2. 启动 `db`、`redis`，等待健康。
3. 用 Backend 镜像执行一次 `bash scripts/prestart.sh`，完成 Alembic migration 和初始数据；不要运行常驻 prestart 容器。
4. 启动或更新八个服务。
5. 执行统一验证器，通过后再切换流量。

```powershell
docker compose -f compose.yml -f compose.docling.yml up -d db redis
docker compose -f compose.yml -f compose.docling.yml run --rm --no-deps backend-api bash scripts/prestart.sh
docker compose -f compose.yml -f compose.docling.yml up -d
pwsh -NoProfile -File scripts/verify-single-node-stack.ps1
```

验证临时栈时添加 `-Ephemeral`，脚本会在 `finally` 中仅对指定 Compose project 执行 `down --volumes --remove-orphans`。不得对共享或生产 project 使用该参数。

## 验收内容

统一验证器检查八服务集合、健康状态、内部服务无宿主机端口、Task Runner 的精确子进程状态、Redis DB 0、Beat schedule 和 Celery ping，并分别终止 Worker 与 Beat 观察容器重启。随后验证：

1. Backend API 停止时，Task Runner 仍能完成已经入队的任务。
2. Task Runner 停止时，Backend API 仍能创建和查询 queued 任务；Task Runner 恢复后任务成功。
3. Backend API 重启不影响 Docling、Classification 和 Data-Juicer 的健康状态。

发布环境不允许使用 `-SkipFaultInjection`；该开关仅供只读诊断或重复执行中的快速健康检查。

## 回滚与排障

应用回滚不得回滚已经执行的数据迁移。先确认目标版本与当前 schema 兼容，再更新镜像标签并重启应用服务。数据库、Redis 和业务卷保持不动。

Task Runner 不健康时读取 `/var/run/textprocessor/task-runner.json`，键必须恰好为 `worker`、`beat`；再检查 Redis DB 0、`/var/lib/celery/beat-schedule` 和容器 restart count。能力容器异常时分别检查内部 `/health`/`/ready`、对应 Redis logical DB 和资源限制，不临时发布宿主机端口绕过内部网络。

## 真实模型边界

统一验证器证明编排和故障恢复，不证明大型模型、GPU 或真实外部服务质量。仅在显式提供经过授权的 fixture、模型 release 和目标硬件时运行各能力 real integration。未运行项必须在发布记录中逐项标为 `not run`，不得用快速测试替代。

仓库中的 `tests/fixtures/compose/classification-stub.yml` 仅用于没有 GPU/批准模型的环境验证八容器编排与故障隔离。它会替换 Classification 进程，不得用于生产部署，也不得作为 Classification 推理、模型质量或 GPU 验收证据。
