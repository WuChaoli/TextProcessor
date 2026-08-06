# Markdown 组合清洗 TextProcessor Worker 设计

## 1. 目标与范围

本文定义 Markdown 组合清洗异步任务在 TextProcessor worker 中的输入解析、staging、本地 processor 执行、结果校验、原子发布、中断恢复和测试要求。

本文建立在《Markdown 组合清洗异步任务接口设计》之上。首版采用两层架构，不增加 Data-Juicer Service 或其他远程处理服务：

```text
FastAPI 任务接口
→ PostgreSQL + Redis/Celery
→ TextProcessor Worker
→ MarkdownCleaningProcessor
→ 原子发布 Markdown
```

`MarkdownCleaningProcessor` 固定组合 `markdown-it-py`、Microsoft Presidio 和 `mdformat-gfm`，但通过项目内 adapter 隔离第三方 API。业务请求不能选择依赖库、处理步骤、正则、掩码或参数。

## 2. 组件边界

```text
Celery Worker
├── MarkdownCleaningRepository
├── InputResolver
├── MarkdownInputValidator
├── CleaningStagingManager
├── MarkdownCleaningProcessor
│   ├── MarkdownParserAdapter
│   ├── ParagraphDeduplicator
│   ├── PresidioRedactionAdapter
│   └── MarkdownFormatterAdapter
├── CleaningOutputValidator
└── AtomicPublisher

Celery Beat
└── MarkdownCleaningRecoveryDispatcher
```

- Repository 负责条件状态转换、租约、摘要、计数和错误持久化。
- InputResolver 负责受控本地与远程输入，不把业务 URI 交给 processor。
- InputValidator 负责编码、大小和基本 Markdown 完整性。
- StagingManager 只管理当前任务独占目录。
- Processor 只接收本地输入与输出路径和受控上下文，不访问数据库、Celery、网络或最终 `targetPath`。
- 第三方 adapter 负责将库行为映射为项目内稳定接口，依赖升级不能越过 adapter 改变业务契约。
- OutputValidator 验证文件和安全统计，不重新实现 processor 算法。
- AtomicPublisher 负责禁止覆盖发布和发布后崩溃恢复。

## 3. Celery 消息与执行权

任务名：

```text
markdown_cleaning.execute
markdown_cleaning.recover
```

execute 消息只携带：

```json
{
  "taskId": "01K1CLEANING00000000000001",
  "taskType": "markdown_cleaning",
  "schemaVersion": 1
}
```

- 消息不携带路径、正文、processor 配置或凭据。
- `task_acks_late=true`，`worker_prefetch_multiplier=1`。
- 重复消息遇到终态任务时安全结束。
- worker 通过带状态、租约 token 和过期时间条件的数据库更新获得执行权。
- 单任务只调用一次组合 processor；processor 内部步骤不拆成独立 Celery task，避免产生部分成功和跨步骤恢复状态。
- Celery transport retry 与业务 attempt 分开计数，均有限次。

## 4. 完整数据流

1. 校验 Celery 消息 schema。
2. 从 PostgreSQL 读取完整任务参数。
3. 从 `queued` 原子转为 `running`，记录 attempt、租约和 `validating_input`。
4. 创建 job 专属 staging 目录。
5. 按输入优先级将选定输入原始字节流式复制到 staging `source.original.md`，计算外部输入 SHA-256 和字节数。
6. 验证文件后缀、UTF-8、大小、控制字符和 Markdown 围栏完整性；允许且仅允许文件起始 UTF-8 BOM，并原子生成无 BOM 的 `source.md` Processor 输入。
7. 更新阶段为 `cleaning`，将任务 UTC deadline 显式传给 `MarkdownCleaningProcessor.process()`。
8. processor 在 staging 内写出 `result.md` 和内存中的安全统计对象。
9. Worker 校验输出摘要、编码、结构和统计 schema。
10. 保存 prepared SHA-256、临时路径和统计，更新阶段为 `publishing`。
11. 将结果复制到目标目录内的 job 专属 `.part` 文件，并以禁止覆盖方式原子发布到 `targetPath`。
12. 保存最终摘要和发布时间，任务转为 `succeeded/completed/100`。
13. 清理本任务可安全删除的 staging 文件。

