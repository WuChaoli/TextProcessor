## Task 4: Presidio allowlist 与五类脱敏（Final）

### Result: DONE (Fix Round 1/5)

- **RED**
  - 补齐 `CN Mobile ID/IPv4/Bank Card/Email` 的识别与校验分支，修复 Presidio 可选依赖下的静态类型边界与 fallback 兼容。
  - 复测修正项：
    - 主路径 `analyze()` 运行时异常需要抛出 `MarkdownCleaningProcessorError(code=SENSITIVE_DATA_REDACTION_FAILED)`。
    - `start/end` 需统一通过 `0 <= start < end <= len(markdown)` 校验；`EMAIL_ADDRESS` 同时要做项目 `EMAIL_PATTERN.fullmatch`。
    - `EmailRecognizer` 缺失时不能回退成四类识别，需提供 `_FallbackEmailRecognizer` 并注册到 `RecognizerRegistry`。

- **GREEN**
  - 已完成类型与行为收敛，主路径/异常路径语义修正为：
    - 有主路径可用（`_HAS_PRESIDIO=True` 且 analyzer 可建）时，返回 `used_fallback=False`。
    - 主路径执行异常时抛出 `MarkdownCleaningProcessorError`，code 固定为 `SENSITIVE_DATA_REDACTION_FAILED`，不返回成功对象。
    - 主路径不可用时走本地 fallback 规则并写 `used_fallback=True`。
    - `CN/Email/IPv4` 主路径结果统一进行边界与内容校验；越界、长度不合法、邮箱不完全匹配的结果会被丢弃。
    - 当 Presidio 内置 `EmailRecognizer` 不可用时，`_build_registry()` 注册 `_FallbackEmailRecognizer`，实现五类稳定识别器。
  - 导出接口稳定：`CNIDCardRecognizer`、`CNMobilePhoneRecognizer`、`IPv4AddressRecognizer` 在 `processors/__init__.py` 中可导出。

### Presidio 主路径与 registry/no-download 证据

- 主路径构造依赖 `NoOpNlpEngine`，不依赖模型下载：
  - [backend/app/features/markdown_cleaning/processors/presidio_adapter.py](C:/Users/wuchaoli/Desktop/codespace/TextProcessor/.worktrees/markdown-cleaning-api/backend/app/features/markdown_cleaning/processors/presidio_adapter.py)
    - `NoOpNlpEngine([{"lang_code": "en", "model_name": "noop-en"}])`
    - `AnalyzerEngine(registry=..., nlp_engine=...)`
- Registry 注册证据（主路径）：
  - 同文件 `_build_registry()` 先后添加：
    - `EmailRecognizer`（若可用）或 `_FallbackEmailRecognizer`
    - `CreditCardRecognizer`（若可用）或 `_FallbackCreditCardRecognizer`
    - `CNMobilePhoneRecognizer`、`CNIDCardRecognizer`、`IPv4AddressRecognizer`
- no-download/本地化证据：
  - 测试 `test_presidio_main_path_uses_noop_nlp_and_allowlist_registry` 断言 `analyzer.nlp_engine.models == [{"lang_code": "en", "model_name": "noop-en"}]`，且 registry 包含固定 recognizer 名单（无远程拉模型）。

### 异常不 fallback 语义证据

- [backend/app/features/markdown_cleaning/processors/presidio_adapter.py](C:/Users/wuchaoli/Desktop/codespace/TextProcessor/.worktrees/markdown-cleaning-api/backend/app/features/markdown_cleaning/processors/presidio_adapter.py)
  - `redact()` 在 `except` 中：
    - `if self._analyzer is not None:` 分支改为 `raise MarkdownCleaningProcessorError(code=SENSITIVE_DATA_REDACTION_FAILED)`；
    - 仅当 `self._analyzer is None` 时执行 `_fallback_matches()` 并返回 `used_fallback=True`。
