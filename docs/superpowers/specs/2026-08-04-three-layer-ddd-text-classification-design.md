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
|-- uv.lock
|-- Dockerfile
|-- src/classification_service/
|   |-- main.py
|   |-- domain/
|   |   |-- classification_result.py
|   |   |-- label_path.py
|   |   |-- model_identity.py
|   |   `-- errors.py
|   |-- application/
|   |   |-- classify_text.py
|   |   |-- dto.py
|   |   `-- ports/
|   |       |-- classifier.py
|   |       |-- inference_executor.py
|   |       `-- text_chunker.py
|   |-- infrastructure/
|   |   |-- model/
|   |   |   |-- setfit_loader.py
|   |   |   |-- tokenizer_chunker.py
|   |   |   |-- score_aggregator.py
|   |   |   |-- top_triple_classifier.py
|   |   |   `-- end_doc_classifier.py
|   |   |-- release/
|   |   |   |-- manifest.py
|   |   |   |-- validator.py
|   |   |   `-- checksum.py
|   |   |-- execution/
|   |   |   |-- admission_controller.py
|   |   |   `-- thread_executor.py
|   |   `-- config.py
|   |-- presentation/
|   |   |-- routes.py
|   |   |-- schemas.py
|   |   |-- authentication.py
|   |   |-- error_mapping.py
|   |   `-- health.py
|   `-- bootstrap.py
|-- tools/
|   |-- package_release.py
|   `-- validate_release.py
`-- tests/
    |-- domain/
    |-- application/
    |-- infrastructure/
    |-- contract/
    |-- integration/
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
  "tags": ["应急", "安全生产", "危化品", "法规标准类"],
  "confidence": {
    "topTripleClassifier": 0.72,
    "endDocClassifier": 0.81
  }
}
```

`tags` 固定为四项：前三项来自 `top-triple-classifier` 的三级主题路径，严格按照父级到子级排列；最后一项来自 `end-doc-classifier` 的文档类型。两个模型均始终返回最高分结果，不按置信度阈值过滤。

`confidence.topTripleClassifier` 和 `confidence.endDocClassifier` 分别是两个模型对各片段执行 `predict_proba` 后，预测类别的文档级算术平均概率。它们不是经过额外校准的真实概率。公开接口不返回模型地址、release 或内部运行信息。

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
  "result": {
    "tags": ["应急", "安全生产", "危化品", "法规标准类"],
    "confidence": {
      "topTripleClassifier": 0.72,
      "endDocClassifier": 0.81
    },
    "models": {
      "topTripleClassifier": {
        "name": "top-triple-classifier",
        "releaseId": "20260729T093134Z-321175f0"
      },
      "endDocClassifier": {
        "name": "end-doc-classifier",
        "releaseId": "20260729T093134Z-321175f0"
      }
    }
  }
}
```

Classification Service 分别验证两个模型的概率矩阵、标签和置信度，再按固定顺序组合四项 `tags`。两个模型任一失败时整个请求失败，不返回部分结果。

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
- Classification Service：`requestId`、releaseId、两个正式模型名称、inputChars、inferenceDurationMs、两个 confidenceBucket、outcome。

所有层都不记录正文。核心指标包括吞吐量、成功率、各错误码、P50/P95/P99 延迟、并发拒绝、两个模型的置信度分布、协议错误、release 调用量、readiness 和模型加载失败。

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
- 三级主题路径、父子顺序、文档类型、固定四项组合和非法层级。
- 两个模型均保留最高分结果和独立置信度，不执行阈值过滤。

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
- 四项 `tags` 的固定组合语义及两个独立 confidence 字段。

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

## 11. SetFit 双模型实现

Classification Service 参考 `DatasetTechTest/experiments/setfit-chinese-roberta-wwm-ext` 的真实训练与评估实现，但生产代码不得导入或依赖该实验项目。

实验任务名与生产名称的映射为：

| 实验任务 | 生产模型名称 | 语义 |
|---|---|---|
| A | `top-triple-classifier` | 18 个主题叶子标签，每个标签是三级路径 |
| B | `end-doc-classifier` | 6 个独立文档类型标签 |

Python 标识符分别使用 `TopTripleClassifier`、`EndDocClassifier`、`top_triple_classifier` 和 `end_doc_classifier`。生产文件、配置、指标和 API 字段不再使用 A/B 命名。

### 11.1 单服务双模型

首版使用一个 Classification Service 同时加载两个模型：

```text
Classification Service
  |-- shared tokenizer and chunking policy
  |-- top-triple-classifier
  `-- end-doc-classifier
```

