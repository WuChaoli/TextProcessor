# 三层 DDD 文本分类接口设计

## 1. 目标与范围

本设计为 TextProcessor 固定三层运行架构，并以“接口3：数据分类（异步单条）”作为首个落地能力。接口3属于轻型前台异步接口：FastAPI 使用 `async/await` 在明确超时内调用分类能力服务，请求完成后直接返回分类结果，不创建 Celery 任务。

系统同时保留双轨执行能力：

- 轻型、秒级且有明确超时上限的能力使用 `/execute` 前台异步接口。
- 长耗时、批量、资源密集或需要可靠重试的能力使用 `/tasks` 后台任务接口。
- 每个 API 的执行模式由契约预先确定，不根据单次运行耗时动态切换。
- 同一能力需要覆盖两种场景时，提供两个显式入口，由调用方选择。

首版只落地单条文本分类 `/execute`。批量分类、重型分类任务、复杂模型管理平台、动态灰度系统、训练和评测平台不在本设计范围内。

## 2. 三层运行架构

### 2.1 第一层：Access Layer

第一层由 Traefik 和外部 FastAPI 接入服务组成，只处理横切能力：

- TLS、外部路由和基础入口策略。
- 调用方身份认证和服务间鉴权。
- 限流、并发控制和请求体大小限制。
- API 版本路由。
- 生成或透传 `requestId`。
- 基础访问日志、指标和追踪。

第一层不读取文本文件，不理解 `fileId` 或 `tags`，不访问业务 PostgreSQL，不调用 Celery，也不执行业务编排。

### 2.2 第二层：Text Processing Gateway

第二层是业务 Gateway，理解文本处理业务并负责：

- 对调用方提供稳定业务接口。
- 校验 `fileId`、`fileStoragePath` 和 `fileOssUrl`。
- 执行受控本地路径和 HTTP URL 安全策略。
- 读取 UTF-8 `.txt`、`.md` 正文。
- 将业务请求转换为第三层能力服务协议。
- 将能力服务响应转换为稳定业务响应和错误码。
- 为重型任务创建 PostgreSQL 任务记录并通过 Redis/Celery 投递。

第二层不加载模型，也不选择某个具体运行实例。

### 2.3 第三层：Capability Services

第三层由独立能力微服务组成，例如分类、结构化提取、清洗、OCR 和摘要服务。能力服务：

- 接收标准化数据，不接收业务文件路径或调用方凭据。
- 不访问第二层数据库，不保存用户会话、任务状态或业务结果。
- 每次调用不依赖上一次业务请求。
- 可以在内存或 GPU 中常驻模型、tokenizer 和标签表；这些是运行状态，不是业务状态。
- 可以独立扩容、替换、发布和执行 readiness 检查。

首版不额外建设 FastAPI 服务分发层。第三层实例发现与负载均衡由 Docker、Kubernetes 或 Traefik 提供，避免形成多余的 HTTP 转发层。

### 2.4 调用关系

轻型分类：

```text
Client
  -> Traefik
  -> Access API
  -> Text Processing Gateway
  -> Classification Service
  -> Text Processing Gateway
  -> Access API
```

重型任务：

```text
Client
  -> Access API
  -> Text Processing Gateway
  -> PostgreSQL create task
  -> Redis/Celery
  -> Text Processing Worker
  -> Capability Service
```

Redis 首版作为 Celery Broker。Celery 消息只携带 `taskId`、任务类型和 schema version；完整参数、状态和结果以 PostgreSQL 为权威来源。

## 3. 目标文件结构

顶层目录表示可独立部署单元；每个业务服务内部再按 DDD 和端口适配器分层：

```text
TextProcessor/
|-- apps/
|   |-- access_api/
|   |-- text_processing_gateway/
|   `-- workers/
|-- services/
|   |-- classification_service/
|   |-- extraction_service/
|   `-- cleaning_service/
|-- packages/
|   |-- service_contracts/
|   |-- observability/
|   `-- testing/
|-- deploy/
|   |-- compose/
|   |-- traefik/
|   `-- containers/
`-- tests/
    |-- contract/
    `-- end_to_end/
```

### 3.1 Access API

接入层没有复杂业务领域，不创建空的 domain 层：

