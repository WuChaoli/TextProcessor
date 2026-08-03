# Data-Juicer Markdown 组合清洗 Profile 设计

## 1. 目标与范围

本文为独立 Data-Juicer Service 增加固定 profile `markdown_cleaning_v1`，对单个 UTF-8 Markdown 文件确定性执行段落级精确去重、规则型脱敏和 Markdown 格式规范化，并生成单个 Markdown 文件及安全统计元数据。

首版不执行句子级去重、跨文档去重、近似去重、姓名/组织/地址实体识别、语义改写、纠错、摘要、翻译、章节重排、链接可达性检查或任意调用方 recipe。本文先固定行为契约；Data-Juicer 具体算子选择和必要的受控 wrapper 将在本文评审后单独讨论并形成实现计划。

## 2. Profile 注册与不变性

```text
profile: markdown_cleaning_v1
profileVersion: 1
input: one UTF-8 Markdown file
output: one UTF-8 Markdown file + safe JSON metadata
```

- profile 在服务端注册，调用方只能选择 allowlist 中的名称。
- `v1` 的步骤顺序、保护区、匹配规则、占位符和格式规则发布后不可原地改变。
- 行为变化必须新增 profile，例如 `markdown_cleaning_v2`。
- 运行时不能从请求接收 operator、正则、掩码、阈值、进程数、模型、凭据或环境变量。
- 相同输入字节、相同 profile 和相同实现版本必须产生相同输出字节及统计。

## 3. 处理流水线

固定顺序：

```text
严格读取与 Markdown 分区
→ 普通正文段落精确去重
→ 普通内容规则脱敏
→ Markdown 格式规范化
→ 重新解析与不变量校验
→ 原子发布
```

顺序理由：

- 去重在脱敏前执行，避免两个原本不同、仅敏感值不同的段落在替换为相同占位符后被错误合并。
- 去重使用内部比较规范化，不依赖最终格式规范化结果。
- 格式规范化最后执行，使最终换行和结构输出唯一且确定。

任一阶段失败则整个 job 失败，不发布部分结果，不跨阶段降级，也不跳过失败规则。

## 4. Markdown 分区与保护区

解析结果至少区分：

```text
heading
paragraph
list_item
blockquote
table
fenced_code
html_block
thematic_break
blank
```

首版规则：

- 围栏代码块的 fence 行和内部内容是强保护区：不去重、不脱敏、不改写。
- 行内代码 span 也是保护区：内部不脱敏，外部普通文本仍处理。
- 标题、列表项、引用块、表格、HTML block 和 thematic break 不参与段落去重。
- 脱敏处理普通段落、标题、列表项、引用块和表格的文本单元，但跳过 fenced code、inline code 和 HTML block。
- 格式规范化可以调整保护区外的结构空白；不得改写代码块内部字节。
- 无法可靠分区或存在未闭合围栏代码块时以 `INVALID_MARKDOWN_INPUT` 失败。

解析器不得执行嵌入式 HTML、脚本、链接或任何主动内容。

## 5. 段落级精确去重

### 5.1 候选范围

只有 `paragraph` 节点参与去重。一个候选段落可以跨多个软换行，但不能跨空行或其他块级节点。标题、列表、引用、表格、代码、HTML 和 thematic break 即使文本相同也必须全部保留。

### 5.2 比较规范化

每个候选段落生成仅用于比较的 key：

1. 将 CRLF/CR 视为 LF。
2. 将段落内部由 Markdown 软换行产生的换行替换为单个空格。
3. 将连续的普通空格和 tab 折叠为一个空格。
4. 去除段落首尾空白。
5. 保留大小写、数字、标点、Markdown inline 标记、全角/半角差异和 Unicode 字符，不执行 Unicode 兼容归一化。

key 必须非空。摘要只用于快速分桶，最终判等必须比较完整 key，避免摘要碰撞误删。

### 5.3 删除规则

- 首次出现的段落原位保留。
- 后续 key 完全相同的段落整体删除。
- 不将相邻段落合并，不移动保留段落的位置。
- 去重后的空白由最终格式规范化阶段收敛。
- `duplicateParagraphsRemoved` 每删除一个完整段落加一。
- 单段内部重复句子不处理。

## 6. 规则型脱敏

### 6.1 类型与占位符

固定类型：

| 类型 | 占位符 | 统计字段 |
|---|---|---|
| 中国大陆手机号 | `[PHONE]` | `phone` |
| 中国居民身份证号 | `[ID_CARD]` | `idCard` |
| 银行卡号 | `[BANK_CARD]` | `bankCard` |
| 邮箱地址 | `[EMAIL]` | `email` |
| IPv4 地址 | `[IPV4]` | `ipv4` |

占位符不可逆且不包含原值片段。输出元数据只记录命中次数，不记录原值、哈希、位置或上下文。

### 6.2 识别规则

