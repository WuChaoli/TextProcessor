# 全局文档去重 TextProcessor Worker 设计

## 1. 目标与范围

本文定义 TextProcessor 侧全局文档去重异步任务的 worker 功能、Celery 拆分、输入下载与 staging、Data-Juicer Service adapter、轮询与恢复、业务结果映射、输出发布、异常分类和测试要求。

本文建立在《全局文档去重异步任务接口设计》之上。外部调用方通过 POST 获得 `taskId`，再通过 GET 查询状态、进度和最终结果 JSON 文件地址；不接收回调，也不返回结果正文。

本文不定义 Data-Juicer Service 内部的 FastAPI、Celery、PostgreSQL、精准去重、MinHash 聚类、profile 执行和部署实现。TextProcessor 只依赖本文定义的稳定 adapter 契约。Data-Juicer Service 将通过独立专项设计确定。

## 2. 总体处理链路

```text
业务方提交任务
    ↓
TextProcessor API 创建任务并入队
    ↓
global_deduplication.submit
    ├── 读取 PostgreSQL 任务
    ├── 下载并校验输入清单
    ├── 下载、解码并校验全部文档
    ├── 写入 input.jsonl 和 mapping.json
    ├── 提交 Data-Juicer job
    └── 保存 externalJobId，调度 poll
          ↓
global_deduplication.poll
    ├── 查询 Data-Juicer job
    ├── 未完成：更新内部进度并延迟重投 poll
    └── 已完成：
          ├── 读取并校验 datajuicer-result.jsonl
          ├── 映射 fileId/fileStoragePath/groupId/keep
          ├── 原子发布最终 JSON
          └── 更新任务为 succeeded

global_deduplication.recover
    └── 恢复提交不确定、轮询过期和 worker 中断任务
```

TextProcessor worker 不直接导入或执行 Data-Juicer Python 包。所有去重和聚类能力通过独立 Data-Juicer Service adapter 调用。

## 3. Celery Task 拆分

### 3.1 `global_deduplication.submit`

职责：

1. 校验 Celery 消息的 `taskId`、task type 和 schema version。
2. 根据 `taskId` 从 PostgreSQL 读取完整任务参数。
3. 使用条件更新获得任务执行权，重复消息不得重复准备输入或提交外部 job。
4. 将任务转为 `running`，设置执行租约和 `processingPhase=validating_input`。
5. 读取 `inputJsonPath`，执行协议、访问、超时和大小策略。
6. 解析并校验输入清单。
7. 下载全部文档，执行格式、大小、累计大小和 UTF-8 解码校验。
8. 在任务专属 staging 目录写入 `input.jsonl` 和 `mapping.json`。
9. 以 `requestId=taskId` 幂等提交 Data-Juicer job。
10. 保存 `externalJobId`、外部 profile 和首次轮询时间。
11. 提交完成后释放本次 submit 执行租约并延迟调度 `poll`。

`submit` 可以执行较长时间的文件下载和 staging，但不得在 FastAPI 请求进程中执行。

### 3.2 `global_deduplication.poll`

职责：

1. 根据 `taskId` 获取短期 poll 租约，防止重复 poll 消息并发查询和 finalize。
2. 查询 Data-Juicer job 状态。
3. 外部 job 为 `pending`、`queued` 或 `running` 时：
   - 保存外部状态和安全进度摘要；
   - 将对外 phase 保持为 `deduplicating`；
   - 计算下一次轮询时间；
   - 延迟重新投递同一个 `poll` task；
   - 释放 poll 租约。
4. 外部 job 为 `failed` 或 `cancelled` 时：
   - 映射稳定错误；
   - 将 TextProcessor 任务转为 `failed`；
   - 不发布部分业务结果。
5. 外部 job 为 `succeeded` 时，在本次 poll 内完成 finalize：
   - 读取外部结果；
   - 校验摘要、schema 和聚类不变量；
   - 生成最终业务结果；
   - 原子发布到 `targetPath`；
   - 将任务转为 `succeeded`。

