# 结构化提取 Worker 与解析服务设计

## 1. 目标与范围

本文定义结构化提取异步单条任务的 worker 处理逻辑、processor 路由、MinerU/Docling HTTP adapter、非阻塞轮询、输出规范化、可靠性、容量控制，以及 Docling 独立部署和验证要求。

本文建立在《结构化提取异步单条任务接口设计》之上。接口仍通过 POST 返回 `taskId`，调用方通过 GET 查询状态和结果路径；不回调业务系统，不返回 Markdown 正文。

首版范围：

- 可安全解码的文本直接按原结构保存为 UTF-8 Markdown 文件。
- PDF、图片和复杂视觉文档通过 MinerU HTTP 服务提取。
- Office、HTML、EPUB 等文档按确定性路由使用 MinerU 或 Docling HTTP 服务。
- 最终只发布一个 Markdown 文件，不发布图片或其他附件。
- MinerU 已有独立 HTTP 服务；Docling 必须作为首版交付的一部分完成独立部署和真实验证。

## 2. 组件边界

```text
Celery Worker
├── InputResolver
├── FormatDetector
├── OfficeDocumentInspector
├── ProcessorRouter
├── PlainTextPassThroughProcessor
├── MinerUHttpAdapter
├── DoclingHttpAdapter
├── MarkdownNormalizer
├── OutputValidator
└── AtomicPublisher
```

- `InputResolver` 根据已确认的输入优先级，将本地文件或受控远程输入复制到任务 staging。
- `FormatDetector` 结合扩展名、文件头、MIME 特征、二进制特征和解码结果识别格式。
- `OfficeDocumentInspector` 对 DOCX 做确定性结构预检。
- `ProcessorRouter` 根据格式、预检结果和 production allowlist 选择唯一 processor。
- `PlainTextPassThroughProcessor` 负责可解码文本的 UTF-8 输出。
- 两个 HTTP adapter 隔离外部服务协议差异。
- `MarkdownNormalizer` 只处理外部 processor 生成的 Markdown。
- `AtomicPublisher` 负责禁止覆盖的最终发布和崩溃恢复。

API route、Celery task 和 adapter 均不包含文档解析算法。Processor 不直接更新任务状态或写最终 `targetPath`。

## 3. Staging 与输入解析

每个任务使用服务端生成的独占目录：

```text
{stagingRoot}/{taskId}/
├── source/
│   └── original.<ext>
├── processor/
│   └── raw-result.json 或 raw-result.md
└── output/
    └── result.md
```

规则：

- 请求不能指定 staging 路径。
- `fileStoragePath` 与 `fileOssUrl` 同时存在时选择本地路径；选定来源失败时不降级。
- 远程输入由 TextProcessor 统一下载，MinerU/Docling 不直接访问业务 URL。
- 输入复制和 HTTP 上传必须流式处理，并执行大小、超时和摘要限制。
- staging 输入保存 SHA-256、大小、检测格式和实际 MIME 特征。
- 重试可复用摘要匹配的 staging 输入和已下载结果。
- 成功后清理 staging；失败后的有限保留期由服务端配置控制。
- 定期清理只能删除数据库状态和保留期均允许删除的任务目录。

## 4. 格式检测与路由

### 4.1 检测原则

路由依据为：

```text
扩展名 + 文件头/MIME + 二进制特征 + 解码结果 + Office 结构预检
```

- 不只相信扩展名、请求 Content-Type 或远端响应 Content-Type。
- 扩展名与文件特征明显冲突时失败，不猜测 processor。
- `.txt` 实际为 PDF、压缩包或可执行文件时不按文本处理。
- Office Open XML 虽为 ZIP，需结合扩展名和包内结构识别。
- 不自动解压任意压缩包。

未知扩展名只有同时满足以下条件才按文本处理：

- 没有已知二进制签名；
- 不包含 NUL 或受限控制字符；
- 可以使用允许编码完整解码；
- 文件大小在文本限制内。

