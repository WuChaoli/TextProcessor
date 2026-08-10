# 137 结构化提取生产运行手册

## 部署边界

137 保持 TextProcessor API、Task Runner、Celery、PostgreSQL 和 Redis 的 systemd
部署。MinerU 是独立外部服务，本项目只消费其 HTTP API，不负责启动、升级或回滚。
Docling 以容器运行，只绑定回环地址或经批准的内网地址。真实凭据只写入生产运行时
环境文件，不写入 Git、命令行参数或验收报告。

## 当前运行清单与启停入口

以下为 2026-08-07 在 `star-SYS-4029GP-TRT`（137）的已验证快照。服务器项目根目录为
`/shineData/text_processor`。进程、容器 ID 和监听状态会变化，执行启停前必须先运行本节
“状态检查”，不得只依据快照操作。

| 组件 | 管理入口 | 当前地址或依赖 | 配置来源 | 生命周期边界 |
|---|---|---|---|---|
| TextProcessor API | `textprocessor-api.service` | `0.0.0.0:18100` | `/shineData/text_processor/.env` | 本项目管理 |
| Task Runner、Celery worker/beat | `textprocessor-task-runner.service` | Redis DB 0；在 API 之后启动 | `/shineData/text_processor/.env` | 本项目管理 |
| PostgreSQL | 容器 `tp-source-db`，镜像 `postgres:17.6-alpine` | `127.0.0.1:55434 -> 5432` | 既有容器配置 | 本项目数据依赖 |
| Redis | 容器 `tp-source-redis`，镜像 `redis:8.2.1` | `127.0.0.1:56380 -> 6379`；网络 `bridge`、`tp-processors` | 既有容器配置 | Celery 与 Docling 共用，数据库编号隔离 |
| Docling API 与 RQ worker | 容器 `tp-docling`，镜像 `textprocessor/docling-service:tp-0.1.1` | `127.0.0.1:5001`；Redis DB 1；网络 `tp-processors` | `runtime/docling.env` | 本项目管理 |
| MinerU | 容器 `mineru-api`，镜像 `mineru:latest` | `0.0.0.0:8001 -> 8000` | MinerU 独立部署 | 外部服务，默认只检查、不随本项目启停 |

两个 systemd unit 文件分别为
`/etc/systemd/system/textprocessor-api.service` 和
`/etc/systemd/system/textprocessor-task-runner.service`，均要求 `/shineData` 已挂载并在
`docker.service`、`network-online.target` 之后启动。PostgreSQL、Redis 的 restart policy
为 `unless-stopped`；Docling 为 `always`。restart policy 不替代启停后的健康检查。

### 状态检查

```bash
cd /shineData/text_processor
systemctl status textprocessor-api.service textprocessor-task-runner.service --no-pager
docker ps --filter name=tp-source-db --filter name=tp-source-redis \
  --filter name=tp-docling --filter name=mineru-api
docker inspect tp-docling --format '{{.State.Status}} {{.State.Health.Status}} retries={{.RestartCount}}'
curl --fail --silent --show-error http://127.0.0.1:8001/health >/dev/null
docling_key=$(sed -n 's/^DOCLING_SERVE_API_KEY=//p' runtime/docling.env)
printf 'header = "X-API-Key: %s"\n' "$docling_key" | \
  curl --fail --silent --show-error --config - \
    http://127.0.0.1:5001/health >/dev/null
unset docling_key
```

不得把 `docling_key`、`.env` 内容或 `/proc/<pid>/environ` 输出到终端记录、日志或工单。

### 启动顺序

```bash
cd /shineData/text_processor
docker start tp-source-db tp-source-redis
docker start tp-docling
systemctl start textprocessor-api.service
systemctl start textprocessor-task-runner.service
```

启动后必须执行状态检查，确认两个 unit 为 `active (running)`、Docling 为 `healthy`，
MinerU 与 Docling 健康请求成功。MinerU 默认不在这组命令内；仅在明确确认其独立部署
边界并获得对应授权后，才单独执行 `docker start mineru-api`。

### 停止顺序

常规维护只停止应用进程，保留数据库、Redis 和处理器：

```bash
systemctl stop textprocessor-task-runner.service
systemctl stop textprocessor-api.service
```

只有明确要求完整停止 TextProcessor 依赖时，才继续按消费者到依赖的顺序执行：

```bash
docker stop tp-docling
docker stop tp-source-redis
docker stop tp-source-db
```

MinerU 是外部服务，不随上述命令停止。不要使用 `docker compose down`、
`down --volumes`，也不要删除数据库、Redis volume、任务记录或已发布结果。

### 重启范围

- 只修改结构化提取 worker 配置或 worker 代码：
  `systemctl restart textprocessor-task-runner.service`。
