# TextProcessor 单机架构精简设计

## 1. 目标与范围

本设计落实 ADR-0001，在单机、单实例前提下减少常驻容器和重复队列，同时保留 API、后台执行和重型能力之间的故障隔离。

本次覆盖 Backend API、Celery Task Runner、Frontend、PostgreSQL、Redis、Docling、Classification、Data-Juicer、Compose 与发布流程。业务算法改造、横向扩容、多机高可用和前端功能设计不在范围内。

## 2. 设计原则

- 每个实际服务使用一个容器，不为横向扩容提前拆分运行单元。
- API 与后台任务执行独立，API 故障不影响已入队或运行中的任务。
- 轻型任务目标耗时不超过 500ms，通过 `async/await` 在当前请求内返回；Route 不实现业务逻辑。
- 重型业务能力统一采用 `Celery + task_id + GET` 协议。
- 禁止 FastAPI `BackgroundTasks`。
- 优先使用中央 Celery；外部异步协议或需要独立资源隔离的重型能力可以保留额外轻量队列。
- 执行模式由 API 契约预先确定，不根据单次实际耗时动态切换。

## 3. 目标运行拓扑

生产默认栈包含八个业务容器：

```text
frontend
backend-api
task-runner
docling
classification
datajuicer
redis
db
```

Traefik 是宿主机共享基础设施，不计入项目业务容器。Adminer 仅通过 `debug` profile 按需启动；Mailcatcher、Playwright 和本地 Proxy 仅属于开发或测试环境。

`backend-api` 与 `task-runner` 复用 `textprocessor-backend` 镜像，但使用不同入口。Frontend、Docling、Classification 和 Data-Juicer 分别构建独立镜像。

## 4. 容器职责

### 4.1 Backend API

- 负责鉴权、协议校验、任务创建、可靠入队和任务查询。
- Route 只负责鉴权、校验和协议适配；业务实现位于 service、processor 或 adapter。
- 不运行 Celery Worker，不执行重型算法，不使用 `BackgroundTasks`。
- 只有 Frontend 和 Backend API 接入 Traefik 网络。

### 4.2 Task Runner

- 在一个容器内运行 Celery Worker 与 Celery Beat。
- 由 Python 监管器启动两个子进程；任一子进程异常退出时终止另一个并使容器失败，由 Docker 重启整个容器。
- 负责加载任务、状态转换、有限重试、恢复、输入输出处理、能力调用和结果发布。
- 第一阶段使用一个中央 broker 和统一任务队列；只有出现可观测的队首阻塞或资源争用时，才在同一容器内增加命名队列或 Worker 进程。

### 4.3 重型能力容器

- Docling 负责文档解析，第一阶段保留现有单容器 API + RQ Worker 和共享 Redis 独立 DB。
- Classification 负责 GPU 模型加载和推理，不保存 TextProcessor 权威任务状态。
- Backend API 只接收并校验调用方输入 URI；Task Runner 通过受控 `fsspec` 下载文本到任务独立 staging 目录，再将共享只读 `file://` URI 交给 Classification。Classification 不保存任务、正文或结果。
- Data-Juicer 负责全局去重等重型数据处理，并正式纳入生产 Compose。
- 能力服务只暴露内部网络，通过稳定 adapter 与 Task Runner 连接。
- Task Runner 以读写方式挂载 Classification staging，Classification 仅以只读方式挂载；内部 URI 必须限制在该根目录并拒绝路径逃逸。
- 能力内部队列只用于资源隔离，不复制 TextProcessor 完整业务状态机。

### 4.4 数据与基础设施

- PostgreSQL 是 TextProcessor 任务状态的权威来源。
- Redis DB 0 是中央 Celery broker；能力内部轻量队列使用独立 DB 或明确前缀隔离。
- PostgreSQL 与 Redis 保持独立容器，不与业务进程合并。

## 5. API 与任务协议

业务处理能力统一采用能力级任务接口：

```text
POST /api/v1/structured-extraction/tasks
GET  /api/v1/structured-extraction/tasks/{task_id}

POST /api/v1/global-deduplication/tasks
GET  /api/v1/global-deduplication/tasks/{task_id}

POST /api/v1/text-classification/tasks
GET  /api/v1/text-classification/tasks/{task_id}

POST /api/v1/markdown-cleaning/tasks
GET  /api/v1/markdown-cleaning/tasks/{task_id}
```

POST 成功返回 HTTP `202`，至少包含 `taskId`、`status` 和 `createdAt`。GET 按调用方身份隔离查询，并返回稳定状态、结果引用或安全错误摘要。