不单独创建 `finalize` Celery task。结果映射和单个 JSON 文件发布相对较快，由获得 finalize 权的 `poll` 完成。

### 3.3 `global_deduplication.recover`

Celery Beat 周期投递恢复扫描。恢复范围：

- `pending` 且超过入队确认窗口的任务；
- `queued` 且超过 dispatch 窗口的任务；
- `running` 且 submit 执行租约过期、尚未保存 `externalJobId` 的任务；
- 已保存 `externalJobId` 但 `nextPollAt` 到期且 poll 租约不存在或已过期的任务；
- 外部结果已发布到 staging，但 TextProcessor 尚未发布最终业务结果的任务；
- 最终业务结果已发布，但 PostgreSQL 尚未更新为 `succeeded` 的任务。

恢复扫描只负责重新投递需要执行的 task 和更新恢复标记，不在 Beat 进程内下载文件、调用外部服务或发布结果。

## 4. Celery 消息契约

所有消息只携带：

```json
{
  "taskId": "0198f000-0000-7000-8000-000000000001",
  "taskType": "global_deduplication",
  "schemaVersion": 1
}
```

禁止在 Celery 消息中携带：

- `inputJsonPath`
- `targetPath`
- 文档清单
- 文档正文
- Data-Juicer profile 参数
- 数据库或存储凭据

完整任务参数始终从 PostgreSQL 读取。

## 5. 输入清单下载与校验

### 5.1 清单读取

`inputJsonPath` 通过 TextProcessor 已有的 `fsspec` 边界读取。首版支持项目配置允许的受控本地路径、`file://`、`http(s)://` 和 `s3://` 输入。

读取要求：

- 下载和解析前限制清单最大字节数；
- 使用 UTF-8 解码；
- 顶层必须是非空 JSON 数组；
- 每条记录必须是 JSON object；
- `fileId` 和 `fileStoragePath` 必填、非空；
- `fileId` 在批次内必须唯一；
- 未知字段允许出现，但立即丢弃，不进入 staging 和结果；
- 任一错误使整个任务失败。

### 5.2 文档读取

对每条记录：

1. 根据 `fileStoragePath` 选择受控读取 adapter。
2. 根据扩展名校验格式，仅接受 `.md`、`.txt`、`.json`。
3. 在完整读入前尽可能检查文件大小。
4. 以流式或有界方式读取，强制执行单文件和批次累计大小限制。
5. 使用 UTF-8 解码；非法编码使整个任务失败。
6. 去除 UTF-8 BOM。
7. 将 `CRLF` 和 `CR` 统一为 `LF`。
8. 保留正文中的空格、数字、标点、Markdown 标记和 JSON 原始结构。
9. JSON 文档首版按原始文本处理，不解析后重排 key，也不做结构规范化。

精准匹配时允许 Data-Juicer profile 忽略整个文档首尾空白，但不得忽略正文内部空白、数字、标点或大小写。MinHash profile 可以按已确认配置统一英文字母大小写。

只有所有文档均校验成功后才允许提交 Data-Juicer job。

## 6. Staging 布局与所有权

每个任务使用独立目录：

```text
{stagingRoot}/{taskId}/
├── input.jsonl
├── mapping.json
├── datajuicer-result.jsonl
├── final-result.json
└── manifest.json
```

职责：

- TextProcessor 创建并管理整个目录。
- TextProcessor 写入 `input.jsonl`、`mapping.json`、`final-result.json` 和 `manifest.json`。
- Data-Juicer Service 只读取 `input.jsonl`，只写入指定的 `datajuicer-result.jsonl`。
- Data-Juicer Service 不删除任何 staging 文件。
- TextProcessor 在任务生命周期结束后按保留期限清理 staging。
- 仍可能恢复的任务不得被清理。

首版假定 TextProcessor worker 与 Data-Juicer Service 位于可信内部环境并可直接访问相同本地路径，不额外设计两服务之间的路径授权、签名或文件传输协议。