- 手机号：识别中国大陆 `1[3-9]` 开头的 11 位号码，允许受控的空格或连字符分隔；前后不能紧邻其他数字。
- 身份证号：识别 18 位中国居民身份证号，最后一位允许 `X/x`；必须通过出生日期合法性和校验位验证。15 位旧号码首版不处理。
- 银行卡号：识别去除空格或连字符后 12 至 19 位数字，必须通过 Luhn 校验；前后不能紧邻其他数字。
- 邮箱：识别常见 dot-atom local-part 与 DNS 域名形式；匹配大小写不敏感，但统计按实际替换片段计数。
- IPv4：识别四个十进制 octet，每段 `0..255`；前后不能紧邻数字或点。

### 6.3 冲突与执行顺序

固定匹配优先级：

```text
email → idCard → phone → bankCard → ipv4
```

- 已替换区间不再参与后续类型匹配。
- 身份证和手机号优先于银行卡，避免有效身份证或手机号被长数字规则截取。
- 邮箱优先，避免邮箱 local-part 或域名片段被其他规则部分替换。
- 占位符本身不应再次匹配任何规则，因此重复执行 profile 不新增脱敏计数。
- 只替换完整匹配内容；保留匹配两侧原有标点和空白。

首版不承诺识别姓名、组织、地址、护照、统一社会信用代码、MAC、IPv6、URL query token 或任意密钥。发现此类需求时新增经过评审的 profile 版本，不扩大 v1 正则。

## 7. Markdown 格式规范化

最终输出规则：

- 输出编码为 UTF-8 无 BOM。
- 所有文本换行统一为 LF。
- 删除保护区外每行行尾空格和 tab。
- 文档开头不保留空行；文档末尾恰有一个 LF。
- 保护区外连续空行收敛为一个空行。
- ATX 标题的 `#` 与标题正文之间恰有一个空格；保留标题级别和正文。
- 标题与相邻非空块之间恰有一个空行。
- 无序列表标记统一为 `-`，保留嵌套层级和相对缩进；不改变有序列表编号。
- 围栏代码块保留原 fence 字符、长度、info string 和内部内容；只保证 fence 块与相邻非空块之间有一个空行。
- 不重排章节、列表项、引用、表格行列或 inline 标记。
- 不解析并重新序列化表格，不修正错别字，不更改链接目标，不增删标题层级。

`formattingChanges` 是实际发生的原子格式修正总数。固定计数规则：每次换行序列转换、BOM 移除、行尾空白删除、空行序列收敛、标题空格修正、块间空行修正或无序列表标记替换各计一次；一次操作影响连续字符时仍计一次。该计数用于观察，不用于重建 diff。

## 8. 输入与输出契约

Data-Juicer job 使用现有内部接口：

```http
POST /v1/jobs
GET  /v1/jobs/{jobId}
```

提交参数：

```json
{
  "requestId": "01K1CLEANING00000000000001",
  "profile": "markdown_cleaning_v1",
  "inputPath": "/shared/task/input/source.md",
  "outputPath": "/shared/task/datajuicer/result.md"
}
```

输入和输出路径必须位于 Data-Juicer 服务端 allowlist 中。输入必须是非空普通文件、严格 UTF-8、大小受限且 SHA-256 在执行前固定。`outputPath` 已存在时失败，不覆盖。

成功结果：

```json
{
  "outputPath": "/shared/task/datajuicer/result.md",
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

输出 Markdown 不嵌入统计、注释或 provenance；元数据独立返回。PostgreSQL 只保存 job 状态、摘要、路径引用、安全计数和生命周期，不保存正文或敏感原值。

## 9. 幂等与状态

Data-Juicer 以 `requestId` 幂等：

- 相同 `requestId + profile + inputPath + outputPath + inputSha256` 返回原 job。
- 相同 requestId 携带不同参数或输入摘要返回 `409 IDEMPOTENCY_CONFLICT`。
- 已失败 job 仍返回原 job；重跑需要新的 requestId。
- 数据库对 requestId 建立唯一约束。

状态机沿用：

```text
pending -> queued -> running -> succeeded
                     |-------> failed
                     |-------> cancelled
