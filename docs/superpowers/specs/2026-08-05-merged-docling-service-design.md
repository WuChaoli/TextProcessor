# 合并 Docling 服务容器设计

## 1. 目标与决策

本文定义 TextProcessor 中 Docling Serve 的新部署单元：Docling HTTP API 与 RQ Worker 继续保持独立进程和 RQ 任务队列语义，但合并到同一个容器；Docling 不再运行专用 Redis，而是复用 TextProcessor Compose 栈中的 Redis 实例。

已确认决策：

- Docling 继续使用 RQ，不迁移到 Celery；
- Docling API 与 RQ Worker 由同一个容器内的 Python PID 1 监管器管理；
- TextProcessor Celery 使用 `redis://redis:6379/0`；
- Docling RQ 使用 `redis://redis:6379/1`；
- 共享 Redis 不增加密码、ACL 或新的认证机制；
- Redis 只允许 Compose 内部网络访问，不发布宿主机端口；
- 保留现有 `docling-api` 服务名和 `http://docling-api:5001` 内部地址；
- Docling 模型缓存继续使用持久化 volume；
- 生产环境仍使用 Docling API key 保护内部 HTTP 接口。

本文取代《结构化提取 Worker 与解析服务设计》第 16 节中“API、RQ Worker 和 Docling 专用 Redis 分为三个容器”的部署决策。结构化提取 API、processor 路由、Docling HTTP adapter、格式 allowlist 和输出契约不变。

## 2. 范围

本次设计范围包括：

- 合并后的 Docling 镜像边界；
- 容器内双进程生命周期监管；
- 共享 Redis 的 logical DB 隔离；
- Compose 服务、volume 和网络关系；
- 健康检查、停止、重启和任务恢复语义；
- 发布配置、验证脚本、运行手册和 CI/CD 需要满足的契约；
- 对现有 TextProcessor Celery 链路的回归要求。

本次不改变 Docling 的业务实现或 TextProcessor 的外部 API。

## 3. 目标拓扑

```text
TextProcessor Backend / Extraction Worker
        │ HTTP + X-API-Key
        ▼
docling-api container
└── Python PID 1 supervisor
    ├── docling-serve run --host 0.0.0.0 --port 5001
    └── docling-serve rq-worker
                 │
                 ▼
       redis://redis:6379/1

TextProcessor Celery Worker / Beat
                 │
                 ▼
       redis://redis:6379/0

shared redis container
├── DB 0: TextProcessor Celery broker
└── DB 1: Docling RQ queue, job state and results
```

`docling-api` 是 Compose 中唯一的 Docling 服务。容器内仍有两个职责清晰的进程：API 负责 HTTP 协议和任务入队，RQ Worker 负责消费并执行解析任务。合并只改变部署边界，不改变队列协议。

## 4. 镜像设计

新增 TextProcessor 自维护的 Docling 包装镜像。镜像必须：

- 以明确版本和 digest 固定的官方 Docling Serve 镜像为基础；
- 只增加 Python PID 1 监管器、联合健康检查和必要启动资产；
- 不复制或修改 Docling 业务源码；
- 不安装 Supervisor、systemd 或其他进程管理系统；
- 不打入模型文件、任务数据、Redis 数据或业务输入；
- 继续通过只读或受控缓存 volume 管理模型资产；
- 使用明确 release tag，生产发布禁止依赖 `latest`；
- 能在离线或受控网络策略下复用已经准备好的模型缓存。

同一镜像只启动一个容器，不再通过不同 command 创建独立 API 和 Worker 容器。

## 5. Python PID 1 监管器

### 5.1 启动

监管器作为容器 `ENTRYPOINT` 和 PID 1，依次创建：

1. Docling API 子进程；
2. Docling RQ Worker 子进程。

两个子进程继承容器标准输出和标准错误，不由监管器缓存、改写或写入独立日志文件。监管器不得记录 API key、文档正文、Redis 内容或其他敏感数据。

监管器启动后持续监视两个子进程。它不承担应用级重试、任务重投或 Redis 重连逻辑。

### 5.2 子进程异常

任一子进程在容器正常停止流程之外退出时：

1. 监管器记录退出进程名称和退出码；
2. 向仍存活的另一个子进程发送终止信号；
3. 等待固定的优雅退出期限；
4. 超时后强制终止仍存活的子进程；
5. 监管器以非零状态退出。

监管器不得在容器内部无限重启单个子进程。容器级恢复由 Compose 的 restart policy 负责，避免出现 API 与 Worker 版本、环境或生命周期不一致的半健康容器。

### 5.3 容器停止

监管器收到 `SIGTERM` 或 `SIGINT` 后：

1. 停止接受新的生命周期动作；
2. 向 API 和 RQ Worker 转发终止信号；
3. 并行等待两个子进程退出；
4. 超过优雅退出期限后强制终止剩余进程；
5. 返回能够反映停止结果的退出码。

Compose 的 `stop_grace_period` 必须大于监管器内部的优雅退出期限，使监管器有机会完成子进程清理。

