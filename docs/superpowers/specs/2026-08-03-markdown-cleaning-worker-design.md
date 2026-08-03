# Markdown 组合清洗 TextProcessor Worker 设计

## 1. 目标与范围

本文定义 Markdown 组合清洗异步任务在 TextProcessor worker 中的输入解析、staging、Data-Juicer Service adapter、提交与轮询、结果校验、原子发布、中断恢复和测试要求。

本文建立在《Markdown 组合清洗异步任务接口设计》之上。TextProcessor worker 不实现段落去重、脱敏或 Markdown 格式规范化算法，不直接导入 Data-Juicer Python 包，也不允许业务请求动态拼装处理步骤。

## 2. 组件边界

```text
Celery execute task
├── MarkdownCleaningRepository
├── InputResolver
├── MarkdownInputValidator
├── CleaningStagingManager
├── DataJuicerCleaningAdapter
└── SubmitOrchestrator

Celery poll task
├── MarkdownCleaningRepository
├── DataJuicerCleaningAdapter
├── CleaningResultValidator
├── AtomicPublisher
└── PollOrchestrator

Celery Beat recovery
└── RecoveryDispatcher
```

- Repository 负责条件状态转换、租约、外部 job 标识、摘要、计数和错误持久化。
- InputResolver 复用项目统一的受控本地与远程读取边界，但使用组合清洗自己的配置和错误映射。
- MarkdownInputValidator 只验证编码、文件边界和可处理性，不执行清洗。
- StagingManager 只管理当前任务的独占目录。
- Adapter 隔离 Data-Juicer HTTP 协议。
- Orchestrator 编排步骤但不包含算法、HTTP transport 细节或最终路径写入实现。
- AtomicPublisher 负责禁止覆盖发布和发布后崩溃恢复。

## 3. Celery 消息与队列

任务名：

```text
markdown_cleaning.execute
markdown_cleaning.poll
markdown_cleaning.recover
```

execute/poll 消息只携带：

```json
{
  "taskId": "01K1CLEANING00000000000001",
  "taskType": "markdown_cleaning",
  "schemaVersion": 1
}
```

规则：

- 不在消息中携带业务路径、正文、profile 参数或凭据。
- `task_acks_late=true`，`worker_prefetch_multiplier=1`。
- 重复消息遇到终态任务时安全结束。
- worker 必须通过带状态与租约条件的数据库更新获得执行权。
- Celery transport retry 与业务重试分别计数，均有限次。

## 4. 完整数据流

### 4.1 execute 阶段

1. 校验消息 schema。
2. 从 PostgreSQL 读取任务完整参数。
3. 从 `queued` 原子转为 `running`，写入租约、attempt 和 `validating_input`。
4. 根据任务创建独占 staging 目录。
5. 按已确定的输入优先级，将选定输入流式复制到 staging。
6. 计算输入 SHA-256 和字节数，验证扩展名、UTF-8 与 Markdown 可处理边界。
7. 生成 Data-Juicer 输入路径和专属结果路径。
8. 以 `requestId=taskId`、固定 `profile=markdown_cleaning_v1` 提交 job。
9. 持久化 `externalJobId`，更新阶段为 `cleaning`。
10. 投递 poll task；execute 不阻塞等待外部 job。

### 4.2 poll 阶段

1. 读取任务与 `externalJobId` 并校验租约。
2. 查询 Data-Juicer job。
3. 外部 job 仍在执行时，保存映射后的单调进度并按退避重新投递 poll。
4. 外部 job 失败时，将稳定错误映射到 TextProcessor 任务。
5. 外部 job 成功时校验 result 元数据、输出路径、SHA-256、UTF-8 和 profile 结果不变量。
6. 将结果复制到 TextProcessor 自己的 staging output，重新计算摘要并验证安全统计。
7. 保存 prepared SHA-256、临时路径、汇总计数并更新为 `publishing`。
8. 以禁止覆盖方式原子发布到 `targetPath`。
9. 保存最终摘要、发布时间，将任务转为 `succeeded/completed/100`。
10. 清理本任务可安全删除的 staging 文件。

poll 不持有长 HTTP 连接覆盖整个处理时长，不使用阻塞 sleep 等待外部完成。

## 5. Staging 设计

```text
{markdownCleaningStagingRoot}/{taskId}/
├── input/
│   └── source.md
├── datajuicer/
│   └── result.md
├── metadata/
│   └── result.json
└── output/
    └── result.md.part
```