- 一个服务实例只绑定一张 GPU。
- 每个请求只规范化和切片一次。
- 两个模型按 `top-triple-classifier -> end-doc-classifier` 串行推理，降低瞬时显存峰值。
- 两个模型均成功后才能组合结果；任一失败时不返回部分结果。
- 不拆分两个 Runtime，也不在首版重训共享编码器多头模型。

### 11.2 确定性推理流水线

```text
text
  -> normalize
  -> tokenize once
  -> sliding-window chunks
  -> uniformly select at most 16 chunks
  |-> top-triple-classifier.predict_proba
  `-> end-doc-classifier.predict_proba
  -> arithmetic mean per model
  -> argmax per model
  -> compose tags and confidence
```

正文规范化只去除 UTF-8 BOM、统一 `CRLF`/`CR` 为 `LF` 并去除首尾空白；不修改正文内部空格、标点和标题顺序，不做摘要、关键词抽取或额外分词。

切片参数与 baseline 评估保持一致：

```text
maxLength = 256
overlap = 32
maxChunksPerDocument = 16
selection = uniform
aggregation = arithmetic_mean
```

内容 token 预算必须扣除 tokenizer 的 special token 数量。窗口超过 16 个时，按确定性均匀索引覆盖首、中、尾区域。

每个模型输出 `(chunkCount, labelCount)` 概率矩阵。服务验证形状、有限值和 `0..1` 范围，对各类别片段概率取算术平均，再按 manifest 标签顺序执行确定性 argmax。`top-triple-classifier` 的叶子标签按 ` > ` 拆成恰好三级，最终组合：

```python
tags = [*top_triple_path, end_doc_label]
```

两个 confidence 是各自预测类别的文档级平均概率，不做阈值过滤。未来若增加概率校准，必须发布新模型 release 并显式更新元数据，不能静默改变 confidence 语义。

## 12. 模型 Release

### 12.1 不可变目录

```text
/models/releases/<release-id>/
|-- manifest.json
|-- checksums.sha256
|-- tokenizer/
|-- top-triple-classifier/
|   `-- model/
`-- end-doc-classifier/
    `-- model/
```

服务通过以下配置选择唯一 release：

```text
CLASSIFICATION_MODEL_RELEASE=/models/releases/<release-id>
CLASSIFICATION_MODEL_RELEASE_SHA256=<manifest digest>
```

启动时不自动选择“最新”目录，不允许请求指定模型版本，不在线下载模型，不运行中覆盖文件或热切换。新版本通过启动新实例、通过 readiness、切换流量、停止旧实例发布。

### 12.2 Manifest 内容

manifest schema version 固定为 `1`，至少包含：

- `releaseId`、`qualityStatus`、`createdAt`。
- 来源项目和训练 run id。
- Python、SetFit、Sentence Transformers、Transformers、Torch 和 scikit-learn 版本。
- 完整 chunking 与 aggregation 配置。
- 两个正式模型名称、相对路径、完整有序标签列表、标签数量和离线指标。
- tokenizer 身份和文件摘要。

`top-triple-classifier` 必须有 18 个完整三级叶子标签；`end-doc-classifier` 必须有 6 个非空文档类型标签。manifest 标签顺序必须与模型分类头完全一致。

### 12.3 开发与生产门禁

当前 baseline `20260729T093134Z-321175f0` 的已知指标为：

| 模型 | accuracy | macro-F1 |
|---|---:|---:|
| `top-triple-classifier` | 0.5441 | 0.5643 |
| `end-doc-classifier` | 0.7714 | 0.7732 |

该 baseline 存在分类头 `lbfgs max_iter=100` 未完全收敛警告，只能打包为 `qualityStatus=experimental`，用于开发联调，不得作为生产模型。

- `development` 允许加载 `experimental` 和 `production-approved`。
- `staging` 默认只允许 `production-approved`；专用模型验证环境可以显式放开。
- `production` 只允许 `production-approved`。
- Classification Service 在生产环境拒绝加载非 `production-approved` release。