- [backend/tests/features/markdown_cleaning/processors/test_presidio_adapter.py](C:/Users/wuchaoli/Desktop/codespace/TextProcessor/.worktrees/markdown-cleaning-api/backend/tests/features/markdown_cleaning/processors/test_presidio_adapter.py)
  - `test_redact_runtime_analyze_error_raises_processor_error` 断言主路径异常抛 `MarkdownCleaningProcessorError`（code=`SENSITIVE_DATA_REDACTION_FAILED`）。
  - `test_main_path_filters_invalid_spans_and_keeps_valid_text_and_summary` 断言越界/非法 span 被丢弃，且文本与命中计数正确。
  - `test_fallback_registry_registers_credit_card_recognizer_when_builtin_missing` 断言 `_FallbackEmailRecognizer` 与 `_FallbackCreditCardRecognizer` 同时注册。

### 验证与证据

- `uv run ruff check backend/app/features/markdown_cleaning/processors/cn_recognizers.py backend/app/features/markdown_cleaning/processors/presidio_adapter.py backend/tests/features/markdown_cleaning/processors/test_presidio_adapter.py backend/tests/features/markdown_cleaning/processors/test_cn_recognizers.py`
- `uv run mypy backend/app/features/markdown_cleaning/processors/cn_recognizers.py backend/app/features/markdown_cleaning/processors/presidio_adapter.py backend/tests/features/markdown_cleaning/processors/test_presidio_adapter.py backend/tests/features/markdown_cleaning/processors/test_cn_recognizers.py`
- `uv run ty check backend/app/features/markdown_cleaning/processors/cn_recognizers.py backend/app/features/markdown_cleaning/processors/presidio_adapter.py backend/tests/features/markdown_cleaning/processors/test_presidio_adapter.py backend/tests/features/markdown_cleaning/processors/test_cn_recognizers.py`
- `uv run pyright backend/app/features/markdown_cleaning/processors/cn_recognizers.py backend/app/features/markdown_cleaning/processors/presidio_adapter.py backend/tests/features/markdown_cleaning/processors/test_presidio_adapter.py backend/tests/features/markdown_cleaning/processors/test_cn_recognizers.py`
- `uv run pytest backend/tests/features/markdown_cleaning/processors/test_cn_recognizers.py backend/tests/features/markdown_cleaning/processors/test_presidio_adapter.py`（设置 `PROJECT_NAME/POSTGRES_SERVER/POSTGRES_USER/FIRST_SUPERUSER/FIRST_SUPERUSER_PASSWORD`）

### 文件变更

- [backend/app/features/markdown_cleaning/processors/__init__.py](C:/Users/wuchaoli/Desktop/codespace/TextProcessor/.worktrees/markdown-cleaning-api/backend/app/features/markdown_cleaning/processors/__init__.py)（本任务导出改动）
- [backend/app/features/markdown_cleaning/processors/cn_recognizers.py](C:/Users/wuchaoli/Desktop/codespace/TextProcessor/.worktrees/markdown-cleaning-api/backend/app/features/markdown_cleaning/processors/cn_recognizers.py)
- [backend/app/features/markdown_cleaning/processors/presidio_adapter.py](C:/Users/wuchaoli/Desktop/codespace/TextProcessor/.worktrees/markdown-cleaning-api/backend/app/features/markdown_cleaning/processors/presidio_adapter.py)
- [backend/tests/features/markdown_cleaning/processors/test_presidio_adapter.py](C:/Users/wuchaoli/Desktop/codespace/TextProcessor/.worktrees/markdown-cleaning-api/backend/tests/features/markdown_cleaning/processors/test_presidio_adapter.py)
- [backend/tests/features/markdown_cleaning/processors/test_cn_recognizers.py](C:/Users/wuchaoli/Desktop/codespace/TextProcessor/.worktrees/markdown-cleaning-api/backend/tests/features/markdown_cleaning/processors/test_cn_recognizers.py)

### 备注

- 该任务提交状态最终为 DONE；未发现阻断性风险；已保留现有导出兼容与 API 契约。