Worker 进程内同步调用 processor。由于该工作属于 CPU 与文件 I/O 混合型后台任务，不占用 FastAPI 事件循环；通过 Celery worker concurrency 和任务 deadline 控制资源。

## 5. Staging

```text
{markdownCleaningStagingRoot}/{taskId}/
├── input/
│   ├── source.original.md  # 外部输入原始字节，允许起始 UTF-8 BOM
│   └── source.md           # Processor 输入，严格 UTF-8 无 BOM
└── output/
    ├── result.md
    └── publish.md.part
```

- staging 根由服务端配置，请求不能指定。
- 所有真实路径必须仍属于 task 目录，拒绝 `..`、符号链接和 junction 逃逸。
- 远程输入由 InputResolver 下载，processor 不访问网络。
- `source.original.md` 的摘要/大小是外部输入与下载复用的权威记录；`source.md` 有独立摘要/大小，只能由 validator 从已验证原件生成。
- validator 只移除文件起始的单个 UTF-8 BOM，不重排 Markdown、不改换行或其他正文；无 BOM 时仍生成独立 `source.md`，禁止让 Processor 直接读取原件。
- 重试只有在两份 staging 文件各自与数据库记录的 SHA-256 和大小一致时才可复用；任一不一致即重新生成或安全失败。
- processor 不直接写最终 `targetPath`。
- 清理只删除当前任务目录，不删除业务输入或最终输出。

## 6. 输入校验

提交 processor 前验证：

- 选定输入可读且为普通文件；
- 扩展名为 `.md` 或 `.markdown`；
- 文件非空且不超过配置上限；
- 外部 `source.original.md` 必须为严格 UTF-8，可允许文件起始的单个 UTF-8 BOM；其他位置的 BOM 不作为编码标记处理；
- validator 必须原子写出严格 UTF-8、无 BOM 的 `source.md`，随后 Processor 继续执行 strict no-BOM 校验；
- 不含 NUL 或被禁止控制字符；
- 围栏代码块闭合，避免正文错误进入保护区；
- 输入和 `targetPath` 解析后不是同一文件；
- 输入 SHA-256 与复用记录一致。

不验证链接可达、HTML 合法、表格列数或正文事实正确性。

## 7. Processor 接口

```python
@dataclass(frozen=True, slots=True)
class MarkdownCleaningSummary:
    duplicate_paragraphs_removed: int
    phone_redactions: int
    id_card_redactions: int
    bank_card_redactions: int
    email_redactions: int
    ipv4_redactions: int
    formatting_changes: int


@dataclass(frozen=True, slots=True)
class MarkdownCleaningResult:
    output_path: Path
    input_sha256: str
    output_sha256: str
    contract_version: str
    summary: MarkdownCleaningSummary


class MarkdownCleaningProcessor(Protocol):
    def process(
        self,
        source: Path,
        destination: Path,
        *,
        deadline: datetime | None = None,
    ) -> MarkdownCleaningResult: ...
```

固定 `contract_version=markdown_cleaning_v1`。Processor：

- 只能读取 source、写 destination；
- 不读取环境中的任意业务配置；
- 不访问 PostgreSQL、Redis、Celery、HTTP 或最终输出路径；
- 不记录正文、敏感原值或匹配上下文；
- 相同输入字节必须产生相同输出字节与统计；
- 任一步骤失败时不返回部分成功结果。
- `deadline` 仅为公共接口兼容而可选；生产 Worker 必须传任务记录中的 timezone-aware UTC deadline。有效 deadline 取任务 deadline 与 Processor 内部最大处理秒数中较早者，已过期立即 `PROCESSING_TIMEOUT`，子进程 timeout 使用剩余时间。

## 8. 第三方依赖边界

### 8.1 `markdown-it-py`

`MarkdownParserAdapter` 使用固定 CommonMark/GFM 配置生成 token stream，识别 paragraph、heading、list、blockquote、table、fenced code、inline code、HTML block 和 thematic break。