### 6.1 `input.jsonl`

每个文档一行，只包含 Data-Juicer 所需字段：

```jsonl
{"uid":0,"text":"第一份文档内容"}
{"uid":1,"text":"第二份文档内容"}
```

`uid` 是从 `0` 开始、按输入清单记录顺序分配的任务内唯一整数。业务 `fileId` 和文件路径不写入该文件。

### 6.2 `mapping.json`

TextProcessor 保存业务映射：

```json
{
  "schemaVersion": 1,
  "taskId": "0198f000-0000-7000-8000-000000000001",
  "documents": [
    {
      "uid": 0,
      "fileId": "1",
      "fileStoragePath": "/data/txt/1.md"
    }
  ]
}
```

`mapping.json` 不提交给 Data-Juicer Service。

### 6.3 `manifest.json`

manifest 至少记录：

```text
schema_version
task_id
profile
input_document_count
input_jsonl_sha256
mapping_sha256
datajuicer_result_sha256
final_result_sha256
created_at
updated_at
```

manifest 不保存文档正文。

## 7. Data-Juicer Service Adapter 契约

### 7.1 提交

```http
POST /v1/jobs
```

```json
{
  "requestId": "0198f000-0000-7000-8000-000000000001",
  "profile": "text_exact_minhash_v1",
  "inputPath": "/data/textprocessor-staging/task-id/input.jsonl",
  "outputPath": "/data/textprocessor-staging/task-id/datajuicer-result.jsonl"
}
```

接受响应：

```json
{
  "jobId": "0198f100-0000-7000-8000-000000000001",
  "requestId": "0198f000-0000-7000-8000-000000000001",
  "profile": "text_exact_minhash_v1",
  "status": "queued"
}
```

TextProcessor 要求 Data-Juicer Service 以 `requestId` 实现幂等：

- 相同 requestId 和相同参数返回原 job。
- 相同 requestId 和不同参数返回 `409 IDEMPOTENCY_CONFLICT`。
- 提交超时后 TextProcessor 可以使用相同请求安全重试。

### 7.2 查询

```http
GET /v1/jobs/{jobId}
```

成功终态示例：

```json
{
  "jobId": "0198f100-0000-7000-8000-000000000001",
  "requestId": "0198f000-0000-7000-8000-000000000001",
  "profile": "text_exact_minhash_v1",
  "status": "succeeded",
  "progress": {
    "phase": "completed",
    "total": 100,
    "processed": 100,
    "percent": 100
  },
  "result": {
    "outputPath": "/data/textprocessor-staging/task-id/datajuicer-result.jsonl",
    "outputSha256": "..."
  },
  "error": null
}
```

TextProcessor 必须验证：

- `jobId` 与已保存外部 job 一致；
- `requestId` 等于当前 `taskId`；
- `profile` 等于任务记录中的 profile；
- result 和 error 互斥；
- 成功结果的 `outputPath` 等于提交时指定路径；
- `outputSha256` 是合法 SHA-256；
- 外部状态和进度字段属于已知枚举与合法范围。

### 7.3 超时

Adapter 使用相互独立的连接超时和响应读取超时。单次 HTTP 超时不能覆盖完整 Data-Juicer job 时长；长任务通过轮询处理。

轮询采用配置化退避和最大处理期限：

```text
GLOBAL_DEDUP_DATAJUICER_SUBMIT_TIMEOUT_SECONDS
GLOBAL_DEDUP_DATAJUICER_POLL_TIMEOUT_SECONDS
GLOBAL_DEDUP_DATAJUICER_POLL_INITIAL_DELAY_SECONDS
GLOBAL_DEDUP_DATAJUICER_POLL_MAX_DELAY_SECONDS
GLOBAL_DEDUP_DATAJUICER_PROCESSING_TIMEOUT_SECONDS
```

## 8. 外部 Profile 约定

TextProcessor 首版固定提交：

```text
text_exact_minhash_v1
```