```

内部阶段：

```text
validating_input
parsing_markdown
deduplicating_paragraphs
redacting_sensitive_data
normalizing_markdown
validating_output
publishing
completed
```

进度单调非递减，按阶段和块数量批量持久化，不按每次正则匹配写数据库。

## 10. 输出不变量与发布

发布前必须重新读取输出并验证：

- 文件非空、严格 UTF-8、无 BOM、仅 LF、末尾恰有一个 LF；
- Markdown 围栏代码块闭合；
- 输出仍可被同一分区器完整解析；
- fenced code 和 inline code 保护内容与输入对应内容逐字节一致；仅块外分隔换行可变化；
- 输出中不存在内部临时 token 或未完成替换标记；
- 所有计数为非负整数且字段集合固定；
- 对输出再次运行 `markdown_cleaning_v1` 时输出字节不变，且 `duplicateParagraphsRemoved=0`、全部 redaction count 为 0、`formattingChanges=0`。

发布流程：

1. 写入 job 专属 `.part` 文件。
2. flush、关闭并计算 SHA-256。
3. 执行全部输出不变量。
4. 保存 prepared SHA-256 和临时路径。
5. 以禁止覆盖方式原子发布到 outputPath。
6. 保存 output SHA-256、统计和发布时间。
7. job 转为 `succeeded`。

发布后数据库未落终态时，摘要相同可恢复成功；摘要不同以 `OUTPUT_CONFLICT` 失败。任何清理只能删除当前 job 的临时文件。

## 11. 错误与重试

稳定错误码：

```text
PROFILE_NOT_SUPPORTED
IDEMPOTENCY_CONFLICT
INPUT_NOT_FOUND
INPUT_READ_FAILED
INPUT_TOO_LARGE
INVALID_MARKDOWN_INPUT
MARKDOWN_PARSE_FAILED
PARAGRAPH_DEDUPLICATION_FAILED
SENSITIVE_DATA_REDACTION_FAILED
MARKDOWN_NORMALIZATION_FAILED
INVALID_PROFILE_OUTPUT
OUTPUT_CONFLICT
OUTPUT_WRITE_FAILED
OUTPUT_INTEGRITY_FAILED
JOB_TIMEOUT
DATABASE_ERROR
INTERNAL_ERROR
```

非法输入、解析、确定性规则、非法输出和输出冲突不重试。worker 异常退出、临时数据库/Redis 故障和未发布前的短暂存储错误可有限重试。重试沿用同一 jobId、requestId、profile、input SHA-256 和 outputPath，不改变规则或跳过阶段。

对外错误不返回正文、敏感原值、匹配上下文、正则细节、内部堆栈或绝对宿主机路径。

## 12. 安全与资源边界

- API 不接受正文、任意 recipe、operator、规则、凭据或环境变量。
- 输入输出路径分别受根目录 allowlist 和真实路径校验约束。
- 解析 Markdown 不执行 HTML、JavaScript、链接、include、插件或 shell。
- 正则实现必须避免灾难性回溯；对单段长度、总段落数、总字节数和任务时长设上限。
- 文件流式读取并受限；不得把超限文件无界加载到内存。
- 日志和指标只记录 jobId、profile、阶段、摘要、大小、耗时和安全计数。
- 不持久化敏感原值、其哈希、匹配位置或上下文窗口。

## 13. 测试与验收

### 13.1 段落去重

- 完全相同段落只保留第一次。
- 软换行、多个空格和 tab 差异按比较规范化判重。
- 大小写、标点、全半角或 inline Markdown 不同的段落不删除。
- 摘要碰撞时完整 key 不同不得误删。
- 相同标题、列表项、引用、表格、代码和 HTML block 全部保留。
- 单段内部重复句子保留。
- 删除计数准确且重复执行为零。

### 13.2 脱敏

- 每类合法正例、边界正例和无效反例。
- 身份证出生日期与校验位验证。
- 银行卡 Luhn 验证和 12 至 19 位边界。
- IPv4 octet 上下界和相邻字符边界。
- 重叠候选严格遵循固定优先级且只替换一次。
- fenced code、inline code 和 HTML block 内值保持不变。
- 占位符不可逆，统计不包含原值，重复执行不新增计数。
- 姓名、普通地址、IPv6、15 位身份证等非目标内容不被误替换。

### 13.3 格式规范化

- BOM、CRLF/CR、行尾空白、首尾空行和连续空行。
- 标题空格、标题块间空行和无序列表标记。
- 嵌套列表缩进、有序列表编号、表格和链接保持。
- fence 字符、长度、info string 与代码内部字节保持。
- 输出格式与 `formattingChanges` 固定计数一致。
- 第二次执行输出字节不变且三类统计全部为零。

### 13.4 组合与属性测试

- 两个只在敏感值上不同的段落在脱敏后仍各自保留，证明去重先于脱敏。
- 重复段落含敏感值时先删除后统计，只统计实际保留并处理内容中的替换。
- 标题、段落、列表、引用、表格、HTML 和代码混合文档保持结构顺序。
- 随机 Unicode、超长段落和大量分区受资源上限保护。
- 任意成功输出再次执行均满足幂等不变量。

### 13.5 服务集成与真实验收

- POST → PostgreSQL → Redis/Celery → profile → output → GET。
- 并发同 requestId 只执行一个 job。
- 重复消息、worker 中断和发布后崩溃可恢复。
- 不同 job 竞争相同 outputPath 时只有一个成功。
- 数据库、日志、指标和错误响应不包含正文或敏感原值。
- 使用固定中文 Markdown 样本逐字节比对预期输出和每类统计，不能只断言 job succeeded。

未运行真实 Data-Juicer Service worker 和固定样本的验证不得声称 profile 已打通。

## 14. 后续算子讨论输入

实现计划前需要把本契约逐项映射到 Data-Juicer v1.5.4：

- 是否已有安全解析 Markdown block 且保护 code 的算子；
- 文内 paragraph exact dedup 是否可由现有 deduplicator 无损表达；
- 五类脱敏是否已有满足校验和边界规则的算子；
- Markdown 结构规范化是否需要独立受控 wrapper；
- 现有算子的输入数据模型是否会破坏文档块顺序或删除非目标记录；
- 哪些能力应复用上游，哪些必须在 `datajuicer_service` adapter/profile 层实现。

算子选择不得反向削弱本文已确认的行为、保护区、幂等与安全边界。