## 6. Redis 共享与隔离

### 6.1 固定连接

```text
CELERY_BROKER_URL=redis://redis:6379/0
DOCLING_SERVE_ENG_RQ_REDIS_URL=redis://redis:6379/1
```

Redis service name 固定为 `redis`。调用方不能通过 API 请求修改 Redis URL、DB number 或队列配置。

### 6.2 隔离边界

DB 0 与 DB 1 只提供逻辑键空间隔离：

- Celery 不得读取、删除或清空 DB 1；
- Docling 不得读取、删除或清空 DB 0；
- 运维脚本不得对共享实例执行无 DB 边界的 `FLUSHALL`；
- 针对 Docling 的检查和清理命令必须显式选择 DB 1；
- 针对 Celery 的检查和清理命令必须显式选择 DB 0。

logical DB 不提供 CPU、内存、连接数、持久化、可用性或故障隔离。Redis 重启、数据损坏、内存耗尽或 volume 丢失可能同时影响两套队列，这属于已接受的首版共同故障域。

### 6.3 安全边界

本次不增加 Redis 密码或 ACL。安全边界依赖：

- Redis 不配置宿主机端口映射；
- Redis 仅加入所需的 Compose 内部网络；
- Traefik 不为 Redis 配置 router 或 service label；
- 生产主机防火墙不开放 Redis 端口；
- 日志和诊断输出不打印队列 payload 或原始文档内容。

Redis 认证、ACL、Sentinel、Cluster 和独立实例迁移需要另立安全或高可用设计。

## 7. Compose 边界

合并后 `compose.docling.yml` 只定义 `docling-api` 以及 `docling-model-cache`：

- `docling-api` 使用自建包装镜像；
- 构建参数引用固定 digest 的 Docling 基础镜像；
- `docling-api` 依赖主 Compose 中 `redis` 的 healthy 状态；
- `DOCLING_SERVE_ENG_KIND` 固定为 `rq`；
- RQ Redis URL 固定指向共享 Redis DB 1；
- API 与 Worker 共享相同环境、版本、模型缓存和资源限制；
- 生产环境不发布端口 `5001`；
- 本地 override 可以显式发布 `5001:5001`；
- 删除 `docling-worker`、`docling-redis` 和 `docling-redis-data`；
- `docling-model-cache` 继续保留，正常停止不得删除该 volume。

正式启动必须同时加载：

```text
compose.yml
compose.docling.yml
```

不能只启动 `compose.docling.yml`，因为它依赖主 Compose 提供的 Redis。生产部署命令和 CI/CD 工作流必须使用同一个 Compose project name 合并两份文件。

## 8. 健康与就绪

容器健康检查必须同时验证：

1. API 子进程仍存活；
2. RQ Worker 子进程仍存活；
3. 使用配置的 API key 请求 `http://localhost:5001/health` 成功；
4. 容器能够访问共享 Redis DB 1。

监管器应提供可由健康检查读取的可靠子进程状态，不依赖模糊的进程名匹配。健康检查不能仅验证 HTTP API，因为 API 存活而 RQ Worker 消失会造成任务永久排队。

首次模型加载期间使用 `start_period`，避免将正常预热判为故障。Redis 短暂不可用且两个 Docling 进程仍存活时，容器标记为 `unhealthy`，由 Docling/RQ 自身执行有限重连；健康检查本身不杀死容器。Redis 故障导致任一子进程退出时，监管器按整体故障结束容器。

`healthy` 只证明当前进程、API 和 Redis 连接可用，不证明真实文档解析、队列恢复或模型质量通过。

## 9. 任务生命周期与恢复

正常链路：

```text
TextProcessor 上传 staging 文件
→ Docling API 在 Redis DB 1 创建 RQ job
→ RQ Worker 消费并解析
→ Docling 将状态和结果写回 DB 1
→ TextProcessor 使用 task ID 轮询并获取结果
```

恢复要求：

- 已成功写入 DB 1 且仍处于 queued 的任务，在合并容器重启后应继续被 Worker 消费；
- API 重启不得清空 DB 1；
- Worker 重启不得改变 TextProcessor 已保存的 Docling task ID；
- 正在执行的 started 任务能否自动恢复取决于锁定版本 Docling/RQ 的真实行为，必须通过故障测试记录，不在设计阶段承诺；
- 如果 started 任务不能自动恢复，运行手册必须记录明确失败表现和人工处置方式；
- TextProcessor 已有的 processor 超时、任务失败和恢复逻辑继续作为业务侧最终保护；
- 正常停止共享栈时禁止使用 `down --volumes`，因为 Redis volume 同时保存 Celery 与 Docling 的队列状态。

## 10. 发布与配置

发布制品至少包括：

- Backend 镜像；
- Frontend 镜像；
- Classification Service 镜像；
- 合并 Docling 包装镜像；
- 锁定版本的 Compose 文件；
- 环境变量模板和脱敏发布清单。

合并 Docling 镜像必须与本次应用发布使用相同 release tag。发布清单还应记录其 Docling 基础镜像完整 digest。

