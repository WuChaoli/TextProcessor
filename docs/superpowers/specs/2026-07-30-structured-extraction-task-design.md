# 结构化提取异步单条任务接口设计

## 1. 目标与范围

首个业务接口用于接收单个文本类文件的结构化提取请求，由后台任务将输入转换为 Markdown 文件。接口不等待处理完成，也不回调业务系统。调用方获得 `taskId` 后，通过 GET 接口轮询任务状态和结果路径。

本设计只确定 API、任务编排、幂等、状态、存储、安全和可靠性边界。具体文本格式、格式识别策略、提取算法及 processor adapter 的实现拆分，在 worker 专项设计中确定。首版允许通过 processor 能力注册表逐步接入纯文本、Markdown、HTML、JSON、XML、CSV 等文本类格式；没有已注册 processor 的格式必须明确失败。

接口不返回 Markdown 正文。PostgreSQL 不保存输入正文或 Markdown 正文。

## 2. 总体架构

采用 PostgreSQL、Celery 和 Redis：

- FastAPI API 负责身份认证、请求校验、协议适配、幂等判断、任务创建和入队，不读取输入文件，也不执行提取算法。
- PostgreSQL 是任务状态和任务参数的唯一权威来源。
- Redis 只作为 Celery broker，不保存权威业务状态。
- Celery 消息只携带 `taskId`、任务类型和 schema version。
- Worker 根据 `taskId` 从 PostgreSQL 读取完整参数，选择输入、调用 processor、发布输出并更新任务状态。
- Processor 只负责将选定输入转换为 Markdown，不依赖 HTTP、数据库、Celery 或任务状态。

不采用 FastAPI `BackgroundTasks`，避免 API 进程重启造成任务丢失；不在首版自建数据库轮询 worker，避免重复实现锁、租约、心跳和重试机制。

## 3. API 契约

### 3.1 创建任务

`POST /api/v1/structured-extraction/tasks`

请求体：

```json
{
  "sessionId": "session-001",
  "fileId": "11",
  "fileStoragePath": "/data/txt/1.txt",
  "fileOssUrl": "http://ww.1.txt",
  "targetPath": "/data/txt2md/1.md"
}
```

字段规则：

- `sessionId`、`fileId`、`targetPath` 必填。
- `fileStoragePath` 和 `fileOssUrl` 至少提供一个。
- 两个输入同时提供时固定使用 `fileStoragePath`。
- 选定输入读取失败时任务失败，不自动降级到另一个输入。
- `targetPath` 是最终 Markdown 文件的完整绝对路径，必须以 `.md` 结尾。

首次创建并成功入队时返回 `202 Accepted`：

```json
{
  "taskId": "01K...",
  "sessionId": "session-001",
  "fileId": "11",
  "status": "queued"
}
```

命中参数一致的幂等任务时仍返回 `202 Accepted`，其中 `taskId` 和 `status` 是原任务的值。

### 3.2 查询任务

`GET /api/v1/structured-extraction/tasks/{taskId}`

执行中响应：

```json
{
  "taskId": "01K...",
  "sessionId": "session-001",
  "fileId": "11",
  "status": "running",
  "createdAt": "2026-07-30T10:00:00+08:00",
  "startedAt": "2026-07-30T10:00:02+08:00",
  "finishedAt": null,
  "result": null,
  "error": null
}
```

成功响应：

```json
{
  "taskId": "01K...",
  "sessionId": "session-001",
  "fileId": "11",
  "status": "succeeded",
  "createdAt": "2026-07-30T10:00:00+08:00",
  "startedAt": "2026-07-30T10:00:02+08:00",
  "finishedAt": "2026-07-30T10:00:08+08:00",
  "result": {
    "fileStoragePath": "/data/txt/1.txt",
    "fileOssUrl": "http://ww.1.txt",
    "targetPath": "/data/txt2md/1.md"
  },
  "error": null
}
```

失败响应：

```json
{
  "taskId": "01K...",
  "sessionId": "session-001",
  "fileId": "11",
  "status": "failed",
  "createdAt": "2026-07-30T10:00:00+08:00",
  "startedAt": "2026-07-30T10:00:02+08:00",
  "finishedAt": "2026-07-30T10:00:03+08:00",
  "result": null,
  "error": {
    "code": "OUTPUT_CONFLICT",
    "message": "目标文件已存在"
  }
}
```

