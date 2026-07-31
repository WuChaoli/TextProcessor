# Data-Juicer 独立处理服务设计

## 1. 目标与范围

本文定义一个可被 TextProcessor 多个业务接口复用的独立 Data-Juicer 内部处理服务。首个受控 profile 为 `text_exact_minhash_v1`，用于对 TextProcessor 已下载、校验并规范化的文本数据执行精准去重、MinHash 近似去重、聚类展开和唯一代表选择。

服务通过异步 HTTP job 接口接收任务，使用独立的 FastAPI、Celery job、PostgreSQL 状态和 Redis queue。首版以 Data-Juicer v1.5.4 源码和 Python 3.11 虚拟环境运行；源码链路真实验证通过后，再将 wrapper、锁定的 Data-Juicer 源码和依赖打包为独立 Docker 镜像。

本文不定义 TextProcessor 的外部业务 API、业务 `groupId`、最终 `keep` JSON 发布或业务输入 URI 策略。这些内容由《全局文档去重异步任务接口设计》和《全局文档去重 TextProcessor Worker 设计》负责。

## 2. 服务边界

```text
TextProcessor Celery worker
    ↓ HTTP
Data-Juicer Service API
    ↓ PostgreSQL job + Redis broker
Data-Juicer Celery worker
    ↓
受控 profile executor
    ↓
共享 staging output
```

职责：

- TextProcessor 负责读取业务清单、下载业务文档、UTF-8 解码、基础文本规范化以及生成可信 `input.jsonl`。
- Data-Juicer Service 只读取 TextProcessor 指定的本地 staging 输入。
- Data-Juicer Service 根据服务端 profile 执行数据处理，不接受任意 recipe、operator 或运行参数。
- Data-Juicer Service 输出通用聚类结果，不感知 `fileId`、`fileStoragePath`、业务 `groupId` 或最终 `keep` 字段。
- TextProcessor 读取并校验聚类结果，完成业务映射和最终发布。

首版假定两个服务位于可信内部环境并可直接访问相同本地文件路径。不额外建设服务间身份认证、路径签名、文件上传下载或细粒度路径授权。

## 3. 代码与依赖布局

服务位于 TextProcessor 仓库，但作为独立 Python package 管理：

```text
services/
└── datajuicer_service/
    ├── pyproject.toml
    ├── uv.lock
    ├── datajuicer_service/
    │   ├── api/
    │   ├── core/
    │   ├── jobs/
    │   └── profiles/
    ├── migrations/
    ├── tests/
    ├── scripts/
    ├── vendor/
    │   └── data-juicer/
    └── Dockerfile  # 源码验证通过后增加
```

`Dockerfile` 在源码环境验证通过后增加。

Data-Juicer 使用 Git submodule 固定：

```text
repository: https://github.com/datajuicer/data-juicer.git
tag: v1.5.4
commit: 7061da6ad06287aa0305eda162429b34361a56a3
Python: 3.11
```

wrapper 不修改 submodule 内的上游文件。由于 Data-Juicer 源码根目录包含 `app.py`，wrapper 使用唯一 package 名 `datajuicer_service` 避免导入冲突；自定义 profile、adapter、HTTP、Celery 和数据库代码全部位于 `services/datajuicer_service/datajuicer_service/`。

源码环境使用独立 `.venv` 和 `uv.lock`。只安装首版文本去重及服务运行所需依赖，不安装 Data-Juicer 的 `all`、`dist`、`tools`、Ray、GPU 或多模态 extras。MinHash 自动计算 LSH bands/rows 需要显式安装 SciPy。

## 4. API 契约

首版只提供：

```http
POST /v1/jobs
GET  /v1/jobs/{jobId}
```

不提供：

- 任意函数或类反射调用；
- 任意 Data-Juicer YAML recipe；
- operator 搜索和动态组合；
- 结果文件下载；
- job 取消；
- 回调；
- MCP 接口。

### 4.1 创建 job

`POST /v1/jobs`

```json
{
  "requestId": "0198f000-0000-7000-8000-000000000001",
  "profile": "text_exact_minhash_v1",
  "inputPath": "/data/textprocessor-staging/task-id/input.jsonl",
  "outputPath": "/data/textprocessor-staging/task-id/datajuicer-result.jsonl"
}
```

字段规则：

