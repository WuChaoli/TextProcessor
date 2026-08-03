# Markdown Cleaning Processor Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development` to implement this plan task-by-task. Every task follows RED → GREEN → REFACTOR and ends with an isolated commit.

**Goal:** 在 Worker 内落地确定性的 Markdown 组合清洗 Processor，依次完成段落精确去重、五类敏感信息脱敏和 GFM 格式规范化，并保证保护区、幂等性、资源限制与稳定错误契约。

**Architecture:** Processor 只读写 Worker 提供的 staging `Path`，不接触 API、数据库、Celery、业务 `targetPath` 或网络。`markdown-it-py` 提供块级结构，项目内 source mapper 提供字符级区间；所有变更以不重叠绝对区间倒序应用。Presidio 只运行项目注册的 allowlist recognizer，不加载远程模型；`mdformat-gfm` 之后重新解析并校验保护区和结构不变量。

**Tech Stack:** Python 3.14、markdown-it-py 4、presidio-analyzer 2、mdformat 1、mdformat-gfm 1、pytest、Ruff、Mypy、Pyright、ty。

## Global Constraints

- 契约以 `docs/superpowers/specs/2026-08-03-markdown-cleaning-processor-design.md` 为准。
- 固定顺序：UTF-8 校验 → 解析/保护 → 段落去重 → 脱敏 → GFM 格式化 → 重解析/不变量 → staging 输出。
- 只对普通段落去重；heading、list、blockquote、table 不去重。fenced code、inline code、HTML 全程不脱敏、不改写。
- Presidio 仅允许 `CN_MOBILE_PHONE`、`CN_ID_CARD`、`CREDIT_CARD`、`EMAIL_ADDRESS`、`IPV4_ADDRESS`；禁止默认 NLP 模型下载或任意实体识别。
- Processor 不读取业务 URI，不发布最终文件，不泄露正文、敏感匹配、绝对路径或第三方堆栈。
- 每项实现先看到目标测试失败，再写最小实现；未运行的验证不得声称通过。

## File Structure

```text
backend/app/features/markdown_cleaning/processors/
├── __init__.py
├── protocol.py
├── models.py
├── errors.py
├── markdown_parser.py
├── source_spans.py
├── paragraph_dedup.py
├── cn_recognizers.py
├── presidio_adapter.py
├── markdown_formatter.py
└── pipeline.py
backend/tests/features/markdown_cleaning/processors/
├── conftest.py
├── test_protocol.py
├── test_markdown_parser.py
├── test_paragraph_dedup.py
├── test_cn_recognizers.py
├── test_presidio_adapter.py
├── test_markdown_formatter.py
├── test_pipeline.py
└── test_golden_corpus.py
backend/tests/fixtures/markdown_cleaning/v1/
└── <case>/{input.md,expected.md,summary.json}
```

### Task 1: 依赖兼容性、公共契约与稳定错误

**Files:**
- Modify: `backend/pyproject.toml`
- Modify: `uv.lock`
- Create: `backend/app/features/markdown_cleaning/processors/{__init__.py,protocol.py,models.py,errors.py}`
- Create: `backend/tests/features/markdown_cleaning/processors/{conftest.py,test_protocol.py,test_dependency_compatibility.py}`

**Steps:**
- [ ] RED：测试 `MarkdownCleaningProcessor.process(source_path, destination_path) -> ProcessorResult`、六项统计、稳定 error code，以及第三方依赖在 Python 3.14 下可导入。
- [ ] 运行 `uv run --project backend pytest backend/tests/features/markdown_cleaning/processors/test_protocol.py backend/tests/features/markdown_cleaning/processors/test_dependency_compatibility.py -q`，确认因接口/依赖缺失失败。
- [ ] 直接固定 `markdown-it-py>=4.2,<5`、`presidio-analyzer>=2.2.363,<3`、`mdformat>=1,<2`、`mdformat-gfm>=1,<2`；不安装 `presidio` meta-package，不引入模型下载。
- [ ] 实现第三方无关的 Protocol、不可变 result/summary/span 模型和异常映射。
- [ ] GREEN：重复运行目标测试，并执行 `uv run --project backend ruff check backend/app/features/markdown_cleaning/processors backend/tests/features/markdown_cleaning/processors`。
- [ ] Commit: `功能：建立Markdown清洗Processor契约`

### Task 2: Markdown 块解析、字符级 source span 与保护区

**Files:**
- Create: `backend/app/features/markdown_cleaning/processors/{markdown_parser.py,source_spans.py}`
- Create: `backend/tests/features/markdown_cleaning/processors/test_markdown_parser.py`

**Steps:**
- [ ] RED：覆盖普通段落、heading、list、blockquote、table、fence、inline code、HTML、重复文本位置、CRLF/Unicode、未闭合 fence 和区间重叠。
- [ ] 使用 `MarkdownIt("commonmark").enable("table")` 得到 block line map；项目内 mapper 将叶节点文本映射为绝对字符区间，禁止用模糊 `str.find` 对齐。
- [ ] 对 fenced code、inline code、HTML、Markdown markup/link destination 建立保护区；所有 edit 校验边界与互不重叠后倒序应用。
- [ ] 将不可映射输入映射为 `INVALID_MARKDOWN_INPUT` 或 `MARKDOWN_PARSE_FAILED`，消息不包含正文。
- [ ] GREEN：运行 `uv run --project backend pytest backend/tests/features/markdown_cleaning/processors/test_markdown_parser.py -q`。
- [ ] Commit: `功能：实现Markdown源码区间解析`