### 4.2 固定路由

| 格式 | 首版 processor |
|---|---|
| PDF、扫描图片、普通图片 | MinerU |
| PPT、PPTX | MinerU |
| DOC | MinerU |
| DOCX | 经结构预检后选择 Docling 或 MinerU |
| XLS、XLSX | Docling |
| HTML、EPUB 及已验证 Docling 格式 | Docling |
| TXT、JSON、XML、YAML、CSV、TSV、Markdown、日志、源代码及可靠识别的未知文本 | 文本直通 |

首版不支持 `.wps`、`.et`、`.dps` 和 OFD。没有通过真实 smoke 的格式不进入 production allowlist，返回 `UNSUPPORTED_INPUT_FORMAT`。

Router 选定 processor 后不在执行失败时跨引擎降级。

### 4.3 DOCX 结构预检

DOCX 检查至少统计：

- `word/media/` 图片数量及图片主导倾向；
- `<w:drawing>`、`<w:pict>` 和 VML；
- `<wp:anchor>` 浮动对象；
- `<w:txbxContent>` 文本框；
- `<w:cols>` 分栏；
- SmartArt、chart、diagram；
- OLE 和其他嵌入对象；
- 文字量与图片量比例；
- 扫描页可能性。

检查结果包含确定字段和理由：

```text
format
textCharacterCount
imageCount
anchoredObjectCount
textBoxCount
columnSectionCount
chartCount
embeddedObjectCount
scannedPageLikelihood
visualComplexityScore
reasons
```

阈值由服务端配置 profile 管理。使用真实 Word 样本建立人工标注路由验证集，校准“Docling 足够”与“需要 MinerU”的规则，不凭经验直接固化生产阈值。

PPT/PPTX 天然以视觉布局为主，固定走 MinerU。Excel 以单元格、公式和工作表结构为核心，首版固定走 Docling；图片型工作簿不静默转 OCR。

## 5. 文本直通

文本直通处理流程：

```text
读取 staging 输入
→ 使用受控编码策略完整解码为 str
→ 保留原正文和结构
→ UTF-8 无 BOM 写入 output/result.md
→ 校验并发布
```

- 不 parse 后重新序列化 JSON、XML、YAML 或 CSV。
- 不添加标题、列表、表格、代码围栏或其他 Markdown 标记。
- 不改变缩进和字段顺序。
- 保留原换行形式。
- 解码失败时明确失败，不使用替换字符吞掉损坏内容。
- `.md` 只是目标文件后缀，不要求输入内容重写为 Markdown 语法。

## 6. 外部 Processor Adapter

统一协议：

```python
class ExternalProcessorAdapter(Protocol):
    def submit(
        self,
        source: Path,
        context: ProcessingContext,
    ) -> ExternalTaskSubmission: ...

    def get_status(
        self,
        external_task_id: str,
    ) -> ExternalTaskStatus: ...

    def fetch_result(
        self,
        external_task_id: str,
        destination: Path,
    ) -> ProcessorArtifact: ...
```

统一外部状态：

```text
PROCESSING
SUCCEEDED
FAILED
```

Adapter 负责：

- 提交、状态和结果协议映射；
- 校验原始响应；
- 提取 processor 名称和版本；
- 将错误归一化并脱敏；
- 将结果写入 staging。

Adapter 不更新 TextProcessor 任务状态，不写最终输出，不接受请求动态指定服务 URL、认证或解析参数。

### 6.1 MinerU 协议

现有服务地址由配置提供。已验证协议为：

```text
GET  /health
POST /tasks
GET  /tasks/{externalTaskId}
GET  /tasks/{externalTaskId}/result
```

提交使用 multipart 字段 `files`，成功返回 HTTP 202 和 `task_id`。

状态映射：