- `requestId` 必填、非空，最大长度受 schema 限制。
- `profile` 必须命中服务端注册的 profile。
- `inputPath` 和 `outputPath` 必填、非空。
- 首版只处理可直接访问的本地路径。
- API 不接收文本正文、Data-Juicer operator 参数、进程数、阈值、凭据或环境变量。

首次创建并成功入队返回 `202 Accepted`：

```json
{
  "jobId": "0198f100-0000-7000-8000-000000000001",
  "requestId": "0198f000-0000-7000-8000-000000000001",
  "profile": "text_exact_minhash_v1",
  "status": "queued"
}
```

### 4.2 查询 job

`GET /v1/jobs/{jobId}`

执行中：

```json
{
  "jobId": "0198f100-0000-7000-8000-000000000001",
  "requestId": "0198f000-0000-7000-8000-000000000001",
  "profile": "text_exact_minhash_v1",
  "status": "running",
  "createdAt": "2026-07-31T10:00:00+08:00",
  "startedAt": "2026-07-31T10:00:02+08:00",
  "finishedAt": null,
  "progress": {
    "phase": "minhash_computing",
    "total": 100,
    "processed": 80,
    "percent": 55
  },
  "result": null,
  "error": null
}
```

成功：

```json
{
  "jobId": "0198f100-0000-7000-8000-000000000001",
  "requestId": "0198f000-0000-7000-8000-000000000001",
  "profile": "text_exact_minhash_v1",
  "status": "succeeded",
  "createdAt": "2026-07-31T10:00:00+08:00",
  "startedAt": "2026-07-31T10:00:02+08:00",
  "finishedAt": "2026-07-31T10:02:00+08:00",
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

失败：

```json
{
  "jobId": "0198f100-0000-7000-8000-000000000001",
  "requestId": "0198f000-0000-7000-8000-000000000001",
  "profile": "text_exact_minhash_v1",
  "status": "failed",
  "createdAt": "2026-07-31T10:00:00+08:00",
  "startedAt": "2026-07-31T10:00:02+08:00",
  "finishedAt": "2026-07-31T10:00:10+08:00",
  "progress": {
    "phase": "validating_input",
    "total": null,
    "processed": 0,
    "percent": 0
  },
  "result": null,
  "error": {
    "code": "INVALID_INPUT_DATASET",
    "message": "输入数据格式不正确"
  }
}
```

`result` 和 `error` 必须互斥。

## 5. 幂等

幂等键为 `requestId`：

- 相同 requestId 和相同 `profile + inputPath + outputPath` 返回原 job。
- 相同 requestId 和不同参数返回 `409 IDEMPOTENCY_CONFLICT`。
- 原 job 已失败或取消时仍返回原 job。
- 调用方需要重新执行时必须使用新的 requestId。
- 参数规范化后生成 `requestFingerprint`。
- PostgreSQL 对 `request_id` 建立唯一约束，不能只依赖先查询后插入。
- 并发重复 POST 只能创建一条 job 记录并投递一个 execute task。

## 6. 状态机与 Celery

状态机：

```text
pending -> queued -> running -> succeeded
                     |-------> failed
                     |-------> cancelled