模型质量审批属于训练和评测流程，不由 Classification Service 自行决定。离线打包工具不能仅凭命令参数把模型提升为生产批准状态。

### 12.4 完整性与来源安全

- release 必须位于配置的只读模型根目录下。
- 禁止路径和符号链接逃逸。
- `checksums.sha256` 覆盖所有模型和 tokenizer 文件。
- 缺失文件、未登记文件、摘要不符或 release 目录可写时启动失败。
- 配置的 manifest digest 必须匹配。
- SetFit/scikit-learn 产物可能包含 pickle 类序列化数据，只允许加载受信任内部流程产生的 release；禁止加载调用方上传或任意下载的模型。

### 12.5 离线工具

`tools/package_release.py` 接收两个受信任模型目录、tokenizer、release id、质量状态和全新目标目录，将实验产物映射到正式模型名称，生成 manifest 和 checksums，且不覆盖已有 release、不联网、不硬编码 DatasetTechTest 路径。

`tools/validate_release.py` 离线验证目录结构、摘要、标签、版本和安全路径，不加载 GPU 模型。真实加载验证由服务启动和 `real_integration` 测试完成。

## 13. 独立 Python 与 CUDA 环境

Classification Service 拥有独立 `pyproject.toml`、`uv.lock` 和容器，不与 Python 3.14 的 TextProcessor backend 混装依赖。

```text
Python >=3.12,<3.13
setfit == 1.1.3
sentence-transformers == 5.6.1
transformers == 4.49.0
torch == 2.13.0
scikit-learn == 1.9.0
numpy == 1.26.4
```

CUDA wheel 使用已经验证的 `torch 2.13.0+cu130` 构建。目标 CUDA runtime 镜像必须在实施时通过真实安装和 RTX 3090 smoke 验证，不能仅凭版本字符串声称兼容。

生产运行规则：

- 强制 CUDA，不允许自动回退 CPU。
- 通过 `CUDA_VISIBLE_DEVICES` 限制为一张物理 GPU，服务内部只使用逻辑 `cuda:0`。
- 以 RTX 3090、模型加载前至少 8 GiB 可用显存作为首版基线。
- CUDA 不可用或显存不足时启动失败。
- `HF_HUB_OFFLINE=1`、`TRANSFORMERS_OFFLINE=1`。
- tokenizer 和模型全部来自已校验的只读 release。
- 容器使用独立锁文件构建，不包含训练、数据集制作、Docling、MinerU 或 Data-Juicer 代码。

CPU 只用于 fake classifier 单元测试，不作为生产后备。

## 14. 启动、并发和故障恢复

### 14.1 启动状态机

```text
starting
  -> validating_release
  -> loading_tokenizer
  -> loading_top_triple_classifier
  -> loading_end_doc_classifier
  -> smoke_testing
  -> ready
```

readiness 只有在 release、tokenizer、两个模型和 smoke 全部成功后才通过。smoke 至少验证非空 token/chunk、最多 16 chunks、两模型形状 `(chunks,18)`/`(chunks,6)`、有限概率、行概率和允许浮点误差以及最终四项 tags 和两个 confidence。

任一步骤失败时记录阶段、releaseId和稳定错误码，进程退出，由容器平台退避重启；不回退其他 release。

### 14.2 单线程推理

SetFit/PyTorch 是阻塞调用，不能直接在 FastAPI 事件循环中执行。首版固定：

```text
Uvicorn workers = 1
ThreadPoolExecutor max_workers = 1
active inference = 1
waiting queue = 8
service budget = 15 seconds
```

tokenizer、NumPy和两个 SetFit 推理作为一个整体提交到专用 executor，不能把两个模型拆成并发线程。使用 `torch.inference_mode()`，不构建梯度。

第 10 个同时到达的请求立即返回 429。排队时间计入 15 秒预算。客户端排队期间断开时移出等待队列。若请求超时时底层推理已经开始，HTTP 请求可以结束，但 semaphore 必须等真实推理结束后释放，禁止在 GPU 工作未结束时启动下一项推理。

15 秒是请求软超时；Python 不能安全强制终止正在执行 GPU 推理的线程。底层永久卡死通过健康检查和容器重启恢复。首版不增加推理子进程 IPC。