- parser 不执行 HTML、脚本、链接或插件代码。
- adapter 必须保留源码区间映射，供处理器只改写允许区域。
- 依赖升级必须通过固定 Markdown corpus 的 token 与输出兼容性测试。

### 8.2 Microsoft Presidio

`PresidioRedactionAdapter` 只启用五类 allowlist recognizer：

```text
CN_MOBILE_PHONE
CN_ID_CARD
CREDIT_CARD
EMAIL_ADDRESS
IPV4_ADDRESS
```

- `CREDIT_CARD`、`EMAIL_ADDRESS` 复用经过验证的内建 recognizer。
- `CN_MOBILE_PHONE` 与 `CN_ID_CARD` 使用项目注册的自定义 recognizer；身份证必须校验日期与校验位。
- IP 结果只接受 IPv4，不能因 Presidio 的通用 IP recognizer 扩展到 IPv6。
- 只把 parser 标记为可处理的文本片段交给 Presidio；fenced code、inline code 和 HTML block 不进入 analyzer。
- 不加载 PERSON、LOCATION 等语义实体，不启用远程 Azure 服务。
- 若 Presidio 最小运行仍要求 NLP engine，使用锁定的本地最小 engine；不得联网下载模型或在请求期间安装依赖。
- adapter 负责稳定占位符、重叠优先级和每类计数，不暴露 analyzer score 或原始匹配。

### 8.3 `mdformat-gfm`

`MarkdownFormatterAdapter` 以固定扩展集合调用 Python API，不执行 CLI 子进程，不启用代码 formatter plugin。

- 首版只允许 GFM tables 等已验证扩展。
- fenced code 内容必须逐字节保持。
- formatter 输出必须通过 AST safety、结构顺序和第二次格式化幂等测试。
- 若 mdformat 对某类合法输入产生超出业务契约的语义变化，该类输入必须明确失败或走项目内受控 normalizer；不得静默接受。
- 依赖或插件自动发现不能改变生产行为；启用扩展必须在代码中显式列出。

## 9. 结果校验

Worker 验证：

- `result.output_path` 等于本任务 staging destination；
- `contract_version` 完全匹配；
- input SHA-256 等于 staging 输入；
- 文件存在、为普通文件、非空且不超过输出上限；
- 实际 SHA-256 等于 processor 返回值；
- 输出严格 UTF-8、无 BOM、LF、末尾恰一个换行；
- 围栏代码块闭合且保护内容未变化；
- 统计字段固定且均为非负整数；
- 输出不含内部临时 token；

对输出再次运行 processor 时输出字节不变，且重复删除、脱敏和格式修改计数均为零；该幂等性质由测试和可选高风险验收验证，生产任务不默认重复运行整条流水线。

## 10. 输出发布

- 处理前目标存在时快速 `OUTPUT_CONFLICT`，发布阶段仍必须禁止覆盖。
- 结果先复制到 `targetPath` 同目录的 task 专属 `.part` 文件。
- flush、关闭并计算摘要后持久化 prepared 状态。
- 以禁止覆盖的同文件系统原子操作发布。
- 不同任务竞争同一目标时只能一个成功。
- 失败时只清理当前 task 的 `.part`，不能删除已有目标。

## 11. 恢复与重试

Beat 扫描：

- 超过入队窗口的 `pending`；
- 超过 dispatch 窗口的 `queued`；
- 租约过期的 `running`；
- prepared/published 但未进入终态的任务。

恢复规则：

- recover 只重新投递 execute 或修复可证明终态，不在 Beat 中读取正文、运行 processor 或发布。
- processor 从相同输入 SHA-256 重新执行必须确定性收敛。
- 最终文件摘要等于 prepared SHA-256 时恢复成功；不同时 `OUTPUT_CONFLICT`。
- 请求、输入、确定性 processor、非法输出与冲突不重试。
- worker 异常、临时数据库/broker/文件系统错误可有限重试。
- 所有重试沿用同一 taskId、输入摘要、contract version 和 targetPath。

## 12. 进度

