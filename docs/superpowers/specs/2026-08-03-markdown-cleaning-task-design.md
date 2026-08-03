# Markdown 组合清洗异步任务接口设计

## 1. 目标与范围

本接口接收单个 Markdown 文件，对文档执行固定的段落去重、规则脱敏和 Markdown 格式规范化，异步生成一个新的 Markdown 文件。调用方通过 POST 获得 `taskId`，再通过 GET 轮询任务状态和结果路径；接口不等待处理完成、不回调业务系统，也不返回 Markdown 正文。

本设计只确定外部 API、幂等、任务状态、访问控制、结果摘要和错误边界。输入解析、本地 processor 执行、输出发布和恢复由 Worker 专项设计负责；具体清洗规则由 Markdown Cleaning Processor 专项设计负责。

首版只处理 UTF-8 Markdown 文件，不接收正文、处理器列表、规则表达式、掩码或阈值。结构化提取、跨文档全局去重、语义改写和实体识别不属于本接口。

## 2. 总体架构

- FastAPI route 只负责身份认证、基础校验、协议映射、幂等任务创建和 Celery 投递。
- PostgreSQL 是任务参数、状态和生命周期的唯一权威来源。
- Redis 只作为 Celery broker。
- Celery 消息只携带 `taskId`、任务类型和 schema version。
- Worker 读取输入，在受控执行器中调用固定的 `MarkdownCleaningProcessor`，校验并发布输出。
- `MarkdownCleaningProcessor` 与 FastAPI route、PostgreSQL、Redis、Celery 和业务路径解耦。

该能力使用独立的 route、任务表、Celery task namespace 和配置，不复用结构化提取或全局去重的业务任务记录。

## 3. API 契约

### 3.1 创建任务

`POST /api/v1/markdown-cleaning/tasks`

请求体：

```json
{
  "sessionId": "session-001",
  "fileId": "11",
  "fileStoragePath": "/data/markdown/1.md",
  "fileOssUrl": "https://oss.internal.example/1.md",
  "targetPath": "/data/cleaned/1.md"
}
```

字段规则：

- `sessionId`、`fileId`、`targetPath` 必填、非空。
- `fileStoragePath` 与 `fileOssUrl` 至少提供一个。
- 两个输入同时提供时固定选择 `fileStoragePath`；选定输入失败时不降级到另一个输入。
- `fileStoragePath` 是 Markdown 输入文件的绝对本地路径。
- `fileOssUrl` 是受控的只读 `http(s)` 输入 URI；字段名保持现有业务协议兼容，不代表调用方可以传入 OSS 凭据。
- `targetPath` 是最终 Markdown 文件的完整绝对本地路径。
- 三个路径字段均不得为空白；输入和输出文件名必须以 `.md` 或 `.markdown` 结尾，大小写不敏感。
- `targetPath` 不得与选定输入指向同一文件，首版不允许原地清洗。

新任务创建并成功入队返回 `202 Accepted`：

```json
{
  "taskId": "01K1CLEANING00000000000001",
  "sessionId": "session-001",
  "fileId": "11",
  "status": "queued"
}
```

命中参数一致的幂等任务时仍返回 `202 Accepted`，其中 `taskId` 和 `status` 为原任务当前值。

### 3.2 查询任务

`GET /api/v1/markdown-cleaning/tasks/{taskId}`

执行中：

```json
{
  "taskId": "01K1CLEANING00000000000001",
  "sessionId": "session-001",
  "fileId": "11",
  "status": "running",
  "progress": {
    "phase": "cleaning",
    "percent": 45
  },
  "createdAt": "2026-08-03T10:00:00+08:00",
  "startedAt": "2026-08-03T10:00:02+08:00",
  "finishedAt": null,
  "result": null,
  "error": null
}
```

成功：

```json
{
  "taskId": "01K1CLEANING00000000000001",
  "sessionId": "session-001",
  "fileId": "11",
  "status": "succeeded",
  "progress": {
    "phase": "completed",
    "percent": 100
  },
  "createdAt": "2026-08-03T10:00:00+08:00",
  "startedAt": "2026-08-03T10:00:02+08:00",
  "finishedAt": "2026-08-03T10:00:08+08:00",
  "result": {
    "fileId": "11",
    "fileStoragePath": "/data/markdown/1.md",
    "fileOssUrl": "https://oss.internal.example/1.md",
    "targetPath": "/data/cleaned/1.md",
    "summary": {
      "duplicateParagraphsRemoved": 3,
      "redactions": {
        "phone": 2,
        "idCard": 1,
        "bankCard": 0,
        "email": 4,
        "ipv4": 1
      },
      "formattingChanges": 12
    }
  },
  "error": null
}
```

失败：

```json
{
  "taskId": "01K1CLEANING00000000000001",
  "sessionId": "session-001",
  "fileId": "11",
  "status": "failed",
  "progress": {
    "phase": "validating_input",
    "percent": 5
  },
  "createdAt": "2026-08-03T10:00:00+08:00",
  "startedAt": "2026-08-03T10:00:02+08:00",
  "finishedAt": "2026-08-03T10:00:03+08:00",
  "result": null,
  "error": {
    "code": "INVALID_MARKDOWN_INPUT",
    "message": "输入文件不是有效的 UTF-8 Markdown 文件"
  }
}
```

`result` 与 `error` 必须互斥。外部协议使用 camelCase，Python 与数据库内部使用 snake_case 并在 schema 边界显式映射。API 不返回输入或输出正文、敏感值、匹配位置、底层库异常或宿主机内部 staging 路径。