- staging 根目录由服务端配置，请求不能指定。
- 每个目录与文件必须验证解析后的真实路径仍属于任务目录。
- 远程输入由 TextProcessor 下载，Data-Juicer 不直接访问业务 URL。
- TextProcessor 只向 Data-Juicer 暴露 job 专属输入与输出路径。
- `result.json` 是 Data-Juicer 的安全元数据，不含正文、敏感原值或匹配位置。
- 重试可复用摘要一致且数据库已记录的输入；摘要不一致时重新准备。
- 清理只删除当前任务目录，不删除业务输入、最终输出或 Data-Juicer 管理范围之外的文件。

## 6. 输入校验

Worker 在提交 Data-Juicer 前必须验证：

- 选定输入可读且为普通文件；
- 文件扩展名为 `.md` 或 `.markdown`；
- 文件大小在配置上限内且非空；
- 字节流可严格解码为 UTF-8；允许 UTF-8 BOM，但 staging 中统一移除 BOM；
- 不包含 NUL 或被禁止的控制字符；
- 解析器可识别并正确闭合围栏代码块；未闭合围栏以 `INVALID_MARKDOWN_INPUT` 失败，避免正文被错误归入代码块保护区；
- 输入与最终输出解析后不是同一文件；
- 输入 SHA-256 与复用 staging 记录一致。

输入允许普通 Markdown 方言内容，但首版不要求验证链接可达、HTML 合法、表格列数一致或 Markdown 语义正确。

## 7. Data-Juicer Adapter 契约

```python
class DataJuicerCleaningAdapter(Protocol):
    def submit(
        self,
        *,
        request_id: str,
        profile: str,
        input_path: Path,
        output_path: Path,
    ) -> CleaningJobSubmission: ...

    def get_job(self, job_id: str) -> CleaningJobStatus: ...
```

内部 API：

```text
POST /v1/jobs
GET  /v1/jobs/{jobId}
```

提交体：

```json
{
  "requestId": "01K1CLEANING00000000000001",
  "profile": "markdown_cleaning_v1",
  "inputPath": "/shared/markdown-cleaning/task-id/input/source.md",
  "outputPath": "/shared/markdown-cleaning/task-id/datajuicer/result.md"
}
```

adapter 要求：

- 服务 URL、认证、profile 和超时均由服务端配置。
- 连接超时、单次响应超时和完整 job deadline 分开配置。
- POST 返回不确定时，使用相同 `requestId` 重试，依赖 Data-Juicer 幂等收敛。
- adapter 将外部状态映射为 `PROCESSING/SUCCEEDED/FAILED`，未知状态视为协议错误。
- 只接受输出路径等于提交时的规范化专属路径。
- 不向业务 API 透传外部 jobId、原始错误或内部 phase。

## 8. 结果元数据与校验

Data-Juicer 成功结果必须包含：

```json
{
  "outputPath": "/shared/markdown-cleaning/task-id/datajuicer/result.md",
  "outputSha256": "hex-sha256",
  "inputSha256": "hex-sha256",
  "profile": "markdown_cleaning_v1",
  "profileVersion": "1",
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
}
```

TextProcessor 必须验证：

- `inputSha256` 等于提交输入摘要；
- profile 与版本完全匹配；
- outputPath 严格属于本任务专属 staging；
- 文件存在、为普通文件、非空且不超过输出上限；
- 实际 SHA-256 等于 `outputSha256`；
- 输出可严格解码为 UTF-8，无 BOM，使用 LF 且文件末尾恰有一个换行；
- 围栏代码块仍闭合；
- 所有统计值为非负整数，字段集合固定；
- 输出不含 profile 定义禁止的未完成内部占位符。

TextProcessor 不重新执行敏感信息检测来证明全部敏感数据已清除；脱敏正确性由 profile 单元测试和真实样本验收负责。Worker 只验证跨服务契约、文件完整性和安全元数据。

## 9. 输出发布

- 处理前发现 `targetPath` 已存在时快速失败，但最终正确性依赖发布时的禁止覆盖操作。
- Data-Juicer 不能直接写业务 `targetPath`。
- Worker 将已验证结果流式复制到目标目录内的 job 专属 `.part` 文件。
- flush、关闭并计算 SHA-256 后，将 prepared 状态写入 PostgreSQL。
- 使用禁止覆盖的同文件系统原子发布；不同任务竞争同一目标时只能有一个成功。
- 发布成功后保存最终摘要和时间，再进入 `succeeded`。
- 失败清理只处理本任务 `.part` 文件，不能删除或覆盖已有目标。

## 10. 恢复与重试

Celery Beat 周期扫描：

- 超过入队窗口的 `pending`；
- 超过 dispatch 窗口的 `queued`；
- 租约过期的 `running`；
- 有 `externalJobId` 但 poll 丢失的任务；
- 已 prepared 或 published 但未进入终态的任务。

恢复规则：

