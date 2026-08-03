# Markdown 组合清洗 Processor 设计

## 1. 目标与范围

本文定义 TextProcessor 内部 `MarkdownCleaningProcessor`，对单个 UTF-8 Markdown 文件确定性执行段落级精确去重、规则型脱敏和 Markdown 格式规范化，并生成单个 Markdown 文件及安全统计。

首版直接运行于 TextProcessor Celery worker，不增加 Data-Juicer Service、远程 processor 或第三套任务状态。实现固定组合：

```text
markdown-it-py
→ ParagraphDeduplicator
→ Microsoft Presidio
→ mdformat-gfm
```

首版不执行句子级或跨文档去重、近似去重、姓名/组织/地址实体识别、语义改写、纠错、摘要、翻译、章节重排、链接检查或调用方自定义 recipe。

## 2. 契约版本与依赖隔离

```text
contractVersion: markdown_cleaning_v1
input: one UTF-8 Markdown file
output: one UTF-8 Markdown file + safe in-memory summary
```

- contract version 的步骤顺序、保护区、匹配规则、占位符和格式规则不可原地改变。
- 行为变化新增 `markdown_cleaning_v2`，不能仅升级依赖后静默改变 v1。
- 第三方依赖全部位于 adapter 后，不在 route、Celery task 或 repository 中直接调用。
- 依赖版本写入项目锁文件；升级必须通过固定 corpus 兼容性测试。
- 运行时不接受 operator、正则、掩码、阈值、模型或插件参数。

## 3. 文件与职责

计划实现边界：

```text
features/markdown_cleaning/processors/
├── protocol.py          # Processor 输入输出协议
├── markdown_parser.py   # markdown-it-py adapter 与源码区间
├── paragraph_dedup.py   # 普通段落精确去重
├── presidio_adapter.py  # 五类 recognizer、重叠和替换
├── cn_recognizers.py    # 中国手机号与身份证校验
├── markdown_formatter.py# mdformat-gfm adapter 与安全检查
├── pipeline.py          # 固定顺序编排和统计
└── errors.py            # 稳定 processor 异常
```

每个模块只承担一项职责。第三方类型不能穿过 processor public protocol，避免依赖升级扩散到 worker。

## 4. 固定流水线

```text
严格读取
→ markdown-it-py 分区并建立保护区
→ 普通正文段落精确去重
→ 可处理文本片段 Presidio 脱敏
→ mdformat-gfm 格式规范化
→ 重新解析与不变量校验
→ 写入 staging destination
```

顺序固定：

- 去重先于脱敏，避免两个仅敏感值不同的段落在替换后错误合并。
- 去重使用内部比较规范化，不依赖最终 formatter 输出。
- 格式规范化最后执行，使最终字节确定。
- 任一步骤失败则 processor 整体失败，不返回部分结果、不降级跳过。

## 5. Markdown 解析与保护区

`MarkdownParserAdapter` 使用 `markdown-it-py` 的固定 CommonMark 配置及显式 GFM table 扩展，产生稳定的项目内 block/span 模型：

```text
heading
paragraph
list_item
blockquote
table
fenced_code
inline_code
html_block
thematic_break
blank
```

- parser 必须保留 block 和 inline 的源码区间；处理后按区间重建文档。
- fenced code 的 fence 行、info string 和内部内容为强保护区：不去重、不脱敏、不格式化内部内容。
- inline code 内容不脱敏；周围普通文本仍处理。
- HTML block 不去重、不脱敏、不执行。
- 标题、列表项、引用、表格和 thematic break 不参与段落去重。
- 标题、列表项、引用和表格中的普通文本参与脱敏。
- 无法可靠映射源码区间或 fence 未闭合时 `INVALID_MARKDOWN_INPUT`。
- 解析过程不执行 HTML、JavaScript、链接、include 或插件代码。

## 6. 段落级精确去重

### 6.1 候选与 key

只有 `paragraph` block 参与。跨多个 Markdown softbreak 的内容仍属于一个段落，但不能跨空行或其他 block。

比较 key：