```

首版不提供取消 API；`cancelled` 只作为预留终态。

### 6.1 `datajuicer.execute`

消息只携带：

```json
{
  "jobId": "0198f100-0000-7000-8000-000000000001",
  "taskType": "datajuicer_job",
  "schemaVersion": 1
}
```

执行流程：

1. 校验消息。
2. 从 PostgreSQL 加载完整 job。
3. 使用条件更新获得 job 执行权并写入租约。
4. 校验输入和输出前置条件。
5. 加载固定 profile。
6. 执行精准分组。
7. 执行 MinHash 计算和聚类。
8. 展开完整成员并选择唯一代表。
9. 写入、校验并原子发布结果。
10. 保存摘要并更新为 `succeeded`。

重复消息遇到终态 job 时安全结束；未获得执行权的 worker 不执行 profile。

### 6.2 `datajuicer.recover`

Celery Beat 周期扫描：

- `pending` 且超过入队窗口的 job；
- `queued` 且超过 dispatch 窗口的 job；
- `running` 且租约过期的 job；
- 结果已发布但数据库尚未成功落终态的 job。

recover 只重新投递 execute 或修复可证明的最终状态，不在 Beat 中运行去重算法。

### 6.3 Celery 运行配置

首版默认：

```text
worker_concurrency = 1
worker_prefetch_multiplier = 1
task_acks_late = true
```

配置项：

```text
DATAJUICER_WORKER_CONCURRENCY
DATAJUICER_PROFILE_TEXT_EXACT_MINHASH_NP
DATAJUICER_JOB_TIMEOUT_SECONDS
DATAJUICER_MAX_ATTEMPTS
DATAJUICER_RECOVERY_INTERVAL_SECONDS
```

单 worker 默认同时执行一个 CPU/内存密集 job。单 job 内部并行度由 profile 的服务端配置控制。后续优先通过增加 worker 实例扩容。

## 7. PostgreSQL 与 Redis

Data-Juicer Service 可以复用 TextProcessor 已有 PostgreSQL 和 Redis 物理实例，但必须逻辑隔离。

PostgreSQL：

- 使用独立 database 和独立 database user；
- Data-Juicer user 只能访问 Data-Juicer job 表；
- TextProcessor 与 Data-Juicer 不直接查询对方数据库；
- 服务间交互只通过 HTTP。

Redis：

- 使用 `datajuicer.*` 专属 Celery queue；
- task name 使用 `datajuicer.` namespace；
- result/backend keys 使用 `datajuicer:` prefix；
- 支持 logical DB 时可以使用独立 DB number；
- Redis Cluster 使用 DB 0 时必须依赖 queue 和 key prefix 隔离；
- 任一服务不得执行清空整个 Redis 实例的操作。

共享物理实例意味着 Redis 或 PostgreSQL 故障可能同时影响两个服务，部署文档必须记录这一共同故障域。

### 7.1 Job 数据模型

至少包含：

```text
job_id
request_id
request_fingerprint
profile
input_path
output_path
status
processing_phase
progress_total
progress_processed
progress_percent
attempt_count
max_attempts
lease_token
lease_expires_at
processing_deadline
input_sha256
input_count
prepared_output_sha256
staging_output_path
output_sha256
published_at
error_code
error_message
created_at
queued_at
started_at
finished_at
updated_at
```

PostgreSQL 不保存输入文本或结果 JSONL 正文。

## 8. Profile 注册与配置

Profile 是服务端代码注册的不可变处理契约：

```python
class ProfileExecutor(Protocol):
    name: str
    version: str

    def execute(
        self,
        input_path: Path,
        output_path: Path,
        progress: ProgressReporter,
    ) -> ProfileResult: ...
```

注册表只暴露允许的 profile。首版唯一 profile：

```text
text_exact_minhash_v1
```

调用方不能修改 profile 参数。任何行为变化必须新增 profile 名称并补齐兼容性、质量和性能验证，不得原地改变 v1。

## 9. 输入数据集契约

输入是 UTF-8 JSONL：

```jsonl
{"uid":0,"text":"第一份文档内容"}
{"uid":1,"text":"第二份文档内容"}
```

校验：

- 文件存在且可读；
- 每一行都是 JSON object；
- 只接受 `uid` 和 `text` 字段；
- `uid` 是非负整数；
- uid 唯一；
- `text` 是字符串；
- 至少一条记录；
- 记录数、文件大小和累计文本字符数不超过服务端配置；
- 解析完成后的 uid 集合和输入摘要写入 job 元数据；
- 任一错误使整个 job 失败。

TextProcessor 已完成业务字段和格式校验，但 Data-Juicer Service 仍需校验自身内部数据契约，避免算法收到不合法结构。

## 10. `text_exact_minhash_v1`

### 10.1 精准分组

对每条输入文本：

1. 对整个字符串执行 `strip()`。
2. 保留大小写。
3. 保留正文内部空白、数字、标点和标记。
4. 对规范化文本计算稳定摘要。
5. 摘要相同且规范化文本相同的记录进入同一精准组。

摘要只用于分桶，最终相等判断仍比较规范化文本，避免摘要碰撞导致误分组。

精准组保留完整 uid 集合。组大小大于 1 时记录 `method=exact`。每个精准组选择一个临时代表参与 MinHash；选择规则与最终代表一致。

### 10.2 MinHash 参数

固定使用：

```yaml
tokenization: character
window_size: 5
lowercase: true
ignore_pattern: null
num_permutations: 256
jaccard_threshold: 0.7
num_bands: null
num_rows_per_band: null
```

bands 和 rows 使用 Data-Juicer v1.5.4 的最优参数计算逻辑。

### 10.3 Data-Juicer 复用边界

首版不直接调用原生 deduplicator 的最终 `run/process` 输出，因为它会过滤非代表样本。

自定义 MinHash stage：

- 使用 Data-Juicer v1.5.4 的 tokenization、shingling、MinHash signature、LSH 参数和并查集相关实现；
- 保留完整 cluster membership；
- 不调用会删除非代表样本的最终 dataset filter；
- 不修改上游源码；
- 通过独立 adapter 封装对上游内部 API 的使用；
- 通过版本兼容性测试锁定类、方法、参数和结果行为。

精准分组使用 wrapper 自身的稳定摘要和完整成员映射。该部分逻辑简单且需要返回全部成员，不强行调用会过滤数据的原生精准 deduplicator。

### 10.4 两阶段合并

```text
全部 uid
    ↓