### Task 3: 普通段落精确去重

**Files:**
- Create: `backend/app/features/markdown_cleaning/processors/paragraph_dedup.py`
- Create: `backend/tests/features/markdown_cleaning/processors/test_paragraph_dedup.py`

**Steps:**
- [ ] RED：证明只删除第二个及后续普通段落；softbreak/空白归一后相同可去重；大小写、标点、全半角、inline Markdown 不被错误合并；非 paragraph 不参与。
- [ ] 基于 parser block/span 生成 key，删除完整重复段落及受控相邻空行，不改变首次出现内容。
- [ ] 统计 `duplicate_paragraphs_removed`，二次执行必须为零。
- [ ] GREEN：运行目标测试并加入解析回归集。
- [ ] Commit: `功能：实现Markdown段落精确去重`

### Task 4: Presidio allowlist 与五类确定性脱敏

**Files:**
- Create: `backend/app/features/markdown_cleaning/processors/{cn_recognizers.py,presidio_adapter.py}`
- Create: `backend/tests/features/markdown_cleaning/processors/{test_cn_recognizers.py,test_presidio_adapter.py}`

**Steps:**
- [ ] RED：覆盖中国手机号、含日期与 GB11643 校验的身份证、12–19 位 Luhn 银行卡、email、IPv4 octet 校验、无效样本、相邻/重叠、五类优先级及保护区。
- [ ] 构造仅含项目 recognizer 的 `RecognizerRegistry` 和无远程模型的最小 NLP engine；若 Presidio Python 3.14 runtime 不可用，以保持相同 adapter 接口的项目内 pattern recognizer 执行，但依赖兼容失败必须有测试证据并在文档记录。
- [ ] Presidio 只接收 mapper 暴露的普通文本 span；按 `email > ID > phone > card > ipv4` 解决重叠，倒序替换为固定 token。
- [ ] 输出各分类计数；异常统一映射为 `REDACTION_FAILED`，不暴露匹配值或 score。
- [ ] GREEN：运行两个目标测试。
- [ ] Commit: `功能：实现Markdown五类信息脱敏`

### Task 5: GFM 格式化与安全不变量

**Files:**
- Create: `backend/app/features/markdown_cleaning/processors/markdown_formatter.py`
- Create: `backend/tests/features/markdown_cleaning/processors/test_markdown_formatter.py`

**Steps:**
- [ ] RED：覆盖 heading/list/table/blockquote/链接/fence/HTML、保护内容逐字节不变、格式化二次执行字节幂等和格式失败。
- [ ] 使用 mdformat Python API 并显式启用 GFM extension；固定 wrap/numbering/codeformatter 行为，不依赖环境 entrypoint 的隐式默认值。
- [ ] 格式化前后重解析，校验结构类别、链接目标及保护区内容；合法但不能安全规范化时返回 `MARKDOWN_NORMALIZATION_FAILED`，不得静默跳过。
- [ ] 用稳定的语义变更单元统计 `formatting_change_count`。
- [ ] GREEN：运行目标测试。
- [ ] Commit: `功能：实现Markdown安全格式规范化`

### Task 6: 组合流水线、资源限制与 golden corpus

**Files:**
- Create: `backend/app/features/markdown_cleaning/processors/pipeline.py`
- Create: `backend/tests/features/markdown_cleaning/processors/{test_pipeline.py,test_golden_corpus.py}`
- Create: `backend/tests/fixtures/markdown_cleaning/v1/<case>/{input.md,expected.md,summary.json}`
- Create: `backend/tests/integration/markdown_cleaning/test_processor_corpus.py`

**Steps:**
- [ ] RED：固定包含重复段落、五类敏感信息、GFM 格式问题和全部保护区的中文 corpus；逐字节断言输出和完整统计；二次处理输出相同且全部变更计数为零。
- [ ] 实现严格 UTF-8/no BOM/no NUL、输入/输出大小、block/span 数量和处理时限检查；destination 使用临时文件写入并原子替换，但只限 staging。
- [ ] 串接固定流水线，任何阶段失败不得留下伪完整结果；返回 source/output hash、字节数和统计。
- [ ] GREEN：运行 `uv run --project backend pytest backend/tests/features/markdown_cleaning/processors backend/tests/integration/markdown_cleaning/test_processor_corpus.py -q`。
- [ ] 执行 `uv run --project backend ruff check ...`、`uv run --project backend mypy app/features/markdown_cleaning/processors`、项目既有 Pyright/ty 目标命令。
- [ ] Commit: `功能：完成Markdown组合清洗Processor`

### Task 7: Processor 独立审查与修复闭环

- [ ] 使用 fresh reviewer 对照 processor spec 审查正确性、安全性、幂等性、测试遗漏与依赖边界。
- [ ] 对每项 finding 重新走 RED → GREEN，由 implementer 修复、原 reviewer 复核。
- [ ] 重新运行 Processor 全测试及 Ruff、Mypy、Pyright、ty；记录真实命令、通过数和未覆盖边界。
- [ ] Commit: `修复：收紧Markdown清洗Processor契约`