```text
apps/access_api/
|-- pyproject.toml
|-- src/access_api/
|   |-- main.py
|   |-- presentation/
|   |   |-- routes/
|   |   |-- schemas/
|   |   |-- middleware/
|   |   `-- dependencies.py
|   |-- application/
|   |   |-- ports/text_processing_gateway.py
|   |   `-- forwarding_service.py
|   |-- infrastructure/
|   |   |-- http/text_processing_gateway_client.py
|   |   `-- config.py
|   `-- bootstrap.py
`-- tests/
```

依赖方向为 `presentation -> application <- infrastructure`。

### 3.2 Text Processing Gateway

第二层以 bounded context 组织，而不是把所有 route、model 和 service 按技术类型集中堆放：

```text
apps/text_processing_gateway/
|-- pyproject.toml
|-- src/text_processing_gateway/
|   |-- main.py
|   |-- classification/
|   |   |-- domain/
|   |   |   |-- value_objects.py
|   |   |   |-- errors.py
|   |   |   `-- policies.py
|   |   |-- application/
|   |   |   |-- commands/execute_classification.py
|   |   |   |-- dto.py
|   |   |   `-- ports/
|   |   |       |-- document_reader.py
|   |   |       `-- classification_service.py
|   |   |-- infrastructure/
|   |   |   |-- document/fsspec_reader.py
|   |   |   `-- http/classification_client.py
|   |   `-- presentation/
|   |       |-- routes.py
|   |       |-- schemas.py
|   |       `-- error_mapping.py
|   |-- extraction/
|   |-- cleaning/
|   |-- task_execution/
|   |   |-- domain/
|   |   |-- application/
|   |   |-- infrastructure/
|   |   `-- presentation/
|   |-- shared/
|   |   |-- domain/
|   |   `-- infrastructure/
|   `-- api.py
`-- tests/
```

各 bounded context 内的依赖方向为：

```text
presentation -> application -> domain
                       |
                       v
                      ports <- infrastructure
```

约束：

- `domain` 不导入 FastAPI、SQLModel、Celery、HTTPX 或 `fsspec`。
- `application` 只依赖 domain 和抽象 port。
- `infrastructure` 实现文件读取、HTTP、数据库和消息投递适配器。
- `presentation` 只负责请求响应和错误映射。
- bounded context 不能直接导入其他上下文的 infrastructure。
- `shared` 只保存稳定的跨上下文概念，不能成为杂物目录。

### 3.3 Worker

Worker 是第二层 application use case 的独立进程适配器：

```text
apps/workers/
|-- pyproject.toml
|-- src/text_processing_workers/
|   |-- celery_app.py
|   |-- classification_tasks.py
|   |-- extraction_tasks.py
|   |-- cleaning_tasks.py
|   `-- bootstrap.py
`-- tests/
```

Celery task 只读取任务消息、从 PostgreSQL 获取完整参数、调用 application use case 并更新状态，不包含业务算法。

### 3.4 Classification Service

```text
services/classification_service/
|-- pyproject.toml
|-- src/classification_service/
|   |-- main.py
|   |-- domain/
|   |   |-- label_path.py
|   |   |-- prediction.py
|   |   `-- errors.py
|   |-- application/
|   |   |-- classify_text.py
|   |   |-- dto.py
|   |   `-- ports/classifier.py
|   |-- infrastructure/
|   |   |-- model/model_loader.py
|   |   |-- model/tokenizer_adapter.py
|   |   |-- model/classifier_adapter.py
|   |   `-- config.py
|   |-- presentation/
|   |   |-- routes.py
|   |   |-- schemas.py
|   |   `-- health.py
|   `-- bootstrap.py
`-- tests/
    |-- unit/
    |-- contract/
    `-- real_integration/
```

### 3.5 跨服务契约

`packages/service_contracts` 只共享跨进程传输协议：

```text
packages/service_contracts/
|-- pyproject.toml
|-- src/service_contracts/
|   |-- common/
|   |   |-- envelope.py
|   |   `-- errors.py
|   `-- classification/
|       |-- v1_request.py
|       `-- v1_response.py
`-- tests/
```

该包不得包含领域实体、数据库模型、业务用例、HTTP 客户端、Celery task 或模型实现。共享的是稳定协议，不是服务内部代码。

## 4. 接口3业务契约

### 4.1 公开接口

```http
POST /api/v1/text-classification/execute
Authorization: Bearer <token>
X-Request-ID: <optional>
Content-Type: application/json
```

请求：

```json
{
  "fileId": "11",
  "fileStoragePath": "/data/txt/1.txt",
  "fileOssUrl": "http://files.internal/1.txt"
}
```

规则：