## 4. 幂等规则

幂等键为 `(callerId, sessionId, fileId)`：

- 相同幂等键且规范化后的 `fileStoragePath + fileOssUrl + targetPath` 一致时，返回原任务。
- 相同幂等键但任一参数不同，返回 `409 IDEMPOTENCY_CONFLICT`。
- 原任务已失败或取消时仍返回原任务；重新处理必须使用新的 `sessionId`。
- 数据库对 `(caller_id, session_id, file_id)` 建立唯一约束，收敛并发创建。
- 请求参数规范化后生成 `requestFingerprint`，但摘要不能替代数据库唯一约束。
- processor contract version 固定为 `markdown_cleaning_v1`，不属于调用方参数；行为升级必须使用新版本并显式迁移，不能静默改变已创建任务。

## 5. 状态与进度

状态机：

```text
pending -> queued -> running -> succeeded
                     |-------> failed
                     |-------> cancelled
```

首版不提供取消接口，`cancelled` 仅作为受控运维和后续扩展终态。所有状态转换使用带当前状态条件的数据库更新。

公共进度阶段固定为：

```text
validating_input
cleaning
publishing
completed
```

`percent` 为 `0..100` 的单调非递减整数，只表示阶段性估算，不承诺与耗时线性对应。本地 processor 的细粒度步骤不进入公共 API。

## 6. 数据模型

任务记录至少包含：

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
processor_contract_version
status
processing_phase
progress_percent
attempt_count
max_attempts
lease_token
lease_expires_at
input_sha256
prepared_output_sha256
output_sha256
staging_path
published_at
duplicate_paragraphs_removed
phone_redaction_count
id_card_redaction_count
bank_card_redaction_count
email_redaction_count
ipv4_redaction_count
formatting_change_count
error_code
error_message
created_at
queued_at
started_at
finished_at
updated_at
```

PostgreSQL 不保存 Markdown 正文、匹配到的敏感值或逐项匹配位置。

## 7. 访问与安全

- POST 与 GET 均使用现有服务端身份认证。
- GET 同时匹配 `taskId + callerId`；不存在和无权访问统一返回 `404`。
- 本地输入和输出必须是规范化后的绝对路径，并位于各自 allowlist 根目录内。
- 拒绝 `..`、符号链接逃逸、设备路径、输入输出同文件和输入输出根目录违规重叠。
- `fileOssUrl` 只允许服务端配置的协议、host/CIDR、端口和重定向目标；禁止内嵌凭据、loopback、link-local、云元数据地址和未授权公网地址。
- 服务端限制输入字节数、下载时长、重定向次数和任务总时长。
- 请求不能提供或扩大文件、网络、对象存储或服务端处理器权限。
- 日志只记录 `requestId`、`taskId`、`callerId`、状态、阶段、计数和错误码，不记录正文、敏感值或完整路径。

## 8. 稳定错误码

请求与幂等：

- `INVALID_REQUEST`
- `IDEMPOTENCY_CONFLICT`
- `QUEUE_SUBMISSION_FAILED`

输入：

- `INPUT_PATH_NOT_ALLOWED`
- `INPUT_URL_NOT_ALLOWED`
- `INPUT_NOT_FOUND`
- `INPUT_ACCESS_FAILED`
- `INPUT_TOO_LARGE`
- `INVALID_MARKDOWN_INPUT`

处理：

- `PROCESSING_FAILED`
- `PROCESSING_TIMEOUT`
- `INVALID_PROCESSOR_OUTPUT`

输出与系统：

- `OUTPUT_PATH_NOT_ALLOWED`
- `OUTPUT_CONFLICT`
- `OUTPUT_WRITE_FAILED`
- `OUTPUT_INTEGRITY_FAILED`
- `DATABASE_ERROR`
- `INTERNAL_ERROR`

HTTP 语义：一致幂等创建返回 `202`，请求校验失败返回 `422`，幂等或输出前置冲突返回 `409`，入队基础设施不可用返回 `503`，任务不存在或调用方无权访问返回 `404`。POST 返回后发生的错误通过 GET 的失败状态表达。

## 9. 测试与验收

### 9.1 API 单元测试

- 必填项、空白、长度、Markdown 后缀和绝对路径校验。
- 两个输入都缺失时拒绝；同时存在时固定选择本地输入。
- 输入输出同文件时拒绝。
- 相同幂等参数返回原任务，不同参数返回冲突。
- 并发相同幂等键只创建一条任务。
- GET 调用方隔离和防枚举。
- 各状态的 `result`/`error` 互斥与 camelCase 映射。
- 成功摘要只含安全计数，不含敏感值或匹配位置。

### 9.2 集成测试

- POST、PostgreSQL 和 Redis/Celery 入队链路。
- 入队失败时任务状态与 HTTP 503 一致。
- Celery 消息只携带任务标识、类型和 schema version。
- 重复 POST 和重复消息不创建重复处理。
- GET 进度单调且终态结构稳定。
- 数据库与日志不包含 Markdown 正文或匹配到的敏感值。

### 9.3 接口验收

- 新请求返回 `202`、新 `taskId` 和 `queued`。
- 一致重放返回同一 `taskId`；冲突重放返回 `409`。
- 成功任务返回原始业务字段、`targetPath` 和安全汇总计数。
- 失败任务返回稳定错误码和脱敏信息。
- 公共 API 不接受 processor 版本、处理步骤、规则或掩码参数。