```text
queued | pending | processing | running → PROCESSING
completed                             → SUCCEEDED
failed                                → FAILED
其他值                                 → 外部协议错误
```

结果响应直接包含 `md_content`，无需下载结果 URL。单文件提交时要求 `results` 恰有一项并取其唯一结果，不依赖结果键严格等于原始文件 stem。

已测试参数作为默认 profile，但不硬编码。至少包含：

```text
backend
parse_method
lang_list
formula_enable
table_enable
return_md
return_middle_json
return_content_list
return_images
response_format_zip
start_page_id
end_page_id
effort
```

首版配置约束：

- `return_md=true`
- `return_images=false`
- `response_format_zip=false`

### 6.2 Docling 协议

采用 `docling-serve` v1 异步 API：

```text
POST /v1/convert/file/async
GET  /v1/status/poll/{task_id}
GET  /v1/result/{task_id}
```

状态映射：

```text
pending | started → PROCESSING
success           → SUCCEEDED
failure           → FAILED
其他值             → 外部协议错误
```

默认 profile 以 Markdown 为输出，图片模式为 placeholder，Docling 首版路由不默认启用 OCR。multipart 字段、结果字段和错误 schema 必须依据实际部署实例的 OpenAPI 与 smoke 结果固定，不能仅凭在线文档写死。

## 7. Processor Profile 配置

MinerU 和 Docling 的服务地址、认证、超时及解析 profile 均由服务端配置提供：

- 启动时加载和校验；
- 修改后通过滚动重启 worker 生效；
- 不支持运行时热更新；
- API 请求不能覆盖解析参数；
- Adapter 只接收已验证的类型化 profile；
- 提交时保存 `profileName` 和规范化配置 SHA-256；
- 重试沿用首次提交时的有效配置；
- 认证 secret 不进入 profile 摘要、响应或普通日志。

任务保存 processor 名称、版本、profile 名称、profile 摘要、检测格式和路由理由。

## 8. Celery 编排

首版包含三个 Celery task：

```text
submit_extraction_task
poll_extraction_task
recover_stalled_extraction_tasks
```

### 8.1 submit_extraction_task

```text
读取任务
→ 在 queued 状态下获取短 dispatch 租约
→ 检查 targetPath 冲突
→ 解析并复制输入到 staging
→ 计算摘要并检测格式
→ 选择 processor
```

文本直通分支从 `queued` 原子转为 `running`，在该 task 内直接生成、校验、发布并完成。

外部 processor 分支：

```text
获取 processor slot
→ queued 原子转为 running
→ multipart 上传 staging 输入
→ 保存 externalTaskId、processor、profile 和处理截止时间
→ 保存 nextPollAt
→ apply_async 首次 poll
→ 当前执行结束
```

没有可用 processor slot 时，保存内部 `waiting_capacity`，释放 dispatch 租约并保持对外 `queued`，再延迟调度 submit task。不得先转为 `running` 再回退到 `queued`。

### 8.2 poll_extraction_task

每次执行只查询外部状态一次，不在 worker 内 `sleep`：

```text
读取任务
→ 抢占短 poll 租约
→ 校验总截止时间
→ adapter.get_status()
```

分支：

- `PROCESSING`：保存 `nextPollAt`，使用 `apply_async(countdown=N)` 安排下一次查询并结束。
- `FAILED`：记录脱敏错误、释放或隔离 slot、按策略清理 staging 并失败。
- `SUCCEEDED`：在同一个 Celery task 内执行结果获取、Markdown 规范化、校验、发布和状态更新。

正常“仍在处理”不是异常，不使用 Celery `retry`。Celery `retry` 只处理查询超时、临时网络错误等真实瞬时错误。

收尾逻辑拆为可独立测试的普通函数，不另建 `finalize_extraction_task`。若后续真实证据表明结果下载和处理明显变重，再提升为独立 task。

### 8.3 recover_stalled_extraction_tasks

由 Celery Beat 周期触发：