- `fileId` 必填，去除首尾空白后长度为 `1..128`。
- `fileStoragePath` 和 `fileOssUrl` 至少提供一个。
- 两者同时提供时固定使用 `fileStoragePath`。
- 本地文件失败时不回退到 `fileOssUrl`。
- 只允许 UTF-8 `.txt`、`.md`。
- 本地文件必须位于配置的输入根目录 allowlist。
- HTTP URL 必须通过 scheme、host、CIDR、大小和超时 allowlist，且不能携带凭据。

成功分类：

```json
{
  "fileId": "11",
  "tags": ["法律法规", "九小"]
}
```

`tags` 是唯一最佳层级路径，严格按照父级到子级排列，不表示多个并列标签。

低置信度仍视为成功：

```json
{
  "fileId": "11",
  "tags": []
}
```

置信度阈值由 Classification Service 根据具体模型版本配置。公开接口不返回置信度、模型地址或模型版本。

### 4.2 Gateway 调用 Classification Service

```http
POST /internal/v1/classify
X-Request-ID: 019...
Authorization: Bearer <internal-service-token>
```

请求：

```json
{
  "schemaVersion": "1",
  "requestId": "019...",
  "text": "待分类的文本正文"
}
```

第二层不发送 `fileId`、文件路径、OSS URL、OSS 凭据或调用方认证信息。第三层响应：

```json
{
  "schemaVersion": "1",
  "requestId": "019...",
  "prediction": {
    "path": ["法律法规", "九小"],
    "confidence": 0.93
  },
  "model": {
    "name": "hierarchical-text-classifier",
    "version": "2026-08-01"
  }
}
```

Classification Service 负责应用阈值；低于阈值时 `path` 为 `[]`。`path` 中不能出现空字符串、非字符串元素或重复相邻层级。

## 5. 资源与超时边界

首版固定默认值：

| 项目 | 默认值 |
|---|---:|
| Access Layer 接收完整请求体 | 2 秒 |
| 本地文件读取 | 2 秒 |
| HTTP 文件下载 | 5 秒 |
| 连接 Classification Service | 2 秒 |
| Classification Service 推理 | 15 秒 |
| Text Processing Gateway 总执行 | 20 秒 |
| Access Layer 总请求 | 22 秒 |
| 原始文件最大值 | 1 MiB |
| 正文最大字符数 | 500,000 |

总超时是硬预算，不是各阶段上限之和。超时值由服务端配置管理，调用方不能扩大。正文为空时拒绝请求。实际模型若不能稳定满足 15 秒推理预算，应通过重型 `/tasks` 接口接入。

## 6. 错误协议与重试

统一公开错误结构：

```json
{
  "detail": {
    "code": "CLASSIFICATION_TIMEOUT",
    "message": "文本分类服务处理超时",
    "requestId": "019..."
  }
}
```

| HTTP | error code | 含义 |
|---:|---|---|
| 400 | `CLASSIFICATION_REQUEST_INVALID` | 字段格式错误 |
| 400 | `INPUT_PATH_NOT_ALLOWED` | 本地路径不存在、越界或不允许 |
| 400 | `INPUT_URL_NOT_ALLOWED` | URL 或网络目标不符合策略 |
| 400 | `INPUT_FORMAT_NOT_SUPPORTED` | 文件格式不支持 |
| 400 | `INPUT_ENCODING_INVALID` | UTF-8 解码失败 |
| 400 | `INPUT_EMPTY` | 正文为空 |
| 413 | `INPUT_TOO_LARGE` | 文件或正文超过上限 |
| 429 | `CLASSIFICATION_RATE_LIMITED` | 达到入口或能力服务限制 |
| 502 | `CLASSIFICATION_PROTOCOL_ERROR` | 下游响应违反契约 |
| 503 | `CLASSIFICATION_UNAVAILABLE` | 分类服务未就绪或不可用 |
| 504 | `CLASSIFICATION_TIMEOUT` | 超过前台异步总预算 |

公开错误和日志不得泄露宿主机绝对路径、正文、内部服务 URL、堆栈、模型文件路径或服务令牌。

`/execute` 首版不做通用自动重试。只有连接建立阶段失败、下游尚未确认接收、总预算充足且存在另一个健康实例时，允许一次受控重试。已收到 HTTP 状态、开始接收响应后断开、推理超时、429 或协议错误均不重试。

## 7. 身份、日志与健康检查

身份链路：

