# 全局文档去重异步任务接口设计

## 1. 目标与范围

本接口接收一份文档清单 JSON 文件地址，对清单中的文本文件执行批次级全局去重。接口不等待处理完成，也不回调业务系统。调用方获得 `taskId` 后，通过 GET 接口轮询任务状态、进度和最终结果 JSON 文件地址。

本设计只确定 API 契约、输入输出文件 schema、幂等、状态、进度、访问控制和错误边界。去重算法、特征生成、聚类方式、`groupId` 生成方式、重复组代表文档选择规则、`keep` 决策依据以及 worker 内部任务拆解不属于本文范围，后续通过 worker 专项设计确定。

接口不返回结果 JSON 正文。PostgreSQL 不保存文档正文或结果 JSON 正文。

## 2. 接口与任务边界

全局文档去重使用独立能力入口，同时复用项目已有的身份认证、PostgreSQL 权威任务状态、Redis broker、Celery 投递和稳定错误响应约定：

- API 路径使用 `global-deduplication`。
- Python feature 和 Celery task type 使用 `global_deduplication`。
- FastAPI API 负责身份认证、基础请求校验、幂等判断、任务创建和入队。
- 输入清单下载、清单内容校验、文档读取、去重处理和结果发布均在 worker 阶段执行。
- PostgreSQL 是任务参数、状态和进度的权威来源。
- Celery 消息只携带 `taskId`、task type 和 schema version。
- 首版不提供回调接口和主动取消接口。

首版只提供：

```http
POST /api/v1/global-deduplication/tasks
GET  /api/v1/global-deduplication/tasks/{taskId}
```

## 3. 创建任务

### 3.1 请求

`POST /api/v1/global-deduplication/tasks`

```json
{
  "sessionId": "session-001",
  "inputJsonPath": "/data/dedup/input/batch-001.json",
  "targetPath": "/data/dedup/output/batch-001.json"
}
```

字段规则：

- `sessionId`：必填、非空；一次全局去重批次使用一个唯一值。
- `inputJsonPath`：必填、非空；指向待处理文档清单的 JSON 文件。
- `targetPath`：必填、非空；表示最终结果 JSON 的完整文件路径，不是目录。
- `targetPath` 必须以 `.json` 结尾。
- 外部协议字段使用 camelCase，Python 和数据库内部使用 snake_case，并在 schema 边界显式映射。

`inputJsonPath` 使用单字段路径路由：

- 受控绝对路径或 `file://`：本地文件输入。
- `http://` 或 `https://`：受控 HTTP 只读输入。
- `s3://`：受控 OSS 或 MinIO 输入。

`targetPath` 同样根据路径协议选择输出 adapter。首版允许受控本地路径、`file://` 和 `s3://` 输出；`http://` 与 `https://` 不能作为输出。

### 3.2 接受响应

首次创建并成功入队时返回 `202 Accepted`：

```json
{
  "taskId": "0198f000-0000-7000-8000-000000000001",
  "sessionId": "session-001",
  "status": "queued"
}
```

命中参数一致的幂等任务时仍返回 `202 Accepted`，其中 `taskId` 和 `status` 是原任务的值。

## 4. 幂等规则

幂等键为：

```text
(callerId, sessionId)
```

- 同一调用方使用相同 `sessionId` 重复提交且参数一致时，不创建新任务，返回原 `taskId` 和当前状态。
- 不同调用方使用相同 `sessionId` 时互不影响。
- 不同 `sessionId` 视为不同任务，即使输入和输出路径相同也不复用任务。
- 原任务已失败或取消时仍返回原任务；重新处理必须使用新的 `sessionId`。
- 相同幂等键携带不同 `inputJsonPath` 或 `targetPath` 时，返回 `409 IDEMPOTENCY_CONFLICT`。
- 服务端对规范化后的 `inputJsonPath` 和 `targetPath` 生成 `requestFingerprint`。
- 数据库必须对 `(caller_id, session_id)` 建立唯一约束，以收敛并发重复提交。

## 5. 输入清单 JSON 契约

### 5.1 文件结构

`inputJsonPath` 指向 UTF-8 编码的 JSON 文件。顶层必须是非空数组：

```json
[
  {
    "fileId": "1",
    "fileStoragePath": "/data/txt/1.md",
    "unknownField": "ignored"
  },
  {
    "fileId": "2",
    "fileStoragePath": "s3://documents/input/2.txt"
  }
]
```

每条记录的规则：

- 必须是 JSON object。
- `fileId` 必填、非空，并且在同一批次内唯一。
- `fileStoragePath` 必填、非空。
- 允许未知字段；未知字段不参与处理，也不透传到结果。
- 空数组以 `EMPTY_DOCUMENT_LIST` 失败。
- 重复 `fileId` 以 `DUPLICATE_FILE_ID` 失败。
- 任意记录不合法时整个任务失败，不跳过错误记录。

### 5.2 文档地址与格式

`fileStoragePath` 是统一文档地址字段，使用与 `inputJsonPath` 相同的路径协议路由和服务端访问策略。

首版仅支持：