`result` 和 `error` 必须互斥。接口始终不返回 Markdown 正文。外部协议使用 camelCase，Python 和数据库内部使用 snake_case，并在 schema 边界显式映射。

## 4. 幂等规则

幂等键为 `(callerId, sessionId, fileId)`：

- 同一调用方使用相同 `sessionId + fileId` 重复提交且参数一致时，不创建新任务，返回原 `taskId` 和当前状态。
- `sessionId` 不同即视为不同请求。
- 原任务已失败或取消时仍返回原任务；重新处理必须使用新的 `sessionId`。
- 相同幂等键携带不同的 `fileStoragePath`、`fileOssUrl` 或 `targetPath` 时，返回 `409 IDEMPOTENCY_CONFLICT`。
- 参数规范化后生成 `requestFingerprint`，用于识别参数冲突。
- 数据库必须对 `(caller_id, session_id, file_id)` 建立唯一约束，以处理并发提交；不能只依赖先查询再插入。

## 5. 状态机与数据流

任务状态为：

```text
pending -> queued -> running -> succeeded
                     |-------> failed
                     |-------> cancelled
```

所有转换必须通过带当前状态条件的数据库更新完成，禁止绕过合法转换。

创建与执行流程：

1. API 完成身份认证和基础参数校验。
2. API 查询或创建幂等任务；并发插入由数据库唯一约束收敛。
3. 新任务写入 PostgreSQL 后投递 Celery 消息。
4. 入队成功后任务转为 `queued` 并返回 `202`。
5. 入队失败时任务转为 `failed`，记录 `QUEUE_SUBMISSION_FAILED`，POST 返回 `503`。
6. Worker 根据 `taskId` 读取任务，通过条件更新从 `queued` 原子转为 `running`。
7. 重复消息遇到终态任务时安全结束；未抢占到执行权的 worker 不执行 processor。
8. Worker 按既定优先级选择输入，并完成访问策略、大小、超时和格式能力校验。
9. Worker 在处理前检查 `targetPath`；目标存在时以 `OUTPUT_CONFLICT` 失败。
10. Worker 调用 processor，将结果写入目标目录内属于本任务的临时文件。
11. Worker 校验输出并计算摘要，以禁止覆盖的原子操作发布到 `targetPath`。
12. 发布阶段再次检查冲突。发布成功后写入结果引用并转为 `succeeded`。
13. 失败时只清理本任务的临时产物，不删除或修改已有目标文件。

## 6. 输出发布与中断恢复

处理前的目标存在检查只能用于快速失败，不能代替发布阶段的原子冲突保护。两个任务同时竞争同一 `targetPath` 时，最终只能有一个任务成功。

为处理“文件已发布但数据库尚未更新成功”的崩溃窗口：

1. 发布前将输出 SHA-256、临时文件标识和发布阶段记录到 PostgreSQL。
2. Worker 以禁止覆盖的原子操作发布结果。
3. 若 worker 在发布后、状态更新前崩溃，重投任务检查目标文件摘要。
4. 目标摘要与本任务的预发布摘要一致时，将任务恢复为 `succeeded`。
5. 摘要不一致时，以 `OUTPUT_CONFLICT` 失败。

Worker 使用延迟确认。任务记录有限重试次数和执行租约或心跳；worker 中断后，恢复机制可以重新执行超出租约的任务。所有重试沿用同一 `taskId`。

## 7. 数据模型

任务记录至少包含以下逻辑字段：

```text
task_id
task_type
schema_version
caller_id
session_id
file_id
request_fingerprint
file_storage_path
file_oss_url
selected_input_type
target_path
status
attempt_count
max_attempts
lease_expires_at
prepared_output_sha256
staging_path
published_at
error_code
error_message
created_at
queued_at
started_at
finished_at
updated_at
```

`taskId` 使用不可预测的 UUID 或 ULID。PostgreSQL 只保存任务参数、状态、输入摘要、结果引用、错误摘要和生命周期字段。

## 8. 安全约束