业务请求不能选择 profile，也不能传递 Data-Juicer operator 或参数。

已确认的 profile 行为假设：

1. 精准去重：
   - 忽略文档首尾空白；
   - 保留大小写；
   - 保留正文内部空白、数字、标点、Markdown 标记和 JSON 结构。
2. MinHash：
   - `tokenization=character`
   - `window_size=5`
   - `lowercase=true`
   - `ignore_pattern=null`
   - `num_permutations=256`
   - `jaccard_threshold=0.7`
   - bands 和 rows 由 Data-Juicer 自动计算。
3. 两阶段合并：
   - 先形成精准重复组并保留完整成员映射；
   - 每个精准组选择临时代表参与 MinHash；
   - MinHash 聚类完成后展开回所有原始成员。
4. 唯一代表选择：
   - 规范化文本长度降序；
   - 长度相同时 uid 升序。

任何 profile 行为变化必须发布新 profile 名称，不得原地改变 `text_exact_minhash_v1`。

## 9. Data-Juicer 结果校验

`datajuicer-result.jsonl` 每个输入 uid 对应一行：

```jsonl
{"uid":0,"clusterId":"cluster-1","representative":true,"method":"exact_minhash"}
{"uid":1,"clusterId":"cluster-1","representative":false,"method":"exact_minhash"}
{"uid":2,"clusterId":null,"representative":true,"method":null}
```

字段：

- `uid: int`
- `clusterId: string | null`
- `representative: boolean`
- `method: "exact" | "minhash" | "exact_minhash" | null`

TextProcessor finalize 前必须验证：

- 文件实际 SHA-256 等于 Data-Juicer 响应中的 `outputSha256`；
- JSONL 每行是合法 object，且只使用已知 schema；
- 输出记录数等于输入记录数；
- 输出 uid 集合与 mapping uid 集合完全一致；
- 不允许重复、缺失、负数或未知 uid；
- `clusterId=null` 时必须 `representative=true` 且 `method=null`；
- 非空 cluster 至少包含两个成员；
- 每个非空 cluster 严格只有一个 `representative=true`；
- 非空 cluster 的 method 一致且属于允许值；
- 不接受正文、路径、凭据或其他非契约字段。

任一不变量失败时，任务以 `INVALID_PROCESSOR_OUTPUT` 失败，不发布业务结果。

## 10. 业务结果映射

TextProcessor 使用 `mapping.json` 和 Data-Juicer 结果生成：