- Markdown：`.md`
- 纯文本：`.txt`
- JSON：`.json`

其他扩展名以 `UNSUPPORTED_DOCUMENT_FORMAT` 失败。只要批次中存在一份不支持、无法访问、超限或无法解码的文档，整个任务失败，不生成部分结果。

### 5.3 服务端资源限制

以下限制由服务端配置控制，调用方不能通过请求提高限制：

```text
GLOBAL_DEDUP_MAX_DOCUMENTS
GLOBAL_DEDUP_MAX_MANIFEST_BYTES
GLOBAL_DEDUP_MAX_DOCUMENT_BYTES
GLOBAL_DEDUP_MAX_TOTAL_BYTES
```

它们分别限制单批文档数、输入清单大小、单份文档大小和批次累计输入大小。具体默认值属于后续部署配置设计。

## 6. 查询任务

`GET /api/v1/global-deduplication/tasks/{taskId}`

GET 必须同时匹配 `taskId + callerId`。任务不存在和无权访问统一返回 `404`。

### 6.1 执行中响应

```json
{
  "taskId": "0198f000-0000-7000-8000-000000000001",
  "sessionId": "session-001",
  "status": "running",
  "createdAt": "2026-07-31T10:00:00+08:00",
  "startedAt": "2026-07-31T10:00:02+08:00",
  "finishedAt": null,
  "progress": {
    "phase": "loading_documents",
    "total": 100,
    "processed": 35,
    "percent": 35
  },
  "result": null,
  "error": null
}
```

### 6.2 成功响应

```json
{
  "taskId": "0198f000-0000-7000-8000-000000000001",
  "sessionId": "session-001",
  "status": "succeeded",
  "createdAt": "2026-07-31T10:00:00+08:00",
  "startedAt": "2026-07-31T10:00:02+08:00",
  "finishedAt": "2026-07-31T10:05:00+08:00",
  "progress": {
    "phase": "completed",
    "total": 100,
    "processed": 100,
    "percent": 100
  },
  "result": {
    "targetPath": "/data/dedup/output/batch-001.json"
  },
  "error": null
}
```

GET 只返回最终结果文件引用，不返回结果 JSON 正文。

### 6.3 失败响应

```json
{
  "taskId": "0198f000-0000-7000-8000-000000000001",
  "sessionId": "session-001",
  "status": "failed",
  "createdAt": "2026-07-31T10:00:00+08:00",
  "startedAt": "2026-07-31T10:00:02+08:00",
  "finishedAt": "2026-07-31T10:00:10+08:00",
  "progress": {
    "phase": "loading_documents",
    "total": 100,
    "processed": 12,
    "percent": 12
  },
  "result": null,
  "error": {
    "code": "DOCUMENT_READ_FAILED",
    "message": "文档读取失败，fileId: 2"
  }
}
```

`result` 和 `error` 必须互斥。

## 7. 状态与进度

任务状态沿用显式状态机：

```text
pending -> queued -> running -> succeeded
                     |-------> failed
                     |-------> cancelled
```

首版没有对外取消 API；`cancelled` 仅作为领域终态保留。

进度结构固定包含：

```text
phase
total
processed
percent
```

阶段值为：

```text
validating_input
loading_documents
deduplicating
publishing_result
completed
```

约束：

- `total` 是输入清单中的文档总数；尚未成功解析清单时可为 `null`。
- `processed` 是已完成当前可计数处理的文档数；尚不可计数时为 `0`。
- `percent` 是 `0` 到 `100` 的整数总体估算值，不承诺与实际耗时线性对应。
- 成功终态必须为 `phase=completed`、`processed=total`、`percent=100`。
- 失败终态保留失败发生时最后一次持久化的进度。

## 8. 输出结果 JSON 契约

`targetPath` 指向 UTF-8 编码的 JSON 数组。每个输入文档必须对应一条输出记录，且输出记录只包含以下四个字段：

```json
[
  {
    "fileId": "1",
    "fileStoragePath": "/data/txt/1.md",
    "groupId": "111",
    "keep": true
  },
  {
    "fileId": "2",
    "fileStoragePath": "/data/txt/2.md",
    "groupId": "111",
    "keep": false
  },
  {
    "fileId": "3",
    "fileStoragePath": "/data/txt/3.md",
    "groupId": null,
    "keep": true
  }
]
```

字段语义：

- `fileId`：原输入记录的文档 ID。
- `fileStoragePath`：原输入记录的文档地址。
- `groupId`：重复组标识；同一重复组内非空且相同，独立文档固定为 `null`。
- `keep`：是否建议业务方保留该文档。

结果不执行文件删除、移动或覆盖，只提供分组与保留建议。

结果必须满足：

- 独立文档为 `groupId=null` 且 `keep=true`。
- 每个重复组严格且只能有一条记录 `keep=true`，同组其他记录全部为 `false`。
- 输入未知字段不出现在输出中。
- 输出数组顺序不属于接口契约，调用方必须使用 `fileId` 关联输入与输出。
- `groupId` 的具体格式和跨任务稳定性不属于本接口契约。