```text
Client -> Access Layer: caller Bearer token
Access Layer -> Gateway: internal token + X-Caller-ID + X-Request-ID
Gateway -> Capability Service: independent internal token + X-Request-ID
```

第二、三层不允许客户端直接访问，并通过网络策略限制入口来源。

日志字段：

- Access Layer：`requestId`、`callerId`、route、statusCode、durationMs。
- Gateway：`requestId`、`callerId`、`fileId`、selectedInputType、capability、statusCode、errorCode、durationMs。
- Classification Service：`requestId`、modelName、modelVersion、inputChars、inferenceDurationMs、confidenceBucket、outcome。

所有层都不记录正文。核心指标包括吞吐量、成功率、各错误码、P50/P95/P99 延迟、并发拒绝、低置信度空标签、协议错误、模型版本调用量、readiness 和模型加载失败。

各服务提供：

```http
GET /health/live
GET /health/ready
```

`live` 只表示进程存活。Classification Service 的 `ready` 必须确认模型、tokenizer 和标签表加载成功。Gateway 不因单个能力服务短暂故障而整体下线。

## 8. 部署与数据所有权

首版独立容器：

```text
traefik
access-api
text-processing-gateway
text-processing-worker
classification-service
postgres
redis
```

- Access API 无数据库、Redis和文件目录挂载。
- Gateway 访问受控输入目录、PostgreSQL、Redis和能力服务。
- Worker 访问 PostgreSQL、Redis、受控输入输出目录和能力服务。
- Classification Service 只访问模型文件，不访问业务文件、PostgreSQL或 Redis。
- PostgreSQL 保存第二层任务、参数、状态和结果。
- Redis 只作为 Celery Broker；轻型 `/execute` 不经过 Redis。

配置由各服务所有，调用方不能指定内部 URL、模型路径、模型版本或超时上限。

## 9. 验证策略

### 9.1 Domain 单元测试

- 本地路径优先，失败不回退 OSS。
- 扩展名、UTF-8、空正文、字节数和字符数边界。
- 唯一最佳路径、父子顺序、阈值边界和非法层级。

### 9.2 Application 用例测试

- `ExecuteClassificationHandler` 通过 `DocumentReader` port 读取正文。
- 只把正文和追踪字段发送到 `ClassificationService` port。
- 业务响应保留原始 `fileId`。
- 下游异常映射为稳定应用错误。
- domain/application 不依赖 Web、数据库、消息或 HTTP 框架。

### 9.3 契约测试

- Access API 与 Gateway 的公开协议。
- Gateway 与 Classification Service 的内部 v1 协议。
- 错误 envelope、`schemaVersion` 和 `requestId`。
- `tags` 类型及单路径语义。

跨服务可以共享 JSON fixture，不共享领域实体。

### 9.4 集成与端到端测试

- FastAPI 和真实 HTTPX adapter。
- 路径逃逸、受控 HTTP host/CIDR、超时和连接失败。
- 下游 429、503、非法 JSON、字段缺失和请求取消。
- 并发限制不阻塞事件循环。
- Compose 验证 Traefik 到 fake Classification Service 的完整链路。
- 外部鉴权失败不进入第二层，`requestId` 贯穿三层，日志不出现正文，第三层不能被外网访问。

PostgreSQL 与 Redis/Celery 集成只用于重型任务链路。

### 9.5 真实模型验证

真实模型测试使用 `real_integration` 标记，与默认快速测试集分离。只有实际运行后才能声称模型加载、readiness、推理预算、标签层级和资源占用通过。

## 10. 渐进迁移

当前 `backend` 已承载结构化提取和全局去重等真实功能，禁止一次性搬迁：

1. 建立 `packages/service_contracts`，固定分类 v1 内部协议。
2. 新建 `services/classification_service`，使用 fake classifier 完成契约闭环。
3. 在现有 `backend` 中按 DDD 新建 classification bounded context，暂时把现有 backend 视为第二层 Gateway。
4. 实现接口3并直接调用 Classification Service。
5. 新建 `apps/access_api`，逐步迁入统一鉴权和流量入口。
6. 端到端验证后，外部分类流量切换到 Access API。
7. 再迁移或重命名现有 backend 为 `apps/text_processing_gateway`。
8. 结构化提取、清洗和去重分别设计、测试和迁移，禁止随接口3顺带移动。
9. 所有消费者切换并验证后，才删除旧入口兼容代码。

该顺序允许接口3先形成可运行闭环，同时逐步落实三层目标架构，避免大爆炸式重构。