- 补发 `queued` 但提交消息丢失的任务；
- 补发 `running`、`nextPollAt` 已过且 poll 租约失效的任务；
- 对账外部孤儿任务和隔离 slot；
- 不恢复超过最大尝试次数、总期限或处于终态的任务。

Celery 延迟消息只是唤醒 worker 的提醒。任务阶段、外部 ID、`nextPollAt` 和截止时间均以 PostgreSQL 为权威。

## 9. 非阻塞轮询与恢复

外部任务提交成功后保存：

```text
externalTaskId
processorName
processorVersion
processingPhase
nextPollAt
processingDeadline
pollLeaseExpiresAt
```

内部阶段至少包括：

```text
staging
waiting_capacity
submitting
submitted
polling
downloading
normalizing
publishing
```

这些阶段不扩展对外六态；等待 processor 容量时对外仍为 `queued`，外部处理中为 `running`。

重复 poll 消息通过 PostgreSQL 条件更新和短租约保证同一时刻只有一个执行者。Worker 重启后使用已保存的 `externalTaskId` 继续查询，不重复上传。

## 10. Markdown 规范化

外部 processor 结果通过语法感知的 `MarkdownNormalizer`：

- 内联图片有 alt 时替换为普通文本，无 alt 时删除；
- 引用式图片执行同样规则；
- 删除仅供已移除图片使用的 link definition；
- HTML `<img>` 有 alt 时保留文本，否则删除；
- 保留 MinerU/Docling 已识别的表格、图注、OCR 文字和图表说明；
- 保留普通链接、标题、表格、公式、代码块和段落；
- 最终确认不存在 Markdown 图片节点、HTML `<img>` 和 `data:image/...`。

不使用纯正则作为唯一清理实现。使用真实 MinerU/Docling 结果建立 golden tests，防止破坏表格、公式和代码块。

文本直通 processor 不经过 Markdown 重写。

## 11. 结果校验与原子发布

发布前至少校验：

- 输出非空；
- UTF-8 无 BOM；
- 大小不超过输出限制；
- 外部结果不包含图片引用；
- 输出 SHA-256 已计算；
- `targetPath` 位于 allowlist；
- 最终目标不存在。

结果写入同目录任务独占临时文件，再通过禁止覆盖的原子操作发布。处理前检查和发布时检查必须同时存在。

为恢复“文件已发布、数据库尚未更新”的崩溃窗口，发布前保存输出摘要和阶段。重试时：

- 目标文件摘要与本任务预发布摘要一致：恢复为 `succeeded`；
- 摘要不一致：`OUTPUT_CONFLICT`。

## 12. 重试与不确定提交

安全重试：

- 短暂本地 I/O 错误；
- 受控远程输入连接失败、超时、502/503；
- 外部状态查询连接失败、超时、502/503；
- 已完成结果获取的瞬时错误；
- 临时 staging 存储错误。

不重试：

- 输入越界、不存在、过大或格式不支持；
- 外部服务明确返回解析失败；
- Markdown 确定性校验失败；
- 输出冲突；
- profile 或协议错误。

外部提交存在响应不确定窗口。只有能够确认请求体尚未发送，或外部协议明确保证未创建任务时才安全重试。上传后发生 read timeout、连接断开或无法判断是否已创建任务时：

```text
status = failed
error.code = PROCESSOR_SUBMISSION_UNCERTAIN
```

不自动重新提交。调用方重新处理必须使用新的 `sessionId`。未来外部服务支持客户端幂等键后，可传递 TextProcessor `taskId` 并放宽该规则。

所有重试次数、退避、HTTP 超时、轮询间隔和总处理期限均配置化，不硬编码。

## 13. Processor 容量与背压

每个外部 processor 配置：

```text
maxInFlightTasks
submitRateLimit
```