## 9. 输出冲突与发布边界

- `targetPath` 已存在时禁止覆盖，任务以 `OUTPUT_CONFLICT` 失败。
- Worker 必须先写入本任务专属临时位置，校验完整结果后再原子发布。
- 处理前检查只能用于快速失败，发布阶段仍须执行禁止覆盖的冲突保护。
- 任务恢复时需要区分外部已有文件与本任务已成功发布但尚未更新数据库的结果。
- 恢复、摘要和原子发布的具体流程在 worker 专项设计中确定，但不得削弱“不覆盖”和“不暴露半成品”的接口保证。

## 10. 安全与访问控制

- POST 和 GET 均使用服务端身份认证。
- GET 使用 `taskId + callerId` 隔离任务并防止枚举。
- 所有输入和输出地址必须经过服务端配置的协议、根路径、host/CIDR、bucket、大小和超时 allowlist。
- 禁止 URI 内嵌凭据、路径逃逸、符号链接逃逸、重定向逃出 allowlist，以及访问未授权公网、loopback、link-local 或云元数据地址。
- `http(s)` 仅允许作为输入。
- 请求不能携带或扩大服务端文件、网络、OSS 或其他存储凭据权限。
- 日志记录 `requestId`、`taskId`、`callerId`、`sessionId`、状态、进度阶段和错误码，不记录清单正文、文档正文、结果正文、凭据或内部堆栈。
- 对外错误不得泄露宿主机绝对路径；允许返回接口契约中的 `fileId`。

## 11. 稳定错误与 HTTP 语义

接口层稳定错误至少包括：

- `INVALID_REQUEST`
- `IDEMPOTENCY_CONFLICT`
- `TASK_NOT_FOUND`
- `QUEUE_SUBMISSION_FAILED`
- `INPUT_PATH_NOT_ALLOWED`
- `INPUT_URL_NOT_ALLOWED`
- `INPUT_MANIFEST_NOT_FOUND`
- `INPUT_MANIFEST_ACCESS_FAILED`
- `INPUT_MANIFEST_TOO_LARGE`
- `INVALID_INPUT_MANIFEST`
- `EMPTY_DOCUMENT_LIST`
- `DUPLICATE_FILE_ID`
- `DOCUMENT_PATH_NOT_ALLOWED`
- `DOCUMENT_NOT_FOUND`
- `DOCUMENT_READ_FAILED`
- `DOCUMENT_TOO_LARGE`
- `BATCH_TOO_LARGE`
- `UNSUPPORTED_DOCUMENT_FORMAT`
- `OUTPUT_PATH_NOT_ALLOWED`
- `OUTPUT_CONFLICT`
- `OUTPUT_WRITE_FAILED`
- `PROCESSING_FAILED`
- `INTERNAL_ERROR`

HTTP 层语义：

- 成功创建或命中一致幂等任务：`202`
- POST 基础请求 schema 校验失败：`422`
- 幂等参数冲突：`409`
- 入队基础设施不可用：`503`
- 任务不存在或调用方无权访问：`404`

POST 接受任务后，worker 阶段发现的清单、文档、处理和输出错误通过 GET 的 `failed` 状态返回，不改变已完成的 POST 响应。

## 12. 接口层测试与验收

本阶段接口实现至少覆盖：

- POST 必填项、空值、长度、camelCase 映射和 `.json` 目标后缀校验。
- 首次提交返回 `202`、`taskId`、`sessionId` 和 `queued`。
- 相同 `(callerId, sessionId)` 且参数一致时返回原任务。
- 相同幂等键但路径不同返回 `409 IDEMPOTENCY_CONFLICT`。
- 并发提交相同幂等键时数据库只创建一条任务记录。
- 不同调用方使用相同 `sessionId` 时任务相互隔离。
- Celery 消息只携带任务标识、task type 和 schema version。
- 入队失败时任务状态和 POST 的 `503` 响应一致。
- GET 只允许任务所属调用方查询；不存在和越权统一返回 `404`。
- GET 在不同状态下正确映射生命周期、进度、结果和错误字段。
- `result` 与 `error` 互斥，成功响应不返回结果 JSON 正文。
- 路径协议和基础 allowlist 在 API 边界得到校验，调用方不能扩大访问权限。

输入清单深度校验、文档访问、资源限制、去重结果不变量、输出发布和中断恢复测试将在 worker 专项设计与实现计划中展开。

## 13. 后续专项设计边界

接口实现完成后，worker 专项设计需要独立确定：

- `.md`、`.txt` 和 `.json` 的内容加载与规范化方式。
- 精确去重、近似去重、特征生成和聚类方案。
- `groupId` 生成规则。
- 重复组内唯一 `keep=true` 文档的选择规则。
- 大批次的内存、并行度、分片和进度更新策略。
- 重试、租约、中断恢复、输出摘要和原子发布的具体实现。
- 真实数据验证、性能基线和部署资源限制默认值。

这些决策不得改变本文已经确认的外部 API、幂等、全批失败、结果字段和每组唯一保留项约束；如果确需修改，必须先更新并重新确认接口设计。