```json
[
  {
    "fileId": "1",
    "fileStoragePath": "/data/txt/1.md",
    "groupId": "9ab31bf4-...",
    "keep": true
  },
  {
    "fileId": "2",
    "fileStoragePath": "/data/txt/2.md",
    "groupId": "9ab31bf4-...",
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

规则：

- `fileId` 和 `fileStoragePath` 来自 `mapping.json`，不信任外部结果携带业务字段。
- `representative` 映射为 `keep`。
- 独立文档固定 `groupId=null`、`keep=true`。
- 每个重复组的业务 `groupId` 使用确定性 UUIDv5：

```text
namespace = taskId
name = 按升序排序后以分隔符连接的成员 uid
```

- Data-Juicer 内部 `clusterId` 不对业务方透传。
- 相同任务无论重试、遍历顺序或 worker 中断，成员集合相同就生成相同 `groupId`。
- 不保证不同任务间相同文档获得相同 `groupId`。
- 最终输出顺序不属于外部契约。实现采用 uid 升序输出，以提高结果可重复性和测试稳定性。
- 输出只包含 `fileId`、`fileStoragePath`、`groupId`、`keep`。

## 11. 业务结果发布与恢复

### 11.1 发布流程

1. 将业务结果写入 staging 下的 `final-result.json.part`。
2. flush、关闭并计算 SHA-256。
3. 校验 JSON 可重新解析，记录数和 uid 映射数量一致。
4. 在 PostgreSQL 保存 prepared output SHA-256、staging path 和发布阶段。
5. 以禁止覆盖的方式原子发布到请求的 `targetPath`。
6. 保存最终路径、摘要和发布时间。
7. 将任务状态更新为 `succeeded`、phase 更新为 `completed`。

### 11.2 输出冲突

- 处理前发现 `targetPath` 已存在时快速以 `OUTPUT_CONFLICT` 失败。
- 发布阶段必须再次执行禁止覆盖的原子冲突保护。
- 不删除、移动或覆盖已有目标文件。
- 不同任务竞争同一 `targetPath` 时只能有一个成功。

### 11.3 崩溃窗口

若最终文件已发布但数据库尚未更新：

- recover 重新投递 poll/finalize；
- worker 读取 prepared output SHA-256；
- 目标文件摘要相同时恢复为 `succeeded`；
- 摘要不同时以 `OUTPUT_CONFLICT` 失败。

只清理当前任务拥有的 `.part` 和 staging 文件，不清理已有目标文件。

## 12. 状态、进度与租约

对外任务状态沿用：

```text
pending -> queued -> running -> succeeded|failed|cancelled
```

TextProcessor 对外进度阶段：

```text
validating_input
loading_documents
deduplicating
publishing_result
completed
```

进度映射：

- 清单下载和 schema 校验：`validating_input`
- 文档下载、解码和 staging：`loading_documents`
- Data-Juicer 所有计算阶段：`deduplicating`
- 结果校验、映射和发布：`publishing_result`
- 成功：`completed`

Data-Juicer 内部详细 phase 只保存为内部元数据和日志，不作为公共 API 枚举。

任务记录至少需要以下 worker 字段：

```text
processing_phase
progress_total
progress_processed
progress_percent
attempt_count
max_attempts
lease_expires_at
external_job_id
external_profile
external_status
external_progress
next_poll_at
processing_deadline
poll_lease_expires_at
staging_path
input_manifest_sha256
input_jsonl_sha256
mapping_sha256
external_output_sha256
prepared_output_sha256
output_sha256
published_at
```

具体表结构可以放入独立 global-deduplication task table；不得复用或污染 `extraction_task`。

## 13. 异常模型

### 13.1 TextProcessor 输入错误

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
- `DOCUMENT_DECODE_FAILED`

这些错误是确定性的，不重试。

### 13.2 Staging 错误

- `STAGING_CREATE_FAILED`
- `STAGING_WRITE_FAILED`
- `STAGING_READ_FAILED`
- `STAGING_INTEGRITY_FAILED`

明确的权限、路径和空间不足错误不重试；可证明为短暂的文件系统错误可以有限重试。

### 13.3 Data-Juicer 提交错误

- `PROCESSOR_PROFILE_NOT_SUPPORTED`
- `PROCESSOR_REQUEST_REJECTED`
- `PROCESSOR_IDEMPOTENCY_CONFLICT`
- `PROCESSOR_SUBMISSION_FAILED`
- `PROCESSOR_SUBMISSION_UNCERTAIN`
- `PROCESSOR_UNAVAILABLE`
- `INVALID_PROCESSOR_RESPONSE`

语义：

- 明确收到 4xx 拒绝时不重试，除受控限流响应外。
- 连接失败且确认请求未到达时可以有限重试。
- 提交请求发送后发生超时或连接中断时，使用 `PROCESSOR_SUBMISSION_UNCERTAIN` 进入恢复流程；不得直接创建另一个外部 job。
- 恢复时以同一 `requestId=taskId` 重新提交，由 Data-Juicer 幂等返回原 job。

### 13.4 Data-Juicer 轮询与执行错误

- `PROCESSOR_JOB_NOT_FOUND`
- `PROCESSOR_POLL_FAILED`
- `PROCESSOR_TIMEOUT`
- `PROCESSOR_FAILED`
- `PROCESSOR_CANCELLED`
- `INVALID_PROCESSOR_OUTPUT`

语义：

- 短暂轮询网络错误有限重试，不立即把业务任务置为 failed。
- 外部 job 明确失败时不由 TextProcessor 创建新 job，映射安全错误后结束业务任务。
- 超过 TextProcessor 配置的整体 processing deadline 时以 `PROCESSOR_TIMEOUT` 失败。
- 外部 job 不存在时先用相同 requestId 执行一次幂等提交恢复；仍无法找到才以 `PROCESSOR_JOB_NOT_FOUND` 失败。
- 外部结果 schema、摘要或不变量失败时以 `INVALID_PROCESSOR_OUTPUT` 失败，不重试相同确定性结果。

### 13.5 输出错误

- `OUTPUT_PATH_NOT_ALLOWED`
- `OUTPUT_CONFLICT`
- `OUTPUT_WRITE_FAILED`
- `OUTPUT_INTEGRITY_FAILED`

输出冲突不重试。短暂输出存储错误可以在未发布最终文件时有限重试。

### 13.6 系统错误

- `QUEUE_SUBMISSION_FAILED`
- `DATABASE_ERROR`
- `INTERNAL_ERROR`

对外错误只返回稳定 code 和安全摘要，不返回外部服务响应正文、文档内容、凭据、内部堆栈或宿主机路径。

## 14. 重试、重复消息与恢复不变量

- 所有 TextProcessor 重试沿用同一 `taskId`。
- 所有 Data-Juicer 提交重试沿用 `requestId=taskId`。
- Celery 消息使用延迟确认和有限重试。
- 重复 submit 消息不能重复下载已完整校验的 staging 输入；可通过 manifest 摘要复用。
- 重复 poll 消息必须通过 poll 租约收敛。
- 终态任务收到重复消息时安全结束。
- 外部 job 成功后重复 finalize 必须生成相同业务 groupId 和相同输出摘要。
- 数据库状态更新使用当前状态或租约 token 条件，避免失去租约的 worker 覆盖新状态。
- 任何失败都不得发布部分业务结果。
- 恢复不得自动切换 profile、改变阈值或改变代表选择规则。

## 15. 可观察性

日志字段至少包括：

```text
request_id
task_id
caller_id
session_id
celery_task_name
processing_phase
external_job_id
external_profile
external_status
attempt_count
error_code
duration_ms
```

日志不得包含：

- 清单 JSON 正文；
- 文档正文；
- Data-Juicer input/output JSONL 正文；
- URI 中的凭据；
- 外部服务原始错误响应；
- 内部堆栈的对外响应。

指标至少包括：

- 创建、成功、失败和恢复任务数；
- 各阶段耗时；
- 文档数和输入总字节数分布；
- Data-Juicer 提交与轮询延迟；
- 提交不确定次数；
- poll 重投次数；
- 输出冲突次数；
- 外部错误码分布。

## 16. 配置边界

TextProcessor 至少新增：

```text
GLOBAL_DEDUP_STAGING_ROOT
GLOBAL_DEDUP_MAX_DOCUMENTS
GLOBAL_DEDUP_MAX_MANIFEST_BYTES
GLOBAL_DEDUP_MAX_DOCUMENT_BYTES
GLOBAL_DEDUP_MAX_TOTAL_BYTES
GLOBAL_DEDUP_DATAJUICER_BASE_URL
GLOBAL_DEDUP_DATAJUICER_PROFILE
GLOBAL_DEDUP_DATAJUICER_SUBMIT_TIMEOUT_SECONDS
GLOBAL_DEDUP_DATAJUICER_POLL_TIMEOUT_SECONDS
GLOBAL_DEDUP_DATAJUICER_POLL_INITIAL_DELAY_SECONDS
GLOBAL_DEDUP_DATAJUICER_POLL_MAX_DELAY_SECONDS
GLOBAL_DEDUP_DATAJUICER_PROCESSING_TIMEOUT_SECONDS
GLOBAL_DEDUP_RECOVERY_INTERVAL_SECONDS
GLOBAL_DEDUP_STAGING_RETENTION_SECONDS
```

首版 `GLOBAL_DEDUP_DATAJUICER_PROFILE` 的唯一允许值为 `text_exact_minhash_v1`。配置从环境或部署配置加载，不固定在 route 或 worker 代码中。

## 17. 测试与验收

### 17.1 单元测试

- Celery 消息 schema、task type 和版本校验。
- 输入清单 UTF-8、非空数组、必填字段和未知字段忽略。
- 重复 `fileId` 使整个任务失败。
- `.md`、`.txt`、`.json` 支持和其他格式拒绝。
- BOM、CRLF/CR 规范化与 JSON 原始文本保留。
- 单文件、清单、文档数和累计大小限制。
- staging 路径由 taskId 派生，输入无法控制任务目录。
- `input.jsonl` 不包含业务路径，`mapping.json` 不包含正文。
- adapter 提交、幂等命中、4xx 拒绝、超时和提交不确定。
- adapter GET 的 jobId、requestId、profile、状态、结果和错误校验。
- 外部结果 uid 集合、cluster 大小、唯一 representative 和 method 校验。
- UUIDv5 groupId 在重试和乱序输入下稳定。
- representative 到 keep 的映射以及独立文档规则。
- 结果只包含四个业务字段。
- 错误分类、是否重试和安全信息映射。

### 17.2 集成测试

- PostgreSQL 任务记录、Celery submit、poll 和 recover 链路。
- worker 真实读取受控本地输入并生成 staging。
- 使用 fake Data-Juicer HTTP service 验证提交和多轮轮询。
- 提交请求在服务端成功但客户端超时时，通过 requestId 恢复原 job。
- 重复 submit 消息只创建一个外部 job。
- 重复 poll 消息只有一个 worker finalize。
- 外部 job 失败、取消、消失、超时和非法输出。
- Celery worker 在下载、提交、轮询、结果映射和发布各阶段中断后的恢复。
- 最终发布前目标已存在及发布时并发冲突。
- 文件发布后数据库更新前崩溃，通过摘要恢复成功。
- 不同任务竞争相同 targetPath 时只有一个成功。
- staging 清理不删除运行中或可恢复任务。

### 17.3 契约测试

TextProcessor 使用固定样例验证 Data-Juicer Service：

- POST request/response schema；
- requestId 幂等与冲突；
- GET 各状态 schema；
- profile 名称；
- outputPath 和 outputSha256；
- 结果 JSONL 完整成员与聚类不变量；
- external error code 映射。

### 17.4 真实验收

默认测试集不启动真实 Data-Juicer。独立的真实集成测试必须覆盖：

- 精准重复 `.md`、`.txt`、`.json`；
- 仅换行、BOM 和首尾空白不同的精准重复；
- 中文 MinHash 近似重复；
- 精准组与 MinHash 组的展开合并；
- 独立文档；
- 长文本优先和 uid 兜底的唯一代表选择；
- TextProcessor API → PostgreSQL → Redis/Celery → Data-Juicer Service → 结果发布 → GET 的完整链路；
- TextProcessor 与 Data-Juicer worker 重启恢复。

未真实启动 Data-Juicer Service 和两个 worker 的测试不得声称端到端打通。

## 18. Data-Juicer Service 后续专项设计输入

后续 Data-Juicer Service spec 必须满足本文 adapter 契约，并独立定义：

- `services/datajuicer_service/` package 布局；
- Data-Juicer v1.5.4 Git submodule 和 Python 3.11 环境；
- FastAPI、Celery、PostgreSQL 和 Redis 实现；
- 复用同一 PostgreSQL/Redis 实例时的 database/user/queue/key 隔离；
- `text_exact_minhash_v1` profile 的精准分组、MinHash、并查集展开和代表选择；
- job 幂等、状态机、租约、重试和恢复；
- 结果 JSONL 原子发布；
- 源码运行验证；
- 验证通过后将 wrapper 与锁定 Data-Juicer 源码打入独立镜像；
- 独立镜像下 API、worker、Beat 和 TextProcessor 真实集成验收。