精准组 + 独立文档
    ↓
每个精准组的临时代表
    ↓
MinHash cluster
    ↓
将 MinHash cluster 展开回精准组全部成员
    ↓
最终完整 cluster
```

规则：

- 精准重复关系不会因 MinHash 代表抽样而丢失。
- MinHash 只处理精准去重后的临时代表。
- MinHash 并查集形成的传递闭包作为最终近似重复簇。
- 最终组大小大于 1 才分配非空 clusterId。
- 精准组被 MinHash 与其他成员合并时，整个最终组使用 `method=exact_minhash`。
- 只由 MinHash 形成的组使用 `method=minhash`。
- 未参与任何重复组的文档使用 `clusterId=null`、`method=null`。

### 10.5 唯一代表

每个最终重复组严格选择一个代表：

```text
normalized_text_length DESC
uid ASC
```

- `normalized_text_length` 是精准匹配规范化后的字符长度。
- 优先选择内容更长的记录。
- 长度相同时选择 uid 更小的记录。
- 独立文档固定 `representative=true`。
- 相同输入重复执行必须得到相同代表。

### 10.6 内部 clusterId

内部 clusterId 使用确定性 UUIDv5：

```text
namespace = 服务代码中固定的 UUID namespace
name = requestId + 分隔符 + 按升序连接的最终成员 uid
```

该 ID 只用于结果文件内部关联和诊断，不作为 TextProcessor 业务 groupId。

## 11. 输出契约

输出是 UTF-8 JSONL，每个输入 uid 一行：

```jsonl
{"uid":0,"clusterId":"cluster-1","representative":true,"method":"exact_minhash"}
{"uid":1,"clusterId":"cluster-1","representative":false,"method":"exact_minhash"}
{"uid":2,"clusterId":null,"representative":true,"method":null}
```

字段固定为：

```text
uid
clusterId
representative
method
```

method 允许值：

```text
exact
minhash
exact_minhash
null
```

输出不包含文本、业务 ID、业务路径、MinHash signature、摘要或 Data-Juicer 内部字段。

服务在发布前自校验：

- 输出记录数等于输入记录数；
- uid 集合完全一致；
- uid 无重复、缺失或未知；
- `clusterId=null` 时 `representative=true` 且 `method=null`；
- 非空 cluster 至少两个成员；
- 非空 cluster 严格一个代表；
- 同一 cluster 的 method 一致；
- 输出重新解析后仍满足 schema；
- 计算并保存 SHA-256。

## 12. 输出发布与恢复

处理前：

- `outputPath` 已存在时以 `OUTPUT_CONFLICT` 失败；
- 不覆盖、删除或移动已有输出；
- 使用 job 专属 `.part` 文件。

发布流程：

1. 写入 `outputPath` 同目录的 job 专属临时文件。
2. flush、关闭并计算 SHA-256。
3. 重新解析并执行全部输出不变量校验。
4. 将 prepared SHA-256 和临时路径写入 PostgreSQL。
5. 以禁止覆盖方式原子发布到 `outputPath`。
6. 保存 `outputSha256` 和发布时间。
7. job 转为 `succeeded`。

崩溃恢复：

- 结果已发布但数据库未落终态时，比较目标摘要和 prepared SHA-256；
- 相同则恢复为 `succeeded`；
- 不同则以 `OUTPUT_CONFLICT` 失败；
- 只清理当前 job 的临时文件；
- 不清理由 TextProcessor 管理的输入、mapping 或任务目录。

## 13. 进度

内部阶段：

```text
validating_input
exact_grouping
minhash_computing
minhash_clustering
expanding_clusters
writing_result
completed
```

进度结构：

```text
phase
total
processed
percent
```

要求：

- 未解析出记录总数前 `total=null`；
- 完成输入校验后 `total` 固定为输入记录数；
- `processed` 表示当前阶段可计数的已处理记录数；
- `percent` 是整体估算值，不承诺与实际耗时线性对应；
- percent 在一次 job 生命周期内不得倒退；
- 成功终态固定为 `completed`、`processed=total`、`percent=100`；
- 失败终态保留最后一次持久化进度。

进度更新应分批持久化，禁止每处理一条文档都写 PostgreSQL。

## 14. 错误与重试

### 14.1 请求与幂等

- `INVALID_REQUEST`
- `PROFILE_NOT_SUPPORTED`
- `IDEMPOTENCY_CONFLICT`
- `JOB_NOT_FOUND`
- `QUEUE_SUBMISSION_FAILED`

### 14.2 输入

- `INPUT_NOT_FOUND`
- `INPUT_READ_FAILED`
- `INPUT_TOO_LARGE`
- `INVALID_INPUT_DATASET`
- `EMPTY_INPUT_DATASET`
- `DUPLICATE_UID`

输入 schema、空数据、重复 uid 和确定性读取错误不重试。

### 14.3 处理

- `EXACT_GROUPING_FAILED`
- `MINHASH_COMPUTE_FAILED`
- `MINHASH_CLUSTERING_FAILED`
- `CLUSTER_EXPANSION_FAILED`
- `PROFILE_EXECUTION_FAILED`
- `JOB_TIMEOUT`

确定性 profile 和数据错误不重试。worker 进程异常退出、临时 PostgreSQL/Redis 故障及可证明的短暂系统错误可以有限重试。

### 14.4 输出

- `INVALID_PROFILE_OUTPUT`
- `OUTPUT_CONFLICT`
- `OUTPUT_WRITE_FAILED`
- `OUTPUT_INTEGRITY_FAILED`

输出冲突和确定性 schema/不变量错误不重试。未发布最终文件前的短暂文件系统错误可以有限重试。

### 14.5 系统

- `DATABASE_ERROR`
- `INTERNAL_ERROR`

所有重试：

- 沿用同一 jobId；
- 使用有限次数和退避；
- 依赖租约避免并发重复执行；
- 达到最大次数后进入 failed；
- 不改变 profile 或参数；
- 不删除已有目标文件。

对外错误只返回稳定 code 和安全摘要，不返回输入正文、结果正文、内部堆栈或 Data-Juicer 原始异常。

## 15. 源码运行

首版先通过源码启动，不构建 Docker。

运行单元：

```text
datajuicer-api
datajuicer-worker
datajuicer-beat
```

三者使用同一 source checkout 和 `.venv`，通过不同命令启动。生产式源码验收必须使用非 editable 的锁定环境或证明 editable 指向固定 submodule commit，不能意外跟随 `main`。

运行前检查：

- Git submodule commit 精确匹配；
- Python 3.11；
- `uv sync --locked` 成功；
- Data-Juicer 版本输出为 1.5.4；
- PostgreSQL migration 已应用；
- Celery API、worker 和 Beat 使用 `datajuicer.*` queue；
- staging 路径对 API/worker/TextProcessor worker 可见；
- profile 注册表只包含允许的 profile。

## 16. Docker 化阶段

源码功能、异常、恢复和真实集成验证通过后，再增加独立镜像：

```text
datajuicer-service:0.1.0
```

镜像包含：

- wrapper；
- 固定 commit 的 Data-Juicer v1.5.4 源码；
- Python 3.11；
- 锁定依赖；
- profile 和 migration；
- API、worker、Beat 启动入口。

同一镜像以不同 command 启动三个容器：

```text
datajuicer-api
datajuicer-worker
datajuicer-beat
```

PostgreSQL、Redis 和 staging 不打入镜像。

不直接采用全功能官方镜像。构建只安装首版需要的文本、科学计算和服务依赖，不安装 Ray、GPU、多模态或全部 tools extras。

Docker 化完成后必须重新执行运行环境验收，源码环境通过不能替代镜像环境通过。

## 17. 可观察性

日志至少包含：

```text
request_id
job_id
profile
celery_task_name
processing_phase
attempt_count
input_count
error_code
duration_ms
```

日志不包含：

- `text` 正文；
- input/output JSONL 正文；
- PostgreSQL/Redis 凭据；
- 内部堆栈的对外响应；
- MinHash signature。

指标至少包括：

- job 创建、排队、运行、成功、失败和恢复数量；
- 各 profile 和阶段耗时；
- 输入记录数与字节数分布；
- 精准重复组数；
- MinHash 重复组数；
- 最终重复组和独立文档数；
- 重试次数、租约过期和恢复次数；
- 输出冲突和非法输出次数；
- worker CPU、内存和 job 排队时间。

## 18. 测试与验收

### 18.1 单元测试

- POST schema、profile allowlist 和 GET response mapping。
- requestId 幂等、一致命中、参数冲突和并发唯一约束。
- job 状态机、租约和非法转换。
- JSONL 非法行、未知字段、空数据、重复 uid 和 text 类型错误。
- 精准匹配的 strip、大小写、内部空白、数字和标点行为。
- 摘要碰撞时仍通过原文比较避免误分组。
- Data-Juicer v1.5.4 tokenization、MinHash 参数和 signature 兼容性。
- 精准组临时代表、MinHash 聚类和完整成员展开。
- `exact`、`minhash`、`exact_minhash` 和独立文档 method。
- 最长文本优先和 uid 兜底的唯一代表。
- 内部 clusterId 确定性。
- 输出完整 uid 集合和所有聚类不变量。
- 输出冲突、临时文件、摘要和原子发布。
- 错误分类与是否重试。

### 18.2 集成测试

- FastAPI POST → PostgreSQL → Redis/Celery → worker → output → GET。
- 并发相同 requestId 只创建一个 job。
- 入队失败状态和 HTTP 503。
- 重复 Celery 消息不重复执行或覆盖输出。
- worker 在精准分组、MinHash、展开和发布阶段中断后的恢复。
- 结果已发布但数据库未更新时通过摘要恢复。
- 不同 job 竞争同一 outputPath 时只有一个成功。
- Celery Beat 只恢复过期 job。
- PostgreSQL database/user 与 Redis queue/key namespace 隔离。

### 18.3 Data-Juicer 兼容性测试

对锁定 v1.5.4 验证：

- `data_juicer.__version__`；
- 自定义 adapter 使用的 import path、类、方法和参数；
- character tokenization；
- 256 permutations；
- 自动 bands/rows；
- 固定样例的 MinHash cluster；
- 上游原生 deduplicator 会过滤样本，而自定义 stage 保留完整成员；
- submodule commit 不匹配时启动或测试失败。

### 18.4 真实数据测试

至少覆盖：

- 完全相同文本；
- BOM、换行和首尾空白差异；
- 大小写不同但精准不重复、MinHash 可近似；
- 中文少量增删改；
- 一份长文与其删节版；
- 精准组再与第三份文档 MinHash 合并；
- JSON 原始文本；
- 无重复批次；
- 全部重复批次；
- 接近阈值的正负样本；
- 资源上限附近的批次。

需要保留每个样例的预期 cluster 和 representative，不能只断言运行成功。

### 18.5 源码端到端验收

真实启动：

- TextProcessor API；
- TextProcessor worker 和 Beat；
- Data-Juicer API；
- Data-Juicer worker 和 Beat；
- PostgreSQL；
- Redis；
- 共享 staging。

验证完整链路：

```text
业务 POST
→ TextProcessor task
→ Data-Juicer job
→ 精准 + MinHash
→ 聚类结果
→ TextProcessor 业务 JSON
→ GET succeeded
```

同时验证两个 worker 重启、提交不确定、轮询恢复和最终摘要。

### 18.6 Docker 验收

源码验收通过后构建镜像，再重复：

- API/worker/Beat 健康；
- migration；
- queue 隔离；
- staging mount；
- TextProcessor 跨服务端到端；
- worker 重启恢复；
- 镜像版本、Data-Juicer commit 和依赖锁定证据。

未实际启动对应环境的验证不得声称通过。