```text
validating_input: 0..15
cleaning:         15..85
publishing:       85..99
completed:        100
```

进度单调非递减。Processor 可通过进程内 callback 按 `parsing/deduplicating/redacting/formatting/validating` 报告内部阶段，但公共 API 统一映射为 `cleaning`，并按时间或增量阈值持久化。

## 13. 配置与容量

至少提供：

```text
MARKDOWN_CLEANING_STAGING_ROOT
MARKDOWN_CLEANING_INPUT_ROOTS
MARKDOWN_CLEANING_OUTPUT_ROOTS
MARKDOWN_CLEANING_ALLOWED_URL_HOSTS
MARKDOWN_CLEANING_INPUT_MAX_BYTES
MARKDOWN_CLEANING_OUTPUT_MAX_BYTES
MARKDOWN_CLEANING_DOWNLOAD_TIMEOUT_SECONDS
MARKDOWN_CLEANING_TASK_TIMEOUT_SECONDS
MARKDOWN_CLEANING_PROCESSOR_MAX_SECONDS
MARKDOWN_CLEANING_CELERY_HARD_TIME_LIMIT_SECONDS
MARKDOWN_CLEANING_MAX_ATTEMPTS
MARKDOWN_CLEANING_LEASE_SECONDS
MARKDOWN_CLEANING_RECOVERY_INTERVAL_SECONDS
MARKDOWN_CLEANING_RECOVERY_BATCH_SIZE
MARKDOWN_CLEANING_WORKER_CONCURRENCY
```

单文档内容需要 token/source-map 与 Presidio interval，worker concurrency 必须按输入上限和内存 smoke 结果设置，不能直接继承轻量 API worker 并发。默认快速测试不下载 NLP 模型。

任务 deadline 由 Worker 从权威任务记录传入 Processor；Celery hard time limit 必须大于配置的整体 task timeout 与子进程终止宽限之和，仅用于父进程或操作系统级终止异常的第二道兜底。

## 14. 可观察性与安全

日志至少包含 `request_id`、`task_id`、`caller_id`、contract version、阶段、attempt、输入输出字节数、耗时、安全统计和错误码。指标至少包括状态数量、阶段耗时、重复段落删除数、各类脱敏数、格式修正数、重试、租约过期、恢复和输出冲突。

日志、指标、tracing 和错误响应禁止包含正文、敏感原值、匹配上下文、Presidio analyzer 结果、完整内部路径、凭据或底层堆栈。

## 15. 测试与验收

### 15.1 单元测试

- 消息 schema、租约、状态机和终态幂等。
- 本地/远程输入优先级、流式限制和不降级。
- UTF-8、起始 BOM、内部 BOM、NUL、空文件、大小和未闭合 fence；断言保留 `source.original.md` 且生成独立无 BOM `source.md`。
- staging 路径、符号链接和任务隔离。
- parser token/source-map 与代码保护。
- Presidio allowlist、自定义中国 recognizer、重叠和计数。
- mdformat 固定扩展、代码保持、AST safety 和幂等。
- processor 固定顺序、确定性和异常归一化。
- 输出摘要、统计 schema、冲突、`.part` 和摘要恢复。

### 15.2 集成测试

- PostgreSQL + Redis/Celery + 本地文件完整 execute 链路。
- 重复消息、worker 在四个 processor 阶段中断后的恢复。
- 两任务竞争相同 targetPath 时只有一个成功。
- 发布完成但数据库未更新时通过摘要恢复。
- 日志与数据库不包含正文或敏感原值。

### 15.3 真实验收

真实启动 TextProcessor API、worker、Beat、PostgreSQL 和 Redis，使用包含重复段落、五类敏感信息、标题、嵌套列表、引用、GFM 表格、inline code、fenced code 和 HTML block 的固定中文样本，逐字节验证预期输出和统计。

验收链路：

```text
POST
→ Celery worker
→ 本地 MarkdownCleaningProcessor
→ 原子发布
→ GET succeeded
```

未真实运行依赖库和固定样本的验证不得声称 processor 已落地；不再要求 Data-Juicer 服务或跨服务端到端测试。