- POST 和 GET 均使用服务端身份认证。
- GET 同时匹配 `taskId + callerId`。任务不存在和无权访问统一返回 `404`，避免任务枚举。
- `fileStoragePath` 必须是规范化后的绝对路径，且位于配置的输入根目录 allowlist 内。
- 拒绝 `..`、符号链接逃逸和不受控设备路径。
- `targetPath` 必须位于配置的输出根目录 allowlist 内。首版禁止输入根目录和输出根目录重叠。
- `fileOssUrl` 作为受控只读 URI 处理。禁止 URL 内嵌凭据、重定向逃出 allowlist、访问 loopback、link-local、云元数据地址或任意未授权公网地址。
- 服务端统一限制协议、host/CIDR、端口、输入大小、下载时长、重定向次数和 worker 处理时限。
- 请求不能携带或扩大服务端文件、网络、OSS 或其他存储凭据权限。
- 日志记录 `requestId`、`taskId`、`callerId`、状态转换和错误码，不记录文本正文、Markdown 正文、凭据或完整内部堆栈。
- 原始路径可以作为任务数据保存并按接口契约返回，但普通日志只记录脱敏路径摘要。

## 9. 错误与重试

稳定错误码包括：

- `INVALID_REQUEST`
- `INPUT_PATH_NOT_ALLOWED`
- `INPUT_URL_NOT_ALLOWED`
- `INPUT_NOT_FOUND`
- `INPUT_ACCESS_FAILED`
- `INPUT_TOO_LARGE`
- `UNSUPPORTED_INPUT_FORMAT`
- `PROCESSING_FAILED`
- `INVALID_PROCESSOR_OUTPUT`
- `OUTPUT_PATH_NOT_ALLOWED`
- `OUTPUT_CONFLICT`
- `OUTPUT_WRITE_FAILED`
- `IDEMPOTENCY_CONFLICT`
- `QUEUE_SUBMISSION_FAILED`
- `INTERNAL_ERROR`

参数、权限、格式不支持、输出冲突和确定性 processor 错误不重试。短暂网络错误、受控远程输入超时和临时存储错误可以使用有限次数及退避策略重试，不允许无限重试。

HTTP 层使用以下语义：

- 成功创建或命中一致幂等任务：`202`
- 基础请求校验失败：`422`
- 幂等参数冲突：`409`
- 入队基础设施不可用：`503`
- 任务不存在或调用方无权访问：`404`

Worker 内发现的错误通过 GET 的任务失败状态返回，不改变已完成的 POST 响应。

## 10. 测试与验收

### 10.1 单元测试

- 请求必填项、空值、长度和格式校验。
- 两个输入都为空时拒绝。
- 两个输入同时存在时固定选择本地路径。
- 本地输入失败时不降级到远程输入。
- 输入输出路径规范化、路径逃逸和符号链接逃逸。
- URL 协议、host/CIDR、凭据、重定向和大小限制。
- 幂等命中、幂等参数冲突和并发唯一约束。
- 所有合法与非法状态转换。
- Processor 注册、选择及不支持格式。
- Processor 输出校验、临时写入和原子发布。
- 处理前输出冲突和发布时并发冲突。
- 临时文件清理不会影响已有文件。
- 输出摘要匹配时恢复成功，摘要不匹配时判定冲突。
- 错误码映射和敏感信息脱敏。

### 10.2 集成测试

- POST、PostgreSQL 和 Redis/Celery 入队链路。
- 并发提交相同幂等键时只创建一个任务。
- 入队失败后任务状态与 POST 响应一致。
- Celery 消息只携带任务标识，worker 从 PostgreSQL 获取完整参数。
- 重复消息不会重复执行或覆盖输出。
- Worker 中断、消息重新投递和执行租约恢复。
- 短暂错误有限重试，确定性错误不重试。
- 发布完成但数据库未更新时恢复为成功。
- 不同任务竞争同一目标时只有一个成功。
- GET 调用方隔离及防枚举行为。
- 各状态下 `result` 和 `error` 的互斥响应。
- 最终文件完整可读，消费者无法看到半成品。
- 数据库和日志不包含文本正文或 Markdown 正文。

真实 processor、真实大型文件和真实外部输入源测试与默认快速测试分离。没有实际执行的真实环境验证不得声称通过。

### 10.3 接口验收

- 首次 POST 返回 `202`、新 `taskId` 和 `queued`。
- 参数一致的重复 POST 返回 `202` 和同一 `taskId`。
- 相同幂等键但路径不同返回 `409 IDEMPOTENCY_CONFLICT`。
- GET 执行中任务返回 `queued` 或 `running`，且 `result`、`error` 均为空。
- GET 成功任务返回原输入引用和 `targetPath`，不包含 Markdown 正文。
- GET 失败任务返回稳定错误码和脱敏信息，且 `result` 为空。