- PostgreSQL 保存长期 in-flight slot、所属 `taskId`、获取时间和租约。
- Redis/Celery 负责短周期提交速率和消息调度。
- 文本直通不占 MinerU/Docling slot。
- 获取不到 slot 时任务进入内部 `waiting_capacity`，延迟重新调度。
- 外部任务终态后释放 slot。
- 每次 poll 刷新 slot 租约。
- 同一 task 与 processor 只能持有一个 slot，数据库约束处理并发抢占。
- 外部 `429` 按 `Retry-After` 或配置退避，不立即循环重试。

PostgreSQL slot 与任务状态可在同一事务中更新，避免 Redis 重启、过期或淘汰造成长期容量记录与权威任务状态不一致。

具体容量只能依据真实服务压测和资源监控确定。

## 14. 超时外部任务与 Slot 隔离

TextProcessor 达到处理期限后立即对外失败：

```text
error.code = PROCESSING_TIMEOUT
```

随后：

- Adapter 支持取消时执行 best-effort cancel；
- 不支持取消或取消结果不确定时，将 processor slot 标记为 `quarantined`；
- 后台对账继续查询外部任务；
- 外部任务终态或不存在时释放 slot；
- 超过配置的隔离上限时释放 slot并产生告警，记录外部状态未知。

业务任务失败状态与外部容量清理分离，避免任务长期显示 `running`，也避免立即低估真实外部负载。

## 15. GET 成功结果扩展

成功结果除原有业务字段外，增加非敏感处理元数据：

```json
{
  "result": {
    "fileStoragePath": "/data/txt/1.txt",
    "fileOssUrl": "http://ww.1.txt",
    "targetPath": "/data/txt2md/1.md",
    "processor": {
      "name": "mineru",
      "version": "1.2.3",
      "profile": "default",
      "profileSha256": "..."
    },
    "routing": {
      "detectedFormat": "docx",
      "reason": [
        "image_dominant_document",
        "anchored_objects=12"
      ]
    },
    "inputSha256": "...",
    "outputSha256": "..."
  }
}
```

不返回服务 URL、token、完整 profile、内部 staging 路径或 Markdown 正文。

## 16. Docling 独立部署

### 16.1 拓扑

```text
TextProcessor Worker
        │ HTTP + API key
        ▼
docling-serve API
        │
        ▼
Docling 专用 Redis
        │
        ▼
docling-serve rq-worker
```

要求：

- 使用官方 `docling-serve` 镜像并固定明确版本和 digest，不使用 `latest`；
- API 与 RQ worker 使用相同版本、模型和 profile；
- Docling Redis 与 TextProcessor Celery broker 隔离；
- 生产不向宿主机公网暴露 Docling，只允许内部网络访问；
- 启用 API key，TextProcessor 通过 secret 注入 `X-API-Key`；
- 生产关闭 Docling UI；
- 禁止 Docling 主动访问任意远程 URL，只接受 TextProcessor staging 文件上传；
- 配置最大文件大小、页数、单任务超时、队列容量和 RQ worker 数量；
- 模型和运行资产使用受控 volume/cache，避免生产启动临时下载不确定版本；
- Redis 配置认证、容量上限和符合恢复目标的持久化；
- API、RQ worker 和 Redis 均配置健康检查、restart policy 和资源限制。

Docling 首版主要处理 Office、HTML、EPUB 等，初始可使用 CPU 镜像；是否使用 GPU 由真实资源基线决定。

### 16.2 契约固定

部署后读取真实实例 `/docs` 或 OpenAPI，固定：

- multipart 文件字段和 profile 字段；
- API key header；
- 提交 task ID；
- 状态枚举；
- 成功结果 Markdown 字段；
- 失败响应；
- 任务不存在、过期和结果清理行为；
- API 与 RQ worker 版本兼容性。

Adapter 契约测试以固定部署版本的真实 schema 和响应为准。

## 17. 测试与验收

### 17.1 单元测试