1. CRLF/CR 视为 LF。
2. softbreak 替换为单个空格。
3. 连续普通空格与 tab 折叠为一个空格。
4. 去除首尾空白。
5. 保留大小写、数字、标点、inline Markdown、全半角和 Unicode 差异。

key 摘要只用于分桶，最终判等比较完整 key。

### 6.2 删除

- 首次出现原位保留，后续完全相同段落整体删除。
- 不合并相邻段落，不移动保留段落。
- 标题、列表、引用、表格、代码和 HTML 即使相同也保留。
- 单段内部重复句子不处理。
- 每删除一个 block，`duplicateParagraphsRemoved` 加一。

## 7. Presidio 脱敏

### 7.1 Allowlist

只注册和请求：

| Entity | 占位符 | 统计字段 | 来源 |
|---|---|---|---|
| `CN_MOBILE_PHONE` | `[PHONE]` | `phone` | 项目自定义 recognizer |
| `CN_ID_CARD` | `[ID_CARD]` | `idCard` | 项目自定义 recognizer |
| `CREDIT_CARD` | `[BANK_CARD]` | `bankCard` | Presidio 内建 + 契约测试 |
| `EMAIL_ADDRESS` | `[EMAIL]` | `email` | Presidio 内建 + 契约测试 |
| `IPV4_ADDRESS` | `[IPV4]` | `ipv4` | 项目包装通用 IP recognizer |

不启用 PERSON、LOCATION、URL、IPv6、MAC、路径、密钥或其他实体。不能把调用方字段传给 analyzer 以扩大实体集合。

### 7.2 中国 recognizer

- 手机号候选为中国大陆 `1[3-9]` 开头的 11 位号码，允许受控空格或连字符；去除分隔符后验证，前后不能紧邻数字。
- 身份证为 18 位中国居民身份证号，末位允许 `X/x`，必须校验行政区码基本形状、出生日期和 GB 11643 校验位；15 位号码不处理。
- 自定义 recognizer 必须返回稳定 entity type 和 source span，不记录原值。

### 7.3 内建 recognizer 约束

- `CREDIT_CARD` 只接受 12–19 位且通过 checksum 的结果；未通过的长数字保留。
- `EMAIL_ADDRESS` 只替换完整邮箱 span。
- 通用 IP 检测结果必须二次校验为四个 `0..255` octet；IPv6 结果丢弃。
- Presidio 内建行为若与上述契约不一致，由 adapter 过滤或以自定义 recognizer 替换，不能修改业务契约。

### 7.4 重叠与替换

固定优先级：

```text
EMAIL_ADDRESS
→ CN_ID_CARD
→ CN_MOBILE_PHONE
→ CREDIT_CARD
→ IPV4_ADDRESS
```

- 高优先级已占用 span 不再参与后续替换。
- 按源码位置从后向前替换，避免 offset 漂移。
- 占位符不匹配任何 recognizer，第二次运行计数为零。
- 只处理 parser 标记为可处理的文本 span。
- 统计按实际替换 span 计数，不保存原值、哈希、位置或上下文。

## 8. mdformat-gfm 格式规范化

`MarkdownFormatterAdapter` 通过 Python API 使用锁定的 `mdformat` 和 `mdformat-gfm`；不调用 CLI，不自动发现并启用代码 formatter。

目标规则：

- UTF-8 无 BOM、LF、文件末尾恰一个 LF；
- 删除保护区外行尾空白；
- 文档开头无空行，连续空行收敛；
- ATX 标题与正文间空格规范；
- 标题和代码块与相邻 block 的空行规范；
- 无序列表标记统一为 `-`，保留层级；
- 不重排章节、列表项、引用或表格；
- 不更改链接目标、正文语义、fence info string 或代码内容。

由于 mdformat 是 opinionated formatter，adapter 必须执行：

1. 格式化前后重新解析。
2. 比较 block 类型和顺序。
3. 比较 protected span 内容。
4. 拒绝超出允许集合的结构变化。
5. 再次格式化，要求输出字节完全不变。