任务统一采用 `pending -> queued -> running -> succeeded|failed|cancelled` 状态机。Celery 消息只携带 `task_id`、`task_type` 和 `schema_version`；完整参数和状态从 PostgreSQL 读取。

登录、用户管理、普通 CRUD 和健康检查不属于业务处理任务，继续通过轻型 `async/await` 接口直接返回。

## 6. 共享 Task Kernel

Backend 内建立共享 Task Kernel，统一以下可靠性机制：

- 状态机与合法转换。
- Celery 消息 envelope 与 schema version 校验。
- 幂等创建、可靠入队和遗漏任务恢复。
- 调用方身份记录与查询隔离。
- 超时、有限重试和稳定 error code。
- 结果发布和重复消息保护。

各 feature 保留自己的请求、业务字段、结果模型和 adapter。Task Kernel 不演变为万能业务路由或包含所有业务字段的通用表。

## 7. 执行模式

### 7.1 轻型前台任务

目标耗时不超过 500ms，以非阻塞 I/O、Redis 原子操作或轻量计算为主。FastAPI 通过 `async/await` 调用 service、processor、adapter 或外部轻型服务，并在当前请求中返回。阻塞库调用或 CPU 密集计算不得直接运行在事件循环中。

### 7.2 重型后台任务

Structured Extraction、Global Deduplication、Classification 和 Markdown Cleaning 统一进入 Celery，POST 返回 `task_id`，调用方通过 GET 查询。计算密集、I/O 密集、GPU 密集或需要依赖隔离的算法由独立能力服务执行。

### 7.3 Async + Celery 组合模式

前台可通过 Redis 等完成原子校验或资源预占并立即返回，Celery 继续处理库存、订单等后续业务。组合模式必须具备幂等键、可靠投递记录、重复消费保护和失败补偿，不允许前台操作成功后进行不可恢复的临时入队。

## 8. Compose 与发布

- 删除 `extraction-worker` 与 `extraction-beat` 服务，替换为 `task-runner`。
- 删除生产默认栈中的 `prestart` 服务。数据库迁移作为发布流程的一次性步骤执行，成功后再启动或更新 API 与 Task Runner。
- `adminer` 使用 `debug` profile；开发和测试辅助服务使用对应 profile 或 override。
- 基础 Compose 定义共同服务，环境差异通过 profile 和 override 表达，避免复制多套服务定义。
- Data-Juicer 补入生产构建、部署、健康检查和发布验证。

## 9. 故障处理

- API 崩溃时，Task Runner 和能力服务继续处理已入队任务；API 恢复后可查询原任务。
- Task Runner 暂时不可用时，只要 PostgreSQL 与 Redis 可用，API 仍可创建任务并返回 `task_id`；恢复后由 Worker 消费或 Beat 恢复。
- Worker 或 Beat 任一退出时，监管器使 Task Runner 整体失败，禁止半健康运行。
- 能力服务异常时执行有限重试；耗尽后写入稳定 error code。重复提交、恢复和结果发布必须幂等。
- PostgreSQL 或 Redis 故障时，API 不得返回虚假的成功入队结果。
- 组合模式最终失败时必须执行补偿或进入明确的人工处置状态。

## 10. 分阶段迁移

### 第一阶段：容器与契约精简

- 合并 Celery Worker 与 Beat 为 Task Runner。
- 收敛生产 Compose、profile、迁移步骤和镜像发布。
- 将四项业务处理能力统一为 `task_id + GET` 协议。
- 建立共享 Task Kernel，优先复用现有可靠性实现。
- 保留 Docling 和其他重型能力现有内部轻量队列，避免同时改动容器和队列语义。

### 第二阶段：能力内部队列审查

- 基于真实延迟、队首阻塞、资源利用率和故障恢复数据审查 Docling、Data-Juicer 内部队列。
- 只有链路复杂度明显大于资源隔离与恢复收益时才移除内部队列。
- 队列技术或任务所有权发生变化时补充 ADR，并执行真实中断恢复验证。

## 11. 验证要求

- 单元测试覆盖 Task Kernel、状态机、envelope、监管器和 adapter。
- 集成测试使用真实 PostgreSQL、Redis、Celery Worker 与 Beat。
- 容器测试覆盖 API/Task Runner 独立故障、能力服务中断和健康检查。
- Docling、Classification、Data-Juicer 真实能力测试与默认快速测试分离。
- 发布验证覆盖 Compose 配置、镜像构建、数据库迁移、启动、故障恢复和临时资源清理。

## 12. 非目标

- 不建设多机高可用或横向扩容。
- 不引入 Kubernetes、服务网格或工作流平台。
- 不把重型算法合并进 Backend 或 Task Runner 镜像。
- 不因本次容器精简立即重写已验证的能力内部队列。