- 文本直通只改变编码，不改变正文结构；
- 格式检测、二进制伪装和未知文本；
- OOXML 检测和 DOCX 路由理由；
- 固定格式路由与 production allowlist；
- 两个 adapter 的状态和错误归一化；
- Markdown 图片清理及结构保留；
- 状态转换、slot、租约、隔离和释放；
- profile 校验、摘要和重启生效语义；
- 输出摘要恢复和冲突。

### 17.2 Adapter 契约测试

- 提交成功并解析外部 task ID；
- 所有已知状态映射；
- 未知状态协议错误；
- 查询瞬时错误安全重试；
- 提交响应不确定时不重提；
- 空、超大、格式错误或缺少 Markdown 的结果；
- MinerU 单结果不依赖 stem；
- Docling 以部署实例真实 OpenAPI 固定协议。

### 17.3 Worker 集成与故障测试

- 文本任务在 submit task 内完成；
- 外部任务保存 ID并非阻塞轮询；
- poll 不在 worker 内 sleep；
- 成功 poll 在同一 task 内收尾；
- 重复消息不重复提交或发布；
- Redis 消息丢失后从 PostgreSQL 恢复；
- 提交、轮询、下载和发布阶段的 worker 中断恢复；
- 输出竞争只有一个任务成功；
- processor 容量不超限；
- 超时任务失败，slot 隔离并对账释放；
- staging 按策略清理。

### 17.4 真实格式 Smoke

至少逐格式验证：

| 格式 | 预期 processor |
|---|---|
| TXT、JSON、XML、CSV、自定义文本扩展名 | 文本直通 |
| 普通 DOCX | Docling |
| 复杂 DOCX | MinerU |
| DOC | MinerU |
| PPT、PPTX | MinerU |
| XLS、XLSX | Docling |
| HTML、EPUB | Docling |
| PDF、扫描图片 | MinerU |

每种启用格式验证路由、外部 ID、最终单一 Markdown、图片清理、UTF-8 无 BOM、摘要和 GET 元数据。未通过的格式不进入 production allowlist。

### 17.5 Docling 部署验证

基础：

- API、Redis、RQ worker 健康；
- RQ worker 已注册并能消费队列；
- 未授权请求被拒绝；
- 正确 API key 可以提交和查询；
- 生产 UI 和非必要远程能力关闭。

格式：

- 真实上传普通 DOCX、XLSX、HTML 和 EPUB；
- production allowlist 中的其他 Docling 格式逐一增加真实样本；
- 不用单个 DOCX 成功推断其他格式。

结果：

- 异步提交返回 task ID；
- 状态从非终态进入 success；
- 结果含非空 Markdown；
- 图片清理不破坏表格、代码和普通链接；
- 最终 UTF-8 无 BOM，且只发布一个 `.md`。

恢复：

- 转换期间重启 API，任务仍可查询或明确恢复；
- 转换期间重启 RQ worker，任务不静默丢失；
- Redis 重启后按持久化目标验证排队任务恢复；
- TextProcessor poll 消息丢失后补发；
- Docling 成功、TextProcessor 发布前崩溃后继续收尾；
- Docling 超时后 slot 隔离并对账释放；
- 错误不暴露内部堆栈、API key 或容器路径。

容量：

- 测量各格式处理时间、CPU、内存和临时磁盘；
- 验证 `maxInFlightTasks` 与 RQ worker 能力匹配；
- 大文件和超页数文件明确拒绝；
- 队列满载时产生可恢复背压。

### 17.6 交付证据

- 固定 Docling 镜像版本和 digest；
- Compose 配置和非敏感 profile；
- secret 注入说明；
- 真实实例 OpenAPI 契约测试；
- 每种启用格式 smoke 结果；
- API、RQ worker、Redis 重启恢复结果；
- 资源基线和最终并发配置依据；
- production allowlist；
- MinerU 当前 healthcheck 和真实提交结果，不能以历史缓存报告代替。