- recover 只投递 execute/poll 或修复可证明的终态，不在 Beat 中读取正文、调用清洗算法或发布文件。
- 未确认提交结果时使用相同 `requestId=taskId` 重提一次并收敛到原 job。
- Data-Juicer 返回 job 不存在时，只允许一次幂等重提；仍不存在则失败为 `PROCESSOR_JOB_NOT_FOUND`。
- 最终文件摘要等于 prepared SHA-256 时恢复为成功；摘要不同则 `OUTPUT_CONFLICT`。
- 所有业务重试沿用同一 `taskId`、输入摘要、profile 和目标路径。
- 请求、输入格式、确定性 profile、非法结果与输出冲突不重试；短暂网络、数据库、broker 和未发布前存储错误有限重试。

## 11. 进度映射

TextProcessor 只公开以下阶段：

```text
validating_input: 0..10
submitting:       10..20
cleaning:         20..85
publishing:       85..99
completed:        100
```

- 外部 job percent 只能映射到 `cleaning` 区间。
- 持久化进度不得倒退。
- 失败保留最后一次已持久化阶段和 percent。
- poll 应按时间或增量阈值批量更新，不能造成无界数据库写入。

## 12. 配置

至少提供：

```text
MARKDOWN_CLEANING_STAGING_ROOT
MARKDOWN_CLEANING_INPUT_ROOTS
MARKDOWN_CLEANING_OUTPUT_ROOTS
MARKDOWN_CLEANING_ALLOWED_URL_HOSTS
MARKDOWN_CLEANING_INPUT_MAX_BYTES
MARKDOWN_CLEANING_OUTPUT_MAX_BYTES
MARKDOWN_CLEANING_DOWNLOAD_TIMEOUT_SECONDS
MARKDOWN_CLEANING_JOB_TIMEOUT_SECONDS
MARKDOWN_CLEANING_MAX_ATTEMPTS
MARKDOWN_CLEANING_LEASE_SECONDS
MARKDOWN_CLEANING_POLL_INTERVAL_SECONDS
MARKDOWN_CLEANING_RECOVERY_INTERVAL_SECONDS
MARKDOWN_CLEANING_RECOVERY_BATCH_SIZE
DATAJUICER_SERVICE_URL
DATAJUICER_MARKDOWN_CLEANING_PROFILE
```

生产环境中 `DATAJUICER_MARKDOWN_CLEANING_PROFILE` 必须固定为 `markdown_cleaning_v1`；业务请求不能覆盖。

## 13. 可观察性

日志至少包含 `request_id`、`task_id`、`caller_id`、`external_job_id`、阶段、attempt、耗时、字节数、安全统计和错误码。指标至少包括任务状态数量、阶段耗时、输入输出大小、重复段落删除数、各类脱敏计数、格式修正数、重试、租约过期、恢复、输出冲突和外部服务延迟。

日志、指标和 tracing 禁止包含输入/输出正文、敏感原值、匹配上下文、完整内部路径、凭据或外部原始堆栈。

## 14. 测试与验收

### 14.1 单元测试

- 消息 schema、终态幂等和租约抢占。
- 本地/远程输入优先级、流式限制和不降级规则。
- UTF-8、BOM、NUL、空文件、大小上限和未闭合代码围栏。
- staging 路径逃逸、符号链接和任务隔离。
- adapter 提交、状态映射、非法响应和超时分类。
- 结果路径、摘要、profile、统计 schema 和 Markdown 完整性校验。
- 处理前冲突、发布时竞争、`.part` 清理和摘要恢复。
- 进度单调映射与安全错误响应。

### 14.2 集成测试

- PostgreSQL + Redis/Celery + 本地文件的 execute/poll 链路。
- fake Data-Juicer HTTP service 的提交、多轮轮询、失败和协议异常。
- 重复消息、提交响应丢失、poll 丢失和 worker 中断恢复。
- 两任务竞争相同 `targetPath` 时只有一个成功。
- 发布完成但数据库未更新时通过摘要恢复。
- 日志与数据库不包含 Markdown 正文或敏感原值。

### 14.3 真实集成验收

独立真实测试启动 TextProcessor API/worker/Beat、PostgreSQL、Redis、Data-Juicer API/worker/Beat 和共享 staging，验证：

```text
业务 POST
→ TextProcessor execute
→ Data-Juicer markdown_cleaning_v1
→ TextProcessor poll
→ 原子发布 Markdown
→ GET succeeded
```

必须使用同时包含重复段落、五类敏感信息、Markdown 标题/列表/引用/表格和代码块的固定样本，并验证输出正文与安全统计。未真实启动两个服务及两个 worker 的测试不得声称端到端打通。