- 修改 API 代码或 API 消费的配置：先重启
  `textprocessor-api.service`，再重启 `textprocessor-task-runner.service`。
- 只维护 Docling：`docker restart tp-docling`，等待其恢复 `healthy` 后再接收新任务。
- PostgreSQL 或 Redis 只在明确诊断需要时单独重启；重启前先检查运行中任务和队列，
  重启后验证数据库连接、Celery 队列和任务恢复。
- MinerU 只在单独授权后使用 `docker restart mineru-api`，不纳入 TextProcessor 常规重启。

每次操作都记录操作前后状态、准确目标、时间和失败摘要。不得因健康检查失败自动收紧
格式 allowlist。

## 基线与配置来源

修改前必须记录以下只读证据：

```bash
systemctl list-units --type=service --all | grep -E 'textprocessor|celery'
systemctl show <unit> -p FragmentPath -p DropInPaths -p EnvironmentFiles
systemctl status <unit> --no-pager
docker ps --format '{{.ID}}|{{.Names}}|{{.Image}}|{{.Status}}'
ss -lntp
```

先通过 `systemctl show` 和 `/proc/<pid>/environ` 确认实际消费结构化提取配置的
unit 和环境文件。不得凭 unit 名猜测配置路径，也不得输出 API key、数据库密码或完整
环境内容。

## Docling 上线

使用仓库固定的 `DOCLING_BASE_IMAGE` 和 `services/docling_service/Dockerfile` 构建
包装镜像。只启动 Docling 及其明确选定的 Redis 依赖，不执行会替换 API、Task Runner、
数据库、分类或 Data-Juicer 的全栈 `compose up`。API 端口绑定到 `127.0.0.1:5001`，
并设置独立的 `DOCLING_SERVE_API_KEY`。

上线最低检查包括：容器为 healthy、API 与 RQ worker 子进程存在、认证请求成功、
Docling 使用预期 Redis DB。健康检查只证明可接通，不替代真实格式验收。

## 开放生产格式

对已确认的环境文件先创建同目录时间戳备份，再设置：

```dotenv
EXTRACTION_WORKER__MINERU_BASE_URL=http://127.0.0.1:8001
EXTRACTION_WORKER__MINERU_API_KEY=
EXTRACTION_WORKER__DOCLING_BASE_URL=http://127.0.0.1:5001
EXTRACTION_WORKER__DOCLING_API_KEY=<runtime-secret>
EXTRACTION_WORKER__PRODUCTION_FORMATS=["text","markdown","json","xml","yaml","csv","tsv","pdf","image","pptx","xlsx","docx","html","epub"]
```

使用部署虚拟环境加载相同环境文件，且只回显处理器 host/port、profile 名和
`production_formats`，确认 Pydantic Settings 可解析后才重启。只重启已证明加载
`ExtractionWorkerSettings` 的 Task Runner/Celery unit；API 仅在进程环境证明确实消费
变更值时重启。重启后记录新 PID、active 状态和启动日志错误摘要。

## 上线后验收

先开放格式，再分别执行处理器直连与生产 API 验收：

- MinerU：PDF、PNG、JPG、PPTX；
- Docling：普通 DOCX、XLSX、HTML、EPUB；
- MinerU：复杂视觉 DOCX。

每项记录 task ID、最终状态、detected format、processor、路由理由、耗时、重试、
结果摘要和内容断言。PNG/JPG 必须是独立任务；DOCX 必须覆盖两个路由。
报告不复制完整正文。测试失败保持格式开放，只报告失败阶段和建议，由用户决定是否
收紧。

每个任务在内部 staging 中使用独立 `manifest.json`，该文件只服务于非终态恢复，进入
终态后清理且不发布到 `targetPath` 所在目录。成功时只原子发布目标 Markdown；同一目录
可承载不同 `targetPath`，只有目标文件本身已存在且摘要不符合本任务恢复条件时才冲突。
恢复时只接受摘要完全一致的已有文件。Docling 提交必须显式携带输入格式及正确
MIME；EPUB 使用 `application/epub+zip`，且验收 EPUB 的 `mimetype` 必须是 ZIP 中
首个、不压缩的条目。

## 观察与回滚

验收期间观察 CPU/GPU、内存、临时磁盘、Celery 积压、重试和 processor slot。
禁止通过删除任务记录、输入或已发布结果来处理失败。

只有获得用户明确授权后才收紧格式：恢复或修改已备份的 allowlist，先验证解析，再
重启实际消费者。Docling 回滚只停止本次部署的 Docling 并恢复 TextProcessor 环境
配置；MinerU 不属于回滚目标。任何停止操作前重新核对准确 unit、容器 ID 和绝对配置
路径，不执行 `down --volumes`。