### 14.3 CUDA OOM

CUDA OOM 时当前请求返回 `503 INFERENCE_FAILED`，实例立即取消 readiness、记录不含正文的 OOM 指标并退出，由容器平台重启。服务不在同一进程内清缓存后继续接收流量。

## 15. Classification Service 内部 HTTP

```http
POST /internal/v1/classify
Authorization: Bearer <internal-service-token>
X-Request-ID: 019...
Content-Type: application/json
```

请求禁止额外字段，只接受 schema version `1`。Header 与 body 的 `requestId` 必须一致。正文去除首尾空白后不能为空，最多 500,000 字符。接口不接收 `fileId`、文件路径、URL、模型版本或超时参数。

内部错误使用同一 envelope：

| HTTP | error code | 场景 |
|---:|---|---|
| 400 | `REQUEST_INVALID` | schema、requestId或正文不合法 |
| 401 | `SERVICE_UNAUTHENTICATED` | 内部服务令牌缺失或错误 |
| 413 | `TEXT_TOO_LARGE` | 正文超过字符上限 |
| 429 | `INFERENCE_CAPACITY_EXCEEDED` | active 与 waiting 容量已满 |
| 500 | `INFERENCE_OUTPUT_INVALID` | 分数、形状或标签违反模型契约 |
| 503 | `MODEL_NOT_READY` | 模型尚未加载完成 |
| 503 | `INFERENCE_FAILED` | SetFit、Torch或 CUDA推理失败 |
| 504 | `INFERENCE_TIMEOUT` | 排队与推理总预算耗尽 |

`GET /health/live` 只表示进程存活。`GET /health/ready` 在内部返回状态、releaseId、qualityStatus 和逻辑设备 `cuda:0`，不返回物理 GPU编号、绝对路径、令牌或文件摘要。

## 16. 生产代码迁移与验证

### 16.1 允许迁入

从实验项目重新实现以下最小算法：

- `training_chunks.py` 的 tokenizer 滑动窗口、重叠和均匀抽样。
- `training_evaluation.py` 的片段概率算术平均和文档级 argmax。
- `setfit_training.py` 的 `SetFitModel.from_pretrained` 和 `predict_proba` 薄适配逻辑。
- `labels.py` 的两个完整有序标签集合，只用于首个 release 校验。

迁入后不保留实验项目 import。

### 16.2 禁止迁入

不迁入训练编排、数据集构建和抽样、Docling/MinerU提取、目录标签推断、训练指标、checkpoint 管理、实验报表、训练正文、缓存、绝对路径或 A/B 命名。

### 16.3 详细验证

默认测试使用 fake tokenizer、classifier 和 executor，覆盖：

- 切片边界、special tokens、32 overlap、16窗口均匀抽样及确定性。
- 概率矩阵形状、NaN/Inf、范围、算术平均、argmax和并列分数标签顺序。
- 三级路径、文档类型、固定四项 tags 和两个 confidence。
- 只切片一次、两个模型固定串行、任一失败不返回部分结果。
- 超时后底层工作未结束时不释放容量。
- 协议额外字段、requestId不一致、认证、正文上限、429、503和健康状态。
- release 摘要、未登记文件、路径/符号链接逃逸、标签数、运行时版本及环境质量门禁。

真实模型一致性测试使用非敏感固定中文文本，在参考实现和新服务中比较 chunks、两组片段概率、文档级平均分、argmax标签和 confidence。该测试标记为 `real_integration`，记录 releaseId、GPU型号、锁文件摘要和结果摘要，不提交正文。

### 16.4 首版验收

- 三层调用链真实运行，Classification Service 不依赖 DatasetTechTest。
- 两个模型从不可变 development release 加载。
- RTX 3090 上只使用一张逻辑 GPU。
- 单请求返回固定四项 tags 和两个 confidence。
- 新服务输出与参考实验推理在规定浮点容差内一致。
- P95 位于 Gateway 20 秒预算内，Classification Service 不超过 15 秒软预算。
- 过载返回429，不阻塞 FastAPI事件循环。
- CUDA OOM使实例退出并由容器恢复。
- 正文、绝对路径和服务凭据不出现在日志中。
- 当前 experimental baseline 只用于开发联调，不得部署到 production。