若合法 GFM 输入不能安全通过，首版以 `MARKDOWN_NORMALIZATION_FAILED` 明确失败。只有实现项目内受控 fallback 并通过同等测试后才允许降级，不能静默跳过 formatter。

`formattingChanges` 按格式化前后受允许规则影响的连续 source span 计数；它是观测指标，不用于重建 diff。计数算法属于 v1 契约，必须用 golden tests 固定。

## 9. Processor 输出

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
    contract_version: Literal["markdown_cleaning_v1"]
    summary: MarkdownCleaningSummary
```

输出文件不嵌入统计、注释或 provenance。Processor 不直接写业务 targetPath。

## 10. 不变量

每次生产执行成功前必须验证：

- 输出非空、严格 UTF-8、无 BOM、LF、末尾恰一个 LF；
- block 可重新解析且顺序符合允许变化；
- fenced/inline code 保护内容与输入对应内容一致；
- 不存在内部临时 token；
- 统计均为非负整数且字段固定；
- 输出 SHA-256 与返回值一致；

此外，contract 要求对输出再次运行 processor 时字节不变，且去重、五类脱敏和格式计数全部为零。该二次执行不变量由单元、集成和真实 corpus 验证；生产默认只运行一次，不为每个请求付出双倍处理成本。

## 11. 错误

稳定 processor 错误：

```text
INVALID_MARKDOWN_INPUT
MARKDOWN_PARSE_FAILED
PARAGRAPH_DEDUPLICATION_FAILED
SENSITIVE_DATA_REDACTION_FAILED
MARKDOWN_NORMALIZATION_FAILED
INVALID_PROCESSOR_OUTPUT
PROCESSING_TIMEOUT
INTERNAL_ERROR
```

第三方异常必须映射并脱敏，不返回正文、敏感原值、match、analyzer score、内部堆栈或宿主机绝对路径。确定性输入与处理错误不重试；worker 进程退出由任务层按相同输入重跑。

## 12. 资源与安全

- 文件大小、段落数、单段长度、token 数、PII 候选数和总处理时间均设上限。
- 正则避免灾难性回溯，自定义 recognizer 使用预编译模式和线性校验。
- Presidio 仅使用本地运行时；不调用 Azure 或其他远程服务。
- 依赖、模型或插件不得在请求期间自动下载或安装。
- Processor 只访问传入的 staging 文件。
- 日志和指标只记录摘要、大小、耗时与安全计数。

## 13. 测试与验收

### 13.1 Parser 与段落去重

- CommonMark/GFM 混合 block 的类型、顺序和 source span。
- 完全相同及 softbreak/空白差异段落只保留第一次。
- 大小写、标点、全半角或 inline Markdown 不同则保留。
- 摘要碰撞时完整 key 不同不误删。
- 标题、列表、引用、表格、代码和 HTML 不去重。
- 单段重复句子保留，删除计数准确。

### 13.2 脱敏

- 五类合法正例、边界正例与无效反例。
- 身份证日期和校验位，银行卡 checksum，IPv4 octet。
- 手机号受控分隔符与数字边界。
- 重叠严格遵循优先级，只替换一次。
- fenced code、inline code 和 HTML block 内值保持。
- 非目标实体不误替换，第二次执行计数为零。

### 13.3 格式

- BOM、CRLF/CR、行尾空白、首尾与连续空行。
- 标题、嵌套列表、GFM 表格、链接和引用。
- fence 字符、info string 与代码内容保持。
- AST safety 拒绝非允许结构变化。
- 第二次格式化字节不变、计数为零。

### 13.4 组合与真实 corpus

- 两个只在敏感值上不同的段落脱敏后仍各自保留。
- 重复段落含敏感值时只统计保留内容中的替换。
- 中文、英文、Unicode、长段落和大量 block 的资源边界。
- golden corpus 逐字节断言输出和全部统计，不能只断言无异常。
- 依赖升级前后重跑 corpus；任何输出变化都必须显式评审 contract version。

完成标准是本地 Processor、真实三项依赖和固定 corpus 验证通过；不需要启动或验证 Data-Juicer Service。