生产部署环境至少提供：

```text
DOCLING_BASE_IMAGE
DOCKER_IMAGE_DOCLING
DOCLING_SERVE_API_KEY
DOCLING_MAX_FILE_SIZE_BYTES
DOCLING_MAX_NUM_PAGES
DOCLING_MAX_DOCUMENT_TIMEOUT_SECONDS
EXTRACTION_DOCLING_BASE_URL=http://docling-api:5001
EXTRACTION_DOCLING_API_KEY
TAG
```

删除专用 Redis 后，不再需要 `DOCLING_REDIS_PASSWORD`。API key 与 TextProcessor adapter 使用的 key 必须一致，但不得写入镜像、Git 或普通日志。

## 11. 验证与验收

### 11.1 静态契约

- 最终 Compose 只包含一个 Docling service；
- 不存在 `docling-worker`、`docling-redis` 或 `docling-redis-data`；
- Docling 明确使用 RQ 和 Redis DB 1；
- Celery 明确保持 Redis DB 0；
- Redis 没有宿主机端口映射；
- Docling 生产配置没有宿主机端口映射；
- Docling 基础镜像固定版本和 digest；
- Compose 合并配置能够通过 `docker compose config --quiet`。

### 11.2 监管器测试

- API 和 RQ Worker 都被启动；
- 标准输出和错误输出进入容器日志；
- `SIGTERM` 和 `SIGINT` 被转发；
- API 意外退出会终止 Worker 并使容器非零退出；
- Worker 意外退出会终止 API 并使容器非零退出；
- 优雅退出超时后会强制终止残留进程；
- 正常退出不留下孤儿或僵尸进程；
- 监管器不会在容器内部无限重启子进程。

### 11.3 运行验证

- `docling-api` 容器达到 healthy；
- 未带 API key 的受保护接口返回拒绝；
- 带有效 API key 可以读取所需 OpenAPI 路径；
- RQ Worker 存活且连接 DB 1；
- Redis DB 0 中不出现 Docling RQ job；
- Redis DB 1 中不出现 TextProcessor Celery task；
- TextProcessor 能提交、轮询并获取一次真实 Docling 异步转换结果；
- TextProcessor Celery 消息仍能正常入队和消费。

### 11.4 故障与恢复验证

在隔离验收环境分别执行：

- queued 状态重启合并 Docling 容器；
- started 状态重启合并 Docling 容器；
- 单独终止 API 子进程；
- 单独终止 RQ Worker 子进程；
- 短暂停止并恢复共享 Redis；
- 重启共享 Redis；
- 重启 TextProcessor Celery Worker，确认不影响 DB 1 中 Docling job 身份。

每项测试必须记录任务最终状态、是否重复执行、是否保留同一 task ID、容器退出与重启行为，以及 Celery 是否受到影响。未实际执行的场景不得声明通过。

## 12. 运行与故障边界

- API healthy 但没有完成异步转换，不算 Docling 栈验收通过；
- logical DB 隔离不能作为容量隔离或高可用证据；
- 共享 Redis 的资源上限必须同时覆盖 Celery 和 Docling RQ；
- Redis eviction policy 不得允许静默淘汰仍有效的队列或结果键；
- 共享 Redis 数据清理必须区分 DB 0 与 DB 1；
- Docling 容器扩容会同时增加 API 和 RQ Worker，不能独立扩缩其中一方；
- 合并容器适用于当前单机、小规模部署；出现独立扩缩容、不同资源限制或故障域需求时，应恢复 API/Worker 分离部署；
- 一个容器内运行两个进程是本项目明确接受的部署折中，不作为其他服务默认模式。

## 13. 非目标

- 不把 Docling RQ 改为 Celery；
- 不让 TextProcessor Celery Worker 直接执行 Docling 解析算法；
- 不修改结构化提取 API、状态机、幂等键或输出协议；
- 不修改 processor 路由、格式 allowlist、模型或解析 profile；
- 不增加 Redis 密码、ACL、Sentinel、Cluster 或独立实例；
- 不将 Redis、模型或任务数据打入 Docling 镜像；
- 不在生产环境公开 Docling API；
- 不承诺 started RQ job 在未经真实验证时能够自动恢复；
- 不在本设计中建设 Kubernetes、水平自动扩缩容或多主机调度。

## 14. 完成定义

只有同时满足以下条件，合并 Docling 服务才可视为交付完成：

1. 包装镜像、PID 1 监管器、联合健康检查和 Compose 配置已经实现；
2. 专用 Docling Redis 与独立 Worker 容器已经移除；
3. 发布工作流会构建并启动合并后的 Docling 镜像；
4. 静态契约、监管器、Compose 和相关回归测试通过；
5. 真实 Redis、RQ、Docling API 和 TextProcessor adapter 链路通过；
6. queued 与 started 重启行为已经真实验证并形成脱敏记录；
7. 运行手册准确描述共享 Redis 的清理、恢复和共同故障域；
8. 未运行的真实测试明确标记为未验证，不以单元测试或 healthcheck 替代。
